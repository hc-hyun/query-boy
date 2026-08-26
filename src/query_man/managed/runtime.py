from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import FastAPI

from query_man.access import AccessPolicy
from query_man.app import (
    _build_delivery_app,
    _probe_registered_sources,
    _require_launch_inventory,
    _require_runtime_capabilities,
)
from query_man.catalog import PostgresCatalog
from query_man.gateway import GatewayService
from query_man.managed.metadata_store import PostgresMetadataStore
from query_man.managed.secrets import SourceSecretCipher
from query_man.managed.source_admin import (
    REPLICA_HEARTBEAT_INTERVAL_MIN_MS,
    ControlGatewayUsageWriter,
    ControlReplicaObservationWriter,
    ControlResourceObservationWriter,
    GatewayUsageDelta,
    GatewayUsageWriter,
    ReplicaObservationWriter,
    ReplicaSourceObservation,
    ResourceObservationFailureReason,
    ResourceObservationSample,
    ResourceObservationWriter,
    SourceAdminService,
    SourcePoolInvalidator,
    SourceReloader,
)
from query_man.managed.source_admin_routes import register_source_admin_routes
from query_man.managed.source_store import PostgresSourceStore
from query_man.metadata import MetadataService
from query_man.models import ResourceObservation, RuntimeCatalogProvider, SourceProfile
from query_man.operations import operations
from query_man.query import (
    GatewayUsageOutcome,
    PostgresQueryExecutor,
    QueryService,
    RuntimeQueryExecutor,
)
from query_man.reader_policy import ReaderSessionPolicyError
from query_man.registry import SourceReader, SourceRegistry, load_budget_profiles
from query_man.runtime_config import RuntimeConfig

logger = logging.getLogger("query_man")
_GATEWAY_USAGE_REPORT_INTERVAL_SECONDS = 60.0
_RESOURCE_OBSERVATION_INTERVAL_SECONDS = 24 * 60 * 60.0
_GATEWAY_USAGE_MAX_GROUPS = 1_000
_GATEWAY_USAGE_MAX_REPORT_DELTAS = 100
_GATEWAY_USAGE_OUTCOMES = frozenset(
    {"success", "rejected", "timeout", "overloaded", "cancelled", "failed"}
)
_GATEWAY_USAGE_DEFINITION = {
    "bucket_start": "terminal_event_utc_hour",
    "query_count": "sum_terminal_counts",
    "terminal_counts": {
        "cancelled_count": ["operator", "disconnect", "shutdown"],
        "failed_count": ["unavailable", "unexpected"],
        "overloaded_count": ["queue", "pool"],
        "rejected_count": [
            "revision",
            "policy",
            "ast",
            "allowlist",
            "plan",
            "user_sql_invalid",
        ],
        "success_count": ["completed"],
        "timeout_count": ["statement", "transaction"],
    },
    "success_only_aggregates": [
        "queue_ms_sum",
        "elapsed_ms_sum",
        "returned_rows_sum",
        "result_bytes_sum",
        "truncated_count",
    ],
}
GATEWAY_USAGE_DEFINITION_REVISION = "sha256:" + hashlib.sha256(
    json.dumps(
        _GATEWAY_USAGE_DEFINITION,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class GatewayUsageReportSnapshot:
    snapshot_id: int
    deltas: tuple[GatewayUsageDelta, ...]


@dataclass(frozen=True)
class _GatewayUsageKey:
    source_id: str
    budget_profile: str
    metadata_revision: str
    definition_revision: str
    bucket_start: datetime


@dataclass
class _GatewayUsageAccumulator:
    query_count: int = 0
    success_count: int = 0
    rejected_count: int = 0
    timeout_count: int = 0
    overloaded_count: int = 0
    cancelled_count: int = 0
    failed_count: int = 0
    queue_ms_sum: int = 0
    elapsed_ms_sum: int = 0
    returned_rows_sum: int = 0
    result_bytes_sum: int = 0
    truncated_count: int = 0


class ManagedGatewayUsageRecorder:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._groups: dict[_GatewayUsageKey, _GatewayUsageAccumulator] = {}
        self._outstanding: GatewayUsageReportSnapshot | None = None
        self._next_snapshot_id = 1

    def reset(self) -> None:
        with self._lock:
            self._groups.clear()
            self._outstanding = None
            self._next_snapshot_id = 1

    def record_gateway_usage(
        self,
        *,
        source_id: str,
        budget_profile: str,
        metadata_revision: str,
        outcome: GatewayUsageOutcome,
        queue_ms: int = 0,
        elapsed_ms: int = 0,
        returned_rows: int = 0,
        result_bytes: int = 0,
        truncated: bool = False,
    ) -> None:
        if outcome not in _GATEWAY_USAGE_OUTCOMES:
            raise ValueError("Gateway usage outcome is invalid")
        if not source_id or not budget_profile or not metadata_revision:
            raise ValueError("Gateway usage attribution is incomplete")
        if outcome == "success":
            success_values = (queue_ms, elapsed_ms, returned_rows, result_bytes)
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in success_values
            ) or not isinstance(truncated, bool):
                raise ValueError("Gateway usage success values are invalid")
        else:
            queue_ms = 0
            elapsed_ms = 0
            returned_rows = 0
            result_bytes = 0
            truncated = False

        observed_at = self._clock()
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("Gateway usage clock must return an aware datetime")
        key = _GatewayUsageKey(
            source_id=source_id,
            budget_profile=budget_profile,
            metadata_revision=metadata_revision,
            definition_revision=GATEWAY_USAGE_DEFINITION_REVISION,
            bucket_start=observed_at.astimezone(UTC).replace(
                minute=0,
                second=0,
                microsecond=0,
            ),
        )
        with self._lock:
            accumulator = self._groups.get(key)
            if accumulator is None:
                if len(self._groups) >= _GATEWAY_USAGE_MAX_GROUPS:
                    protected = (
                        {self._key(delta) for delta in self._outstanding.deltas}
                        if self._outstanding is not None
                        else set()
                    )
                    oldest = min(
                        (
                            candidate
                            for candidate in self._groups
                            if candidate not in protected
                        ),
                        key=self._sort_key,
                        default=None,
                    )
                    if oldest is None:
                        return
                    self._groups.pop(oldest)
                accumulator = _GatewayUsageAccumulator()
                self._groups[key] = accumulator

            accumulator.query_count += 1
            if outcome == "success":
                accumulator.success_count += 1
                accumulator.queue_ms_sum += queue_ms
                accumulator.elapsed_ms_sum += elapsed_ms
                accumulator.returned_rows_sum += returned_rows
                accumulator.result_bytes_sum += result_bytes
                accumulator.truncated_count += int(truncated)
            elif outcome == "rejected":
                accumulator.rejected_count += 1
            elif outcome == "timeout":
                accumulator.timeout_count += 1
            elif outcome == "overloaded":
                accumulator.overloaded_count += 1
            elif outcome == "cancelled":
                accumulator.cancelled_count += 1
            else:
                accumulator.failed_count += 1

    def gateway_usage_report_snapshot(
        self,
        limit: int = _GATEWAY_USAGE_MAX_REPORT_DELTAS,
    ) -> GatewayUsageReportSnapshot | None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= _GATEWAY_USAGE_MAX_REPORT_DELTAS
        ):
            raise ValueError("Gateway usage report limit must be between 1 and 100")
        with self._lock:
            if self._outstanding is not None:
                return self._outstanding
            if not self._groups:
                return None
            keys = sorted(self._groups, key=self._sort_key)[:limit]
            snapshot = GatewayUsageReportSnapshot(
                snapshot_id=self._next_snapshot_id,
                deltas=tuple(self._delta(key, self._groups[key]) for key in keys),
            )
            self._next_snapshot_id += 1
            self._outstanding = snapshot
            return snapshot

    def ack_gateway_usage_report(self, snapshot_id: int) -> None:
        with self._lock:
            snapshot = self._outstanding
            if snapshot is None or snapshot.snapshot_id != snapshot_id:
                return
            for delta in snapshot.deltas:
                key = self._key(delta)
                accumulator = self._groups.get(key)
                if accumulator is None:
                    continue
                accumulator.query_count -= delta.query_count
                accumulator.success_count -= delta.success_count
                accumulator.rejected_count -= delta.rejected_count
                accumulator.timeout_count -= delta.timeout_count
                accumulator.overloaded_count -= delta.overloaded_count
                accumulator.cancelled_count -= delta.cancelled_count
                accumulator.failed_count -= delta.failed_count
                accumulator.queue_ms_sum -= delta.queue_ms_sum
                accumulator.elapsed_ms_sum -= delta.elapsed_ms_sum
                accumulator.returned_rows_sum -= delta.returned_rows_sum
                accumulator.result_bytes_sum -= delta.result_bytes_sum
                accumulator.truncated_count -= delta.truncated_count
                if accumulator.query_count == 0:
                    self._groups.pop(key)
            self._outstanding = None

    @staticmethod
    def _sort_key(key: _GatewayUsageKey) -> tuple[datetime, str, str, str, str]:
        return (
            key.bucket_start,
            key.source_id,
            key.budget_profile,
            key.metadata_revision,
            key.definition_revision,
        )

    @staticmethod
    def _key(delta: GatewayUsageDelta) -> _GatewayUsageKey:
        return _GatewayUsageKey(
            source_id=delta.source_id,
            budget_profile=delta.budget_profile,
            metadata_revision=delta.metadata_revision,
            definition_revision=delta.definition_revision,
            bucket_start=delta.bucket_start,
        )

    @staticmethod
    def _delta(
        key: _GatewayUsageKey,
        accumulator: _GatewayUsageAccumulator,
    ) -> GatewayUsageDelta:
        return GatewayUsageDelta(
            source_id=key.source_id,
            budget_profile=key.budget_profile,
            metadata_revision=key.metadata_revision,
            definition_revision=key.definition_revision,
            bucket_start=key.bucket_start,
            query_count=accumulator.query_count,
            success_count=accumulator.success_count,
            rejected_count=accumulator.rejected_count,
            timeout_count=accumulator.timeout_count,
            overloaded_count=accumulator.overloaded_count,
            cancelled_count=accumulator.cancelled_count,
            failed_count=accumulator.failed_count,
            queue_ms_sum=accumulator.queue_ms_sum,
            elapsed_ms_sum=accumulator.elapsed_ms_sum,
            returned_rows_sum=accumulator.returned_rows_sum,
            result_bytes_sum=accumulator.result_bytes_sum,
            truncated_count=accumulator.truncated_count,
        )


def build_app(
    runtime_config: RuntimeConfig,
    *,
    registry: SourceRegistry | None = None,
    catalog: RuntimeCatalogProvider | None = None,
    query_executor: RuntimeQueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
) -> FastAPI:
    if runtime_config.source_mode != "managed":
        raise ValueError("Managed runtime requires managed source mode")
    if registry is not None:
        raise ValueError("Managed source mode does not accept a bootstrap registry")
    operations.reset()
    registry = SourceRegistry([])
    _require_launch_inventory(registry)
    operations.reconcile_sources(registry.source_ids())
    catalog = PostgresCatalog(reject_domain_columns=False) if catalog is None else catalog
    _require_runtime_capabilities(
        "catalog",
        catalog,
        ("load", "close", "invalidate", "observe_resources"),
    )
    control_dsn = runtime_config.control_dsn
    encryption_key = runtime_config.source_encryption_key
    replica_id = runtime_config.replica_id
    if control_dsn is None or encryption_key is None or replica_id is None:
        raise ValueError("Managed source mode configuration is incomplete")
    verified_revisions: dict[str, frozenset[str]] = {}
    metadata_store = PostgresMetadataStore(control_dsn)
    metadata = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=runtime_config.metadata_cache_ttl_ms,
        max_stale_ms=runtime_config.metadata_max_stale_ms,
        refresh_retry_ms=runtime_config.metadata_retry_delay_ms,
        store=metadata_store,
        verified_revisions=verified_revisions,
    )
    query_executor = PostgresQueryExecutor() if query_executor is None else query_executor
    _require_runtime_capabilities(
        "query_executor",
        query_executor,
        ("execute", "cancel", "close", "stop_accepting", "drain", "invalidate"),
    )
    usage_recorder = ManagedGatewayUsageRecorder()
    query_service = QueryService(
        registry,
        metadata,
        query_executor,
        usage_recorder=usage_recorder,
    )
    if access_policy is None:
        if runtime_config.access_policy_file is not None:
            access_policy = AccessPolicy.load(runtime_config.access_policy_file)
        elif runtime_config.api_token is not None:
            access_policy = AccessPolicy.legacy(runtime_config.api_token)
        else:
            access_policy = AccessPolicy.local()
    access_policy.require_shared_access()
    gateway = GatewayService(registry, metadata, query_service)
    source_store = PostgresSourceStore(control_dsn)
    replica_writer = ControlReplicaObservationWriter(source_store)
    resource_writer = ControlResourceObservationWriter(source_store)
    gateway_writer = ControlGatewayUsageWriter(source_store)
    cipher = SourceSecretCipher.from_base64(encryption_key)
    invalidators: tuple[SourcePoolInvalidator, ...] = (catalog, query_executor)
    budgets = load_budget_profiles(runtime_config.budget_file)
    source_reloader = SourceReloader(
        registry,
        metadata,
        metadata_store,
        source_store,
        cipher,
        budgets,
        verified_revisions,
        invalidators,
    )
    source_admin = SourceAdminService(
        source_store,
        source_reloader,
        metadata,
        query_service,
        cipher,
        budgets,
        verified_revisions,
        PostgresCatalog,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        reload_task: asyncio.Task[None] | None = None

        async def cleanup_step(
            step: str,
            cleanup: Callable[[], Awaitable[None]],
        ) -> None:
            try:
                await cleanup()
            except BaseException:
                logger.warning("startup_cleanup_step_failed step=%s", step)

        async def cleanup_failed_startup() -> None:
            if reload_task is not None:
                task = reload_task

                async def cancel_reload_task() -> None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

                await cleanup_step("reload_task", cancel_reload_task)
            cleanup_steps: list[tuple[str, object, Callable[[], Awaitable[None]]]] = [
                ("query_executor", query_executor, query_executor.close),
                ("catalog", catalog, catalog.close),
                ("metadata", metadata, metadata.close),
                ("source_store", source_store, source_store.close),
            ]
            attempted_resources: set[int] = set()
            for step, resource, cleanup in cleanup_steps:
                resource_id = id(resource)
                if resource_id in attempted_resources:
                    continue
                attempted_resources.add(resource_id)
                await cleanup_step(step, cleanup)

        operations.set_component_health("source_reload", "initializing")
        await source_reloader.sync()
        operations.reconcile_sources(registry.source_ids())
        await _probe_registered_sources(registry, metadata)
        reload_task = asyncio.create_task(
            _reload_sources(
                source_reloader,
                runtime_config.source_reload_interval_ms,
                replica_writer,
                replica_id,
                registry=registry,
                catalog=catalog,
                metadata=metadata,
                resource_writer=resource_writer,
                gateway_writer=gateway_writer,
                usage_recorder=usage_recorder,
            )
        )
        child_entered = False
        try:
            mcp_app: FastAPI = app.state.mcp_app
            async with mcp_app.router.lifespan_context(mcp_app):
                child_entered = True
                try:
                    yield
                finally:
                    operations.set_accepting(False)
                    reload_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await reload_task
                    await query_executor.drain(runtime_config.shutdown_grace_ms)
                    try:
                        await query_executor.close()
                    finally:
                        try:
                            await catalog.close()
                        finally:
                            try:
                                await metadata.close()
                            finally:
                                await source_store.close()
        except BaseException:
            if not child_entered:
                await cleanup_failed_startup()
            raise

    return _build_delivery_app(
        runtime_config,
        registry=registry,
        catalog=catalog,
        metadata=metadata,
        query_executor=query_executor,
        query_service=query_service,
        access_policy=access_policy,
        gateway=gateway,
        lifespan=lifespan,
        route_registrar=register_source_admin_routes,
        extra_state={
            "source_admin": source_admin,
            "source_reloader": source_reloader,
            "gateway_usage_recorder": usage_recorder,
        },
    )


async def _reload_sources(
    reloader: SourceReloader,
    interval_ms: int,
    observation_writer: ReplicaObservationWriter | None = None,
    replica_id: str | None = None,
    *,
    registry: SourceReader | None = None,
    catalog: RuntimeCatalogProvider | None = None,
    metadata: MetadataService | None = None,
    resource_writer: ResourceObservationWriter | None = None,
    gateway_writer: GatewayUsageWriter | None = None,
    usage_recorder: ManagedGatewayUsageRecorder | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    reload_interval = interval_ms / 1_000
    heartbeat_interval_ms = max(interval_ms, REPLICA_HEARTBEAT_INTERVAL_MIN_MS)
    report_interval = heartbeat_interval_ms / 1_000
    incarnation: int | None = None
    resource_dependencies = (registry, catalog, metadata, resource_writer)
    if any(dependency is not None for dependency in resource_dependencies) and any(
        dependency is None for dependency in resource_dependencies
    ):
        raise ValueError("Resource observation reporting configuration is incomplete")

    if observation_writer is not None:
        if replica_id is None:
            raise ValueError("Replica ID is required for observation reporting")
        try:
            incarnation = await observation_writer.register_replica(
                replica_id,
                heartbeat_interval_ms,
            )
        except Exception:
            logger.exception("replica_observation_registration_failed")
        if incarnation is not None:
            await _report_replica_observation(
                observation_writer,
                replica_id,
                incarnation,
            )

    next_reload = loop.time() + reload_interval
    next_report = loop.time() + report_interval
    reporting_tasks: list[asyncio.Task[None]] = []
    # ponytail: process-local serialization preserves one slot in the fixed
    # two-connection Control source pool for authority and replica operations.
    observation_write_lock = asyncio.Lock()
    if (
        registry is not None
        and catalog is not None
        and metadata is not None
        and resource_writer is not None
    ):
        reporting_tasks.append(
            asyncio.create_task(
                _resource_observation_loop(
                    registry,
                    catalog,
                    metadata,
                    resource_writer,
                    reload_interval,
                    observation_write_lock,
                )
            )
        )
    if (
        gateway_writer is not None
        and usage_recorder is not None
        and incarnation is not None
    ):
        assert replica_id is not None
        reporting_tasks.append(
            asyncio.create_task(
                _gateway_usage_loop(
                    gateway_writer,
                    usage_recorder,
                    replica_id,
                    incarnation,
                    observation_write_lock,
                )
            )
        )

    try:
        while True:
            deadline = next_reload if incarnation is None else min(next_reload, next_report)
            await asyncio.sleep(max(0.0, deadline - loop.time()))
            if loop.time() >= next_reload:
                await reloader.sync()
                next_reload = loop.time() + reload_interval
            if incarnation is not None and loop.time() >= next_report:
                assert observation_writer is not None
                assert replica_id is not None
                await _report_replica_observation(
                    observation_writer,
                    replica_id,
                    incarnation,
                )
                next_report = loop.time() + report_interval
    finally:
        for task in reporting_tasks:
            task.cancel()
        if reporting_tasks:
            await asyncio.gather(*reporting_tasks, return_exceptions=True)


async def _report_replica_observation(
    writer: ReplicaObservationWriter,
    replica_id: str,
    incarnation: int,
) -> None:
    snapshot = operations.replica_runtime_snapshot()
    sources = (
        ()
        if snapshot.reason_code is not None
        else tuple(
            ReplicaSourceObservation(
                source_id=source.source_id,
                applied_generation=source.applied_generation,
                applied_state_version=source.applied_state_version,
                applied_enabled=source.applied_enabled,
                applied_metadata_revision=source.applied_metadata_revision,
                source_health=source.source_health,
                reason_code=source.reason_code,
            )
            for source in snapshot.sources
        )
    )
    try:
        await writer.report_replica(
            replica_id,
            incarnation,
            reason_code=snapshot.reason_code,
            sources=sources,
        )
    except Exception:
        logger.exception("replica_observation_report_failed")


def _registered_profiles(registry: SourceReader) -> dict[str, SourceProfile]:
    profiles: dict[str, SourceProfile] = {}
    for source_id in sorted(registry.source_ids()):
        source = registry.get(source_id)
        if source is not None:
            profiles[source_id] = source
    return profiles


async def _resource_observation_loop(
    registry: SourceReader,
    catalog: RuntimeCatalogProvider,
    metadata: MetadataService,
    writer: ResourceObservationWriter,
    poll_interval: float,
    write_lock: asyncio.Lock | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    tracked_profiles = _registered_profiles(registry)
    initial_profiles = tuple(
        source
        for source in tracked_profiles.values()
        if source.observability is not None
    )
    next_observation_by_source: dict[str, float] = {}
    if initial_profiles:
        await _collect_resource_observations(
            initial_profiles,
            catalog,
            metadata,
            writer,
            write_lock,
        )
        next_observation_by_source.update(
            {
                source.source_id: loop.time()
                + _RESOURCE_OBSERVATION_INTERVAL_SECONDS
                for source in initial_profiles
            }
        )
    while True:
        next_due = min(next_observation_by_source.values(), default=None)
        await asyncio.sleep(
            min(
                poll_interval,
                (
                    max(0.0, next_due - loop.time())
                    if next_due is not None
                    else poll_interval
                ),
            )
        )
        current_profiles = _registered_profiles(registry)
        now = loop.time()
        profiles = tuple(
            source
            for source_id, source in current_profiles.items()
            if source.observability is not None
            and (
                tracked_profiles.get(source_id) is not source
                or source_id not in next_observation_by_source
                or now >= next_observation_by_source[source_id]
            )
        )
        active_configured = {
            source_id
            for source_id, source in current_profiles.items()
            if source.observability is not None
        }
        next_observation_by_source = {
            source_id: deadline
            for source_id, deadline in next_observation_by_source.items()
            if source_id in active_configured
        }
        tracked_profiles = current_profiles
        if profiles:
            await _collect_resource_observations(
                profiles,
                catalog,
                metadata,
                writer,
                write_lock,
            )
            attempted_at = loop.time()
            next_observation_by_source.update(
                {
                    source.source_id: attempted_at
                    + _RESOURCE_OBSERVATION_INTERVAL_SECONDS
                    for source in profiles
                }
            )


async def _gateway_usage_loop(
    writer: GatewayUsageWriter,
    usage_recorder: ManagedGatewayUsageRecorder,
    replica_id: str,
    incarnation: int,
    write_lock: asyncio.Lock | None = None,
) -> None:
    loop = asyncio.get_running_loop()
    sequence = 1
    pending: tuple[int | None, tuple[GatewayUsageDelta, ...]] | None = None
    next_report = loop.time() + _GATEWAY_USAGE_REPORT_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(max(0.0, next_report - loop.time()))
        sequence, pending = await _report_gateway_usage(
            writer,
            usage_recorder,
            replica_id,
            incarnation,
            sequence,
            pending,
            write_lock,
        )
        while next_report <= loop.time():
            next_report += _GATEWAY_USAGE_REPORT_INTERVAL_SECONDS


def _definition_revision(material: dict[str, object]) -> str:
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _resource_observation_samples(
    source: SourceProfile,
    observation: ResourceObservation,
) -> tuple[ResourceObservationSample, ...]:
    definition = source.observability
    if definition is None:
        return ()
    migration_ref = source.provenance.database_migration_ref
    samples: list[ResourceObservationSample] = []
    if observation.representative_records is not None:
        representative = definition.representative_records
        samples.append(
            ResourceObservationSample(
                metric="representative_records",
                value=observation.representative_records,
                unit="rows",
                method="postgres_catalog_estimate",
                definition_revision=_definition_revision(
                    {
                        "database_migration_ref": migration_ref,
                        "grain": representative.grain,
                        "method": "postgres_catalog_estimate",
                        "metric": "representative_records",
                        "physical_relation": representative.physical_relation,
                    }
                ),
            )
        )
    relations = sorted(definition.storage_relations)
    samples.extend(
        (
            ResourceObservationSample(
                metric="table_bytes",
                value=observation.table_bytes,
                unit="bytes",
                method="postgres_relation_size",
                definition_revision=_definition_revision(
                    {
                        "database_migration_ref": migration_ref,
                        "method": "postgres_relation_size",
                        "metric": "table_bytes",
                        "relations": relations,
                    }
                ),
            ),
            ResourceObservationSample(
                metric="index_bytes",
                value=observation.index_bytes,
                unit="bytes",
                method="postgres_relation_size",
                definition_revision=_definition_revision(
                    {
                        "database_migration_ref": migration_ref,
                        "method": "postgres_relation_size",
                        "metric": "index_bytes",
                        "relations": relations,
                    }
                ),
            ),
            ResourceObservationSample(
                metric="total_storage_bytes",
                value=observation.total_storage_bytes,
                unit="bytes",
                method="postgres_relation_size",
                definition_revision=_definition_revision(
                    {
                        "database_migration_ref": migration_ref,
                        "method": "postgres_relation_size",
                        "metric": "total_storage_bytes",
                        "relations": relations,
                    }
                ),
            ),
        )
    )
    return tuple(samples)


async def _collect_resource_observations(
    sources: tuple[SourceProfile, ...],
    catalog: RuntimeCatalogProvider,
    metadata: MetadataService,
    writer: ResourceObservationWriter,
    write_lock: asyncio.Lock | None = None,
) -> None:
    for source in sources:
        if source.observability is None:
            continue
        generation = source.control_generation
        if generation is None:
            logger.warning(
                "resource_observation_report_failed",
                extra={"source_id": source.source_id},
            )
            continue
        try:
            with operations.suppress_source_health_updates():
                prepared = await metadata.get_published(source.source_id)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if isinstance(error.__cause__, ReaderSessionPolicyError):
                await _report_resource_observation_failure(
                    writer,
                    source.source_id,
                    generation,
                    "RESOURCE_READ_FAILED",
                    write_lock,
                )
            else:
                await _report_resource_observation_failure(
                    writer,
                    source.source_id,
                    generation,
                    "METADATA_UNAVAILABLE",
                    write_lock,
                )
            continue
        try:
            with operations.suppress_source_health_updates():
                observation = await catalog.observe_resources(source)
            samples = _resource_observation_samples(source, observation)
        except asyncio.CancelledError:
            raise
        except Exception:
            await _report_resource_observation_failure(
                writer,
                source.source_id,
                generation,
                "RESOURCE_READ_FAILED",
                write_lock,
            )
            continue
        try:
            if write_lock is None:
                await writer.report_resource_observations(
                    source.source_id,
                    generation,
                    prepared.revision,
                    samples,
                )
            else:
                async with write_lock:
                    await writer.report_resource_observations(
                        source.source_id,
                        generation,
                        prepared.revision,
                        samples,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "resource_observation_report_failed",
                extra={"source_id": source.source_id},
            )


async def _report_resource_observation_failure(
    writer: ResourceObservationWriter,
    source_id: str,
    generation: int,
    reason_code: ResourceObservationFailureReason,
    write_lock: asyncio.Lock | None,
) -> None:
    logger.warning(
        "resource_observation_failed",
        extra={"source_id": source_id, "reason_code": reason_code},
    )
    try:
        if write_lock is None:
            await writer.report_resource_observation_failure(
                source_id,
                generation,
                reason_code,
            )
        else:
            async with write_lock:
                await writer.report_resource_observation_failure(
                    source_id,
                    generation,
                    reason_code,
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "resource_observation_failure_report_failed",
            extra={"source_id": source_id, "reason_code": reason_code},
        )


async def _report_gateway_usage(
    writer: GatewayUsageWriter,
    usage_recorder: ManagedGatewayUsageRecorder,
    replica_id: str,
    incarnation: int,
    sequence: int,
    pending: tuple[int | None, tuple[GatewayUsageDelta, ...]] | None = None,
    write_lock: asyncio.Lock | None = None,
) -> tuple[int, tuple[int | None, tuple[GatewayUsageDelta, ...]] | None]:
    if pending is None:
        snapshot = usage_recorder.gateway_usage_report_snapshot(100)
        pending = (
            None,
            (),
        )
        if snapshot is not None:
            pending = (
                snapshot.snapshot_id,
                tuple(
                    GatewayUsageDelta(
                        source_id=delta.source_id,
                        budget_profile=delta.budget_profile,
                        metadata_revision=delta.metadata_revision,
                        definition_revision=delta.definition_revision,
                        bucket_start=delta.bucket_start,
                        query_count=delta.query_count,
                        success_count=delta.success_count,
                        rejected_count=delta.rejected_count,
                        timeout_count=delta.timeout_count,
                        overloaded_count=delta.overloaded_count,
                        cancelled_count=delta.cancelled_count,
                        failed_count=delta.failed_count,
                        queue_ms_sum=delta.queue_ms_sum,
                        elapsed_ms_sum=delta.elapsed_ms_sum,
                        returned_rows_sum=delta.returned_rows_sum,
                        result_bytes_sum=delta.result_bytes_sum,
                        truncated_count=delta.truncated_count,
                    )
                    for delta in snapshot.deltas
                ),
            )
    snapshot_id, deltas = pending
    try:
        if write_lock is None:
            await writer.report_gateway_usage(
                replica_id,
                incarnation,
                sequence,
                deltas,
            )
        else:
            async with write_lock:
                await writer.report_gateway_usage(
                    replica_id,
                    incarnation,
                    sequence,
                    deltas,
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("gateway_usage_report_failed")
        return sequence, pending
    if snapshot_id is not None:
        usage_recorder.ack_gateway_usage_report(snapshot_id)
    return sequence + 1, None
