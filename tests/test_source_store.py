from __future__ import annotations

import asyncio
import inspect
import re
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import cast

import pytest
import yaml
from psycopg import AsyncConnection, Error
from psycopg.errors import RaiseException

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
    MutationIdempotencyConflictError,
    MutationReplay,
    MutationRequest,
    PostgresSourceStore,
    SourceGenerationConflictError,
    SourcePublishPinnedError,
    _decode_catalog,
    _decode_mutation,
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


def _source_fixture(source_id: str) -> tuple[ValidatedSourceManifest, PreparedMetadata]:
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
