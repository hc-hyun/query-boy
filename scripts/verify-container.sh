#!/usr/bin/env bash

set -euo pipefail

container_id="$(docker compose ps -q query-man)"
if [[ -z "$container_id" ]]; then
  echo "query-man container is not running" >&2
  exit 1
fi

published_port="$(docker compose port query-man 3000 | sed 's/.*://')"
base_url="http://127.0.0.1:${published_port}"
readiness="$(curl -fsS "${base_url}/ready")"
if [[ "$readiness" != '{"status":"ready"}' ]]; then
  echo "unexpected readiness response: ${readiness}" >&2
  exit 1
fi

unauthenticated_status="$(
  curl -sS -o /dev/null -w '%{http_code}' "${base_url}/sources"
)"
if [[ "$unauthenticated_status" != "401" ]]; then
  echo "unauthenticated /sources returned ${unauthenticated_status}, expected 401" >&2
  exit 1
fi

container_uid="$(docker compose exec -T query-man id -u | tr -d '\r')"
if [[ "$container_uid" == "0" ]]; then
  echo "query-man container is running as root" >&2
  exit 1
fi

if [[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_id")" != "true" ]]; then
  echo "query-man root filesystem is writable" >&2
  exit 1
fi

docker compose exec -T query-man sh -c \
  'test ! -e /app/.env && test ! -e /app/.git && test ! -e /app/tests'

docker compose exec -T query-man python - <<'PY'
from __future__ import annotations

import asyncio
import os
from collections.abc import Generator

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client


class BearerAuth(httpx2.Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(
        self, request: httpx2.Request
    ) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


async def verify() -> None:
    token = os.environ["QUERY_MAN_CODEX_MCP_TOKEN"]
    async with (
        httpx2.AsyncClient(auth=BearerAuth(token), trust_env=False) as authenticated_http,
        Client(
            streamable_http_client(
                "http://127.0.0.1:3000/mcp",
                http_client=authenticated_http,
            ),
            read_timeout_seconds=10,
        ) as client,
    ):
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == [
            "list_sources",
            "get_context",
            "query",
        ]

        sources = await client.call_tool("list_sources", {})
        assert sources.structured_content is not None
        source_ids = {
            source["source_id"] for source in sources.structured_content["sources"]
        }
        assert source_ids == {"development-issues", "market-voc"}

        context = await client.call_tool(
            "get_context",
            {
                "source_id": "development-issues",
                "question": "전체 개발 문제 건수를 보여줘",
            },
        )
        assert context.structured_content is not None
        assert context.structured_content["quality_level"] == "L2"
        revision = context.structured_content["metadata_revision"]
        result = await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": "SELECT count(*) AS issue_count FROM ai.issue_overview",
                "metadata_revision": revision,
            },
        )
        assert result.structured_content is not None
        assert result.structured_content["status"] == "ok"
        assert result.structured_content["columns"] == ["issue_count"]
        assert result.structured_content["rows"] == [{"issue_count": 600}]
        assert result.structured_content["row_count"] == 1
        assert result.structured_content["truncated"] is False


asyncio.run(verify())
PY

echo "container HTTP/MCP smoke passed"
