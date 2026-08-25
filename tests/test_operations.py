from __future__ import annotations

import json
import logging
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from query_man.operations import (
    GATEWAY_USAGE_DEFINITION_REVISION,
    GatewayUsageDelta,
    GatewayUsageReportSnapshot,
    OperationalState,
    ReplicaRuntimeSnapshot,
    ReplicaSourceRuntimeState,
    SafeJsonFormatter,
)


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
    state.increment("query_execution_started", "active-source")
    state.increment("query_execution_started", "removed-source")
    state.increment("source_reload_scan_failed")

    state.reconcile_sources(["active-source"])
    state.set_source_health("removed-source", "unavailable")

    assert state.snapshot()["sources"] == {"active-source": "healthy"}
    assert state.public_status() == "ready"
    assert {
        (metric["name"], metric.get("source_id"))
        for metric in state.snapshot()["metrics"]
    } == {
        ("query_execution_started", "active-source"),
        ("source_reload_scan_failed", None),
    }


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


def test_gateway_usage_groups_terminal_events_by_trusted_dimensions_and_utc_hour() -> None:
    current = [datetime(2026, 8, 25, 10, 45, tzinfo=timezone(timedelta(hours=9)))]
    state = OperationalState(clock=lambda: current[0])

    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-1",
        outcome="success",
        queue_ms=2,
        elapsed_ms=7,
        returned_rows=3,
        result_bytes=41,
        truncated=True,
    )
    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-1",
        outcome="rejected",
        queue_ms=999,
        elapsed_ms=999,
        returned_rows=999,
        result_bytes=999,
        truncated=True,
    )
    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="strict",
        metadata_revision="revision-1",
        outcome="timeout",
    )
    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-2",
        outcome="failed",
    )
    current[0] += timedelta(hours=1)
    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-1",
        outcome="overloaded",
    )

    snapshot = state.gateway_usage_report_snapshot()

    assert isinstance(snapshot, GatewayUsageReportSnapshot)
    assert GATEWAY_USAGE_DEFINITION_REVISION.startswith("sha256:")
    assert len(GATEWAY_USAGE_DEFINITION_REVISION) == 71
    assert snapshot.deltas == tuple(
        sorted(
            snapshot.deltas,
            key=lambda delta: (
                delta.bucket_start,
                delta.source_id,
                delta.budget_profile,
                delta.metadata_revision,
                delta.definition_revision,
            ),
        )
    )
    by_dimension = {
        (delta.budget_profile, delta.metadata_revision, delta.bucket_start): delta
        for delta in snapshot.deltas
    }
    first_bucket = datetime(2026, 8, 25, 1, tzinfo=UTC)
    combined = by_dimension[("standard", "revision-1", first_bucket)]
    assert combined == GatewayUsageDelta(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-1",
        definition_revision=GATEWAY_USAGE_DEFINITION_REVISION,
        bucket_start=first_bucket,
        query_count=2,
        success_count=1,
        rejected_count=1,
        timeout_count=0,
        overloaded_count=0,
        cancelled_count=0,
        failed_count=0,
        queue_ms_sum=2,
        elapsed_ms_sum=7,
        returned_rows_sum=3,
        result_bytes_sum=41,
        truncated_count=1,
    )
    assert by_dimension[("strict", "revision-1", first_bucket)].timeout_count == 1
    assert by_dimension[("standard", "revision-2", first_bucket)].failed_count == 1
    assert (
        by_dimension[("standard", "revision-1", first_bucket + timedelta(hours=1))]
        .overloaded_count
        == 1
    )
    with pytest.raises(FrozenInstanceError):
        combined.query_count = 3  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.deltas[0] = combined  # type: ignore[index]


def test_gateway_usage_snapshot_ack_is_retry_safe_and_preserves_new_events() -> None:
    observed_at = datetime(2026, 8, 25, 4, 30, tzinfo=UTC)
    state = OperationalState(clock=lambda: observed_at)
    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-1",
        outcome="success",
        queue_ms=1,
        elapsed_ms=2,
        returned_rows=3,
        result_bytes=4,
    )

    first = state.gateway_usage_report_snapshot()
    assert first is not None
    assert state.gateway_usage_report_snapshot() is first

    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-1",
        outcome="success",
        queue_ms=10,
        elapsed_ms=20,
        returned_rows=30,
        result_bytes=40,
        truncated=True,
    )
    state.record_gateway_usage(
        source_id="source-b",
        budget_profile="standard",
        metadata_revision="revision-1",
        outcome="failed",
    )
    state.ack_gateway_usage_report(first.snapshot_id + 1)
    assert state.gateway_usage_report_snapshot() is first

    state.ack_gateway_usage_report(first.snapshot_id)
    state.ack_gateway_usage_report(first.snapshot_id)
    remaining = state.gateway_usage_report_snapshot()

    assert remaining is not None
    assert remaining.snapshot_id > first.snapshot_id
    by_source = {delta.source_id: delta for delta in remaining.deltas}
    assert by_source["source-a"].query_count == 1
    assert by_source["source-a"].queue_ms_sum == 10
    assert by_source["source-a"].elapsed_ms_sum == 20
    assert by_source["source-a"].returned_rows_sum == 30
    assert by_source["source-a"].result_bytes_sum == 40
    assert by_source["source-a"].truncated_count == 1
    assert by_source["source-b"].failed_count == 1
    state.ack_gateway_usage_report(remaining.snapshot_id)
    assert state.gateway_usage_report_snapshot() is None


def test_gateway_usage_caps_batches_and_evicts_the_oldest_pending_group() -> None:
    current = [datetime(2026, 8, 24, 23, 30, tzinfo=UTC)]
    state = OperationalState(clock=lambda: current[0])
    state.record_gateway_usage(
        source_id="oldest-source",
        budget_profile="standard",
        metadata_revision="revision-oldest",
        outcome="failed",
    )
    current[0] += timedelta(hours=1)
    for index in range(1_000):
        state.record_gateway_usage(
            source_id=f"source-{index:04d}",
            budget_profile="standard",
            metadata_revision="revision-1",
            outcome="failed",
        )

    with pytest.raises(ValueError, match="between 1 and 100"):
        state.gateway_usage_report_snapshot(101)
    deltas: list[GatewayUsageDelta] = []
    while (snapshot := state.gateway_usage_report_snapshot()) is not None:
        assert len(snapshot.deltas) <= 100
        deltas.extend(snapshot.deltas)
        state.ack_gateway_usage_report(snapshot.snapshot_id)

    assert len(deltas) == 1_000
    assert "oldest-source" not in {delta.source_id for delta in deltas}
    assert sum(delta.query_count for delta in deltas) == 1_000


def test_gateway_usage_reset_clears_pending_without_changing_public_metrics() -> None:
    state = OperationalState(clock=lambda: datetime(2026, 8, 25, tzinfo=UTC))
    state.increment("query_execution_started", "source-a")
    public_before = state.snapshot()
    state.record_gateway_usage(
        source_id="source-a",
        budget_profile="standard",
        metadata_revision="revision-1",
        outcome="cancelled",
    )
    outstanding = state.gateway_usage_report_snapshot()
    assert outstanding is not None
    assert state.snapshot() == public_before

    state.reset()
    assert state.gateway_usage_report_snapshot() is None
    assert state.snapshot() == {
        "accepting": True,
        "sources": {},
        "components": {},
        "metrics": [],
    }

    state.record_gateway_usage(
        source_id="source-b",
        budget_profile="standard",
        metadata_revision="revision-2",
        outcome="failed",
    )
    after_reset = state.gateway_usage_report_snapshot()
    assert after_reset is not None
    assert after_reset.snapshot_id == 1


def test_staging_scope_does_not_mutate_production_source_health() -> None:
    state = OperationalState()
    state.reconcile_sources(["production-source"])
    state.set_source_health("production-source", "healthy")

    with state.suppress_source_health_updates():
        state.set_source_health("production-source", "unavailable")
        state.set_source_health("candidate-source", "healthy")

    assert state.snapshot()["sources"] == {"production-source": "healthy"}


def test_replica_runtime_snapshot_is_frozen_sorted_and_resettable() -> None:
    state = OperationalState()
    state.set_replica_source_applied("z-source", 2, 3, True)
    state.set_replica_source_applied("a-source", 4, 5, False)
    state.set_replica_scan_failed(True)

    snapshot = state.replica_runtime_snapshot()

    assert isinstance(snapshot, ReplicaRuntimeSnapshot)
    assert snapshot.reason_code == "CONTROL_SCAN_FAILED"
    assert [source.source_id for source in snapshot.sources] == ["a-source", "z-source"]
    assert all(isinstance(source, ReplicaSourceRuntimeState) for source in snapshot.sources)
    with pytest.raises(FrozenInstanceError):
        snapshot.reason_code = None  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        snapshot.sources[0].source_health = "healthy"  # type: ignore[misc]

    state.set_replica_source_failure("a-source", "RUNTIME_APPLY_FAILED")
    assert snapshot.sources[0].reason_code is None

    state.reset()
    assert state.replica_runtime_snapshot() == ReplicaRuntimeSnapshot(
        reason_code=None,
        sources=(),
    )


def test_replica_runtime_state_does_not_change_public_snapshot() -> None:
    state = OperationalState()
    state.set_replica_source_applied("private-source", 1, 2, True)
    state.set_replica_source_failure("private-source", "RUNTIME_APPLY_FAILED")
    state.set_replica_scan_failed(True)

    assert state.snapshot() == {
        "accepting": True,
        "sources": {},
        "components": {},
        "metrics": [],
    }
    assert state.public_status() == "initializing"


def test_replica_source_apply_atomically_resets_observed_runtime_fields() -> None:
    state = OperationalState()
    state.set_replica_source_applied("source-a", 1, 2, True)
    state.set_replica_metadata_revision("source-a", "revision-1")
    state.set_source_health("source-a", "healthy")
    state.set_replica_source_failure("source-a", "RUNTIME_APPLY_FAILED")

    state.set_replica_source_applied("source-a", 3, 4, True)
    assert state.replica_runtime_snapshot().sources == (
        ReplicaSourceRuntimeState(
            source_id="source-a",
            applied_generation=3,
            applied_state_version=4,
            applied_enabled=True,
            applied_metadata_revision=None,
            source_health=None,
            reason_code=None,
        ),
    )

    state.set_replica_metadata_revision("source-a", "revision-2")
    state.set_source_health("source-a", "stale")
    state.set_replica_source_applied("source-a", 5, 6, False)
    assert state.replica_runtime_snapshot().sources == (
        ReplicaSourceRuntimeState(
            source_id="source-a",
            applied_generation=5,
            applied_state_version=6,
            applied_enabled=False,
            applied_metadata_revision=None,
            source_health=None,
            reason_code=None,
        ),
    )


def test_replica_source_failure_is_bounded_and_preserves_applied_state() -> None:
    state = OperationalState()
    state.set_replica_source_applied("source-a", 7, 8, True)
    state.set_replica_source_failure("source-a", "RUNTIME_VALIDATION_REJECTED")

    observed = state.replica_runtime_snapshot().sources[0]
    assert observed.applied_generation == 7
    assert observed.applied_state_version == 8
    assert observed.reason_code == "RUNTIME_VALIDATION_REJECTED"

    with pytest.raises(ValueError, match="reason is invalid"):
        state.set_replica_source_failure("source-a", "UNBOUNDED_REASON")  # type: ignore[arg-type]
    assert state.replica_runtime_snapshot().sources[0] == observed

    state.set_replica_source_failure("not-applied", "RUNTIME_APPLY_FAILED")
    pending = state.replica_runtime_snapshot().sources[0]
    assert pending.source_id == "not-applied"
    assert pending.applied_generation is None
    assert pending.reason_code == "RUNTIME_APPLY_FAILED"


@pytest.mark.parametrize(
    "reason_code",
    ["RUNTIME_VALIDATION_REJECTED", "RUNTIME_APPLY_FAILED"],
)
def test_clear_replica_source_apply_failure_preserves_applied_observations(
    reason_code: str,
) -> None:
    state = OperationalState()
    state.set_replica_source_applied("source-a", 7, 8, True)
    state.set_replica_metadata_revision("source-a", "revision-1")
    state.set_source_health("source-a", "healthy")
    state.set_replica_source_failure("source-a", reason_code)  # type: ignore[arg-type]

    before = state.replica_runtime_snapshot().sources[0]
    state.clear_replica_source_apply_failure("source-a")
    after = state.replica_runtime_snapshot().sources[0]

    assert after == ReplicaSourceRuntimeState(
        source_id=before.source_id,
        applied_generation=before.applied_generation,
        applied_state_version=before.applied_state_version,
        applied_enabled=before.applied_enabled,
        applied_metadata_revision=before.applied_metadata_revision,
        source_health=before.source_health,
        reason_code=None,
    )


def test_clear_replica_source_apply_failure_preserves_metadata_probe_failure() -> None:
    state = OperationalState()
    state.set_replica_source_applied("source-a", 1, 1, True)
    state.set_replica_source_failure("source-a", "METADATA_PROBE_FAILED")

    state.clear_replica_source_apply_failure("source-a")
    state.clear_replica_source_apply_failure("not-observed")

    assert state.replica_runtime_snapshot().sources[0].reason_code == "METADATA_PROBE_FAILED"


def test_replica_scan_failure_reason_clears_without_dropping_source_state() -> None:
    state = OperationalState()
    state.set_replica_source_applied("source-a", 1, 1, True)

    state.set_replica_scan_failed(True)
    failed = state.replica_runtime_snapshot()
    assert failed.reason_code == "CONTROL_SCAN_FAILED"
    assert [source.source_id for source in failed.sources] == ["source-a"]

    state.set_replica_scan_failed(False)
    recovered = state.replica_runtime_snapshot()
    assert recovered.reason_code is None
    assert recovered.sources == failed.sources


def test_replica_metadata_success_clears_only_metadata_probe_failure() -> None:
    state = OperationalState()
    state.set_replica_source_applied("source-a", 1, 1, True)
    state.set_replica_source_failure("source-a", "METADATA_PROBE_FAILED")

    state.set_replica_metadata_revision("source-a", None)
    assert state.replica_runtime_snapshot().sources[0].reason_code == "METADATA_PROBE_FAILED"

    state.set_replica_metadata_revision("source-a", "revision-1")
    observed = state.replica_runtime_snapshot().sources[0]
    assert observed.applied_metadata_revision == "revision-1"
    assert observed.reason_code is None

    state.set_replica_source_failure("source-a", "RUNTIME_APPLY_FAILED")
    state.set_replica_metadata_revision("source-a", "revision-2")
    observed = state.replica_runtime_snapshot().sources[0]
    assert observed.applied_metadata_revision == "revision-2"
    assert observed.reason_code == "RUNTIME_APPLY_FAILED"


def test_staging_scope_suppresses_replica_metadata_and_health_updates() -> None:
    state = OperationalState()
    state.reconcile_sources(["source-a"])
    state.set_replica_source_applied("source-a", 1, 1, True)
    state.set_replica_metadata_revision("source-a", "production-revision")
    state.set_source_health("source-a", "healthy")

    with state.suppress_source_health_updates():
        state.set_replica_metadata_revision("source-a", "candidate-revision")
        state.set_source_health("source-a", "unavailable")

    observed = state.replica_runtime_snapshot().sources[0]
    assert observed.applied_metadata_revision == "production-revision"
    assert observed.source_health == "healthy"
    assert state.snapshot()["sources"] == {"source-a": "healthy"}


def test_source_health_syncs_only_existing_enabled_replica_state() -> None:
    state = OperationalState()
    state.set_source_health("not-applied", "healthy")
    state.set_replica_source_applied("disabled", 1, 1, False)
    state.set_source_health("disabled", "healthy")
    state.set_replica_source_applied("enabled", 2, 2, True)
    state.set_source_health("enabled", "stale")

    observed = {
        source.source_id: source
        for source in state.replica_runtime_snapshot().sources
    }
    assert "not-applied" not in observed
    assert observed["disabled"].source_health is None
    assert observed["enabled"].source_health == "stale"


def test_reconcile_sources_does_not_remove_replica_applied_state() -> None:
    state = OperationalState()
    state.set_replica_source_applied("disabled-source", 1, 2, False)

    state.reconcile_sources([])

    assert [
        source.source_id for source in state.replica_runtime_snapshot().sources
    ] == ["disabled-source"]
