from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.guarded_query.sql_validation import SQL_POLICY_REVISION
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.service import MetadataService
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import QUERY_CAVE_CONFIG_DIRECTORY


@pytest.mark.integration
@pytest.mark.load
@pytest.mark.asyncio
async def test_interactive_budget_under_representative_local_load() -> None:
    state_directory = os.environ.get("QUERY_CAVE_STATE_DIRECTORY")
    if not state_directory:
        pytest.skip("Query Cave is not running")

    environment = dict(os.environ)
    environment["QUERY_MAN_POSTGRES_HOST"] = "127.0.0.1"
    registry = SourceRegistry.load(
        QUERY_CAVE_CONFIG_DIRECTORY / "sources",
        QUERY_CAVE_CONFIG_DIRECTORY / "budget-profiles.yaml",
        QUERY_CAVE_CONFIG_DIRECTORY / "database-profiles.yaml",
        Path(state_directory) / "host",
        environment,
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    service = QueryService(registry, metadata, executor)
    source_id = "query-cave"
    query = "SELECT case_id, summary FROM signal_schema.case_files_view ORDER BY case_id"
    try:
        revision = (await metadata.get_published(source_id)).revision
        async def measured_query() -> tuple[int, dict[str, object]]:
            started = time.monotonic()
            result = await service.query(
                source_id,
                query,
                revision,
                SQL_POLICY_REVISION,
            )
            return round((time.monotonic() - started) * 1000), result

        results = await asyncio.gather(*(measured_query() for _iteration in range(20)))
        service_wall = [duration for duration, _result in results]
        execution = [int(result["elapsed_ms"]) for _duration, result in results]
        queue = [int(result["queue_ms"]) for _duration, result in results]
        plan_costs = [
            float(result["plan_summary"]["total_cost"])  # type: ignore[index]
            for _duration, result in results
        ]
        summary = {
            "queries": len(results),
            "source": source_id,
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

        assert all(result["status"] == "ok" for _duration, result in results)
        assert all(result["row_count"] == 3 for _duration, result in results)
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
