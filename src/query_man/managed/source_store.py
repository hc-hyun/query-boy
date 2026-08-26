from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from query_man.managed.secrets import EncryptedSecret
from query_man.metadata_store import encode_snapshot
from query_man.models import PreparedMetadata
from query_man.registry import (
    IDENTIFIER_PATTERN,
    POSTGRES_IDENTIFIER_MAX_LENGTH,
    QUALIFIED_RELATION_MAX_LENGTH,
    RELATION_NAME_PATTERN,
    STABLE_SLUG_MAX_LENGTH,
    STABLE_SLUG_PATTERN,
)
from query_man.verified import VerifiedQuery

POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807


class SourceGenerationConflictError(Exception):
    pass


class StoredSourceNotFoundError(Exception):
    pass


class SourcePublishPinnedError(Exception):
    pass


class MutationIdempotencyConflictError(Exception):
    pass


class MutationReplay(Exception):
    def __init__(self, receipt: MutationReceipt) -> None:
        super().__init__("The source mutation already has an authoritative receipt")
        self.receipt = receipt


class ReplicaObservationConflictError(Exception):
    pass


class GatewayUsageConflictError(Exception):
    pass


class ResourceObservationConflictError(Exception):
    pass


@dataclass(frozen=True)
class StoredSource:
    source_id: str
    generation: int
    manifest: dict[str, object]
    encrypted_secret: EncryptedSecret
    metadata_revision: str
    enabled: bool
    state_version: int = 1


@dataclass(frozen=True)
class SourceCatalogConnection:
    host: str
    port: int
    database: str
    user: str
    ssl: bool


@dataclass(frozen=True)
class SourceCatalogRecord:
    source_id: str
    generation: int
    enabled: bool
    state_version: int
    activated_at: datetime
    generation_created_at: datetime
    name: str
    description: str
    owner: str
    environment: str
    database_migration_ref: str
    budget_profile: str
    minimum_quality_level: str
    tenant_isolation: str
    connection: SourceCatalogConnection
    allowed_schemas: tuple[str, ...]
    allowed_relation_kinds: tuple[str, ...]
    semantic_default_relation: str | None
    semantic_relation_count: int
    semantic_join_count: int
    semantic_business_term_count: int
    semantic_question_rule_count: int
    semantic_composition_hint_count: int
    published_metadata_revision: str
    active_metadata_revision: str | None
    metadata_pinned: bool | None
    metadata_activated_at: datetime | None
    is_current: bool


@dataclass(frozen=True)
class SourceCatalogPage:
    items: tuple[SourceCatalogRecord, ...]
    next_after_source_id: str | None


@dataclass(frozen=True)
class SourceGenerationPage:
    current: SourceCatalogRecord
    items: tuple[SourceCatalogRecord, ...]
    next_before_generation: int | None


@dataclass(frozen=True)
class MutationRequest:
    idempotency_key: str
    request_hash: str
    operation: str
    source_id: str
    actor: str
    reason: str
    expected_generation: int
    expected_state_version: int


@dataclass(frozen=True)
class MutationReceipt:
    event_id: int
    idempotency_key: str
    request_hash: str
    operation: str
    source_id: str
    actor: str
    reason: str
    expected_generation: int
    expected_state_version: int
    outcome: str
    resulting_generation: int | None
    resulting_state_version: int | None
    http_status: int
    error_code: str | None
    result: dict[str, object]
    recorded_at: datetime


@dataclass(frozen=True)
class MutationPage:
    items: tuple[MutationReceipt, ...]
    next_before_event_id: int | None


@dataclass(frozen=True)
class _ReplicaSourceObservationWrite:
    source_id: str
    applied_generation: int | None
    applied_state_version: int | None
    applied_enabled: bool | None
    applied_metadata_revision: str | None
    source_health: str | None
    reason_code: str | None


@dataclass(frozen=True)
class _ResourceObservationWrite:
    metric: str
    value: int
    unit: str
    method: str
    definition_revision: str


@dataclass(frozen=True)
class _GatewayUsageDeltaWrite:
    source_id: str
    budget_profile: str
    metadata_revision: str
    definition_revision: str
    bucket_start: datetime
    query_count: int
    success_count: int
    rejected_count: int
    timeout_count: int
    overloaded_count: int
    cancelled_count: int
    failed_count: int
    queue_ms_sum: int
    elapsed_ms_sum: int
    returned_rows_sum: int
    result_bytes_sum: int
    truncated_count: int


@dataclass(frozen=True)
class ReplicaObservationRecord:
    replica_id: str
    observed_at: datetime
    fresh_until: datetime
    read_at: datetime
    report_reason_code: str | None
    applied_generation: int | None
    applied_state_version: int | None
    applied_enabled: bool | None
    applied_metadata_revision: str | None
    source_health: str | None
    reason_code: str | None


@dataclass(frozen=True)
class ReplicaObservationPage:
    source_id: str
    desired_generation: int
    desired_state_version: int
    desired_enabled: bool
    desired_metadata_revision: str | None
    items: tuple[ReplicaObservationRecord, ...]
    next_after_replica_id: str | None


@dataclass(frozen=True)
class ResourceObservationAttemptRecord:
    generation: int
    last_attempt_at: datetime
    last_attempt_outcome: str
    last_attempt_reason_code: str | None
    last_success_at: datetime | None
    last_success_has_representative: bool | None


@dataclass(frozen=True)
class ResourceObservationValueRecord:
    value: int
    metadata_revision: str
    sample_bucket_start: datetime
    observed_at: datetime
    fresh_until: datetime


@dataclass(frozen=True)
class ResourceObservationRecord:
    metric: str
    unit: str
    method: str
    definition_revision: str
    current: ResourceObservationValueRecord
    previous: ResourceObservationValueRecord | None


@dataclass(frozen=True)
class GatewayUsageRollupRecord:
    source_id: str
    budget_profile: str
    metadata_revision: str
    definition_revision: str
    bucket_start: datetime
    query_count: int
    success_count: int
    rejected_count: int
    timeout_count: int
    overloaded_count: int
    cancelled_count: int
    failed_count: int
    queue_ms_sum: int
    elapsed_ms_sum: int
    returned_rows_sum: int
    result_bytes_sum: int
    truncated_count: int
    observed_at: datetime


@dataclass(frozen=True)
class SourceUsageProjection:
    source_id: str
    enabled: bool
    generation: int
    resource_configured: bool
    read_at: datetime
    resource_attempt: ResourceObservationAttemptRecord | None
    resource_observations: tuple[ResourceObservationRecord, ...]
    live_reporter_count: int
    live_current_cursor_count: int
    live_fresh_cursor_count: int
    accepted_cursor_count: int
    last_report_at: datetime | None
    reporter_fresh_until: datetime | None
    window_start: datetime
    window_end: datetime
    gateway_rollups: tuple[GatewayUsageRollupRecord, ...]


_CATALOG_PROJECTION = (
    "active.source_id, revision.generation, active.enabled, active.state_version, "
    "active.activated_at, revision.created_at AS generation_created_at, "
    "revision.manifest -> 'version' AS manifest_version, "
    "revision.manifest ->> 'name' AS name, "
    "revision.manifest ->> 'description' AS description, "
    "revision.manifest #>> '{provenance,owner}' AS owner, "
    "revision.manifest #>> '{provenance,environment}' AS environment, "
    "revision.manifest #>> '{provenance,database_migration_ref}' "
    "AS database_migration_ref, "
    "revision.manifest ->> 'budget_profile' AS budget_profile, "
    "revision.manifest ->> 'minimum_quality_level' AS minimum_quality_level, "
    "revision.manifest ->> 'tenant_isolation' AS tenant_isolation, "
    "revision.manifest #>> '{connection,host}' AS connection_host, "
    "revision.manifest #> '{connection,port}' AS connection_port, "
    "revision.manifest #>> '{connection,database}' AS connection_database, "
    "revision.manifest #>> '{connection,user}' AS connection_user, "
    "revision.manifest #> '{connection,ssl}' AS connection_ssl, "
    "revision.manifest -> 'allowed_schemas' AS allowed_schemas, "
    "revision.manifest -> 'allowed_relation_kinds' AS allowed_relation_kinds, "
    "revision.manifest #>> '{semantic_overlay,default_relation}' "
    "AS semantic_default_relation, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,relations}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,relations}') END AS semantic_relation_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,joins}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,joins}') END AS semantic_join_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,business_terms}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,business_terms}') END AS semantic_business_term_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,question_rules}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,question_rules}') END AS semantic_question_rule_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,composition_hints}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,composition_hints}') END AS semantic_composition_hint_count, "
    "revision.metadata_revision AS published_metadata_revision, "
    "metadata.revision AS active_metadata_revision, metadata.pinned AS metadata_pinned, "
    "metadata.activated_at AS metadata_activated_at, "
    "active.generation = revision.generation AS is_current "
)
_MUTATION_PROJECTION = (
    "receipt.event_id, receipt.idempotency_key::text AS idempotency_key, "
    "receipt.request_hash, receipt.operation, receipt.source_id, receipt.actor, "
    "receipt.reason, receipt.expected_generation, receipt.expected_state_version, "
    "receipt.outcome, receipt.resulting_generation, "
    "receipt.resulting_state_version, receipt.http_status, receipt.error_code, "
    "receipt.result, receipt.recorded_at "
)
_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_RELATION_NAME = re.compile(RELATION_NAME_PATTERN)
_STABLE_SLUG = re.compile(STABLE_SLUG_PATTERN)
_IDEMPOTENCY_KEY = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_REQUEST_HASH = re.compile(r"^hmac-sha256:[a-f0-9]{64}$")
_ACTOR = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,79}$")
_REASON = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MUTATION_OPERATIONS = frozenset(
    {
        "publish_source",
        "rotate_credential",
        "publish_verified_query",
        "rollback_source",
        "resume_metadata_publish",
        "deactivate_source",
    }
)
_REPLICA_HEARTBEAT_INTERVAL_MIN_MS = 5_000
_REPLICA_HEARTBEAT_INTERVAL_MAX_MS = 300_000
_REPLICA_REPORT_REASONS = frozenset({"CONTROL_SCAN_FAILED"})
_REPLICA_SOURCE_REASONS = frozenset(
    {
        "RUNTIME_VALIDATION_REJECTED",
        "RUNTIME_APPLY_FAILED",
        "METADATA_PROBE_FAILED",
    }
)
_SOURCE_HEALTH_VALUES = frozenset(
    {"initializing", "healthy", "stale", "unavailable"}
)
_RESOURCE_METRIC_CONTRACT = {
    "representative_records": ("rows", "postgres_catalog_estimate"),
    "table_bytes": ("bytes", "postgres_relation_size"),
    "index_bytes": ("bytes", "postgres_relation_size"),
    "total_storage_bytes": ("bytes", "postgres_relation_size"),
}
_RESOURCE_MANDATORY_METRICS = frozenset(
    {"table_bytes", "index_bytes", "total_storage_bytes"}
)
_RESOURCE_ATTEMPT_FAILURE_REASONS = frozenset(
    {"METADATA_UNAVAILABLE", "RESOURCE_READ_FAILED"}
)
_RESOURCE_FRESHNESS = timedelta(hours=72)
_GATEWAY_USAGE_MAX_BATCH = 100
_GATEWAY_USAGE_MAX_ROWS_PER_SOURCE = 1_000
_GATEWAY_USAGE_RETENTION = timedelta(days=31)
_GATEWAY_REPORT_FRESHNESS = timedelta(seconds=180)


class PostgresSourceStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: AsyncConnectionPool[Any] | None = None
        self._pool_lock = asyncio.Lock()

    async def list_active(self) -> list[StoredSource]:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT active.source_id, active.generation, active.enabled, "
                "active.state_version, "
                "revision.manifest, revision.secret_nonce, revision.secret_ciphertext, "
                "revision.metadata_revision "
                "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "ORDER BY active.source_id"
            )
            rows = await cursor.fetchall()
        return [_decode(row) for row in rows]

    async def get_active(self, source_id: str) -> StoredSource | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT active.source_id, active.generation, active.enabled, "
                "active.state_version, "
                "revision.manifest, revision.secret_nonce, revision.secret_ciphertext, "
                "revision.metadata_revision "
                "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "WHERE active.source_id = %s",
                (source_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _decode(row)

    async def get_revision(self, source_id: str, generation: int) -> StoredSource:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT source_id, generation, true AS enabled, 0 AS state_version, "
                "manifest, secret_nonce, "
                "secret_ciphertext, metadata_revision "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s AND generation = %s",
                (source_id, generation),
            )
            row = await cursor.fetchone()
        if row is None:
            raise StoredSourceNotFoundError
        return _decode(row)

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
        _validate_page_limit(limit)
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT "
                + _CATALOG_PROJECTION
                + "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = active.source_id "
                "WHERE (%s::text IS NULL OR active.source_id > %s) "
                "AND (%s::boolean IS NULL OR active.enabled = %s) "
                "AND (%s::text IS NULL OR "
                "revision.manifest #>> '{provenance,owner}' = %s) "
                "AND (%s::text IS NULL OR "
                "revision.manifest #>> '{provenance,environment}' = %s) "
                "AND (%s::text IS NULL OR revision.manifest ->> 'budget_profile' = %s) "
                "ORDER BY active.source_id ASC LIMIT %s",
                (
                    after_source_id,
                    after_source_id,
                    enabled,
                    enabled,
                    owner,
                    owner,
                    environment,
                    environment,
                    budget_profile,
                    budget_profile,
                    limit + 1,
                ),
            )
            rows = await cursor.fetchall()
        records = tuple(_decode_catalog(row) for row in rows[:limit])
        return SourceCatalogPage(
            items=records,
            next_after_source_id=(records[-1].source_id if len(rows) > limit else None),
        )

    async def get_catalog(self, source_id: str) -> SourceCatalogRecord | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT "
                + _CATALOG_PROJECTION
                + "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = active.source_id "
                "WHERE active.source_id = %s",
                (source_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _decode_catalog(row)

    async def register_replica(
        self,
        replica_id: str,
        heartbeat_interval_ms: int,
    ) -> int:
        _validate_replica_id(replica_id)
        _validate_heartbeat_interval(heartbeat_interval_ms)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "INSERT INTO control.runtime_replicas "
                "(replica_id, incarnation, heartbeat_interval_ms, "
                "report_reason_code, observed_at) "
                "VALUES (%s, 1, %s, NULL, clock_timestamp()) "
                "ON CONFLICT (replica_id) DO UPDATE "
                "SET incarnation = control.runtime_replicas.incarnation + 1, "
                "heartbeat_interval_ms = EXCLUDED.heartbeat_interval_ms, "
                "report_reason_code = NULL, observed_at = clock_timestamp() "
                "WHERE control.runtime_replicas.incarnation < %s "
                "RETURNING incarnation",
                (replica_id, heartbeat_interval_ms, POSTGRES_BIGINT_MAX),
            )
            row = await cursor.fetchone()
            if row is None:
                raise ReplicaObservationConflictError
            incarnation = int(row["incarnation"])
            await connection.execute(
                "UPDATE control.runtime_source_observations "
                "SET applied_generation = NULL, applied_state_version = NULL, "
                "applied_enabled = NULL, applied_metadata_revision = NULL, "
                "source_health = NULL, reason_code = NULL "
                "WHERE replica_id = %s",
                (replica_id,),
            )
        return incarnation

    async def report_replica(
        self,
        replica_id: str,
        incarnation: int,
        *,
        reason_code: str | None,
        sources: tuple[_ReplicaSourceObservationWrite, ...],
    ) -> None:
        _validate_replica_id(replica_id)
        _validate_positive_bigint(incarnation, "Replica incarnation")
        if reason_code is not None and reason_code not in _REPLICA_REPORT_REASONS:
            raise ValueError("Replica report reason is invalid")
        if reason_code is not None and sources:
            raise ValueError("A failed replica scan cannot publish source observations")
        source_ids: set[str] = set()
        for source in sources:
            _validate_replica_source_observation(source)
            if source.source_id in source_ids:
                raise ValueError("Replica source observations must be unique")
            source_ids.add(source.source_id)

        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            cursor = await connection.execute(
                "UPDATE control.runtime_replicas "
                "SET report_reason_code = %s, observed_at = clock_timestamp() "
                "WHERE replica_id = %s AND incarnation = %s "
                "RETURNING replica_id",
                (reason_code, replica_id, incarnation),
            )
            if await cursor.fetchone() is None:
                raise ReplicaObservationConflictError
            if reason_code is not None:
                return
            await connection.execute(
                "UPDATE control.runtime_source_observations "
                "SET applied_generation = NULL, applied_state_version = NULL, "
                "applied_enabled = NULL, applied_metadata_revision = NULL, "
                "source_health = NULL, reason_code = NULL "
                "WHERE replica_id = %s",
                (replica_id,),
            )
            for source in sources:
                await connection.execute(
                    "INSERT INTO control.runtime_source_observations "
                    "(replica_id, incarnation, source_id, applied_generation, "
                    "applied_state_version, applied_enabled, "
                    "applied_metadata_revision, source_health, reason_code) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (replica_id, source_id) DO UPDATE "
                    "SET incarnation = EXCLUDED.incarnation, "
                    "applied_generation = EXCLUDED.applied_generation, "
                    "applied_state_version = EXCLUDED.applied_state_version, "
                    "applied_enabled = EXCLUDED.applied_enabled, "
                    "applied_metadata_revision = EXCLUDED.applied_metadata_revision, "
                    "source_health = EXCLUDED.source_health, "
                    "reason_code = EXCLUDED.reason_code",
                    (
                        replica_id,
                        incarnation,
                        source.source_id,
                        source.applied_generation,
                        source.applied_state_version,
                        source.applied_enabled,
                        source.applied_metadata_revision,
                        source.source_health,
                        source.reason_code,
                    ),
                )

    async def report_resource_observations(
        self,
        source_id: str,
        generation: int,
        metadata_revision: str,
        observations: tuple[_ResourceObservationWrite, ...],
    ) -> None:
        _validate_source_id(source_id)
        _validate_positive_bigint(generation, "Resource observation generation")
        if not _is_revision(metadata_revision):
            raise ValueError("Resource observation metadata revision is invalid")
        if not 3 <= len(observations) <= len(_RESOURCE_METRIC_CONTRACT):
            raise ValueError("Resource observation batch size is invalid")
        metrics: set[str] = set()
        for observation in observations:
            _validate_resource_observation(observation)
            if observation.metric in metrics:
                raise ValueError("Resource observation metrics must be unique")
            metrics.add(observation.metric)
        if not _RESOURCE_MANDATORY_METRICS.issubset(metrics):
            raise ValueError("Resource observation mandatory metrics are missing")

        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_resource_observation(connection, source_id)
            await _require_resource_observation_generation(
                connection,
                source_id,
                generation,
                metadata_revision=metadata_revision,
            )
            clock_cursor = await connection.execute(
                "WITH read_clock AS MATERIALIZED ("
                "SELECT clock_timestamp() AS observed_at"
                ") SELECT observed_at, "
                "date_trunc('day', observed_at AT TIME ZONE 'UTC') "
                "AT TIME ZONE 'UTC' AS sample_bucket_start "
                "FROM read_clock"
            )
            clock_row = await clock_cursor.fetchone()
            if clock_row is None:
                raise RuntimeError("Control resource observation clock is unavailable")
            observed_at = _required_timestamp(clock_row, "observed_at")
            sample_bucket_start = _required_timestamp(
                clock_row,
                "sample_bucket_start",
            )
            fresh_until = observed_at + _RESOURCE_FRESHNESS
            for observation in sorted(observations, key=lambda item: item.metric):
                await connection.execute(
                    "INSERT INTO control.source_resource_observations "
                    "(source_id, metric, unit, method, definition_revision, value, "
                    "metadata_revision, sample_bucket_start, observed_at, fresh_until) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (source_id, metric) DO UPDATE SET "
                    "unit = EXCLUDED.unit, method = EXCLUDED.method, "
                    "definition_revision = EXCLUDED.definition_revision, "
                    "previous_value = CASE "
                    "WHEN EXCLUDED.sample_bucket_start > "
                    "control.source_resource_observations.sample_bucket_start "
                    "AND EXCLUDED.method = control.source_resource_observations.method "
                    "AND EXCLUDED.definition_revision = "
                    "control.source_resource_observations.definition_revision "
                    "THEN control.source_resource_observations.value "
                    "WHEN EXCLUDED.method <> control.source_resource_observations.method "
                    "OR EXCLUDED.definition_revision <> "
                    "control.source_resource_observations.definition_revision "
                    "THEN NULL ELSE control.source_resource_observations.previous_value END, "
                    "previous_metadata_revision = CASE "
                    "WHEN EXCLUDED.sample_bucket_start > "
                    "control.source_resource_observations.sample_bucket_start "
                    "AND EXCLUDED.method = control.source_resource_observations.method "
                    "AND EXCLUDED.definition_revision = "
                    "control.source_resource_observations.definition_revision "
                    "THEN control.source_resource_observations.metadata_revision "
                    "WHEN EXCLUDED.method <> control.source_resource_observations.method "
                    "OR EXCLUDED.definition_revision <> "
                    "control.source_resource_observations.definition_revision "
                    "THEN NULL ELSE "
                    "control.source_resource_observations.previous_metadata_revision END, "
                    "previous_sample_bucket_start = CASE "
                    "WHEN EXCLUDED.sample_bucket_start > "
                    "control.source_resource_observations.sample_bucket_start "
                    "AND EXCLUDED.method = control.source_resource_observations.method "
                    "AND EXCLUDED.definition_revision = "
                    "control.source_resource_observations.definition_revision "
                    "THEN control.source_resource_observations.sample_bucket_start "
                    "WHEN EXCLUDED.method <> control.source_resource_observations.method "
                    "OR EXCLUDED.definition_revision <> "
                    "control.source_resource_observations.definition_revision "
                    "THEN NULL ELSE "
                    "control.source_resource_observations.previous_sample_bucket_start END, "
                    "previous_observed_at = CASE "
                    "WHEN EXCLUDED.sample_bucket_start > "
                    "control.source_resource_observations.sample_bucket_start "
                    "AND EXCLUDED.method = control.source_resource_observations.method "
                    "AND EXCLUDED.definition_revision = "
                    "control.source_resource_observations.definition_revision "
                    "THEN control.source_resource_observations.observed_at "
                    "WHEN EXCLUDED.method <> control.source_resource_observations.method "
                    "OR EXCLUDED.definition_revision <> "
                    "control.source_resource_observations.definition_revision "
                    "THEN NULL ELSE "
                    "control.source_resource_observations.previous_observed_at END, "
                    "previous_fresh_until = CASE "
                    "WHEN EXCLUDED.sample_bucket_start > "
                    "control.source_resource_observations.sample_bucket_start "
                    "AND EXCLUDED.method = control.source_resource_observations.method "
                    "AND EXCLUDED.definition_revision = "
                    "control.source_resource_observations.definition_revision "
                    "THEN control.source_resource_observations.fresh_until "
                    "WHEN EXCLUDED.method <> control.source_resource_observations.method "
                    "OR EXCLUDED.definition_revision <> "
                    "control.source_resource_observations.definition_revision "
                    "THEN NULL ELSE "
                    "control.source_resource_observations.previous_fresh_until END, "
                    "value = EXCLUDED.value, "
                    "metadata_revision = EXCLUDED.metadata_revision, "
                    "sample_bucket_start = EXCLUDED.sample_bucket_start, "
                    "observed_at = EXCLUDED.observed_at, "
                    "fresh_until = EXCLUDED.fresh_until "
                    "WHERE EXCLUDED.sample_bucket_start >= "
                    "control.source_resource_observations.sample_bucket_start",
                    (
                        source_id,
                        observation.metric,
                        observation.unit,
                        observation.method,
                        observation.definition_revision,
                        observation.value,
                        metadata_revision,
                        sample_bucket_start,
                        observed_at,
                        fresh_until,
                    ),
                )
            await connection.execute(
                "INSERT INTO control.source_resource_observation_attempts "
                "(source_id, generation, last_attempt_at, last_attempt_outcome, "
                "last_attempt_reason_code, last_success_at, "
                "last_success_has_representative) "
                "VALUES (%s, %s, %s, 'succeeded', NULL, %s, %s) "
                "ON CONFLICT (source_id) DO UPDATE SET "
                "generation = EXCLUDED.generation, "
                "last_attempt_at = EXCLUDED.last_attempt_at, "
                "last_attempt_outcome = EXCLUDED.last_attempt_outcome, "
                "last_attempt_reason_code = NULL, "
                "last_success_at = EXCLUDED.last_success_at, "
                "last_success_has_representative = "
                "EXCLUDED.last_success_has_representative",
                (
                    source_id,
                    generation,
                    observed_at,
                    observed_at,
                    "representative_records" in metrics,
                ),
            )

    async def report_resource_observation_failure(
        self,
        source_id: str,
        generation: int,
        reason_code: str,
    ) -> None:
        _validate_source_id(source_id)
        _validate_positive_bigint(generation, "Resource observation generation")
        if reason_code not in _RESOURCE_ATTEMPT_FAILURE_REASONS:
            raise ValueError("Resource observation failure reason is invalid")

        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_resource_observation(connection, source_id)
            await _require_resource_observation_generation(
                connection,
                source_id,
                generation,
            )
            clock_cursor = await connection.execute(
                "SELECT clock_timestamp() AS attempted_at"
            )
            clock_row = await clock_cursor.fetchone()
            if clock_row is None:
                raise RuntimeError("Control resource observation clock is unavailable")
            attempted_at = _required_timestamp(clock_row, "attempted_at")
            await connection.execute(
                "INSERT INTO control.source_resource_observation_attempts "
                "(source_id, generation, last_attempt_at, last_attempt_outcome, "
                "last_attempt_reason_code, last_success_at, "
                "last_success_has_representative) "
                "VALUES (%s, %s, %s, 'failed', %s, NULL, NULL) "
                "ON CONFLICT (source_id) DO UPDATE SET "
                "generation = EXCLUDED.generation, "
                "last_attempt_at = EXCLUDED.last_attempt_at, "
                "last_attempt_outcome = EXCLUDED.last_attempt_outcome, "
                "last_attempt_reason_code = EXCLUDED.last_attempt_reason_code, "
                "last_success_at = CASE WHEN "
                "control.source_resource_observation_attempts.generation = "
                "EXCLUDED.generation THEN "
                "control.source_resource_observation_attempts.last_success_at END, "
                "last_success_has_representative = CASE WHEN "
                "control.source_resource_observation_attempts.generation = "
                "EXCLUDED.generation THEN "
                "control.source_resource_observation_attempts."
                "last_success_has_representative END",
                (source_id, generation, attempted_at, reason_code),
            )

    async def report_gateway_usage(
        self,
        replica_id: str,
        incarnation: int,
        sequence: int,
        deltas: tuple[_GatewayUsageDeltaWrite, ...],
    ) -> None:
        _validate_replica_id(replica_id)
        _validate_positive_bigint(incarnation, "Gateway reporter incarnation")
        _validate_positive_bigint(sequence, "Gateway report sequence")
        if len(deltas) > _GATEWAY_USAGE_MAX_BATCH:
            raise ValueError("Gateway usage report batch is too large")
        for delta in deltas:
            _validate_gateway_usage_delta(delta)
        payload_hash = _gateway_usage_payload_hash(deltas)

        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_gateway_reporter(connection, replica_id)
            clock_cursor = await connection.execute("SELECT clock_timestamp() AS read_at")
            clock_row = await clock_cursor.fetchone()
            if clock_row is None:
                raise RuntimeError("Control database clock is unavailable")
            read_at = _required_timestamp(clock_row, "read_at")
            current_bucket = _utc_hour(read_at)
            oldest_bucket = current_bucket - _GATEWAY_USAGE_RETENTION

            cursor = await connection.execute(
                "SELECT incarnation, last_sequence, last_payload_hash "
                "FROM control.gateway_usage_report_cursors "
                "WHERE replica_id = %s FOR UPDATE",
                (replica_id,),
            )
            stored_cursor = await cursor.fetchone()
            is_replay = False
            if stored_cursor is None:
                if sequence != 1:
                    raise GatewayUsageConflictError
            elif int(stored_cursor["incarnation"]) == incarnation:
                last_sequence = int(stored_cursor["last_sequence"])
                if sequence == last_sequence:
                    if stored_cursor["last_payload_hash"] == payload_hash:
                        is_replay = True
                    else:
                        raise GatewayUsageConflictError
                if sequence != last_sequence + 1:
                    if not is_replay:
                        raise GatewayUsageConflictError
            elif sequence != 1:
                raise GatewayUsageConflictError

            source_ids = sorted({delta.source_id for delta in deltas})
            if not is_replay:
                if any(
                    delta.bucket_start < oldest_bucket
                    or delta.bucket_start > current_bucket
                    for delta in deltas
                ):
                    raise ValueError("Gateway usage bucket is outside the retention window")
                for source_id in source_ids:
                    await _lock_gateway_usage(connection, source_id)
                for delta in deltas:
                    await connection.execute(
                        "INSERT INTO control.gateway_usage_rollups "
                        "(source_id, budget_profile, metadata_revision, "
                        "definition_revision, bucket_start, query_count, success_count, "
                        "rejected_count, timeout_count, overloaded_count, cancelled_count, "
                        "failed_count, queue_ms_sum, elapsed_ms_sum, returned_rows_sum, "
                        "result_bytes_sum, truncated_count, observed_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                        "%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (source_id, budget_profile, metadata_revision, "
                        "definition_revision, bucket_start) DO UPDATE SET "
                        "query_count = control.gateway_usage_rollups.query_count + "
                        "EXCLUDED.query_count, "
                        "success_count = control.gateway_usage_rollups.success_count + "
                        "EXCLUDED.success_count, "
                        "rejected_count = control.gateway_usage_rollups.rejected_count + "
                        "EXCLUDED.rejected_count, "
                        "timeout_count = control.gateway_usage_rollups.timeout_count + "
                        "EXCLUDED.timeout_count, "
                        "overloaded_count = control.gateway_usage_rollups.overloaded_count + "
                        "EXCLUDED.overloaded_count, "
                        "cancelled_count = control.gateway_usage_rollups.cancelled_count + "
                        "EXCLUDED.cancelled_count, "
                        "failed_count = control.gateway_usage_rollups.failed_count + "
                        "EXCLUDED.failed_count, "
                        "queue_ms_sum = control.gateway_usage_rollups.queue_ms_sum + "
                        "EXCLUDED.queue_ms_sum, "
                        "elapsed_ms_sum = control.gateway_usage_rollups.elapsed_ms_sum + "
                        "EXCLUDED.elapsed_ms_sum, "
                        "returned_rows_sum = control.gateway_usage_rollups.returned_rows_sum + "
                        "EXCLUDED.returned_rows_sum, "
                        "result_bytes_sum = control.gateway_usage_rollups.result_bytes_sum + "
                        "EXCLUDED.result_bytes_sum, "
                        "truncated_count = control.gateway_usage_rollups.truncated_count + "
                        "EXCLUDED.truncated_count, observed_at = GREATEST("
                        "control.gateway_usage_rollups.observed_at, "
                        "EXCLUDED.observed_at)",
                        (
                            delta.source_id,
                            delta.budget_profile,
                            delta.metadata_revision,
                            delta.definition_revision,
                            delta.bucket_start,
                            delta.query_count,
                            delta.success_count,
                            delta.rejected_count,
                            delta.timeout_count,
                            delta.overloaded_count,
                            delta.cancelled_count,
                            delta.failed_count,
                            delta.queue_ms_sum,
                            delta.elapsed_ms_sum,
                            delta.returned_rows_sum,
                            delta.result_bytes_sum,
                            delta.truncated_count,
                            read_at,
                        ),
                    )
                for source_id in source_ids:
                    await connection.execute(
                        "DELETE FROM control.gateway_usage_rollups WHERE ctid IN ("
                        "SELECT ctid FROM control.gateway_usage_rollups "
                        "WHERE source_id = %s "
                        "ORDER BY bucket_start DESC, observed_at DESC, budget_profile, "
                        "metadata_revision, definition_revision "
                        "OFFSET %s)",
                        (source_id, _GATEWAY_USAGE_MAX_ROWS_PER_SOURCE),
                    )

            replica_cursor = await connection.execute(
                "WITH report_clock AS MATERIALIZED ("
                "SELECT clock_timestamp() AS reported_at"
                ") SELECT replica.incarnation, report_clock.reported_at "
                "FROM control.runtime_replicas AS replica CROSS JOIN report_clock "
                "WHERE replica.replica_id = %s FOR UPDATE OF replica",
                (replica_id,),
            )
            replica_row = await replica_cursor.fetchone()
            if replica_row is None or int(replica_row["incarnation"]) != incarnation:
                raise GatewayUsageConflictError
            if is_replay:
                return
            reported_at = _required_timestamp(replica_row, "reported_at")
            await connection.execute(
                "INSERT INTO control.gateway_usage_report_cursors "
                "(replica_id, incarnation, last_sequence, last_payload_hash, "
                "observed_at, fresh_until) VALUES (%s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (replica_id) DO UPDATE SET "
                "incarnation = EXCLUDED.incarnation, "
                "last_sequence = EXCLUDED.last_sequence, "
                "last_payload_hash = EXCLUDED.last_payload_hash, "
                "observed_at = EXCLUDED.observed_at, "
                "fresh_until = EXCLUDED.fresh_until",
                (
                    replica_id,
                    incarnation,
                    sequence,
                    payload_hash,
                    reported_at,
                    reported_at + _GATEWAY_REPORT_FRESHNESS,
                ),
            )

    async def list_replica_observations(
        self,
        source_id: str,
        *,
        after_replica_id: str | None = None,
        limit: int = 50,
    ) -> ReplicaObservationPage | None:
        _validate_replica_id(source_id)
        if after_replica_id is not None:
            _validate_replica_id(after_replica_id)
        _validate_page_limit(limit)
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "WITH read_clock AS MATERIALIZED ("
                "SELECT clock_timestamp() AS read_at"
                "), desired AS MATERIALIZED ("
                "SELECT active.source_id, active.generation, active.state_version, "
                "active.enabled, CASE WHEN active.enabled THEN metadata.revision END "
                "AS metadata_revision "
                "FROM control.active_source_profiles AS active "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = active.source_id "
                "WHERE active.source_id = %s"
                "), selected_replicas AS MATERIALIZED ("
                "SELECT replica_id, incarnation, heartbeat_interval_ms, "
                "report_reason_code, observed_at "
                "FROM control.runtime_replicas "
                "WHERE (%s::text IS NULL OR replica_id COLLATE \"C\" > "
                "%s::text COLLATE \"C\") "
                "ORDER BY replica_id COLLATE \"C\" ASC LIMIT %s"
                ") SELECT desired.source_id, desired.generation AS desired_generation, "
                "desired.state_version AS desired_state_version, "
                "desired.enabled AS desired_enabled, "
                "desired.metadata_revision AS desired_metadata_revision, "
                "replica.replica_id, replica.report_reason_code, replica.observed_at, "
                "replica.observed_at + "
                "replica.heartbeat_interval_ms * interval '3 milliseconds' "
                "AS fresh_until, read_clock.read_at, "
                "observation.applied_generation, "
                "observation.applied_state_version, observation.applied_enabled, "
                "observation.applied_metadata_revision, observation.source_health, "
                "observation.reason_code "
                "FROM desired CROSS JOIN read_clock "
                "LEFT JOIN selected_replicas AS replica ON true "
                "LEFT JOIN control.runtime_source_observations AS observation "
                "ON observation.replica_id = replica.replica_id "
                "AND observation.incarnation = replica.incarnation "
                "AND observation.source_id = desired.source_id "
                "ORDER BY replica.replica_id COLLATE \"C\" ASC NULLS LAST",
                (
                    source_id,
                    after_replica_id,
                    after_replica_id,
                    limit + 1,
                ),
            )
            rows = await cursor.fetchall()
        if not rows:
            return None
        desired = _decode_desired_replica_state(rows[0])
        replica_rows = [row for row in rows if row.get("replica_id") is not None]
        records = tuple(
            _decode_replica_observation(row) for row in replica_rows[:limit]
        )
        return ReplicaObservationPage(
            source_id=desired[0],
            desired_generation=desired[1],
            desired_state_version=desired[2],
            desired_enabled=desired[3],
            desired_metadata_revision=desired[4],
            items=records,
            next_after_replica_id=(
                records[-1].replica_id if len(replica_rows) > limit else None
            ),
        )

    async def get_source_usage(self, source_id: str) -> SourceUsageProjection | None:
        _validate_source_id(source_id)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            source_cursor = await connection.execute(
                "SELECT active.source_id, active.enabled, active.generation, "
                "revision.manifest ? 'observability' "
                "AS resource_observability_present, "
                "jsonb_typeof(revision.manifest -> 'observability') "
                "AS resource_observability_type, clock_timestamp() AS read_at "
                "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "WHERE active.source_id = %s",
                (source_id,),
            )
            source_row = await source_cursor.fetchone()
            if source_row is None:
                return None
            read_at = _required_timestamp(source_row, "read_at")
            window_end = _utc_hour(read_at)
            window_start = window_end - _GATEWAY_USAGE_RETENTION

            attempt_cursor = await connection.execute(
                "SELECT generation, last_attempt_at, last_attempt_outcome, "
                "last_attempt_reason_code, last_success_at, "
                "last_success_has_representative "
                "FROM control.source_resource_observation_attempts "
                "WHERE source_id = %s",
                (source_id,),
            )
            attempt_row = await attempt_cursor.fetchone()

            resource_cursor = await connection.execute(
                "SELECT metric, unit, method, definition_revision, value, "
                "metadata_revision, sample_bucket_start, observed_at, fresh_until, "
                "previous_value, previous_metadata_revision, "
                "previous_sample_bucket_start, previous_observed_at, "
                "previous_fresh_until "
                "FROM control.source_resource_observations "
                "WHERE source_id = %s "
                "ORDER BY CASE metric "
                "WHEN 'representative_records' THEN 1 "
                "WHEN 'table_bytes' THEN 2 WHEN 'index_bytes' THEN 3 "
                "WHEN 'total_storage_bytes' THEN 4 ELSE 5 END",
                (source_id,),
            )
            resource_rows = await resource_cursor.fetchall()
            if len(resource_rows) > len(_RESOURCE_METRIC_CONTRACT):
                raise RuntimeError("Resource observation cardinality is invalid")

            reporter_cursor = await connection.execute(
                "WITH read_clock(read_at) AS (VALUES (%s::timestamptz)) "
                "SELECT "
                "count(*) FILTER (WHERE read_clock.read_at <= "
                "replica.observed_at + replica.heartbeat_interval_ms * "
                "interval '3 milliseconds') AS live_reporter_count, "
                "count(*) FILTER (WHERE read_clock.read_at <= "
                "replica.observed_at + replica.heartbeat_interval_ms * "
                "interval '3 milliseconds' "
                "AND cursor.incarnation = replica.incarnation) "
                "AS live_current_cursor_count, "
                "count(*) FILTER (WHERE read_clock.read_at <= "
                "replica.observed_at + replica.heartbeat_interval_ms * "
                "interval '3 milliseconds' "
                "AND cursor.incarnation = replica.incarnation "
                "AND read_clock.read_at <= cursor.fresh_until) "
                "AS live_fresh_cursor_count, "
                "count(cursor.replica_id) AS accepted_cursor_count, "
                "max(cursor.observed_at) AS last_report_at, "
                "min(cursor.fresh_until) FILTER (WHERE read_clock.read_at <= "
                "replica.observed_at + replica.heartbeat_interval_ms * "
                "interval '3 milliseconds' "
                "AND cursor.incarnation = replica.incarnation) "
                "AS reporter_fresh_until "
                "FROM control.runtime_replicas AS replica "
                "LEFT JOIN control.gateway_usage_report_cursors AS cursor "
                "ON cursor.replica_id = replica.replica_id "
                "CROSS JOIN read_clock",
                (read_at,),
            )
            reporter_row = await reporter_cursor.fetchone()
            if reporter_row is None:
                raise RuntimeError("Gateway reporter projection is unavailable")

            usage_cursor = await connection.execute(
                "SELECT source_id, budget_profile, metadata_revision, "
                "definition_revision, bucket_start, query_count, success_count, "
                "rejected_count, timeout_count, overloaded_count, cancelled_count, "
                "failed_count, queue_ms_sum, elapsed_ms_sum, returned_rows_sum, "
                "result_bytes_sum, truncated_count, observed_at "
                "FROM control.gateway_usage_rollups "
                "WHERE source_id = %s AND bucket_start >= %s AND bucket_start <= %s "
                "ORDER BY bucket_start DESC, observed_at DESC, "
                "budget_profile COLLATE \"C\" ASC, metadata_revision ASC, "
                "definition_revision ASC LIMIT %s",
                (
                    source_id,
                    window_start,
                    window_end,
                    _GATEWAY_USAGE_MAX_ROWS_PER_SOURCE + 1,
                ),
            )
            usage_rows = await usage_cursor.fetchall()
            if len(usage_rows) > _GATEWAY_USAGE_MAX_ROWS_PER_SOURCE:
                raise RuntimeError("Gateway usage cardinality is invalid")

        return SourceUsageProjection(
            source_id=_stable_slug(source_row, "source_id"),
            enabled=_required_bool(source_row, "enabled"),
            generation=_bounded_int(
                source_row,
                "generation",
                1,
                POSTGRES_BIGINT_MAX,
            ),
            resource_configured=_decode_resource_observability_configured(source_row),
            read_at=read_at,
            resource_attempt=(
                None
                if attempt_row is None
                else _decode_resource_observation_attempt(attempt_row)
            ),
            resource_observations=tuple(
                _decode_resource_observation(row) for row in resource_rows
            ),
            live_reporter_count=_projection_count(
                reporter_row,
                "live_reporter_count",
            ),
            live_current_cursor_count=_projection_count(
                reporter_row,
                "live_current_cursor_count",
            ),
            live_fresh_cursor_count=_projection_count(
                reporter_row,
                "live_fresh_cursor_count",
            ),
            accepted_cursor_count=_projection_count(
                reporter_row,
                "accepted_cursor_count",
            ),
            last_report_at=_optional_timestamp(reporter_row, "last_report_at"),
            reporter_fresh_until=_optional_timestamp(
                reporter_row,
                "reporter_fresh_until",
            ),
            window_start=window_start,
            window_end=window_end,
            gateway_rollups=tuple(
                _decode_gateway_usage_rollup(row) for row in usage_rows
            ),
        )

    async def list_generation_history(
        self,
        source_id: str,
        *,
        before_generation: int | None = None,
        limit: int = 50,
    ) -> SourceGenerationPage | None:
        _validate_page_limit(limit)
        _validate_before_generation(before_generation)
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "WITH selected_generations AS MATERIALIZED ("
                "SELECT source_id, generation "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s "
                "AND (%s::bigint IS NULL OR generation < %s) "
                "ORDER BY generation DESC LIMIT %s"
                ") "
                "SELECT 0 AS catalog_row_order, "
                + _CATALOG_PROJECTION
                + "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = active.source_id "
                "WHERE active.source_id = %s "
                "UNION ALL "
                "SELECT 1 AS catalog_row_order, "
                + _CATALOG_PROJECTION
                + "FROM selected_generations AS selected "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = selected.source_id "
                "AND revision.generation = selected.generation "
                "JOIN control.active_source_profiles AS active "
                "ON active.source_id = revision.source_id "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = revision.source_id "
                "ORDER BY catalog_row_order ASC, generation DESC",
                (
                    source_id,
                    before_generation,
                    before_generation,
                    limit + 1,
                    source_id,
                ),
            )
            rows = await cursor.fetchall()
        if not rows:
            return None
        current_rows = [row for row in rows if row.get("catalog_row_order") == 0]
        history_rows = [row for row in rows if row.get("catalog_row_order") == 1]
        if len(current_rows) != 1 or len(current_rows) + len(history_rows) != len(rows):
            raise ValueError("Stored source history shape is invalid")
        current = _decode_catalog(current_rows[0])
        if not current.is_current:
            raise ValueError("Stored source history current pointer is invalid")
        records = tuple(_decode_catalog(row) for row in history_rows[:limit])
        return SourceGenerationPage(
            current=current,
            items=records,
            next_before_generation=(
                records[-1].generation if len(history_rows) > limit else None
            ),
        )

    async def get_mutation(self, idempotency_key: str) -> MutationReceipt | None:
        _validate_idempotency_key(idempotency_key)
        pool = await self._get_pool()
        async with pool.connection() as connection:
            receipt = await _read_mutation(connection, idempotency_key)
        return receipt

    async def list_mutations(
        self,
        source_id: str,
        *,
        before_event_id: int | None = None,
        limit: int = 50,
    ) -> MutationPage | None:
        _validate_page_limit(limit)
        _validate_event_cursor(before_event_id)
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "WITH selected_events AS MATERIALIZED ("
                "SELECT event_id FROM control.source_mutation_receipts "
                "WHERE source_id = %s "
                "AND (%s::bigint IS NULL OR event_id < %s) "
                "ORDER BY event_id DESC LIMIT %s"
                "), source_presence AS ("
                "SELECT EXISTS("
                "SELECT 1 FROM control.active_source_profiles WHERE source_id = %s"
                ") OR EXISTS("
                "SELECT 1 FROM control.source_mutation_receipts WHERE source_id = %s"
                ") AS source_present"
                ") SELECT source_presence.source_present, "
                + _MUTATION_PROJECTION
                + "FROM source_presence "
                "LEFT JOIN selected_events ON true "
                "LEFT JOIN control.source_mutation_receipts AS receipt "
                "ON receipt.event_id = selected_events.event_id "
                "ORDER BY receipt.event_id DESC NULLS LAST",
                (
                    source_id,
                    before_event_id,
                    before_event_id,
                    limit + 1,
                    source_id,
                    source_id,
                ),
            )
            rows = await cursor.fetchall()
        if len(rows) == 1 and rows[0].get("event_id") is None:
            return MutationPage((), None) if rows[0].get("source_present") is True else None
        if not rows or any(row.get("source_present") is not True for row in rows):
            raise ValueError("Stored source mutation history shape is invalid")
        receipts = tuple(_decode_mutation(row) for row in rows[:limit])
        return MutationPage(
            receipts,
            receipts[-1].event_id if len(rows) > limit else None,
        )

    async def record_mutation_rejection(
        self,
        mutation: MutationRequest,
        *,
        http_status: int,
        error_code: str,
    ) -> MutationReceipt:
        _validate_mutation_request(mutation)
        if http_status not in {400, 409} or _ERROR_CODE.fullmatch(error_code) is None:
            raise ValueError("Mutation rejection is invalid")
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_mutation(connection, mutation.idempotency_key)
            existing = await _read_mutation(connection, mutation.idempotency_key)
            if existing is not None:
                _require_same_mutation(existing, mutation)
                return existing
            receipt = await _insert_mutation_receipt(
                connection,
                mutation,
                outcome="rejected",
                resulting_generation=None,
                resulting_state_version=None,
                http_status=http_status,
                error_code=error_code,
                result={},
            )
        return receipt

    async def next_generation(self, source_id: str) -> int:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT coalesce(max(generation), 0) + 1 AS generation "
                "FROM control.source_profile_revisions WHERE source_id = %s",
                (source_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise StoredSourceNotFoundError
        return int(row["generation"])

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
        if generation <= 0:
            raise SourceGenerationConflictError
        _validate_mutation_arguments(
            mutation,
            mutation_result,
            source_id,
            expected_generation,
            expected_state_version,
            allowed_operations=("publish_source", "rotate_credential"),
        )
        if mutation_result is not None:
            _require_mutation_result_fields(
                mutation_result,
                generation=generation,
                metadata_revision=metadata.revision,
            )
        snapshot = encode_snapshot(metadata.snapshot)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _require_new_mutation(connection, mutation)
            await _lock_source_transition(connection, source_id)
            current_generation, current_state_version = await _lock_state(
                connection,
                source_id,
            )
            if (
                current_generation != expected_generation
                or current_state_version != expected_state_version
            ):
                raise SourceGenerationConflictError
            await connection.execute(
                "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (source_id, metadata.revision, Jsonb(snapshot)),
            )
            cursor = await connection.execute(
                "SELECT snapshot FROM control.metadata_snapshots "
                "WHERE source_id = %s AND revision = %s",
                (source_id, metadata.revision),
            )
            stored_snapshot = await cursor.fetchone()
            if stored_snapshot is None or stored_snapshot["snapshot"] != snapshot:
                raise SourceGenerationConflictError
            try:
                await connection.execute(
                    "INSERT INTO control.source_profile_revisions "
                    "(source_id, generation, manifest, secret_nonce, secret_ciphertext, "
                    "metadata_revision) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        source_id,
                        generation,
                        Jsonb(manifest),
                        encrypted_secret.nonce,
                        encrypted_secret.ciphertext,
                        metadata.revision,
                    ),
                )
            except errors.UniqueViolation as error:
                raise SourceGenerationConflictError from error
            cursor = await connection.execute(
                "INSERT INTO control.active_metadata_revisions "
                "(source_id, revision, pinned, activated_at) "
                "VALUES (%s, %s, false, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET revision = EXCLUDED.revision, activated_at = EXCLUDED.activated_at "
                "WHERE NOT control.active_metadata_revisions.pinned "
                "RETURNING revision",
                (source_id, metadata.revision),
            )
            if await cursor.fetchone() is None:
                raise SourcePublishPinnedError
            cursor = await connection.execute(
                "INSERT INTO control.active_source_profiles "
                "(source_id, generation, enabled, state_version, activated_at) "
                "VALUES (%s, %s, true, 1, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET generation = EXCLUDED.generation, enabled = true, "
                "state_version = control.active_source_profiles.state_version + 1, "
                "activated_at = EXCLUDED.activated_at "
                "RETURNING state_version",
                (source_id, generation),
            )
            state_row = await cursor.fetchone()
            if state_row is None:
                raise SourceGenerationConflictError
            state_version = int(state_row["state_version"])
            if mutation is not None and mutation_result is not None:
                await _insert_mutation_receipt(
                    connection,
                    mutation,
                    outcome="succeeded",
                    resulting_generation=generation,
                    resulting_state_version=state_version,
                    http_status=200,
                    error_code=None,
                    result=mutation_result,
                )
        return StoredSource(
            source_id,
            generation,
            manifest,
            encrypted_secret,
            metadata.revision,
            True,
            state_version,
        )

    async def deactivate(
        self,
        source_id: str,
        expected_generation: int,
        *,
        expected_state_version: int,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> int:
        _validate_mutation_arguments(
            mutation,
            mutation_result,
            source_id,
            expected_generation,
            expected_state_version,
            allowed_operations=("deactivate_source",),
        )
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _require_new_mutation(connection, mutation)
            await _lock_source_transition(connection, source_id)
            cursor = await connection.execute(
                "UPDATE control.active_source_profiles "
                "SET enabled = false, state_version = state_version + 1, "
                "activated_at = clock_timestamp() "
                "WHERE source_id = %s AND generation = %s "
                "AND state_version = %s AND enabled "
                "RETURNING state_version",
                (source_id, expected_generation, expected_state_version),
            )
            row = await cursor.fetchone()
            if row is None:
                raise SourceGenerationConflictError
            state_version = int(row["state_version"])
            if mutation is not None and mutation_result is not None:
                await _insert_mutation_receipt(
                    connection,
                    mutation,
                    outcome="succeeded",
                    resulting_generation=expected_generation,
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
        _validate_mutation_arguments(
            mutation,
            mutation_result,
            source_id,
            expected_generation,
            expected_state_version,
            allowed_operations=("rollback_source",),
        )
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _require_new_mutation(connection, mutation)
            await _lock_source_transition(connection, source_id)
            current_generation, current_state_version = await _lock_state(
                connection,
                source_id,
            )
            if (
                current_generation != expected_generation
                or current_state_version != expected_state_version
            ):
                raise SourceGenerationConflictError
            cursor = await connection.execute(
                "SELECT source_id, generation, true AS enabled, 0 AS state_version, "
                "manifest, secret_nonce, "
                "secret_ciphertext, metadata_revision "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s AND generation = %s FOR SHARE",
                (source_id, generation),
            )
            row = await cursor.fetchone()
            if row is None:
                raise StoredSourceNotFoundError
            value = _decode(row)
            if mutation_result is not None:
                _require_mutation_result_fields(
                    mutation_result,
                    generation=value.generation,
                    metadata_revision=value.metadata_revision,
                )
            cursor = await connection.execute(
                "UPDATE control.active_source_profiles "
                "SET generation = %s, enabled = true, state_version = state_version + 1, "
                "activated_at = clock_timestamp() "
                "WHERE source_id = %s AND state_version = %s "
                "RETURNING state_version",
                (generation, source_id, expected_state_version),
            )
            state_row = await cursor.fetchone()
            if state_row is None:
                raise SourceGenerationConflictError
            await connection.execute(
                "UPDATE control.active_metadata_revisions "
                "SET revision = %s, pinned = true, activated_at = clock_timestamp() "
                "WHERE source_id = %s",
                (value.metadata_revision, source_id),
            )
            state_version = int(state_row["state_version"])
            if mutation is not None and mutation_result is not None:
                await _insert_mutation_receipt(
                    connection,
                    mutation,
                    outcome="succeeded",
                    resulting_generation=value.generation,
                    resulting_state_version=state_version,
                    http_status=200,
                    error_code=None,
                    result=mutation_result,
                )
        return StoredSource(
            value.source_id,
            value.generation,
            value.manifest,
            value.encrypted_secret,
            value.metadata_revision,
            value.enabled,
            state_version,
        )

    async def publish_verified_query(
        self,
        query: VerifiedQuery,
        *,
        mutation: MutationRequest | None = None,
        mutation_result: dict[str, object] | None = None,
    ) -> None:
        if mutation is not None:
            _validate_mutation_arguments(
                mutation,
                mutation_result,
                query.source_id,
                mutation.expected_generation,
                mutation.expected_state_version,
                allowed_operations=("publish_verified_query",),
            )
            if mutation_result is None:
                raise ValueError("Mutation request and result must be provided together")
            _require_mutation_result_fields(
                mutation_result,
                query_id=query.query_id,
                metadata_revision=query.metadata_revision,
                row_count=query.expected.row_count,
                result_hash=query.expected.result_hash,
            )
        elif mutation_result is not None:
            raise ValueError("Mutation request and result must be provided together")
        document = {
            "columns": list(query.expected.columns),
            "row_count": query.expected.row_count,
            "result_hash": query.expected.result_hash,
        }
        relations = list(query.relations)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _require_new_mutation(connection, mutation)
            if mutation is not None:
                await _lock_source_transition(connection, query.source_id)
                generation, state_version = await _lock_state(connection, query.source_id)
                if (
                    generation != mutation.expected_generation
                    or state_version != mutation.expected_state_version
                ):
                    raise SourceGenerationConflictError
                cursor = await connection.execute(
                    "SELECT 1 FROM control.active_source_profiles AS source "
                    "JOIN control.active_metadata_revisions AS metadata "
                    "ON metadata.source_id = source.source_id "
                    "WHERE source.source_id = %s AND source.enabled "
                    "AND metadata.revision = %s",
                    (query.source_id, query.metadata_revision),
                )
                if await cursor.fetchone() is None:
                    raise SourceGenerationConflictError
            await connection.execute(
                "INSERT INTO control.verified_query_contracts "
                "(source_id, query_id, metadata_revision, question, relations, sql, expected) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    query.source_id,
                    query.query_id,
                    query.metadata_revision,
                    query.question,
                    Jsonb(relations),
                    query.sql,
                    Jsonb(document),
                ),
            )
            cursor = await connection.execute(
                "SELECT question, relations, sql, expected "
                "FROM control.verified_query_contracts "
                "WHERE source_id = %s AND query_id = %s AND metadata_revision = %s",
                (query.source_id, query.query_id, query.metadata_revision),
            )
            stored = await cursor.fetchone()
            if (
                stored is None
                or stored["question"] != query.question
                or stored["relations"] != relations
                or stored["sql"] != query.sql
                or stored["expected"] != document
            ):
                raise SourceGenerationConflictError
            if mutation is not None and mutation_result is not None:
                await _insert_mutation_receipt(
                    connection,
                    mutation,
                    outcome="succeeded",
                    resulting_generation=generation,
                    resulting_state_version=state_version,
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
        _validate_mutation_arguments(
            mutation,
            mutation_result,
            source_id,
            mutation.expected_generation,
            mutation.expected_state_version,
            allowed_operations=("resume_metadata_publish",),
        )
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _require_new_mutation(connection, mutation)
            await _lock_source_transition(connection, source_id)
            generation, state_version = await _lock_state(connection, source_id)
            if (
                generation != mutation.expected_generation
                or state_version != mutation.expected_state_version
            ):
                raise SourceGenerationConflictError
            cursor = await connection.execute(
                "UPDATE control.active_metadata_revisions AS metadata "
                "SET pinned = false "
                "FROM control.active_source_profiles AS source "
                "WHERE source.source_id = %s AND source.enabled "
                "AND source.generation = %s AND source.state_version = %s "
                "AND metadata.source_id = source.source_id "
                "AND metadata.revision = %s AND metadata.pinned "
                "RETURNING metadata.source_id",
                (
                    source_id,
                    mutation.expected_generation,
                    mutation.expected_state_version,
                    expected_metadata_revision,
                ),
            )
            if await cursor.fetchone() is None:
                raise SourceGenerationConflictError
            await _insert_mutation_receipt(
                connection,
                mutation,
                outcome="succeeded",
                resulting_generation=generation,
                resulting_state_version=state_version,
                http_status=200,
                error_code=None,
                result=mutation_result,
            )

    async def verified_revision_map(self) -> dict[str, frozenset[str]]:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT source_id, metadata_revision "
                "FROM control.verified_query_contracts "
                "GROUP BY source_id, metadata_revision"
            )
            rows = await cursor.fetchall()
        revisions: dict[str, set[str]] = {}
        for row in rows:
            revisions.setdefault(str(row["source_id"]), set()).add(
                str(row["metadata_revision"])
            )
        return {
            source_id: frozenset(source_revisions)
            for source_id, source_revisions in revisions.items()
        }

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _get_pool(self) -> AsyncConnectionPool[Any]:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    kwargs={
                        "application_name": "query-man-source-control",
                        "connect_timeout": 2,
                        "row_factory": dict_row,
                    },
                    min_size=0,
                    max_size=2,
                    timeout=2,
                    max_idle=10,
                    open=False,
                )
                await pool.open()
                self._pool = pool
        return self._pool


async def _lock_state(connection: Any, source_id: str) -> tuple[int, int]:
    cursor = await connection.execute(
        "SELECT generation, state_version FROM control.active_source_profiles "
        "WHERE source_id = %s FOR UPDATE",
        (source_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0, 0
    return int(row["generation"]), int(row["state_version"])


async def _lock_source_transition(connection: Any, source_id: str) -> None:
    await connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock("
        "pg_catalog.hashtextextended(%s, 0))",
        (source_id,),
    )


async def _lock_gateway_usage(connection: Any, source_id: str) -> None:
    await connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
        (f"query-man/gateway-usage/{source_id}",),
    )


async def _lock_resource_observation(connection: Any, source_id: str) -> None:
    await connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
        (f"query-man/resource-observation/{source_id}",),
    )


async def _require_resource_observation_generation(
    connection: Any,
    source_id: str,
    generation: int,
    *,
    metadata_revision: str | None = None,
) -> None:
    if metadata_revision is None:
        cursor = await connection.execute(
            "SELECT active.generation "
            "FROM control.active_source_profiles AS active "
            "JOIN control.source_profile_revisions AS revision "
            "ON revision.source_id = active.source_id "
            "AND revision.generation = active.generation "
            "WHERE active.source_id = %s AND active.enabled "
            "AND active.generation = %s "
            "AND jsonb_typeof(revision.manifest -> 'observability') = 'object' "
            "FOR SHARE OF active",
            (source_id, generation),
        )
    else:
        cursor = await connection.execute(
            "SELECT active.generation "
            "FROM control.active_source_profiles AS active "
            "JOIN control.source_profile_revisions AS revision "
            "ON revision.source_id = active.source_id "
            "AND revision.generation = active.generation "
            "JOIN control.active_metadata_revisions AS metadata "
            "ON metadata.source_id = active.source_id "
            "WHERE active.source_id = %s AND active.enabled "
            "AND active.generation = %s AND metadata.revision = %s "
            "AND jsonb_typeof(revision.manifest -> 'observability') = 'object' "
            "FOR SHARE OF active, metadata",
            (source_id, generation, metadata_revision),
        )
    if await cursor.fetchone() is None:
        raise ResourceObservationConflictError


async def _lock_gateway_reporter(connection: Any, replica_id: str) -> None:
    await connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(%s, 0))",
        (f"query-man/gateway-reporter/{replica_id}",),
    )


async def _lock_mutation(connection: Any, idempotency_key: str) -> None:
    await connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock("
        "pg_catalog.hashtextextended(%s, 0))",
        (f"query-man/mutation/{idempotency_key}",),
    )


async def _require_new_mutation(
    connection: Any,
    mutation: MutationRequest | None,
) -> None:
    if mutation is None:
        return
    await _lock_mutation(connection, mutation.idempotency_key)
    existing = await _read_mutation(connection, mutation.idempotency_key)
    if existing is not None:
        _require_same_mutation(existing, mutation)
        raise MutationReplay(existing)


async def _read_mutation(
    connection: Any,
    idempotency_key: str,
) -> MutationReceipt | None:
    cursor = await connection.execute(
        "SELECT "
        + _MUTATION_PROJECTION
        + "FROM control.source_mutation_receipts AS receipt "
        "WHERE receipt.idempotency_key = %s",
        (idempotency_key,),
    )
    row = await cursor.fetchone()
    return None if row is None else _decode_mutation(row)


async def _insert_mutation_receipt(
    connection: Any,
    mutation: MutationRequest,
    *,
    outcome: str,
    resulting_generation: int | None,
    resulting_state_version: int | None,
    http_status: int,
    error_code: str | None,
    result: dict[str, object],
) -> MutationReceipt:
    _validate_mutation_completion(
        mutation,
        outcome=outcome,
        resulting_generation=resulting_generation,
        resulting_state_version=resulting_state_version,
        http_status=http_status,
        error_code=error_code,
        result=result,
    )
    try:
        cursor = await connection.execute(
            "INSERT INTO control.source_mutation_receipts "
            "(idempotency_key, request_hash, operation, source_id, actor, reason, "
            "expected_generation, expected_state_version, resulting_generation, "
            "resulting_state_version, outcome, http_status, error_code, result) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING event_id",
            (
                mutation.idempotency_key,
                mutation.request_hash,
                mutation.operation,
                mutation.source_id,
                mutation.actor,
                mutation.reason,
                mutation.expected_generation,
                mutation.expected_state_version,
                resulting_generation,
                resulting_state_version,
                outcome,
                http_status,
                error_code,
                Jsonb(result),
            ),
        )
        if await cursor.fetchone() is None:
            raise ValueError("Stored source mutation receipt insert failed")
    except errors.UniqueViolation as error:
        raise MutationIdempotencyConflictError from error
    receipt = await _read_mutation(connection, mutation.idempotency_key)
    if receipt is None:
        raise ValueError("Stored source mutation receipt is unavailable")
    return receipt


def _require_same_mutation(
    receipt: MutationReceipt,
    mutation: MutationRequest,
) -> None:
    if (
        receipt.idempotency_key != mutation.idempotency_key
        or receipt.request_hash != mutation.request_hash
        or receipt.operation != mutation.operation
        or receipt.source_id != mutation.source_id
        or receipt.actor != mutation.actor
        or receipt.reason != mutation.reason
        or receipt.expected_generation != mutation.expected_generation
        or receipt.expected_state_version != mutation.expected_state_version
    ):
        raise MutationIdempotencyConflictError


def _validate_mutation_arguments(
    mutation: MutationRequest | None,
    result: dict[str, object] | None,
    source_id: str,
    expected_generation: int,
    expected_state_version: int,
    *,
    allowed_operations: tuple[str, ...],
) -> None:
    if mutation is None or result is None:
        if mutation is not None or result is not None:
            raise ValueError("Mutation request and result must be provided together")
        return
    _validate_mutation_request(mutation)
    if (
        mutation.operation not in allowed_operations
        or mutation.source_id != source_id
        or mutation.expected_generation != expected_generation
        or mutation.expected_state_version != expected_state_version
    ):
        raise ValueError("Mutation request does not match the source transition")
    _validate_success_result(mutation, result)


def _require_mutation_result_fields(
    result: dict[str, object],
    **expected: object,
) -> None:
    if any(result.get(field) != value for field, value in expected.items()):
        raise ValueError("Mutation result does not match the source transition")


def _validate_mutation_request(mutation: MutationRequest) -> None:
    _validate_idempotency_key(mutation.idempotency_key)
    if _REQUEST_HASH.fullmatch(mutation.request_hash) is None:
        raise ValueError("Mutation request hash is invalid")
    if mutation.operation not in _MUTATION_OPERATIONS:
        raise ValueError("Mutation operation is invalid")
    if _STABLE_SLUG.fullmatch(mutation.source_id) is None:
        raise ValueError("Mutation source_id is invalid")
    if _ACTOR.fullmatch(mutation.actor) is None:
        raise ValueError("Mutation actor is invalid")
    if _REASON.fullmatch(mutation.reason) is None:
        raise ValueError("Mutation reason is invalid")
    for value in (mutation.expected_generation, mutation.expected_state_version):
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= POSTGRES_BIGINT_MAX:
            raise ValueError("Mutation expected state is invalid")


def _validate_mutation_completion(
    mutation: MutationRequest,
    *,
    outcome: str,
    resulting_generation: int | None,
    resulting_state_version: int | None,
    http_status: int,
    error_code: str | None,
    result: dict[str, object],
) -> None:
    _validate_mutation_request(mutation)
    if outcome == "succeeded":
        if (
            http_status != 200
            or error_code is not None
            or resulting_generation is None
            or resulting_state_version is None
        ):
            raise ValueError("Mutation success receipt is invalid")
        for value in (resulting_generation, resulting_state_version):
            if isinstance(value, bool) or not 0 <= value <= POSTGRES_BIGINT_MAX:
                raise ValueError("Mutation resulting state is invalid")
        _validate_success_result(mutation, result)
        if mutation.operation in {
            "publish_source",
            "rotate_credential",
            "rollback_source",
        } and result.get("generation") != resulting_generation:
            raise ValueError("Mutation result does not match the resulting generation")
    elif outcome == "rejected":
        if (
            http_status not in {400, 409}
            or error_code is None
            or _ERROR_CODE.fullmatch(error_code) is None
            or resulting_generation is not None
            or resulting_state_version is not None
            or result
        ):
            raise ValueError("Mutation rejection receipt is invalid")
    else:
        raise ValueError("Mutation outcome is invalid")
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > 8192:
        raise ValueError("Mutation result is too large")


def _validate_success_result(
    mutation: MutationRequest,
    result: dict[str, object],
) -> None:
    fields = {
        "publish_source": {
            "status",
            "source_id",
            "generation",
            "metadata_revision",
            "quality_level",
        },
        "rotate_credential": {
            "status",
            "source_id",
            "generation",
            "metadata_revision",
            "quality_level",
        },
        "publish_verified_query": {
            "status",
            "query_id",
            "source_id",
            "metadata_revision",
            "row_count",
            "result_hash",
        },
        "rollback_source": {
            "status",
            "source_id",
            "generation",
            "metadata_revision",
        },
        "resume_metadata_publish": {"status", "source_id"},
        "deactivate_source": {"status", "source_id"},
    }[mutation.operation]
    if set(result) != fields or result.get("source_id") != mutation.source_id:
        raise ValueError("Mutation result projection is invalid")
    expected_status = {
        "publish_source": "published",
        "rotate_credential": "published",
        "publish_verified_query": "verified",
        "rollback_source": "rolled_back",
        "resume_metadata_publish": "resumed",
        "deactivate_source": "deactivated",
    }[mutation.operation]
    if result.get("status") != expected_status:
        raise ValueError("Mutation result status is invalid")
    if "generation" in result:
        generation = result["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise ValueError("Mutation result generation is invalid")
    for key in ("metadata_revision", "result_hash"):
        if key in result and not _is_revision(result[key]):
            raise ValueError("Mutation result revision is invalid")
    if "quality_level" in result and result["quality_level"] not in {"L0", "L1", "L2"}:
        raise ValueError("Mutation result quality level is invalid")
    if "query_id" in result:
        query_id = result["query_id"]
        if not isinstance(query_id, str) or re.fullmatch(r"^[a-z][a-z0-9-]{0,99}$", query_id) is None:
            raise ValueError("Mutation result query_id is invalid")
    if "row_count" in result:
        row_count = result["row_count"]
        if isinstance(row_count, bool) or not isinstance(row_count, int) or not 0 <= row_count <= 100_000:
            raise ValueError("Mutation result row count is invalid")


def _is_revision(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _validate_idempotency_key(idempotency_key: str) -> None:
    if not isinstance(idempotency_key, str) or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None:
        raise ValueError("Mutation idempotency key is invalid")
    try:
        parsed = uuid.UUID(idempotency_key)
    except ValueError as error:
        raise ValueError("Mutation idempotency key is invalid") from error
    if str(parsed) != idempotency_key:
        raise ValueError("Mutation idempotency key is invalid")


def _validate_event_cursor(before_event_id: int | None) -> None:
    if before_event_id is None:
        return
    if (
        isinstance(before_event_id, bool)
        or not isinstance(before_event_id, int)
        or not 1 <= before_event_id <= POSTGRES_BIGINT_MAX
    ):
        raise ValueError("Mutation event cursor must be a positive PostgreSQL bigint")


def _validate_replica_id(replica_id: str) -> None:
    if (
        not isinstance(replica_id, str)
        or not 1 <= len(replica_id) <= STABLE_SLUG_MAX_LENGTH
        or _STABLE_SLUG.fullmatch(replica_id) is None
    ):
        raise ValueError("Replica ID must be a stable slug")


def _validate_source_id(source_id: str) -> None:
    if (
        not isinstance(source_id, str)
        or not 1 <= len(source_id) <= STABLE_SLUG_MAX_LENGTH
        or _STABLE_SLUG.fullmatch(source_id) is None
    ):
        raise ValueError("Source ID must be a stable slug")


def _validate_heartbeat_interval(heartbeat_interval_ms: int) -> None:
    if (
        isinstance(heartbeat_interval_ms, bool)
        or not isinstance(heartbeat_interval_ms, int)
        or not _REPLICA_HEARTBEAT_INTERVAL_MIN_MS
        <= heartbeat_interval_ms
        <= _REPLICA_HEARTBEAT_INTERVAL_MAX_MS
    ):
        raise ValueError("Replica heartbeat interval is invalid")


def _validate_positive_bigint(value: int, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= POSTGRES_BIGINT_MAX
    ):
        raise ValueError(f"{label} must be a positive PostgreSQL bigint")


def _validate_non_negative_bigint(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= POSTGRES_BIGINT_MAX:
        raise ValueError(f"{label} must be a non-negative PostgreSQL bigint")


def _validate_resource_observation(observation: _ResourceObservationWrite) -> None:
    contract = _RESOURCE_METRIC_CONTRACT.get(observation.metric)
    if contract is None or contract != (observation.unit, observation.method):
        raise ValueError("Resource observation metric contract is invalid")
    _validate_non_negative_bigint(observation.value, "Resource observation value")
    if not _is_revision(observation.definition_revision):
        raise ValueError("Resource observation definition revision is invalid")


def _validate_gateway_usage_delta(delta: _GatewayUsageDeltaWrite) -> None:
    _validate_source_id(delta.source_id)
    if not isinstance(delta.budget_profile, str) or _IDENTIFIER.fullmatch(delta.budget_profile) is None:
        raise ValueError("Gateway usage budget profile is invalid")
    if not _is_revision(delta.metadata_revision):
        raise ValueError("Gateway usage metadata revision is invalid")
    if not _is_revision(delta.definition_revision):
        raise ValueError("Gateway usage definition revision is invalid")
    if not isinstance(delta.bucket_start, datetime) or delta.bucket_start.utcoffset() is None:
        raise ValueError("Gateway usage bucket must be timezone-aware")
    if delta.bucket_start != _utc_hour(delta.bucket_start):
        raise ValueError("Gateway usage bucket must be UTC-hour aligned")
    values = {
        "query count": delta.query_count,
        "success count": delta.success_count,
        "rejected count": delta.rejected_count,
        "timeout count": delta.timeout_count,
        "overloaded count": delta.overloaded_count,
        "cancelled count": delta.cancelled_count,
        "failed count": delta.failed_count,
        "queue milliseconds": delta.queue_ms_sum,
        "elapsed milliseconds": delta.elapsed_ms_sum,
        "returned rows": delta.returned_rows_sum,
        "result bytes": delta.result_bytes_sum,
        "truncated count": delta.truncated_count,
    }
    for label, value in values.items():
        _validate_non_negative_bigint(value, f"Gateway usage {label}")
    terminal_count = (
        delta.success_count
        + delta.rejected_count
        + delta.timeout_count
        + delta.overloaded_count
        + delta.cancelled_count
        + delta.failed_count
    )
    if delta.query_count != terminal_count:
        raise ValueError("Gateway usage terminal counts are inconsistent")
    if delta.truncated_count > delta.success_count:
        raise ValueError("Gateway usage truncated count is inconsistent")
    if delta.success_count == 0 and any(
        (
            delta.queue_ms_sum,
            delta.elapsed_ms_sum,
            delta.returned_rows_sum,
            delta.result_bytes_sum,
            delta.truncated_count,
        )
    ):
        raise ValueError("Gateway usage success-only sums are inconsistent")


def _utc_hour(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _gateway_usage_payload_hash(
    deltas: tuple[_GatewayUsageDeltaWrite, ...],
) -> str:
    payload = [
        {
            "source_id": delta.source_id,
            "budget_profile": delta.budget_profile,
            "metadata_revision": delta.metadata_revision,
            "definition_revision": delta.definition_revision,
            "bucket_start": delta.bucket_start.astimezone(UTC).isoformat(),
            "query_count": delta.query_count,
            "success_count": delta.success_count,
            "rejected_count": delta.rejected_count,
            "timeout_count": delta.timeout_count,
            "overloaded_count": delta.overloaded_count,
            "cancelled_count": delta.cancelled_count,
            "failed_count": delta.failed_count,
            "queue_ms_sum": delta.queue_ms_sum,
            "elapsed_ms_sum": delta.elapsed_ms_sum,
            "returned_rows_sum": delta.returned_rows_sum,
            "result_bytes_sum": delta.result_bytes_sum,
            "truncated_count": delta.truncated_count,
        }
        for delta in sorted(
            deltas,
            key=lambda item: (
                item.source_id,
                item.budget_profile,
                item.metadata_revision,
                item.definition_revision,
                item.bucket_start,
            ),
        )
    ]
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"


def _validate_replica_source_observation(
    observation: _ReplicaSourceObservationWrite,
) -> None:
    _validate_replica_id(observation.source_id)
    applied = (
        observation.applied_generation,
        observation.applied_state_version,
        observation.applied_enabled,
    )
    if all(value is None for value in applied):
        if observation.applied_metadata_revision is not None:
            raise ValueError("Unapplied replica source cannot have metadata")
    elif any(value is None for value in applied):
        raise ValueError("Replica applied source state is incomplete")
    else:
        assert observation.applied_generation is not None
        assert observation.applied_state_version is not None
        _validate_positive_bigint(
            observation.applied_generation,
            "Replica applied generation",
        )
        _validate_positive_bigint(
            observation.applied_state_version,
            "Replica applied state version",
        )
        if not isinstance(observation.applied_enabled, bool):
            raise ValueError("Replica applied enabled state is invalid")
        if (
            observation.applied_enabled is False
            and observation.applied_metadata_revision is not None
        ):
            raise ValueError("A disabled replica source cannot have applied metadata")
    if (
        observation.applied_metadata_revision is not None
        and not _is_revision(observation.applied_metadata_revision)
    ):
        raise ValueError("Replica applied metadata revision is invalid")
    if (
        observation.source_health is not None
        and observation.source_health not in _SOURCE_HEALTH_VALUES
    ):
        raise ValueError("Replica source health is invalid")
    if (
        observation.reason_code is not None
        and observation.reason_code not in _REPLICA_SOURCE_REASONS
    ):
        raise ValueError("Replica source reason is invalid")


def _decode_desired_replica_state(
    row: dict[str, Any],
) -> tuple[str, int, int, bool, str | None]:
    source_id = _stable_slug(row, "source_id")
    generation = _bounded_int(row, "desired_generation", 1, POSTGRES_BIGINT_MAX)
    state_version = _bounded_int(
        row,
        "desired_state_version",
        1,
        POSTGRES_BIGINT_MAX,
    )
    enabled = _required_bool(row, "desired_enabled")
    metadata_revision = _optional_revision(row, "desired_metadata_revision")
    if enabled and metadata_revision is None:
        raise ValueError("Stored desired metadata state is invalid")
    if not enabled and metadata_revision is not None:
        raise ValueError("Stored disabled desired metadata state is invalid")
    return source_id, generation, state_version, enabled, metadata_revision


def _decode_replica_observation(row: dict[str, Any]) -> ReplicaObservationRecord:
    report_reason_code = row.get("report_reason_code")
    if report_reason_code is not None and report_reason_code not in _REPLICA_REPORT_REASONS:
        raise ValueError("Stored replica report reason is invalid")
    source = _ReplicaSourceObservationWrite(
        source_id=_stable_slug(row, "source_id"),
        applied_generation=_optional_bounded_int(
            row,
            "applied_generation",
            1,
            POSTGRES_BIGINT_MAX,
        ),
        applied_state_version=_optional_bounded_int(
            row,
            "applied_state_version",
            1,
            POSTGRES_BIGINT_MAX,
        ),
        applied_enabled=_optional_bool(row, "applied_enabled"),
        applied_metadata_revision=_optional_revision(
            row,
            "applied_metadata_revision",
        ),
        source_health=_optional_text(row, "source_health", 13),
        reason_code=_optional_text(row, "reason_code", 64),
    )
    _validate_replica_source_observation(source)
    return ReplicaObservationRecord(
        replica_id=_stable_slug(row, "replica_id"),
        observed_at=_required_timestamp(row, "observed_at"),
        fresh_until=_required_timestamp(row, "fresh_until"),
        read_at=_required_timestamp(row, "read_at"),
        report_reason_code=report_reason_code,
        applied_generation=source.applied_generation,
        applied_state_version=source.applied_state_version,
        applied_enabled=source.applied_enabled,
        applied_metadata_revision=source.applied_metadata_revision,
        source_health=source.source_health,
        reason_code=source.reason_code,
    )


def _decode_resource_observability_configured(row: dict[str, Any]) -> bool:
    present = _required_bool(row, "resource_observability_present")
    observability_type = row.get("resource_observability_type")
    if not present:
        if observability_type is not None:
            raise RuntimeError("Source observability configuration is invalid")
        return False
    if observability_type != "object":
        raise RuntimeError("Source observability configuration is invalid")
    return True


def _decode_resource_observation_attempt(
    row: dict[str, Any],
) -> ResourceObservationAttemptRecord:
    generation = _bounded_int(row, "generation", 1, POSTGRES_BIGINT_MAX)
    last_attempt_at = _required_timestamp(row, "last_attempt_at")
    outcome = _required_text(row, "last_attempt_outcome", 16)
    reason_code = _optional_text(row, "last_attempt_reason_code", 64)
    last_success_at = _optional_timestamp(row, "last_success_at")
    has_representative = _optional_bool(
        row,
        "last_success_has_representative",
    )
    if outcome == "succeeded":
        if (
            reason_code is not None
            or last_success_at != last_attempt_at
            or has_representative is None
        ):
            raise RuntimeError("Resource observation attempt is invalid")
    elif outcome == "failed":
        if reason_code not in _RESOURCE_ATTEMPT_FAILURE_REASONS:
            raise RuntimeError("Resource observation attempt is invalid")
    else:
        raise RuntimeError("Resource observation attempt is invalid")
    if (last_success_at is None) != (has_representative is None):
        raise RuntimeError("Resource observation attempt is invalid")
    if last_success_at is not None and last_success_at > last_attempt_at:
        raise RuntimeError("Resource observation attempt is invalid")
    return ResourceObservationAttemptRecord(
        generation=generation,
        last_attempt_at=last_attempt_at,
        last_attempt_outcome=outcome,
        last_attempt_reason_code=reason_code,
        last_success_at=last_success_at,
        last_success_has_representative=has_representative,
    )


def _decode_resource_observation(
    row: dict[str, Any],
) -> ResourceObservationRecord:
    metric = _required_text(row, "metric", 64)
    unit = _required_text(row, "unit", 16)
    method = _required_text(row, "method", 64)
    if _RESOURCE_METRIC_CONTRACT.get(metric) != (unit, method):
        raise RuntimeError("Resource observation metric is invalid")
    current = ResourceObservationValueRecord(
        value=_bounded_int(row, "value", 0, POSTGRES_BIGINT_MAX),
        metadata_revision=_required_revision(row, "metadata_revision"),
        sample_bucket_start=_required_timestamp(row, "sample_bucket_start"),
        observed_at=_required_timestamp(row, "observed_at"),
        fresh_until=_required_timestamp(row, "fresh_until"),
    )
    if current.fresh_until != current.observed_at + _RESOURCE_FRESHNESS:
        raise RuntimeError("Resource observation freshness is invalid")

    previous_value = _optional_bounded_int(
        row,
        "previous_value",
        0,
        POSTGRES_BIGINT_MAX,
    )
    previous_revision = _optional_revision(row, "previous_metadata_revision")
    previous_bucket = _optional_timestamp(row, "previous_sample_bucket_start")
    previous_observed_at = _optional_timestamp(row, "previous_observed_at")
    previous_fresh_until = _optional_timestamp(row, "previous_fresh_until")
    previous_fields = (
        previous_value,
        previous_revision,
        previous_bucket,
        previous_observed_at,
        previous_fresh_until,
    )
    if all(value is None for value in previous_fields):
        previous = None
    elif any(value is None for value in previous_fields):
        raise RuntimeError("Previous resource observation is invalid")
    else:
        assert previous_value is not None
        assert previous_revision is not None
        assert previous_bucket is not None
        assert previous_observed_at is not None
        assert previous_fresh_until is not None
        if previous_fresh_until != previous_observed_at + _RESOURCE_FRESHNESS:
            raise RuntimeError("Previous resource observation freshness is invalid")
        previous = ResourceObservationValueRecord(
            value=previous_value,
            metadata_revision=previous_revision,
            sample_bucket_start=previous_bucket,
            observed_at=previous_observed_at,
            fresh_until=previous_fresh_until,
        )
    return ResourceObservationRecord(
        metric=metric,
        unit=unit,
        method=method,
        definition_revision=_required_revision(row, "definition_revision"),
        current=current,
        previous=previous,
    )


def _decode_gateway_usage_rollup(row: dict[str, Any]) -> GatewayUsageRollupRecord:
    delta = _GatewayUsageDeltaWrite(
        source_id=_stable_slug(row, "source_id"),
        budget_profile=_identifier(row, "budget_profile"),
        metadata_revision=_required_revision(row, "metadata_revision"),
        definition_revision=_required_revision(row, "definition_revision"),
        bucket_start=_required_timestamp(row, "bucket_start"),
        query_count=_bounded_int(row, "query_count", 0, POSTGRES_BIGINT_MAX),
        success_count=_bounded_int(row, "success_count", 0, POSTGRES_BIGINT_MAX),
        rejected_count=_bounded_int(row, "rejected_count", 0, POSTGRES_BIGINT_MAX),
        timeout_count=_bounded_int(row, "timeout_count", 0, POSTGRES_BIGINT_MAX),
        overloaded_count=_bounded_int(row, "overloaded_count", 0, POSTGRES_BIGINT_MAX),
        cancelled_count=_bounded_int(row, "cancelled_count", 0, POSTGRES_BIGINT_MAX),
        failed_count=_bounded_int(row, "failed_count", 0, POSTGRES_BIGINT_MAX),
        queue_ms_sum=_bounded_int(row, "queue_ms_sum", 0, POSTGRES_BIGINT_MAX),
        elapsed_ms_sum=_bounded_int(row, "elapsed_ms_sum", 0, POSTGRES_BIGINT_MAX),
        returned_rows_sum=_bounded_int(
            row,
            "returned_rows_sum",
            0,
            POSTGRES_BIGINT_MAX,
        ),
        result_bytes_sum=_bounded_int(
            row,
            "result_bytes_sum",
            0,
            POSTGRES_BIGINT_MAX,
        ),
        truncated_count=_bounded_int(
            row,
            "truncated_count",
            0,
            POSTGRES_BIGINT_MAX,
        ),
    )
    _validate_gateway_usage_delta(delta)
    return GatewayUsageRollupRecord(
        **vars(delta),
        observed_at=_required_timestamp(row, "observed_at"),
    )


def _projection_count(row: dict[str, Any], key: str) -> int:
    return _bounded_int(row, key, 0, POSTGRES_BIGINT_MAX)


def _decode(row: dict[str, Any]) -> StoredSource:
    manifest = row["manifest"]
    if not isinstance(manifest, dict):
        raise ValueError("Stored source manifest is invalid")
    return StoredSource(
        source_id=str(row["source_id"]),
        generation=int(row["generation"]),
        manifest=manifest,
        encrypted_secret=EncryptedSecret(
            bytes(row["secret_nonce"]),
            bytes(row["secret_ciphertext"]),
        ),
        metadata_revision=str(row["metadata_revision"]),
        enabled=bool(row["enabled"]),
        state_version=int(row["state_version"]),
    )


def _decode_mutation(row: dict[str, Any]) -> MutationReceipt:
    idempotency_key = str(row.get("idempotency_key"))
    _validate_idempotency_key(idempotency_key)
    request_hash = row.get("request_hash")
    operation = row.get("operation")
    source_id = row.get("source_id")
    actor = row.get("actor")
    reason = row.get("reason")
    if not isinstance(request_hash, str) or _REQUEST_HASH.fullmatch(request_hash) is None:
        raise ValueError("Stored source mutation request hash is invalid")
    if not isinstance(operation, str) or operation not in _MUTATION_OPERATIONS:
        raise ValueError("Stored source mutation operation is invalid")
    if not isinstance(source_id, str) or _STABLE_SLUG.fullmatch(source_id) is None:
        raise ValueError("Stored source mutation source_id is invalid")
    if not isinstance(actor, str) or _ACTOR.fullmatch(actor) is None:
        raise ValueError("Stored source mutation actor is invalid")
    if not isinstance(reason, str) or _REASON.fullmatch(reason) is None:
        raise ValueError("Stored source mutation reason is invalid")
    expected_generation = _mutation_int(row, "expected_generation", minimum=0)
    expected_state_version = _mutation_int(row, "expected_state_version", minimum=0)
    outcome = row.get("outcome")
    result = row.get("result")
    if not isinstance(result, dict):
        raise ValueError("Stored source mutation result is invalid")
    resulting_generation = _optional_mutation_int(row, "resulting_generation", minimum=0)
    resulting_state_version = _optional_mutation_int(
        row,
        "resulting_state_version",
        minimum=0,
    )
    http_status = _mutation_int(row, "http_status", minimum=100, maximum=599)
    error_code = row.get("error_code")
    if error_code is not None and (
        not isinstance(error_code, str) or _ERROR_CODE.fullmatch(error_code) is None
    ):
        raise ValueError("Stored source mutation error code is invalid")
    mutation = MutationRequest(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        operation=operation,
        source_id=source_id,
        actor=actor,
        reason=reason,
        expected_generation=expected_generation,
        expected_state_version=expected_state_version,
    )
    _validate_mutation_completion(
        mutation,
        outcome=str(outcome),
        resulting_generation=resulting_generation,
        resulting_state_version=resulting_state_version,
        http_status=http_status,
        error_code=error_code,
        result=result,
    )
    return MutationReceipt(
        event_id=_mutation_int(row, "event_id", minimum=1),
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        operation=operation,
        source_id=source_id,
        actor=actor,
        reason=reason,
        expected_generation=expected_generation,
        expected_state_version=expected_state_version,
        outcome=str(outcome),
        resulting_generation=resulting_generation,
        resulting_state_version=resulting_state_version,
        http_status=http_status,
        error_code=error_code,
        result=result,
        recorded_at=_mutation_timestamp(row, "recorded_at"),
    )


def _mutation_int(
    row: dict[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int = POSTGRES_BIGINT_MAX,
) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Stored source mutation {key} is invalid")
    return value


def _optional_mutation_int(
    row: dict[str, Any],
    key: str,
    *,
    minimum: int,
) -> int | None:
    if row.get(key) is None:
        return None
    return _mutation_int(row, key, minimum=minimum)


def _mutation_timestamp(row: dict[str, Any], key: str) -> datetime:
    value = row.get(key)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"Stored source mutation {key} is invalid")
    return value


def _decode_catalog(row: dict[str, Any]) -> SourceCatalogRecord:
    if _bounded_int(row, "manifest_version", 2, 2) != 2:
        raise ValueError("Stored source catalog manifest version is invalid")
    environment = _required_text(row, "environment", 11)
    if environment not in {"production", "staging", "development", "test"}:
        raise ValueError("Stored source catalog environment is invalid")
    minimum_quality_level = _required_text(row, "minimum_quality_level", 2)
    if minimum_quality_level not in {"L0", "L1", "L2"}:
        raise ValueError("Stored source catalog quality level is invalid")
    tenant_isolation = _required_text(row, "tenant_isolation", 4)
    if tenant_isolation not in {"none", "rls"}:
        raise ValueError("Stored source catalog tenant isolation is invalid")
    allowed_relation_kinds = _text_tuple(row, "allowed_relation_kinds", 1, 4, 30)
    if not set(allowed_relation_kinds) <= {
        "table",
        "partitioned_table",
        "view",
        "materialized_view",
    }:
        raise ValueError("Stored source catalog relation kinds are invalid")

    active_metadata_revision = _optional_revision(row, "active_metadata_revision")
    metadata_pinned = _optional_bool(row, "metadata_pinned")
    metadata_activated_at = _optional_timestamp(row, "metadata_activated_at")
    if active_metadata_revision is None:
        if metadata_pinned is not None or metadata_activated_at is not None:
            raise ValueError("Stored source catalog metadata state is invalid")
    elif metadata_pinned is None or metadata_activated_at is None:
        raise ValueError("Stored source catalog metadata state is invalid")

    return SourceCatalogRecord(
        source_id=_stable_slug(row, "source_id"),
        generation=_bounded_int(row, "generation", 1, POSTGRES_BIGINT_MAX),
        enabled=_required_bool(row, "enabled"),
        state_version=_bounded_int(row, "state_version", 1, POSTGRES_BIGINT_MAX),
        activated_at=_required_timestamp(row, "activated_at"),
        generation_created_at=_required_timestamp(row, "generation_created_at"),
        name=_required_text(row, "name", 200),
        description=_required_text(row, "description", 2_000),
        owner=_stable_slug(row, "owner"),
        environment=environment,
        database_migration_ref=_database_migration_ref(row),
        budget_profile=_identifier(row, "budget_profile"),
        minimum_quality_level=minimum_quality_level,
        tenant_isolation=tenant_isolation,
        connection=SourceCatalogConnection(
            host=_required_text(row, "connection_host", 253),
            port=_bounded_int(row, "connection_port", 1, 65_535),
            database=_identifier(row, "connection_database"),
            user=_identifier(row, "connection_user"),
            ssl=_required_bool(row, "connection_ssl"),
        ),
        allowed_schemas=_identifier_tuple(row, "allowed_schemas", 1, 20),
        allowed_relation_kinds=allowed_relation_kinds,
        semantic_default_relation=_optional_relation_name(
            row,
            "semantic_default_relation",
        ),
        semantic_relation_count=_bounded_int(row, "semantic_relation_count", 0, 200),
        semantic_join_count=_bounded_int(row, "semantic_join_count", 0, 500),
        semantic_business_term_count=_bounded_int(
            row,
            "semantic_business_term_count",
            0,
            200,
        ),
        semantic_question_rule_count=_bounded_int(
            row,
            "semantic_question_rule_count",
            0,
            200,
        ),
        semantic_composition_hint_count=_bounded_int(
            row,
            "semantic_composition_hint_count",
            0,
            200,
        ),
        published_metadata_revision=_required_revision(row, "published_metadata_revision"),
        active_metadata_revision=active_metadata_revision,
        metadata_pinned=metadata_pinned,
        metadata_activated_at=metadata_activated_at,
        is_current=_required_bool(row, "is_current"),
    )


def _validate_page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Catalog page limit must be between 1 and 100")


def _validate_before_generation(before_generation: int | None) -> None:
    if before_generation is None:
        return
    if (
        isinstance(before_generation, bool)
        or not isinstance(before_generation, int)
        or not 1 <= before_generation <= POSTGRES_BIGINT_MAX
    ):
        raise ValueError("Generation cursor must be a positive PostgreSQL bigint")


def _required_text(row: dict[str, Any], key: str, maximum_length: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= maximum_length or "\x00" in value:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_text(row: dict[str, Any], key: str, maximum_length: int) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    return _required_text(row, key, maximum_length)


def _identifier(row: dict[str, Any], key: str) -> str:
    value = _required_text(row, key, POSTGRES_IDENTIFIER_MAX_LENGTH)
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_relation_name(row: dict[str, Any], key: str) -> str | None:
    value = _optional_text(row, key, QUALIFIED_RELATION_MAX_LENGTH)
    if value is not None and _RELATION_NAME.fullmatch(value) is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _stable_slug(row: dict[str, Any], key: str) -> str:
    value = _required_text(row, key, STABLE_SLUG_MAX_LENGTH)
    if _STABLE_SLUG.fullmatch(value) is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _database_migration_ref(row: dict[str, Any]) -> str:
    value = _required_text(row, "database_migration_ref", 255)
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("Stored source catalog database_migration_ref is invalid")
    return value


def _text_tuple(
    row: dict[str, Any],
    key: str,
    minimum_items: int,
    maximum_items: int,
    maximum_item_length: int,
) -> tuple[str, ...]:
    value = row.get(key)
    if not isinstance(value, list) or not minimum_items <= len(value) <= maximum_items:
        raise ValueError(f"Stored source catalog {key} is invalid")
    items = tuple(value)
    if any(
        not isinstance(item, str)
        or not 1 <= len(item) <= maximum_item_length
        or "\x00" in item
        for item in items
    ):
        raise ValueError(f"Stored source catalog {key} is invalid")
    if len(set(items)) != len(items):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return items


def _identifier_tuple(
    row: dict[str, Any],
    key: str,
    minimum_items: int,
    maximum_items: int,
) -> tuple[str, ...]:
    items = _text_tuple(
        row,
        key,
        minimum_items,
        maximum_items,
        POSTGRES_IDENTIFIER_MAX_LENGTH,
    )
    if any(_IDENTIFIER.fullmatch(item) is None for item in items):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return items


def _bounded_int(row: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_bounded_int(
    row: dict[str, Any],
    key: str,
    minimum: int,
    maximum: int,
) -> int | None:
    if row.get(key) is None:
        return None
    return _bounded_int(row, key, minimum, maximum)


def _required_bool(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_bool(row: dict[str, Any], key: str) -> bool | None:
    if row.get(key) is None:
        return None
    return _required_bool(row, key)


def _required_timestamp(row: dict[str, Any], key: str) -> datetime:
    value = row.get(key)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_timestamp(row: dict[str, Any], key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    return _required_timestamp(row, key)


def _required_revision(row: dict[str, Any], key: str) -> str:
    value = _required_text(row, key, 71)
    digest = value.removeprefix("sha256:")
    if len(value) != 71 or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_revision(row: dict[str, Any], key: str) -> str | None:
    if row.get(key) is None:
        return None
    return _required_revision(row, key)
