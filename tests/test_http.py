from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from query_man.delivery.access import AccessPolicy, CallerContext
from query_man.delivery.app import build_http_app
from query_man.errors import (
    MetadataRevisionMismatchError,
    MetadataUnavailableError,
    QueryRejectedError,
    QueryUnavailableError,
    SourceNotFoundError,
)
from query_man.runtime.operations import operations

_QUERY_TOKEN = "query-token-value-with-at-least-32-characters"
_OPERATOR_TOKEN = "operator-token-value-with-at-least-32-characters"
_METADATA_REVISION = f"sha256:{'a' * 64}"
_SQL_POLICY_REVISION = f"sha256:{'b' * 64}"
_QUERY_ID = "8a18a5ec-97fd-4f4a-aac9-45aef533019d"


class RecordingGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.failures: dict[str, Exception] = {}
        self.hang_query = False
        self.query_started = asyncio.Event()
        self.query_cancelled = asyncio.Event()

    def list_sources(self, caller: CallerContext) -> dict[str, object]:
        self._raise("sources")
        self.calls.append(("sources", caller))
        return {
            "sources": [
                {
                    "source_id": "source-a",
                    "name": "Source A",
                    "description": "Curated source",
                }
            ]
        }

    async def get_context(
        self,
        caller: CallerContext,
        source_id: str,
    ) -> dict[str, object]:
        self._raise("meta")
        self.calls.append(("meta", caller, source_id))
        return {
            "metadata_revision": _METADATA_REVISION,
            "sql_policy_revision": _SQL_POLICY_REVISION,
            "relations": [],
        }

    async def query(
        self,
        caller: CallerContext,
        source_id: str,
        sql: str,
        metadata_revision: str,
        sql_policy_revision: str,
    ) -> dict[str, object]:
        self._raise("query")
        self.calls.append(
            (
                "query",
                caller,
                source_id,
                sql,
                metadata_revision,
                sql_policy_revision,
            )
        )
        if self.hang_query:
            self.query_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.query_cancelled.set()
                raise
        return {
            "status": "ok",
            "query_id": _QUERY_ID,
            "columns": ["count"],
            "rows": [{"count": 1}],
            "row_count": 1,
            "result_bytes": 13,
            "truncated": False,
        }

    async def cancel_query(
        self,
        caller: CallerContext,
        query_id: str,
    ) -> dict[str, str]:
        self._raise("cancel")
        self.calls.append(("cancel", caller, query_id))
        return {"status": "cancel_requested", "query_id": query_id}

    def _raise(self, operation: str) -> None:
        error = self.failures.get(operation)
        if error is not None:
            raise error


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield


@pytest.fixture(autouse=True)
def _reset_operations() -> AsyncIterator[None]:
    operations.reset()
    yield
    operations.reset()


def _app(
    gateway: RecordingGateway | None = None,
    policy: AccessPolicy | None = None,
) -> tuple[FastAPI, RecordingGateway]:
    selected_gateway = gateway or RecordingGateway()
    return (
        build_http_app(
            access_policy=policy or AccessPolicy.local(),
            gateway=selected_gateway,  # type: ignore[arg-type]
            lifespan=_lifespan,
        ),
        selected_gateway,
    )


def _client(
    gateway: RecordingGateway | None = None,
    policy: AccessPolicy | None = None,
) -> tuple[httpx.AsyncClient, RecordingGateway]:
    app, selected_gateway = _app(gateway, policy)
    return (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ),
        selected_gateway,
    )


def _access_policy(tmp_path: Path) -> AccessPolicy:
    policy_file = tmp_path / "access.yaml"
    policy_file.write_text(
        """
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: OPERATOR_TOKEN
    operator: true
""".strip(),
        encoding="utf-8",
    )
    return AccessPolicy.load(
        policy_file,
        {
            "QUERY_TOKEN": _QUERY_TOKEN,
            "OPERATOR_TOKEN": _OPERATOR_TOKEN,
        },
    )


def _query_payload(sql: str = "SELECT count(*) FROM ai.items") -> dict[str, str]:
    return {
        "source_id": "source-a",
        "sql": sql,
        "metadata_revision": _METADATA_REVISION,
        "sql_policy_revision": _SQL_POLICY_REVISION,
    }


def test_exposes_only_the_http_api_surface() -> None:
    app, _gateway = _app()
    paths = app.openapi()["paths"]

    assert {
        path: set(operations)
        for path, operations in paths.items()
    } == {
        "/health": {"get"},
        "/ready": {"get"},
        "/admin/health": {"get"},
        "/admin/metrics": {"get"},
        "/sources": {"get"},
        "/meta": {"post"},
        "/query": {"post"},
        "/queries/{query_id}": {"delete"},
    }


@pytest.mark.asyncio
async def test_health_and_readiness_are_public_and_inventory_free() -> None:
    session, _gateway = _client(policy=AccessPolicy.legacy(_QUERY_TOKEN))
    async with session:
        health = await session.get("/health")
        initializing = await session.get("/ready")
        operations.reconcile_sources(["private-source-id"])
        operations.set_source_health("private-source-id", "healthy")
        ready = await session.get("/ready")

    assert health.json() == {"status": "ok"}
    assert initializing.status_code == 503
    assert initializing.json() == {"status": "initializing"}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert "private-source-id" not in ready.text


@pytest.mark.asyncio
async def test_routes_source_metadata_and_query_to_the_gateway() -> None:
    session, gateway = _client()
    async with session:
        sources = await session.get("/sources")
        context = await session.post("/meta", json={"source_id": "source-a"})
        query = await session.post("/query", json=_query_payload())

    local = CallerContext("local-development", "local-development")
    assert sources.json()["sources"][0]["source_id"] == "source-a"
    assert context.json()["metadata_revision"] == _METADATA_REVISION
    assert query.status_code == 200
    assert query.json()["rows"] == [{"count": 1}]
    assert gateway.calls == [
        ("sources", local),
        ("meta", local, "source-a"),
        (
            "query",
            local,
            "source-a",
            "SELECT count(*) FROM ai.items",
            _METADATA_REVISION,
            _SQL_POLICY_REVISION,
        ),
    ]


@pytest.mark.asyncio
async def test_opaque_bearer_authentication_is_fail_closed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="query_man.audit")
    session, _gateway = _client(policy=AccessPolicy.legacy(_QUERY_TOKEN))
    invalid_requests: list[dict[str, Any]] = [
        {},
        {"headers": {"authorization": "Basic private"}},
        {"headers": {"authorization": "Bearer wrong-token-value-with-at-least-32-characters"}},
        {
            "headers": [
                ("authorization", f"Bearer {_QUERY_TOKEN}"),
                ("authorization", f"Bearer {_QUERY_TOKEN}"),
            ]
        },
    ]

    async with session:
        rejected = [
            await session.get("/sources", **request)
            for request in invalid_requests
        ]
        accepted = await session.get(
            "/sources",
            headers={"authorization": f"Bearer {_QUERY_TOKEN}"},
        )

    assert all(response.status_code == 401 for response in rejected)
    assert all(response.json()["error"]["code"] == "UNAUTHORIZED" for response in rejected)
    assert all(
        response.headers["www-authenticate"] == 'Bearer error="invalid_token"'
        for response in rejected
    )
    assert accepted.status_code == 200
    assert "authentication_failed" in caplog.text
    assert _QUERY_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_operator_endpoints_enforce_operator_identity(tmp_path: Path) -> None:
    policy = _access_policy(tmp_path)
    operations.reconcile_sources(["source-a"])
    operations.set_source_health("source-a", "healthy")
    operations.increment("query_execution_started", "source-a")
    session, gateway = _client(policy=policy)
    query_headers = {"authorization": f"Bearer {_QUERY_TOKEN}"}
    operator_headers = {"authorization": f"Bearer {_OPERATOR_TOKEN}"}

    async with session:
        denied_health = await session.get("/admin/health", headers=query_headers)
        denied_cancel = await session.delete(f"/queries/{_QUERY_ID}", headers=query_headers)
        health = await session.get("/admin/health", headers=operator_headers)
        metrics = await session.get("/admin/metrics", headers=operator_headers)
        invalid_id = await session.delete("/queries/not-a-uuid", headers=operator_headers)
        cancelled = await session.delete(f"/queries/{_QUERY_ID}", headers=operator_headers)

    assert denied_health.status_code == denied_cancel.status_code == 403
    assert denied_health.json()["error"]["code"] == "OPERATOR_REQUIRED"
    assert denied_health.headers["www-authenticate"] == 'Bearer error="insufficient_scope"'
    assert health.json() == {
        "status": "ready",
        "accepting": True,
        "sources": {"source-a": "healthy"},
    }
    assert metrics.json()["metrics"] == [
        {
            "name": "query_execution_started",
            "source_id": "source-a",
            "value": 1,
        }
    ]
    assert invalid_id.status_code == 400
    assert cancelled.json() == {
        "status": "cancel_requested",
        "query_id": _QUERY_ID,
    }
    assert gateway.calls == [
        (
            "cancel",
            CallerContext("operator", "operations", operator=True),
            _QUERY_ID,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/meta",
            {
                "source_id": "source-a",
                "question": "removed-field",
                "host": "private.example",
            },
        ),
        (
            "/query",
            {
                **_query_payload(),
                "tenant_id": "caller-selected-tenant",
                "budget_profile": "caller-selected-budget",
            },
        ),
        (
            "/query",
            {**_query_payload(), "metadata_revision": "not-a-revision"},
        ),
        (
            "/query",
            {**_query_payload(), "sql": ""},
        ),
    ],
)
async def test_request_validation_is_strict_bounded_and_non_disclosing(
    path: str,
    payload: dict[str, str],
) -> None:
    secret = "private-request-value"
    payload["unknown_field"] = secret
    session, gateway = _client()

    async with session:
        response = await session.post(path, json=payload)

    error = response.json()["error"]
    assert response.status_code == 400
    assert error["code"] == "INVALID_REQUEST"
    assert len(error["details"]) <= 32
    assert all(set(detail) == {"path", "code", "message"} for detail in error["details"])
    assert secret not in response.text
    assert not gateway.calls


@pytest.mark.asyncio
async def test_malformed_json_returns_a_bounded_error_without_echoing_body() -> None:
    secret = "private-malformed-json"
    session, gateway = _client()

    async with session:
        response = await session.post(
            "/meta",
            content=f'{{"source_id":"{secret}'.encode(),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert secret not in response.text
    assert not gateway.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "error", "expected_status", "expected_code"),
    [
        ("meta", SourceNotFoundError(), 404, "SOURCE_NOT_FOUND"),
        (
            "meta",
            MetadataUnavailableError({"private_database_detail": "private-view"}),
            503,
            "METADATA_UNAVAILABLE",
        ),
        ("query", QueryUnavailableError(), 503, "QUERY_UNAVAILABLE"),
        (
            "sources",
            RuntimeError("connect host=10.20.30.40 password=private-password"),
            500,
            "INTERNAL_ERROR",
        ),
    ],
)
async def test_gateway_failures_have_stable_non_disclosing_responses(
    operation: str,
    error: Exception,
    expected_status: int,
    expected_code: str,
) -> None:
    gateway = RecordingGateway()
    gateway.failures[operation] = error
    session, _gateway = _client(gateway)

    async with session:
        if operation == "sources":
            response = await session.get("/sources")
        elif operation == "meta":
            response = await session.post("/meta", json={"source_id": "private-source"})
        else:
            response = await session.post(
                "/query",
                json=_query_payload("SELECT 'private-sql-literal'"),
            )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    for private_value in (
        "private-source",
        "private-view",
        "10.20.30.40",
        "private-password",
        "private-sql-literal",
    ):
        assert private_value not in response.text


@pytest.mark.asyncio
async def test_revision_and_policy_rejections_do_not_echo_sql() -> None:
    secret = "private-sql-literal"
    gateway = RecordingGateway()
    session, _gateway = _client(gateway)

    gateway.failures["query"] = MetadataRevisionMismatchError()
    async with session:
        stale = await session.post(
            "/query",
            json=_query_payload(f"SELECT '{secret}'"),
        )
        gateway.failures["query"] = QueryRejectedError(
            "SQL_OPERATOR_NOT_ALLOWED",
            rejected_construct="NOT BETWEEN",
        )
        rejected = await session.post(
            "/query",
            json=_query_payload(f"SELECT 1 WHERE '{secret}' NOT BETWEEN 'a' AND 'b'"),
        )

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "METADATA_REVISION_MISMATCH"
    assert rejected.status_code == 400
    assert rejected.json()["error"]["details"] == {
        "reason_code": "SQL_OPERATOR_NOT_ALLOWED",
        "rejected_construct": "NOT BETWEEN",
    }
    assert secret not in stale.text
    assert secret not in rejected.text


@pytest.mark.asyncio
async def test_shutdown_refuses_new_work_but_keeps_health_visible() -> None:
    operations.set_accepting(False)
    session, gateway = _client()

    async with session:
        health = await session.get("/health")
        ready = await session.get("/ready")
        sources = await session.get("/sources")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json() == {"status": "shutting_down"}
    assert sources.status_code == 503
    assert sources.json()["error"]["code"] == "SERVICE_SHUTTING_DOWN"
    assert not gateway.calls


async def _post_then_disconnect(
    app: FastAPI,
    payload: dict[str, str],
) -> list[dict[str, Any]]:
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put(
        {
            "type": "http.request",
            "body": json.dumps(payload).encode(),
            "more_body": False,
        }
    )
    await incoming.put({"type": "http.disconnect"})
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/query",
            "raw_path": b"/query",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"test"),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "state": {},
        },
        receive,
        send,
    )
    return sent


@pytest.mark.asyncio
async def test_client_disconnect_cancels_inflight_query() -> None:
    gateway = RecordingGateway()
    gateway.hang_query = True
    app, _gateway = _app(gateway)

    sent = await asyncio.wait_for(
        _post_then_disconnect(app, _query_payload()),
        timeout=1,
    )

    assert gateway.query_started.is_set()
    assert gateway.query_cancelled.is_set()
    response_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 408
