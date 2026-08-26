from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI

import query_man.delivery.app as app_module
import query_man.managed.runtime as managed_runtime_module
from query_man.delivery.access import AccessPolicy
from query_man.guarded_query.query import RuntimeQueryExecutor
from query_man.metadata.models import (
    CatalogSnapshot,
    ResourceObservation,
    RuntimeCatalogProvider,
)
from query_man.runtime.config import RuntimeConfig
from query_man.source_catalog.models import SourceProfile
from tests.helpers import ROOT_DIRECTORY

_SOURCE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_QUERY_TOKEN = "query-token-value-with-at-least-32-characters"
_ADMIN_TOKEN = "admin-token-value-with-at-least-32-characters"


def _runtime(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=None,
        source_directory=tmp_path / "missing" / "sources",
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=0,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        source_mode="managed",
        control_dsn="host=control.invalid dbname=query_man",
        source_encryption_key=_SOURCE_KEY,
        source_reload_interval_ms=60_000,
        shutdown_grace_ms=30_000,
        replica_id="startup-cleanup-runtime",
    )


def _shared_policy(tmp_path: Path) -> AccessPolicy:
    policy_path = tmp_path / "access.yaml"
    policy_path.write_text(
        """
version: 2
callers:
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""".strip(),
        encoding="utf-8",
    )
    return AccessPolicy.load(
        policy_path,
        {"QUERY_TOKEN": _QUERY_TOKEN, "ADMIN_TOKEN": _ADMIN_TOKEN},
    )


def _build_failing_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_steps: frozenset[str],
    *,
    alias_query_and_catalog: bool = False,
) -> tuple[FastAPI, list[str], RuntimeError]:
    events: list[str] = []
    startup_error = RuntimeError("startup-secret-must-not-be-logged")

    def raise_if_failed(step: str) -> None:
        if step in failed_steps:
            raise RuntimeError(f"cleanup-secret-must-not-be-logged:{step}")

    class FakeMetadataStore:
        def __init__(self, _control_dsn: str) -> None:
            pass

        async def close(self) -> None:
            events.append("metadata_store_close")
            raise_if_failed("metadata")

    class FakeSourceStore:
        def __init__(self, _control_dsn: str) -> None:
            pass

        async def list_active(self) -> list[object]:
            return []

        async def verified_revision_map(self) -> dict[str, frozenset[str]]:
            return {}

        async def close(self) -> None:
            events.append("source_store_close")
            raise_if_failed("source_store")

    class FakeCatalog:
        async def load(self, _source: SourceProfile) -> CatalogSnapshot:
            raise AssertionError("catalog load is not expected")

        async def close(self) -> None:
            events.append("catalog_close")
            raise_if_failed("catalog")

        async def invalidate(self, _source_id: str) -> None:
            pass

        async def observe_resources(
            self,
            _source: SourceProfile,
        ) -> ResourceObservation:
            raise AssertionError("resource observation is not expected")

    class FakeQueryExecutor:
        def __init__(self) -> None:
            self.accepting = True

        def stop_accepting(self) -> None:
            self.accepting = False
            events.append("query_stop_accepting")

        async def drain(self, grace_ms: int) -> None:
            self.stop_accepting()
            events.append(f"query_drain:{grace_ms}")

        async def close(self) -> None:
            if self.accepting:
                await self.drain(0)
            events.append("query_close")
            raise_if_failed("query_executor")

        async def load(self, _source: SourceProfile) -> CatalogSnapshot:
            raise AssertionError("catalog load is not expected")

        async def execute(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            raise AssertionError("query execution is not expected")

        async def cancel(self, _query_id: str) -> bool:
            return False

        async def invalidate(self, _source_id: str) -> None:
            pass

        async def observe_resources(
            self,
            _source: SourceProfile,
        ) -> ResourceObservation:
            raise AssertionError("resource observation is not expected")

    class FailingChildLifespan:
        async def __aenter__(self) -> None:
            events.append("child_enter")
            await asyncio.sleep(0)
            raise startup_error

        async def __aexit__(self, *_args: object) -> None:
            events.append("child_exit")

    class FakeMCPServer:
        def streamable_http_app(self, **_kwargs: object) -> FastAPI:
            def failing_lifespan(_app: FastAPI) -> FailingChildLifespan:
                return FailingChildLifespan()

            return FastAPI(lifespan=failing_lifespan)

    async def tracked_reload_task(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        events.append("reload_started")
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            events.append("reload_cancelled")
            raise_if_failed("reload_task")
            raise

    monkeypatch.setattr(managed_runtime_module, "PostgresMetadataStore", FakeMetadataStore)
    monkeypatch.setattr(managed_runtime_module, "PostgresSourceStore", FakeSourceStore)
    monkeypatch.setattr(managed_runtime_module, "_reload_sources", tracked_reload_task)
    monkeypatch.setattr(
        app_module,
        "create_mcp_server",
        lambda *_args, **_kwargs: FakeMCPServer(),
    )

    fake_query_executor = FakeQueryExecutor()
    query_executor: RuntimeQueryExecutor = fake_query_executor
    catalog: RuntimeCatalogProvider = (
        fake_query_executor if alias_query_and_catalog else FakeCatalog()
    )
    app = managed_runtime_module.build_app(
        _runtime(tmp_path),
        catalog=catalog,
        query_executor=query_executor,
        access_policy=_shared_policy(tmp_path),
    )

    metadata_close = app.state.metadata.close

    async def tracked_metadata_close() -> None:
        events.append("metadata_close")
        await metadata_close()

    monkeypatch.setattr(app.state.metadata, "close", tracked_metadata_close)

    source_reloader = app.state.source_reloader
    assert source_reloader is not None
    initial_sync = source_reloader.sync

    async def tracked_initial_sync() -> None:
        events.append("reload_sync")
        await initial_sync()

    monkeypatch.setattr(source_reloader, "sync", tracked_initial_sync)
    return app, events, startup_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failed_steps",
    [
        frozenset(),
        frozenset(
            {
                "reload_task",
                "query_executor",
                "catalog",
                "metadata",
                "source_store",
            }
        ),
    ],
    ids=["cleanup-succeeds", "every-cleanup-step-fails"],
)
async def test_child_lifespan_enter_failure_cleans_parent_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    failed_steps: frozenset[str],
) -> None:
    caplog.set_level(logging.WARNING, logger="query_man")
    app, events, startup_error = _build_failing_app(
        monkeypatch,
        tmp_path,
        failed_steps,
    )

    with pytest.raises(RuntimeError) as raised:
        async with app.router.lifespan_context(app):
            pytest.fail("the parent lifespan must not start")

    assert raised.value is startup_error
    assert events == [
        "reload_sync",
        "child_enter",
        "reload_started",
        "reload_cancelled",
        "query_stop_accepting",
        "query_drain:0",
        "query_close",
        "catalog_close",
        "metadata_close",
        "metadata_store_close",
        "source_store_close",
    ]
    assert "child_exit" not in events
    assert all(events.count(event) == 1 for event in events)

    cleanup_messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("startup_cleanup_step_failed")
    ]
    assert cleanup_messages == [
        f"startup_cleanup_step_failed step={step}"
        for step in (
            "reload_task",
            "query_executor",
            "catalog",
            "metadata",
            "source_store",
        )
        if step in failed_steps
    ]
    assert "startup-secret-must-not-be-logged" not in caplog.text
    assert "cleanup-secret-must-not-be-logged" not in caplog.text


@pytest.mark.asyncio
async def test_child_enter_failure_closes_aliased_parent_resource_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    app, events, _startup_error = _build_failing_app(
        monkeypatch,
        tmp_path,
        frozenset(),
        alias_query_and_catalog=True,
    )

    with pytest.raises(RuntimeError):
        async with app.router.lifespan_context(app):
            pytest.fail("the parent lifespan must not start")

    assert events.count("query_close") == 1
    assert events.count("query_drain:0") == 1
