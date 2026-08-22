from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from query_man.registry import (
    RegistryConfigurationError,
    SourceRegistry,
    load_budget_profiles,
    migrate_source_manifest,
    validate_source_manifest,
)
from tests.helpers import DUMMY_ENVIRONMENT, ROOT_DIRECTORY, load_test_registry


def test_loads_public_source_fields_only() -> None:
    registry = load_test_registry({**DUMMY_ENVIRONMENT, "POSTGRES_PORT": "55432"})
    assert len(registry) == 2
    assert registry.get("development-issues").connection.port == 55_432  # type: ignore[union-attr]
    assert [item["source_id"] for item in registry.list()] == [
        "development-issues",
        "market-voc",
    ]
    serialized = str(registry.list())
    assert "development-test-secret" not in serialized
    assert "password" not in serialized
    assert "database" not in serialized


def test_missing_secret_fails_closed() -> None:
    with pytest.raises(RegistryConfigurationError, match="DEVELOPMENT_ISSUES_READER_PASSWORD"):
        load_test_registry({"POSTGRES_PORT": "5432", "MARKET_VOC_READER_PASSWORD": "secret"})


def test_duplicate_source_ids_are_rejected(tmp_path: Path) -> None:
    source = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    shutil.copy(source, tmp_path / "one.yaml")
    shutil.copy(source, tmp_path / "two.yaml")
    with pytest.raises(RegistryConfigurationError, match="Duplicate source_id"):
        SourceRegistry.load(
            tmp_path,
            ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
            DUMMY_ENVIRONMENT,
        )


def test_system_schemas_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "system-test.yaml").write_text(
        """version: 1
source_id: system-test
name: System Test
description: Must not expose system catalogs
connection:
  host: 127.0.0.1
  port: 5432
  database: postgres
  user: system_test_reader
  password_env: SYSTEM_TEST_READER_PASSWORD
  ssl: false
allowed_schemas: [pg_catalog]
allowed_relation_kinds: [table]
budget_profile: interactive
""",
        encoding="utf-8",
    )
    with pytest.raises(RegistryConfigurationError, match="system schema"):
        SourceRegistry.load(
            tmp_path,
            ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
            {"SYSTEM_TEST_READER_PASSWORD": "secret"},
        )


def test_migrates_v0_budget_field_and_rejects_future_versions() -> None:
    raw = {
        "version": 0,
        "source_id": "legacy-source",
        "name": "Legacy Source",
        "description": "Legacy source contract",
        "connection": {
            "host": "127.0.0.1",
            "port": 5432,
            "database": "legacy",
            "user": "legacy_reader",
            "password_env": "LEGACY_SOURCE_READER_PASSWORD",
            "ssl": False,
        },
        "allowed_schemas": ["ai"],
        "allowed_relation_kinds": ["view"],
        "budget": "interactive",
    }

    migrated = migrate_source_manifest(raw)
    assert migrated["version"] == 1
    assert migrated["budget_profile"] == "interactive"
    assert "budget" not in migrated
    assert raw["version"] == 0
    with pytest.raises(ValueError, match="unsupported"):
        migrate_source_manifest({"version": 2})


def test_validates_control_plane_manifest_without_storing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    monkeypatch.setenv("POSTGRES_PORT", "55432")
    validated = validate_source_manifest(
        raw,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        "control-plane-secret",
    )

    assert validated.profile.connection.password == "control-plane-secret"
    assert "control-plane-secret" not in str(validated.document)
    assert validated.document["version"] == 1
    assert validated.profile.connection.port == 55_432
    assert validated.document["connection"]["port"] == 55_432  # type: ignore[index]
    assert "port_env" not in validated.document["connection"]  # type: ignore[operator]


def test_rls_manifest_requires_security_invoker_view_only_surface() -> None:
    raw = yaml.safe_load(
        (
            ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets.yaml"
        ).read_text(encoding="utf-8")
    )
    raw["tenant_isolation"] = "rls"
    raw["allowed_relation_kinds"] = ["table", "view"]

    with pytest.raises(RegistryConfigurationError, match="security-invoker views only"):
        validate_source_manifest(
            raw,
            load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
            "reader-secret",
        )
