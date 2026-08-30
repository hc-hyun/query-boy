from __future__ import annotations

import json
import logging
import sys

from query_man.runtime.operations import OperationalState, SafeJsonFormatter


class _UnrenderableLogValue:
    def __str__(self) -> str:
        raise AssertionError("database dependency log message must not be rendered")


def test_safe_json_formatter_redacts_secrets_literals_and_exception_details() -> None:
    formatter = SafeJsonFormatter()
    try:
        raise RuntimeError("database host 10.20.30.40 password=raw-secret")
    except RuntimeError:
        record = logging.LogRecord(
            "query_man.test",
            logging.ERROR,
            __file__,
            1,
            "request failed Authorization=Bearer abc.def password=raw-secret sql='private value'",
            (),
            exc_info=True,
        )
        record.exc_info = sys.exc_info()

    payload = json.loads(formatter.format(record))
    serialized = json.dumps(payload)
    assert payload["exception_type"] == "RuntimeError"
    assert "raw-secret" not in serialized
    assert "private value" not in serialized
    assert "10.20.30.40" not in serialized
    assert "abc.def" not in serialized


def test_safe_json_formatter_emits_bounded_audit_fields_as_top_level_json() -> None:
    formatter = SafeJsonFormatter()
    record = logging.LogRecord(
        "query_man.audit",
        logging.INFO,
        __file__,
        1,
        "query_succeeded query_id=%s",
        ("query-1",),
        exc_info=None,
    )
    record.query_id = "query-1"
    record.subject_id = "pseudonymous-subject-1"
    record.source_id = "development-issues"
    record.mcp_http_request_id = "mcp-http-request-1"
    record.mcp_call_id = "mcp-call-1"
    record.tool_name = "query"
    record.duration_ms = 13
    record.response_started_ms = 11
    record.response_bytes = 128
    record.status_code = 200
    record.outcome = "success"
    record.fingerprint = "pg_query:abc"
    record.elapsed_ms = 12
    record.plan_total_cost = 42.5

    payload = json.loads(formatter.format(record))

    assert payload["query_id"] == "query-1"
    assert payload["subject_id"] == "pseudonymous-subject-1"
    assert payload["source_id"] == "development-issues"
    assert payload["mcp_http_request_id"] == "mcp-http-request-1"
    assert payload["mcp_call_id"] == "mcp-call-1"
    assert payload["tool_name"] == "query"
    assert payload["duration_ms"] == 13
    assert payload["response_started_ms"] == 11
    assert payload["response_bytes"] == 128
    assert payload["status_code"] == 200
    assert payload["outcome"] == "success"
    assert payload["fingerprint"] == "pg_query:abc"
    assert payload["elapsed_ms"] == 12
    assert payload["plan_total_cost"] == 42.5


def test_safe_json_formatter_normalizes_database_dependency_logs_without_rendering() -> None:
    formatter = SafeJsonFormatter()
    driver_error = ConnectionError(
        "could not connect host=10.20.30.40 user=reader dbname=private_db"
    )
    record = logging.LogRecord(
        "psycopg.pool",
        logging.WARNING,
        __file__,
        1,
        "driver failure %s endpoint=postgres.internal:5432",
        (_UnrenderableLogValue(),),
        exc_info=(ConnectionError, driver_error, None),
    )
    record.source_id = "must-not-be-rendered"

    payload = json.loads(formatter.format(record))
    serialized = json.dumps(payload)

    assert payload == {
        "event": "database_dependency_log",
        "exception_type": "ConnectionError",
        "level": "warning",
        "logger": "psycopg.pool",
        "timestamp": payload["timestamp"],
    }
    for sensitive in (
        "10.20.30.40",
        "reader",
        "private_db",
        "postgres.internal",
        "driver failure",
        "must-not-be-rendered",
    ):
        assert sensitive not in serialized


def test_safe_json_formatter_normalizes_psycopg_pool_logger_prefix() -> None:
    formatter = SafeJsonFormatter()
    record = logging.LogRecord(
        "psycopg_pool.connection",
        logging.ERROR,
        __file__,
        1,
        "database connection failed",
        (),
        exc_info=None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "database_dependency_log"
    assert payload["logger"] == "psycopg_pool.connection"
    assert payload["level"] == "error"


def test_operational_state_hides_source_inventory_from_public_status() -> None:
    state = OperationalState()
    state.set_source_health("private-source", "unavailable")

    assert state.public_status() == "unavailable"
    assert "private-source" not in state.public_status()
    assert state.snapshot()["sources"] == {"private-source": "unavailable"}


def test_readiness_is_degraded_while_any_active_source_remains_usable() -> None:
    state = OperationalState()
    state.reconcile_sources(["healthy-source", "unavailable-source"])

    assert state.public_status() == "initializing"
    state.set_source_health("healthy-source", "healthy")
    state.set_source_query_health("healthy-source", "healthy")
    state.set_source_health("unavailable-source", "unavailable")
    assert state.public_status() == "degraded"

    state.set_component_health("metadata_catalog", "unavailable")
    state.set_source_health("unavailable-source", "stale")
    state.set_source_query_health("unavailable-source", "healthy")
    assert state.public_status() == "degraded"

    state.set_source_health("healthy-source", "unavailable")
    state.set_source_health("unavailable-source", "unavailable")
    assert state.public_status() == "unavailable"


def test_inventory_reconcile_removes_inactive_and_ignores_late_health_write() -> None:
    state = OperationalState()
    state.reconcile_sources(["active-source", "removed-source"])
    state.set_source_health("active-source", "healthy")
    state.set_source_query_health("active-source", "healthy")
    state.set_source_health("removed-source", "stale")
    state.set_source_query_health("removed-source", "healthy")
    state.increment("query_execution_started", "active-source")
    state.increment("query_execution_started", "removed-source")
    state.increment("catalog_refresh_failed")

    state.reconcile_sources(["active-source"])
    state.set_source_health("removed-source", "unavailable")
    state.set_source_query_health("removed-source", "unavailable")

    assert state.snapshot()["sources"] == {"active-source": "healthy"}
    assert state.public_status() == "ready"
    assert {
        (metric["name"], metric.get("source_id"))
        for metric in state.snapshot()["metrics"]
    } == {
        ("query_execution_started", "active-source"),
        ("catalog_refresh_failed", None),
    }


def test_source_health_keeps_metadata_and_query_failures_independent() -> None:
    state = OperationalState()
    state.reconcile_sources(["source"])

    state.set_source_health("source", "healthy")
    assert state.snapshot()["sources"] == {"source": "healthy"}
    assert state.public_status() == "ready"

    state.set_source_query_health("source", "unavailable")
    state.set_source_health("source", "healthy")
    assert state.snapshot()["sources"] == {"source": "unavailable"}
    assert state.public_status() == "unavailable"

    state.set_source_query_health("source", "healthy")
    state.set_source_health("source", "stale")
    assert state.snapshot()["sources"] == {"source": "stale"}
    assert state.public_status() == "degraded"


def test_query_health_reconcile_prunes_removed_sources_and_ignores_late_writes() -> None:
    state = OperationalState()
    state.reconcile_sources(["active-source", "removed-source"])
    for source_id in ("active-source", "removed-source"):
        state.set_source_health(source_id, "healthy")
        state.set_source_query_health(source_id, "healthy")

    state.reconcile_sources(["active-source"])
    state.set_source_query_health("removed-source", "unavailable")

    assert state.snapshot()["sources"] == {"active-source": "healthy"}
    assert state.public_status() == "ready"


def test_metric_snapshot_sorts_global_and_source_labels_together() -> None:
    state = OperationalState()
    state.increment("mcp_tool_completed")
    state.increment("mcp_tool_completed", "development-issues")
    state.observe("mcp_tool_duration_ms", 3)
    state.observe("mcp_tool_duration_ms", 7, "development-issues")

    assert state.snapshot()["metrics"] == [
        {"name": "mcp_tool_completed", "value": 1},
        {
            "name": "mcp_tool_completed",
            "source_id": "development-issues",
            "value": 1,
        },
        {"name": "mcp_tool_duration_ms_count", "value": 1},
        {
            "name": "mcp_tool_duration_ms_count",
            "source_id": "development-issues",
            "value": 1,
        },
        {"name": "mcp_tool_duration_ms_sum", "value": 3.0},
        {
            "name": "mcp_tool_duration_ms_sum",
            "source_id": "development-issues",
            "value": 7.0,
        },
    ]
