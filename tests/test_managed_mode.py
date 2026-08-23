from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

import query_man.app as app_module
from query_man.registry import SourceRegistry
from query_man.runtime_config import RuntimeConfig
from query_man.verified import VerifiedQueryRegistry
from tests.helpers import ROOT_DIRECTORY, load_test_registry

_SOURCE_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def _runtime(
    source_mode: Literal["bootstrap", "managed"],
    source_directory: Path,
) -> RuntimeConfig:
    managed = source_mode == "managed"
    return RuntimeConfig(
        host="127.0.0.1",
        port=3000,
        log_level="critical",
        api_token=None,
        source_directory=source_directory,
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=0,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        source_mode=source_mode,
        control_dsn="host=control.invalid dbname=query_man" if managed else None,
        source_encryption_key=_SOURCE_KEY if managed else None,
    )


def _unexpected_file_load(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("managed mode must not load bootstrap files")


def test_managed_mode_starts_empty_without_loading_source_or_verified_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(SourceRegistry, "load", _unexpected_file_load)
    monkeypatch.setattr(VerifiedQueryRegistry, "load", _unexpected_file_load)

    app = app_module.build_app(_runtime("managed", tmp_path / "missing" / "sources"))

    assert app.state.registry.source_ids() == frozenset()
    assert app.state.source_admin is not None
    assert app.state.source_reloader is not None


def test_managed_mode_rejects_an_injected_registry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not accept a bootstrap registry"):
        app_module.build_app(
            _runtime("managed", tmp_path / "missing" / "sources"),
            registry=SourceRegistry([]),
        )


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
