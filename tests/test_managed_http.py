from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from query_man.access import AccessPolicy
from query_man.errors import (
    MutationNotFoundError,
    SourceControlUnavailableError,
    SourceNotFoundError,
)
from query_man.managed.runtime import build_app as build_managed_app
from query_man.managed.source_admin import (
    CONTROL_SEQUENCE_MAX,
    MutationContext,
    PublishVerifiedQueryInput,
    VerifiedExpectedInput,
)
from query_man.models import RuntimeCatalogProvider
from query_man.query import RuntimeQueryExecutor
from query_man.runtime_config import RuntimeConfig
from tests.helpers import load_test_registry
from tests.test_http import (
    _ADMIN_TOKEN,
    _QUERY_A_TOKEN,
    _QUERY_B_TOKEN,
    NeverCalledCatalog,
    RecordingQueryExecutor,
    _shared_access_policy,
    runtime_config,
)

_SOURCE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def test_admin_routes_only_import_public_control_interface() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "query_man"
        / "managed"
        / "source_admin_routes.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    public_control_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_modules.add(module)
            imported_modules.update(
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
            )
            if module == "query_man.managed.source_admin":
                public_control_names.update(alias.name for alias in node.names)

    for forbidden in (
        "query_man.managed.source_store",
        "query_man.verified",
    ):
        assert not any(
            imported == forbidden or imported.startswith(f"{forbidden}.")
            for imported in imported_modules
        )
    assert {
        "CONTROL_SEQUENCE_MAX",
        "PublishVerifiedQueryInput",
        "VerifiedExpectedInput",
    } <= public_control_names


def build_app(
    config: RuntimeConfig,
    *,
    registry: object | None = None,
    catalog: RuntimeCatalogProvider | None = None,
    query_executor: RuntimeQueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
) -> FastAPI:
    del registry
    return build_managed_app(
        replace(
            config,
            source_mode="managed",
            control_dsn="host=control.invalid dbname=query_man",
            source_encryption_key=_SOURCE_KEY,
            replica_id="managed-http-test",
        ),
        catalog=catalog,
        query_executor=query_executor,
        access_policy=access_policy,
    )


def client(
    catalog: RuntimeCatalogProvider,
    api_token: str | None = None,
    query_executor: RuntimeQueryExecutor | None = None,
    access_policy: AccessPolicy | None = None,
) -> httpx.AsyncClient:
    app = build_app(
        runtime_config(api_token),
        registry=load_test_registry(),
        catalog=catalog,
        query_executor=query_executor,
        access_policy=access_policy,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


_MUTATION_KEYS = (
    "4a3fcf7e-818f-4b68-9d31-21d392a28701",
    "4a3fcf7e-818f-4b68-9d31-21d392a28702",
    "4a3fcf7e-818f-4b68-9d31-21d392a28703",
    "4a3fcf7e-818f-4b68-9d31-21d392a28704",
    "4a3fcf7e-818f-4b68-9d31-21d392a28705",
    "4a3fcf7e-818f-4b68-9d31-21d392a28706",
)
_UNKNOWN_MUTATION_KEY = "d2e42ce6-c8f4-49e3-9107-e744db9c43bf"
_UNAVAILABLE_MUTATION_KEY = "ee4b5cc3-1688-4c6f-930c-9844be367c81"


def _operator_mutation_headers(
    idempotency_key: str,
    *,
    reason: str = "ctrl-205",
    expected_generation: int = 7,
    expected_state_version: int = 9,
    expected_metadata_revision: str | None = None,
) -> dict[str, str]:
    headers = {
        "authorization": f"Bearer {_ADMIN_TOKEN}",
        "idempotency-key": idempotency_key,
        "x-query-man-reason": reason,
        "x-expected-generation": str(expected_generation),
        "x-expected-state-version": str(expected_state_version),
    }
    if expected_metadata_revision is not None:
        headers["x-expected-metadata-revision"] = expected_metadata_revision
    return headers


class RecordingSourceAdmin:
    def __init__(self) -> None:
        self.list_calls: list[tuple[object, ...]] = []
        self.detail_calls: list[str] = []
        self.history_calls: list[tuple[object, ...]] = []
        self.replica_calls: list[tuple[object, ...]] = []
        self.usage_calls: list[str] = []
        self.receipt_calls: list[str] = []
        self.source_mutation_calls: list[tuple[object, ...]] = []
        self.mutation_calls: list[tuple[object, ...]] = []

    async def list_sources(
        self,
        limit: int = 50,
        after_source_id: str | None = None,
        enabled: bool | None = None,
        owner: str | None = None,
        environment: str | None = None,
        budget_profile: str | None = None,
    ) -> dict[str, object]:
        self.list_calls.append(
            (
                limit,
                after_source_id,
                enabled,
                owner,
                environment,
                budget_profile,
            )
        )
        return {"sources": [], "next_after_source_id": None}

    async def get_source(self, source_id: str) -> dict[str, object]:
        self.detail_calls.append(source_id)
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private control database detail"
            )
        return {"source_id": source_id, "connection": {"ssl": True}}

    async def source_history(
        self,
        source_id: str,
        limit: int = 50,
        before_generation: int | None = None,
    ) -> dict[str, object]:
        self.history_calls.append((source_id, limit, before_generation))
        if source_id == "unknown-source":
            raise SourceNotFoundError
        return {
            "source_id": source_id,
            "current": {"generation": 2},
            "generations": [],
            "next_before_generation": None,
        }

    async def source_replicas(
        self,
        source_id: str,
        limit: int = 50,
        after_replica_id: str | None = None,
    ) -> dict[str, object]:
        self.replica_calls.append((source_id, limit, after_replica_id))
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private replica observation detail"
            )
        replicas: list[dict[str, object]] = []
        if source_id == "known-source":
            replicas.append(
                {
                    "replica_id": "replica-alpha",
                    "status": "available",
                    "source_health": "healthy",
                    "applied": {
                        "enabled": True,
                        "generation": 7,
                        "state_version": 9,
                        "metadata_revision": f"sha256:{'1' * 64}",
                    },
                    "drift": [],
                    "observed_at": "2026-08-25T12:00:00+00:00",
                    "fresh_until": "2026-08-25T12:00:15+00:00",
                    "stale_age_ms": 0,
                    "reason_code": None,
                }
            )
        elif source_id == "replica-status-source":
            replicas.extend(
                [
                    {
                        "replica_id": "replica-stale",
                        "status": "stale",
                        "source_health": "healthy",
                        "applied": {
                            "enabled": True,
                            "generation": 6,
                            "state_version": 8,
                            "metadata_revision": f"sha256:{'2' * 64}",
                        },
                        "drift": ["generation", "state_version", "metadata_revision"],
                        "observed_at": "2026-08-25T11:59:00+00:00",
                        "fresh_until": "2026-08-25T11:59:15+00:00",
                        "stale_age_ms": 45_000,
                        "reason_code": "HEARTBEAT_EXPIRED",
                    },
                    {
                        "replica_id": "replica-unavailable",
                        "status": "unavailable",
                        "source_health": None,
                        "applied": None,
                        "drift": ["not_applied"],
                        "observed_at": "2026-08-25T12:00:00+00:00",
                        "fresh_until": "2026-08-25T12:00:15+00:00",
                        "stale_age_ms": 0,
                        "reason_code": "CONTROL_SCAN_FAILED",
                    },
                ]
            )
        return {
            "source_id": source_id,
            "desired": {
                "enabled": True,
                "generation": 7,
                "state_version": 9,
                "metadata_revision": f"sha256:{'1' * 64}",
            },
            "replicas": replicas,
            "next_after_replica_id": None,
        }

    async def source_usage(self, source_id: str) -> dict[str, object]:
        self.usage_calls.append(source_id)
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private usage projection detail"
            )
        return {
            "source_id": source_id,
            "enabled": True,
            "read_at": "2026-08-25T12:00:00+00:00",
            "resource": {
                "status": "pending",
                "reason_code": "NOT_OBSERVED",
                "last_attempt": None,
                "fresh_until": None,
                "metrics": [],
            },
            "gateway": {
                "status": "pending",
                "reason_code": "NOT_REPORTED",
                "last_report_at": None,
                "fresh_until": None,
                "lower_bound": True,
                "window_start": "2026-07-25T12:00:00+00:00",
                "window_end": "2026-08-25T12:00:00+00:00",
                "rollups": [],
            },
            "monetary_cost": {
                "status": "not_configured",
                "reason_code": "PROVIDER_NOT_CONFIGURED",
                "last_attempt": None,
            },
        }

    async def get_mutation(self, idempotency_key: str) -> dict[str, object]:
        self.receipt_calls.append(idempotency_key)
        if idempotency_key == _UNKNOWN_MUTATION_KEY:
            raise MutationNotFoundError
        if idempotency_key == _UNAVAILABLE_MUTATION_KEY:
            raise SourceControlUnavailableError from RuntimeError(
                "private mutation receipt detail"
            )
        return {
            "idempotency_key": idempotency_key,
            "outcome": "succeeded",
        }

    async def source_mutations(
        self,
        source_id: str,
        limit: int = 50,
        before_event_id: int | None = None,
    ) -> dict[str, object]:
        self.source_mutation_calls.append((source_id, limit, before_event_id))
        if source_id == "unknown-source":
            raise SourceNotFoundError
        if source_id == "unavailable-source":
            raise SourceControlUnavailableError from RuntimeError(
                "private source mutation detail"
            )
        return {
            "source_id": source_id,
            "mutations": [],
            "next_before_event_id": 41,
        }

    async def publish(
        self,
        source_id: str,
        manifest: object,
        credential: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("publish", source_id, manifest, credential, mutation)
        )
        return {"status": "published", "source_id": source_id}

    async def rotate_credential(
        self,
        source_id: str,
        credential: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("rotate_credential", source_id, credential, mutation)
        )
        return {"status": "credential_rotated", "source_id": source_id}

    async def publish_verified_query(
        self,
        query: PublishVerifiedQueryInput,
        tenant_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("publish_verified_query", query, tenant_id, mutation)
        )
        return {"status": "verified", "source_id": query.source_id}

    async def rollback(
        self,
        source_id: str,
        generation: int,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(
            ("rollback", source_id, generation, mutation)
        )
        return {"status": "rolled_back", "source_id": source_id}

    async def resume_automatic_publish(
        self,
        source_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(("resume", source_id, mutation))
        return {"status": "resumed", "source_id": source_id}

    async def deactivate(
        self,
        source_id: str,
        mutation: MutationContext | None = None,
    ) -> dict[str, object]:
        self.mutation_calls.append(("deactivate", source_id, mutation))
        return {"status": "deactivated", "source_id": source_id}


@pytest.mark.asyncio
async def test_query_credentials_reject_every_admin_operation_and_cancel(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    credential = "must-not-appear-in-response"
    query_id = "30c7b03d-659d-47d4-b6f5-cb2ea9a9eaf0"
    executor = RecordingQueryExecutor()
    executor.cancel_result = True
    requests: list[tuple[str, str, dict[str, object] | None]] = [
        ("GET", "/admin/health", None),
        ("GET", "/admin/metrics", None),
        ("GET", "/admin/sources", None),
        ("GET", "/admin/sources?limit=0", None),
        ("GET", "/admin/sources/INVALID_SOURCE", None),
        ("GET", "/admin/sources/unknown-source", None),
        ("GET", "/admin/sources/unknown-source/history", None),
        ("GET", "/admin/sources/INVALID_SOURCE/replicas?limit=0", None),
        ("GET", "/admin/sources/INVALID_SOURCE/usage?unexpected=value", None),
        ("GET", "/admin/sources/unknown-source/mutations?before_event_id=0", None),
        ("GET", f"/admin/mutations/{_MUTATION_KEYS[0]}", None),
        ("GET", "/admin/mutations/not-a-canonical-uuid", None),
        (
            "PUT",
            "/admin/sources/new-source",
            {"manifest": {}, "credential": credential},
        ),
        (
            "POST",
            "/admin/sources/new-source/credential",
            {"credential": credential},
        ),
        (
            "POST",
            "/admin/sources/new-source/verified-queries",
            {
                "query_id": "admin-boundary-check",
                "question": "관리 경계를 검증해줘",
                "metadata_revision": f"sha256:{'0' * 64}",
                "relations": ["ai.issue_overview"],
                "sql": "SELECT issue_id FROM ai.issue_overview LIMIT 1",
                "expected": {
                    "columns": ["issue_id"],
                    "row_count": 0,
                    "result_hash": f"sha256:{'0' * 64}",
                },
            },
        ),
        ("POST", "/admin/sources/new-source/rollback/1", None),
        ("POST", "/admin/sources/new-source/metadata/resume", None),
        ("DELETE", "/admin/sources/new-source", None),
        ("DELETE", f"/queries/{query_id}", None),
        ("DELETE", "/queries/not-a-uuid", None),
    ]
    async with client(
        NeverCalledCatalog(),
        query_executor=executor,
        access_policy=policy,
    ) as session:
        for token in (_QUERY_A_TOKEN, _QUERY_B_TOKEN):
            headers = {"authorization": f"Bearer {token}"}
            for method, path, body in requests:
                options = {} if body is None else {"json": body}
                response = await session.request(
                    method,
                    path,
                    headers=headers,
                    **options,  # type: ignore[arg-type]
                )
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["error"]["code"] == "OPERATOR_REQUIRED"
                assert credential not in response.text

            mutation_headers = {
                **_operator_mutation_headers(_MUTATION_KEYS[0]),
                "authorization": f"Bearer {token}",
                "content-type": "application/json",
            }
            malformed_body = await session.put(
                "/admin/sources/new-source",
                headers=mutation_headers,
                content=b"{",
            )
            malformed_path = await session.post(
                "/admin/sources/new-source/rollback/not-a-generation",
                headers=mutation_headers,
            )
            duplicated_header = await session.post(
                "/admin/sources/new-source/credential",
                headers=[
                    ("authorization", f"Bearer {token}"),
                    ("idempotency-key", _MUTATION_KEYS[0]),
                    ("idempotency-key", _MUTATION_KEYS[1]),
                    ("x-query-man-reason", "ctrl-205"),
                    ("x-expected-generation", "7"),
                    ("x-expected-state-version", "9"),
                ],
                json={"credential": credential},
            )
            for response in (malformed_body, malformed_path, duplicated_header):
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "OPERATOR_REQUIRED"
                assert credential not in response.text

        admin_headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
        health = await session.get("/admin/health", headers=admin_headers)
        metrics = await session.get("/admin/metrics", headers=admin_headers)
        cancelled = await session.delete(
            f"/queries/{query_id}",
            headers=admin_headers,
        )
        malformed_cancel = await session.delete(
            "/queries/not-a-uuid",
            headers=admin_headers,
        )

    assert health.status_code == 200
    assert metrics.status_code == 200
    assert cancelled.status_code == 200
    assert cancelled.json() == {"status": "cancel_requested", "query_id": query_id}
    assert malformed_cancel.status_code == 400
    assert malformed_cancel.json()["error"]["code"] == "INVALID_REQUEST"
    assert executor.cancel_calls == [query_id]


@pytest.mark.asyncio
async def test_admin_mutation_and_receipt_routes_require_authentication_before_validation(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    requests = [
        ("PUT", "/admin/sources/INVALID_SOURCE"),
        ("POST", "/admin/sources/INVALID_SOURCE/credential"),
        ("POST", "/admin/sources/INVALID_SOURCE/verified-queries"),
        ("POST", "/admin/sources/INVALID_SOURCE/rollback/not-a-generation"),
        ("POST", "/admin/sources/INVALID_SOURCE/metadata/resume"),
        ("DELETE", "/admin/sources/INVALID_SOURCE"),
        ("GET", "/admin/mutations/not-a-canonical-uuid"),
        ("GET", "/admin/sources/INVALID_SOURCE/mutations?limit=0"),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        responses = [
            await session.request(method, path, content=b"{")
            for method, path in requests
        ]

    assert all(response.status_code == 401 for response in responses)
    assert all(response.json()["error"]["code"] == "UNAUTHORIZED" for response in responses)
    assert admin.receipt_calls == []
    assert admin.source_mutation_calls == []
    assert admin.mutation_calls == []


@pytest.mark.asyncio
async def test_replica_admin_authenticates_and_authorizes_before_validation(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    invalid_path = "/admin/sources/INVALID_SOURCE/replicas?limit=0&unexpected=value"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        unauthenticated = await session.get(invalid_path)
        query_caller = await session.get(
            invalid_path,
            headers={"authorization": f"Bearer {_QUERY_A_TOKEN}"},
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "UNAUTHORIZED"
    assert query_caller.status_code == 403
    assert query_caller.json()["error"]["code"] == "OPERATOR_REQUIRED"
    assert admin.replica_calls == []


@pytest.mark.asyncio
async def test_operator_replica_admin_forwards_and_preserves_projection(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        default_page = await session.get(
            "/admin/sources/known-source/replicas",
            headers=headers,
        )
        explicit_page = await session.get(
            "/admin/sources/known-source/replicas",
            headers=headers,
            params={"limit": "7", "after_replica_id": "replica-alpha"},
        )
        empty_page = await session.get(
            "/admin/sources/empty-source/replicas",
            headers=headers,
        )
        status_page = await session.get(
            "/admin/sources/replica-status-source/replicas",
            headers=headers,
        )

    assert all(
        response.status_code == 200
        for response in (default_page, explicit_page, empty_page, status_page)
    )
    assert admin.replica_calls == [
        ("known-source", 50, None),
        ("known-source", 7, "replica-alpha"),
        ("empty-source", 50, None),
        ("replica-status-source", 50, None),
    ]
    expected_available = {
        "source_id": "known-source",
        "desired": {
            "enabled": True,
            "generation": 7,
            "state_version": 9,
            "metadata_revision": f"sha256:{'1' * 64}",
        },
        "replicas": [
            {
                "replica_id": "replica-alpha",
                "status": "available",
                "source_health": "healthy",
                "applied": {
                    "enabled": True,
                    "generation": 7,
                    "state_version": 9,
                    "metadata_revision": f"sha256:{'1' * 64}",
                },
                "drift": [],
                "observed_at": "2026-08-25T12:00:00+00:00",
                "fresh_until": "2026-08-25T12:00:15+00:00",
                "stale_age_ms": 0,
                "reason_code": None,
            }
        ],
        "next_after_replica_id": None,
    }
    assert default_page.json() == explicit_page.json() == expected_available
    assert empty_page.json() == {
        "source_id": "empty-source",
        "desired": expected_available["desired"],
        "replicas": [],
        "next_after_replica_id": None,
    }
    assert status_page.json() == {
        "source_id": "replica-status-source",
        "desired": expected_available["desired"],
        "replicas": [
            {
                "replica_id": "replica-stale",
                "status": "stale",
                "source_health": "healthy",
                "applied": {
                    "enabled": True,
                    "generation": 6,
                    "state_version": 8,
                    "metadata_revision": f"sha256:{'2' * 64}",
                },
                "drift": ["generation", "state_version", "metadata_revision"],
                "observed_at": "2026-08-25T11:59:00+00:00",
                "fresh_until": "2026-08-25T11:59:15+00:00",
                "stale_age_ms": 45_000,
                "reason_code": "HEARTBEAT_EXPIRED",
            },
            {
                "replica_id": "replica-unavailable",
                "status": "unavailable",
                "source_health": None,
                "applied": None,
                "drift": ["not_applied"],
                "observed_at": "2026-08-25T12:00:00+00:00",
                "fresh_until": "2026-08-25T12:00:15+00:00",
                "stale_age_ms": 0,
                "reason_code": "CONTROL_SCAN_FAILED",
            },
        ],
        "next_after_replica_id": None,
    }


@pytest.mark.asyncio
async def test_operator_replica_admin_returns_bounded_errors(tmp_path: Path) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    invalid_paths = [
        "/admin/sources/known-source/replicas?limit=0",
        "/admin/sources/known-source/replicas?limit=101",
        "/admin/sources/known-source/replicas?after_replica_id=",
        "/admin/sources/known-source/replicas?after_replica_id=Invalid_Replica",
        f"/admin/sources/known-source/replicas?after_replica_id={'a' * 81}",
        "/admin/sources/known-source/replicas?after_replica_id=%20replica-alpha%20",
        "/admin/sources/known-source/replicas?unexpected=value",
        "/admin/sources/known-source/replicas?limit=10&limit=20",
        (
            "/admin/sources/known-source/replicas?"
            "after_replica_id=replica-a&after_replica_id=replica-b"
        ),
        "/admin/sources/INVALID_SOURCE/replicas",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        invalid = [
            await session.get(path, headers=headers) for path in invalid_paths
        ]
        unknown = await session.get(
            "/admin/sources/unknown-source/replicas",
            headers=headers,
        )
        unavailable = await session.get(
            "/admin/sources/unavailable-source/replicas",
            headers=headers,
        )

    assert all(response.status_code == 400 for response in invalid)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in invalid
    )
    assert all(len(response.content) < 4_096 for response in invalid)
    assert admin.replica_calls == [
        ("unknown-source", 50, None),
        ("unavailable-source", 50, None),
    ]
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "SOURCE_CONTROL_UNAVAILABLE"
    assert "private replica observation detail" not in unavailable.text


@pytest.mark.asyncio
async def test_usage_admin_authenticates_and_authorizes_before_validation(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    invalid_path = "/admin/sources/INVALID_SOURCE/usage?unexpected=value"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        unauthenticated = await session.get(invalid_path)
        query_caller = await session.get(
            invalid_path,
            headers={"authorization": f"Bearer {_QUERY_A_TOKEN}"},
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "UNAUTHORIZED"
    assert query_caller.status_code == 403
    assert query_caller.json()["error"]["code"] == "OPERATOR_REQUIRED"
    assert admin.usage_calls == []


@pytest.mark.asyncio
async def test_operator_usage_admin_preserves_control_projection(
    tmp_path: Path,
) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        response = await session.get(
            "/admin/sources/known-source/usage",
            headers=headers,
        )

    assert response.status_code == 200
    assert admin.usage_calls == ["known-source"]
    assert response.json() == {
        "source_id": "known-source",
        "enabled": True,
        "read_at": "2026-08-25T12:00:00+00:00",
        "resource": {
            "status": "pending",
            "reason_code": "NOT_OBSERVED",
            "last_attempt": None,
            "fresh_until": None,
            "metrics": [],
        },
        "gateway": {
            "status": "pending",
            "reason_code": "NOT_REPORTED",
            "last_report_at": None,
            "fresh_until": None,
            "lower_bound": True,
            "window_start": "2026-07-25T12:00:00+00:00",
            "window_end": "2026-08-25T12:00:00+00:00",
            "rollups": [],
        },
        "monetary_cost": {
            "status": "not_configured",
            "reason_code": "PROVIDER_NOT_CONFIGURED",
            "last_attempt": None,
        },
    }


@pytest.mark.asyncio
async def test_operator_usage_admin_returns_bounded_errors(tmp_path: Path) -> None:
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=_shared_access_policy(tmp_path),
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    invalid_paths = [
        "/admin/sources/known-source/usage?unexpected=value",
        "/admin/sources/known-source/usage?limit=50",
        "/admin/sources/known-source/usage?unexpected=a&unexpected=b",
        "/admin/sources/INVALID_SOURCE/usage",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        invalid = [
            await session.get(path, headers=headers) for path in invalid_paths
        ]
        unknown = await session.get(
            "/admin/sources/unknown-source/usage",
            headers=headers,
        )
        unavailable = await session.get(
            "/admin/sources/unavailable-source/usage",
            headers=headers,
        )

    assert all(response.status_code == 400 for response in invalid)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in invalid
    )
    assert all(len(response.content) < 4_096 for response in invalid)
    assert admin.usage_calls == ["unknown-source", "unavailable-source"]
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "SOURCE_CONTROL_UNAVAILABLE"
    assert "private usage projection detail" not in unavailable.text


@pytest.mark.asyncio
async def test_operator_mutation_routes_forward_context_and_payload(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    manifest = {"version": 1, "source_id": "new-source"}
    credential = "postgresql://source-admin-secret"
    metadata_revision = f"sha256:{'1' * 64}"
    result_hash = f"sha256:{'2' * 64}"
    verified_payload = {
        "query_id": "known-source-check",
        "question": "Check the known source",
        "metadata_revision": metadata_revision,
        "relations": ["ai.issue_overview"],
        "sql": "SELECT issue_id FROM ai.issue_overview LIMIT 1",
        "expected": {
            "columns": ["issue_id"],
            "row_count": 1,
            "result_hash": result_hash,
        },
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        published = await session.put(
            "/admin/sources/new-source",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[0],
                reason="ctrl-205.publish",
                expected_generation=0,
                expected_state_version=0,
            ),
            json={"manifest": manifest, "credential": credential},
        )
        rotated = await session.post(
            "/admin/sources/known-source/credential",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[1],
                reason="ctrl-205.rotate",
            ),
            json={"credential": credential},
        )
        verified = await session.post(
            "/admin/sources/known-source/verified-queries",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[2],
                reason="ctrl-205.verify",
            ),
            json=verified_payload,
        )
        rolled_back = await session.post(
            "/admin/sources/known-source/rollback/3",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[3],
                reason="ctrl-205.rollback",
            ),
        )
        resumed = await session.post(
            "/admin/sources/known-source/metadata/resume",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[4],
                reason="ctrl-205.resume",
                expected_metadata_revision=metadata_revision,
            ),
        )
        deactivated = await session.delete(
            "/admin/sources/known-source",
            headers=_operator_mutation_headers(
                _MUTATION_KEYS[5],
                reason="ctrl-205.deactivate",
            ),
        )

    assert all(
        response.status_code == 200
        for response in (
            published,
            rotated,
            verified,
            rolled_back,
            resumed,
            deactivated,
        )
    )
    assert admin.mutation_calls == [
        (
            "publish",
            "new-source",
            manifest,
            credential,
            MutationContext(
                idempotency_key=_MUTATION_KEYS[0],
                actor="admin",
                reason="ctrl-205.publish",
                expected_generation=0,
                expected_state_version=0,
            ),
        ),
        (
            "rotate_credential",
            "known-source",
            credential,
            MutationContext(
                idempotency_key=_MUTATION_KEYS[1],
                actor="admin",
                reason="ctrl-205.rotate",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
        (
            "publish_verified_query",
            PublishVerifiedQueryInput(
                query_id="known-source-check",
                source_id="known-source",
                question="Check the known source",
                sql="SELECT issue_id FROM ai.issue_overview LIMIT 1",
                metadata_revision=metadata_revision,
                relations=("ai.issue_overview",),
                expected=VerifiedExpectedInput(
                    columns=("issue_id",),
                    row_count=1,
                    result_hash=result_hash,
                ),
            ),
            "operations",
            MutationContext(
                idempotency_key=_MUTATION_KEYS[2],
                actor="admin",
                reason="ctrl-205.verify",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
        (
            "rollback",
            "known-source",
            3,
            MutationContext(
                idempotency_key=_MUTATION_KEYS[3],
                actor="admin",
                reason="ctrl-205.rollback",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
        (
            "resume",
            "known-source",
            MutationContext(
                idempotency_key=_MUTATION_KEYS[4],
                actor="admin",
                reason="ctrl-205.resume",
                expected_generation=7,
                expected_state_version=9,
                expected_metadata_revision=metadata_revision,
            ),
        ),
        (
            "deactivate",
            "known-source",
            MutationContext(
                idempotency_key=_MUTATION_KEYS[5],
                actor="admin",
                reason="ctrl-205.deactivate",
                expected_generation=7,
                expected_state_version=9,
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_operator_mutation_routes_reject_invalid_contract_before_service(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    credential = "must-not-be-disclosed"
    metadata_revision = f"sha256:{'1' * 64}"
    existing_headers = _operator_mutation_headers(_MUTATION_KEYS[0])
    absent_headers = _operator_mutation_headers(
        _MUTATION_KEYS[0],
        expected_generation=0,
        expected_state_version=0,
    )
    missing_header = dict(existing_headers)
    del missing_header["x-query-man-reason"]
    malformed_key = {
        **existing_headers,
        "idempotency-key": "NOT-A-CANONICAL-UUID",
    }
    unsafe_reason = {
        **existing_headers,
        "x-query-man-reason": "ticket contains private detail",
    }
    mismatched_state = {
        **existing_headers,
        "x-expected-state-version": "0",
    }
    overflow_state = {
        **existing_headers,
        "x-expected-generation": str(CONTROL_SEQUENCE_MAX + 1),
    }
    unexpected_revision = {
        **existing_headers,
        "x-expected-metadata-revision": metadata_revision,
    }
    duplicate_key_headers = [
        *existing_headers.items(),
        ("idempotency-key", _MUTATION_KEYS[1]),
    ]
    duplicate_content_type_headers = [
        *existing_headers.items(),
        ("content-type", "application/json"),
        ("content-type", "application/json"),
    ]
    ambiguous_content_types = (
        "application/json; charset=utf-8, text/plain",
        "application/json; charset",
        'application/json; charset="utf-8',
        "application/json; charset=utf-8; charset=utf-8",
    )
    duplicate_json_body = (
        b'{"credential":"discarded-secret","credential":"selected-secret"}'
    )
    oversized_extra_key_body = (
        b'{"credential":"safe","embedded-secret-'
        + b"x" * 65_536
        + b'":true}'
    )
    excessive_member_body = (
        b'{"credential":"safe",'
        + b",".join(f'"field-{index}":0'.encode() for index in range(2_000))
        + b"}"
    )
    non_finite_number_body = (
        b'{"manifest":{"source_id":1e999},"credential":"must-not-be-disclosed"}'
    )
    deeply_nested_body = b"[" * 10_000 + b"]" * 10_000
    invalid_requests: list[tuple[str, str, dict[str, object]]] = [
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": missing_header, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": malformed_key, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": unsafe_reason, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": mismatched_state, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": overflow_state, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": _operator_mutation_headers(
                    _MUTATION_KEYS[0],
                    expected_generation=0,
                    expected_state_version=0,
                ),
                "json": {"credential": credential},
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": unexpected_revision, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {"headers": duplicate_key_headers, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "text/plain"},
                "content": credential.encode(),
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": duplicate_content_type_headers,
                "content": b'{"credential":"safe"}',
            },
        ),
        *(
            (
                "POST",
                "/admin/sources/known-source/credential",
                {
                    "headers": {
                        **existing_headers,
                        "content-type": content_type,
                    },
                    "content": b'{"credential":"safe"}',
                },
            )
            for content_type in ambiguous_content_types
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": duplicate_json_body,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": oversized_extra_key_body,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": excessive_member_body,
            },
        ),
        (
            "PUT",
            "/admin/sources/new-source",
            {
                "headers": {**absent_headers, "content-type": "application/json"},
                "content": non_finite_number_body,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/credential",
            {
                "headers": {**existing_headers, "content-type": "application/json"},
                "content": deeply_nested_body,
            },
        ),
        (
            "PUT",
            "/admin/sources/new-source",
            {
                "headers": {**absent_headers, "content-type": "application/json"},
                "content": b"{",
            },
        ),
        (
            "PUT",
            "/admin/sources/new-source",
            {
                "headers": {**absent_headers, "content-type": "application/json"},
                "content": b"x" * 1_048_577,
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/verified-queries",
            {"headers": existing_headers, "json": {"credential": credential}},
        ),
        (
            "POST",
            "/admin/sources/known-source/rollback/3",
            {"headers": existing_headers, "content": b"unexpected"},
        ),
        (
            "POST",
            "/admin/sources/known-source/metadata/resume",
            {"headers": existing_headers},
        ),
        (
            "DELETE",
            "/admin/sources/known-source?unexpected=value",
            {"headers": existing_headers},
        ),
        (
            "PUT",
            "/admin/sources/INVALID_SOURCE",
            {
                "headers": absent_headers,
                "json": {"manifest": {}, "credential": credential},
            },
        ),
        (
            "POST",
            "/admin/sources/known-source/rollback/0",
            {"headers": existing_headers},
        ),
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        responses = [
            await session.request(method, path, **options)  # type: ignore[arg-type]
            for method, path, options in invalid_requests
        ]

    assert all(response.status_code == 400 for response in responses)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in responses
    )
    assert all(len(response.content) < 4_096 for response in responses)
    assert all(credential not in response.text for response in responses)
    assert "private detail" not in "".join(response.text for response in responses)
    assert "embedded-secret" not in "".join(response.text for response in responses)
    assert "discarded-secret" not in "".join(response.text for response in responses)
    assert "selected-secret" not in "".join(response.text for response in responses)
    assert admin.mutation_calls == []


@pytest.mark.asyncio
async def test_operator_admin_source_gets_validate_and_forward_queries(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        listed = await session.get(
            "/admin/sources",
            headers=headers,
            params={
                "limit": "25",
                "after_source_id": "first-source",
                "enabled": "false",
                "owner": "data-platform",
                "environment": "production",
                "budget_profile": "interactive",
            },
        )
        detail = await session.get("/admin/sources/known-source", headers=headers)
        history = await session.get(
            "/admin/sources/known-source/history",
            headers=headers,
            params={"limit": "10", "before_generation": "7"},
        )
        receipt = await session.get(
            f"/admin/mutations/{_MUTATION_KEYS[0]}",
            headers=headers,
        )
        mutations = await session.get(
            "/admin/sources/known-source/mutations",
            headers=headers,
            params={"limit": "12", "before_event_id": "42"},
        )

    assert all(
        response.status_code == 200
        for response in (listed, detail, history, receipt, mutations)
    )
    assert admin.list_calls == [
        (
            25,
            "first-source",
            False,
            "data-platform",
            "production",
            "interactive",
        )
    ]
    assert admin.detail_calls == ["known-source"]
    assert admin.history_calls == [("known-source", 10, 7)]
    assert admin.receipt_calls == [_MUTATION_KEYS[0]]
    assert admin.source_mutation_calls == [("known-source", 12, 42)]
    assert history.json()["source_id"] == "known-source"
    assert receipt.json()["idempotency_key"] == _MUTATION_KEYS[0]
    assert mutations.json()["next_before_event_id"] == 41


@pytest.mark.asyncio
async def test_operator_admin_source_gets_return_bounded_errors(
    tmp_path: Path,
) -> None:
    policy = _shared_access_policy(tmp_path)
    app = build_app(
        runtime_config(),
        registry=load_test_registry(),
        catalog=NeverCalledCatalog(),
        access_policy=policy,
    )
    admin = RecordingSourceAdmin()
    app.state.source_admin = admin
    headers = {"authorization": f"Bearer {_ADMIN_TOKEN}"}
    invalid_paths = [
        "/admin/sources?limit=0",
        "/admin/sources?limit=101",
        "/admin/sources?after_source_id=Invalid_Source",
        "/admin/sources?owner=Invalid_Owner",
        "/admin/sources?environment=preview",
        "/admin/sources?budget_profile=invalid-profile",
        f"/admin/sources?budget_profile={'a' * 64}",
        "/admin/sources?unexpected=value",
        "/admin/sources?limit=10&limit=20",
        "/admin/sources/INVALID_SOURCE",
        "/admin/sources/known-source?unexpected=value",
        "/admin/sources/known-source/history?before_generation=0",
        (
            "/admin/sources/known-source/history?before_generation="
            f"{CONTROL_SEQUENCE_MAX + 1}"
        ),
        "/admin/mutations/not-a-canonical-uuid",
        f"/admin/mutations/{_MUTATION_KEYS[0]}?unexpected=value",
        "/admin/sources/INVALID_SOURCE/mutations",
        "/admin/sources/known-source/mutations?limit=0",
        "/admin/sources/known-source/mutations?limit=101",
        "/admin/sources/known-source/mutations?before_event_id=0",
        (
            "/admin/sources/known-source/mutations?before_event_id="
            f"{CONTROL_SEQUENCE_MAX + 1}"
        ),
        "/admin/sources/known-source/mutations?unexpected=value",
        "/admin/sources/known-source/mutations?limit=10&limit=20",
    ]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as session:
        invalid = [
            await session.get(path, headers=headers) for path in invalid_paths
        ]
        unknown_detail = await session.get(
            "/admin/sources/unknown-source",
            headers=headers,
        )
        unknown_history = await session.get(
            "/admin/sources/unknown-source/history",
            headers=headers,
        )
        unavailable_detail = await session.get(
            "/admin/sources/unavailable-source",
            headers=headers,
        )
        unknown_receipt = await session.get(
            f"/admin/mutations/{_UNKNOWN_MUTATION_KEY}",
            headers=headers,
        )
        unavailable_receipt = await session.get(
            f"/admin/mutations/{_UNAVAILABLE_MUTATION_KEY}",
            headers=headers,
        )
        unknown_mutations = await session.get(
            "/admin/sources/unknown-source/mutations",
            headers=headers,
        )
        unavailable_mutations = await session.get(
            "/admin/sources/unavailable-source/mutations",
            headers=headers,
        )

    assert all(response.status_code == 400 for response in invalid)
    assert all(
        response.json()["error"]["code"] == "INVALID_REQUEST"
        for response in invalid
    )
    assert admin.list_calls == []
    assert admin.history_calls == [("unknown-source", 50, None)]
    assert admin.receipt_calls == [
        _UNKNOWN_MUTATION_KEY,
        _UNAVAILABLE_MUTATION_KEY,
    ]
    assert admin.source_mutation_calls == [
        ("unknown-source", 50, None),
        ("unavailable-source", 50, None),
    ]
    assert unknown_detail.status_code == unknown_history.status_code == 404
    assert unknown_detail.json()["error"]["code"] == "SOURCE_NOT_FOUND"
    assert unknown_history.json() == unknown_detail.json()
    assert unknown_receipt.status_code == 404
    assert unknown_receipt.json()["error"]["code"] == "MUTATION_NOT_FOUND"
    assert unknown_mutations.status_code == 404
    assert unknown_mutations.json() == unknown_detail.json()
    assert all(
        response.status_code == 503
        for response in (
            unavailable_detail,
            unavailable_receipt,
            unavailable_mutations,
        )
    )
    assert all(
        response.json()["error"]["code"] == "SOURCE_CONTROL_UNAVAILABLE"
        for response in (
            unavailable_detail,
            unavailable_receipt,
            unavailable_mutations,
        )
    )
    unavailable_text = "".join(
        response.text
        for response in (
            unavailable_detail,
            unavailable_receipt,
            unavailable_mutations,
        )
    )
    assert "private control database detail" not in unavailable_text
    assert "private mutation receipt detail" not in unavailable_text
    assert "private source mutation detail" not in unavailable_text
