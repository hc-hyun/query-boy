from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass

import pytest
from mcp.types import CallToolResult
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo

from query_man.assurance.verified import VerifiedQueryRegistry
from tests.test_mcp_server import (
    McpServerSettings,
    _assert_verified_result,
    _contract,
    _mcp_client,
    _structured,
    _validate_loopback_mcp_url,
    mcp_server_settings,  # noqa: F401 -- shared pytest fixture
    suppress_http_client_request_logs,  # noqa: F401 -- shared autouse fixture
    verified_queries,  # noqa: F401 -- shared pytest fixture
)
from tests.test_mcp_server_load import (
    _DEVELOPMENT_COUNT_SQL,
    _DEVELOPMENT_SOURCE,
    _MARKET_COUNT_SQL,
    _MARKET_SOURCE,
    _SLOW_SQL,
    _active_query_man_session_count,
    _assert_exact_count,
    _error_code,
    _measured_query,
    _revision,
    _wait_for_active_query_man_sessions,
)

pytestmark = [pytest.mark.mcp_server, pytest.mark.soak, pytest.mark.asyncio]

_REPLICA_SERVICES = ("query-man", "query-man-replica")
_RESOURCE_PROBE = """
import json
from pathlib import Path

children = Path("/proc/1/task/1/children").read_text().split()
if len(children) != 1:
    raise RuntimeError(f"expected one application child, observed {len(children)}")
pid = children[0]
status = Path("/proc", pid, "status").read_text().splitlines()
rss_kb = int(next(line.split()[1] for line in status if line.startswith("VmRSS:")))
print(json.dumps({
    "pid": int(pid),
    "fd_count": len(list(Path("/proc", pid, "fd").iterdir())),
    "rss_kb": rss_kb,
}))
""".strip()


@dataclass(frozen=True)
class _ResourceSample:
    pid: int
    fd_count: int
    rss_kb: int
    restart_count: int
    oom_killed: bool


@pytest.fixture(scope="module")
def mcp_replica_settings(
    mcp_server_settings: McpServerSettings,  # noqa: F811 -- shared fixture
) -> tuple[McpServerSettings, McpServerSettings]:
    replica_port = os.environ.get("QUERY_MAN_REPLICA_PORT", "3001")
    replica_url = os.environ.get(
        "QUERY_MAN_MCP_REPLICA_URL",
        f"http://127.0.0.1:{replica_port}/mcp",
    )
    _validate_loopback_mcp_url(replica_url)
    if replica_url == mcp_server_settings.url:
        pytest.fail("MCP soak requires two distinct loopback replica URLs")
    return (
        mcp_server_settings,
        McpServerSettings(url=replica_url, token=mcp_server_settings.token),
    )


def _docker_output(*arguments: str) -> str:
    completed = subprocess.run(
        ["docker", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return completed.stdout.strip()


def _resource_sample(service: str) -> _ResourceSample:
    container_id = _docker_output("compose", "--profile", "soak", "ps", "-q", service)
    if not container_id:
        pytest.fail(f"Compose service {service} is not running")
    restart_count, oom_killed = _docker_output(
        "inspect",
        "--format",
        "{{.RestartCount}} {{.State.OOMKilled}}",
        container_id,
    ).split()
    process = json.loads(
        _docker_output(
            "compose",
            "--profile",
            "soak",
            "exec",
            "-T",
            service,
            "python",
            "-c",
            _RESOURCE_PROBE,
        )
    )
    return _ResourceSample(
        pid=int(process["pid"]),
        fd_count=int(process["fd_count"]),
        rss_kb=int(process["rss_kb"]),
        restart_count=int(restart_count),
        oom_killed=oom_killed == "true",
    )


async def _wait_for_fd_recovery(
    service: str,
    baseline: _ResourceSample,
    *,
    maximum_growth: int,
) -> _ResourceSample:
    deadline = time.monotonic() + 10
    while True:
        sample = _resource_sample(service)
        if sample.fd_count <= baseline.fd_count + maximum_growth:
            return sample
        if time.monotonic() >= deadline:
            return sample
        await asyncio.sleep(0.25)


async def _one_stateless_session(
    settings: McpServerSettings,
    replica_index: int,
    semaphore: asyncio.Semaphore,
) -> tuple[int, int]:
    async with semaphore:
        started = time.monotonic()
        async with _mcp_client(settings) as client:
            sources = _structured(await client.call_tool("list_sources", {}))
            assert {source["source_id"] for source in sources["sources"]} == {
                "development-issues",
                "market-voc",
            }
        return replica_index, round((time.monotonic() - started) * 1_000)


async def _run_session_range(
    settings: tuple[McpServerSettings, McpServerSettings],
    *,
    start: int,
    count: int,
    parallelism: int,
) -> list[tuple[int, int]]:
    semaphore = asyncio.Semaphore(parallelism)
    return await asyncio.gather(
        *(
            _one_stateless_session(
                settings[index % len(settings)],
                index % len(settings),
                semaphore,
            )
            for index in range(start, start + count)
        )
    )


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


async def test_two_replicas_serve_exact_queries_with_unique_ids(
    mcp_replica_settings: tuple[McpServerSettings, McpServerSettings],
    verified_queries: VerifiedQueryRegistry,  # noqa: F811 -- shared fixture
) -> None:
    contract = _contract(verified_queries, "market-devices-without-voc")

    async def query_replica(settings: McpServerSettings) -> tuple[str, list[str]]:
        async with _mcp_client(settings) as client:
            tools = await client.list_tools()
            assert [tool.name for tool in tools.tools] == [
                "list_sources",
                "get_context",
                "query",
            ]
            tool_contract = json.dumps(
                [
                    tool.model_dump(mode="json", by_alias=True, exclude_none=True)
                    for tool in tools.tools
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            context = _structured(
                await client.call_tool(
                    "get_context",
                    {"source_id": contract.source_id, "question": contract.question},
                )
            )
            assert context["metadata_revision"] == contract.metadata_revision
            results = await asyncio.gather(
                *(
                    client.call_tool(
                        "query",
                        {
                            "source_id": contract.source_id,
                            "sql": contract.sql,
                            "metadata_revision": contract.metadata_revision,
                            "sql_policy_revision": context["sql_policy_revision"],
                        },
                    )
                    for _ in range(8)
                )
            )
            return tool_contract, [
                _assert_verified_result(result, contract) for result in results
            ]

    started = time.monotonic()
    replica_results = await asyncio.gather(
        *(query_replica(settings) for settings in mcp_replica_settings)
    )
    assert replica_results[0][0] == replica_results[1][0]
    query_ids_by_replica = [query_ids for _tool_contract, query_ids in replica_results]
    query_ids = [query_id for group in query_ids_by_replica for query_id in group]
    assert len(set(query_ids)) == 16
    print(
        json.dumps(
            {
                "event": "mcp_replica_exact_summary",
                "replicas": len(mcp_replica_settings),
                "queries": len(query_ids),
                "unique_query_ids": len(set(query_ids)),
                "wall_ms": round((time.monotonic() - started) * 1_000),
            },
            sort_keys=True,
        )
    )


async def test_two_replica_source_saturation_is_bounded_and_recovers(
    mcp_replica_settings: tuple[McpServerSettings, McpServerSettings],
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
    started = time.monotonic()
    try:
        assert await _active_query_man_session_count(observer) == 0
        async with (
            _mcp_client(mcp_replica_settings[0]) as first,
            _mcp_client(mcp_replica_settings[1]) as second,
        ):
            development_revision, market_revision = await asyncio.gather(
                _revision(first, _DEVELOPMENT_SOURCE, "개발 문제 수"),
                _revision(first, _MARKET_SOURCE, "시장 VOC 수"),
            )
            clients = (first, second)
            holder_tasks = [
                asyncio.create_task(
                    _measured_query(
                        client,
                        source_id=_DEVELOPMENT_SOURCE,
                        sql=_SLOW_SQL,
                        metadata_revision=development_revision,
                    )
                )
                for client in clients
                for _ in range(2)
            ]
            active_holders = await _wait_for_active_query_man_sessions(
                observer,
                expected=4,
            )
            overload_tasks = [
                asyncio.create_task(
                    _measured_query(
                        client,
                        source_id=_DEVELOPMENT_SOURCE,
                        sql=_DEVELOPMENT_COUNT_SQL,
                        metadata_revision=development_revision,
                    )
                )
                for client in clients
            ]
            market_task = asyncio.create_task(
                _measured_query(
                    first,
                    source_id=_MARKET_SOURCE,
                    sql=_MARKET_COUNT_SQL,
                    metadata_revision=market_revision,
                )
            )
            overload_results = await asyncio.gather(*overload_tasks)
            market_result, market_wall_ms = await market_task
            overload_codes = [_error_code(result) for result, _wall_ms in overload_results]
            assert overload_codes == ["QUERY_OVERLOADED", "QUERY_OVERLOADED"]
            _assert_exact_count(market_result, column="voc_count", count=1_200)

            holder_results = await asyncio.gather(*holder_tasks)
            holder_codes = [_error_code(result) for result, _wall_ms in holder_results]
            assert holder_codes == ["QUERY_TIMEOUT"] * 4
            recovered = await asyncio.gather(
                *(
                    _measured_query(
                        client,
                        source_id=_DEVELOPMENT_SOURCE,
                        sql=_DEVELOPMENT_COUNT_SQL,
                        metadata_revision=development_revision,
                    )
                    for client in clients
                )
            )
            for result, _wall_ms in recovered:
                _assert_exact_count(result, column="issue_count", count=600)
            assert await _active_query_man_session_count(observer) == 0

        print(
            json.dumps(
                {
                    "event": "mcp_replica_saturation_summary",
                    "active_holders": active_holders,
                    "holder_codes": holder_codes,
                    "overload_codes": overload_codes,
                    "market_wall_ms": market_wall_ms,
                    "total_ms": round((time.monotonic() - started) * 1_000),
                },
                sort_keys=True,
            )
        )
    finally:
        for task in holder_tasks:
            if not task.done():
                task.cancel()
        if holder_tasks:
            await asyncio.gather(*holder_tasks, return_exceptions=True)
        await observer.close()


async def test_one_thousand_stateless_sessions_do_not_leak_process_resources(
    mcp_replica_settings: tuple[McpServerSettings, McpServerSettings],
) -> None:
    total_sessions = int(os.environ.get("QUERY_MAN_MCP_SOAK_SESSIONS", "1000"))
    parallelism = int(os.environ.get("QUERY_MAN_MCP_SOAK_PARALLELISM", "20"))
    if total_sessions < 1_000 or total_sessions % 2 != 0:
        pytest.fail("QUERY_MAN_MCP_SOAK_SESSIONS must be an even value of at least 1000")
    if not 1 <= parallelism <= 64:
        pytest.fail("QUERY_MAN_MCP_SOAK_PARALLELISM must be between 1 and 64")

    warmup_sessions = 100
    remaining = total_sessions - warmup_sessions
    first_phase_sessions = remaining // 2
    second_phase_sessions = remaining - first_phase_sessions
    started = time.monotonic()

    warmup = await _run_session_range(
        mcp_replica_settings,
        start=0,
        count=warmup_sessions,
        parallelism=parallelism,
    )
    baseline = tuple(_resource_sample(service) for service in _REPLICA_SERVICES)
    first_phase = await _run_session_range(
        mcp_replica_settings,
        start=warmup_sessions,
        count=first_phase_sessions,
        parallelism=parallelism,
    )
    middle = tuple(_resource_sample(service) for service in _REPLICA_SERVICES)
    second_phase = await _run_session_range(
        mcp_replica_settings,
        start=warmup_sessions + first_phase_sessions,
        count=second_phase_sessions,
        parallelism=parallelism,
    )
    final = tuple(
        [
            await _wait_for_fd_recovery(service, sample, maximum_growth=8)
            for service, sample in zip(_REPLICA_SERVICES, baseline, strict=True)
        ]
    )

    all_results = [*warmup, *first_phase, *second_phase]
    assert len(all_results) == total_sessions
    assert [sum(index == replica for index, _latency in all_results) for replica in range(2)] == [
        total_sessions // 2,
        total_sessions // 2,
    ]
    for index in range(2):
        assert baseline[index].restart_count == middle[index].restart_count == final[index].restart_count == 0
        assert not baseline[index].oom_killed
        assert not middle[index].oom_killed
        assert not final[index].oom_killed
        assert baseline[index].pid == middle[index].pid == final[index].pid
        assert final[index].fd_count <= baseline[index].fd_count + 8
        assert middle[index].rss_kb <= baseline[index].rss_kb + 32 * 1_024
        assert final[index].rss_kb <= baseline[index].rss_kb + 32 * 1_024
        assert final[index].rss_kb <= middle[index].rss_kb + 16 * 1_024

    latencies = [latency for _replica, latency in all_results]
    print(
        json.dumps(
            {
                "event": "mcp_session_soak_summary",
                "sessions": total_sessions,
                "parallelism": parallelism,
                "sessions_per_replica": total_sessions // 2,
                "wall_ms": round((time.monotonic() - started) * 1_000),
                "latency_ms": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "max": max(latencies),
                },
                "resources": [
                    {
                        "service": service,
                        "baseline": baseline_sample.__dict__,
                        "middle": middle_sample.__dict__,
                        "final": final_sample.__dict__,
                    }
                    for service, baseline_sample, middle_sample, final_sample in zip(
                        _REPLICA_SERVICES,
                        baseline,
                        middle,
                        final,
                        strict=True,
                    )
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
