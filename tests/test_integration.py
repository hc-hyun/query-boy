from __future__ import annotations

import asyncio
import os
from dataclasses import replace

import pytest
from dotenv import load_dotenv

from query_man.catalog import PostgresCatalog
from query_man.errors import QueryOverloadedError, QueryRejectedError, QueryTimeoutError
from query_man.metadata import MetadataService
from query_man.query import PostgresQueryExecutor, QueryService
from query_man.registry import SourceRegistry
from query_man.sql_validation import DEFAULT_ALLOWED_FUNCTIONS, validate_sql
from tests.helpers import ROOT_DIRECTORY


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_catalog_maps_golden_questions() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    catalog = PostgresCatalog()
    service = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    cases = [
        (
            "development-issues",
            "최근 90일 동안 모델별 개발 문제 건수와 미해결 건수를 보여줘.",
            ["ai.issue_overview"],
        ),
        (
            "development-issues",
            "사용자별 등록 문제 수, 담당 문제 수와 작성 댓글 수를 비교해줘.",
            ["ai.issue_overview", "ai.issue_comments"],
        ),
        (
            "market-voc",
            "모델별 기기 수, VOC 수와 기기당 VOC 수를 높은 순서로 보여줘.",
            ["ai.device_overview"],
        ),
        ("market-voc", "VOC가 한 번도 없는 기기는 몇 대인가?", ["ai.device_overview"]),
        ("market-voc", "NURI 세대별 힌지 VOC 수를 비교해줘.", ["ai.voc_overview"]),
    ]
    try:
        for source_id, question, expected in cases:
            response = await service.get_context(source_id, question)
            assert [item["name"] for item in response["relations"]] == expected
            assert str(response["metadata_revision"]).startswith("sha256:")
    finally:
        await catalog.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_guarded_query_enforces_plan_and_result_limits() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    if not os.environ.get("DEVELOPMENT_ISSUES_READER_PASSWORD"):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    catalog = PostgresCatalog()
    executor = PostgresQueryExecutor()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    service = QueryService(registry, metadata, executor)
    try:
        published = await metadata.get_published("development-issues")
        counted = await service.query(
            "development-issues",
            "SELECT count(*) AS issue_count FROM ai.issue_overview",
            published.revision,
        )
        assert counted["rows"] == [{"issue_count": 600}]
        assert counted["truncated"] is False
        assert counted["plan_summary"]["node_count"] > 0  # type: ignore[index]

        limited = await service.query(
            "development-issues",
            "SELECT * FROM ai.issue_comments ORDER BY comment_id",
            published.revision,
        )
        assert limited["row_count"] == 1000
        assert limited["truncated"] is True
        assert limited["result_bytes"] <= 1_048_576  # type: ignore[operator]

        with pytest.raises(QueryRejectedError) as caught:
            await service.query(
                "development-issues",
                "SELECT count(*) FROM ai.issue_overview AS a "
                "CROSS JOIN ai.issue_overview AS b CROSS JOIN ai.issue_overview AS c",
                published.revision,
            )
        assert caught.value.details["reason_code"] in {  # type: ignore[index]
            "QUERY_PLAN_COST_EXCEEDED",
            "QUERY_PLAN_ROWS_EXCEEDED",
        }

        source = registry.get("development-issues")
        assert source is not None
        slow_sql = (
            "SELECT count(*) FROM ai.issue_overview AS a "
            "CROSS JOIN ai.issue_overview AS b CROSS JOIN ai.issue_overview AS c"
        )
        timeout_source = replace(
            source,
            budget=replace(
                source.budget,
                query_statement_timeout_ms=1,
                max_plan_total_cost=2_147_483_647,
                max_plan_rows=2_147_483_647,
            ),
        )
        validated = validate_sql(
            slow_sql,
            allowed_relations=(relation.qualified_name for relation in published.snapshot.relations),
        )
        with pytest.raises(QueryTimeoutError):
            await executor.execute(timeout_source, slow_sql, published.revision, validated)

        recovered = await service.query(
            "development-issues",
            "SELECT count(*) AS issue_count FROM ai.issue_overview",
            published.revision,
        )
        assert recovered["rows"] == [{"issue_count": 600}]

        for forbidden_sql, reason_code in [
            ("SELECT * FROM development.issues", "SQL_RELATION_NOT_ALLOWED"),
            ("DELETE FROM ai.issue_overview", "SQL_STATEMENT_NOT_ALLOWED"),
            ("SELECT pg_catalog.pg_sleep(0.01)", "SQL_FUNCTION_NOT_ALLOWED"),
        ]:
            with pytest.raises(QueryRejectedError) as forbidden:
                await service.query(
                    "development-issues",
                    forbidden_sql,
                    published.revision,
                )
            assert forbidden.value.details == {"reason_code": reason_code}

        volatile_sql = "SELECT pg_catalog.random() FROM ai.issue_overview LIMIT 1"
        volatile = validate_sql(
            volatile_sql,
            allowed_relations=(relation.qualified_name for relation in published.snapshot.relations),
            allowed_functions=DEFAULT_ALLOWED_FUNCTIONS | {"random"},
        )
        with pytest.raises(QueryRejectedError) as volatile_error:
            await executor.execute(source, volatile_sql, published.revision, volatile)
        assert volatile_error.value.details == {
            "reason_code": "QUERY_RESOLVED_FUNCTION_NOT_ALLOWED"
        }
    finally:
        await executor.close()
        await catalog.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_query_concurrency_cancel_and_source_isolation() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["DEVELOPMENT_ISSUES_READER_PASSWORD", "MARKET_VOC_READER_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local reader credentials are not configured")

    registry = SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
    )
    catalog = PostgresCatalog()
    metadata = MetadataService(registry, catalog, cache_ttl_ms=30_000)
    executor = PostgresQueryExecutor()
    try:
        development = registry.get("development-issues")
        market = registry.get("market-voc")
        assert development is not None
        assert market is not None
        development_metadata = await metadata.get_published(development.source_id)
        market_metadata = await metadata.get_published(market.source_id)
        limited_development = replace(
            development,
            budget=replace(
                development.budget,
                query_statement_timeout_ms=2_000,
                query_transaction_timeout_ms=3_000,
                query_queue_timeout_ms=20,
                max_pool_size=1,
                max_concurrent_queries=1,
                max_plan_total_cost=10**15,
                max_plan_rows=10**15,
            ),
        )
        slow_sql = (
            "SELECT count(*) FROM ai.issue_overview AS a "
            "CROSS JOIN ai.issue_overview AS b "
            "CROSS JOIN ai.issue_overview AS c "
            "CROSS JOIN ai.issue_overview AS d"
        )
        slow_validated = validate_sql(
            slow_sql,
            allowed_relations=(
                relation.qualified_name for relation in development_metadata.snapshot.relations
            ),
        )
        slow_task = asyncio.create_task(
            executor.execute(
                limited_development,
                slow_sql,
                development_metadata.revision,
                slow_validated,
            )
        )
        await asyncio.sleep(0.05)

        dev_count_sql = "SELECT count(*) AS issue_count FROM ai.issue_overview"
        dev_count_validated = validate_sql(
            dev_count_sql,
            allowed_relations=(
                relation.qualified_name for relation in development_metadata.snapshot.relations
            ),
        )
        with pytest.raises(QueryOverloadedError):
            await executor.execute(
                limited_development,
                dev_count_sql,
                development_metadata.revision,
                dev_count_validated,
            )

        market_sql = "SELECT count(*) AS voc_count FROM ai.voc_overview"
        market_validated = validate_sql(
            market_sql,
            allowed_relations=(relation.qualified_name for relation in market_metadata.snapshot.relations),
        )
        market_result = await executor.execute(
            market,
            market_sql,
            market_metadata.revision,
            market_validated,
        )
        assert market_result["rows"] == [{"voc_count": 1200}]

        slow_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow_task

        recovered = await executor.execute(
            limited_development,
            dev_count_sql,
            development_metadata.revision,
            dev_count_validated,
        )
        assert recovered["rows"] == [{"issue_count": 600}]
    finally:
        await executor.close()
        await catalog.close()
