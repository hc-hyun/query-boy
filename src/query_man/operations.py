from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

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
    "mcp_call_id",
    "tool_name",
    "protocol_version",
    "duration_ms",
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
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: defaultdict[tuple[str, str | None], int] = defaultdict(int)
        self._totals: defaultdict[tuple[str, str | None], float] = defaultdict(float)
        self._source_health: dict[str, str] = {}
        self._active_sources: set[str] | None = None
        self._component_health: dict[str, str] = {}
        self._accepting = True

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._totals.clear()
            self._source_health.clear()
            self._active_sources = None
            self._component_health.clear()
            self._accepting = True

    def increment(self, name: str, source_id: str | None = None, value: int = 1) -> None:
        with self._lock:
            self._counters[(name, source_id)] += value

    def observe(self, name: str, value: float, source_id: str | None = None) -> None:
        with self._lock:
            self._counters[(f"{name}_count", source_id)] += 1
            self._totals[(f"{name}_sum", source_id)] += value

    def set_source_health(self, source_id: str, status: str) -> None:
        if _source_health_updates_suppressed.get():
            return
        with self._lock:
            if self._active_sources is not None and source_id not in self._active_sources:
                return
            self._source_health[source_id] = status

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
