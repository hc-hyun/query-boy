from __future__ import annotations

import asyncio
import json
import os
import time

import pytest
from dotenv import load_dotenv

from query_man.guarded_query.query import PostgresQueryExecutor, QueryService
from query_man.guarded_query.sql_validation import SQL_POLICY_REVISION
from query_man.metadata.catalog import PostgresCatalog
from query_man.metadata.service import MetadataService
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import ROOT_DIRECTORY


@pytest.mark.integration
@pytest.mark.load
@pytest.mark.asyncio
async def test_interactive_budget_under_representative_local_load() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    reader_password = os.environ.get("DEVELOPMENT_ISSUES_READER_PASSWORD")
    if not reader_password:
        pytest.skip("local fixture reader credentials are not configured")

    environment = dict(os.environ)
    environment["FIXTURE_SOURCE_READER_PASSWORD"] = reader_password
    environment["QUERY_MAN_POSTGRES_HOST"] = "127.0.0.1"
    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "tests" / "fixtures" / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        environment,
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    service = QueryService(registry, metadata, executor)
    source_id = "fixture-source"
    query = "SELECT record_id, text_value FROM ai.fixture_records ORDER BY record_id"
    try:
        revision = (await metadata.get_published(source_id)).revision
        metadata.invalidate()

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
