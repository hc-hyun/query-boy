from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
import yaml

from query_man.errors import SourceValidationError
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
    SourceGenerationConflictError,
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
