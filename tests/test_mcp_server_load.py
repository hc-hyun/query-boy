from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import pytest
from mcp.client import Client
from mcp.types import CallToolResult
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo

from query_man.guarded_query.sql_validation import SQL_POLICY_REVISION
from tests.test_mcp_server import (
    McpServerSettings,
    _mcp_client,
    _structured,
    mcp_server_settings,  # noqa: F401 -- shared pytest fixture
    suppress_http_client_request_logs,  # noqa: F401 -- shared autouse fixture
)

pytestmark = [pytest.mark.mcp_server, pytest.mark.load, pytest.mark.asyncio]

_DEVELOPMENT_SOURCE = "development-issues"
_MARKET_SOURCE = "market-voc"
_SLOW_SQL = "SELECT sum(power(issue_id::pg_catalog.numeric, 40000)) AS score FROM ai.issue_overview"
_DEVELOPMENT_COUNT_SQL = "SELECT count(*) AS issue_count FROM ai.issue_overview"
_MARKET_COUNT_SQL = "SELECT count(*) AS voc_count FROM ai.voc_overview"
_ACTIVE_QUERY_MAN_SESSIONS = """
SELECT count(*)
FROM pg_catalog.pg_stat_activity
WHERE datname = 'development_issues'
  AND usename = 'development_issues_reader'
  AND state = 'active'
  AND application_name ~
      '^query-man:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
"""


async def _revision(client: Client, source_id: str, question: str) -> str:
    result = await client.call_tool(
        "get_context",
        {"source_id": source_id, "question": question},
    )
    assert result.is_error is False
    revision = _structured(result)["metadata_revision"]
    assert isinstance(revision, str)
    return revision


async def _measured_query(
    client: Client,
    *,
    source_id: str,
    sql: str,
    metadata_revision: str,
) -> tuple[CallToolResult, int]:
    started = time.monotonic()
    result = await client.call_tool(
        "query",
        {
            "source_id": source_id,
            "sql": sql,
            "metadata_revision": metadata_revision,
            "sql_policy_revision": SQL_POLICY_REVISION,
        },
    )
    return result, round((time.monotonic() - started) * 1_000)


async def _active_query_man_session_count(
    observer: AsyncConnection[Any],
) -> int:
    cursor = await observer.execute(_ACTIVE_QUERY_MAN_SESSIONS)
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _wait_for_active_query_man_sessions(
    observer: AsyncConnection[Any],
    *,
    expected: int,
    timeout_seconds: float = 4,
) -> int:
    deadline = time.monotonic() + timeout_seconds
    maximum_observed = 0
    while True:
        count = await _active_query_man_session_count(observer)
        maximum_observed = max(maximum_observed, count)
        if count == expected:
            return count
        if count > expected:
            pytest.fail(f"expected exactly {expected} active Query Man sessions, observed {count}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                f"timed out waiting for {expected} active Query Man sessions; maximum observed was {maximum_observed}"
            )
        await asyncio.sleep(min(0.02, remaining))


def _error_code(result: CallToolResult) -> str:
    assert result.is_error is True
    error = _structured(result)["error"]
    assert isinstance(error, dict)
    code = error.get("code")
    assert isinstance(code, str)
    return code


def _assert_exact_count(
    result: CallToolResult,
    *,
    column: str,
    count: int,
) -> None:
    assert result.is_error is False
    body = _structured(result)
    assert body["status"] == "ok"
    assert body["columns"] == [column]
    assert body["rows"] == [{column: count}]
    assert body["row_count"] == 1
    assert body["truncated"] is False


async def test_source_saturation_is_isolated_and_recovers(
    mcp_server_settings: McpServerSettings,  # noqa: F811 -- imported pytest fixture
) -> None:
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL admin credentials are not configured")

    observer = await AsyncConnection.connect(
        make_conninfo(
            host="127.0.0.1",
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            sslmode="disable",
        ),
        autocommit=True,
    )
    holder_tasks: list[asyncio.Task[tuple[CallToolResult, int]]] = []
    test_started = time.monotonic()
    try:
        assert await _active_query_man_session_count(observer) == 0
        async with _mcp_client(mcp_server_settings) as client:
            development_revision, market_revision = await asyncio.gather(
                _revision(client, _DEVELOPMENT_SOURCE, "개발 문제 수"),
                _revision(client, _MARKET_SOURCE, "시장 VOC 수"),
            )

            holder_tasks = [
                asyncio.create_task(
                    _measured_query(
                        client,
                        source_id=_DEVELOPMENT_SOURCE,
                        sql=_SLOW_SQL,
                        metadata_revision=development_revision,
                    )
                )
                for _ in range(2)
            ]
            active_holders = await _wait_for_active_query_man_sessions(
                observer,
                expected=2,
            )

            overloaded_task = asyncio.create_task(
                _measured_query(
                    client,
                    source_id=_DEVELOPMENT_SOURCE,
                    sql=_DEVELOPMENT_COUNT_SQL,
                    metadata_revision=development_revision,
                )
            )
            market_task = asyncio.create_task(
                _measured_query(
                    client,
                    source_id=_MARKET_SOURCE,
                    sql=_MARKET_COUNT_SQL,
                    metadata_revision=market_revision,
                )
            )
            (
                (overloaded_result, overloaded_wall_ms),
                (
                    market_result,
                    market_wall_ms,
                ),
            ) = await asyncio.gather(overloaded_task, market_task)

            overloaded_code = _error_code(overloaded_result)
            assert overloaded_code == "QUERY_OVERLOADED"
            _assert_exact_count(market_result, column="voc_count", count=1_200)

            holder_results = await asyncio.gather(*holder_tasks)
            holder_codes = [_error_code(result) for result, _wall_ms in holder_results]
            assert holder_codes == ["QUERY_TIMEOUT", "QUERY_TIMEOUT"]

            recovered_result, recovered_wall_ms = await _measured_query(
                client,
                source_id=_DEVELOPMENT_SOURCE,
                sql=_DEVELOPMENT_COUNT_SQL,
                metadata_revision=development_revision,
            )
            _assert_exact_count(recovered_result, column="issue_count", count=600)
            assert await _active_query_man_session_count(observer) == 0

        summary = {
            "event": "mcp_source_saturation_summary",
            "active_holders": active_holders,
            "error_codes": {
                "development_holders": holder_codes,
                "development_overload": overloaded_code,
            },
            "wall_ms": {
                "development_holders": [wall_ms for _result, wall_ms in holder_results],
                "development_overload": overloaded_wall_ms,
                "development_recovery": recovered_wall_ms,
                "market_control": market_wall_ms,
                "total": round((time.monotonic() - test_started) * 1_000),
            },
        }
        print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    finally:
        for task in holder_tasks:
            if not task.done():
                task.cancel()
        if holder_tasks:
            await asyncio.gather(*holder_tasks, return_exceptions=True)
        await observer.close()
