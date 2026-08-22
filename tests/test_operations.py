from __future__ import annotations

import json
import logging
import sys

from query_man.operations import OperationalState, SafeJsonFormatter


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
    state.set_source_health("unavailable-source", "unavailable")
    assert state.public_status() == "degraded"

    state.set_component_health("source_reload", "unavailable")
    state.set_source_health("unavailable-source", "stale")
    assert state.public_status() == "degraded"

    state.set_source_health("healthy-source", "unavailable")
    state.set_source_health("unavailable-source", "unavailable")
    assert state.public_status() == "unavailable"


def test_inventory_reconcile_removes_inactive_and_ignores_late_health_write() -> None:
    state = OperationalState()
    state.reconcile_sources(["active-source", "removed-source"])
    state.set_source_health("active-source", "healthy")
    state.set_source_health("removed-source", "stale")

    state.reconcile_sources(["active-source"])
    state.set_source_health("removed-source", "unavailable")

    assert state.snapshot()["sources"] == {"active-source": "healthy"}
    assert state.public_status() == "ready"


def test_staging_scope_does_not_mutate_production_source_health() -> None:
    state = OperationalState()
    state.reconcile_sources(["production-source"])
    state.set_source_health("production-source", "healthy")

    with state.suppress_source_health_updates():
        state.set_source_health("production-source", "unavailable")
        state.set_source_health("candidate-source", "healthy")

    assert state.snapshot()["sources"] == {"production-source": "healthy"}
