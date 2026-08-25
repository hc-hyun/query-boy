from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any, get_type_hints

import pytest
import yaml

from query_man.errors import (
    MetadataRevisionMismatchError,
    QueryInvalidError,
    QueryRejectedError,
    SourceControlUnavailableError,
    SourceNotFoundError,
    SourceValidationError,
)
from query_man.errors import (
    MutationIdempotencyConflictError as MutationIdempotencyConflictAppError,
)
from query_man.metadata import MetadataService
from query_man.models import (
    CatalogSnapshot,
    PreparedMetadata,
    ResourceObservation,
    SourceProfile,
)
from query_man.operations import operations
from query_man.query import QueryService
from query_man.reader_policy import ReaderSessionPolicyError
from query_man.registry import (
    RegistryConfigurationError,
    SourceRegistry,
    load_budget_profiles,
    validate_source_manifest,
)
from query_man.secrets import EncryptedSecret, SourceSecretCipher
from query_man.source_admin import (
    CONTROL_SEQUENCE_MAX,
    REPLICA_HEARTBEAT_INTERVAL_MAX_MS,
    REPLICA_HEARTBEAT_INTERVAL_MIN_MS,
    ControlReplicaObservationWriter,
    GatewayUsageDelta,
    GatewayUsageWriter,
    MutationContext,
    PublishVerifiedQueryInput,
    ReplicaObservationWriter,
    ReplicaSourceObservation,
    ResourceObservationSample,
    ResourceObservationWriter,
    SourceAdminService,
    SourceReloader,
    VerifiedExpectedInput,
)
from query_man.source_store import (
    MutationIdempotencyConflictError,
    MutationPage,
    MutationReceipt,
    MutationReplay,
    MutationRequest,
    ReplicaObservationPage,
    ReplicaObservationRecord,
    SourceCatalogConnection,
    SourceCatalogPage,
    SourceCatalogRecord,
    SourceGenerationConflictError,
    SourceGenerationPage,
    SourcePublishPinnedError,
    StoredSource,
    StoredSourceNotFoundError,
    _ReplicaSourceObservationWrite,
)
from query_man.sql_validation import ValidatedSql
from query_man.verified import ExpectedResult, VerifiedQuery, create_result_hash
from tests.helpers import ROOT_DIRECTORY, minimal_development_snapshot


def test_verified_publish_input_is_frozen_control_contract() -> None:
    expected = VerifiedExpectedInput(
        columns=("status",),
        row_count=1,
        result_hash=f"sha256:{'1' * 64}",
    )
    query = PublishVerifiedQueryInput(
        query_id="third-source-status",
        source_id="third-source",
        question="상태를 보여줘",
        sql="SELECT status FROM ai.issue_overview",
        metadata_revision=f"sha256:{'2' * 64}",
        relations=("ai.issue_overview",),
        expected=expected,
    )

    assert CONTROL_SEQUENCE_MAX == 9_223_372_036_854_775_807
    assert tuple(field.name for field in fields(VerifiedExpectedInput)) == (
        "columns",
        "row_count",
        "result_hash",
    )
    assert tuple(field.name for field in fields(PublishVerifiedQueryInput)) == (
        "query_id",
        "source_id",
        "question",
        "sql",
        "metadata_revision",
        "relations",
        "expected",
    )
    assert get_type_hints(VerifiedExpectedInput) == {
        "columns": tuple[str, ...],
        "row_count": int,
        "result_hash": str,
    }
    assert get_type_hints(PublishVerifiedQueryInput) == {
        "query_id": str,
        "source_id": str,
        "question": str,
        "sql": str,
        "metadata_revision": str,
        "relations": tuple[str, ...],
        "expected": VerifiedExpectedInput,
    }
    for target, field_name, value in (
        (query, "query_id", "changed"),
        (expected, "row_count", 2),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(target, field_name, value)


def test_replica_observation_input_is_frozen_control_contract() -> None:
    observation = ReplicaSourceObservation(
        source_id="third-source",
        applied_generation=2,
        applied_state_version=3,
        applied_enabled=True,
        applied_metadata_revision=f"sha256:{'2' * 64}",
        source_health="healthy",
        reason_code=None,
    )

    assert REPLICA_HEARTBEAT_INTERVAL_MIN_MS == 5_000
    assert REPLICA_HEARTBEAT_INTERVAL_MAX_MS == 300_000
    assert tuple(field.name for field in fields(ReplicaSourceObservation)) == (
        "source_id",
        "applied_generation",
        "applied_state_version",
        "applied_enabled",
        "applied_metadata_revision",
        "source_health",
        "reason_code",
    )
    with pytest.raises(FrozenInstanceError):
        observation.source_health = "stale"  # type: ignore[misc]


def test_resource_and_gateway_write_inputs_are_frozen_control_contracts() -> None:
    sample = ResourceObservationSample(
        metric="representative_records",
        value=10,
        unit="rows",
        method="postgres_catalog_estimate",
        definition_revision=f"sha256:{'1' * 64}",
    )
    delta = GatewayUsageDelta(
        source_id="third-source",
        budget_profile="interactive",
        metadata_revision=f"sha256:{'2' * 64}",
        definition_revision=f"sha256:{'3' * 64}",
        bucket_start=datetime(2026, 8, 25, 1, tzinfo=UTC),
        query_count=1,
        success_count=1,
        rejected_count=0,
        timeout_count=0,
        overloaded_count=0,
        cancelled_count=0,
        failed_count=0,
        queue_ms_sum=1,
        elapsed_ms_sum=2,
        returned_rows_sum=3,
        result_bytes_sum=4,
        truncated_count=0,
    )

    assert tuple(field.name for field in fields(ResourceObservationSample)) == (
        "metric",
        "value",
        "unit",
        "method",
        "definition_revision",
    )
    assert tuple(field.name for field in fields(GatewayUsageDelta)) == (
        "source_id",
        "budget_profile",
        "metadata_revision",
        "definition_revision",
        "bucket_start",
        "query_count",
        "success_count",
        "rejected_count",
        "timeout_count",
        "overloaded_count",
        "cancelled_count",
        "failed_count",
        "queue_ms_sum",
        "elapsed_ms_sum",
        "returned_rows_sum",
        "result_bytes_sum",
        "truncated_count",
    )
    assert get_type_hints(
        ResourceObservationWriter.report_resource_observations
    ) == {
        "source_id": str,
        "metadata_revision": str,
        "samples": tuple[ResourceObservationSample, ...],
        "return": type(None),
    }
    assert get_type_hints(GatewayUsageWriter.report_gateway_usage) == {
        "replica_id": str,
        "incarnation": int,
        "sequence": int,
        "deltas": tuple[GatewayUsageDelta, ...],
        "return": type(None),
    }
    with pytest.raises(FrozenInstanceError):
        sample.value = 11  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        delta.query_count = 2  # type: ignore[misc]


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
        self.mutations: dict[str, MutationReceipt] = {}
        self.replica_page: ReplicaObservationPage | None = None
        self.replica_registrations: list[tuple[str, int]] = []
        self.replica_reports: list[
            tuple[
                str,
                int,
                str | None,
                tuple[_ReplicaSourceObservationWrite, ...],
            ]
        ] = []
        self.replica_queries: list[tuple[str, str | None, int]] = []

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

    async def register_replica(
        self,
        replica_id: str,
        heartbeat_interval_ms: int,
    ) -> int:
        self.replica_registrations.append((replica_id, heartbeat_interval_ms))
        return len(self.replica_registrations)

    async def report_replica(
        self,
        replica_id: str,
        incarnation: int,
        *,
        reason_code: str | None,
        sources: tuple[_ReplicaSourceObservationWrite, ...],
    ) -> None:
        self.replica_reports.append(
            (replica_id, incarnation, reason_code, sources)
        )

    async def list_replica_observations(
        self,
        source_id: str,
        *,
        after_replica_id: str | None = None,
        limit: int = 50,
    ) -> ReplicaObservationPage | None:
        self.replica_queries.append((source_id, after_replica_id, limit))
        return self.replica_page

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

    async def get_mutation(self, idempotency_key: str) -> MutationReceipt | None:
        return self.mutations.get(idempotency_key)

    async def list_mutations(
        self,
        source_id: str,
        *,
        before_event_id: int | None = None,
        limit: int = 50,
    ) -> MutationPage | None:
        receipts = sorted(
            (
                receipt
                for receipt in self.mutations.values()
                if receipt.source_id == source_id
                and (before_event_id is None or receipt.event_id < before_event_id)
            ),
            key=lambda receipt: receipt.event_id,
            reverse=True,
        )
        if source_id not in self.active and not receipts:
            return None
        page = tuple(receipts[:limit])
        return MutationPage(
            page,
            page[-1].event_id if len(receipts) > limit else None,
        )

    async def record_mutation_rejection(
        self,
        mutation: MutationRequest,
        *,
        http_status: int,
        error_code: str,
    ) -> MutationReceipt:
        existing = self._existing_mutation(mutation)
        if existing is not None:
            return existing
        receipt = self._receipt(
            mutation,
            outcome="rejected",
            resulting_generation=None,
            resulting_state_version=None,
            http_status=http_status,
            error_code=error_code,
            result={},
        )
        self.mutations[mutation.idempotency_key] = receipt
        return receipt

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
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> StoredSource:
        if mutation is not None:
            existing = self._existing_mutation(mutation)
            if existing is not None:
                raise MutationReplay(existing)
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
        if mutation is not None and mutation_result is not None:
            self.mutations[mutation.idempotency_key] = self._receipt(
                mutation,
                outcome="succeeded",
                resulting_generation=generation,
                resulting_state_version=record.state_version,
                http_status=200,
                error_code=None,
                result=mutation_result,
            )
        return record

    async def deactivate(
        self,
        source_id: str,
        expected_generation: int,
        *,
        expected_state_version: int,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> int:
        if mutation is not None:
            existing = self._existing_mutation(mutation)
            if existing is not None:
                raise MutationReplay(existing)
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
        if mutation is not None and mutation_result is not None:
            self.mutations[mutation.idempotency_key] = self._receipt(
                mutation,
                outcome="succeeded",
                resulting_generation=current.generation,
                resulting_state_version=state_version,
                http_status=200,
                error_code=None,
                result=mutation_result,
            )
        return state_version

    async def rollback(
        self,
        source_id: str,
        generation: int,
        expected_generation: int,
        *,
        expected_state_version: int,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> StoredSource:
        if mutation is not None:
            existing = self._existing_mutation(mutation)
            if existing is not None:
                raise MutationReplay(existing)
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
        if mutation is not None and mutation_result is not None:
            self.mutations[mutation.idempotency_key] = self._receipt(
                mutation,
                outcome="succeeded",
                resulting_generation=record.generation,
                resulting_state_version=record.state_version,
                http_status=200,
                error_code=None,
                result=mutation_result,
            )
        return record

    def _existing_mutation(self, mutation: MutationRequest) -> MutationReceipt | None:
        existing = self.mutations.get(mutation.idempotency_key)
        if existing is None:
            return None
        if (
            existing.request_hash != mutation.request_hash
            or existing.operation != mutation.operation
            or existing.source_id != mutation.source_id
            or existing.actor != mutation.actor
            or existing.reason != mutation.reason
            or existing.expected_generation != mutation.expected_generation
            or existing.expected_state_version != mutation.expected_state_version
        ):
            raise MutationIdempotencyConflictError
        return existing

    def _receipt(
        self,
        mutation: MutationRequest,
        *,
        outcome: str,
        resulting_generation: int | None,
        resulting_state_version: int | None,
        http_status: int,
        error_code: str | None,
        result: dict[str, object],
    ) -> MutationReceipt:
        return MutationReceipt(
            event_id=len(self.mutations) + 1,
            idempotency_key=mutation.idempotency_key,
            request_hash=mutation.request_hash,
            operation=mutation.operation,
            source_id=mutation.source_id,
            actor=mutation.actor,
            reason=mutation.reason,
            expected_generation=mutation.expected_generation,
            expected_state_version=mutation.expected_state_version,
            outcome=outcome,
            resulting_generation=resulting_generation,
            resulting_state_version=resulting_state_version,
            http_status=http_status,
            error_code=error_code,
            result=result,
            recorded_at=datetime(2026, 8, 23, tzinfo=UTC)
            + timedelta(seconds=len(self.mutations) + 1),
        )

    async def close(self) -> None:
        pass

    async def publish_verified_query(
        self,
        query: VerifiedQuery,
        *,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> None:
        if mutation is not None:
            existing = self._existing_mutation(mutation)
            if existing is not None:
                raise MutationReplay(existing)
        self.verified.append(query)
        if mutation is not None and mutation_result is not None:
            current = self.active[query.source_id]
            self.mutations[mutation.idempotency_key] = self._receipt(
                mutation,
                outcome="succeeded",
                resulting_generation=current.generation,
                resulting_state_version=current.state_version,
                http_status=200,
                error_code=None,
                result=mutation_result,
            )

    async def resume_metadata_publish(
        self,
        source_id: str,
        expected_metadata_revision: str,
        *,
        mutation: MutationRequest,
        mutation_result: dict[str, object],
    ) -> None:
        existing = self._existing_mutation(mutation)
        if existing is not None:
            raise MutationReplay(existing)
        current = self.active[source_id]
        if (
            self.metadata.active.get(source_id) != expected_metadata_revision
            or source_id not in self.metadata.pinned
        ):
            raise SourceGenerationConflictError
        self.metadata.pinned.remove(source_id)
        self.mutations[mutation.idempotency_key] = self._receipt(
            mutation,
            outcome="succeeded",
            resulting_generation=current.generation,
            resulting_state_version=current.state_version,
            http_status=200,
            error_code=None,
            result=mutation_result,
        )

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
    def __init__(
        self,
        snapshot: CatalogSnapshot | None = None,
        *,
        observed_sources: list[str] | None = None,
        observation_error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot or minimal_development_snapshot()
        self.observed_sources = observed_sources
        self.observation_error = observation_error

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot

    async def observe_resources(self, source: SourceProfile) -> ResourceObservation:
        if self.observation_error is not None:
            raise self.observation_error
        if self.observed_sources is not None:
            self.observed_sources.append(source.source_id)
        return ResourceObservation(
            representative_records=10,
            table_bytes=20,
            index_bytes=5,
            total_storage_bytes=25,
        )

    async def invalidate(self, _source_id: str) -> None:
        pass

    async def close(self) -> None:
        pass


class SwitchingCatalogFactory:
    def __init__(self) -> None:
        self.snapshot = minimal_development_snapshot()
        self.calls = 0
        self.observed_sources: list[str] = []
        self.observation_error: Exception | None = None

    def __call__(self) -> StaticCatalog:
        self.calls += 1
        return StaticCatalog(
            self.snapshot,
            observed_sources=self.observed_sources,
            observation_error=self.observation_error,
        )


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


def _replica_record(
    replica_id: str,
    observed_at: datetime,
    fresh_until: datetime,
    read_at: datetime,
    *,
    report_reason_code: str | None = None,
    applied_generation: int | None = None,
    applied_state_version: int | None = None,
    applied_enabled: bool | None = None,
    applied_metadata_revision: str | None = None,
    source_health: str | None = None,
    reason_code: str | None = None,
) -> ReplicaObservationRecord:
    return ReplicaObservationRecord(
        replica_id=replica_id,
        observed_at=observed_at,
        fresh_until=fresh_until,
        read_at=read_at,
        report_reason_code=report_reason_code,
        applied_generation=applied_generation,
        applied_state_version=applied_state_version,
        applied_enabled=applied_enabled,
        applied_metadata_revision=applied_metadata_revision,
        source_health=source_health,
        reason_code=reason_code,
    )


def _mutation_context(
    key_suffix: int,
    *,
    actor: str = "operator-a",
    reason: str = "change/CTRL-05",
    expected_generation: int = 0,
    expected_state_version: int = 0,
    expected_metadata_revision: str | None = None,
) -> MutationContext:
    return MutationContext(
        idempotency_key=f"00000000-0000-4000-8000-{key_suffix:012d}",
        actor=actor,
        reason=reason,
        expected_generation=expected_generation,
        expected_state_version=expected_state_version,
        expected_metadata_revision=expected_metadata_revision,
    )


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
    incomplete = replace(incomplete, relations=incomplete.relations[:1])
    catalog_factory.snapshot = incomplete

    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", _manifest(), "bad-update-secret")

    assert store.active["third-source"] == before
    assert registry.get("third-source").connection.password == "first-secret"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_staging_validates_configured_resource_targets() -> None:
    catalog_factory = SwitchingCatalogFactory()
    admin, _registry, store, _invalidator, _cipher, _reloader = _services(
        catalog_factory
    )

    await admin.publish("third-source", _manifest(), "first-secret")

    assert catalog_factory.observed_sources == ["third-source"]
    before = store.active["third-source"]
    catalog_factory.observation_error = RuntimeError(
        "configured resource target is unavailable"
    )
    with pytest.raises(SourceValidationError):
        await admin.publish("third-source", _manifest(), "bad-update-secret")

    assert store.active["third-source"] == before
    assert catalog_factory.observed_sources == ["third-source"]


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
        replica_before = operations.replica_runtime_snapshot()
        assert replica_before.sources[0].applied_metadata_revision is not None
        assert replica_before.sources[0].source_health == "healthy"

        incomplete = minimal_development_snapshot()
        incomplete = replace(incomplete, relations=incomplete.relations[:1])
        catalog_factory.snapshot = incomplete
        with pytest.raises(SourceValidationError):
            await admin.publish("third-source", _manifest(), "bad-update-secret")

        assert operations.snapshot()["sources"] == {"third-source": "healthy"}
        assert operations.public_status() == "ready"
        assert operations.replica_runtime_snapshot() == replica_before
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_dynamic_apply_and_deactivate_reconcile_source_inventory() -> None:
    operations.reset()
    try:
        admin, _registry, _store, _invalidator, _cipher, _reloader = _services()

        await admin.publish("third-source", _manifest(), "first-secret")
        assert operations.snapshot()["sources"] == {"third-source": "healthy"}
        enabled = operations.replica_runtime_snapshot().sources[0]
        assert (
            enabled.applied_generation,
            enabled.applied_state_version,
            enabled.applied_enabled,
            enabled.source_health,
        ) == (1, 1, True, "healthy")
        assert enabled.applied_metadata_revision is not None

        await admin.deactivate("third-source")
        operations.set_source_health("third-source", "unavailable")
        assert operations.snapshot()["sources"] == {}
        assert operations.public_status() == "unavailable"
        disabled = operations.replica_runtime_snapshot().sources[0]
        assert (
            disabled.applied_generation,
            disabled.applied_state_version,
            disabled.applied_enabled,
            disabled.applied_metadata_revision,
            disabled.source_health,
            disabled.reason_code,
        ) == (1, 2, False, None, None, None)

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
        replica = operations.replica_runtime_snapshot()
        assert replica.reason_code == "CONTROL_SCAN_FAILED"
        assert replica.sources == ()
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
        failed_replica = operations.replica_runtime_snapshot()
        assert failed_replica.reason_code == "CONTROL_SCAN_FAILED"
        assert len(failed_replica.sources) == 1

        monkeypatch.setattr(store, "list_active", successful_scan)
        await reloader.sync()
        assert operations.snapshot()["components"] == {"source_reload": "healthy"}
        assert operations.public_status() == "ready"
        assert operations.replica_runtime_snapshot().reason_code is None
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
        replica = operations.replica_runtime_snapshot().sources[0]
        assert replica.reason_code == "RUNTIME_VALIDATION_REJECTED"
        applied_metadata_revision = replica.applied_metadata_revision

        store.active["third-source"] = current
        await reloader.sync()
        assert operations.snapshot()["components"] == {"source_reload": "healthy"}
        assert operations.public_status() == "ready"
        converged = operations.replica_runtime_snapshot().sources[0]
        assert converged.reason_code is None
        assert converged.applied_metadata_revision == applied_metadata_revision
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_reload_apply_and_metadata_probe_failures_have_bounded_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations.reset()
    try:
        admin, registry, store, invalidator, _cipher, reloader = _services()
        await admin.publish("third-source", _manifest(), "first-secret")
        current = store.active["third-source"]

        async def fail_invalidation(_source_id: str) -> None:
            raise RuntimeError("pool invalidation failed")

        monkeypatch.setattr(invalidator, "invalidate", fail_invalidation)
        store.active["third-source"] = replace(
            current,
            state_version=current.state_version + 1,
        )
        await reloader.sync()
        failed_apply = operations.replica_runtime_snapshot().sources[0]
        assert failed_apply.reason_code == "RUNTIME_APPLY_FAILED"
        assert failed_apply.applied_state_version == current.state_version

        monkeypatch.setattr(invalidator, "invalidate", RecordingInvalidator().invalidate)

        async def fail_probe(_source_id: str) -> PreparedMetadata:
            raise RuntimeError("metadata probe failed")

        monkeypatch.setattr(reloader._metadata, "get_published", fail_probe)
        await reloader.sync()
        failed_probe = operations.replica_runtime_snapshot().sources[0]
        assert failed_probe.applied_state_version == current.state_version + 1
        assert failed_probe.applied_metadata_revision is None
        assert failed_probe.source_health == "unavailable"
        assert failed_probe.reason_code == "METADATA_PROBE_FAILED"
        assert registry.get("third-source") is not None
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
async def test_replica_writer_maps_only_public_sanitized_observations() -> None:
    _admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    writer: ReplicaObservationWriter = ControlReplicaObservationWriter(store)
    observation = ReplicaSourceObservation(
        source_id="third-source",
        applied_generation=2,
        applied_state_version=3,
        applied_enabled=True,
        applied_metadata_revision=f"sha256:{'2' * 64}",
        source_health="healthy",
        reason_code=None,
    )

    incarnation = await writer.register_replica("runtime-a", 5_000)
    await writer.report_replica(
        "runtime-a",
        incarnation,
        reason_code=None,
        sources=(observation,),
    )

    assert incarnation == 1
    assert store.replica_registrations == [("runtime-a", 5_000)]
    assert store.replica_reports == [
        (
            "runtime-a",
            1,
            None,
            (
                _ReplicaSourceObservationWrite(
                    source_id="third-source",
                    applied_generation=2,
                    applied_state_version=3,
                    applied_enabled=True,
                    applied_metadata_revision=f"sha256:{'2' * 64}",
                    source_health="healthy",
                    reason_code=None,
                ),
            ),
        )
    ]
    assert "secret" not in repr(store.replica_reports).lower()
    assert "manifest" not in repr(store.replica_reports).lower()


@pytest.mark.asyncio
async def test_replica_admin_projection_has_ordered_drift_and_db_clock_freshness() -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    observed_at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    fresh_until = observed_at + timedelta(seconds=15)
    desired_revision = f"sha256:{'3' * 64}"
    exact_applied = {
        "applied_generation": 2,
        "applied_state_version": 3,
        "applied_enabled": True,
        "applied_metadata_revision": desired_revision,
    }
    store.replica_page = ReplicaObservationPage(
        source_id="third-source",
        desired_generation=2,
        desired_state_version=3,
        desired_enabled=True,
        desired_metadata_revision=desired_revision,
        items=(
            _replica_record(
                "runtime-available",
                observed_at,
                fresh_until,
                fresh_until,
                source_health="healthy",
                **exact_applied,
            ),
            _replica_record(
                "runtime-stale",
                observed_at,
                fresh_until,
                fresh_until + timedelta(milliseconds=1, microseconds=1),
                report_reason_code="CONTROL_SCAN_FAILED",
                applied_generation=1,
                applied_state_version=1,
                applied_enabled=False,
                applied_metadata_revision=None,
                source_health="stale",
            ),
            _replica_record(
                "runtime-scan-failed",
                observed_at,
                fresh_until,
                fresh_until,
                report_reason_code="CONTROL_SCAN_FAILED",
                source_health="healthy",
                **exact_applied,
            ),
            _replica_record(
                "runtime-apply-failed",
                observed_at,
                fresh_until,
                fresh_until,
                reason_code="RUNTIME_APPLY_FAILED",
                source_health="unavailable",
                **exact_applied,
            ),
            _replica_record(
                "runtime-pending",
                observed_at,
                fresh_until,
                fresh_until,
                source_health="initializing",
            ),
        ),
        next_after_replica_id="runtime-pending",
    )

    response = await admin.source_replicas(
        "third-source",
        limit=5,
        after_replica_id="previous-runtime",
    )

    assert store.replica_queries == [("third-source", "previous-runtime", 5)]
    assert response["desired"] == {
        "enabled": True,
        "generation": 2,
        "state_version": 3,
        "metadata_revision": desired_revision,
    }
    replicas = response["replicas"]
    assert isinstance(replicas, list)
    assert [replica["status"] for replica in replicas] == [
        "available",
        "stale",
        "unavailable",
        "unavailable",
        "pending",
    ]
    assert [replica["reason_code"] for replica in replicas] == [
        None,
        "HEARTBEAT_EXPIRED",
        "CONTROL_SCAN_FAILED",
        "RUNTIME_APPLY_FAILED",
        "NOT_OBSERVED",
    ]
    assert replicas[0]["drift"] == []
    assert replicas[0]["applied"] == {
        "enabled": True,
        "generation": 2,
        "state_version": 3,
        "metadata_revision": desired_revision,
    }
    assert replicas[1]["drift"] == [
        "enabled",
        "generation",
        "state_version",
        "metadata_revision",
    ]
    assert replicas[1]["stale_age_ms"] == 2
    assert replicas[4]["drift"] == ["not_applied"]
    assert replicas[4]["applied"] is None
    assert replicas[4]["source_health"] == "initializing"
    assert replicas[0]["observed_at"] == observed_at.isoformat()
    assert replicas[0]["fresh_until"] == fresh_until.isoformat()
    assert response["next_after_replica_id"] == "runtime-pending"
    rendered = repr(response).lower()
    assert "secret" not in rendered
    assert "manifest" not in rendered
    assert "credential" not in rendered


@pytest.mark.asyncio
async def test_disabled_replica_desired_state_has_no_metadata_drift() -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    observed_at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    store.replica_page = ReplicaObservationPage(
        source_id="third-source",
        desired_generation=2,
        desired_state_version=4,
        desired_enabled=False,
        desired_metadata_revision=None,
        items=(
            _replica_record(
                "runtime-disabled",
                observed_at,
                observed_at + timedelta(seconds=15),
                observed_at + timedelta(seconds=1),
                applied_generation=2,
                applied_state_version=4,
                applied_enabled=False,
                applied_metadata_revision=None,
                source_health=None,
            ),
        ),
        next_after_replica_id=None,
    )

    response = await admin.source_replicas("third-source")
    replicas = response["replicas"]
    assert isinstance(replicas, list)
    assert response["desired"] == {
        "enabled": False,
        "generation": 2,
        "state_version": 4,
        "metadata_revision": None,
    }
    assert replicas[0]["status"] == "available"
    assert replicas[0]["drift"] == []
    assert replicas[0]["reason_code"] is None


@pytest.mark.asyncio
async def test_replica_admin_maps_malformed_projection_to_safe_unavailable() -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    observed_at = datetime(2026, 8, 25, 12, tzinfo=UTC)
    store.replica_page = ReplicaObservationPage(
        source_id="third-source",
        desired_generation=2,
        desired_state_version=3,
        desired_enabled=True,
        desired_metadata_revision=f"sha256:{'3' * 64}",
        items=(
            _replica_record(
                "runtime-malformed",
                observed_at,
                datetime(2026, 8, 25, 12, 0, 15),
                observed_at + timedelta(seconds=1),
                source_health="healthy",
            ),
        ),
        next_after_replica_id=None,
    )

    with pytest.raises(SourceControlUnavailableError) as failure:
        await admin.source_replicas("third-source")

    assert failure.value.code == "SOURCE_CONTROL_UNAVAILABLE"
    assert failure.value.message == "Source administration is unavailable."
    assert failure.value.details is None
    assert isinstance(failure.value.__cause__, TypeError)


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
    with pytest.raises(SourceNotFoundError):
        await admin.source_replicas("unknown-source")

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

    async def fail_replicas(
        _source_id: str,
        **_page: object,
    ) -> ReplicaObservationPage | None:
        raise RuntimeError("private control database detail")

    monkeypatch.setattr(store, "list_replica_observations", fail_replicas)
    with pytest.raises(SourceControlUnavailableError):
        await admin.source_replicas("unknown-source")

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
    query = PublishVerifiedQueryInput(
        query_id="third-source-open-status",
        source_id="third-source",
        question="상태 예시를 보여줘",
        sql="SELECT status FROM ai.issue_overview ORDER BY status LIMIT 1",
        metadata_revision=current.metadata_revision,
        relations=("ai.issue_overview",),
        expected=VerifiedExpectedInput(
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
    query = PublishVerifiedQueryInput(
        query_id="third-source-open-status",
        source_id="third-source",
        question="상태 예시를 보여줘",
        sql="SELECT status FROM ai.issue_overview ORDER BY status LIMIT 1",
        metadata_revision=current.metadata_revision,
        relations=("ai.issue_overview",),
        expected=VerifiedExpectedInput(
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
    assert store.verified == [
        VerifiedQuery(
            query_id=query.query_id,
            source_id=query.source_id,
            question=query.question,
            sql=query.sql,
            metadata_revision=query.metadata_revision,
            relations=query.relations,
            expected=ExpectedResult(
                columns=query.expected.columns,
                row_count=query.expected.row_count,
                result_hash=query.expected.result_hash,
            ),
        )
    ]


@pytest.mark.asyncio
async def test_mutation_replay_returns_one_terminal_receipt_without_repeating_work() -> None:
    catalog_factory = SwitchingCatalogFactory()
    admin, _registry, store, _invalidator, _cipher, _reloader = _services(
        catalog_factory
    )
    context = _mutation_context(1)
    manifest = _manifest()

    first = await admin.publish(
        "third-source",
        manifest,
        "first-secret",
        context,
    )
    reordered_manifest = dict(reversed(list(manifest.items())))
    replay = await admin.publish(
        "third-source",
        reordered_manifest,
        "first-secret",
        context,
    )
    fetched = await admin.get_mutation(context.idempotency_key)

    assert first == replay == fetched
    assert first["outcome"] == "succeeded"
    assert first["result"] == {
        "status": "published",
        "source_id": "third-source",
        "generation": 1,
        "metadata_revision": store.active["third-source"].metadata_revision,
        "quality_level": "L1",
    }
    assert first["resulting_state"] == {"generation": 1, "state_version": 1}
    assert str(first["request_hash"]).startswith("hmac-sha256:")
    assert "first-secret" not in repr(first)
    assert len(store.history) == 1
    assert len(store.mutations) == 1
    assert catalog_factory.calls == 1


@pytest.mark.asyncio
async def test_same_mutation_key_rejects_any_changed_request_envelope() -> None:
    catalog_factory = SwitchingCatalogFactory()
    admin, _registry, store, _invalidator, _cipher, _reloader = _services(
        catalog_factory
    )
    context = _mutation_context(2)
    manifest = _manifest()
    await admin.publish("third-source", manifest, "first-secret", context)

    changed_schemas = _manifest()
    allowed_schemas = changed_schemas["allowed_schemas"]
    assert isinstance(allowed_schemas, list)
    allowed_schemas.append("reporting")
    attempts = (
        (manifest, "changed-secret", context),
        (manifest, "first-secret", _mutation_context(2, actor="operator-b")),
        (manifest, "first-secret", _mutation_context(2, reason="change/CTRL-99")),
        (
            manifest,
            "first-secret",
            _mutation_context(2, expected_generation=1, expected_state_version=1),
        ),
        (changed_schemas, "first-secret", context),
    )

    for attempted_manifest, credential, attempted_context in attempts:
        with pytest.raises(MutationIdempotencyConflictAppError):
            await admin.publish(
                "third-source",
                attempted_manifest,
                credential,
                attempted_context,
            )

    assert len(store.history) == 1
    assert len(store.mutations) == 1
    assert catalog_factory.calls == 1


@pytest.mark.asyncio
async def test_deterministic_rejection_is_receipted_and_replayed_without_payload() -> None:
    catalog_factory = SwitchingCatalogFactory()
    admin, _registry, store, _invalidator, _cipher, _reloader = _services(
        catalog_factory
    )
    context = _mutation_context(3)
    mismatched_manifest = _manifest()
    mismatched_manifest["source_id"] = "different-source"

    with pytest.raises(SourceValidationError):
        await admin.publish(
            "third-source",
            mismatched_manifest,
            "rejected-secret",
            context,
        )
    with pytest.raises(SourceValidationError):
        await admin.publish(
            "third-source",
            dict(reversed(list(mismatched_manifest.items()))),
            "rejected-secret",
            context,
        )

    receipt = store.mutations[context.idempotency_key]
    second_context = _mutation_context(6)
    with pytest.raises(SourceValidationError):
        await admin.publish(
            "third-source",
            mismatched_manifest,
            "rejected-secret",
            second_context,
        )
    second_receipt = store.mutations[second_context.idempotency_key]
    assert receipt.outcome == "rejected"
    assert receipt.http_status == 400
    assert receipt.error_code == "SOURCE_VALIDATION_FAILED"
    assert receipt.result == {}
    assert receipt.resulting_generation is None
    assert receipt.resulting_state_version is None
    assert "rejected-secret" not in repr(receipt)
    assert second_receipt.request_hash != receipt.request_hash
    assert store.active == {}
    assert catalog_factory.calls == 0


@pytest.mark.asyncio
async def test_transient_staging_failure_is_not_receipted_and_same_key_can_retry() -> None:
    unavailable = True

    class RecoveringCatalog(StaticCatalog):
        async def load(self, source: SourceProfile) -> CatalogSnapshot:
            if unavailable:
                raise RuntimeError("catalog temporarily unavailable")
            return await super().load(source)

    admin, _registry, store, _invalidator, _cipher, _reloader = _services(
        RecoveringCatalog
    )
    context = _mutation_context(7)

    with pytest.raises(SourceControlUnavailableError):
        await admin.publish(
            "third-source",
            _manifest(),
            "retryable-secret",
            context,
        )

    assert store.mutations == {}
    unavailable = False
    response = await admin.publish(
        "third-source",
        _manifest(),
        "retryable-secret",
        context,
    )

    assert response["outcome"] == "succeeded"
    assert store.mutations[context.idempotency_key].outcome == "succeeded"


@pytest.mark.asyncio
async def test_deterministic_staging_rejection_is_receipted_without_restaging() -> None:
    catalog_factory = SwitchingCatalogFactory()
    admin, _registry, store, _invalidator, _cipher, _reloader = _services(
        catalog_factory
    )
    await admin.publish("third-source", _manifest(), "first-secret")
    current = store.active["third-source"]
    incomplete = minimal_development_snapshot()
    incomplete = replace(incomplete, relations=incomplete.relations[:1])
    catalog_factory.snapshot = incomplete
    context = _mutation_context(
        8,
        expected_generation=current.generation,
        expected_state_version=current.state_version,
    )

    for _attempt in range(2):
        with pytest.raises(SourceValidationError):
            await admin.publish(
                "third-source",
                _manifest(),
                "invalid-staged-secret",
                context,
            )

    receipt = store.mutations[context.idempotency_key]
    assert receipt.outcome == "rejected"
    assert receipt.error_code == "SOURCE_VALIDATION_FAILED"
    assert catalog_factory.calls == 2
    assert store.active["third-source"] == current
    assert "invalid-staged-secret" not in repr(receipt)


@pytest.mark.asyncio
async def test_reader_policy_staging_rejection_is_receipted() -> None:
    class PolicyMismatchCatalog(StaticCatalog):
        async def load(self, _source: SourceProfile) -> CatalogSnapshot:
            raise ReaderSessionPolicyError("reader policy mismatch")

    admin, _registry, store, _invalidator, _cipher, _reloader = _services(
        PolicyMismatchCatalog
    )
    context = _mutation_context(9)

    for _attempt in range(2):
        with pytest.raises(SourceValidationError):
            await admin.publish(
                "third-source",
                _manifest(),
                "policy-mismatch-secret",
                context,
            )

    receipt = store.mutations[context.idempotency_key]
    assert receipt.outcome == "rejected"
    assert receipt.error_code == "SOURCE_VALIDATION_FAILED"
    assert "policy-mismatch-secret" not in repr(receipt)


@pytest.mark.asyncio
async def test_post_commit_reload_failure_preserves_success_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations.reset()
    try:
        admin, _registry, store, _invalidator, _cipher, reloader = _services()
        context = _mutation_context(4)

        async def fail_reload(_record: StoredSource) -> None:
            raise RuntimeError("local reload failed")

        monkeypatch.setattr(reloader, "apply", fail_reload)
        response = await admin.publish(
            "third-source",
            _manifest(),
            "committed-secret",
            context,
        )

        assert response["outcome"] == "succeeded"
        assert response["resulting_state"] == {"generation": 1, "state_version": 1}
        assert store.active["third-source"].generation == 1
        assert store.mutations[context.idempotency_key].outcome == "succeeded"
        assert operations.snapshot()["components"] == {
            "source_reload": "unavailable"
        }
    finally:
        operations.reset()


@pytest.mark.parametrize(
    "query_error",
    [
        MetadataRevisionMismatchError(),
        QueryInvalidError("QUERY_INVALID_CAST"),
        QueryRejectedError("QUERY_PLAN_COST_EXCEEDED"),
    ],
)
@pytest.mark.asyncio
async def test_deterministic_verified_query_failure_is_receipted_and_replayed(
    query_error: Exception,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    current = store.active["third-source"]
    rows = [{"status": "OPEN"}]
    query = PublishVerifiedQueryInput(
        query_id="third-source-open-status",
        source_id="third-source",
        question="receipt에 저장하면 안 되는 질문",
        sql="SELECT status FROM ai.issue_overview ORDER BY status LIMIT 1",
        metadata_revision=current.metadata_revision,
        relations=("ai.issue_overview",),
        expected=VerifiedExpectedInput(
            columns=("status",),
            row_count=1,
            result_hash=create_result_hash(("status",), rows),
        ),
    )
    query_calls = 0

    async def fail_query(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal query_calls
        query_calls += 1
        raise query_error

    monkeypatch.setattr(admin._queries, "query", fail_query)
    mutation = _mutation_context(
        5,
        expected_generation=current.generation,
        expected_state_version=current.state_version,
    )

    for _attempt in range(2):
        with pytest.raises(SourceValidationError):
            await admin.publish_verified_query(query, "engineering", mutation)

    receipt = store.mutations[mutation.idempotency_key]
    assert receipt.outcome == "rejected"
    assert receipt.error_code == "SOURCE_VALIDATION_FAILED"
    assert receipt.result == {}
    assert query_calls == 1
    assert query.question not in repr(receipt)
    assert query.sql not in repr(receipt)


@pytest.mark.asyncio
async def test_verified_query_local_source_gap_is_not_receipted_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin, _registry, store, _invalidator, _cipher, _reloader = _services()
    await admin.publish("third-source", _manifest(), "first-secret")
    current = store.active["third-source"]
    rows = [{"status": "OPEN"}]
    query = PublishVerifiedQueryInput(
        query_id="third-source-retry-gap",
        source_id="third-source",
        question="로컬 적용 지연 뒤 재시도",
        sql="SELECT status FROM ai.issue_overview ORDER BY status LIMIT 1",
        metadata_revision=current.metadata_revision,
        relations=("ai.issue_overview",),
        expected=VerifiedExpectedInput(
            columns=("status",),
            row_count=1,
            result_hash=create_result_hash(("status",), rows),
        ),
    )
    original_query = admin._queries.query
    query_calls = 0

    async def intermittent_query(
        source_id: str,
        sql: str,
        metadata_revision: str,
        sql_policy_revision: str,
        *,
        query_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, object]:
        nonlocal query_calls
        query_calls += 1
        if query_calls == 1:
            raise SourceNotFoundError
        return await original_query(
            source_id,
            sql,
            metadata_revision,
            sql_policy_revision,
            query_id=query_id,
            tenant_id=tenant_id,
        )

    monkeypatch.setattr(admin._queries, "query", intermittent_query)
    mutation = _mutation_context(
        10,
        expected_generation=current.generation,
        expected_state_version=current.state_version,
    )

    with pytest.raises(SourceControlUnavailableError):
        await admin.publish_verified_query(query, "engineering", mutation)
    assert mutation.idempotency_key not in store.mutations

    response = await admin.publish_verified_query(query, "engineering", mutation)
    assert response["outcome"] == "succeeded"
    assert store.mutations[mutation.idempotency_key].outcome == "succeeded"
    assert query_calls == 2
