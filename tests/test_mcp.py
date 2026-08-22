from __future__ import annotations

import json

from mcp.client import Client

from query_man.access import AccessPolicy, CallerContext
from query_man.gateway import GatewayService
from query_man.mcp_server import create_mcp_server
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.query import QueryService
from query_man.sql_validation import ValidatedSql
from tests.helpers import load_test_registry, minimal_development_snapshot


class StaticCatalog:
    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return minimal_development_snapshot()

    async def close(self) -> None:
        pass


class StaticExecutor:
    async def execute(
        self,
        _source: SourceProfile,
        _sql: str,
        metadata_revision: str,
        validated: ValidatedSql,
        *,
        query_id: str | None = None,
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "query_id": query_id or "test-query-id",
            "metadata_revision": metadata_revision,
            "fingerprint": validated.fingerprint,
            "columns": ["issue_count"],
            "rows": [{"issue_count": 600}],
            "row_count": 1,
            "result_bytes": 21,
            "truncated": False,
            "queue_ms": 0,
            "elapsed_ms": 1,
            "plan_summary": {"total_cost": 1.0, "max_rows": 1, "node_count": 1},
        }

    async def cancel(self, _query_id: str, _allowed_sources: frozenset[str]) -> bool:
        return False

    async def close(self) -> None:
        pass


def mcp_fixture() -> tuple[object, MetadataService]:
    registry = load_test_registry()
    metadata = MetadataService(registry, StaticCatalog())
    executor = StaticExecutor()
    queries = QueryService(registry, metadata, executor)
    policy = AccessPolicy.local(["development-issues"])
    gateway = GatewayService(registry, metadata, queries, policy)
    caller = CallerContext(
        caller_id="test-analyst",
        tenant_id="engineering",
        allowed_sources=frozenset({"development-issues"}),
    )
    return create_mcp_server(gateway, lambda: caller), metadata


async def test_mcp_exposes_fixed_tools_and_reuses_gateway_policy() -> None:
    server, _metadata = mcp_fixture()
    async with Client(server) as client:  # type: ignore[arg-type]
        listed_tools = await client.list_tools()
        assert [tool.name for tool in listed_tools.tools] == [
            "list_sources",
            "get_context",
            "query",
        ]
        assert all("host" not in tool.input_schema.get("properties", {}) for tool in listed_tools.tools)
        schemas = {tool.name: tool.input_schema for tool in listed_tools.tools}
        assert schemas["list_sources"]["properties"] == {}
        assert schemas["get_context"]["required"] == ["source_id", "question"]
        assert schemas["get_context"]["properties"]["max_objects"] == {
            "default": 2,
            "maximum": 4,
            "minimum": 1,
            "title": "Max Objects",
            "type": "integer",
        }
        assert schemas["query"]["required"] == [
            "source_id",
            "sql",
            "metadata_revision",
        ]
        assert schemas["query"]["properties"]["metadata_revision"]["pattern"] == (
            r"^sha256:[a-f0-9]{64}$"
        )

        sources = await client.call_tool("list_sources")
        assert len(json.dumps(sources.structured_content).encode()) < 1_024
        assert [source["source_id"] for source in sources.structured_content["sources"]] == [  # type: ignore[index]
            "development-issues"
        ]
        assert "password" not in str(sources.structured_content)
        denied = await client.call_tool(
            "get_context",
            {"source_id": "market-voc", "question": "VOC 수"},
        )
        assert denied.structured_content == {
            "error": {
                "code": "SOURCE_NOT_FOUND",
                "message": "The requested source was not found.",
            }
        }


async def test_mcp_context_revision_and_query_contract() -> None:
    server, _metadata = mcp_fixture()
    async with Client(server) as client:  # type: ignore[arg-type]
        context = await client.call_tool(
            "get_context",
            {"source_id": "development-issues", "question": "문제 수"},
        )
        revision = str(context.structured_content["metadata_revision"])  # type: ignore[index]
        result = await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": "SELECT count(*) AS issue_count FROM ai.issue_overview",
                "metadata_revision": revision,
            },
        )
        assert result.structured_content["rows"] == [{"issue_count": 600}]  # type: ignore[index]
        assert len(json.dumps(result.structured_content).encode()) < 4_096

        mismatch = await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": "SELECT count(*) FROM ai.issue_overview",
                "metadata_revision": f"sha256:{'0' * 64}",
            },
        )
        assert mismatch.structured_content["error"]["code"] == (  # type: ignore[index]
            "METADATA_REVISION_MISMATCH"
        )
