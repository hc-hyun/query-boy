from __future__ import annotations

import asyncio
from pathlib import Path
from textwrap import dedent, indent
from typing import Literal, cast, get_type_hints

import pytest

import query_man.app as app_module
from query_man.access import AccessPolicy, AccessPolicyConfigurationError
from query_man.catalog import PostgresCatalog
from query_man.models import RuntimeCatalogProvider
from query_man.operations import operations
from query_man.query import PostgresQueryExecutor, RuntimeQueryExecutor
from query_man.registry import SourceRegistry
from query_man.runtime_config import RuntimeConfig
from query_man.source_admin import ReplicaSourceObservation
from query_man.verified import VerifiedQueryRegistry
from tests.helpers import ROOT_DIRECTORY, load_test_registry

_SOURCE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_QUERY_TOKEN = "query-token-value-with-at-least-32-characters"
_ADMIN_TOKEN = "admin-token-value-with-at-least-32-characters"


def test_runtime_composition_requires_composite_provider_contracts() -> None:
    hints = get_type_hints(app_module.build_app)

    assert hints["catalog"] == RuntimeCatalogProvider | None
    assert hints["query_executor"] == RuntimeQueryExecutor | None


@pytest.mark.parametrize("method", ["load", "close", "invalidate"])
def test_runtime_rejects_catalog_with_missing_required_capability(
    method: str,
) -> None:
    catalog = PostgresCatalog()
    setattr(catalog, method, None)

    with pytest.raises(
        TypeError,
        match=rf"catalog is missing required runtime capabilities: {method}",
    ):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            catalog=catalog,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "method",
    ["execute", "cancel", "close", "stop_accepting", "drain", "invalidate"],
)
def test_runtime_rejects_query_executor_with_missing_required_capability(
    method: str,
) -> None:
    executor = PostgresQueryExecutor()
    setattr(executor, method, None)

    with pytest.raises(
        TypeError,
        match=rf"query_executor is missing required runtime capabilities: {method}",
    ):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            query_executor=executor,  # type: ignore[arg-type]
        )


def test_runtime_does_not_replace_falsey_incomplete_adapters() -> None:
    class FalseyCatalog(PostgresCatalog):
        def __bool__(self) -> bool:
            return False

    class FalseyQueryExecutor(PostgresQueryExecutor):
        def __bool__(self) -> bool:
            return False

    catalog = FalseyCatalog()
    catalog.invalidate = None  # type: ignore[assignment]
    with pytest.raises(TypeError, match=r"catalog.*invalidate"):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            catalog=catalog,
        )

    executor = FalseyQueryExecutor()
    executor.drain = None  # type: ignore[assignment]
    with pytest.raises(TypeError, match=r"query_executor.*drain"):
        app_module.build_app(
            _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
            registry=load_test_registry(),
            query_executor=executor,
        )


def _runtime(
    source_mode: Literal["bootstrap", "managed"],
    source_directory: Path,
    *,
    access_policy_file: Path | None = None,
) -> RuntimeConfig:
    managed = source_mode == "managed"
    return RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=None,
        source_directory=source_directory,
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=access_policy_file,
        metadata_cache_ttl_ms=0,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        source_mode=source_mode,
        control_dsn="host=control.invalid dbname=query_man" if managed else None,
        source_encryption_key=_SOURCE_KEY if managed else None,
        replica_id="runtime-test" if managed else None,
    )


def _unexpected_file_load(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("managed mode must not load bootstrap files")


def _policy(
    tmp_path: Path,
    callers: str,
    environment: dict[str, str],
) -> AccessPolicy:
    return AccessPolicy.load(_write_policy(tmp_path, callers), environment)


def _write_policy(tmp_path: Path, callers: str) -> Path:
    path = tmp_path / "access.yaml"
    body = indent(dedent(callers).strip(), "  ")
    path.write_text(f"version: 2\ncallers:\n{body}\n", encoding="utf-8")
    return path


def _shared_policy(tmp_path: Path) -> AccessPolicy:
    return _policy(
        tmp_path,
        """
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""",
        {"QUERY_TOKEN": _QUERY_TOKEN, "ADMIN_TOKEN": _ADMIN_TOKEN},
    )


def test_managed_mode_starts_empty_without_loading_source_or_verified_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(SourceRegistry, "load", _unexpected_file_load)
    monkeypatch.setattr(VerifiedQueryRegistry, "load", _unexpected_file_load)

    app = app_module.build_app(
        _runtime("managed", tmp_path / "missing" / "sources"),
        access_policy=_shared_policy(tmp_path),
    )

    assert app.state.registry.source_ids() == frozenset()
    assert app.state.source_admin is not None
    assert app.state.source_reloader is not None
    assert app.state.source_reloader._invalidators == (
        app.state.catalog,
        app.state.query_executor,
    )


def test_managed_mode_rejects_an_injected_registry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not accept a bootstrap registry"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            registry=SourceRegistry([]),
            access_policy=_shared_policy(tmp_path),
        )


def test_managed_mode_rejects_anonymous_local_compatibility(tmp_path: Path) -> None:
    with pytest.raises(
        AccessPolicyConfigurationError,
        match="authenticated query and admin identities",
    ):
        app_module.build_app(_runtime("managed", tmp_path / "missing" / "sources"))


def test_managed_mode_rejects_legacy_query_only_policy(tmp_path: Path) -> None:
    with pytest.raises(AccessPolicyConfigurationError, match="admin identity"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            access_policy=AccessPolicy.legacy(_QUERY_TOKEN),
        )


def test_managed_mode_rejects_policy_without_admin(tmp_path: Path) -> None:
    query_only = _policy(
        tmp_path,
        """
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
""",
        {"QUERY_TOKEN": _QUERY_TOKEN},
    )

    with pytest.raises(AccessPolicyConfigurationError, match="admin identity"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            access_policy=query_only,
        )


def test_managed_mode_rejects_policy_without_query_identity(tmp_path: Path) -> None:
    admin_only = _policy(
        tmp_path,
        """
  - caller_id: operator
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""",
        {"ADMIN_TOKEN": _ADMIN_TOKEN},
    )

    with pytest.raises(AccessPolicyConfigurationError, match="non-admin query identity"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            access_policy=admin_only,
        )


def test_managed_mode_loads_shared_policy_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    policy_file = _write_policy(
        tmp_path,
        """
  - caller_id: analyst
    tenant_id: engineering
    token_env: QUERY_TOKEN
  - caller_id: operator
    tenant_id: operations
    token_env: ADMIN_TOKEN
    operator: true
""",
    )
    monkeypatch.setenv("QUERY_TOKEN", _QUERY_TOKEN)
    monkeypatch.setenv("ADMIN_TOKEN", _ADMIN_TOKEN)

    app = app_module.build_app(
        _runtime(
            "managed",
            tmp_path / "missing" / "sources",
            access_policy_file=policy_file,
        )
    )

    query_caller = app.state.access_policy.authenticate(_QUERY_TOKEN)
    admin_caller = app.state.access_policy.authenticate(_ADMIN_TOKEN)
    assert query_caller is not None and query_caller.operator is False
    assert admin_caller is not None and admin_caller.operator is True


def test_bootstrap_mode_preserves_an_explicit_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = SourceRegistry([])
    monkeypatch.setattr(SourceRegistry, "load", _unexpected_file_load)
    monkeypatch.setattr(
        VerifiedQueryRegistry,
        "load",
        lambda *_args, **_kwargs: VerifiedQueryRegistry([]),
    )

    app = app_module.build_app(
        _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
        registry=registry,
    )

    assert app.state.registry is registry
    assert app.state.source_admin is None
    assert app.state.source_reloader is None


def test_bootstrap_mode_does_not_construct_control_plane_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "PostgresMetadataStore", _unexpected_file_load)
    monkeypatch.setattr(app_module, "PostgresSourceStore", _unexpected_file_load)

    app = app_module.build_app(
        _runtime("bootstrap", ROOT_DIRECTORY / "config" / "sources"),
        registry=load_test_registry(),
    )

    assert app.state.source_admin is None
    assert app.state.source_reloader is None


@pytest.mark.parametrize(
    ("reload_interval_ms", "heartbeat_interval_ms"),
    [(250, 5_000), (6_000, 6_000)],
)
@pytest.mark.asyncio
async def test_managed_reload_task_registers_once_and_reports_internal_snapshot(
    reload_interval_ms: int,
    heartbeat_interval_ms: int,
) -> None:
    class Reloader:
        async def sync(self) -> None:
            pass

    class Writer:
        def __init__(self) -> None:
            self.registrations: list[tuple[str, int]] = []
            self.reports: list[tuple[object, ...]] = []
            self.reported = asyncio.Event()

        async def register_replica(self, replica_id: str, interval_ms: int) -> int:
            self.registrations.append((replica_id, interval_ms))
            return 7

        async def report_replica(
            self,
            replica_id: str,
            incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            self.reports.append((replica_id, incarnation, reason_code, sources))
            self.reported.set()

    operations.reset()
    try:
        operations.set_replica_source_applied("source-a", 2, 3, True)
        operations.set_replica_metadata_revision("source-a", f"sha256:{'1' * 64}")
        operations.set_source_health("source-a", "healthy")
        public_before = operations.snapshot()
        writer = Writer()
        task = asyncio.create_task(
            app_module._reload_sources(  # type: ignore[arg-type]
                Reloader(),
                reload_interval_ms,
                writer,
                "runtime-a",
            )
        )
        await asyncio.wait_for(writer.reported.wait(), timeout=1)

        assert writer.registrations == [("runtime-a", heartbeat_interval_ms)]
        assert len(writer.reports) == 1
        replica_id, incarnation, reason_code, sources = writer.reports[0]
        assert (replica_id, incarnation, reason_code) == ("runtime-a", 7, None)
        assert isinstance(sources, tuple) and len(sources) == 1
        source = cast(ReplicaSourceObservation, sources[0])
        assert source.source_id == "source-a"
        assert source.applied_generation == 2
        assert source.applied_state_version == 3
        assert source.applied_enabled is True
        assert source.applied_metadata_revision == f"sha256:{'1' * 64}"
        assert source.source_health == "healthy"
        assert source.reason_code is None
        assert operations.snapshot() == public_before
    finally:
        if "task" in locals():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        operations.reset()


@pytest.mark.asyncio
async def test_replica_report_omits_sources_during_control_scan_failure() -> None:
    class Writer:
        def __init__(self) -> None:
            self.report: tuple[object, ...] | None = None

        async def report_replica(
            self,
            replica_id: str,
            incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            self.report = (replica_id, incarnation, reason_code, sources)

    operations.reset()
    try:
        operations.set_replica_source_applied("source-a", 1, 1, True)
        operations.set_replica_scan_failed(True)
        writer = Writer()

        await app_module._report_replica_observation(  # type: ignore[arg-type]
            writer,
            "runtime-a",
            4,
        )

        assert writer.report == ("runtime-a", 4, "CONTROL_SCAN_FAILED", ())
        assert len(operations.replica_runtime_snapshot().sources) == 1
    finally:
        operations.reset()


@pytest.mark.parametrize("failure", ["registration", "report"])
@pytest.mark.asyncio
async def test_replica_observation_failure_does_not_restart_registration_or_reload(
    failure: str,
) -> None:
    class Reloader:
        def __init__(self) -> None:
            self.synced = asyncio.Event()

        async def sync(self) -> None:
            self.synced.set()

    class Writer:
        def __init__(self) -> None:
            self.registrations = 0
            self.reports = 0

        async def register_replica(self, _replica_id: str, _interval_ms: int) -> int:
            self.registrations += 1
            if failure == "registration":
                raise RuntimeError("registration unavailable")
            return 3

        async def report_replica(
            self,
            _replica_id: str,
            _incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            del reason_code, sources
            self.reports += 1
            raise RuntimeError("report unavailable")

    operations.reset()
    reloader = Reloader()
    writer = Writer()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            reloader,
            1,
            writer,
            "runtime-a",
        )
    )
    try:
        await asyncio.wait_for(reloader.synced.wait(), timeout=1)
        assert writer.registrations == 1
        assert writer.reports == (0 if failure == "registration" else 1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operations.reset()


@pytest.mark.asyncio
async def test_replica_report_failure_retries_same_incarnation_without_reregister(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reloader:
        async def sync(self) -> None:
            pass

    class Writer:
        def __init__(self) -> None:
            self.registrations = 0
            self.reports = 0
            self.retried = asyncio.Event()

        async def register_replica(self, _replica_id: str, _interval_ms: int) -> int:
            self.registrations += 1
            return 9

        async def report_replica(
            self,
            _replica_id: str,
            incarnation: int,
            *,
            reason_code: str | None,
            sources: tuple[object, ...],
        ) -> None:
            del reason_code, sources
            assert incarnation == 9
            self.reports += 1
            if self.reports >= 2:
                self.retried.set()
            raise RuntimeError("report unavailable")

    monkeypatch.setattr(app_module, "REPLICA_HEARTBEAT_INTERVAL_MIN_MS", 1)
    operations.reset()
    writer = Writer()
    task = asyncio.create_task(
        app_module._reload_sources(  # type: ignore[arg-type]
            Reloader(),
            1,
            writer,
            "runtime-a",
        )
    )
    try:
        await asyncio.wait_for(writer.retried.wait(), timeout=1)
        assert writer.registrations == 1
        assert writer.reports >= 2
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        operations.reset()
