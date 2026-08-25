from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from contextlib import suppress
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
import yaml
from psycopg import AsyncConnection, Error
from psycopg.errors import RaiseException

import query_man.source_store as source_store_module
from query_man.metadata_store import PostgresMetadataStore
from query_man.models import PreparedMetadata
from query_man.registry import (
    POSTGRES_IDENTIFIER_MAX_LENGTH,
    ValidatedSourceManifest,
    load_budget_profiles,
    validate_source_manifest,
)
from query_man.revision import create_metadata_revision
from query_man.secrets import SourceSecretCipher
from query_man.source_store import (
    _CATALOG_PROJECTION,
    _MUTATION_PROJECTION,
    POSTGRES_BIGINT_MAX,
    GatewayUsageConflictError,
    MutationIdempotencyConflictError,
    MutationReplay,
    MutationRequest,
    PostgresSourceStore,
    ReplicaObservationConflictError,
    ResourceObservationConflictError,
    SourceGenerationConflictError,
    SourcePublishPinnedError,
    _decode_catalog,
    _decode_desired_replica_state,
    _decode_mutation,
    _decode_replica_observation,
    _GatewayUsageDeltaWrite,
    _ReplicaSourceObservationWrite,
    _ResourceObservationWrite,
)
from query_man.verified import ExpectedResult, VerifiedQuery, create_result_hash
from tests.helpers import ROOT_DIRECTORY, minimal_development_snapshot


def _mutation_fixture(
    source_id: str,
    operation: str,
    expected_generation: int,
    expected_state_version: int,
    *,
    idempotency_key: str | None = None,
    hash_character: str = "a",
) -> MutationRequest:
    return MutationRequest(
        idempotency_key=idempotency_key or str(uuid.uuid4()),
        request_hash=f"hmac-sha256:{hash_character * 64}",
        operation=operation,
        source_id=source_id,
        actor="source-admin",
        reason="integration-test",
        expected_generation=expected_generation,
        expected_state_version=expected_state_version,
    )


def _source_fixture(
    source_id: str,
    *,
    resource_observability: bool = True,
) -> tuple[ValidatedSourceManifest, PreparedMetadata]:
    raw = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["source_id"] = source_id
    raw["connection"]["password_env"] = (  # type: ignore[index]
        f"{source_id.replace('-', '_').upper()}_READER_PASSWORD"
    )
    raw["minimum_quality_level"] = "L0"
    if not resource_observability:
        raw.pop("observability", None)
    validated = validate_source_manifest(
        raw,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        "receipt-reader-secret",
    )
    snapshot = minimal_development_snapshot()
    return validated, PreparedMetadata(
        snapshot,
        create_metadata_revision(validated.profile, snapshot),
    )


def _publish_result(
    source_id: str,
    generation: int,
    metadata_revision: str,
) -> dict[str, object]:
    return {
        "status": "published",
        "source_id": source_id,
        "generation": generation,
        "metadata_revision": metadata_revision,
        "quality_level": "L0",
    }


async def _publish_observation_source(
    dsn: str,
    source_id: str,
) -> tuple[PostgresSourceStore, str]:
    validated, metadata = _source_fixture(source_id)
    store = PostgresSourceStore(dsn)
    generation = await store.next_generation(source_id)
    await store.publish(
        source_id,
        0,
        generation,
        validated.document,
        SourceSecretCipher(b"u" * 32).encrypt(
            source_id,
            generation,
            "reader-secret",
        ),
        metadata,
        expected_state_version=0,
    )
    return store, metadata.revision


def _gateway_usage_delta(
    source_id: str,
    metadata_revision: str,
    *,
    bucket_start: datetime | None = None,
) -> _GatewayUsageDeltaWrite:
    return _GatewayUsageDeltaWrite(
        source_id=source_id,
        budget_profile="interactive",
        metadata_revision=metadata_revision,
        definition_revision=f"sha256:{'d' * 64}",
        bucket_start=bucket_start
        or datetime.now(UTC).replace(minute=0, second=0, microsecond=0),
        query_count=1,
        success_count=1,
        rejected_count=0,
        timeout_count=0,
        overloaded_count=0,
        cancelled_count=0,
        failed_count=0,
        queue_ms_sum=2,
        elapsed_ms_sum=3,
        returned_rows_sum=4,
        result_bytes_sum=5,
        truncated_count=0,
    )


def _resource_observation_batch(
    definition_revision: str,
    *,
    include_representative: bool = True,
    value_offset: int = 0,
) -> tuple[_ResourceObservationWrite, ...]:
    observations = (
        _ResourceObservationWrite(
            "representative_records",
            100 + value_offset,
            "rows",
            "postgres_catalog_estimate",
            definition_revision,
        ),
        _ResourceObservationWrite(
            "table_bytes",
            10 + value_offset,
            "bytes",
            "postgres_relation_size",
            definition_revision,
        ),
        _ResourceObservationWrite(
            "index_bytes",
            2 + value_offset,
            "bytes",
            "postgres_relation_size",
            definition_revision,
        ),
        _ResourceObservationWrite(
            "total_storage_bytes",
            12 + value_offset,
            "bytes",
            "postgres_relation_size",
            definition_revision,
        ),
    )
    return observations if include_representative else observations[1:]


@pytest.mark.asyncio
async def test_source_store_rejects_mutation_operation_mismatches_before_io() -> None:
    validated, metadata = _source_fixture("source-mutation-operation")
    source = validated.profile
    store = PostgresSourceStore("postgresql://unused")
    encrypted = SourceSecretCipher(b"o" * 32).encrypt(
        source.source_id,
        1,
        "reader-secret",
    )
    wrong_mutation = _mutation_fixture(
        source.source_id,
        "deactivate_source",
        0,
        0,
    )
    wrong_result: dict[str, object] = {
        "status": "deactivated",
        "source_id": source.source_id,
    }
    query = VerifiedQuery(
        query_id="operation-guard-query",
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

    with pytest.raises(ValueError, match="does not match the source transition"):
        await store.publish(
            source.source_id,
            0,
            1,
            validated.document,
            encrypted,
            metadata,
            expected_state_version=0,
            mutation=wrong_mutation,
            mutation_result=wrong_result,
        )
    with pytest.raises(ValueError, match="does not match the source transition"):
        await store.deactivate(
            source.source_id,
            0,
            expected_state_version=0,
            mutation=replace(wrong_mutation, operation="resume_metadata_publish"),
            mutation_result={"status": "resumed", "source_id": source.source_id},
        )
    with pytest.raises(ValueError, match="does not match the source transition"):
        await store.rollback(
            source.source_id,
            1,
            0,
            expected_state_version=0,
            mutation=wrong_mutation,
            mutation_result=wrong_result,
        )
    with pytest.raises(ValueError, match="does not match the source transition"):
        await store.publish_verified_query(
            query,
            mutation=wrong_mutation,
            mutation_result=wrong_result,
        )
    with pytest.raises(ValueError, match="does not match the source transition"):
        await store.resume_metadata_publish(
            source.source_id,
            metadata.revision,
            mutation=wrong_mutation,
            mutation_result=wrong_result,
        )


@pytest.mark.asyncio
async def test_source_store_binds_known_result_fields_before_io() -> None:
    validated, metadata = _source_fixture("source-mutation-result")
    source = validated.profile
    store = PostgresSourceStore("postgresql://unused")
    encrypted = SourceSecretCipher(b"b" * 32).encrypt(
        source.source_id,
        1,
        "reader-secret",
    )
    publish_mutation = _mutation_fixture(source.source_id, "publish_source", 0, 0)
    for wrong_publish_result in (
        _publish_result(source.source_id, 2, metadata.revision),
        _publish_result(source.source_id, 1, "sha256:" + "f" * 64),
    ):
        with pytest.raises(ValueError, match="does not match the source transition"):
            await store.publish(
                source.source_id,
                0,
                1,
                validated.document,
                encrypted,
                metadata,
                expected_state_version=0,
                mutation=publish_mutation,
                mutation_result=wrong_publish_result,
            )

    expected = ExpectedResult(
        columns=("status", "count"),
        row_count=1,
        result_hash=create_result_hash(
            ("status", "count"),
            [{"status": "OPEN", "count": 1}],
        ),
    )
    query = VerifiedQuery(
        query_id="result-guard-query",
        source_id=source.source_id,
        question="상태별 건수를 보여줘",
        sql="SELECT status, count(*) FROM ai.issue_overview GROUP BY status",
        metadata_revision=metadata.revision,
        relations=("ai.issue_overview",),
        expected=expected,
    )
    verified_mutation = _mutation_fixture(
        source.source_id,
        "publish_verified_query",
        0,
        0,
    )
    verified_result: dict[str, object] = {
        "status": "verified",
        "query_id": query.query_id,
        "source_id": source.source_id,
        "metadata_revision": metadata.revision,
        "row_count": expected.row_count,
        "result_hash": expected.result_hash,
    }
    for field, wrong_value in (
        ("query_id", "different-query"),
        ("metadata_revision", "sha256:" + "f" * 64),
        ("row_count", expected.row_count + 1),
        ("result_hash", "sha256:" + "f" * 64),
    ):
        wrong_verified_result = {**verified_result, field: wrong_value}
        with pytest.raises(ValueError, match="does not match the source transition"):
            await store.publish_verified_query(
                query,
                mutation=verified_mutation,
                mutation_result=wrong_verified_result,
            )


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
        refreshed_snapshot = replace(
            refreshed_snapshot,
            relations=(
                replace(
                    refreshed_snapshot.relations[0],
                    comment="catalog active pointer refresh",
                ),
                *refreshed_snapshot.relations[1:],
            ),
        )
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_mutation_receipts_are_atomic_idempotent_and_append_only(
    disposable_control_dsn: str,
) -> None:
    dsn = disposable_control_dsn
    validated, metadata = _source_fixture("source-mutation-receipt")
    source = validated.profile
    cipher = SourceSecretCipher(b"r" * 32)
    store = PostgresSourceStore(dsn)
    try:
        generation = await store.next_generation(source.source_id)
        encrypted_secret = cipher.encrypt(
            source.source_id,
            generation,
            "receipt-reader-secret",
        )
        mutation = _mutation_fixture(source.source_id, "publish_source", 0, 0)
        result = _publish_result(source.source_id, generation, metadata.revision)

        attempts = await asyncio.gather(
            store.publish(
                source.source_id,
                0,
                generation,
                validated.document,
                encrypted_secret,
                metadata,
                expected_state_version=0,
                mutation=mutation,
                mutation_result=result,
            ),
            store.publish(
                source.source_id,
                0,
                generation,
                validated.document,
                encrypted_secret,
                metadata,
                expected_state_version=0,
                mutation=mutation,
                mutation_result=result,
            ),
            return_exceptions=True,
        )
        published_attempts = [
            attempt for attempt in attempts if not isinstance(attempt, BaseException)
        ]
        concurrent_replays = [
            attempt for attempt in attempts if isinstance(attempt, MutationReplay)
        ]
        assert len(published_attempts) == 1
        assert len(concurrent_replays) == 1
        published = published_attempts[0]

        receipt = await store.get_mutation(mutation.idempotency_key)
        assert receipt is not None
        assert concurrent_replays[0].receipt == receipt
        assert receipt.request_hash == mutation.request_hash
        assert receipt.operation == "publish_source"
        assert receipt.actor == mutation.actor
        assert receipt.reason == mutation.reason
        assert receipt.expected_generation == 0
        assert receipt.expected_state_version == 0
        assert receipt.outcome == "succeeded"
        assert receipt.resulting_generation == published.generation
        assert receipt.resulting_state_version == published.state_version
        assert receipt.http_status == 200
        assert receipt.error_code is None
        assert receipt.result == result
        receipt_document = repr(asdict(receipt))
        for forbidden in (
            "receipt-reader-secret",
            "password_env",
            "secret_nonce",
            "secret_ciphertext",
            "SELECT ",
        ):
            assert forbidden not in receipt_document

        with pytest.raises(MutationReplay) as replay:
            await store.publish(
                source.source_id,
                0,
                generation,
                validated.document,
                encrypted_secret,
                metadata,
                expected_state_version=0,
                mutation=mutation,
                mutation_result=result,
            )
        assert replay.value.receipt == receipt
        after_replay = await store.get_active(source.source_id)
        assert after_replay is not None
        assert (after_replay.generation, after_replay.state_version) == (
            published.generation,
            published.state_version,
        )
        assert await store.next_generation(source.source_id) == generation + 1

        conflicting = replace(mutation, request_hash="hmac-sha256:" + "b" * 64)
        with pytest.raises(MutationIdempotencyConflictError):
            await store.publish(
                source.source_id,
                0,
                generation,
                validated.document,
                encrypted_secret,
                metadata,
                expected_state_version=0,
                mutation=conflicting,
                mutation_result=result,
            )
        after_conflict = await store.get_active(source.source_id)
        assert after_conflict is not None
        assert (after_conflict.generation, after_conflict.state_version) == (
            published.generation,
            published.state_version,
        )

        rejected_mutation = _mutation_fixture(
            source.source_id,
            "deactivate_source",
            published.generation,
            published.state_version,
        )
        rejected = await store.record_mutation_rejection(
            rejected_mutation,
            http_status=409,
            error_code="SOURCE_GENERATION_CONFLICT",
        )
        assert rejected.outcome == "rejected"
        assert rejected.resulting_generation is None
        assert rejected.resulting_state_version is None
        assert rejected.result == {}
        assert (
            await store.record_mutation_rejection(
                rejected_mutation,
                http_status=409,
                error_code="SOURCE_GENERATION_CONFLICT",
            )
            == rejected
        )
        with pytest.raises(MutationIdempotencyConflictError):
            await store.record_mutation_rejection(
                replace(
                    rejected_mutation,
                    request_hash="hmac-sha256:" + "c" * 64,
                ),
                http_status=409,
                error_code="SOURCE_GENERATION_CONFLICT",
            )

        first_page = await store.list_mutations(source.source_id, limit=1)
        assert first_page is not None
        assert first_page.items == (rejected,)
        assert first_page.next_before_event_id == rejected.event_id
        second_page = await store.list_mutations(
            source.source_id,
            before_event_id=first_page.next_before_event_id,
            limit=1,
        )
        assert second_page is not None
        assert second_page.items == (receipt,)
        assert second_page.next_before_event_id is None
        assert await store.list_mutations("missing-source") is None

        connection = await AsyncConnection.connect(dsn)
        try:
            with pytest.raises(RaiseException):
                await connection.execute(
                    "UPDATE control.source_mutation_receipts SET reason = %s "
                    "WHERE event_id = %s",
                    ("changed", receipt.event_id),
                )
            await connection.rollback()
            with pytest.raises(RaiseException):
                await connection.execute(
                    "DELETE FROM control.source_mutation_receipts WHERE event_id = %s",
                    (receipt.event_id,),
                )
            await connection.rollback()
        finally:
            await connection.close()
        assert await store.get_mutation(mutation.idempotency_key) == receipt

        failing_generation = await store.next_generation(source.source_id)
        failing_mutation = _mutation_fixture(
            source.source_id,
            "publish_source",
            published.generation,
            published.state_version,
        )
        failing_result = _publish_result(
            source.source_id,
            failing_generation,
            metadata.revision,
        )
        connection = await AsyncConnection.connect(dsn)
        try:
            await connection.execute(
                "SELECT setval("
                "'control.source_mutation_receipts_event_id_seq', %s, true)",
                (POSTGRES_BIGINT_MAX,),
            )
            await connection.commit()
        finally:
            await connection.close()

        with pytest.raises(Error):
            await store.publish(
                source.source_id,
                published.generation,
                failing_generation,
                validated.document,
                cipher.encrypt(
                    source.source_id,
                    failing_generation,
                    "second-reader-secret",
                ),
                metadata,
                expected_state_version=published.state_version,
                mutation=failing_mutation,
                mutation_result=failing_result,
            )
        after_failed_receipt = await store.get_active(source.source_id)
        assert after_failed_receipt is not None
        assert (after_failed_receipt.generation, after_failed_receipt.state_version) == (
            published.generation,
            published.state_version,
        )
        assert await store.next_generation(source.source_id) == failing_generation
        assert await store.get_mutation(failing_mutation.idempotency_key) is None
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verified_and_resume_receipts_use_commit_time_authority_state(
    disposable_control_dsn: str,
) -> None:
    dsn = disposable_control_dsn
    validated, metadata = _source_fixture("source-mutation-guards")
    source = validated.profile
    cipher = SourceSecretCipher(b"g" * 32)
    store = PostgresSourceStore(dsn)
    try:
        generation = await store.next_generation(source.source_id)
        published = await store.publish(
            source.source_id,
            0,
            generation,
            validated.document,
            cipher.encrypt(source.source_id, generation, "receipt-reader-secret"),
            metadata,
            expected_state_version=0,
        )
        empty_page = await store.list_mutations(source.source_id)
        assert empty_page is not None
        assert empty_page.items == ()
        assert empty_page.next_before_event_id is None

        wrong_rollback = _mutation_fixture(
            source.source_id,
            "rollback_source",
            published.generation,
            published.state_version,
        )
        rollback_result: dict[str, object] = {
            "status": "rolled_back",
            "source_id": source.source_id,
            "generation": published.generation,
            "metadata_revision": metadata.revision,
        }
        for wrong_rollback_result in (
            {**rollback_result, "generation": published.generation + 1},
            {**rollback_result, "metadata_revision": "sha256:" + "f" * 64},
        ):
            with pytest.raises(ValueError, match="does not match the source transition"):
                await store.rollback(
                    source.source_id,
                    published.generation,
                    published.generation,
                    expected_state_version=published.state_version,
                    mutation=wrong_rollback,
                    mutation_result=wrong_rollback_result,
                )
        after_wrong_rollback = await store.get_active(source.source_id)
        assert after_wrong_rollback == published
        assert await store.get_mutation(wrong_rollback.idempotency_key) is None

        expected = ExpectedResult(
            columns=("status", "count"),
            row_count=1,
            result_hash=create_result_hash(
                ("status", "count"),
                [{"status": "OPEN", "count": 1}],
            ),
        )
        query = VerifiedQuery(
            query_id="mutation-guard-query",
            source_id=source.source_id,
            question="상태별 건수를 보여줘",
            sql="SELECT status, count(*) FROM ai.issue_overview GROUP BY status",
            metadata_revision=metadata.revision,
            relations=("ai.issue_overview",),
            expected=expected,
        )
        verified_result: dict[str, object] = {
            "status": "verified",
            "query_id": query.query_id,
            "source_id": source.source_id,
            "metadata_revision": metadata.revision,
            "row_count": expected.row_count,
            "result_hash": expected.result_hash,
        }

        stale_mutation = _mutation_fixture(
            source.source_id,
            "publish_verified_query",
            published.generation,
            0,
        )
        with pytest.raises(SourceGenerationConflictError):
            await store.publish_verified_query(
                query,
                mutation=stale_mutation,
                mutation_result=verified_result,
            )
        assert await store.get_mutation(stale_mutation.idempotency_key) is None
        assert source.source_id not in await store.verified_revision_map()

        wrong_revision = "sha256:" + "f" * 64
        metadata_mismatch_query = replace(
            query,
            query_id="mutation-metadata-mismatch",
            metadata_revision=wrong_revision,
        )
        metadata_mismatch_result = {
            **verified_result,
            "query_id": metadata_mismatch_query.query_id,
            "metadata_revision": wrong_revision,
        }
        metadata_mismatch_mutation = _mutation_fixture(
            source.source_id,
            "publish_verified_query",
            published.generation,
            published.state_version,
        )
        with pytest.raises(SourceGenerationConflictError):
            await store.publish_verified_query(
                metadata_mismatch_query,
                mutation=metadata_mismatch_mutation,
                mutation_result=metadata_mismatch_result,
            )
        assert (
            await store.get_mutation(metadata_mismatch_mutation.idempotency_key)
            is None
        )
        assert source.source_id not in await store.verified_revision_map()

        verified_mutation = _mutation_fixture(
            source.source_id,
            "publish_verified_query",
            published.generation,
            published.state_version,
        )
        await store.publish_verified_query(
            query,
            mutation=verified_mutation,
            mutation_result=verified_result,
        )
        verified_receipt = await store.get_mutation(verified_mutation.idempotency_key)
        assert verified_receipt is not None
        assert verified_receipt.result == verified_result
        assert verified_receipt.resulting_generation == published.generation
        assert verified_receipt.resulting_state_version == published.state_version
        assert (await store.verified_revision_map())[source.source_id] == frozenset(
            {metadata.revision}
        )

        pinned = await store.rollback(
            source.source_id,
            published.generation,
            published.generation,
            expected_state_version=published.state_version,
        )
        pinned_catalog = await store.get_catalog(source.source_id)
        assert pinned_catalog is not None
        assert pinned_catalog.metadata_pinned is True

        wrong_resume = _mutation_fixture(
            source.source_id,
            "resume_metadata_publish",
            pinned.generation,
            pinned.state_version,
        )
        resume_result = {"status": "resumed", "source_id": source.source_id}
        with pytest.raises(SourceGenerationConflictError):
            await store.resume_metadata_publish(
                source.source_id,
                wrong_revision,
                mutation=wrong_resume,
                mutation_result=resume_result,
            )
        after_wrong_resume = await store.get_catalog(source.source_id)
        assert after_wrong_resume is not None
        assert after_wrong_resume.metadata_pinned is True
        assert await store.get_mutation(wrong_resume.idempotency_key) is None

        resume_mutation = _mutation_fixture(
            source.source_id,
            "resume_metadata_publish",
            pinned.generation,
            pinned.state_version,
        )
        connection = await AsyncConnection.connect(dsn)
        try:
            await connection.execute(
                "SELECT setval("
                "'control.source_mutation_receipts_event_id_seq', %s, true)",
                (POSTGRES_BIGINT_MAX,),
            )
            await connection.commit()
        finally:
            await connection.close()
        with pytest.raises(Error):
            await store.resume_metadata_publish(
                source.source_id,
                metadata.revision,
                mutation=resume_mutation,
                mutation_result=resume_result,
            )
        after_failed_resume = await store.get_catalog(source.source_id)
        assert after_failed_resume is not None
        assert after_failed_resume.metadata_pinned is True
        assert await store.get_mutation(resume_mutation.idempotency_key) is None

        connection = await AsyncConnection.connect(dsn)
        try:
            await connection.execute(
                "SELECT setval("
                "'control.source_mutation_receipts_event_id_seq', "
                "(SELECT max(event_id) FROM control.source_mutation_receipts), true)"
            )
            await connection.commit()
        finally:
            await connection.close()
        await store.resume_metadata_publish(
            source.source_id,
            metadata.revision,
            mutation=resume_mutation,
            mutation_result=resume_result,
        )
        resumed_catalog = await store.get_catalog(source.source_id)
        assert resumed_catalog is not None
        assert resumed_catalog.metadata_pinned is False
        assert resumed_catalog.state_version == pinned.state_version
        resume_receipt = await store.get_mutation(resume_mutation.idempotency_key)
        assert resume_receipt is not None
        assert resume_receipt.result == resume_result
        assert resume_receipt.resulting_generation == pinned.generation
        assert resume_receipt.resulting_state_version == pinned.state_version
        with pytest.raises(MutationReplay) as replay:
            await store.resume_metadata_publish(
                source.source_id,
                metadata.revision,
                mutation=resume_mutation,
                mutation_result=resume_result,
            )
        assert replay.value.receipt == resume_receipt
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_replica_observations_are_latest_fenced_and_use_active_metadata(
    disposable_control_dsn: str,
) -> None:
    validated, metadata = _source_fixture("replica-observation-source")
    source = validated.profile
    store = PostgresSourceStore(disposable_control_dsn)
    metadata_store = PostgresMetadataStore(disposable_control_dsn)
    cipher = SourceSecretCipher(b"r" * 32)
    try:
        generation = await store.next_generation(source.source_id)
        published = await store.publish(
            source.source_id,
            0,
            generation,
            validated.document,
            cipher.encrypt(source.source_id, generation, "reader-secret"),
            metadata,
            expected_state_version=0,
        )

        alpha_incarnation = await store.register_replica("replica-alpha", 5_000)
        assert alpha_incarnation == 1
        initial = await store.list_replica_observations(source.source_id)
        assert initial is not None
        assert (
            initial.desired_generation,
            initial.desired_state_version,
            initial.desired_enabled,
            initial.desired_metadata_revision,
        ) == (published.generation, published.state_version, True, metadata.revision)
        assert len(initial.items) == 1
        initial_alpha = initial.items[0]
        assert initial_alpha.replica_id == "replica-alpha"
        assert initial_alpha.applied_generation is None
        assert initial_alpha.source_health is None
        assert initial_alpha.fresh_until - initial_alpha.observed_at == timedelta(
            seconds=15
        )
        assert initial_alpha.read_at >= initial_alpha.observed_at

        applied = _ReplicaSourceObservationWrite(
            source_id=source.source_id,
            applied_generation=published.generation,
            applied_state_version=published.state_version,
            applied_enabled=True,
            applied_metadata_revision=metadata.revision,
            source_health="healthy",
            reason_code=None,
        )
        await store.report_replica(
            "replica-alpha",
            alpha_incarnation,
            reason_code=None,
            sources=(applied,),
        )
        observed = await store.list_replica_observations(source.source_id)
        assert observed is not None
        observed_alpha = observed.items[0]
        assert (
            observed_alpha.applied_generation,
            observed_alpha.applied_state_version,
            observed_alpha.applied_enabled,
            observed_alpha.applied_metadata_revision,
            observed_alpha.source_health,
        ) == (
            published.generation,
            published.state_version,
            True,
            metadata.revision,
            "healthy",
        )

        await store.report_replica(
            "replica-alpha",
            alpha_incarnation,
            reason_code="CONTROL_SCAN_FAILED",
            sources=(),
        )
        failed_scan = await store.list_replica_observations(source.source_id)
        assert failed_scan is not None
        assert failed_scan.items[0].report_reason_code == "CONTROL_SCAN_FAILED"
        assert failed_scan.items[0].applied_generation == published.generation

        await store.report_replica(
            "replica-alpha",
            alpha_incarnation,
            reason_code=None,
            sources=(applied,),
        )
        before_failed_report = await _replica_rows(disposable_control_dsn)
        invalid_generation = replace(
            applied,
            applied_generation=published.generation + 100,
        )
        with pytest.raises(Error):
            await store.report_replica(
                "replica-alpha",
                alpha_incarnation,
                reason_code=None,
                sources=(invalid_generation,),
            )
        assert await _replica_rows(disposable_control_dsn) == before_failed_report

        refreshed_snapshot = replace(
            metadata.snapshot,
            relations=(
                replace(
                    metadata.snapshot.relations[0],
                    comment="replica observation active metadata",
                ),
                *metadata.snapshot.relations[1:],
            ),
        )
        refreshed_metadata = PreparedMetadata(
            refreshed_snapshot,
            create_metadata_revision(source, refreshed_snapshot),
        )
        await metadata_store.publish(
            replace(
                source,
                control_generation=published.generation,
                control_state_version=published.state_version,
            ),
            refreshed_metadata,
        )
        refreshed = await store.list_replica_observations(source.source_id)
        assert refreshed is not None
        assert refreshed.desired_metadata_revision == refreshed_metadata.revision
        assert refreshed.items[0].applied_metadata_revision == metadata.revision

        assert await store.register_replica("replica-beta", 300_000) == 1
        first_page = await store.list_replica_observations(source.source_id, limit=1)
        assert first_page is not None
        assert [item.replica_id for item in first_page.items] == ["replica-alpha"]
        assert first_page.next_after_replica_id == "replica-alpha"
        second_page = await store.list_replica_observations(
            source.source_id,
            after_replica_id=first_page.next_after_replica_id,
            limit=1,
        )
        assert second_page is not None
        assert [item.replica_id for item in second_page.items] == ["replica-beta"]
        assert second_page.next_after_replica_id is None
        assert second_page.items[0].fresh_until - second_page.items[0].observed_at == (
            timedelta(minutes=15)
        )

        new_incarnation = await store.register_replica("replica-alpha", 5_000)
        assert new_incarnation == alpha_incarnation + 1
        with pytest.raises(ReplicaObservationConflictError):
            await store.report_replica(
                "replica-alpha",
                alpha_incarnation,
                reason_code=None,
                sources=(applied,),
            )
        fenced = await store.list_replica_observations(source.source_id)
        assert fenced is not None
        fenced_alpha = next(
            item for item in fenced.items if item.replica_id == "replica-alpha"
        )
        assert fenced_alpha.applied_generation is None
        assert fenced_alpha.source_health is None

        state_version = await store.deactivate(
            source.source_id,
            published.generation,
            expected_state_version=published.state_version,
        )
        disabled = await store.list_replica_observations(source.source_id)
        assert disabled is not None
        assert disabled.desired_enabled is False
        assert disabled.desired_state_version == state_version
        assert disabled.desired_metadata_revision is None

        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT "
                "(SELECT count(*) FROM control.runtime_replicas), "
                "(SELECT count(*) FROM control.runtime_source_observations)"
            )
            assert await cursor.fetchone() == (2, 1)
        finally:
            await connection.close()
    finally:
        await metadata_store.close()
        await store.close()


async def _replica_rows(dsn: str) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = await AsyncConnection.connect(dsn)
    try:
        replicas = await connection.execute(
            "SELECT replica_id, incarnation, heartbeat_interval_ms, "
            "report_reason_code, observed_at FROM control.runtime_replicas "
            "ORDER BY replica_id"
        )
        observations = await connection.execute(
            "SELECT replica_id, incarnation, source_id, applied_generation, "
            "applied_state_version, applied_enabled, applied_metadata_revision, "
            "source_health, reason_code FROM control.runtime_source_observations "
            "ORDER BY replica_id, source_id"
        )
        return await replicas.fetchall(), await observations.fetchall()
    finally:
        await connection.close()


async def _gateway_usage_rows(
    dsn: str,
    source_id: str,
    replica_id: str,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    connection = await AsyncConnection.connect(dsn)
    try:
        rollups = await connection.execute(
            "SELECT source_id, budget_profile, metadata_revision, "
            "definition_revision, bucket_start, query_count, success_count, "
            "rejected_count, timeout_count, overloaded_count, cancelled_count, "
            "failed_count, queue_ms_sum, elapsed_ms_sum, returned_rows_sum, "
            "result_bytes_sum, truncated_count FROM control.gateway_usage_rollups "
            "WHERE source_id = %s ORDER BY bucket_start, budget_profile",
            (source_id,),
        )
        cursors = await connection.execute(
            "SELECT replica_id, incarnation, last_sequence, last_payload_hash, "
            "observed_at, fresh_until "
            "FROM control.gateway_usage_report_cursors WHERE replica_id = %s",
            (replica_id,),
        )
        return await rollups.fetchall(), await cursors.fetchall()
    finally:
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resource_observations_coalesce_shift_and_reset_definition(
    disposable_control_dsn: str,
) -> None:
    source_id = "resource-observation-source"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    definition = f"sha256:{'a' * 64}"
    initial = (
        _ResourceObservationWrite(
            "representative_records",
            100,
            "rows",
            "postgres_catalog_estimate",
            definition,
        ),
        _ResourceObservationWrite(
            "table_bytes",
            10,
            "bytes",
            "postgres_relation_size",
            definition,
        ),
        _ResourceObservationWrite(
            "index_bytes",
            2,
            "bytes",
            "postgres_relation_size",
            definition,
        ),
        _ResourceObservationWrite(
            "total_storage_bytes",
            12,
            "bytes",
            "postgres_relation_size",
            definition,
        ),
    )
    table_sample = initial[1]
    try:
        await store.report_resource_observations(
            source_id,
            1,
            metadata_revision,
            initial,
        )
        await store.report_resource_observations(
            source_id,
            1,
            metadata_revision,
            (replace(table_sample, value=20), initial[2], initial[3]),
        )

        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT count(*), value, previous_value, "
                "fresh_until - observed_at "
                "FROM control.source_resource_observations "
                "WHERE source_id = %s AND metric = 'table_bytes' "
                "GROUP BY value, previous_value, fresh_until, observed_at",
                (source_id,),
            )
            assert await cursor.fetchone() == (1, 20, None, timedelta(hours=72))
            cursor = await connection.execute(
                "SELECT count(*) FROM control.source_resource_observations "
                "WHERE source_id = %s",
                (source_id,),
            )
            assert await cursor.fetchone() == (4,)

            await connection.execute(
                "UPDATE control.source_resource_observations "
                "SET sample_bucket_start = sample_bucket_start - interval '1 day', "
                "observed_at = observed_at - interval '1 day', "
                "fresh_until = fresh_until - interval '1 day' "
                "WHERE source_id = %s AND metric = 'table_bytes'",
                (source_id,),
            )
            await connection.commit()
        finally:
            await connection.close()

        await store.report_resource_observations(
            source_id,
            1,
            metadata_revision,
            (replace(table_sample, value=30), initial[2], initial[3]),
        )
        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT value, previous_value, metadata_revision, "
                "previous_metadata_revision, "
                "sample_bucket_start - previous_sample_bucket_start, "
                "fresh_until - observed_at, "
                "previous_fresh_until - previous_observed_at "
                "FROM control.source_resource_observations "
                "WHERE source_id = %s AND metric = 'table_bytes'",
                (source_id,),
            )
            assert await cursor.fetchone() == (
                30,
                20,
                metadata_revision,
                metadata_revision,
                timedelta(days=1),
                timedelta(hours=72),
                timedelta(hours=72),
            )
        finally:
            await connection.close()

        changed_definition = f"sha256:{'b' * 64}"
        await store.report_resource_observations(
            source_id,
            1,
            metadata_revision,
            (
                replace(
                    table_sample,
                    value=40,
                    definition_revision=changed_definition,
                ),
                replace(initial[2], definition_revision=changed_definition),
                replace(initial[3], definition_revision=changed_definition),
            ),
        )
        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT value, definition_revision, previous_value, "
                "previous_metadata_revision, previous_sample_bucket_start, "
                "previous_observed_at, previous_fresh_until "
                "FROM control.source_resource_observations "
                "WHERE source_id = %s AND metric = 'table_bytes'",
                (source_id,),
            )
            assert await cursor.fetchone() == (
                40,
                changed_definition,
                None,
                None,
                None,
                None,
                None,
            )
        finally:
            await connection.close()
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resource_attempt_preserves_current_generation_success_and_fences_generation(
    disposable_control_dsn: str,
) -> None:
    source_id = "resource-attempt-source"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    definition = f"sha256:{'8' * 64}"
    try:
        await store.report_resource_observations(
            source_id,
            1,
            metadata_revision,
            _resource_observation_batch(definition),
        )
        succeeded = await store.get_source_usage(source_id)
        assert succeeded is not None
        assert succeeded.resource_attempt is not None
        success_at = succeeded.resource_attempt.last_success_at
        assert success_at is not None
        assert succeeded.resource_attempt.last_attempt_outcome == "succeeded"
        assert succeeded.resource_attempt.last_success_has_representative is True
        assert {row.current.observed_at for row in succeeded.resource_observations} == {
            success_at
        }
        assert {row.current.fresh_until for row in succeeded.resource_observations} == {
            success_at + timedelta(hours=72)
        }

        with pytest.raises(ResourceObservationConflictError):
            await store.report_resource_observations(
                source_id,
                1,
                f"sha256:{'0' * 64}",
                _resource_observation_batch(definition, value_offset=1),
            )
        unchanged = await store.get_source_usage(source_id)
        assert unchanged is not None
        assert unchanged.resource_attempt == succeeded.resource_attempt
        assert unchanged.resource_observations == succeeded.resource_observations

        await store.report_resource_observation_failure(
            source_id,
            1,
            "RESOURCE_READ_FAILED",
        )
        failed = await store.get_source_usage(source_id)
        assert failed is not None
        assert failed.resource_attempt is not None
        assert failed.resource_attempt.last_attempt_outcome == "failed"
        assert failed.resource_attempt.last_attempt_reason_code == "RESOURCE_READ_FAILED"
        assert failed.resource_attempt.last_attempt_at >= success_at
        assert failed.resource_attempt.last_success_at == success_at
        assert failed.resource_attempt.last_success_has_representative is True

        validated, metadata = _source_fixture(source_id)
        generation = await store.next_generation(source_id)
        assert generation == 2
        await store.publish(
            source_id,
            1,
            generation,
            validated.document,
            SourceSecretCipher(b"v" * 32).encrypt(
                source_id,
                generation,
                "rotated-reader-secret",
            ),
            metadata,
            expected_state_version=1,
        )
        with pytest.raises(ResourceObservationConflictError):
            await store.report_resource_observation_failure(
                source_id,
                1,
                "METADATA_UNAVAILABLE",
            )

        await store.report_resource_observation_failure(
            source_id,
            generation,
            "METADATA_UNAVAILABLE",
        )
        reset = await store.get_source_usage(source_id)
        assert reset is not None
        assert reset.generation == generation
        assert reset.resource_attempt is not None
        assert reset.resource_attempt.generation == generation
        assert reset.resource_attempt.last_attempt_outcome == "failed"
        assert reset.resource_attempt.last_success_at is None
        assert reset.resource_attempt.last_success_has_representative is None

        await store.report_resource_observations(
            source_id,
            generation,
            metadata_revision,
            _resource_observation_batch(
                definition,
                include_representative=False,
                value_offset=10,
            ),
        )
        current = await store.get_source_usage(source_id)
        assert current is not None
        assert current.resource_attempt is not None
        current_success_at = current.resource_attempt.last_success_at
        assert current_success_at is not None
        assert current.resource_attempt.last_success_has_representative is False
        matching_metrics = {
            row.metric
            for row in current.resource_observations
            if row.current.observed_at == current_success_at
        }
        assert matching_metrics == {
            "table_bytes",
            "index_bytes",
            "total_storage_bytes",
        }
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_usage_distinguishes_absent_from_malformed_observability(
    disposable_control_dsn: str,
) -> None:
    source_id = "resource-config-shape"
    validated, metadata = _source_fixture(
        source_id,
        resource_observability=False,
    )
    store = PostgresSourceStore(disposable_control_dsn)
    await store.publish(
        source_id,
        0,
        1,
        validated.document,
        SourceSecretCipher(b"w" * 32).encrypt(
            source_id,
            1,
            "reader-secret",
        ),
        metadata,
        expected_state_version=0,
    )
    try:
        absent = await store.get_source_usage(source_id)
        assert absent is not None
        assert absent.resource_configured is False

        for index, malformed in enumerate((None, [], "invalid"), start=1):
            malformed_source_id = f"resource-config-malformed-{index}"
            malformed_validated, malformed_metadata = _source_fixture(
                malformed_source_id,
                resource_observability=False,
            )
            malformed_manifest = dict(malformed_validated.document)
            malformed_manifest["observability"] = malformed
            await store.publish(
                malformed_source_id,
                0,
                1,
                malformed_manifest,
                SourceSecretCipher(b"x" * 32).encrypt(
                    malformed_source_id,
                    1,
                    "reader-secret",
                ),
                malformed_metadata,
                expected_state_version=0,
            )
            with pytest.raises(RuntimeError, match="configuration is invalid"):
                await store.get_source_usage(malformed_source_id)
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resource_observation_batch_rolls_back_atomically(
    disposable_control_dsn: str,
) -> None:
    source_id = "resource-atomic-source"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    definition = f"sha256:{'c' * 64}"
    connection = await AsyncConnection.connect(disposable_control_dsn)
    try:
        await connection.execute(
            "ALTER TABLE control.source_resource_observations "
            "ADD CONSTRAINT resource_atomic_test_reject_index "
            "CHECK (metric <> 'index_bytes')"
        )
        await connection.commit()
    finally:
        await connection.close()

    try:
        with pytest.raises(Error):
            await store.report_resource_observations(
                source_id,
                1,
                metadata_revision,
                (
                    _ResourceObservationWrite(
                        "table_bytes",
                        10,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                    _ResourceObservationWrite(
                        "index_bytes",
                        2,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                    _ResourceObservationWrite(
                        "total_storage_bytes",
                        12,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                ),
            )
        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT count(*) FROM control.source_resource_observations "
                "WHERE source_id = %s",
                (source_id,),
            )
            assert await cursor.fetchone() == (0,)
            cursor = await connection.execute(
                "SELECT count(*) "
                "FROM control.source_resource_observation_attempts "
                "WHERE source_id = %s",
                (source_id,),
            )
            assert await cursor.fetchone() == (0,)
        finally:
            await connection.close()
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_resource_reports_preserve_latest_db_clock_sample(
    disposable_control_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = "resource-concurrent-source"
    store_a, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    store_b = PostgresSourceStore(disposable_control_dsn)
    definition = f"sha256:{'4' * 64}"
    first_acquired = asyncio.Event()
    second_reached = asyncio.Event()
    release_first = asyncio.Event()
    call_count = 0
    original_lock = source_store_module._lock_resource_observation

    async def coordinated_lock(connection: object, locked_source_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await original_lock(connection, locked_source_id)
            first_acquired.set()
            await release_first.wait()
            return
        second_reached.set()
        await original_lock(connection, locked_source_id)

    monkeypatch.setattr(
        source_store_module,
        "_lock_resource_observation",
        coordinated_lock,
    )
    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        first_task = asyncio.create_task(
            store_a.report_resource_observations(
                source_id,
                1,
                metadata_revision,
                (
                    _ResourceObservationWrite(
                        "table_bytes",
                        10,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                    _ResourceObservationWrite(
                        "index_bytes",
                        2,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                    _ResourceObservationWrite(
                        "total_storage_bytes",
                        12,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                ),
            )
        )
        await asyncio.wait_for(first_acquired.wait(), timeout=1)
        second_task = asyncio.create_task(
            store_b.report_resource_observations(
                source_id,
                1,
                metadata_revision,
                (
                    _ResourceObservationWrite(
                        "index_bytes",
                        4,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                    _ResourceObservationWrite(
                        "table_bytes",
                        20,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                    _ResourceObservationWrite(
                        "total_storage_bytes",
                        24,
                        "bytes",
                        "postgres_relation_size",
                        definition,
                    ),
                ),
            )
        )
        await asyncio.wait_for(second_reached.wait(), timeout=1)
        release_first.set()
        async with asyncio.timeout(10):
            await asyncio.gather(first_task, second_task)

        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT metric, value, previous_value, "
                "fresh_until - observed_at "
                "FROM control.source_resource_observations "
                "WHERE source_id = %s ORDER BY metric",
                (source_id,),
            )
            assert await cursor.fetchall() == [
                ("index_bytes", 4, None, timedelta(hours=72)),
                ("table_bytes", 20, None, timedelta(hours=72)),
                ("total_storage_bytes", 24, None, timedelta(hours=72)),
            ]
        finally:
            await connection.close()
    finally:
        release_first.set()
        for task in (first_task, second_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        await store_b.close()
        await store_a.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_gateway_report_advances_and_replays_cursor_without_rollup(
    disposable_control_dsn: str,
) -> None:
    replica_id = "gateway-empty-replica"
    store = PostgresSourceStore(disposable_control_dsn)
    try:
        incarnation = await store.register_replica(replica_id, 5_000)
        await store.report_gateway_usage(replica_id, incarnation, 1, ())
        first = await _gateway_usage_rows(
            disposable_control_dsn,
            "missing-empty-source",
            replica_id,
        )
        assert first[0] == []
        assert len(first[1]) == 1
        assert first[1][0][0:3] == (replica_id, incarnation, 1)
        assert first[1][0][3].startswith("sha256:")
        assert first[1][0][5] - first[1][0][4] == timedelta(seconds=180)

        await store.report_gateway_usage(replica_id, incarnation, 1, ())
        assert await _gateway_usage_rows(
            disposable_control_dsn,
            "missing-empty-source",
            replica_id,
        ) == first

        await store.report_gateway_usage(replica_id, incarnation, 2, ())
        advanced = await _gateway_usage_rows(
            disposable_control_dsn,
            "missing-empty-source",
            replica_id,
        )
        assert advanced[0] == []
        assert advanced[1][0][0:3] == (replica_id, incarnation, 2)
        assert advanced[1][0][5] - advanced[1][0][4] == timedelta(seconds=180)
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_usage_projection_uses_global_live_reporter_health_and_fixed_order(
    disposable_control_dsn: str,
) -> None:
    source_id = "source-usage-projection"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    replica_a = "source-usage-reporter-a"
    replica_b = "source-usage-reporter-b"
    try:
        incarnation_a = await store.register_replica(replica_a, 300_000)
        incarnation_b = await store.register_replica(replica_b, 300_000)
        current_bucket = datetime.now(UTC).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        current_delta = _gateway_usage_delta(
            source_id,
            metadata_revision,
            bucket_start=current_bucket,
        )
        previous_delta = replace(
            current_delta,
            budget_profile="batch",
            definition_revision=f"sha256:{'e' * 64}",
            bucket_start=current_bucket - timedelta(hours=1),
        )
        await store.report_gateway_usage(
            replica_a,
            incarnation_a,
            1,
            (previous_delta, current_delta),
        )
        await store.report_gateway_usage(replica_b, incarnation_b, 1, ())
        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            await connection.execute(
                "INSERT INTO control.gateway_usage_rollups "
                "(source_id, budget_profile, metadata_revision, "
                "definition_revision, bucket_start) VALUES "
                "(%s, 'window_start', %s, %s, "
                "date_trunc('hour', clock_timestamp()) - interval '31 days'), "
                "(%s, 'window_end', %s, %s, "
                "date_trunc('hour', clock_timestamp())), "
                "(%s, 'before_window', %s, %s, "
                "date_trunc('hour', clock_timestamp()) - "
                "interval '31 days 1 hour'), "
                "(%s, 'after_window', %s, %s, "
                "date_trunc('hour', clock_timestamp()) + interval '1 hour')",
                (
                    source_id,
                    metadata_revision,
                    f"sha256:{'1' * 64}",
                    source_id,
                    metadata_revision,
                    f"sha256:{'2' * 64}",
                    source_id,
                    metadata_revision,
                    f"sha256:{'3' * 64}",
                    source_id,
                    metadata_revision,
                    f"sha256:{'4' * 64}",
                ),
            )
            await connection.commit()
        finally:
            await connection.close()

        available = await store.get_source_usage(source_id)
        assert available is not None
        assert available.read_at.tzinfo is not None
        assert available.window_end == available.read_at.replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        assert available.window_start == available.window_end - timedelta(days=31)
        assert (
            available.live_reporter_count,
            available.live_current_cursor_count,
            available.live_fresh_cursor_count,
            available.accepted_cursor_count,
        ) == (2, 2, 2, 2)
        assert available.last_report_at is not None
        assert available.reporter_fresh_until is not None
        rollups = list(available.gateway_rollups)
        ordered = list(rollups)
        ordered.sort(key=lambda row: row.definition_revision)
        ordered.sort(key=lambda row: row.metadata_revision)
        ordered.sort(key=lambda row: row.budget_profile)
        ordered.sort(key=lambda row: row.observed_at, reverse=True)
        ordered.sort(key=lambda row: row.bucket_start, reverse=True)
        assert rollups == ordered
        by_budget = {row.budget_profile: row for row in rollups}
        assert by_budget["window_start"].bucket_start == available.window_start
        assert by_budget["window_end"].bucket_start == available.window_end
        assert "before_window" not in by_budget
        assert "after_window" not in by_budget

        restarted_incarnation = await store.register_replica(replica_b, 300_000)
        assert restarted_incarnation == incarnation_b + 1
        unavailable = await store.get_source_usage(source_id)
        assert unavailable is not None
        assert (
            unavailable.live_reporter_count,
            unavailable.live_current_cursor_count,
            unavailable.live_fresh_cursor_count,
            unavailable.accepted_cursor_count,
        ) == (2, 1, 1, 2)

        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            await connection.execute(
                "UPDATE control.runtime_replicas "
                "SET observed_at = clock_timestamp() - interval '16 minutes' "
                "WHERE replica_id IN (%s, %s)",
                (replica_a, replica_b),
            )
            await connection.commit()
        finally:
            await connection.close()
        stale = await store.get_source_usage(source_id)
        assert stale is not None
        assert (
            stale.live_reporter_count,
            stale.live_current_cursor_count,
            stale.live_fresh_cursor_count,
            stale.accepted_cursor_count,
        ) == (0, 0, 0, 2)
        assert stale.last_report_at is not None
        assert stale.reporter_fresh_until is None
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_usage_projection_fails_closed_above_rollup_bound(
    disposable_control_dsn: str,
) -> None:
    source_id = "source-usage-cardinality"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    connection = await AsyncConnection.connect(disposable_control_dsn)
    try:
        await connection.execute(
            "INSERT INTO control.gateway_usage_rollups "
            "(source_id, budget_profile, metadata_revision, "
            "definition_revision, bucket_start) "
            "SELECT %s, budget_profile, %s, %s, "
            "date_trunc('hour', clock_timestamp()) - hour_offset * interval '1 hour' "
            "FROM generate_series(0, 500) AS hour_offset "
            "CROSS JOIN unnest(ARRAY['batch', 'interactive']) AS budget_profile",
            (
                source_id,
                metadata_revision,
                f"sha256:{'9' * 64}",
            ),
        )
        await connection.commit()
    finally:
        await connection.close()

    try:
        with pytest.raises(RuntimeError, match="cardinality"):
            await store.get_source_usage(source_id)
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gateway_usage_rejects_buckets_outside_logical_window_atomically(
    disposable_control_dsn: str,
) -> None:
    source_id = "gateway-window-source"
    replica_id = "gateway-window-replica"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    try:
        incarnation = await store.register_replica(replica_id, 5_000)
        current_bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        for bucket_start in (
            current_bucket - timedelta(days=31, hours=1),
            current_bucket + timedelta(hours=1),
        ):
            with pytest.raises(ValueError, match="outside the retention window"):
                await store.report_gateway_usage(
                    replica_id,
                    incarnation,
                    1,
                    (
                        _gateway_usage_delta(
                            source_id,
                            metadata_revision,
                            bucket_start=bucket_start,
                        ),
                    ),
                )

        assert await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        ) == ([], [])

        old_delta = _gateway_usage_delta(
            source_id,
            metadata_revision,
            bucket_start=current_bucket - timedelta(days=31, hours=1),
        )
        payload_hash = source_store_module._gateway_usage_payload_hash((old_delta,))
        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            await connection.execute(
                "WITH old_clock AS MATERIALIZED ("
                "SELECT clock_timestamp() - interval '32 days' AS observed_at"
                ") INSERT INTO control.gateway_usage_report_cursors "
                "(replica_id, incarnation, last_sequence, last_payload_hash, "
                "observed_at, fresh_until) SELECT %s, %s, 1, %s, observed_at, "
                "observed_at + interval '180 seconds' FROM old_clock",
                (replica_id, incarnation, payload_hash),
            )
            await connection.commit()
        finally:
            await connection.close()

        before_replay = await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        )
        await store.report_gateway_usage(
            replica_id,
            incarnation,
            1,
            (old_delta,),
        )
        assert await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        ) == before_replay
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gateway_source_lock_wait_does_not_block_replica_heartbeat(
    disposable_control_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = "gateway-lock-source"
    replica_id = "gateway-lock-replica"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    blocker = await AsyncConnection.connect(disposable_control_dsn)
    gateway_task: asyncio.Task[None] | None = None
    original_lock = source_store_module._lock_gateway_usage
    gateway_reached_lock = asyncio.Event()

    async def tracked_lock(connection: object, locked_source_id: str) -> None:
        gateway_reached_lock.set()
        await original_lock(connection, locked_source_id)

    monkeypatch.setattr(source_store_module, "_lock_gateway_usage", tracked_lock)
    try:
        incarnation = await store.register_replica(replica_id, 5_000)
        async with blocker.transaction():
            await original_lock(blocker, source_id)
            gateway_task = asyncio.create_task(
                store.report_gateway_usage(
                    replica_id,
                    incarnation,
                    1,
                    (_gateway_usage_delta(source_id, metadata_revision),),
                )
            )
            await asyncio.wait_for(gateway_reached_lock.wait(), timeout=1)
            await asyncio.sleep(0)

            await asyncio.wait_for(
                store.report_replica(
                    replica_id,
                    incarnation,
                    reason_code="CONTROL_SCAN_FAILED",
                    sources=(),
                ),
                timeout=1,
            )
            assert not gateway_task.done()

        await asyncio.wait_for(gateway_task, timeout=5)
        rollups, cursors = await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        )
        assert len(rollups) == 1
        assert cursors[0][0:3] == (replica_id, incarnation, 1)
    finally:
        if gateway_task is not None and not gateway_task.done():
            gateway_task.cancel()
            with suppress(asyncio.CancelledError):
                await gateway_task
        await blocker.close()
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_gateway_reports_do_not_regress_rollup_observed_at(
    disposable_control_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = "gateway-observed-at-source"
    store_a, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    store_b = PostgresSourceStore(disposable_control_dsn)
    replica_a = "gateway-observed-at-a"
    replica_b = "gateway-observed-at-b"
    first_reached = asyncio.Event()
    release_first = asyncio.Event()
    call_count = 0
    original_lock = source_store_module._lock_gateway_usage

    async def delay_first_lock(connection: object, locked_source_id: str) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_reached.set()
            await release_first.wait()
        await original_lock(connection, locked_source_id)

    monkeypatch.setattr(source_store_module, "_lock_gateway_usage", delay_first_lock)
    first_task: asyncio.Task[None] | None = None
    try:
        incarnation_a = await store_a.register_replica(replica_a, 5_000)
        incarnation_b = await store_b.register_replica(replica_b, 5_000)
        delta = _gateway_usage_delta(source_id, metadata_revision)
        first_task = asyncio.create_task(
            store_a.report_gateway_usage(
                replica_a,
                incarnation_a,
                1,
                (delta,),
            )
        )
        await asyncio.wait_for(first_reached.wait(), timeout=1)
        await store_b.report_gateway_usage(
            replica_b,
            incarnation_b,
            1,
            (delta,),
        )

        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT observed_at FROM control.gateway_usage_rollups "
                "WHERE source_id = %s AND budget_profile = 'interactive'",
                (source_id,),
            )
            newer_row = await cursor.fetchone()
            assert newer_row is not None
            newer_observed_at = newer_row[0]
        finally:
            await connection.close()

        release_first.set()
        await asyncio.wait_for(first_task, timeout=5)
        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT query_count, success_count, observed_at "
                "FROM control.gateway_usage_rollups "
                "WHERE source_id = %s AND budget_profile = 'interactive'",
                (source_id,),
            )
            assert await cursor.fetchone() == (2, 2, newer_observed_at)
        finally:
            await connection.close()
    finally:
        release_first.set()
        if first_task is not None and not first_task.done():
            first_task.cancel()
            with suppress(asyncio.CancelledError):
                await first_task
        await store_b.close()
        await store_a.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_first_gateway_reports_are_idempotent_and_conflict_safe(
    disposable_control_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = "gateway-first-report-source"
    store_a, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    store_b = PostgresSourceStore(disposable_control_dsn)
    same_replica = "gateway-first-report-same"
    conflict_replica = "gateway-first-report-conflict"
    original_lock = source_store_module._lock_gateway_reporter
    barrier = asyncio.Barrier(2)

    async def synchronized_lock(connection: object, replica_id: str) -> None:
        await barrier.wait()
        await original_lock(connection, replica_id)

    monkeypatch.setattr(source_store_module, "_lock_gateway_reporter", synchronized_lock)
    delta = _gateway_usage_delta(source_id, metadata_revision)
    try:
        same_incarnation = await store_a.register_replica(same_replica, 5_000)
        async with asyncio.timeout(10):
            await asyncio.gather(
                store_a.report_gateway_usage(
                    same_replica,
                    same_incarnation,
                    1,
                    (delta,),
                ),
                store_b.report_gateway_usage(
                    same_replica,
                    same_incarnation,
                    1,
                    (delta,),
                ),
            )
        rollups, cursors = await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            same_replica,
        )
        assert len(rollups) == 1
        assert rollups[0][5:] == (1, 1, 0, 0, 0, 0, 0, 2, 3, 4, 5, 0)
        assert cursors[0][0:3] == (same_replica, same_incarnation, 1)

        barrier = asyncio.Barrier(2)
        conflict_incarnation = await store_a.register_replica(conflict_replica, 5_000)
        payloads = ((delta,), (replace(delta, result_bytes_sum=6),))
        async with asyncio.timeout(10):
            results = await asyncio.gather(
                store_a.report_gateway_usage(
                    conflict_replica,
                    conflict_incarnation,
                    1,
                    payloads[0],
                ),
                store_b.report_gateway_usage(
                    conflict_replica,
                    conflict_incarnation,
                    1,
                    payloads[1],
                ),
                return_exceptions=True,
            )
        assert sum(result is None for result in results) == 1
        assert sum(
            isinstance(result, GatewayUsageConflictError) for result in results
        ) == 1
        winner = next(index for index, result in enumerate(results) if result is None)
        rollups, cursors = await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            conflict_replica,
        )
        assert rollups[0][5:7] == (2, 2)
        assert rollups[0][15] in (10, 11)
        assert cursors[0][0:4] == (
            conflict_replica,
            conflict_incarnation,
            1,
            source_store_module._gateway_usage_payload_hash(payloads[winner]),
        )
    finally:
        await store_b.close()
        await store_a.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gateway_usage_is_fenced_idempotent_additive_and_atomic(
    disposable_control_dsn: str,
) -> None:
    source_id = "gateway-usage-source"
    replica_id = "gateway-replica"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    delta = _gateway_usage_delta(source_id, metadata_revision)
    maximum_batch = (delta,) * 100
    try:
        incarnation = await store.register_replica(replica_id, 5_000)
        await store.report_gateway_usage(
            replica_id,
            incarnation,
            1,
            maximum_batch,
        )
        first = await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        )
        assert len(first[0]) == 1
        assert first[0][0][5:] == (
            100,
            100,
            0,
            0,
            0,
            0,
            0,
            200,
            300,
            400,
            500,
            0,
        )
        assert len(first[1]) == 1
        assert first[1][0][0:3] == (replica_id, incarnation, 1)
        assert first[1][0][3].startswith("sha256:")
        assert first[1][0][5] - first[1][0][4] == timedelta(seconds=180)

        await store.report_gateway_usage(
            replica_id,
            incarnation,
            1,
            maximum_batch,
        )
        assert await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        ) == first

        changed_payload = (
            replace(delta, result_bytes_sum=delta.result_bytes_sum + 1),
            *maximum_batch[1:],
        )
        with pytest.raises(GatewayUsageConflictError):
            await store.report_gateway_usage(
                replica_id,
                incarnation,
                1,
                changed_payload,
            )
        with pytest.raises(GatewayUsageConflictError):
            await store.report_gateway_usage(
                replica_id,
                incarnation,
                3,
                (delta,),
            )
        assert await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        ) == first

        await store.report_gateway_usage(
            replica_id,
            incarnation,
            2,
            (delta,),
        )
        added = await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        )
        assert added[0][0][5:] == (
            101,
            101,
            0,
            0,
            0,
            0,
            0,
            202,
            303,
            404,
            505,
            0,
        )
        assert added[1][0][0:3] == (replica_id, incarnation, 2)

        with pytest.raises(Error):
            await store.report_gateway_usage(
                replica_id,
                incarnation,
                3,
                (
                    delta,
                    replace(delta, source_id="missing-gateway-source"),
                ),
            )
        assert await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        ) == added

        new_incarnation = await store.register_replica(replica_id, 5_000)
        assert new_incarnation == incarnation + 1
        with pytest.raises(GatewayUsageConflictError):
            await store.report_gateway_usage(
                replica_id,
                incarnation,
                3,
                (delta,),
            )
        await store.report_gateway_usage(
            replica_id,
            new_incarnation,
            1,
            (delta,),
        )
        restarted = await _gateway_usage_rows(
            disposable_control_dsn,
            source_id,
            replica_id,
        )
        assert restarted[0][0][5:] == (
            102,
            102,
            0,
            0,
            0,
            0,
            0,
            204,
            306,
            408,
            510,
            0,
        )
        assert restarted[1][0][0:3] == (replica_id, new_incarnation, 1)
    finally:
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gateway_usage_caps_touched_source_without_age_based_deletion(
    disposable_control_dsn: str,
) -> None:
    source_id = "gateway-pruning-source"
    other_source_id = "gateway-pruning-other"
    store, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    other_store, other_metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        other_source_id,
    )
    replica_id = "gateway-pruning-replica"
    definition_revision = f"sha256:{'1' * 64}"
    connection = await AsyncConnection.connect(disposable_control_dsn)
    try:
        cursor = await connection.execute(
            "SELECT date_trunc('hour', clock_timestamp() AT TIME ZONE 'UTC') "
            "AT TIME ZONE 'UTC'"
        )
        clock_row = await cursor.fetchone()
        assert clock_row is not None
        current_bucket = clock_row[0]
        assert isinstance(current_bucket, datetime)
        old_bucket = current_bucket - timedelta(days=31, hours=1)

        await connection.execute(
            "INSERT INTO control.gateway_usage_rollups "
            "(source_id, budget_profile, metadata_revision, "
            "definition_revision, bucket_start, query_count, success_count, "
            "rejected_count, timeout_count, overloaded_count, cancelled_count, "
            "failed_count, queue_ms_sum, elapsed_ms_sum, returned_rows_sum, "
            "result_bytes_sum, truncated_count, observed_at) "
            "SELECT %s, 'profile_' || lpad(item::text, 4, '0'), %s, %s, %s, "
            "1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "
            "%s - (1001 - item) * interval '1 second' "
            "FROM generate_series(0, 1000) AS item",
            (
                source_id,
                metadata_revision,
                definition_revision,
                current_bucket,
                current_bucket,
            ),
        )
        await connection.execute(
            "INSERT INTO control.gateway_usage_rollups "
            "(source_id, budget_profile, metadata_revision, "
            "definition_revision, bucket_start, query_count, success_count, "
            "rejected_count, timeout_count, overloaded_count, cancelled_count, "
            "failed_count, queue_ms_sum, elapsed_ms_sum, returned_rows_sum, "
            "result_bytes_sum, truncated_count, observed_at) "
            "VALUES (%s, 'old_profile', %s, %s, %s, "
            "1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, %s)",
            (
                other_source_id,
                other_metadata_revision,
                definition_revision,
                old_bucket,
                old_bucket,
            ),
        )
        await connection.commit()

        cursor = await connection.execute(
            "SELECT source_id, count(*) FROM control.gateway_usage_rollups "
            "GROUP BY source_id ORDER BY source_id"
        )
        assert await cursor.fetchall() == [
            (other_source_id, 1),
            (source_id, 1_001),
        ]
    finally:
        await connection.close()

    try:
        incarnation = await store.register_replica(replica_id, 5_000)
        await store.report_gateway_usage(
            replica_id,
            incarnation,
            1,
            (
                _gateway_usage_delta(
                    source_id,
                    metadata_revision,
                    bucket_start=current_bucket,
                ),
                _gateway_usage_delta(
                    other_source_id,
                    other_metadata_revision,
                    bucket_start=current_bucket,
                ),
            ),
        )

        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT count(*), "
                "bool_or(budget_profile = 'profile_0000'), "
                "bool_or(budget_profile = 'profile_0001'), "
                "bool_or(budget_profile = 'profile_0002'), "
                "bool_or(budget_profile = 'profile_1000'), "
                "bool_or(budget_profile = 'interactive') "
                "FROM control.gateway_usage_rollups WHERE source_id = %s",
                (source_id,),
            )
            assert await cursor.fetchone() == (
                1_000,
                False,
                False,
                True,
                True,
                True,
            )
            cursor = await connection.execute(
                "SELECT count(*), "
                "bool_or(budget_profile = 'old_profile'), "
                "bool_or(budget_profile = 'interactive'), "
                "min(bucket_start) < %s "
                "FROM control.gateway_usage_rollups WHERE source_id = %s",
                (current_bucket - timedelta(days=31), other_source_id),
            )
            assert await cursor.fetchone() == (2, True, True, True)
        finally:
            await connection.close()
    finally:
        await other_store.close()
        await store.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_two_replicas_preserve_gateway_cap_and_replay_concurrently(
    disposable_control_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = "gateway-concurrent-source"
    store_a, metadata_revision = await _publish_observation_source(
        disposable_control_dsn,
        source_id,
    )
    store_b = PostgresSourceStore(disposable_control_dsn)
    replica_a = "gateway-concurrent-a"
    replica_b = "gateway-concurrent-b"
    connection = await AsyncConnection.connect(disposable_control_dsn)
    try:
        cursor = await connection.execute(
            "SELECT date_trunc('hour', clock_timestamp() AT TIME ZONE 'UTC') "
            "AT TIME ZONE 'UTC'"
        )
        clock_row = await cursor.fetchone()
        assert clock_row is not None
        current_bucket = clock_row[0]
        assert isinstance(current_bucket, datetime)
        await connection.execute(
            "INSERT INTO control.gateway_usage_rollups "
            "(source_id, budget_profile, metadata_revision, "
            "definition_revision, bucket_start, query_count, success_count, "
            "observed_at) "
            "SELECT %s, 'seed_' || lpad(item::text, 4, '0'), %s, %s, %s, 1, 1, "
            "%s - (998 - item) * interval '1 second' "
            "FROM generate_series(0, 997) AS item",
            (
                source_id,
                metadata_revision,
                f"sha256:{'2' * 64}",
                current_bucket,
                current_bucket,
            ),
        )
        await connection.commit()
    finally:
        await connection.close()

    original_lock = source_store_module._lock_gateway_usage
    barrier = asyncio.Barrier(2)

    async def synchronized_lock(connection: object, locked_source_id: str) -> None:
        await barrier.wait()
        await original_lock(connection, locked_source_id)

    monkeypatch.setattr(source_store_module, "_lock_gateway_usage", synchronized_lock)
    shared = replace(
        _gateway_usage_delta(
            source_id,
            metadata_revision,
            bucket_start=current_bucket,
        ),
        budget_profile="shared",
    )
    alpha_only = replace(shared, budget_profile="alpha_only")
    beta_only = replace(shared, budget_profile="beta_only")
    try:
        incarnation_a = await store_a.register_replica(replica_a, 5_000)
        incarnation_b = await store_b.register_replica(replica_b, 5_000)
        reports = (
            store_a.report_gateway_usage(
                replica_a,
                incarnation_a,
                1,
                (shared, alpha_only),
            ),
            store_b.report_gateway_usage(
                replica_b,
                incarnation_b,
                1,
                (shared, beta_only),
            ),
        )
        async with asyncio.timeout(10):
            await asyncio.gather(*reports)

        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT count(*), "
                "bool_or(budget_profile = 'seed_0000'), "
                "bool_and(budget_profile IN ('shared', 'alpha_only', 'beta_only')) "
                "FILTER (WHERE budget_profile IN "
                "('shared', 'alpha_only', 'beta_only')) "
                "FROM control.gateway_usage_rollups WHERE source_id = %s",
                (source_id,),
            )
            assert await cursor.fetchone() == (1_000, False, True)
            cursor = await connection.execute(
                "SELECT query_count, success_count, queue_ms_sum, elapsed_ms_sum, "
                "returned_rows_sum, result_bytes_sum "
                "FROM control.gateway_usage_rollups "
                "WHERE source_id = %s AND budget_profile = 'shared'",
                (source_id,),
            )
            assert await cursor.fetchone() == (2, 2, 4, 6, 8, 10)
            cursor = await connection.execute(
                "SELECT replica_id, incarnation, last_sequence "
                "FROM control.gateway_usage_report_cursors "
                "WHERE replica_id IN (%s, %s) ORDER BY replica_id",
                (replica_a, replica_b),
            )
            assert await cursor.fetchall() == [
                (replica_a, incarnation_a, 1),
                (replica_b, incarnation_b, 1),
            ]
            cursor = await connection.execute(
                "SELECT md5(string_agg(row_to_json(rollup)::text, '|' "
                "ORDER BY budget_profile, metadata_revision, definition_revision, "
                "bucket_start)) FROM control.gateway_usage_rollups AS rollup "
                "WHERE source_id = %s",
                (source_id,),
            )
            before_replay = await cursor.fetchone()
        finally:
            await connection.close()

        await asyncio.gather(
            store_a.report_gateway_usage(
                replica_a,
                incarnation_a,
                1,
                (shared, alpha_only),
            ),
            store_b.report_gateway_usage(
                replica_b,
                incarnation_b,
                1,
                (shared, beta_only),
            ),
        )
        connection = await AsyncConnection.connect(disposable_control_dsn)
        try:
            cursor = await connection.execute(
                "SELECT md5(string_agg(row_to_json(rollup)::text, '|' "
                "ORDER BY budget_profile, metadata_revision, definition_revision, "
                "bucket_start)) FROM control.gateway_usage_rollups AS rollup "
                "WHERE source_id = %s",
                (source_id,),
            )
            assert await cursor.fetchone() == before_replay
        finally:
            await connection.close()
    finally:
        await store_b.close()
        await store_a.close()


@pytest.mark.asyncio
async def test_resource_and_gateway_reports_reject_batches_above_contract_limits() -> None:
    store = PostgresSourceStore("postgresql://unused")
    definition = f"sha256:{'e' * 64}"
    sample = _ResourceObservationWrite(
        "table_bytes",
        1,
        "bytes",
        "postgres_relation_size",
        definition,
    )
    delta = _gateway_usage_delta("bounded-source", f"sha256:{'f' * 64}")

    with pytest.raises(ValueError, match="batch size"):
        await store.report_resource_observations(
            "bounded-source",
            1,
            f"sha256:{'f' * 64}",
            (),
        )
    with pytest.raises(ValueError, match="batch size"):
        await store.report_resource_observations(
            "bounded-source",
            1,
            f"sha256:{'f' * 64}",
            (sample,) * 5,
        )
    with pytest.raises(ValueError, match="batch is too large"):
        await store.report_gateway_usage(
            "bounded-replica",
            1,
            1,
            (delta,) * 101,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "success_only_field",
    [
        "queue_ms_sum",
        "elapsed_ms_sum",
        "returned_rows_sum",
        "result_bytes_sum",
    ],
)
async def test_gateway_report_rejects_success_only_sums_without_success_before_io(
    success_only_field: str,
) -> None:
    store = PostgresSourceStore("postgresql://unused")
    delta = replace(
        _gateway_usage_delta("bounded-source", f"sha256:{'f' * 64}"),
        success_count=0,
        rejected_count=1,
        queue_ms_sum=0,
        elapsed_ms_sum=0,
        returned_rows_sum=0,
        result_bytes_sum=0,
    )
    invalid_delta = replace(delta, **{success_only_field: 1})

    with pytest.raises(ValueError, match="success-only sums"):
        await store.report_gateway_usage(
            "bounded-replica",
            1,
            1,
            (invalid_delta,),
        )


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
    ("replica_id", "heartbeat_interval_ms"),
    [
        ("", 5_000),
        ("UPPERCASE", 5_000),
        ("unsafe/replica", 5_000),
        ("a" * 81, 5_000),
        ("safe-replica", 4_999),
        ("safe-replica", 300_001),
        ("safe-replica", True),
    ],
)
async def test_replica_registration_rejects_invalid_identity_and_interval_before_io(
    replica_id: str,
    heartbeat_interval_ms: int,
) -> None:
    store = PostgresSourceStore("postgresql://unused")

    with pytest.raises(ValueError, match=r"Replica ID|heartbeat interval"):
        await store.register_replica(replica_id, heartbeat_interval_ms)


@pytest.mark.asyncio
async def test_replica_reporting_rejects_invalid_bounded_state_before_io() -> None:
    store = PostgresSourceStore("postgresql://unused")
    revision = f"sha256:{'0' * 64}"
    valid = _ReplicaSourceObservationWrite(
        source_id="safe-source",
        applied_generation=1,
        applied_state_version=1,
        applied_enabled=True,
        applied_metadata_revision=revision,
        source_health="healthy",
        reason_code=None,
    )

    invalid_reports = (
        {"incarnation": 0, "reason_code": None, "sources": ()},
        {"incarnation": 1, "reason_code": "UNKNOWN", "sources": ()},
        {
            "incarnation": 1,
            "reason_code": "CONTROL_SCAN_FAILED",
            "sources": (valid,),
        },
        {"incarnation": 1, "reason_code": None, "sources": (valid, valid)},
        {
            "incarnation": 1,
            "reason_code": None,
            "sources": (replace(valid, applied_state_version=None),),
        },
        {
            "incarnation": 1,
            "reason_code": None,
            "sources": (replace(valid, applied_enabled=False),),
        },
        {
            "incarnation": 1,
            "reason_code": None,
            "sources": (replace(valid, source_health="unknown"),),
        },
        {
            "incarnation": 1,
            "reason_code": None,
            "sources": (replace(valid, reason_code="UNKNOWN"),),
        },
    )
    for report in invalid_reports:
        with pytest.raises(ValueError):
            await store.report_replica(
                "safe-replica",
                cast(int, report["incarnation"]),
                reason_code=cast(str | None, report["reason_code"]),
                sources=cast(
                    tuple[_ReplicaSourceObservationWrite, ...],
                    report["sources"],
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True])
async def test_replica_projection_rejects_unbounded_page_limits(limit: int) -> None:
    store = PostgresSourceStore("postgresql://unused")

    with pytest.raises(ValueError, match="page limit"):
        await store.list_replica_observations("safe-source", limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", "UPPERCASE", "unsafe/cursor", "a" * 81])
async def test_replica_projection_rejects_invalid_keyset_cursor(cursor: str) -> None:
    store = PostgresSourceStore("postgresql://unused")

    with pytest.raises(ValueError, match="Replica ID"):
        await store.list_replica_observations(
            "safe-source",
            after_replica_id=cursor,
        )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True])
async def test_source_mutations_reject_unbounded_page_limits(limit: int) -> None:
    store = PostgresSourceStore("postgresql://unused")

    with pytest.raises(ValueError, match="page limit"):
        await store.list_mutations("source", limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "before_event_id",
    [0, POSTGRES_BIGINT_MAX + 1, True, 1.5, "1"],
)
async def test_source_mutations_reject_invalid_event_cursors(
    before_event_id: object,
) -> None:
    store = PostgresSourceStore("postgresql://unused")

    with pytest.raises(ValueError, match="event cursor"):
        await store.list_mutations(
            "source",
            before_event_id=before_event_id,  # type: ignore[arg-type]
        )


def test_source_mutation_decoder_accepts_terminal_receipts() -> None:
    succeeded = _mutation_row()
    decoded_success = _decode_mutation(succeeded)

    assert decoded_success.event_id == 1
    assert decoded_success.idempotency_key == succeeded["idempotency_key"]
    assert decoded_success.outcome == "succeeded"
    assert decoded_success.result == succeeded["result"]

    rejected = {
        **succeeded,
        "event_id": 2,
        "outcome": "rejected",
        "resulting_generation": None,
        "resulting_state_version": None,
        "http_status": 409,
        "error_code": "SOURCE_GENERATION_CONFLICT",
        "result": {},
    }
    decoded_rejection = _decode_mutation(rejected)

    assert decoded_rejection.event_id == 2
    assert decoded_rejection.outcome == "rejected"
    assert decoded_rejection.error_code == "SOURCE_GENERATION_CONFLICT"
    assert decoded_rejection.result == {}


def test_source_mutation_decoder_rejects_resulting_generation_mismatch() -> None:
    row = _mutation_row()
    result = dict(cast(dict[str, object], row["result"]))
    result["generation"] = 2
    row["result"] = result

    with pytest.raises(ValueError, match="resulting generation"):
        _decode_mutation(row)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", 0),
        ("idempotency_key", "A8D4306F-3628-49BC-A09E-17CFE947FCB1"),
        ("request_hash", "sha256:" + "0" * 64),
        ("operation", "delete_source"),
        ("source_id", "unsafe/source"),
        ("actor", "unsafe actor"),
        ("reason", "unsafe reason"),
        ("expected_generation", -1),
        ("expected_state_version", True),
        ("outcome", "pending"),
        ("resulting_state_version", None),
        ("http_status", 201),
        ("error_code", "UNEXPECTED_ERROR"),
        (
            "result",
            {
                "status": "published",
                "source_id": "safe-source",
                "generation": 1,
                "metadata_revision": "sha256:" + "0" * 64,
                "quality_level": "L0",
                "secret": "must-not-project",
            },
        ),
        ("recorded_at", datetime(2026, 8, 23)),
    ],
)
def test_source_mutation_decoder_rejects_unsafe_or_nonterminal_fields(
    field: str,
    value: object,
) -> None:
    row = _mutation_row()
    row[field] = value

    with pytest.raises(ValueError, match=r"source mutation|Mutation"):
        _decode_mutation(row)


def test_source_mutation_queries_project_only_bounded_receipt_fields() -> None:
    mutation_query_source = _MUTATION_PROJECTION + "".join(
        inspect.getsource(method)
        for method in (
            PostgresSourceStore.get_mutation,
            PostgresSourceStore.list_mutations,
        )
    )
    for forbidden in (
        "secret_nonce",
        "secret_ciphertext",
        "manifest",
        "password_env",
        "host_env",
        "port_env",
        "metadata_snapshots",
        "verified_query_contracts",
        "question",
        "sql",
    ):
        assert forbidden not in mutation_query_source
    assert "SELECT *" not in mutation_query_source.upper()
    for required in (
        "idempotency_key",
        "request_hash",
        "operation",
        "actor",
        "reason",
        "expected_generation",
        "expected_state_version",
        "resulting_generation",
        "resulting_state_version",
        "outcome",
        "result",
    ):
        assert required in _MUTATION_PROJECTION


def test_replica_observation_decoders_enforce_desired_and_report_shapes() -> None:
    row = _replica_observation_row()
    desired = _decode_desired_replica_state(row)
    observation = _decode_replica_observation(row)

    assert desired == (
        "safe-source",
        2,
        3,
        True,
        f"sha256:{'1' * 64}",
    )
    assert observation.replica_id == "safe-replica"
    assert observation.applied_generation == 2
    assert observation.applied_metadata_revision == f"sha256:{'1' * 64}"
    assert observation.source_health == "healthy"

    for invalid_desired in (
        {**row, "desired_metadata_revision": None},
        {
            **row,
            "desired_enabled": False,
            "desired_metadata_revision": f"sha256:{'1' * 64}",
        },
    ):
        with pytest.raises(ValueError, match="desired metadata"):
            _decode_desired_replica_state(invalid_desired)

    for field, value in (
        ("report_reason_code", "UNKNOWN"),
        ("applied_state_version", None),
        ("applied_enabled", False),
        ("source_health", "unknown"),
        ("reason_code", "UNKNOWN"),
        ("observed_at", datetime(2026, 8, 25)),
    ):
        invalid_observation = {**row, field: value}
        with pytest.raises(
            ValueError,
            match=r"replica|Replica|Stored source catalog",
        ):
            _decode_replica_observation(invalid_observation)


def test_replica_observation_query_is_one_bounded_secret_free_snapshot() -> None:
    projection_source = inspect.getsource(
        PostgresSourceStore.list_replica_observations
    )

    assert projection_source.count("await connection.execute(") == 1
    assert "clock_timestamp()" in projection_source
    assert "active_metadata_revisions" in projection_source
    assert (
        "ORDER BY replica_id COLLATE \"C\" ASC LIMIT %s"
        in projection_source.replace("\\\"", '"')
    )
    assert "SELECT *" not in projection_source.upper()
    for forbidden in (
        "secret_nonce",
        "secret_ciphertext",
        "manifest",
        "snapshot",
        "credential",
        "password_env",
    ):
        assert forbidden not in projection_source


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


def _mutation_row() -> dict[str, object]:
    return {
        "event_id": 1,
        "idempotency_key": "a8d4306f-3628-49bc-a09e-17cfe947fcb1",
        "request_hash": "hmac-sha256:" + "0" * 64,
        "operation": "publish_source",
        "source_id": "safe-source",
        "actor": "source-admin",
        "reason": "unit-test",
        "expected_generation": 0,
        "expected_state_version": 0,
        "outcome": "succeeded",
        "resulting_generation": 1,
        "resulting_state_version": 1,
        "http_status": 200,
        "error_code": None,
        "result": {
            "status": "published",
            "source_id": "safe-source",
            "generation": 1,
            "metadata_revision": "sha256:" + "0" * 64,
            "quality_level": "L0",
        },
        "recorded_at": datetime(2026, 8, 23, tzinfo=UTC),
    }


def _replica_observation_row() -> dict[str, object]:
    observed_at = datetime(2026, 8, 25, tzinfo=UTC)
    revision = f"sha256:{'1' * 64}"
    return {
        "source_id": "safe-source",
        "desired_generation": 2,
        "desired_state_version": 3,
        "desired_enabled": True,
        "desired_metadata_revision": revision,
        "replica_id": "safe-replica",
        "report_reason_code": None,
        "observed_at": observed_at,
        "fresh_until": observed_at + timedelta(seconds=15),
        "read_at": observed_at + timedelta(seconds=1),
        "applied_generation": 2,
        "applied_state_version": 3,
        "applied_enabled": True,
        "applied_metadata_revision": revision,
        "source_health": "healthy",
        "reason_code": None,
    }


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
