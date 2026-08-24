from __future__ import annotations

from pathlib import Path
from textwrap import dedent, indent
from typing import Literal, get_type_hints

import pytest

import query_man.app as app_module
from query_man.access import AccessPolicy, AccessPolicyConfigurationError
from query_man.catalog import PostgresCatalog
from query_man.models import RuntimeCatalogProvider
from query_man.query import PostgresQueryExecutor, RuntimeQueryExecutor
from query_man.registry import SourceRegistry
from query_man.runtime_config import RuntimeConfig
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
