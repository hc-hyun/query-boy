from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
import yaml

from query_man.source_catalog.models import (
    ClientCertificateAuthentication,
    PasswordAuthentication,
)
from query_man.source_catalog.registry import (
    POSTGRES_IDENTIFIER_MAX_LENGTH,
    RegistryConfigurationError,
    SourceRegistry,
    load_budget_profiles,
)
from tests.helpers import DUMMY_ENVIRONMENT, ROOT_DIRECTORY, load_test_registry


def _development_manifest() -> dict[str, object]:
    raw: object = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "sources" / "development-issues" / "source.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(raw, dict)
    return raw


def _database_profiles() -> dict[str, object]:
    raw: object = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "database-profiles.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(raw, dict)
    return raw


def _write_database_profiles(tmp_path: Path, raw: dict[str, object]) -> Path:
    path = tmp_path / "config" / "database-profiles.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def _development_database(raw: dict[str, object]) -> dict[str, object]:
    profiles = raw["profiles"]
    assert isinstance(profiles, dict)
    profile = profiles["development-issues"]
    assert isinstance(profile, dict)
    return profile


def _load_single_manifest(
    tmp_path: Path,
    raw: dict[str, object],
    *,
    budget_file: Path | None = None,
    database_file: Path | None = None,
    environment: dict[str, str] | None = None,
) -> SourceRegistry:
    source_directory = tmp_path / "config" / "sources"
    source_directory.mkdir(parents=True)
    source_id = raw.get("source_id")
    assert isinstance(source_id, str)
    source_path = source_directory / source_id
    source_path.mkdir()
    (source_path / "source.yaml").write_text(
        yaml.safe_dump(raw),
        encoding="utf-8",
    )
    (source_path / "views.sql").write_text("-- reviewed test view artifact\n", encoding="utf-8")
    return SourceRegistry.load(
        source_directory,
        budget_file or ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        database_file or ROOT_DIRECTORY / "config" / "database-profiles.yaml",
        Path("/run/secrets/query-man/databases"),
        environment or DUMMY_ENVIRONMENT,
    )


def test_published_source_profile_is_immutable() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None

    assert isinstance(source.allowed_schemas, tuple)
    assert isinstance(source.allowed_relation_kinds, tuple)

    with pytest.raises(FrozenInstanceError):
        source.name = "mutated"  # type: ignore[misc]


def test_source_profile_construction_does_not_retain_mutable_schema_list() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    schemas = list(source.allowed_schemas)

    copied_source = replace(source, allowed_schemas=schemas)  # type: ignore[arg-type]
    schemas.append("mutated")

    assert "mutated" not in copied_source.allowed_schemas


def test_loads_public_source_fields_only() -> None:
    registry = load_test_registry(
        {
            **DUMMY_ENVIRONMENT,
            "POSTGRES_PORT": "55432",
            "QUERY_MAN_POSTGRES_HOST": "postgres",
        }
    )
    development = registry.get("development-issues")
    assert development is not None
    assert development.view_contract_version == 1
    assert development.connection.host == "postgres"
    assert development.connection.port == 55_432

    listed = registry.list()
    assert [source["source_id"] for source in listed] == sorted(registry.source_ids())
    assert all(set(source) == {"source_id", "name", "description"} for source in listed)
    development_public = next(source for source in listed if source["source_id"] == "development-issues")
    assert development_public == {
        "source_id": "development-issues",
        "name": "개발 문제점",
        "description": "개발 및 검증 과정에서 발견한 문제, 원인, 대책과 댓글",
    }
    serialized = str(listed)
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
    budget = load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml")["interactive"]

    assert budget.version == 2
    assert budget.work_mem_kb == 8_192
    assert budget.temp_file_limit_kb == 65_536
    assert budget.max_parallel_workers_per_gather == 0
    assert budget.jit_enabled is False
    assert budget.max_concurrent_queries == budget.max_pool_size == 2


def test_budget_accepts_pool_capacity_above_query_concurrency(
    tmp_path: Path,
) -> None:
    raw = yaml.safe_load((ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(encoding="utf-8"))
    raw["profiles"]["interactive"]["max_pool_size"] = 3
    path = tmp_path / "budget-profiles.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    budget = load_budget_profiles(path)["interactive"]

    assert budget.max_pool_size == 3
    assert budget.max_concurrent_queries == 2


def test_rejects_query_concurrency_above_pool_capacity(tmp_path: Path) -> None:
    raw = yaml.safe_load((ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(encoding="utf-8"))
    raw["profiles"]["interactive"]["max_concurrent_queries"] = 3
    path = tmp_path / "budget-profiles.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(
        RegistryConfigurationError,
        match="max_concurrent_queries must be less than or equal to max_pool_size",
    ):
        load_budget_profiles(path)


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
    raw = yaml.safe_load((ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(encoding="utf-8"))
    raw["profiles"]["interactive"][field] = value
    path = tmp_path / "budget-profiles.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(RegistryConfigurationError):
        load_budget_profiles(path)


def test_rejects_older_budget_schema_version(tmp_path: Path) -> None:
    raw = yaml.safe_load((ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(encoding="utf-8"))
    raw["version"] = 1
    path = tmp_path / "budget-profiles.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(RegistryConfigurationError, match="version must be 2"):
        load_budget_profiles(path)


def test_resolves_database_scoped_client_certificate_paths() -> None:
    source = load_test_registry().get("development-issues")

    assert source is not None
    assert source.connection.database_profile == "development-issues"
    authentication = source.connection.authentication
    assert isinstance(authentication, ClientCertificateAuthentication)
    assert authentication.root_certificate == Path(
        "/run/secrets/query-man/databases/development-issues/ca.crt"
    )
    assert authentication.client_certificate == Path(
        "/run/secrets/query-man/databases/development-issues/client.crt"
    )
    assert authentication.client_key == Path(
        "/run/secrets/query-man/databases/development-issues/client.key"
    )


def test_missing_fixture_password_fails_closed(tmp_path: Path) -> None:
    manifest = _development_manifest()
    provenance = manifest["provenance"]
    assert isinstance(provenance, dict)
    provenance["environment"] = "test"
    databases = _database_profiles()
    database = _development_database(databases)
    database["sslmode"] = "disable"
    database["authentication"] = {
        "type": "password",
        "password_env": "TEST_DATABASE_READER_PASSWORD",
    }

    with pytest.raises(RegistryConfigurationError, match="TEST_DATABASE_READER_PASSWORD"):
        _load_single_manifest(
            tmp_path,
            manifest,
            database_file=_write_database_profiles(tmp_path, databases),
            environment={"POSTGRES_PORT": "5432"},
        )


def test_rejects_password_authentication_for_non_test_source(tmp_path: Path) -> None:
    databases = _database_profiles()
    database = _development_database(databases)
    database["sslmode"] = "disable"
    database["authentication"] = {
        "type": "password",
        "password_env": "TEST_DATABASE_READER_PASSWORD",
    }

    with pytest.raises(RegistryConfigurationError, match="allowed only for test sources"):
        _load_single_manifest(
            tmp_path,
            _development_manifest(),
            database_file=_write_database_profiles(tmp_path, databases),
            environment={
                **DUMMY_ENVIRONMENT,
                "TEST_DATABASE_READER_PASSWORD": "fixture-password",
            },
        )


def test_rejects_unknown_database_profile(tmp_path: Path) -> None:
    manifest = _development_manifest()
    manifest["database_profile"] = "missing-database"

    with pytest.raises(RegistryConfigurationError, match="unknown database profile"):
        _load_single_manifest(tmp_path, manifest)


@pytest.mark.parametrize("version", [0, 2, "1", 1.0, True])
def test_rejects_non_v1_database_profile_file(
    tmp_path: Path,
    version: object,
) -> None:
    databases = _database_profiles()
    databases["version"] = version

    with pytest.raises(RegistryConfigurationError, match="version"):
        _load_single_manifest(
            tmp_path,
            _development_manifest(),
            database_file=_write_database_profiles(tmp_path, databases),
        )


def test_rejects_database_profile_unknown_authentication_field(tmp_path: Path) -> None:
    databases = _database_profiles()
    authentication = _development_database(databases)["authentication"]
    assert isinstance(authentication, dict)
    authentication["private_key"] = "must-not-be-versioned"

    with pytest.raises(RegistryConfigurationError, match="private_key"):
        _load_single_manifest(
            tmp_path,
            _development_manifest(),
            database_file=_write_database_profiles(tmp_path, databases),
        )


def test_blank_connection_host_environment_fails_closed() -> None:
    with pytest.raises(RegistryConfigurationError, match="resolved an invalid host"):
        load_test_registry({**DUMMY_ENVIRONMENT, "QUERY_MAN_POSTGRES_HOST": " "})


@pytest.mark.parametrize(
    "host",
    [
        "/var/run/postgresql",
        "@query-man",
        "db.example,/var/run/postgresql",
        "db.example,@query-man",
        "db.example,",
        "db.example, /var/run/postgresql",
    ],
)
def test_non_tcp_connection_host_fails_closed(host: str) -> None:
    with pytest.raises(RegistryConfigurationError, match="resolved an invalid host"):
        load_test_registry({**DUMMY_ENVIRONMENT, "QUERY_MAN_POSTGRES_HOST": host})


def test_rejects_flat_source_manifest(tmp_path: Path) -> None:
    (tmp_path / "development-issues.yaml").write_text("version: 4\n", encoding="utf-8")

    with pytest.raises(RegistryConfigurationError, match="Unexpected source entry"):
        SourceRegistry.load(
            tmp_path,
            ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
            ROOT_DIRECTORY / "config" / "database-profiles.yaml",
            Path("/run/secrets/query-man/databases"),
            DUMMY_ENVIRONMENT,
        )


def test_source_directory_name_must_match_source_id(tmp_path: Path) -> None:
    raw = _development_manifest()
    source_directory = tmp_path / "sources"
    source_path = source_directory / "wrong-name"
    source_path.mkdir(parents=True)
    (source_path / "source.yaml").write_text(
        yaml.safe_dump(raw),
        encoding="utf-8",
    )
    (source_path / "views.sql").write_text("-- reviewed\n", encoding="utf-8")

    with pytest.raises(RegistryConfigurationError, match="must match directory name"):
        SourceRegistry.load(
            source_directory,
            ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
            ROOT_DIRECTORY / "config" / "database-profiles.yaml",
            Path("/run/secrets/query-man/databases"),
            DUMMY_ENVIRONMENT,
        )


@pytest.mark.parametrize("artifact", ["source.yaml", "views.sql", "README.md"])
def test_source_directory_requires_exact_artifact_pair(
    tmp_path: Path,
    artifact: str,
) -> None:
    raw = _development_manifest()
    source_directory = tmp_path / "sources"
    source_path = source_directory / "development-issues"
    source_path.mkdir(parents=True)
    if artifact != "source.yaml":
        (source_path / "source.yaml").write_text(
            yaml.safe_dump(raw),
            encoding="utf-8",
        )
    if artifact != "views.sql":
        (source_path / "views.sql").write_text("-- reviewed\n", encoding="utf-8")
    if artifact == "README.md":
        (source_path / artifact).write_text("unexpected\n", encoding="utf-8")

    with pytest.raises(
        RegistryConfigurationError,
        match=r"exactly source\.yaml and views\.sql",
    ):
        SourceRegistry.load(
            source_directory,
            ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
            ROOT_DIRECTORY / "config" / "database-profiles.yaml",
            Path("/run/secrets/query-man/databases"),
            DUMMY_ENVIRONMENT,
        )


@pytest.mark.parametrize("artifact", ["source-directory", "source.yaml", "views.sql"])
def test_source_package_rejects_symlinks(
    tmp_path: Path,
    artifact: str,
) -> None:
    raw = _development_manifest()
    source_directory = tmp_path / "config" / "sources"
    source_directory.mkdir(parents=True)
    source_path = source_directory / "development-issues"
    if artifact == "source-directory":
        real_source_path = tmp_path / "real-source-package"
        real_source_path.mkdir()
        (real_source_path / "source.yaml").write_text(
            yaml.safe_dump(raw),
            encoding="utf-8",
        )
        (real_source_path / "views.sql").write_text(
            "-- reviewed\n",
            encoding="utf-8",
        )
        source_path.symlink_to(real_source_path, target_is_directory=True)
        expected = "Unexpected source entry"
    else:
        source_path.mkdir()
        external_file = tmp_path / artifact
        external_file.write_text(
            yaml.safe_dump(raw) if artifact == "source.yaml" else "-- reviewed\n",
            encoding="utf-8",
        )
        (source_path / artifact).symlink_to(external_file)
        other_artifact = "views.sql" if artifact == "source.yaml" else "source.yaml"
        (source_path / other_artifact).write_text(
            "-- reviewed\n" if other_artifact == "views.sql" else yaml.safe_dump(raw),
            encoding="utf-8",
        )
        expected = r"exactly source\.yaml and views\.sql"

    with pytest.raises(RegistryConfigurationError, match=expected):
        SourceRegistry.load(
            source_directory,
            ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
            ROOT_DIRECTORY / "config" / "database-profiles.yaml",
            Path("/run/secrets/query-man/databases"),
            DUMMY_ENVIRONMENT,
        )


def test_database_migration_ref_must_point_to_sibling_views_sql(
    tmp_path: Path,
) -> None:
    raw = _development_manifest()
    provenance = raw["provenance"]
    assert isinstance(provenance, dict)
    provenance["database_migration_ref"] = "unrelated/development-issues/views.sql"

    with pytest.raises(RegistryConfigurationError, match=r"sibling views\.sql"):
        _load_single_manifest(tmp_path, raw)


def test_system_schemas_are_rejected(tmp_path: Path) -> None:
    raw = _development_manifest()
    raw["source_id"] = "system-test"
    raw["allowed_schemas"] = ["pg_catalog"]
    raw["provenance"] = {
        "owner": "query-man",
        "environment": "test",
        "database_migration_ref": "config/sources/system-test/views.sql",
    }
    with pytest.raises(RegistryConfigurationError, match="system schema"):
        _load_single_manifest(tmp_path, raw)


@pytest.mark.parametrize("version", [0, 1, 2, 3, 4, 5, 7, "6", 6.0, True])
def test_rejects_non_v6_source_manifest(tmp_path: Path, version: object) -> None:
    raw = _development_manifest()
    raw["version"] = version

    with pytest.raises(RegistryConfigurationError, match="version"):
        _load_single_manifest(tmp_path, raw)


@pytest.mark.parametrize("version", [0, -1, "1", 1.0, True, None])
def test_rejects_invalid_view_contract_version(
    tmp_path: Path,
    version: object,
) -> None:
    raw = _development_manifest()
    raw["view_contract_version"] = version

    with pytest.raises(RegistryConfigurationError, match="view_contract_version"):
        _load_single_manifest(tmp_path, raw)


def test_accepts_arbitrarily_large_positive_view_contract_version(
    tmp_path: Path,
) -> None:
    raw = _development_manifest()
    raw["view_contract_version"] = 10**100

    source = _load_single_manifest(tmp_path, raw).get("development-issues")

    assert source is not None
    assert source.view_contract_version == 10**100


@pytest.mark.parametrize("sslmode", ["disable", "require", "verify-full"])
def test_fixture_password_authentication_accepts_supported_sslmode(
    tmp_path: Path,
    sslmode: str,
) -> None:
    manifest = _development_manifest()
    provenance = manifest["provenance"]
    assert isinstance(provenance, dict)
    provenance["environment"] = "test"
    databases = _database_profiles()
    database = _development_database(databases)
    database["sslmode"] = sslmode
    database["authentication"] = {
        "type": "password",
        "password_env": "TEST_DATABASE_READER_PASSWORD",
    }
    source = _load_single_manifest(
        tmp_path,
        manifest,
        database_file=_write_database_profiles(tmp_path, databases),
        environment={
            **DUMMY_ENVIRONMENT,
            "TEST_DATABASE_READER_PASSWORD": "fixture-password",
        },
    ).get("development-issues")

    assert source is not None
    assert source.connection.sslmode == sslmode
    assert source.connection.authentication == PasswordAuthentication("fixture-password")


@pytest.mark.parametrize("sslmode", ["disable", "require"])
def test_client_certificate_requires_verify_full(
    tmp_path: Path,
    sslmode: str,
) -> None:
    databases = _database_profiles()
    _development_database(databases)["sslmode"] = sslmode

    with pytest.raises(RegistryConfigurationError, match="requires sslmode verify-full"):
        _load_single_manifest(
            tmp_path,
            _development_manifest(),
            database_file=_write_database_profiles(tmp_path, databases),
        )


@pytest.mark.parametrize(
    "sslmode",
    ["prefer", "allow", "verify-ca", "unknown", "REQUIRE", True, None],
)
def test_rejects_unsupported_database_sslmode(
    tmp_path: Path,
    sslmode: object,
) -> None:
    databases = _database_profiles()
    _development_database(databases)["sslmode"] = sslmode

    with pytest.raises(RegistryConfigurationError, match="sslmode"):
        _load_single_manifest(
            tmp_path,
            _development_manifest(),
            database_file=_write_database_profiles(tmp_path, databases),
        )


def test_rejects_legacy_source_connection_fields(tmp_path: Path) -> None:
    manifest = _development_manifest()
    manifest["connection"] = {"password_env": "RETIRED_PASSWORD"}

    with pytest.raises(RegistryConfigurationError, match="connection"):
        _load_single_manifest(tmp_path, manifest)


def test_accepts_postgresql_identifier_boundary_in_source_fields(
    tmp_path: Path,
) -> None:
    raw = _development_manifest()
    identifier = "a" * POSTGRES_IDENTIFIER_MAX_LENGTH
    raw["allowed_schemas"] = [identifier]
    raw["budget_profile"] = identifier
    raw["reader_user"] = identifier
    databases = _database_profiles()
    _development_database(databases)["database"] = identifier
    database_file = _write_database_profiles(tmp_path, databases)
    budget_raw = yaml.safe_load((ROOT_DIRECTORY / "config" / "budget-profiles.yaml").read_text(encoding="utf-8"))
    assert isinstance(budget_raw, dict)
    profiles = budget_raw["profiles"]
    assert isinstance(profiles, dict)
    profiles[identifier] = profiles.pop("interactive")
    budget_file = tmp_path / "budget-profiles.yaml"
    budget_file.write_text(yaml.safe_dump(budget_raw), encoding="utf-8")

    source = _load_single_manifest(
        tmp_path,
        raw,
        budget_file=budget_file,
        database_file=database_file,
    ).get("development-issues")

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


def test_multiple_sources_can_reuse_one_database_profile(tmp_path: Path) -> None:
    source_directory = tmp_path / "config" / "sources"
    for source_id, reader_user in (
        ("first-source", "first_reader"),
        ("second-source", "second_reader"),
    ):
        manifest = _development_manifest()
        manifest["source_id"] = source_id
        manifest["reader_user"] = reader_user
        manifest["provenance"] = {
            "owner": "query-man",
            "environment": "test",
            "database_migration_ref": f"config/sources/{source_id}/views.sql",
        }
        package = source_directory / source_id
        package.mkdir(parents=True)
        (package / "source.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        (package / "views.sql").write_text("-- reviewed\n", encoding="utf-8")

    registry = SourceRegistry.load(
        source_directory,
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        ROOT_DIRECTORY / "config" / "database-profiles.yaml",
        Path("/run/secrets/query-man/databases"),
        DUMMY_ENVIRONMENT,
    )

    first = registry.get("first-source")
    second = registry.get("second-source")
    assert first is not None and second is not None
    assert first.connection.database_profile == second.connection.database_profile
    assert first.connection.database == second.connection.database
    assert first.connection.authentication == second.connection.authentication
    assert first.connection.user != second.connection.user


@pytest.mark.parametrize(
    "relation_kinds",
    [[], ["table"], ["materialized_view"], ["view", "table"], ["view", "view"]],
)
def test_source_relation_kind_allowlist_is_exactly_view(
    tmp_path: Path,
    relation_kinds: list[str],
) -> None:
    raw = _development_manifest()
    raw["allowed_relation_kinds"] = relation_kinds

    with pytest.raises(RegistryConfigurationError, match="allowed_relation_kinds"):
        _load_single_manifest(tmp_path, raw)


@pytest.mark.parametrize("retired_field", ["tenant_isolation", "semantic_overlay"])
def test_rejects_retired_source_policy_fields(
    tmp_path: Path,
    retired_field: str,
) -> None:
    raw = _development_manifest()
    raw[retired_field] = "rls" if retired_field == "tenant_isolation" else {}

    with pytest.raises(RegistryConfigurationError, match=retired_field):
        _load_single_manifest(tmp_path, raw)
