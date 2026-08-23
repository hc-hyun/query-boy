from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import urlsplit

import httpx2
import pytest
from dotenv import load_dotenv
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult

from query_man.mcp_server import MCP_PROTOCOL_VERSION
from query_man.quality import QualityEvaluation
from query_man.sql_validation import SQL_POLICY_REVISION
from query_man.verified import VerifiedQuery, VerifiedQueryRegistry, create_result_hash
from tests.helpers import ROOT_DIRECTORY

pytestmark = [pytest.mark.mcp_server, pytest.mark.asyncio]

_KNOWN_SOURCES = {"development-issues", "market-voc"}
_DISCOVER_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "server/discover",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "query-man-server-test",
                "version": "1",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    },
}
_HANDSHAKE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "query-man-handshake-test", "version": "1"},
    },
}
_CURRENT_VERSION_HANDSHAKE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 3,
    "method": "initialize",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "query-man-handshake-test",
                "version": "1",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        },
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "query-man-handshake-test", "version": "1"},
    },
}


@dataclass(frozen=True)
class McpServerSettings:
    url: str
    token: str = field(repr=False)


class BearerAuth(httpx2.Auth):
    def __init__(self, token: str) -> None:
        self._token = token

    def auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, httpx2.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


@pytest.fixture(scope="module", autouse=True)
def suppress_http_client_request_logs() -> Generator[None, None, None]:
    client_logger = logging.getLogger("httpx2")
    previous_level = client_logger.level
    client_logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        client_logger.setLevel(previous_level)


@pytest.fixture(scope="module")
def mcp_server_settings() -> McpServerSettings:
    load_dotenv(ROOT_DIRECTORY / ".env", override=False)
    token = os.environ.get("QUERY_MAN_CODEX_MCP_TOKEN")
    if not token:
        pytest.skip("QUERY_MAN_CODEX_MCP_TOKEN is not configured")

    port = os.environ.get("QUERY_MAN_PORT", "3000")
    url = os.environ.get(
        "QUERY_MAN_MCP_TEST_URL",
        os.environ.get("QUERY_MAN_MCP_URL", f"http://127.0.0.1:{port}/mcp"),
    )
    _validate_loopback_mcp_url(url)
    return McpServerSettings(url=url, token=token)


def _validate_loopback_mcp_url(url: str) -> None:
    parsed = urlsplit(url)
    try:
        loopback = parsed.hostname == "localhost" or (
            parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
        )
        _ = parsed.port
    except ValueError:
        loopback = False
    if (
        parsed.scheme != "http"
        or not loopback
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        pytest.fail("MCP server tests require an uncredentialed loopback http:// URL ending in /mcp")


@pytest.fixture(scope="module")
def quality_evaluation() -> QualityEvaluation:
    return QualityEvaluation.load(
        ROOT_DIRECTORY / "config" / "quality-evaluation.yaml",
        _KNOWN_SOURCES,
    )


@pytest.fixture(scope="module")
def verified_queries() -> VerifiedQueryRegistry:
    return VerifiedQueryRegistry.load(
        ROOT_DIRECTORY / "config" / "verified-queries.yaml",
        _KNOWN_SOURCES,
    )


@asynccontextmanager
async def _mcp_client(settings: McpServerSettings) -> AsyncIterator[Client]:
    async with (
        httpx2.AsyncClient(
            auth=BearerAuth(settings.token),
            timeout=httpx2.Timeout(15),
            trust_env=False,
        ) as authenticated_http,
        Client(
            streamable_http_client(
                settings.url,
                http_client=authenticated_http,
            ),
            mode=MCP_PROTOCOL_VERSION,
            read_timeout_seconds=15,
        ) as client,
    ):
        yield client


def _structured(result: CallToolResult) -> dict[str, Any]:
    assert result.structured_content is not None
    return cast(dict[str, Any], result.structured_content)


def _contract(verified: VerifiedQueryRegistry, query_id: str) -> VerifiedQuery:
    return next(query for query in verified.queries if query.query_id == query_id)


def _assert_verified_result(result: CallToolResult, contract: VerifiedQuery) -> str:
    assert result.is_error is False
    body = _structured(result)
    assert body["status"] == "ok"
    assert body["truncated"] is False
    assert tuple(body["columns"]) == contract.expected.columns
    assert body["row_count"] == contract.expected.row_count
    assert create_result_hash(tuple(body["columns"]), body["rows"]) == (contract.expected.result_hash)
    query_id = body["query_id"]
    assert isinstance(query_id, str) and query_id
    return query_id


async def test_tools_and_revision_refresh_workflow(
    mcp_server_settings: McpServerSettings,
    verified_queries: VerifiedQueryRegistry,
) -> None:
    contract = _contract(verified_queries, "market-devices-without-voc")
    async with _mcp_client(mcp_server_settings) as client:
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == [
            "list_sources",
            "get_context",
            "query",
        ]
        assert all(tool.description for tool in tools.tools)
        assert all(tool.input_schema["additionalProperties"] is False for tool in tools.tools)
        assert client.protocol_version == MCP_PROTOCOL_VERSION

        listed = _structured(await client.call_tool("list_sources", {}))
        assert {source["source_id"] for source in listed["sources"]} == _KNOWN_SOURCES
        assert len(json.dumps(listed, ensure_ascii=False, separators=(",", ":")).encode()) < 1_024
        assert "password" not in json.dumps(listed, ensure_ascii=False).casefold()

        context = _structured(
            await client.call_tool(
                "get_context",
                {"source_id": contract.source_id, "question": contract.question},
            )
        )
        assert context["metadata_revision"] == contract.metadata_revision
        assert [relation["name"] for relation in context["relations"]] == list(contract.relations)

        mismatched = await client.call_tool(
            "query",
            {
                "source_id": contract.source_id,
                "sql": contract.sql,
                "metadata_revision": f"sha256:{'0' * 64}",
                "sql_policy_revision": context["sql_policy_revision"],
            },
        )
        assert mismatched.is_error is True
        assert _structured(mismatched)["error"]["code"] == "METADATA_REVISION_MISMATCH"

        refreshed = _structured(
            await client.call_tool(
                "get_context",
                {"source_id": contract.source_id, "question": contract.question},
            )
        )
        assert refreshed["metadata_revision"] == contract.metadata_revision
        retried = await client.call_tool(
            "query",
            {
                "source_id": contract.source_id,
                "sql": contract.sql,
                "metadata_revision": refreshed["metadata_revision"],
                "sql_policy_revision": refreshed["sql_policy_revision"],
            },
        )
        _assert_verified_result(retried, contract)


async def test_all_quality_evaluation_cases_through_mcp(
    mcp_server_settings: McpServerSettings,
    quality_evaluation: QualityEvaluation,
) -> None:
    relation_failures: list[str] = []
    answerability_failures: list[str] = []
    answerability_count = 0
    context_sizes: list[int] = []

    async with _mcp_client(mcp_server_settings) as client:
        for case in quality_evaluation.cases:
            response = await client.call_tool(
                "get_context",
                {"source_id": case.source_id, "question": case.question},
            )
            assert response.is_error is False, case.case_id
            body = _structured(response)
            actual_relations = tuple(relation["name"] for relation in body["relations"])
            if actual_relations != case.expected_relations:
                relation_failures.append(case.case_id)
            if case.expected_answerability is not None:
                answerability_count += 1
                if body["answerability"]["status"] != case.expected_answerability:
                    answerability_failures.append(case.case_id)
            context_sizes.append(
                len(
                    json.dumps(
                        body,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                )
            )

    assert not relation_failures
    assert not answerability_failures
    relation_accuracy = 1 - (len(relation_failures) / len(quality_evaluation.cases))
    answerability_recall = 1 - (len(answerability_failures) / answerability_count if answerability_count else 0)
    assert relation_accuracy >= quality_evaluation.gates.min_relation_accuracy
    assert answerability_recall >= quality_evaluation.gates.min_answerability_recall
    assert max(context_sizes) <= quality_evaluation.gates.max_context_bytes
    print(
        json.dumps(
            {
                "event": "mcp_quality_summary",
                "cases": len(quality_evaluation.cases),
                "relation_accuracy": relation_accuracy,
                "answerability_recall": answerability_recall,
                "max_context_bytes": max(context_sizes),
            },
            sort_keys=True,
        )
    )


async def test_all_verified_query_contracts_through_mcp(
    mcp_server_settings: McpServerSettings,
    verified_queries: VerifiedQueryRegistry,
) -> None:
    observed_query_ids: set[str] = set()
    async with _mcp_client(mcp_server_settings) as client:
        for contract in verified_queries.queries:
            context_result = await client.call_tool(
                "get_context",
                {"source_id": contract.source_id, "question": contract.question},
            )
            assert context_result.is_error is False, contract.query_id
            context = _structured(context_result)
            assert context["metadata_revision"] == contract.metadata_revision, contract.query_id
            assert tuple(relation["name"] for relation in context["relations"]) == (contract.relations), (
                contract.query_id
            )

            result = await client.call_tool(
                "query",
                {
                    "source_id": contract.source_id,
                    "sql": contract.sql,
                    "metadata_revision": contract.metadata_revision,
                    "sql_policy_revision": context["sql_policy_revision"],
                },
            )
            observed_query_ids.add(_assert_verified_result(result, contract))

    assert len(observed_query_ids) == len(verified_queries.queries)
    print(
        json.dumps(
            {
                "event": "mcp_verified_summary",
                "contracts": len(verified_queries.queries),
                "unique_query_ids": len(observed_query_ids),
            },
            sort_keys=True,
        )
    )


async def test_raw_transport_security_and_protocol_boundaries(
    mcp_server_settings: McpServerSettings,
) -> None:
    auth_header = {"authorization": f"Bearer {mcp_server_settings.token}"}
    discover_body = json.dumps(_DISCOVER_REQUEST, separators=(",", ":")).encode()
    handshake_body = json.dumps(_HANDSHAKE_REQUEST, separators=(",", ":")).encode()
    current_version_handshake_body = json.dumps(
        _CURRENT_VERSION_HANDSHAKE_REQUEST,
        separators=(",", ":"),
    ).encode()
    modern_headers = {
        "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        "mcp-method": "server/discover",
    }
    marker = "SENSITIVE_MALFORMED_BODY_MARKER"
    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(15),
        trust_env=False,
    ) as client:
        accepted = await client.post(
            mcp_server_settings.url,
            headers={
                **auth_header,
                **modern_headers,
                "content-type": "application/json; charset=utf-8",
            },
            content=discover_body,
        )
        untrusted_host = await client.post(
            mcp_server_settings.url,
            headers={**auth_header, **modern_headers, "host": "attacker.invalid"},
            json={},
        )
        untrusted_origin = await client.post(
            mcp_server_settings.url,
            headers={
                **auth_header,
                **modern_headers,
                "origin": "https://attacker.invalid",
            },
            json={},
        )
        wrong_media_type = await client.post(
            mcp_server_settings.url,
            headers={**auth_header, **modern_headers, "content-type": "text/plain"},
            content=discover_body,
        )
        prefixed_media_type = await client.post(
            mcp_server_settings.url,
            headers={
                **auth_header,
                **modern_headers,
                "content-type": "application/json-evil",
            },
            content=discover_body,
        )
        duplicate_media_type = await client.post(
            mcp_server_settings.url,
            headers=[
                ("authorization", f"Bearer {mcp_server_settings.token}"),
                ("mcp-protocol-version", MCP_PROTOCOL_VERSION),
                ("mcp-method", "server/discover"),
                ("content-type", "application/json"),
                ("content-type", "application/json-evil"),
            ],
            content=discover_body,
        )
        oversized = await client.post(
            mcp_server_settings.url,
            headers={
                **auth_header,
                **modern_headers,
                "content-type": "application/json",
            },
            content=marker.encode() + (b"x" * 1_048_576),
        )
        malformed = [
            await client.post(
                mcp_server_settings.url,
                headers={
                    **auth_header,
                    **modern_headers,
                    "content-type": "application/json",
                },
                content=body,
            )
            for body in (
                b"",
                b"{}",
                b"[]",
                f'{{"jsonrpc":"2.0","id":1,"{marker}":'.encode(),
            )
        ]
        unauthenticated = await client.post(
            mcp_server_settings.url,
            headers={"content-type": "application/json"},
            content=discover_body,
        )
        invalid_token = await client.post(
            mcp_server_settings.url,
            headers={
                "authorization": "Bearer invalid-test-token",
                **modern_headers,
                "content-type": "application/json",
            },
            content=discover_body,
        )
        duplicate_token = await client.post(
            mcp_server_settings.url,
            headers=[
                ("authorization", f"Bearer {mcp_server_settings.token}"),
                ("authorization", f"Bearer {mcp_server_settings.token}"),
                ("mcp-protocol-version", MCP_PROTOCOL_VERSION),
                ("mcp-method", "server/discover"),
                ("content-type", "application/json"),
            ],
            content=discover_body,
        )
        unsupported_protocol = [
            await client.post(
                mcp_server_settings.url,
                headers={
                    **auth_header,
                    "content-type": "application/json",
                    **headers,
                },
                content=handshake_body,
            )
            for headers in (
                {},
                {"mcp-protocol-version": "2025-11-25"},
                {"mcp-protocol-version": "2099-01-01"},
            )
        ]
        duplicate_protocol = await client.post(
            mcp_server_settings.url,
            headers=[
                ("authorization", f"Bearer {mcp_server_settings.token}"),
                ("content-type", "application/json"),
                ("mcp-protocol-version", MCP_PROTOCOL_VERSION),
                ("mcp-protocol-version", MCP_PROTOCOL_VERSION),
            ],
            content=handshake_body,
        )
        current_version_handshake = await client.post(
            mcp_server_settings.url,
            headers={
                **auth_header,
                "content-type": "application/json",
                "mcp-protocol-version": MCP_PROTOCOL_VERSION,
                "mcp-method": "initialize",
            },
            content=current_version_handshake_body,
        )
        modern_get = await client.get(
            mcp_server_settings.url,
            headers={
                **auth_header,
                "mcp-protocol-version": "2026-07-28",
                "accept": "application/json",
            },
        )
        modern_delete = await client.delete(
            mcp_server_settings.url,
            headers={
                **auth_header,
                "mcp-protocol-version": "2026-07-28",
            },
        )

    assert accepted.status_code == 200
    assert accepted.json()["jsonrpc"] == "2.0"
    assert accepted.json()["result"]["supportedVersions"] == [MCP_PROTOCOL_VERSION]
    instructions = accepted.json()["result"]["instructions"]
    assert "list_sources" in instructions
    assert "get_context" in instructions
    assert "METADATA_REVISION_MISMATCH" in instructions
    assert untrusted_host.status_code == 421
    assert untrusted_origin.status_code == 403
    assert wrong_media_type.status_code == 400
    assert prefixed_media_type.status_code == 400
    assert duplicate_media_type.status_code == 400
    assert oversized.status_code == 413
    assert marker not in oversized.text
    assert all(response.status_code == 400 for response in malformed)
    assert all(marker not in response.text for response in malformed)
    assert all(len(response.content) < 8_192 for response in [oversized, *malformed])
    for response in (unauthenticated, invalid_token, duplicate_token):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHORIZED"
        assert len(response.content) < 1_024
    for response in (*unsupported_protocol, duplicate_protocol):
        assert response.status_code == 400
        assert response.json()["error"] == {
            "code": -32022,
            "message": "Unsupported protocol version",
            "data": {"supported": [MCP_PROTOCOL_VERSION]},
        }
        assert len(response.content) < 1_024
    assert current_version_handshake.status_code == 404
    assert current_version_handshake.json()["error"] == {
        "code": -32601,
        "message": "Method not found",
        "data": "initialize",
    }
    assert len(current_version_handshake.content) < 1_024
    assert modern_get.status_code == 405
    assert modern_get.headers["allow"] == "POST"
    assert modern_delete.status_code == 405
    assert modern_delete.headers["allow"] == "POST"


async def test_tool_validation_does_not_disclose_input(
    mcp_server_settings: McpServerSettings,
) -> None:
    marker = "SENSITIVE_MCP_ARGUMENT_MARKER_DO_NOT_ECHO"
    async with _mcp_client(mcp_server_settings) as client:
        rejected = [
            await client.call_tool(
                "query",
                {
                    "source_id": "development-issues",
                    "sql": marker + ("x" * 100_000),
                    "metadata_revision": f"sha256:{'0' * 64}",
                    "sql_policy_revision": SQL_POLICY_REVISION,
                },
            ),
            await client.call_tool("list_sources", {"host": marker}),
            await client.call_tool(
                "get_context",
                {
                    "source_id": "development-issues",
                    "question": "문제 수",
                    "max_objects": "2",
                },
            ),
        ]

    assert all(result.is_error is True for result in rejected)
    assert all(result.structured_content is None for result in rejected)
    assert all(marker not in str(result.content) for result in rejected)
    assert all(len(str(result.content).encode()) < 8_192 for result in rejected)


async def test_query_policy_rejections_are_structured_and_bounded(
    mcp_server_settings: McpServerSettings,
) -> None:
    unknown_source = "unregistered-sensitive-source"
    async with _mcp_client(mcp_server_settings) as client:
        revision = _structured(
            await client.call_tool(
                "get_context",
                {"source_id": "development-issues", "question": "문제 수"},
            )
        )["metadata_revision"]
        rejected = [
            (
                await client.call_tool(
                    "query",
                    {
                        "source_id": "development-issues",
                        "sql": sql,
                        "metadata_revision": revision,
                        "sql_policy_revision": SQL_POLICY_REVISION,
                    },
                ),
                reason_code,
            )
            for sql, reason_code in (
                (
                    "DELETE FROM ai.issue_overview",
                    "SQL_STATEMENT_NOT_ALLOWED",
                ),
                (
                    "SELECT count(*) FROM issue_overview",
                    "SQL_RELATION_MUST_BE_QUALIFIED",
                ),
                (
                    "SELECT pg_catalog.pg_sleep(0.01) FROM ai.issue_overview LIMIT 1",
                    "SQL_FUNCTION_NOT_ALLOWED",
                ),
            )
        ]
        hidden = await client.call_tool(
            "get_context",
            {"source_id": unknown_source, "question": "anything"},
        )

    for result, reason_code in rejected:
        assert result.is_error is True
        body = _structured(result)
        assert body["error"]["code"] == "QUERY_REJECTED"
        assert body["error"]["details"] == {"reason_code": reason_code}
        assert len(str(result.content).encode()) < 2_048
    assert hidden.is_error is True
    assert _structured(hidden)["error"] == {
        "code": "SOURCE_NOT_FOUND",
        "message": "The requested source was not found.",
    }
    assert unknown_source not in str(hidden.content)


async def test_same_client_handles_bounded_concurrent_query_batch(
    mcp_server_settings: McpServerSettings,
    verified_queries: VerifiedQueryRegistry,
) -> None:
    contract = _contract(verified_queries, "market-devices-without-voc")
    arguments = {
        "source_id": contract.source_id,
        "sql": contract.sql,
        "metadata_revision": contract.metadata_revision,
        "sql_policy_revision": SQL_POLICY_REVISION,
    }
    started = time.monotonic()
    async with _mcp_client(mcp_server_settings) as client:
        results = await asyncio.gather(*(client.call_tool("query", arguments) for _ in range(24)))

    query_ids = [_assert_verified_result(result, contract) for result in results]
    assert len(set(query_ids)) == len(query_ids)
    bodies = [_structured(result) for result in results]
    print(
        json.dumps(
            {
                "event": "mcp_same_client_parallel_summary",
                "queries": len(results),
                "unique_query_ids": len(set(query_ids)),
                "wall_ms": round((time.monotonic() - started) * 1_000),
                "max_queue_ms": max(int(body["queue_ms"]) for body in bodies),
                "max_execution_ms": max(int(body["elapsed_ms"]) for body in bodies),
            },
            sort_keys=True,
        )
    )


async def _run_independent_session(
    settings: McpServerSettings,
    contract: VerifiedQuery,
) -> str:
    async with _mcp_client(settings) as client:
        listed = _structured(await client.call_tool("list_sources", {}))
        assert {source["source_id"] for source in listed["sources"]} == _KNOWN_SOURCES
        context = _structured(
            await client.call_tool(
                "get_context",
                {"source_id": contract.source_id, "question": contract.question},
            )
        )
        assert context["metadata_revision"] == contract.metadata_revision
        assert tuple(relation["name"] for relation in context["relations"]) == contract.relations
        result = await client.call_tool(
            "query",
            {
                "source_id": contract.source_id,
                "sql": contract.sql,
                "metadata_revision": contract.metadata_revision,
                "sql_policy_revision": context["sql_policy_revision"],
            },
        )
        return _assert_verified_result(result, contract)


async def test_independent_sessions_are_isolated_and_exact(
    mcp_server_settings: McpServerSettings,
    verified_queries: VerifiedQueryRegistry,
) -> None:
    contracts = (
        _contract(verified_queries, "development-recent-model-issues"),
        _contract(verified_queries, "market-devices-without-voc"),
    )
    started = time.monotonic()
    query_ids = await asyncio.gather(
        *(_run_independent_session(mcp_server_settings, contracts[index % 2]) for index in range(8))
    )
    assert len(set(query_ids)) == len(query_ids)
    print(
        json.dumps(
            {
                "event": "mcp_independent_sessions_summary",
                "sessions": len(query_ids),
                "unique_query_ids": len(set(query_ids)),
                "wall_ms": round((time.monotonic() - started) * 1_000),
            },
            sort_keys=True,
        )
    )
