from __future__ import annotations

import inspect
import re
from dataclasses import asdict, replace
from datetime import UTC, datetime

import pytest
import yaml

from query_man.metadata_store import PostgresMetadataStore
from query_man.models import PreparedMetadata
from query_man.registry import (
    POSTGRES_IDENTIFIER_MAX_LENGTH,
    load_budget_profiles,
    validate_source_manifest,
)
from query_man.revision import create_metadata_revision
from query_man.secrets import SourceSecretCipher
from query_man.source_store import (
    _CATALOG_PROJECTION,
    POSTGRES_BIGINT_MAX,
    PostgresSourceStore,
    SourceGenerationConflictError,
    SourcePublishPinnedError,
    _decode_catalog,
)
from query_man.verified import ExpectedResult, VerifiedQuery, create_result_hash
from tests.helpers import ROOT_DIRECTORY, minimal_development_snapshot


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_store_publishes_rotates_rolls_back_and_deactivates(
    disposable_control_dsn: str,
) -> None:
    dsn = disposable_control_dsn
    raw = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["source_id"] = "source-store-fixture"
    raw["connection"]["password_env"] = "SOURCE_STORE_FIXTURE_READER_PASSWORD"
    raw["minimum_quality_level"] = "L0"
    validated = validate_source_manifest(
        raw,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        "first-reader-secret",
    )
    source = validated.profile
    snapshot = minimal_development_snapshot()
    metadata = PreparedMetadata(snapshot, create_metadata_revision(source, snapshot))
    cipher = SourceSecretCipher(b"s" * 32)
    store = PostgresSourceStore(dsn)
    metadata_store = PostgresMetadataStore(dsn)
    try:
        current = await store.get_active(source.source_id)
        if current is not None and not current.enabled:
            current = await store.rollback(
                source.source_id,
                current.generation,
                current.generation,
                expected_state_version=current.state_version,
            )
        if current is not None:
            await metadata_store.unpin(
                replace(
                    source,
                    control_generation=current.generation,
                    control_state_version=current.state_version,
                )
            )
        expected = 0 if current is None else current.generation
        first_generation = await store.next_generation(source.source_id)
        first_secret = cipher.encrypt(source.source_id, first_generation, "first-reader-secret")
        first = await store.publish(
            source.source_id,
            expected,
            first_generation,
            validated.document,
            first_secret,
            metadata,
            expected_state_version=0 if current is None else current.state_version,
        )
        assert first.generation == first_generation
        assert cipher.decrypt(source.source_id, first.generation, first.encrypted_secret) == (
            "first-reader-secret"
        )

        rotated_generation = await store.next_generation(source.source_id)
        rotated_secret = cipher.encrypt(source.source_id, rotated_generation, "rotated-secret")
        rotated = await store.publish(
            source.source_id,
            first.generation,
            rotated_generation,
            validated.document,
            rotated_secret,
            metadata,
            expected_state_version=first.state_version,
        )
        assert rotated.generation == rotated_generation
        assert rotated.state_version == first.state_version + 1
        assert cipher.decrypt(source.source_id, rotated.generation, rotated.encrypted_secret) == (
            "rotated-secret"
        )

        catalog_page = await store.list_catalog(
            owner=source.provenance.owner,
            environment=source.provenance.environment,
            budget_profile=source.budget.name,
        )
        assert catalog_page.next_after_source_id is None
        assert len(catalog_page.items) == 1
        catalog = catalog_page.items[0]
        assert catalog.source_id == source.source_id
        assert catalog.generation == rotated.generation
        assert catalog.enabled is True
        assert catalog.state_version == rotated.state_version
        assert catalog.name == source.name
        assert catalog.description == source.description
        assert catalog.owner == source.provenance.owner
        assert catalog.environment == source.provenance.environment
        assert (
            catalog.database_migration_ref
            == source.provenance.database_migration_ref
        )
        assert catalog.budget_profile == source.budget.name
        assert catalog.minimum_quality_level == source.minimum_quality_level
        assert catalog.tenant_isolation == source.tenant_isolation
        assert catalog.connection.host == source.connection.host
        assert catalog.connection.port == source.connection.port
        assert catalog.connection.database == source.connection.database
        assert catalog.connection.user == source.connection.user
        assert catalog.connection.ssl == source.connection.ssl
        assert catalog.allowed_schemas == tuple(source.allowed_schemas)
        assert catalog.allowed_relation_kinds == tuple(source.allowed_relation_kinds)
        assert catalog.semantic_default_relation == source.semantic_overlay.default_relation
        assert catalog.semantic_relation_count == len(source.semantic_overlay.relations)
        assert catalog.semantic_join_count == len(source.semantic_overlay.joins)
        assert catalog.semantic_business_term_count == len(
            source.semantic_overlay.business_terms
        )
        assert catalog.semantic_question_rule_count == len(
            source.semantic_overlay.question_rules
        )
        assert catalog.semantic_composition_hint_count == len(
            source.semantic_overlay.composition_hints
        )
        assert catalog.published_metadata_revision == metadata.revision
        assert catalog.active_metadata_revision == metadata.revision
        assert catalog.metadata_pinned is False
        assert catalog.metadata_activated_at is not None
        assert catalog.is_current is True
        catalog_document = repr(asdict(catalog))
        assert "password_env" not in catalog_document
        assert "host_env" not in catalog_document
        assert "port_env" not in catalog_document
        assert "SOURCE_STORE_FIXTURE_READER_PASSWORD" not in catalog_document

        assert await store.get_catalog(source.source_id) == catalog
        assert await store.get_catalog("missing-source") is None
        assert not (
            await store.list_catalog(owner="different-owner")
        ).items

        refreshed_snapshot = minimal_development_snapshot()
        refreshed_snapshot.relations[0].comment = "catalog active pointer refresh"
        refreshed_metadata = PreparedMetadata(
            refreshed_snapshot,
            create_metadata_revision(source, refreshed_snapshot),
        )
        assert refreshed_metadata.revision != metadata.revision
        await metadata_store.publish(
            replace(
                source,
                control_generation=rotated.generation,
                control_state_version=rotated.state_version,
            ),
            refreshed_metadata,
        )
        refreshed_catalog = await store.get_catalog(source.source_id)
        assert refreshed_catalog is not None
        assert refreshed_catalog.published_metadata_revision == metadata.revision
        assert refreshed_catalog.active_metadata_revision == refreshed_metadata.revision
        assert refreshed_catalog.metadata_pinned is False

        first_history_page = await store.list_generation_history(
            source.source_id,
            limit=1,
        )
        assert first_history_page is not None
        assert first_history_page.current.generation == rotated.generation
        assert first_history_page.current.is_current is True
        assert [item.generation for item in first_history_page.items] == [
            rotated.generation
        ]
        assert first_history_page.items[0].is_current is True
        assert first_history_page.next_before_generation == rotated.generation
        second_history_page = await store.list_generation_history(
            source.source_id,
            before_generation=first_history_page.next_before_generation,
            limit=1,
        )
        assert second_history_page is not None
        assert [item.generation for item in second_history_page.items] == [
            first.generation
        ]
        assert second_history_page.items[0].is_current is False
        assert second_history_page.next_before_generation is None
        exhausted_history_page = await store.list_generation_history(
            source.source_id,
            before_generation=first.generation,
        )
        assert exhausted_history_page is not None
        assert exhausted_history_page.current.generation == rotated.generation
        assert exhausted_history_page.items == ()
        assert exhausted_history_page.next_before_generation is None
        assert await store.list_generation_history("missing-source") is None

        verified_query = VerifiedQuery(
            query_id="source-store-status-count",
            source_id=source.source_id,
            question="상태별 건수를 보여줘",
            sql="SELECT status, count(*) FROM ai.issue_overview GROUP BY status",
            metadata_revision=metadata.revision,
            relations=("ai.issue_overview",),
            expected=ExpectedResult(
                columns=("status", "count"),
                row_count=1,
                result_hash=create_result_hash(
                    ("status", "count"),
                    [{"status": "OPEN", "count": 1}],
                ),
            ),
        )
        await store.publish_verified_query(verified_query)
        assert metadata.revision in (
            await store.verified_revision_map()
        )[source.source_id]

        rolled_back = await store.rollback(
            source.source_id,
            first.generation,
            rotated.generation,
            expected_state_version=rotated.state_version,
        )
        assert rolled_back.generation == first.generation
        assert rolled_back.state_version == rotated.state_version + 1
        assert cipher.decrypt(source.source_id, rolled_back.generation, rolled_back.encrypted_secret) == (
            "first-reader-secret"
        )
        rollback_history = await store.list_generation_history(source.source_id)
        assert rollback_history is not None
        assert [item.generation for item in rollback_history.items] == [
            rotated.generation,
            first.generation,
        ]
        assert [item.is_current for item in rollback_history.items] == [False, True]
        assert all(
            item.state_version == rolled_back.state_version
            and item.enabled is True
            and item.active_metadata_revision == metadata.revision
            and item.metadata_pinned is True
            for item in rollback_history.items
        )

        resumed_generation = await store.next_generation(source.source_id)
        resumed_secret = cipher.encrypt(
            source.source_id,
            resumed_generation,
            "resumed-secret",
        )
        with pytest.raises(SourceGenerationConflictError):
            await store.publish(
                source.source_id,
                rolled_back.generation,
                resumed_generation,
                validated.document,
                resumed_secret,
                metadata,
                expected_state_version=first.state_version,
            )
        with pytest.raises(SourcePublishPinnedError):
            await store.publish(
                source.source_id,
                rolled_back.generation,
                resumed_generation,
                validated.document,
                resumed_secret,
                metadata,
                expected_state_version=rolled_back.state_version,
            )

        await metadata_store.unpin(
            replace(
                source,
                control_generation=rolled_back.generation,
                control_state_version=rolled_back.state_version,
            )
        )
        resumed = await store.publish(
            source.source_id,
            rolled_back.generation,
            resumed_generation,
            validated.document,
            resumed_secret,
            metadata,
            expected_state_version=rolled_back.state_version,
        )
        assert resumed.state_version == rolled_back.state_version + 1

        inactive_state_version = await store.deactivate(
            source.source_id,
            resumed.generation,
            expected_state_version=resumed.state_version,
        )
        inactive = await store.get_active(source.source_id)
        assert inactive is not None
        assert inactive.enabled is False
        assert inactive.state_version == inactive_state_version
        assert inactive.state_version == resumed.state_version + 1
        disabled_page = await store.list_catalog(enabled=False)
        assert [item.source_id for item in disabled_page.items] == [source.source_id]
        assert disabled_page.items[0].enabled is False
        assert not (await store.list_catalog(enabled=True)).items

        second_raw = yaml.safe_load(
            (ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml").read_text(
                encoding="utf-8"
            )
        )
        second_raw["source_id"] = "source-store-page-fixture"
        second_raw["connection"]["password_env"] = (  # type: ignore[index]
            "SOURCE_STORE_PAGE_FIXTURE_READER_PASSWORD"
        )
        second_validated = validate_source_manifest(
            second_raw,
            load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
            "second-reader-secret",
        )
        second_source = replace(
            second_validated.profile,
            minimum_quality_level="L0",
        )
        second_snapshot = minimal_development_snapshot()
        second_metadata = PreparedMetadata(
            second_snapshot,
            create_metadata_revision(second_source, second_snapshot),
        )
        second_generation = await store.next_generation(second_source.source_id)
        await store.publish(
            second_source.source_id,
            0,
            second_generation,
            second_validated.document,
            cipher.encrypt(
                second_source.source_id,
                second_generation,
                "second-reader-secret",
            ),
            second_metadata,
            expected_state_version=0,
        )
        first_list_page = await store.list_catalog(limit=1)
        assert [item.source_id for item in first_list_page.items] == [source.source_id]
        assert first_list_page.next_after_source_id == source.source_id
        second_list_page = await store.list_catalog(
            after_source_id=first_list_page.next_after_source_id,
            limit=1,
        )
        assert [item.source_id for item in second_list_page.items] == [
            second_source.source_id
        ]
        assert second_list_page.next_after_source_id is None
    finally:
        await metadata_store.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True])
async def test_source_catalog_rejects_unbounded_page_limits(limit: int) -> None:
    store = PostgresSourceStore("postgresql://unused")

    with pytest.raises(ValueError, match="page limit"):
        await store.list_catalog(limit=limit)
    with pytest.raises(ValueError, match="page limit"):
        await store.list_generation_history("source", limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before_generation",
    [0, POSTGRES_BIGINT_MAX + 1, True, 1.5, "1"],
)
async def test_source_history_rejects_invalid_generation_cursors(
    before_generation: object,
) -> None:
    store = PostgresSourceStore("postgresql://unused")

    with pytest.raises(ValueError, match="Generation cursor"):
        await store.list_generation_history(
            "source",
            before_generation=before_generation,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", "unsafe/source"),
        ("owner", "UPPERCASE"),
        ("database_migration_ref", "migration\ncredential"),
        ("database_migration_ref", "   "),
        ("budget_profile", "invalid-profile"),
        ("budget_profile", "a" * 64),
        ("connection_database", "unsafe/database"),
        ("connection_user", "a" * 64),
        ("allowed_schemas", ["ai"] * 21),
        ("allowed_schemas", [{"unsafe": "shape"}]),
        ("allowed_schemas", ["unsafe/schema"]),
        ("semantic_default_relation", "a" * 64 + ".table_name"),
        ("semantic_relation_count", 201),
    ],
)
def test_source_catalog_decoder_rejects_unsafe_or_unbounded_fields(
    field: str,
    value: object,
) -> None:
    row = _catalog_row()
    row[field] = value

    with pytest.raises(ValueError, match="Stored source catalog"):
        _decode_catalog(row)


def test_source_catalog_decoder_accepts_registry_identifier_boundaries() -> None:
    row = _catalog_row()
    identifier = "a" * POSTGRES_IDENTIFIER_MAX_LENGTH
    row["budget_profile"] = identifier
    row["connection_database"] = identifier
    row["connection_user"] = identifier
    row["allowed_schemas"] = [identifier]
    row["semantic_default_relation"] = f"{identifier}.{identifier}"

    decoded = _decode_catalog(row)

    assert decoded.budget_profile == identifier
    assert decoded.connection.database == identifier
    assert decoded.connection.user == identifier
    assert decoded.allowed_schemas == (identifier,)
    assert decoded.semantic_default_relation == f"{identifier}.{identifier}"


def test_source_catalog_queries_project_only_explicit_safe_manifest_fields() -> None:
    catalog_query_source = _CATALOG_PROJECTION + "".join(
        inspect.getsource(method)
        for method in (
            PostgresSourceStore.list_catalog,
            PostgresSourceStore.get_catalog,
            PostgresSourceStore.list_generation_history,
        )
    )
    for forbidden in (
        "secret_nonce",
        "secret_ciphertext",
        "password_env",
        "host_env",
        "port_env",
        "metadata_snapshots",
        "verified_query_contracts",
    ):
        assert forbidden not in catalog_query_source
    for match in re.finditer(r"revision[.]manifest", _CATALOG_PROJECTION):
        assert _CATALOG_PROJECTION[match.end() :].lstrip().startswith(("->", "#>"))
    history_source = inspect.getsource(PostgresSourceStore.list_generation_history)
    assert history_source.count("await connection.execute(") == 1


def _catalog_row() -> dict[str, object]:
    timestamp = datetime(2026, 8, 23, tzinfo=UTC)
    revision = "sha256:" + "0" * 64
    return {
        "source_id": "safe-source",
        "generation": 2,
        "enabled": True,
        "state_version": 3,
        "activated_at": timestamp,
        "generation_created_at": timestamp,
        "manifest_version": 2,
        "name": "Safe source",
        "description": "Bounded admin projection",
        "owner": "query-man",
        "environment": "production",
        "database_migration_ref": "migrations/20260823_source.sql",
        "budget_profile": "interactive",
        "minimum_quality_level": "L2",
        "tenant_isolation": "none",
        "connection_host": "database.internal",
        "connection_port": 5432,
        "connection_database": "source_database",
        "connection_user": "source_reader",
        "connection_ssl": True,
        "allowed_schemas": ["ai"],
        "allowed_relation_kinds": ["view"],
        "semantic_default_relation": "ai.source_overview",
        "semantic_relation_count": 1,
        "semantic_join_count": 0,
        "semantic_business_term_count": 0,
        "semantic_question_rule_count": 0,
        "semantic_composition_hint_count": 0,
        "published_metadata_revision": revision,
        "active_metadata_revision": revision,
        "metadata_pinned": False,
        "metadata_activated_at": timestamp,
        "is_current": True,
    }
