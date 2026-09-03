from __future__ import annotations

import json
import logging

import pytest

from query_man.runtime.operations import OperationalState, SafeJsonFormatter


class _Unrenderable:
    def __str__(self) -> str:
        raise AssertionError("database dependency messages must not be rendered")


def test_formatter_redacts_messages_and_keeps_safe_audit_fields() -> None:
    record = logging.LogRecord(
        "query_man.audit",
        logging.ERROR,
        __file__,
        1,
        "failed Bearer private-token password=private-password sql='private literal'",
        (),
        exc_info=(RuntimeError, RuntimeError("private database detail"), None),
    )
    record.query_id = "query-1"
    record.caller_id = "caller-1"
    record.tenant_id = "tenant-1"
    record.source_id = "source-1"
    record.elapsed_ms = 12
    record.plan_total_cost = 42.5

    payload = json.loads(SafeJsonFormatter().format(record))
    serialized = json.dumps(payload)

    assert payload["exception_type"] == "RuntimeError"
    assert payload["query_id"] == "query-1"
    assert payload["caller_id"] == "caller-1"
    assert payload["tenant_id"] == "tenant-1"
    assert payload["source_id"] == "source-1"
    assert payload["elapsed_ms"] == 12
    assert payload["plan_total_cost"] == 42.5
    for secret in ("private-token", "private-password", "private literal", "private database detail"):
        assert secret not in serialized


@pytest.mark.parametrize("logger_name", ["psycopg", "psycopg.pool", "psycopg_pool.connection"])
def test_formatter_never_renders_database_dependency_messages(
    logger_name: str,
) -> None:
    record = logging.LogRecord(
        logger_name,
        logging.WARNING,
        __file__,
        1,
        "driver failure %s",
        (_Unrenderable(),),
        exc_info=(ConnectionError, ConnectionError("private DSN"), None),
    )
    record.source_id = "private-source"

    payload = json.loads(SafeJsonFormatter().format(record))

    assert payload == {
        "event": "database_dependency_log",
        "exception_type": "ConnectionError",
        "level": "warning",
        "logger": logger_name,
        "timestamp": payload["timestamp"],
    }


def test_public_status_tracks_source_health_and_shutdown() -> None:
    state = OperationalState()
    assert state.public_status() == "initializing"

    state.reconcile_sources([])
    assert state.public_status() == "unavailable"

    state.reconcile_sources(["source"])
    assert state.public_status() == "initializing"
    state.set_source_health("source", "healthy")
    assert state.public_status() == "ready"
    state.set_source_health("source", "stale")
    assert state.public_status() == "degraded"
    state.set_source_query_health("source", "unavailable")
    assert state.public_status() == "unavailable"

    state.set_accepting(False)
    assert state.public_status() == "shutting_down"


def test_source_health_dimensions_remain_independent() -> None:
    state = OperationalState()
    state.reconcile_sources(["source"])
    state.set_source_health("source", "healthy")
    state.set_source_query_health("source", "unavailable")
    assert state.snapshot()["sources"] == {"source": "unavailable"}

    state.set_source_query_health("source", "healthy")
    state.set_source_health("source", "stale")
    assert state.snapshot()["sources"] == {"source": "stale"}

    state.set_source_health("source", "healthy")
    assert state.snapshot()["sources"] == {"source": "healthy"}


def test_reconcile_prunes_removed_source_state_and_metrics() -> None:
    state = OperationalState()
    state.reconcile_sources(["active", "removed"])
    for source_id in ("active", "removed"):
        state.set_source_health(source_id, "healthy")
        state.increment("query_execution_started", source_id)
    state.increment("metadata_refresh_failed")

    state.reconcile_sources(["active"])
    state.set_source_health("removed", "unavailable")
    state.increment("late_metric", "removed")

    snapshot = state.snapshot()
    assert snapshot["sources"] == {"active": "healthy"}
    assert {
        (metric["name"], metric.get("source_id"))
        for metric in snapshot["metrics"]
    } == {
        ("query_execution_started", "active"),
        ("metadata_refresh_failed", None),
        ("late_metric", "removed"),
    }


def test_metric_snapshot_is_sorted_and_aggregates_observations() -> None:
    state = OperationalState()
    state.reconcile_sources(["source"])
    state.increment("query_completed", "source", value=2)
    state.observe("query_elapsed_ms", 3, "source")
    state.observe("query_elapsed_ms", 7, "source")

    assert state.snapshot()["metrics"] == [
        {"name": "query_completed", "source_id": "source", "value": 2},
        {"name": "query_elapsed_ms_count", "source_id": "source", "value": 2},
        {"name": "query_elapsed_ms_sum", "source_id": "source", "value": 10.0},
    ]
