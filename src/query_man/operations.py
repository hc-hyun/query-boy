from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|credential|token|secret)\s*[=:]\s*[^\s,;]+"
)
_SQL_LITERAL = re.compile(r"'(?:''|[^'])*'")
_source_health_updates_suppressed: ContextVar[bool] = ContextVar(
    "query_man_source_health_updates_suppressed",
    default=False,
)
_STRUCTURED_LOG_FIELDS = (
    "mcp_http_request_id",
    "mcp_call_id",
    "tool_name",
    "protocol_version",
    "duration_ms",
    "response_started_ms",
    "response_bytes",
    "status_code",
    "outcome",
    "query_id",
    "caller_id",
    "tenant_id",
    "source_id",
    "fingerprint",
    "error_code",
    "reason_code",
    "cancel_reason",
    "queue_ms",
    "elapsed_ms",
    "row_count",
    "result_bytes",
    "truncated",
    "plan_total_cost",
    "plan_max_rows",
    "plan_node_count",
    "plan_cost_limit",
    "plan_rows_limit",
    "plan_nodes_limit",
)

ReplicaRuntimeReason = Literal["CONTROL_SCAN_FAILED"]
ReplicaSourceHealth = Literal["initializing", "healthy", "stale", "unavailable"]
ReplicaSourceReason = Literal[
    "RUNTIME_VALIDATION_REJECTED",
    "RUNTIME_APPLY_FAILED",
    "METADATA_PROBE_FAILED",
]
GatewayUsageOutcome = Literal[
    "success",
    "rejected",
    "timeout",
    "overloaded",
    "cancelled",
    "failed",
]
_REPLICA_SOURCE_HEALTH = frozenset(
    {"initializing", "healthy", "stale", "unavailable"}
)
_GATEWAY_USAGE_OUTCOMES = frozenset(
    {"success", "rejected", "timeout", "overloaded", "cancelled", "failed"}
)
_GATEWAY_USAGE_MAX_GROUPS = 1_000
_GATEWAY_USAGE_MAX_REPORT_DELTAS = 100
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
_REPLICA_SOURCE_REASONS = frozenset(
    {
        "RUNTIME_VALIDATION_REJECTED",
        "RUNTIME_APPLY_FAILED",
        "METADATA_PROBE_FAILED",
    }
)


@dataclass(frozen=True)
class ReplicaSourceRuntimeState:
    source_id: str
    applied_generation: int | None
    applied_state_version: int | None
    applied_enabled: bool | None
    applied_metadata_revision: str | None
    source_health: ReplicaSourceHealth | None
    reason_code: ReplicaSourceReason | None


@dataclass(frozen=True)
class ReplicaRuntimeSnapshot:
    reason_code: ReplicaRuntimeReason | None
    sources: tuple[ReplicaSourceRuntimeState, ...]


@dataclass(frozen=True)
class GatewayUsageDelta:
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


class SafeJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": redact(record.getMessage()),
        }
        if record.exc_info is not None and record.exc_info[0] is not None:
            payload["exception_type"] = record.exc_info[0].__name__
        for field in _STRUCTURED_LOG_FIELDS:
            if field not in record.__dict__:
                continue
            value = record.__dict__[field]
            if isinstance(value, str):
                payload[field] = redact(value)
            elif value is None or isinstance(value, bool | int | float):
                payload[field] = value
            else:
                payload[field] = redact(str(value))
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(SafeJsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


def redact(value: str) -> str:
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)
    return _SQL_LITERAL.sub("'[REDACTED]'", value)


class OperationalState:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._counters: defaultdict[tuple[str, str | None], int] = defaultdict(int)
        self._totals: defaultdict[tuple[str, str | None], float] = defaultdict(float)
        self._source_health: dict[str, str] = {}
        self._active_sources: set[str] | None = None
        self._component_health: dict[str, str] = {}
        self._accepting = True
        self._replica_scan_failed = False
        self._replica_sources: dict[str, ReplicaSourceRuntimeState] = {}
        self._gateway_usage_groups: dict[
            _GatewayUsageKey, _GatewayUsageAccumulator
        ] = {}
        self._gateway_usage_outstanding: GatewayUsageReportSnapshot | None = None
        self._gateway_usage_next_snapshot_id = 1

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._totals.clear()
            self._source_health.clear()
            self._active_sources = None
            self._component_health.clear()
            self._accepting = True
            self._replica_scan_failed = False
            self._replica_sources.clear()
            self._gateway_usage_groups.clear()
            self._gateway_usage_outstanding = None
            self._gateway_usage_next_snapshot_id = 1

    def increment(self, name: str, source_id: str | None = None, value: int = 1) -> None:
        with self._lock:
            self._counters[(name, source_id)] += value

    def observe(self, name: str, value: float, source_id: str | None = None) -> None:
        with self._lock:
            self._counters[(f"{name}_count", source_id)] += 1
            self._totals[(f"{name}_sum", source_id)] += value

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
        bucket_start = observed_at.astimezone(UTC).replace(
            minute=0,
            second=0,
            microsecond=0,
        )
        key = _GatewayUsageKey(
            source_id=source_id,
            budget_profile=budget_profile,
            metadata_revision=metadata_revision,
            definition_revision=GATEWAY_USAGE_DEFINITION_REVISION,
            bucket_start=bucket_start,
        )

        with self._lock:
            accumulator = self._gateway_usage_groups.get(key)
            if accumulator is None:
                if len(self._gateway_usage_groups) >= _GATEWAY_USAGE_MAX_GROUPS:
                    protected = (
                        {
                            self._gateway_usage_key(delta)
                            for delta in self._gateway_usage_outstanding.deltas
                        }
                        if self._gateway_usage_outstanding is not None
                        else set()
                    )
                    eviction_candidates = (
                        candidate
                        for candidate in self._gateway_usage_groups
                        if candidate not in protected
                    )
                    oldest = min(
                        eviction_candidates,
                        key=self._gateway_usage_sort_key,
                        default=None,
                    )
                    if oldest is None:
                        return
                    self._gateway_usage_groups.pop(oldest)
                accumulator = _GatewayUsageAccumulator()
                self._gateway_usage_groups[key] = accumulator

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
            if self._gateway_usage_outstanding is not None:
                return self._gateway_usage_outstanding
            if not self._gateway_usage_groups:
                return None
            keys = sorted(
                self._gateway_usage_groups,
                key=self._gateway_usage_sort_key,
            )[:limit]
            snapshot = GatewayUsageReportSnapshot(
                snapshot_id=self._gateway_usage_next_snapshot_id,
                deltas=tuple(
                    self._gateway_usage_delta(
                        key,
                        self._gateway_usage_groups[key],
                    )
                    for key in keys
                ),
            )
            self._gateway_usage_next_snapshot_id += 1
            self._gateway_usage_outstanding = snapshot
            return snapshot

    def ack_gateway_usage_report(self, snapshot_id: int) -> None:
        with self._lock:
            snapshot = self._gateway_usage_outstanding
            if snapshot is None or snapshot.snapshot_id != snapshot_id:
                return
            for delta in snapshot.deltas:
                key = self._gateway_usage_key(delta)
                accumulator = self._gateway_usage_groups.get(key)
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
                    self._gateway_usage_groups.pop(key)
            self._gateway_usage_outstanding = None

    @staticmethod
    def _gateway_usage_sort_key(
        key: _GatewayUsageKey,
    ) -> tuple[datetime, str, str, str, str]:
        return (
            key.bucket_start,
            key.source_id,
            key.budget_profile,
            key.metadata_revision,
            key.definition_revision,
        )

    @staticmethod
    def _gateway_usage_key(delta: GatewayUsageDelta) -> _GatewayUsageKey:
        return _GatewayUsageKey(
            source_id=delta.source_id,
            budget_profile=delta.budget_profile,
            metadata_revision=delta.metadata_revision,
            definition_revision=delta.definition_revision,
            bucket_start=delta.bucket_start,
        )

    @staticmethod
    def _gateway_usage_delta(
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

    def set_source_health(self, source_id: str, status: str) -> None:
        if _source_health_updates_suppressed.get():
            return
        with self._lock:
            if self._active_sources is not None and source_id not in self._active_sources:
                return
            self._source_health[source_id] = status
            replica_state = self._replica_sources.get(source_id)
            if (
                replica_state is not None
                and replica_state.applied_enabled is True
                and status in _REPLICA_SOURCE_HEALTH
            ):
                self._replica_sources[source_id] = replace(
                    replica_state,
                    source_health=cast(ReplicaSourceHealth, status),
                )

    def set_replica_scan_failed(self, failed: bool) -> None:
        with self._lock:
            self._replica_scan_failed = failed

    def set_replica_source_applied(
        self,
        source_id: str,
        generation: int,
        state_version: int,
        enabled: bool,
    ) -> None:
        with self._lock:
            self._replica_sources[source_id] = ReplicaSourceRuntimeState(
                source_id=source_id,
                applied_generation=generation,
                applied_state_version=state_version,
                applied_enabled=enabled,
                applied_metadata_revision=None,
                source_health=None,
                reason_code=None,
            )

    def set_replica_source_failure(
        self,
        source_id: str,
        reason_code: ReplicaSourceReason,
    ) -> None:
        if reason_code not in _REPLICA_SOURCE_REASONS:
            raise ValueError("Replica source reason is invalid")
        with self._lock:
            current = self._replica_sources.get(source_id)
            if current is None:
                current = ReplicaSourceRuntimeState(
                    source_id=source_id,
                    applied_generation=None,
                    applied_state_version=None,
                    applied_enabled=None,
                    applied_metadata_revision=None,
                    source_health=None,
                    reason_code=None,
                )
            self._replica_sources[source_id] = replace(
                current,
                reason_code=reason_code,
            )

    def clear_replica_source_apply_failure(self, source_id: str) -> None:
        with self._lock:
            current = self._replica_sources.get(source_id)
            if current is None or current.reason_code not in {
                "RUNTIME_VALIDATION_REJECTED",
                "RUNTIME_APPLY_FAILED",
            }:
                return
            self._replica_sources[source_id] = replace(current, reason_code=None)

    def set_replica_metadata_revision(
        self,
        source_id: str,
        revision: str | None,
    ) -> None:
        if _source_health_updates_suppressed.get():
            return
        with self._lock:
            current = self._replica_sources.get(source_id)
            if current is None or current.applied_enabled is not True:
                return
            reason_code = current.reason_code
            if revision is not None and reason_code == "METADATA_PROBE_FAILED":
                reason_code = None
            self._replica_sources[source_id] = replace(
                current,
                applied_metadata_revision=revision,
                reason_code=reason_code,
            )

    def replica_runtime_snapshot(self) -> ReplicaRuntimeSnapshot:
        with self._lock:
            reason_code: ReplicaRuntimeReason | None = (
                "CONTROL_SCAN_FAILED" if self._replica_scan_failed else None
            )
            return ReplicaRuntimeSnapshot(
                reason_code=reason_code,
                sources=tuple(
                    self._replica_sources[source_id]
                    for source_id in sorted(self._replica_sources)
                ),
            )

    def reconcile_sources(self, source_ids: Iterable[str]) -> None:
        active = set(source_ids)
        with self._lock:
            self._active_sources = active
            self._source_health = {
                source_id: self._source_health.get(source_id, "initializing")
                for source_id in active
            }
            self._counters = defaultdict(
                int,
                {
                    key: value
                    for key, value in self._counters.items()
                    if key[1] is None or key[1] in active
                },
            )
            self._totals = defaultdict(
                float,
                {
                    key: value
                    for key, value in self._totals.items()
                    if key[1] is None or key[1] in active
                },
            )

    def set_component_health(self, component: str, status: str) -> None:
        with self._lock:
            self._component_health[component] = status

    @contextmanager
    def suppress_source_health_updates(self) -> Iterator[None]:
        token = _source_health_updates_suppressed.set(True)
        try:
            yield
        finally:
            _source_health_updates_suppressed.reset(token)

    def set_accepting(self, accepting: bool) -> None:
        with self._lock:
            self._accepting = accepting

    def public_status(self) -> str:
        with self._lock:
            if not self._accepting:
                return "shutting_down"
            if not self._source_health:
                return "initializing" if self._active_sources is None else "unavailable"
            usable = any(
                status in {"healthy", "stale"}
                for status in self._source_health.values()
            )
            if not usable:
                if all(
                    status == "initializing"
                    for status in self._source_health.values()
                ):
                    return "initializing"
                return "unavailable"
            if (
                any(status != "healthy" for status in self._source_health.values())
                or any(status != "healthy" for status in self._component_health.values())
            ):
                return "degraded"
            return "ready"

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            metrics = [
                {
                    "name": name,
                    **({"source_id": source_id} if source_id is not None else {}),
                    "value": value,
                }
                for (name, source_id), value in sorted(
                    self._counters.items(),
                    key=lambda item: (item[0][0], item[0][1] or ""),
                )
            ]
            metrics.extend(
                {
                    "name": name,
                    **({"source_id": source_id} if source_id is not None else {}),
                    "value": value,
                }
                for (name, source_id), value in sorted(
                    self._totals.items(),
                    key=lambda item: (item[0][0], item[0][1] or ""),
                )
            )
            return {
                "accepting": self._accepting,
                "sources": dict(sorted(self._source_health.items())),
                "components": dict(sorted(self._component_health.items())),
                "metrics": metrics,
            }


operations = OperationalState()
