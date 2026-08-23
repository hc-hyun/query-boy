from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from query_man.errors import (
    SourceControlUnavailableError,
    SourceNotFoundError,
    SourceValidationError,
)
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, PreparedMetadata, SourceProfile
from query_man.operations import operations
from query_man.query import QueryService
from query_man.registry import (
    RegistryConfigurationError,
    SourceRegistry,
    load_budget_profiles,
    validate_source_manifest,
)
from query_man.secrets import EncryptedSecret, SourceSecretCipher
from query_man.source_admin import SourceAdminService, SourceReloader
from query_man.source_store import (
    SourceCatalogConnection,
    SourceCatalogPage,
    SourceCatalogRecord,
    SourceGenerationConflictError,
    SourceGenerationPage,
    SourcePublishPinnedError,
    StoredSource,
    StoredSourceNotFoundError,
)
from query_man.sql_validation import ValidatedSql
from query_man.verified import ExpectedResult, VerifiedQuery, create_result_hash
from tests.helpers import ROOT_DIRECTORY, minimal_development_snapshot


class MemoryMetadataStore:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], PreparedMetadata] = {}
        self.active: dict[str, str] = {}
        self.pinned: set[str] = set()

    async def get_revision(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        return self.values[(source.source_id, revision)]

    async def get_active(self, source: SourceProfile) -> PreparedMetadata | None:
        revision = self.active.get(source.source_id)
        return None if revision is None else self.values[(source.source_id, revision)]

    async def publish(self, source: SourceProfile, value: PreparedMetadata) -> PreparedMetadata:
        self.values[(source.source_id, value.revision)] = value
        if source.source_id not in self.pinned:
            self.active[source.source_id] = value.revision
        return self.values[(source.source_id, self.active[source.source_id])]

    async def activate(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        self.active[source.source_id] = revision
        self.pinned.add(source.source_id)
        return self.values[(source.source_id, revision)]

    async def unpin(self, source: SourceProfile) -> None:
        self.pinned.discard(source.source_id)

    async def close(self) -> None:
        pass


class MemorySourceStore:
    def __init__(self, metadata: MemoryMetadataStore) -> None:
        self.metadata = metadata
        self.active: dict[str, StoredSource] = {}
        self.history: dict[tuple[str, int], StoredSource] = {}
        self.verified: list[VerifiedQuery] = []

    async def list_active(self) -> list[StoredSource]:
        return list(self.active.values())

    async def get_active(self, source_id: str) -> StoredSource | None:
        return self.active.get(source_id)

    async def get_revision(self, source_id: str, generation: int) -> StoredSource:
        try:
            return replace(self.history[(source_id, generation)], state_version=0)
        except KeyError as error:
            raise StoredSourceNotFoundError from error

    async def list_catalog(
        self,
        *,
        after_source_id: str | None = None,
        limit: int = 50,
        enabled: bool | None = None,
        owner: str | None = None,
        environment: str | None = None,
        budget_profile: str | None = None,
    ) -> SourceCatalogPage:
        records = [
            self._catalog_record(record)
            for source_id, record in sorted(self.active.items())
            if (after_source_id is None or source_id > after_source_id)
        ]
        records = [
            record
            for record in records
            if (enabled is None or record.enabled is enabled)
            and (owner is None or record.owner == owner)
            and (environment is None or record.environment == environment)
            and (budget_profile is None or record.budget_profile == budget_profile)
        ]
        return SourceCatalogPage(
            tuple(records[:limit]),
            records[limit - 1].source_id if len(records) > limit else None,
        )

    async def get_catalog(self, source_id: str) -> SourceCatalogRecord | None:
        record = self.active.get(source_id)
        return None if record is None else self._catalog_record(record)

    async def list_generation_history(
        self,
        source_id: str,
        *,
        before_generation: int | None = None,
        limit: int = 50,
    ) -> SourceGenerationPage | None:
        records = [
            self._catalog_record(record)
            for (current_source_id, generation), record in sorted(
                self.history.items(),
                key=lambda item: item[0][1],
                reverse=True,
            )
            if current_source_id == source_id
            and (before_generation is None or generation < before_generation)
        ]
        if source_id not in self.active:
            return None
        return SourceGenerationPage(
            current=self._catalog_record(self.active[source_id]),
            items=tuple(records[:limit]),
            next_before_generation=(
                records[limit - 1].generation if len(records) > limit else None
            ),
        )

    def _catalog_record(self, revision: StoredSource) -> SourceCatalogRecord:
        active = self.active[revision.source_id]
        manifest = revision.manifest
        provenance = manifest["provenance"]
        connection = manifest["connection"]
        semantic = manifest.get("semantic_overlay", {})
        assert isinstance(provenance, dict)
        assert isinstance(connection, dict)
        assert isinstance(semantic, dict)
        allowed_schemas = manifest["allowed_schemas"]
        allowed_relation_kinds = manifest["allowed_relation_kinds"]
        assert isinstance(allowed_schemas, list)
        assert isinstance(allowed_relation_kinds, list)
        activated_at = datetime(2026, 8, 23, tzinfo=UTC) + timedelta(
            seconds=active.state_version
        )
        return SourceCatalogRecord(
            source_id=revision.source_id,
            generation=revision.generation,
            enabled=active.enabled,
            state_version=active.state_version,
            activated_at=activated_at,
            generation_created_at=datetime(2026, 8, 23, tzinfo=UTC)
            + timedelta(seconds=revision.generation),
            name=str(manifest["name"]),
            description=str(manifest["description"]),
            owner=str(provenance["owner"]),
            environment=str(provenance["environment"]),
            database_migration_ref=str(provenance["database_migration_ref"]),
            budget_profile=str(manifest["budget_profile"]),
            minimum_quality_level=str(manifest.get("minimum_quality_level", "L0")),
            tenant_isolation=str(manifest.get("tenant_isolation", "none")),
            connection=SourceCatalogConnection(
                host=str(connection["host"]),
                port=int(connection["port"]),
                database=str(connection["database"]),
                user=str(connection["user"]),
                ssl=bool(connection.get("ssl", False)),
            ),
            allowed_schemas=tuple(str(value) for value in allowed_schemas),
            allowed_relation_kinds=tuple(
                str(value) for value in allowed_relation_kinds
            ),
            semantic_default_relation=(
                str(semantic["default_relation"])
                if semantic.get("default_relation") is not None
                else None
            ),
            semantic_relation_count=len(semantic.get("relations", [])),
            semantic_join_count=len(semantic.get("joins", [])),
            semantic_business_term_count=len(semantic.get("business_terms", [])),
            semantic_question_rule_count=len(semantic.get("question_rules", [])),
            semantic_composition_hint_count=len(
                semantic.get("composition_hints", [])
            ),
            published_metadata_revision=revision.metadata_revision,
            active_metadata_revision=self.metadata.active.get(revision.source_id),
            metadata_pinned=revision.source_id in self.metadata.pinned,
            metadata_activated_at=activated_at,
            is_current=active.generation == revision.generation,
        )

    async def next_generation(self, source_id: str) -> int:
        generations = [
            generation
            for current_source, generation in self.history
            if current_source == source_id
        ]
        return max(generations, default=0) + 1

    async def publish(
        self,
        source_id: str,
        expected_generation: int,
        generation: int,
        manifest: dict[str, object],
        encrypted_secret: EncryptedSecret,
        metadata: PreparedMetadata,
        *,
        expected_state_version: int,
    ) -> StoredSource:
        current = self.active.get(source_id)
        current_generation = 0 if current is None else current.generation
        current_state_version = 0 if current is None else current.state_version
        if (
            expected_generation != current_generation
            or expected_state_version != current_state_version
        ):
            raise SourceGenerationConflictError
        if source_id in self.metadata.pinned:
            raise SourcePublishPinnedError
        revision = StoredSource(
            source_id,
            generation,
            manifest,
            encrypted_secret,
            metadata.revision,
            True,
            0,
        )
        record = replace(revision, state_version=current_state_version + 1)
        self.metadata.values[(source_id, metadata.revision)] = metadata
        self.metadata.active[source_id] = metadata.revision
        self.history[(source_id, generation)] = revision
        self.active[source_id] = record
        return record

    async def deactivate(
        self,
        source_id: str,
        expected_generation: int,
        *,
        expected_state_version: int,
    ) -> int:
        current = self.active[source_id]
        if (
            current.generation != expected_generation
            or current.state_version != expected_state_version
            or not current.enabled
        ):
            raise SourceGenerationConflictError
        state_version = current.state_version + 1
        self.active[source_id] = replace(
            current,
            enabled=False,
            state_version=state_version,
        )
        return state_version

    async def rollback(
        self,
        source_id: str,
        generation: int,
        expected_generation: int,
        *,
        expected_state_version: int,
    ) -> StoredSource:
        current = self.active[source_id]
        if (
            current.generation != expected_generation
            or current.state_version != expected_state_version
        ):
            raise SourceGenerationConflictError
        record = replace(
            self.history[(source_id, generation)],
            enabled=True,
            state_version=current.state_version + 1,
        )
        self.active[source_id] = record
        self.metadata.active[source_id] = record.metadata_revision
        self.metadata.pinned.add(source_id)
        return record

    async def close(self) -> None:
        pass

    async def publish_verified_query(self, query: VerifiedQuery) -> None:
        self.verified.append(query)

    async def verified_revision_map(self) -> dict[str, frozenset[str]]:
        return {
            source_id: frozenset(
                query.metadata_revision
                for query in self.verified
                if query.source_id == source_id
            )
            for source_id in {query.source_id for query in self.verified}
        }


class StaticCatalog:
    def __init__(self, snapshot: CatalogSnapshot | None = None) -> None:
        self.snapshot = snapshot or minimal_development_snapshot()

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot

    async def close(self) -> None:
        pass


class SwitchingCatalogFactory:
    def __init__(self) -> None:
        self.snapshot = minimal_development_snapshot()

    def __call__(self) -> StaticCatalog:
        return StaticCatalog(self.snapshot)


class RecordingInvalidator:
    def __init__(self) -> None:
        self.source_ids: list[str] = []

    async def invalidate(self, source_id: str) -> None:
        self.source_ids.append(source_id)


class StaticQueryExecutor:
    async def execute(
        self,
        _source: SourceProfile,
        _sql: str,
        metadata_revision: str,
        _validated: ValidatedSql,
        *,
        query_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, object]:
        rows = [{"status": "OPEN"}]
        return {
            "status": "ok",
            "query_id": query_id or "verified-query-test",
            "metadata_revision": metadata_revision,
            "fingerprint": "test",
            "columns": ["status"],
            "rows": rows,
            "row_count": 1,
            "result_bytes": 19,
            "truncated": False,
            "queue_ms": 0,
            "elapsed_ms": 1,
            "plan_summary": {"total_cost": 1.0, "max_rows": 1, "node_count": 1},
        }

    async def cancel(self, _query_id: str) -> bool:
        return False

    async def close(self) -> None:
        pass


def _manifest() -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["source_id"] = "third-source"
    raw["connection"]["password_env"] = "THIRD_SOURCE_READER_PASSWORD"
    raw["minimum_quality_level"] = "L0"
    return raw


def _services(
    catalog_factory: object = StaticCatalog,
) -> tuple[
    SourceAdminService,
    SourceRegistry,
    MemorySourceStore,
    RecordingInvalidator,
    SourceSecretCipher,
    SourceReloader,
]:
    registry = SourceRegistry([])
    metadata_store = MemoryMetadataStore()
    source_store = MemorySourceStore(metadata_store)
    metadata = MetadataService(registry, StaticCatalog(), store=metadata_store)
    cipher = SourceSecretCipher(b"a" * 32)
    invalidator = RecordingInvalidator()
    budgets = load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml")
    verified_revisions: dict[str, frozenset[str]] = {}
    reloader = SourceReloader(
        registry,
        metadata,
        metadata_store,
        source_store,
        cipher,
        budgets,
        verified_revisions,
        (invalidator,),
    )
    admin = SourceAdminService(
        source_store,
        reloader,
        metadata,
        QueryService(registry, metadata, StaticQueryExecutor()),
        cipher,
        budgets,
        verified_revisions,
        catalog_factory,  # type: ignore[arg-type]
    )
    return admin, registry, source_store, invalidator, cipher, reloader


@pytest.mark.asyncio
async def test_publish_rotate_deactivate_and_rollback_apply_without_restart() -> None:
    admin, registry, store, invalidator, _cipher, _reloader = _services()

    published = await admin.publish("third-source", _manifest(), "first-secret")
    assert published["generation"] == 1
    assert registry.get("third-source") is not None
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]
    assert registry.get("third-source").control_generation == 1  # type: ignore[union-attr]
    assert registry.get("third-source").control_state_version == 1  # type: ignore[union-attr]

    rotated = await admin.rotate_credential("third-source", "rotated-secret")
    assert rotated["generation"] == 2
    assert registry.get("third-source").connection.password == "rotated-secret"  # type: ignore[union-attr]
    assert registry.get("third-source").control_generation == 2  # type: ignore[union-attr]
    assert registry.get("third-source").control_state_version == 2  # type: ignore[union-attr]

    await admin.deactivate("third-source")
    assert registry.get("third-source") is None

    rolled_back = await admin.rollback("third-source", 1)
    assert rolled_back["generation"] == 1
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]
    assert registry.get("third-source").control_generation == 1  # type: ignore[union-attr]
    assert registry.get("third-source").control_state_version == 4  # type: ignore[union-attr]
    assert store.active["third-source"].generation == 1
    assert store.active["third-source"].state_version == 4
    assert invalidator.source_ids == ["third-source"] * 4


@pytest.mark.asyncio
async def test_credential_rotation_rejects_disabled_source_without_reactivation() -> None:
    admin, registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    await admin.deactivate("third-source")
    before = store.active["third-source"]

    with pytest.raises(SourceValidationError):
        await admin.rotate_credential("third-source", "rotated-secret")

    assert store.active["third-source"] == before
    assert store.active["third-source"].enabled is False
    assert registry.get("third-source") is None


@pytest.mark.asyncio
async def test_rollback_pin_blocks_publish_until_operator_resumes() -> None:
    admin, registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    await admin.publish("third-source", _manifest(), "second-secret")
    await admin.rollback("third-source", 1)
    pinned = store.active["third-source"]

    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", _manifest(), "blocked-secret")

    assert store.active["third-source"] == pinned
    assert "third-source" in store.metadata.pinned

    resumed = await admin.resume_automatic_publish("third-source")
    published = await admin.publish("third-source", _manifest(), "resumed-secret")

    assert resumed == {"status": "resumed", "source_id": "third-source"}
    assert "third-source" not in store.metadata.pinned
    assert published["generation"] == 3
    assert store.active["third-source"].state_version == 4
    assert registry.get("third-source").connection.password == "resumed-secret"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reloader_rejects_older_and_equal_conflicting_state() -> None:
    admin, registry, store, invalidator, _cipher, reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    stale = store.active["third-source"]
    await admin.publish("third-source", _manifest(), "second-secret")
    current = store.active["third-source"]

    with pytest.raises(SourceGenerationConflictError):
        await reloader.apply(stale)
    with pytest.raises(SourceGenerationConflictError):
        await reloader.apply(replace(stale, state_version=current.state_version))

    assert registry.get("third-source").connection.password == "second-secret"  # type: ignore[union-attr]
    assert invalidator.source_ids == ["third-source"] * 2


@pytest.mark.asyncio
async def test_rollback_rejects_revision_with_different_connection_identity() -> None:
    admin, registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    await admin.publish("third-source", _manifest(), "second-secret")
    before = store.active["third-source"]
    candidate = store.history[("third-source", 1)]
    connection = candidate.manifest["connection"]
    assert isinstance(connection, dict)
    rebound_manifest = {
        **candidate.manifest,
        "connection": {**connection, "host": "alternate-database.example"},
    }
    store.history[("third-source", 1)] = replace(
        candidate,
        manifest=rebound_manifest,
    )

    with pytest.raises(SourceValidationError):
        await admin.rollback("third-source", 1)

    assert store.active["third-source"] == before
    assert registry.get("third-source").connection.password == "second-secret"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reloader_rejects_candidate_with_different_connection_identity() -> None:
    admin, registry, store, invalidator, _cipher, reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    current = store.active["third-source"]
    connection = current.manifest["connection"]
    assert isinstance(connection, dict)
    rebound = replace(
        current,
        manifest={
            **current.manifest,
            "connection": {**connection, "host": "alternate-database.example"},
        },
        state_version=current.state_version + 1,
    )

    with pytest.raises(RegistryConfigurationError):
        await reloader.apply(rebound)

    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]
    assert invalidator.source_ids == ["third-source"]


@pytest.mark.asyncio
async def test_failed_staging_preserves_current_source() -> None:
    catalog_factory = SwitchingCatalogFactory()
    admin, registry, store, _invalidator, _cipher, _reloader = _services(
        catalog_factory
    )
    await admin.publish("third-source", _manifest(), "first-secret")
    before = store.active["third-source"]

    incomplete = minimal_development_snapshot()
    incomplete.relations = incomplete.relations[:1]
    catalog_factory.snapshot = incomplete

    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", _manifest(), "bad-update-secret")

    assert store.active["third-source"] == before
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_failed_staging_does_not_pollute_production_health() -> None:
    operations.reset()
    try:
        catalog_factory = SwitchingCatalogFactory()
        admin, _registry, _store, _invalidator, _cipher, _reloader = _services(
            catalog_factory
        )
        await admin.publish("third-source", _manifest(), "first-secret")
        assert operations.snapshot()["sources"] == {"third-source": "healthy"}

        incomplete = minimal_development_snapshot()
        incomplete.relations = incomplete.relations[:1]
        catalog_factory.snapshot = incomplete
        with pytest.raises(SourceValidationError):
            await admin.publish("third-source", _manifest(), "bad-update-secret")

        assert operations.snapshot()["sources"] == {"third-source": "healthy"}
        assert operations.public_status() == "ready"
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_dynamic_apply_and_deactivate_reconcile_source_inventory() -> None:
    operations.reset()
    try:
        admin, _registry, _store, _invalidator, _cipher, _reloader = _services()

        await admin.publish("third-source", _manifest(), "first-secret")
        assert operations.snapshot()["sources"] == {"third-source": "healthy"}

        await admin.deactivate("third-source")
        operations.set_source_health("third-source", "unavailable")
        assert operations.snapshot()["sources"] == {}
        assert operations.public_status() == "unavailable"

        await admin.rollback("third-source", 1)
        assert operations.snapshot()["sources"] == {"third-source": "healthy"}
        assert operations.public_status() == "ready"
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_initial_reload_scan_failure_keeps_managed_registry_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations.reset()
    try:
        _admin, registry, store, _invalidator, _cipher, reloader = _services()
        operations.reconcile_sources(registry.source_ids())

        async def fail_scan() -> list[StoredSource]:
            raise RuntimeError("control database unavailable")

        monkeypatch.setattr(store, "list_active", fail_scan)
        await reloader.sync()

        snapshot = operations.snapshot()
        assert registry.source_ids() == frozenset()
        assert snapshot["sources"] == {}
        assert snapshot["components"] == {"source_reload": "unavailable"}
        assert operations.public_status() == "unavailable"
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_reload_scan_failure_degrades_but_keeps_usable_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations.reset()
    try:
        admin, _registry, store, _invalidator, _cipher, reloader = _services()
        await admin.publish("third-source", _manifest(), "first-secret")

        async def fail_scan() -> list[StoredSource]:
            raise RuntimeError("control database unavailable")

        successful_scan = store.list_active
        monkeypatch.setattr(store, "list_active", fail_scan)
        await reloader.sync()

        snapshot = operations.snapshot()
        assert snapshot["sources"] == {"third-source": "healthy"}
        assert snapshot["components"] == {"source_reload": "unavailable"}
        assert any(
            metric["name"] == "source_reload_scan_failed"
            and metric["value"] == 1
            for metric in snapshot["metrics"]
        )
        assert operations.public_status() == "degraded"

        monkeypatch.setattr(store, "list_active", successful_scan)
        await reloader.sync()
        assert operations.snapshot()["components"] == {"source_reload": "healthy"}
        assert operations.public_status() == "ready"
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_reload_apply_failure_keeps_component_degraded_until_clean_scan() -> None:
    operations.reset()
    try:
        admin, registry, store, _invalidator, _cipher, reloader = _services()
        await admin.publish("third-source", _manifest(), "first-secret")
        current = store.active["third-source"]
        connection = current.manifest["connection"]
        assert isinstance(connection, dict)
        store.active["third-source"] = replace(
            current,
            manifest={
                **current.manifest,
                "connection": {
                    **connection,
                    "host": "alternate-database.example",
                },
            },
            state_version=current.state_version + 1,
        )

        await reloader.sync()

        snapshot = operations.snapshot()
        assert registry.get("third-source") is not None
        assert registry.get("third-source").connection.host == "127.0.0.1"  # type: ignore[union-attr]
        assert snapshot["sources"] == {"third-source": "healthy"}
        assert snapshot["components"] == {"source_reload": "unavailable"}
        assert any(
            metric["name"] == "source_reload_apply_failed"
            and metric["source_id"] == "third-source"
            and metric["value"] == 1
            for metric in snapshot["metrics"]
        )
        assert operations.public_status() == "degraded"

        store.active["third-source"] = current
        await reloader.sync()
        assert operations.snapshot()["components"] == {"source_reload": "healthy"}
        assert operations.public_status() == "ready"
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_connection_identity_change_is_rejected_without_reusing_verification() -> None:
    admin, registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    before = store.active["third-source"]
    rebound = _manifest()
    rebound["connection"]["host"] = "alternate-database.example"  # type: ignore[index]

    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", rebound, "second-secret")

    assert store.active["third-source"] == before
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_environment_change_is_rejected_for_the_same_source_identity() -> None:
    admin, registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    before = store.active["third-source"]
    rebound = _manifest()
    provenance = rebound["provenance"]
    assert isinstance(provenance, dict)
    provenance["environment"] = "production"

    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", rebound, "second-secret")

    assert store.active["third-source"] == before
    assert registry.get("third-source").provenance.environment == "development"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_admin_read_models_are_paginated_and_secret_free() -> None:
    admin, _registry, _store, _invalidator, _cipher, _reloader = _services()
    first = await admin.publish("third-source", _manifest(), "first-secret")
    republished = _manifest()
    provenance = republished["provenance"]
    assert isinstance(provenance, dict)
    provenance["owner"] = "data-platform"
    provenance["database_migration_ref"] = "migrations/development-issues/0042"

    second = await admin.publish("third-source", republished, "second-secret")

    assert second["generation"] == 2
    assert second["metadata_revision"] == first["metadata_revision"]
    listed = await admin.list_sources(
        1,
        owner="data-platform",
        environment="development",
        budget_profile="interactive",
    )
    assert listed["next_after_source_id"] is None
    assert listed["sources"] == [
        {
            "source_id": "third-source",
            "name": "개발 문제점",
            "description": "개발 및 검증 과정에서 발견한 문제, 원인, 대책과 댓글",
            "owner": "data-platform",
            "environment": "development",
            "enabled": True,
            "generation": 2,
            "state_version": 2,
            "activated_at": datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
            "budget_profile": "interactive",
            "minimum_quality_level": "L0",
            "published_metadata_revision": first["metadata_revision"],
            "active_metadata_revision": first["metadata_revision"],
            "metadata_pinned": False,
        }
    ]
    assert await admin.list_sources(owner="another-owner") == {
        "sources": [],
        "next_after_source_id": None,
    }

    detail = await admin.get_source("third-source")
    assert detail["database_migration_ref"] == "migrations/development-issues/0042"
    assert detail["connection"] == {
        "host": "127.0.0.1",
        "port": 5432,
        "database": "development_issues",
        "user": "development_issues_reader",
        "ssl": False,
    }
    assert detail["semantic_summary"] == {
        "default_relation": "ai.issue_overview",
        "relation_count": 3,
        "join_count": 1,
        "business_term_count": 3,
        "question_rule_count": 1,
        "composition_hint_count": 1,
    }
    limits = detail["effective_budget_limits"]
    assert isinstance(limits, dict)
    assert limits["name"] == "interactive"
    assert limits["version"] == 2
    assert limits["max_result_rows"] == 1_000
    assert limits["max_concurrent_queries"] == 2

    history = await admin.source_history("third-source", 1)
    assert history["next_before_generation"] == 2
    assert history["generations"] == [
        {
            "generation": 2,
            "generation_created_at": datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC),
            "owner": "data-platform",
            "environment": "development",
            "database_migration_ref": "migrations/development-issues/0042",
            "budget_profile": "interactive",
            "published_metadata_revision": first["metadata_revision"],
            "minimum_quality_level": "L0",
            "is_current": True,
        }
    ]
    older = await admin.source_history("third-source", before_generation=2)
    assert [item["generation"] for item in older["generations"]] == [1]  # type: ignore[index]
    assert older["generations"][0]["is_current"] is False  # type: ignore[index]

    rolled_back = await admin.rollback("third-source", 1)
    restored = await admin.get_source("third-source")
    restored_history = await admin.source_history("third-source")
    assert rolled_back["generation"] == 1
    assert restored["owner"] == "query-man"
    assert (
        restored["database_migration_ref"]
        == "docker/postgres/init/10-development-issues-schema.sql"
    )
    assert restored["metadata_pinned"] is True
    assert [
        item["generation"] for item in restored_history["generations"]  # type: ignore[index]
    ] == [2, 1]
    assert [
        item["is_current"] for item in restored_history["generations"]  # type: ignore[index]
    ] == [False, True]
    assert restored_history["current"]["generation"] == 1  # type: ignore[index]

    rendered = repr((listed, detail, history, older, restored, restored_history))
    assert "first-secret" not in rendered
    assert "second-secret" not in rendered
    assert "THIRD_SOURCE_READER_PASSWORD" not in rendered
    assert "encrypted_secret" not in rendered
    assert "manifest" not in rendered


@pytest.mark.asyncio
async def test_source_history_uses_one_atomic_store_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    await admin.publish("third-source", _manifest(), "second-secret")

    async def reject_split_read(_source_id: str) -> SourceCatalogRecord | None:
        raise AssertionError("source history must not read the current pointer separately")

    monkeypatch.setattr(store, "get_catalog", reject_split_read)
    history = await admin.source_history("third-source")
    exhausted = await admin.source_history(
        "third-source",
        before_generation=1,
    )

    assert history["current"]["generation"] == 2  # type: ignore[index]
    assert [
        item["generation"]
        for item in history["generations"]  # type: ignore[union-attr]
        if item["is_current"]
    ] == [2]
    assert exhausted["current"]["generation"] == 2  # type: ignore[index]
    assert exhausted["generations"] == []
    assert exhausted["next_before_generation"] is None


@pytest.mark.asyncio
async def test_admin_read_service_maps_missing_and_unavailable_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()

    with pytest.raises(SourceNotFoundError):
        await admin.get_source("unknown-source")
    with pytest.raises(SourceNotFoundError):
        await admin.source_history("unknown-source")

    async def fail_get(_source_id: str) -> SourceCatalogRecord | None:
        raise RuntimeError("private control database detail")

    async def fail_list(**_filters: object) -> SourceCatalogPage:
        raise RuntimeError("private control database detail")

    monkeypatch.setattr(store, "get_catalog", fail_get)
    with pytest.raises(SourceControlUnavailableError):
        await admin.get_source("unknown-source")

    async def fail_history(
        _source_id: str,
        **_page: object,
    ) -> SourceGenerationPage | None:
        raise RuntimeError("private control database detail")

    monkeypatch.setattr(store, "list_generation_history", fail_history)
    with pytest.raises(SourceControlUnavailableError):
        await admin.source_history("unknown-source")

    monkeypatch.setattr(store, "list_catalog", fail_list)
    with pytest.raises(SourceControlUnavailableError):
        await admin.list_sources()


@pytest.mark.asyncio
async def test_first_control_publish_allows_matching_bootstrap_connection_identity() -> None:
    admin, registry, store, _invalidator, _cipher, _reloader = _services()
    manifest = _manifest()
    budgets = load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml")
    registry.upsert(validate_source_manifest(manifest, budgets, "bootstrap-secret").profile)
    assert registry.get("third-source").control_generation is None  # type: ignore[union-attr]

    published = await admin.publish("third-source", manifest, "control-secret")

    assert published["generation"] == 1
    assert store.active["third-source"].generation == 1
    assert registry.get("third-source").connection.password == "control-secret"  # type: ignore[union-attr]
    assert registry.get("third-source").control_generation == 1  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("host", "alternate-database.example"),
        ("port", 6543),
        ("database", "alternate_database"),
        ("user", "alternate_reader"),
        ("ssl", True),
    ],
)
@pytest.mark.asyncio
async def test_first_control_publish_rejects_bootstrap_connection_identity_change(
    field: str,
    value: object,
) -> None:
    admin, registry, store, _invalidator, _cipher, _reloader = _services()
    manifest = _manifest()
    budgets = load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml")
    registry.upsert(validate_source_manifest(manifest, budgets, "bootstrap-secret").profile)
    rebound = _manifest()
    rebound["connection"][field] = value  # type: ignore[index]
    if field == "port":
        rebound["connection"].pop("port_env", None)  # type: ignore[union-attr]

    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", rebound, "control-secret")

    assert store.active == {}
    assert registry.get("third-source").connection.password == "bootstrap-secret"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_reloader_applies_external_generation() -> None:
    admin, _registry, store, _invalidator, cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    registry = SourceRegistry([])
    metadata = MetadataService(registry, StaticCatalog(), store=store.metadata)
    reloader = SourceReloader(
        registry,
        metadata,
        store.metadata,
        store,
        cipher,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        {},
    )

    await reloader.sync()

    assert registry.get("third-source") is not None


@pytest.mark.asyncio
async def test_reloader_replaces_bootstrap_verified_revisions_for_controlled_source() -> None:
    admin, _registry, store, _invalidator, cipher, _reloader = _services()
    manifest = _manifest()
    await admin.publish("third-source", manifest, "first-secret")
    current = store.active["third-source"]
    rows = [{"status": "OPEN"}]
    query = VerifiedQuery(
        query_id="third-source-open-status",
        source_id="third-source",
        question="상태 예시를 보여줘",
        sql="SELECT status FROM ai.issue_overview ORDER BY status LIMIT 1",
        metadata_revision=current.metadata_revision,
        relations=("ai.issue_overview",),
        expected=ExpectedResult(
            columns=("status",),
            row_count=1,
            result_hash=create_result_hash(("status",), rows),
        ),
    )
    await admin.publish_verified_query(query, "engineering")
    manifest["minimum_quality_level"] = "L2"
    await admin.publish("third-source", manifest, "first-secret")

    external_registry = SourceRegistry([])
    metadata = MetadataService(external_registry, StaticCatalog(), store=store.metadata)
    stale_revision = f"sha256:{'0' * 64}"
    assert stale_revision != current.metadata_revision
    external_verified = {"third-source": frozenset({stale_revision})}
    reloader = SourceReloader(
        external_registry,
        metadata,
        store.metadata,
        store,
        cipher,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        external_verified,
    )

    await reloader.sync()

    assert external_registry.get("third-source") is not None
    assert external_verified == {
        "third-source": frozenset({current.metadata_revision})
    }


@pytest.mark.asyncio
async def test_verified_query_contract_enables_l2_publish() -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    manifest = _manifest()
    await admin.publish("third-source", manifest, "first-secret")
    current = store.active["third-source"]
    rows = [{"status": "OPEN"}]
    query = VerifiedQuery(
        query_id="third-source-open-status",
        source_id="third-source",
        question="상태 예시를 보여줘",
        sql="SELECT status FROM ai.issue_overview ORDER BY status LIMIT 1",
        metadata_revision=current.metadata_revision,
        relations=("ai.issue_overview",),
        expected=ExpectedResult(
            columns=("status",),
            row_count=1,
            result_hash=create_result_hash(("status",), rows),
        ),
    )

    mismatched = replace(
        query,
        expected=replace(query.expected, result_hash=f"sha256:{'0' * 64}"),
    )
    with pytest.raises(SourceValidationError):
        await admin.publish_verified_query(mismatched, "engineering")
    assert store.verified == []

    verified = await admin.publish_verified_query(query, "engineering")
    manifest["minimum_quality_level"] = "L2"
    promoted = await admin.publish("third-source", manifest, "first-secret")

    assert verified["status"] == "verified"
    assert promoted["quality_level"] == "L2"
    assert store.verified == [query]
