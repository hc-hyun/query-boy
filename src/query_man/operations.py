from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|credential|token|secret)\s*[=:]\s*[^\s,;]+"
)
_SQL_LITERAL = re.compile(r"'(?:''|[^'])*'")


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
        self._accepting = True

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._totals.clear()
            self._source_health.clear()
            self._accepting = True

    def increment(self, name: str, source_id: str | None = None, value: int = 1) -> None:
        with self._lock:
            self._counters[(name, source_id)] += value

    def observe(self, name: str, value: float, source_id: str | None = None) -> None:
        with self._lock:
            self._counters[(f"{name}_count", source_id)] += 1
            self._totals[(f"{name}_sum", source_id)] += value

    def set_source_health(self, source_id: str, status: str) -> None:
        with self._lock:
            self._source_health[source_id] = status

    def set_accepting(self, accepting: bool) -> None:
        with self._lock:
            self._accepting = accepting

    def public_status(self) -> str:
        with self._lock:
            if not self._accepting:
                return "shutting_down"
            if any(status == "unavailable" for status in self._source_health.values()):
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
                for (name, source_id), value in sorted(self._counters.items())
            ]
            metrics.extend(
                {
                    "name": name,
                    **({"source_id": source_id} if source_id is not None else {}),
                    "value": value,
                }
                for (name, source_id), value in sorted(self._totals.items())
            )
            return {
                "accepting": self._accepting,
                "sources": dict(sorted(self._source_health.items())),
                "metrics": metrics,
            }


operations = OperationalState()
