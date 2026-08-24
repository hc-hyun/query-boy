from __future__ import annotations

import ast
import inspect
import json
import shutil
import textwrap
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType
from typing import get_type_hints

import pytest
import yaml

import query_man.quality as quality_module
import query_man.verified as verified_module
from query_man.app import _probe_registered_sources
from query_man.gateway import GatewayService
from query_man.metadata import MetadataService
from query_man.models import SourceProfile
from query_man.query import QueryService
from query_man.registry import (
    POSTGRES_IDENTIFIER_MAX_LENGTH,
    RegistryConfigurationError,
    SourceProjectionWriter,
    SourceReader,
    SourceRegistry,
    load_budget_profiles,
    validate_source_manifest,
)
from query_man.source_admin import SourceAdminService, SourceReloader
from tests.helpers import DUMMY_ENVIRONMENT, ROOT_DIRECTORY, load_test_registry


def _public_methods(protocol: type[object]) -> set[str]:
    return {
        name
        for name, value in vars(protocol).items()
        if not name.startswith("_") and callable(value)
    }


def _local_annotation(function: object, variable: str) -> str | None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == variable
        ):
            return ast.unparse(node.annotation)
    return None


def test_source_capability_protocols_have_exact_approved_shapes() -> None:
    assert _public_methods(SourceReader) == {"get", "list", "source_ids"}
    assert get_type_hints(SourceReader.list) == {
        "return": list[dict[str, str]],
    }
    assert get_type_hints(SourceReader.get) == {
        "source_id": str,
        "return": SourceProfile | None,
    }
    assert get_type_hints(SourceReader.source_ids) == {
        "return": frozenset[str],
    }
    assert SourceReader in SourceProjectionWriter.__mro__
    assert _public_methods(SourceProjectionWriter) == {"remove", "upsert"}
    assert get_type_hints(SourceProjectionWriter.upsert) == {
        "source": SourceProfile,
        "return": type(None),
    }
    assert get_type_hints(SourceProjectionWriter.remove) == {
        "source_id": str,
        "return": type(None),
    }


def test_source_consumers_receive_only_the_capability_they_need() -> None:
    assert get_type_hints(GatewayService.__init__)["registry"] is SourceReader
    assert get_type_hints(MetadataService.__init__)["registry"] is SourceReader
    assert get_type_hints(QueryService.__init__)["registry"] is SourceReader
    assert get_type_hints(_probe_registered_sources)["registry"] is SourceReader
    assert (
        get_type_hints(SourceReloader.__init__)["registry"]
        is SourceProjectionWriter
    )
    assert _local_annotation(quality_module._run, "registry") == "SourceReader"
    assert _local_annotation(verified_module._run, "registry") == "SourceReader"
    assert _local_annotation(SourceAdminService._stage, "registry") == "SourceReader"


def test_published_source_profile_graph_is_recursively_immutable() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    overlay = source.semantic_overlay

    assert isinstance(source.allowed_schemas, tuple)
    assert isinstance(source.allowed_relation_kinds, tuple)
    assert isinstance(overlay.relations, tuple)
    assert isinstance(overlay.joins, tuple)
    assert isinstance(overlay.business_terms, tuple)
    assert isinstance(overlay.question_rules, tuple)
    assert isinstance(overlay.composition_hints, tuple)
    for relation in overlay.relations:
        assert isinstance(relation.aliases, tuple)
        assert isinstance(relation.use_for, tuple)
        assert isinstance(relation.column_aliases, MappingProxyType)
        assert isinstance(relation.value_hints, MappingProxyType)
        assert all(isinstance(items, tuple) for items in relation.column_aliases.values())
        assert all(isinstance(items, tuple) for items in relation.value_hints.values())
        assert isinstance(relation.measures, tuple)
        assert all(isinstance(measure.aliases, tuple) for measure in relation.measures)
        if relation.grain is not None:
            assert isinstance(relation.grain.key_columns, tuple)
    for join in overlay.joins:
        assert isinstance(join.column_pairs, tuple)
        assert all(isinstance(pair, MappingProxyType) for pair in join.column_pairs)
    for term in overlay.business_terms:
        assert isinstance(term.aliases, tuple)
        assert isinstance(term.predicates, tuple)
        assert all(isinstance(predicate.values, tuple) for predicate in term.predicates)
    for rule in overlay.question_rules:
        assert isinstance(rule.phrases, tuple)
        assert isinstance(rule.missing_concepts, tuple)
        assert isinstance(rule.options, tuple)
    for hint in overlay.composition_hints:
        assert isinstance(hint.phrases, tuple)
        assert isinstance(hint.combine_keys, tuple)

    with pytest.raises(FrozenInstanceError):
        source.name = "mutated"  # type: ignore[misc]
    with pytest.raises(TypeError):
        overlay.relations[0].column_aliases["mutated"] = ()  # type: ignore[index]
    with pytest.raises(TypeError):
        overlay.joins[0].column_pairs[0]["left"] = "mutated"  # type: ignore[index]
    with pytest.raises(AttributeError):
        overlay.relations.append(overlay.relations[0])  # type: ignore[attr-defined]


def test_source_profile_construction_does_not_retain_mutable_aliases() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    relation = source.semantic_overlay.relations[0]
    join = source.semantic_overlay.joins[0]
    schemas = list(source.allowed_schemas)
    aliases = ["original-alias"]
    column_aliases = {"issue_id": ["original-column-alias"]}
    pair = {"left": "issue_id", "right": "issue_id"}

    copied_source = replace(source, allowed_schemas=schemas)  # type: ignore[arg-type]
    copied_relation = replace(  # type: ignore[arg-type]
        relation,
        aliases=aliases,
        column_aliases=column_aliases,
    )
    copied_join = replace(join, column_pairs=[pair])  # type: ignore[arg-type]
    schemas.append("mutated")
    aliases.append("mutated")
    column_aliases["issue_id"].append("mutated")
    pair["left"] = "mutated"

    assert "mutated" not in copied_source.allowed_schemas
    assert copied_relation.aliases == ("original-alias",)
    assert copied_relation.column_aliases["issue_id"] == (
        "original-column-alias",
    )
    assert copied_join.column_pairs[0]["left"] == "issue_id"


def test_loads_public_source_fields_only() -> None:
    registry = load_test_registry(
        {
            **DUMMY_ENVIRONMENT,
            "POSTGRES_PORT": "55432",
            "QUERY_MAN_POSTGRES_HOST": "postgres",
        }
    )
    assert len(registry) == 2
    assert registry.get("development-issues").connection.host == "postgres"  # type: ignore[union-attr]
    assert registry.get("development-issues").connection.port == 55_432  # type: ignore[union-attr]
    assert [item["source_id"] for item in registry.list()] == [
        "development-issues",
        "market-voc",
    ]
    serialized = str(registry.list())
    assert "development-test-secret" not in serialized
    assert "password" not in serialized
    assert "database" not in serialized


def test_loads_versioned_hard_session_budget() -> None:
    budget = load_budget_profiles(
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml"
    )["interactive"]

    assert budget.version == 2
    assert budget.work_mem_kb == 8_192
    assert budget.temp_file_limit_kb == 65_536
    assert budget.max_parallel_workers_per_gather == 0
    assert budget.jit_enabled is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("work_mem_kb", 63),
        ("work_mem_kb", 65_537),
        ("temp_file_limit_kb", -1),
        ("temp_file_limit_kb", 1_048_577),
        ("max_parallel_workers_per_gather", -1),
        ("max_parallel_workers_per_gather", 5),
        ("jit_enabled", "off"),
    ],
)
def test_rejects_unsafe_hard_session_budget(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    raw = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["profiles"]["interactive"][field] = value
    path = tmp_path / "budget-profiles.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(RegistryConfigurationError):
        load_budget_profiles(path)


def test_rejects_older_budget_schema_version(tmp_path: Path) -> None:
    raw = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["version"] = 1
    path = tmp_path / "budget-profiles.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(RegistryConfigurationError, match="version must be 2"):
        load_budget_profiles(path)


def test_missing_secret_fails_closed() -> None:
    with pytest.raises(RegistryConfigurationError, match="DEVELOPMENT_ISSUES_READER_PASSWORD"):
        load_test_registry({"POSTGRES_PORT": "5432", "MARKET_VOC_READER_PASSWORD": "secret"})


def test_blank_connection_host_environment_fails_closed() -> None:
    with pytest.raises(RegistryConfigurationError, match="resolved an invalid host"):
        load_test_registry({**DUMMY_ENVIRONMENT, "QUERY_MAN_POSTGRES_HOST": " "})


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
        """version: 2
source_id: system-test
name: System Test
description: Must not expose system catalogs
provenance:
  owner: query-man
  environment: test
  database_migration_ref: tests/system-test.sql
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


@pytest.mark.parametrize("version", [0, 1, 3])
def test_rejects_non_v2_source_manifest(version: int) -> None:
    source_path = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    raw["version"] = version

    with pytest.raises(RegistryConfigurationError, match="version must be 2"):
        validate_source_manifest(
            raw,
            load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
            "reader-secret",
        )


def test_accepts_postgresql_identifier_boundary_in_projected_fields() -> None:
    source_path = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    identifier = "a" * POSTGRES_IDENTIFIER_MAX_LENGTH
    raw["allowed_schemas"] = [identifier]
    raw["budget_profile"] = identifier
    raw["semantic_overlay"] = {}
    connection = raw["connection"]
    assert isinstance(connection, dict)
    connection["database"] = identifier
    connection["user"] = identifier
    budgets = load_budget_profiles(
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml"
    )
    budget = replace(budgets["interactive"], name=identifier)

    validated = validate_source_manifest(raw, {identifier: budget}, "reader-secret")

    assert validated.profile.budget.name == identifier
    assert validated.profile.allowed_schemas == (identifier,)
    assert validated.profile.connection.database == identifier
    assert validated.profile.connection.user == identifier


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_schemas", ["a" * (POSTGRES_IDENTIFIER_MAX_LENGTH + 1)]),
        ("budget_profile", "a" * (POSTGRES_IDENTIFIER_MAX_LENGTH + 1)),
    ],
)
def test_rejects_projected_identifiers_above_postgresql_limit(
    field: str,
    value: object,
) -> None:
    source_path = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    raw[field] = value

    with pytest.raises(RegistryConfigurationError, match=field):
        validate_source_manifest(
            raw,
            load_budget_profiles(
                ROOT_DIRECTORY / "config" / "budget-profiles.yaml"
            ),
            "reader-secret",
        )


@pytest.mark.parametrize(
    "provenance",
    [
        {},
        {
            "owner": "Query-Man",
            "environment": "development",
            "database_migration_ref": "migrations/0001.sql",
        },
        {
            "owner": "a" * 81,
            "environment": "development",
            "database_migration_ref": "migrations/0001.sql",
        },
        {
            "owner": "query-man",
            "environment": "qa",
            "database_migration_ref": "migrations/0001.sql",
        },
        {
            "owner": "query-man",
            "environment": "development",
            "database_migration_ref": "   ",
        },
        {
            "owner": "query-man",
            "environment": "development",
            "database_migration_ref": "migrations/0001.sql\t",
        },
        {
            "owner": "query-man",
            "environment": "development",
            "database_migration_ref": "migrations/\x7f0001.sql",
        },
        {
            "owner": "query-man",
            "environment": "development",
            "database_migration_ref": "x" * 256,
        },
        {
            "owner": "query-man",
            "environment": "development",
            "database_migration_ref": "migrations/0001.sql",
            "secret": "must-not-be-accepted",
        },
    ],
)
def test_rejects_invalid_source_provenance(provenance: object) -> None:
    source_path = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    raw["provenance"] = provenance

    with pytest.raises(RegistryConfigurationError, match="provenance"):
        validate_source_manifest(
            raw,
            load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
            "reader-secret",
        )


def test_requires_source_provenance() -> None:
    source_path = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    raw.pop("provenance")

    with pytest.raises(RegistryConfigurationError, match="provenance"):
        validate_source_manifest(
            raw,
            load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
            "reader-secret",
        )


def test_validates_control_plane_manifest_without_storing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
    raw = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    monkeypatch.setenv("QUERY_MAN_POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "55432")
    validated = validate_source_manifest(
        raw,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        "control-plane-secret",
    )

    assert validated.profile.connection.password == "control-plane-secret"
    assert "control-plane-secret" not in str(validated.document)
    assert json.loads(json.dumps(validated.document)) == validated.document
    assert isinstance(validated.document["allowed_schemas"], list)
    assert isinstance(validated.document["allowed_relation_kinds"], list)
    assert isinstance(validated.document["semantic_overlay"], dict)
    assert isinstance(validated.document["semantic_overlay"]["relations"], list)  # type: ignore[index]
    assert validated.document["version"] == 2
    assert validated.document["provenance"] == raw["provenance"]
    assert validated.profile.provenance.owner == "query-man"
    assert validated.profile.provenance.environment == "development"
    assert (
        validated.profile.provenance.database_migration_ref
        == "docker/postgres/init/10-development-issues-schema.sql"
    )
    assert validated.profile.connection.host == "postgres"
    assert validated.document["connection"]["host"] == "postgres"  # type: ignore[index]
    assert validated.profile.connection.port == 55_432
    assert validated.document["connection"]["port"] == 55_432  # type: ignore[index]
    assert "host_env" not in validated.document["connection"]  # type: ignore[operator]
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
