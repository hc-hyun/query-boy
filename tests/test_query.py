from __future__ import annotations

import asyncio
from typing import Any

import pytest

from query_man.app import _until_disconnect
from query_man.errors import MetadataRevisionMismatchError, QueryRejectedError, QueryTimeoutError
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.query import PlanSummary, QueryService, _summarize_plan
from query_man.sql_validation import ValidatedSql
from tests.helpers import load_test_registry, minimal_development_snapshot


class StaticCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return minimal_development_snapshot()

    async def close(self) -> None:
        pass


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[SourceProfile, str, str, ValidatedSql]] = []

    async def execute(
        self,
        source: SourceProfile,
        sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
    ) -> dict[str, object]:
        self.calls.append((source, sql, metadata_revision, validated))
        return {
            "status": "ok",
            "query_id": "test-query-id",
            "metadata_revision": metadata_revision,
            "fingerprint": validated.fingerprint,
            "columns": ["issue_count"],
            "rows": [{"issue_count": 3}],
            "row_count": 1,
            "result_bytes": 19,
            "truncated": False,
            "queue_ms": 0,
            "elapsed_ms": 1,
            "plan_summary": {"total_cost": 1.0, "max_rows": 1, "node_count": 1},
        }

    async def close(self) -> None:
        pass


def query_service() -> tuple[QueryService, MetadataService, RecordingExecutor]:
    registry = load_test_registry()
    metadata = MetadataService(registry, StaticCatalog())
    executor = RecordingExecutor()
    return QueryService(registry, metadata, executor), metadata, executor


@pytest.mark.asyncio
async def test_validates_revision_and_sql_before_execution() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")

    response = await service.query(
        "development-issues",
        "SELECT count(*) AS issue_count FROM ai.issue_overview",
        published.revision,
    )

    assert response["status"] == "ok"
    assert len(executor.calls) == 1
    assert executor.calls[0][3].relations == ("ai.issue_overview",)


@pytest.mark.asyncio
async def test_rejects_stale_revision_before_execution() -> None:
    service, _metadata, executor = query_service()

    with pytest.raises(MetadataRevisionMismatchError):
        await service.query("development-issues", "SELECT 1", f"sha256:{'0' * 64}")

    assert executor.calls == []


@pytest.mark.asyncio
async def test_maps_ast_rejection_to_stable_query_error() -> None:
    service, metadata, executor = query_service()
    published = await metadata.get_published("development-issues")

    with pytest.raises(QueryRejectedError) as caught:
        await service.query("development-issues", "DELETE FROM ai.issue_overview", published.revision)

    assert caught.value.details == {"reason_code": "SQL_STATEMENT_NOT_ALLOWED"}
    assert executor.calls == []


def test_summarizes_nested_explain_plan() -> None:
    plan: dict[str, Any] = {
        "Total Cost": 42.25,
        "Plan Rows": 2,
        "Plans": [
            {"Total Cost": 10, "Plan Rows": 100},
            {"Total Cost": 20, "Plan Rows": 5, "Plans": [{"Plan Rows": 300}]},
        ],
    }

    assert _summarize_plan(plan) == PlanSummary(total_cost=42.25, max_rows=300, node_count=4)


def test_rejects_plan_without_required_estimates() -> None:
    with pytest.raises(RuntimeError):
        _summarize_plan({"Plan Rows": 1})


@pytest.mark.asyncio
async def test_client_disconnect_cancels_active_query() -> None:
    cancelled = asyncio.Event()

    async def pending() -> dict[str, object]:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    class DisconnectedRequest:
        async def is_disconnected(self) -> bool:
            return True

    with pytest.raises(QueryTimeoutError):
        await _until_disconnect(DisconnectedRequest(), pending())  # type: ignore[arg-type]
    assert cancelled.is_set()
