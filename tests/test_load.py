from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from dotenv import load_dotenv

from query_man.catalog import PostgresCatalog
from query_man.metadata import MetadataService
from query_man.query import PostgresQueryExecutor, QueryService
from query_man.registry import SourceRegistry
from tests.helpers import ROOT_DIRECTORY


@pytest.mark.integration
@pytest.mark.load
@pytest.mark.asyncio
async def test_interactive_budget_under_representative_local_load() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    service = QueryService(registry, metadata, executor)
    cases = {
        "development-issues": (
            "SELECT status, count(*) AS issue_count "
            "FROM ai.issue_overview GROUP BY status ORDER BY status"
        ),
        "market-voc": (
            "SELECT status, count(*) AS voc_count "
            "FROM ai.voc_overview GROUP BY status ORDER BY status"
        ),
    }
    try:
        revisions = {
            source_id: (await metadata.get_published(source_id)).revision for source_id in cases
        }
        metadata.invalidate()

        async def measured_query(source_id: str, sql: str) -> tuple[str, int, dict[str, object]]:
            started = time.monotonic()
            result = await service.query(source_id, sql, revisions[source_id])
            return source_id, round((time.monotonic() - started) * 1000), result

        queries = [
            measured_query(source_id, sql)
            for _iteration in range(20)
            for source_id, sql in cases.items()
        ]
        results = await asyncio.gather(*queries)
        service_wall = [duration for _source_id, duration, _result in results]
        execution = [
            int(result["elapsed_ms"]) for _source_id, _duration, result in results
        ]
        queue = [int(result["queue_ms"]) for _source_id, _duration, result in results]
        plan_costs = [
            float(result["plan_summary"]["total_cost"])  # type: ignore[index]
            for _source_id, _duration, result in results
        ]
        summary = {
            "queries": len(results),
            "sources": sorted(cases),
            "service_wall_ms_p50": _percentile(service_wall, 0.50),
            "service_wall_ms_p95": _percentile(service_wall, 0.95),
            "service_wall_ms_max": max(service_wall),
            "execution_ms_p50": _percentile(execution, 0.50),
            "execution_ms_p95": _percentile(execution, 0.95),
            "execution_ms_max": max(execution),
            "queue_ms_p95": _percentile(queue, 0.95),
            "queue_ms_max": max(queue),
            "plan_total_cost_max": max(plan_costs),
        }
        print(json.dumps(summary, sort_keys=True))

        assert all(result["status"] == "ok" for _source_id, _duration, result in results)
        assert all(int(result["row_count"]) > 0 for _source_id, _duration, result in results)
        assert max(service_wall) < 8_000
        assert max(execution) < 8_000
        assert max(queue) <= 1_000
        assert max(plan_costs) <= 100_000
    finally:
        await executor.close()
        await catalog.close()


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * percentile)]
