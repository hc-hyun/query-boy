from __future__ import annotations

import ast
import inspect
import shutil
import textwrap
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType
from typing import get_type_hints

import pytest
import yaml

import query_man.assurance.cli as assurance_cli_module
from query_man.delivery.gateway import GatewayService
from query_man.guarded_query.query import QueryService
from query_man.metadata.service import MetadataService
from query_man.runtime.composition import _probe_registered_sources
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.registry import (
    POSTGRES_IDENTIFIER_MAX_LENGTH,
    RegistryConfigurationError,
    SourceReader,
    SourceRegistry,
    load_budget_profiles,
)
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


def _development_manifest() -> dict[str, object]:
    raw: object = yaml.safe_load(
        (
            ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml"
        ).read_text(encoding="utf-8")
    )
    assert isinstance(raw, dict)
    return raw


def _load_single_manifest(
    tmp_path: Path,
    raw: dict[str, object],
    *,
    budget_file: Path | None = None,
    environment: dict[str, str] | None = None,
) -> SourceRegistry:
    source_directory = tmp_path / "sources"
    source_directory.mkdir()
    (source_directory / "source.yaml").write_text(
        yaml.safe_dump(raw),
        encoding="utf-8",
    )
    return SourceRegistry.load(
        source_directory,
        budget_file or ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        environment or DUMMY_ENVIRONMENT,
    )


def test_source_reader_protocol_has_exact_approved_shape() -> None:
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


def test_source_consumers_receive_only_the_capability_they_need() -> None:
    assert get_type_hints(GatewayService.__init__)["registry"] is SourceReader
    assert get_type_hints(MetadataService.__init__)["registry"] is SourceReader
    assert get_type_hints(QueryService.__init__)["registry"] is SourceReader
    assert get_type_hints(_probe_registered_sources)["registry"] is SourceReader
    assert (
        _local_annotation(assurance_cli_module._run_evaluation, "registry")
        == "SourceReader"
    )
    assert (
        _local_annotation(assurance_cli_module._run_verification, "registry")
        == "SourceReader"
    )


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
    assert registry.list() == [
        {
            "source_id": "development-issues",
            "name": "개발 문제점",
            "description": "개발 및 검증 과정에서 발견한 문제, 원인, 대책과 댓글",
        },
        {
            "source_id": "market-voc",
            "name": "시장 VOC",
            "description": "시장에서 접수된 불량, 제품 기기, 원인, 대응과 댓글",
        },
    ]
    serialized = str(registry.list())
    assert "development-test-secret" not in serialized
    assert "password" not in serialized
    assert "database" not in serialized
    assert "development.issues" not in serialized


def test_rejects_retired_managed_observability_field(tmp_path: Path) -> None:
    raw = _development_manifest()
    raw["observability"] = {
        "representative_records": {
            "grain": "development_issue",
            "physical_relation": "development.issues",
        },
        "storage_relations": ["development.issues"],
    }

    with pytest.raises(RegistryConfigurationError, match="observability"):
        _load_single_manifest(tmp_path, raw)


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
def test_rejects_non_v2_source_manifest(tmp_path: Path, version: int) -> None:
    raw = _development_manifest()
    raw["version"] = version

    with pytest.raises(RegistryConfigurationError, match="version must be 2"):
        _load_single_manifest(tmp_path, raw)


def test_accepts_postgresql_identifier_boundary_in_source_fields(
    tmp_path: Path,
) -> None:
    raw = _development_manifest()
    identifier = "a" * POSTGRES_IDENTIFIER_MAX_LENGTH
    raw["allowed_schemas"] = [identifier]
    raw["budget_profile"] = identifier
    raw["semantic_overlay"] = {}
    connection = raw["connection"]
    assert isinstance(connection, dict)
    connection["database"] = identifier
    connection["user"] = identifier
    budget_raw = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert isinstance(budget_raw, dict)
    profiles = budget_raw["profiles"]
    assert isinstance(profiles, dict)
    profiles[identifier] = profiles.pop("interactive")
    budget_file = tmp_path / "budget-profiles.yaml"
    budget_file.write_text(yaml.safe_dump(budget_raw), encoding="utf-8")

    source = _load_single_manifest(tmp_path, raw, budget_file=budget_file).get(
        "development-issues"
    )

    assert source is not None
    assert source.budget.name == identifier
    assert source.allowed_schemas == (identifier,)
    assert source.connection.database == identifier
    assert source.connection.user == identifier


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("allowed_schemas", ["a" * (POSTGRES_IDENTIFIER_MAX_LENGTH + 1)]),
        ("budget_profile", "a" * (POSTGRES_IDENTIFIER_MAX_LENGTH + 1)),
    ],
)
def test_rejects_source_identifiers_above_postgresql_limit(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    raw = _development_manifest()
    raw[field] = value

    with pytest.raises(RegistryConfigurationError, match=field):
        _load_single_manifest(tmp_path, raw)


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
def test_rejects_invalid_source_provenance(
    tmp_path: Path,
    provenance: object,
) -> None:
    raw = _development_manifest()
    raw["provenance"] = provenance

    with pytest.raises(RegistryConfigurationError, match="provenance"):
        _load_single_manifest(tmp_path, raw)


def test_requires_source_provenance(tmp_path: Path) -> None:
    raw = _development_manifest()
    raw.pop("provenance")

    with pytest.raises(RegistryConfigurationError, match="provenance"):
        _load_single_manifest(tmp_path, raw)


def test_rejects_non_source_scoped_secret_environment(
    tmp_path: Path,
) -> None:
    raw = _development_manifest()
    connection = raw["connection"]
    assert isinstance(connection, dict)
    connection["password_env"] = "SHARED_READER_PASSWORD"

    with pytest.raises(
        RegistryConfigurationError,
        match="must use the source-scoped secret",
    ):
        _load_single_manifest(
            tmp_path,
            raw,
            environment={
                **DUMMY_ENVIRONMENT,
                "SHARED_READER_PASSWORD": "shared-secret",
            },
        )


@pytest.mark.parametrize(
    "relation_kinds",
    [["table"], ["view"], ["table", "view"]],
)
def test_yaml_registry_rejects_every_rls_source(
    tmp_path: Path,
    relation_kinds: list[str],
) -> None:
    raw = _development_manifest()
    raw["tenant_isolation"] = "rls"
    raw["allowed_relation_kinds"] = relation_kinds

    with pytest.raises(RegistryConfigurationError, match="RLS sources are not supported"):
        _load_single_manifest(tmp_path, raw)
