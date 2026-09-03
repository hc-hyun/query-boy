from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass

import pytest

from tests.test_mcp_server import (
    McpServerSettings,
    _mcp_client,
    _structured,
    _validate_loopback_mcp_url,
    mcp_server_settings,  # noqa: F401 -- shared pytest fixture
    suppress_http_client_request_logs,  # noqa: F401 -- shared autouse fixture
)
from tests.test_mcp_server_load import (
    _COUNT_SQL,
    _FIXTURE_SOURCE,
    _assert_exact_count,
    _measured_query,
    _revision,
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
            assert {source["source_id"] for source in sources["sources"]} == {_FIXTURE_SOURCE}
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
) -> None:
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
            metadata_revision = await _revision(client, _FIXTURE_SOURCE, "record count")
            results = await asyncio.gather(
                *(
                    _measured_query(
                        client,
                        source_id=_FIXTURE_SOURCE,
                        sql=_COUNT_SQL,
                        metadata_revision=metadata_revision,
                    )
                    for _ in range(8)
                )
            )
            query_ids: list[str] = []
            for result, _elapsed_ms in results:
                _assert_exact_count(result, column="record_count", count=3)
                query_id = _structured(result)["query_id"]
                assert isinstance(query_id, str) and query_id
                query_ids.append(query_id)
            return tool_contract, query_ids

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
