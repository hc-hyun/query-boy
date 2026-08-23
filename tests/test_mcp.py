from __future__ import annotations

import json
import logging
from typing import cast

import pytest
from mcp.client import Client

from query_man.access import CallerContext
from query_man.errors import QueryInvalidError
from query_man.gateway import GatewayService
from query_man.mcp_server import create_mcp_server
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, SourceProfile
from query_man.operations import operations
from query_man.query import QueryService
from query_man.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DEFAULT_ALLOWED_TYPES,
    DEFAULT_ALLOWED_UNQUALIFIED_TYPES,
    ValidatedSql,
)
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
        tenant_id: str | None = None,
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

    async def cancel(self, _query_id: str) -> bool:
        return False

    async def close(self) -> None:
        pass


class ExplodingGateway:
    detail = "sensitive-internal-database-detail"

    def list_sources(self, _caller: CallerContext) -> dict[str, object]:
        raise RuntimeError(self.detail)

    async def get_context(
        self,
        _caller: CallerContext,
        _source_id: str,
        _question: str,
        _max_objects: int,
    ) -> dict[str, object]:
        raise RuntimeError(self.detail)

    async def query(
        self,
        _caller: CallerContext,
        _source_id: str,
        _sql: str,
        _metadata_revision: str,
        _sql_policy_revision: str,
    ) -> dict[str, object]:
        raise RuntimeError(self.detail)


class InvalidResultGateway(ExplodingGateway):
    def list_sources(self, _caller: CallerContext) -> dict[str, object]:
        return {"invalid": object()}


class InvalidQueryGateway(ExplodingGateway):
    async def query(
        self,
        _caller: CallerContext,
        _source_id: str,
        _sql: str,
        _metadata_revision: str,
        _sql_policy_revision: str,
    ) -> dict[str, object]:
        raise QueryInvalidError("QUERY_UNDEFINED_COLUMN")


def mcp_fixture(
    caller: CallerContext | None = None,
) -> tuple[object, MetadataService]:
    registry = load_test_registry()
    metadata = MetadataService(registry, StaticCatalog())
    executor = StaticExecutor()
    queries = QueryService(registry, metadata, executor)
    gateway = GatewayService(registry, metadata, queries)
    caller = caller or CallerContext(
        caller_id="test-analyst",
        tenant_id="engineering",
    )
    return create_mcp_server(gateway, lambda: caller), metadata


async def test_mcp_exposes_fixed_tools_and_shared_gateway_sources() -> None:
    server, _metadata = mcp_fixture()
    async with Client(server) as client:  # type: ignore[arg-type]
        listed_tools = await client.list_tools()
        assert [tool.name for tool in listed_tools.tools] == [
            "list_sources",
            "get_context",
            "query",
        ]
        descriptions = {tool.name: tool.description for tool in listed_tools.tools}
        assert descriptions["get_context"] == (
            "Get question-scoped metadata and the revision required by query. "
            "The response includes allowed SQL functions, cast types, and unqualified cast forms. "
            "max_objects must be an integer from 1 through 4 and defaults to 2."
        )
        assert all("host" not in tool.input_schema.get("properties", {}) for tool in listed_tools.tools)
        tools = {tool.name: tool for tool in listed_tools.tools}
        assert all(
            tools[name].output_schema is not None
            and tools[name].output_schema.get("type") == "object"
            and tools[name].output_schema.get("additionalProperties") is True
            for name in ("list_sources", "query")
        )
        context_output = tools["get_context"].output_schema
        assert context_output is not None
        success_output = context_output["$defs"]["GetContextSuccessOutput"]
        assert success_output["required"] == [
            "metadata_revision",
            "sql_policy_revision",
            "sql_capabilities",
        ]
        assert success_output["properties"]["metadata_revision"]["pattern"] == (
            r"^sha256:[a-f0-9]{64}$"
        )
        assert success_output["properties"]["sql_policy_revision"]["pattern"] == (
            r"^sha256:[a-f0-9]{64}$"
        )
        capabilities_output = context_output["$defs"]["SqlCapabilitiesOutput"]
        assert capabilities_output["required"] == [
            "functions",
            "cast_types",
            "unqualified_cast_types",
        ]
        assert all(
            capabilities_output["properties"][name] == {
                "items": {"type": "string"},
                "title": title,
                "type": "array",
            }
            for name, title in (
                ("functions", "Functions"),
                ("cast_types", "Cast Types"),
                ("unqualified_cast_types", "Unqualified Cast Types"),
            )
        )
        assert {item["$ref"] for item in context_output["anyOf"]} == {
            "#/$defs/GetContextSuccessOutput",
            "#/$defs/_ToolErrorOutput",
        }
        schemas = {tool.name: tool.input_schema for tool in listed_tools.tools}
        assert all(schema["additionalProperties"] is False for schema in schemas.values())
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
            "sql_policy_revision",
        ]
        assert schemas["query"]["properties"]["metadata_revision"]["pattern"] == (r"^sha256:[a-f0-9]{64}$")
        assert schemas["query"]["properties"]["sql_policy_revision"]["pattern"] == (
            r"^sha256:[a-f0-9]{64}$"
        )

        sources = await client.call_tool("list_sources")
        assert len(json.dumps(sources.structured_content).encode()) < 1_024
        assert [source["source_id"] for source in sources.structured_content["sources"]] == [  # type: ignore[index]
            "development-issues",
            "market-voc",
        ]
        assert "password" not in str(sources.structured_content)

        rejected_extras = [
            await client.call_tool("list_sources", {"host": "attacker.invalid"}),
            await client.call_tool(
                "get_context",
                {
                    "source_id": "development-issues",
                    "question": "문제 수",
                    "host": "attacker.invalid",
                },
            ),
            await client.call_tool(
                "query",
                {
                    "source_id": "development-issues",
                    "sql": "SELECT count(*) FROM ai.issue_overview",
                    "metadata_revision": f"sha256:{'0' * 64}",
                    "sql_policy_revision": f"sha256:{'0' * 64}",
                    "tenant_id": "caller-selected-tenant",
                },
            ),
        ]
        assert all(result.is_error is True for result in rejected_extras)
        assert all(result.structured_content is None for result in rejected_extras)
        assert all("Extra inputs are not permitted" in str(result.content) for result in rejected_extras)


async def test_mcp_context_revision_and_query_contract() -> None:
    server, _metadata = mcp_fixture()
    async with Client(server) as client:  # type: ignore[arg-type]
        context = await client.call_tool(
            "get_context",
            {"source_id": "development-issues", "question": "문제 수"},
        )
        assert context.structured_content["sql_capabilities"] == {  # type: ignore[index]
            "functions": sorted(DEFAULT_ALLOWED_FUNCTIONS),
            "cast_types": sorted(DEFAULT_ALLOWED_TYPES),
            "unqualified_cast_types": sorted(DEFAULT_ALLOWED_UNQUALIFIED_TYPES),
        }
        revision = str(context.structured_content["metadata_revision"])  # type: ignore[index]
        policy_revision = str(context.structured_content["sql_policy_revision"])  # type: ignore[index]
        result = await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": "SELECT count(*) AS issue_count FROM ai.issue_overview",
                "metadata_revision": revision,
                "sql_policy_revision": policy_revision,
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
                "sql_policy_revision": policy_revision,
            },
        )
        assert mismatch.structured_content["error"]["code"] == (  # type: ignore[index]
            "METADATA_REVISION_MISMATCH"
        )
        assert mismatch.is_error is True


async def test_mcp_query_rejection_reports_bounded_construct_without_echoing_sql() -> None:
    sensitive_literal = "SENSITIVE_SQL_LITERAL_DO_NOT_ECHO"
    server, _metadata = mcp_fixture()
    async with Client(server) as client:  # type: ignore[arg-type]
        context = await client.call_tool(
            "get_context",
            {"source_id": "development-issues", "question": "제조일별 시험기 수"},
        )
        result = await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": (
                    "SELECT count(*) FROM ai.test_unit_overview "
                    "WHERE manufactured_at NOT BETWEEN DATE '2026-05-01' "
                    "AND DATE '2026-05-31' "
                    f"AND serial_number <> '{sensitive_literal}'"
                ),
                "metadata_revision": context.structured_content["metadata_revision"],  # type: ignore[index]
                "sql_policy_revision": context.structured_content["sql_policy_revision"],  # type: ignore[index]
            },
        )

    assert result.is_error is True
    assert result.structured_content == {
        "error": {
            "code": "QUERY_REJECTED",
            "message": "The query is not allowed by the source policy.",
            "details": {
                "reason_code": "SQL_OPERATOR_NOT_ALLOWED",
                "rejected_construct": "NOT BETWEEN",
            },
        }
    }
    assert sensitive_literal not in str(result.content)
    assert len(str(result.content).encode()) < 512


async def test_mcp_query_invalid_reports_only_bounded_correction_reason() -> None:
    sensitive_sql = "SELECT private_missing_column FROM ai.private_relation"
    caller = CallerContext(
        caller_id="test-analyst",
        tenant_id="engineering",
    )
    server = create_mcp_server(InvalidQueryGateway(), lambda: caller)  # type: ignore[arg-type]

    async with Client(server) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": sensitive_sql,
                "metadata_revision": f"sha256:{'0' * 64}",
                "sql_policy_revision": f"sha256:{'0' * 64}",
            },
        )

    assert result.is_error is True
    assert result.structured_content == {
        "error": {
            "code": "QUERY_INVALID",
            "message": "The query must be corrected before it can run.",
            "details": {"reason_code": "QUERY_UNDEFINED_COLUMN"},
        }
    }
    assert sensitive_sql not in str(result.content)
    assert len(str(result.content).encode()) < 512


async def test_mcp_normalizes_strings_and_rejects_implicit_integer_coercion() -> None:
    server, _metadata = mcp_fixture()
    async with Client(server) as client:  # type: ignore[arg-type]
        context = await client.call_tool(
            "get_context",
            {
                "source_id": " development-issues ",
                "question": " 문제 수 ",
                "max_objects": 2,
            },
        )
        assert context.is_error is False
        revision = str(context.structured_content["metadata_revision"])  # type: ignore[index]
        policy_revision = str(context.structured_content["sql_policy_revision"])  # type: ignore[index]
        queried = await client.call_tool(
            "query",
            {
                "source_id": " development-issues ",
                "sql": " SELECT count(*) AS issue_count FROM ai.issue_overview ",
                "metadata_revision": f" {revision} ",
                "sql_policy_revision": f" {policy_revision} ",
            },
        )
        assert queried.is_error is False
        assert queried.structured_content["row_count"] == 1  # type: ignore[index]

        rejected = [
            await client.call_tool(
                "get_context",
                {
                    "source_id": "development-issues",
                    "question": "문제 수",
                    "max_objects": value,
                },
            )
            for value in (True, "2", 2.0)
        ]
        blank_question = await client.call_tool(
            "get_context",
            {"source_id": "development-issues", "question": "   "},
        )

    assert all(result.is_error is True for result in [*rejected, blank_question])


async def test_mcp_validation_does_not_echo_oversized_sensitive_input() -> None:
    marker = "SENSITIVE_SQL_MARKER_DO_NOT_ECHO"
    server, _metadata = mcp_fixture()
    async with Client(server) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": marker + ("x" * 100_000),
                "metadata_revision": f"sha256:{'0' * 64}",
                "sql_policy_revision": f"sha256:{'0' * 64}",
            },
        )

    assert result.is_error is True
    assert result.structured_content is None
    assert marker not in str(result.content)


async def test_mcp_debug_logs_correlate_calls_without_recording_inputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    question_marker = "SENSITIVE_QUESTION_MARKER"
    sql_marker = "SENSITIVE_SQL_MARKER"
    operations.reset()
    server, _metadata = mcp_fixture(
        CallerContext(
            caller_id="test-analyst",
            tenant_id="engineering",
        )
    )
    caplog.set_level(logging.DEBUG, logger="query_man.mcp")

    async with Client(server) as client:  # type: ignore[arg-type]
        context = await client.call_tool(
            "get_context",
            {"source_id": "development-issues", "question": question_marker},
        )
        await client.call_tool(
            "query",
            {
                "source_id": "development-issues",
                "sql": f"SELECT count(*) AS issue_count FROM ai.issue_overview /* {sql_marker} */",
                "metadata_revision": context.structured_content["metadata_revision"],  # type: ignore[index]
                "sql_policy_revision": context.structured_content["sql_policy_revision"],  # type: ignore[index]
            },
        )

    records = [
        record
        for record in caplog.records
        if record.name == "query_man.mcp" and record.getMessage().startswith("mcp_tool_")
    ]
    started = [record for record in records if record.getMessage() == "mcp_tool_started"]
    completed = [record for record in records if record.getMessage() == "mcp_tool_completed"]
    assert len(started) == len(completed) == 2
    assert {record.mcp_call_id for record in started} == {record.mcp_call_id for record in completed}
    assert {record.tool_name for record in completed} == {"get_context", "query"}
    assert all(record.outcome == "success" for record in completed)
    assert all(record.caller_id == "test-analyst" for record in completed)
    assert all(record.source_id == "development-issues" for record in completed)
    assert all(isinstance(record.duration_ms, int) for record in completed)
    assert question_marker not in caplog.text
    assert sql_marker not in caplog.text
    metrics = {
        (metric["name"], metric.get("source_id")): metric["value"] for metric in operations.snapshot()["metrics"]
    }
    assert metrics[("mcp_tool_started", None)] == 2
    assert metrics[("mcp_tool_completed", "development-issues")] == 2
    assert metrics[("mcp_tool_duration_ms_count", "development-issues")] == 2
    operations.reset()


async def test_mcp_unknown_source_is_not_logged_or_used_as_metric_label(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unknown_source = "unregistered-sensitive-source"
    operations.reset()
    server, _metadata = mcp_fixture(
        CallerContext(
            caller_id="test-operator",
            tenant_id="engineering",
            operator=True,
        )
    )
    caplog.set_level(logging.DEBUG)

    async with Client(server) as client:  # type: ignore[arg-type]
        result = await client.call_tool(
            "query",
            {
                "source_id": unknown_source,
                "sql": "SELECT 1",
                "metadata_revision": f"sha256:{'0' * 64}",
                "sql_policy_revision": f"sha256:{'0' * 64}",
            },
        )

    assert result.is_error is True
    assert result.structured_content == {
        "error": {
            "code": "SOURCE_NOT_FOUND",
            "message": "The requested source was not found.",
        }
    }
    assert unknown_source not in caplog.text
    metrics = operations.snapshot()["metrics"]
    assert all(metric.get("source_id") != unknown_source for metric in metrics)
    operations.reset()


async def test_mcp_sanitizes_unexpected_tool_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caller = CallerContext(
        caller_id="test-analyst",
        tenant_id="engineering",
    )
    server = create_mcp_server(
        cast(GatewayService, ExplodingGateway()),
        lambda: caller,
    )
    caplog.set_level(logging.ERROR, logger="query_man")

    async with Client(server) as client:
        results = [
            await client.call_tool("list_sources", {}),
            await client.call_tool(
                "get_context",
                {"source_id": "development-issues", "question": "문제 수"},
            ),
            await client.call_tool(
                "query",
                {
                    "source_id": "development-issues",
                    "sql": "SELECT count(*) FROM ai.issue_overview",
                    "metadata_revision": f"sha256:{'0' * 64}",
                    "sql_policy_revision": f"sha256:{'0' * 64}",
                },
            ),
        ]

    expected = {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred.",
        }
    }
    assert all(result.is_error is True for result in results)
    assert all(result.structured_content == expected for result in results)
    assert all(ExplodingGateway.detail not in str(result.content) for result in results)
    assert caplog.messages.count("Unhandled MCP tool error") == 3


async def test_mcp_serialization_failure_records_one_error_completion(
    caplog: pytest.LogCaptureFixture,
) -> None:
    operations.reset()
    caller = CallerContext(
        caller_id="test-analyst",
        tenant_id="engineering",
    )
    server = create_mcp_server(
        cast(GatewayService, InvalidResultGateway()),
        lambda: caller,
    )
    caplog.set_level(logging.DEBUG, logger="query_man.mcp")

    async with Client(server) as client:
        result = await client.call_tool("list_sources", {})

    completed = [
        record
        for record in caplog.records
        if record.name == "query_man.mcp" and record.getMessage() == "mcp_tool_completed"
    ]
    assert result.is_error is True
    assert result.structured_content == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred.",
        }
    }
    assert len(completed) == 1
    assert completed[0].outcome == "error"
    assert completed[0].error_code == "INTERNAL_ERROR"
    metrics = {(metric["name"], metric["value"]) for metric in operations.snapshot()["metrics"]}
    assert ("mcp_tool_completed", 1) in metrics
    assert ("mcp_tool_failed", 1) in metrics
    assert ("mcp_tool_duration_ms_count", 1) in metrics
    operations.reset()


async def test_mcp_sanitizes_unexpected_caller_provider_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    detail = "sensitive-caller-provider-detail"

    def exploding_caller_provider() -> CallerContext:
        raise RuntimeError(detail)

    server = create_mcp_server(
        cast(GatewayService, ExplodingGateway()),
        exploding_caller_provider,
    )
    caplog.set_level(logging.ERROR, logger="query_man")

    async with Client(server) as client:
        results = [
            await client.call_tool("list_sources", {}),
            await client.call_tool(
                "get_context",
                {"source_id": "development-issues", "question": "문제 수"},
            ),
            await client.call_tool(
                "query",
                {
                    "source_id": "development-issues",
                    "sql": "SELECT count(*) FROM ai.issue_overview",
                    "metadata_revision": f"sha256:{'0' * 64}",
                    "sql_policy_revision": f"sha256:{'0' * 64}",
                },
            ),
        ]

    expected = {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal error occurred.",
        }
    }
    assert all(result.is_error is True for result in results)
    assert all(result.structured_content == expected for result in results)
    assert all(detail not in str(result.content) for result in results)
    assert caplog.messages.count("Unhandled MCP tool error") == 3
