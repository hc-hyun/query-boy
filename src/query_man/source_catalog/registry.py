from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from query_man.source_catalog.models import (
    BudgetProfile,
    ResolvedConnection,
    SourceEnvironment,
    SourceProfile,
    SourceProvenance,
    SSLMode,
)

POSTGRES_IDENTIFIER_MAX_LENGTH = 63
IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$"
STABLE_SLUG_MAX_LENGTH = 80
STABLE_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

Identifier = Annotated[
    str,
    Field(pattern=IDENTIFIER_PATTERN, max_length=POSTGRES_IDENTIFIER_MAX_LENGTH),
]
EnvironmentVariableName = Annotated[
    str,
    Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=128),
]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
Description = Annotated[str, Field(min_length=1, max_length=2000)]
StableSlug = Annotated[
    str,
    Field(pattern=STABLE_SLUG_PATTERN, max_length=STABLE_SLUG_MAX_LENGTH),
]
DatabaseMigrationRef = Annotated[str, Field(min_length=1, max_length=255)]


def _is_forbidden_schema(schema: str) -> bool:
    normalized = schema.casefold()
    return normalized == "information_schema" or normalized.startswith("pg_")


class RegistryConfigurationError(Exception):
    pass


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Budget(_StrictModel):
    metadata_statement_timeout_ms: int = Field(ge=100, le=30_000)
    query_statement_timeout_ms: int = Field(ge=100, le=60_000)
    query_transaction_timeout_ms: int = Field(ge=100, le=300_000)
    query_queue_timeout_ms: int = Field(ge=1, le=60_000)
    lock_timeout_ms: int = Field(ge=1, le=10_000)
    work_mem_kb: int = Field(ge=64, le=65_536)
    temp_file_limit_kb: int = Field(ge=0, le=1_048_576)
    max_parallel_workers_per_gather: int = Field(ge=0, le=4)
    jit_enabled: bool = Field(strict=True)
    max_pool_size: int = Field(ge=1, le=20)
    max_concurrent_queries: int = Field(ge=1, le=20)
    max_metadata_relations: int = Field(ge=1, le=10_000)
    max_metadata_columns: int = Field(ge=1, le=100_000)
    max_columns_per_relation: int = Field(ge=1, le=1_600)
    max_context_columns_per_relation: int = Field(ge=1, le=200)
    max_metadata_response_bytes: int = Field(ge=1_024, le=100 * 1_024 * 1_024)
    max_result_rows: int = Field(ge=1, le=100_000)
    max_result_bytes: int = Field(ge=1_024, le=100 * 1_024 * 1_024)
    max_sql_bytes: int = Field(ge=1_024, le=10 * 1_024 * 1_024)
    max_plan_total_cost: int = Field(ge=1, le=2_147_483_647)
    max_plan_rows: int = Field(ge=1, le=2_147_483_647)
    max_plan_nodes: int = Field(ge=1, le=100_000)

    @model_validator(mode="after")
    def require_pool_capacity_for_admitted_queries(self) -> _Budget:
        if self.max_concurrent_queries > self.max_pool_size:
            raise ValueError("max_concurrent_queries must be less than or equal to max_pool_size")
        return self


class _BudgetFile(_StrictModel):
    version: int = Field(strict=True)
    profiles: dict[Identifier, _Budget]

    @model_validator(mode="after")
    def require_version_two(self) -> _BudgetFile:
        if self.version != 2:
            raise ValueError("version must be 2")
        return self


class _Connection(_StrictModel):
    host: str = Field(min_length=1, max_length=253)
    host_env: EnvironmentVariableName | None = None
    port: int = Field(ge=1, le=65_535)
    port_env: EnvironmentVariableName | None = None
    database: Identifier
    user: Identifier
    password_env: EnvironmentVariableName
    sslmode: SSLMode


class _Provenance(_StrictModel):
    owner: StableSlug
    environment: SourceEnvironment
    database_migration_ref: DatabaseMigrationRef

    @field_validator("database_migration_ref", mode="before")
    @classmethod
    def reject_ascii_control_characters(cls, value: object) -> object:
        if isinstance(value, str) and any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("database_migration_ref cannot contain ASCII control characters")
        return value


class _SourceFile(_StrictModel):
    version: int = Field(strict=True)
    source_id: StableSlug
    name: ShortText
    description: Description
    provenance: _Provenance
    connection: _Connection
    allowed_schemas: list[Identifier] = Field(min_length=1, max_length=20)
    allowed_relation_kinds: list[Literal["view"]] = Field(min_length=1, max_length=1)
    view_contract_version: int = Field(strict=True, ge=1)
    budget_profile: Identifier

    @model_validator(mode="after")
    def require_current_contract(self) -> _SourceFile:
        if self.version != 5:
            raise ValueError("version must be 5")
        if self.allowed_relation_kinds != ["view"]:
            raise ValueError("allowed_relation_kinds must be exactly [view]")
        return self


class SourceRegistry:
    def __init__(self, sources: list[SourceProfile]) -> None:
        self._sources = {source.source_id: source for source in sources}

    @classmethod
    def load(
        cls,
        source_directory: Path,
        budget_file: Path,
        environment: Mapping[str, str] | None = None,
    ) -> SourceRegistry:
        env = os.environ if environment is None else environment
        budgets = load_budget_profiles(budget_file)
        try:
            source_paths = sorted(source_directory.iterdir())
        except OSError as error:
            raise RegistryConfigurationError(f"Cannot read {source_directory}: {error}") from error
        if not source_paths:
            raise RegistryConfigurationError(f"No source directories found in {source_directory.resolve()}")

        sources: list[SourceProfile] = []
        seen: set[str] = set()
        for source_path in source_paths:
            if source_path.is_symlink() or not source_path.is_dir():
                raise RegistryConfigurationError(f"Unexpected source entry: {source_path.resolve()}")
            _require_source_package(source_path)
            manifest_path = source_path / "source.yaml"
            parsed = _parse_model(manifest_path, _SourceFile)
            if source_path.name != parsed.source_id:
                raise RegistryConfigurationError(
                    f"{manifest_path} source_id must match directory name {source_path.name}"
                )
            _require_sibling_view_reference(source_path, parsed, manifest_path)
            if parsed.source_id in seen:
                raise RegistryConfigurationError(f"Duplicate source_id: {parsed.source_id}")
            seen.add(parsed.source_id)
            sources.append(_resolve_source(parsed, budgets, env, manifest_path))
        return cls(sources)

    def list(self) -> list[dict[str, str]]:
        return sorted(
            (
                {
                    "source_id": source.source_id,
                    "name": source.name,
                    "description": source.description,
                }
                for source in self._sources.values()
            ),
            key=lambda item: item["source_id"],
        )

    def get(self, source_id: str) -> SourceProfile | None:
        return self._sources.get(source_id)

    def source_ids(self) -> frozenset[str]:
        return frozenset(self._sources)

    def __len__(self) -> int:
        return len(self._sources)


def _require_source_package(source_path: Path) -> None:
    try:
        source_files = sorted(source_path.iterdir())
    except OSError as error:
        raise RegistryConfigurationError(f"Cannot read {source_path}: {error}") from error
    if {path.name for path in source_files} != {"source.yaml", "views.sql"} or any(
        path.is_symlink() or not path.is_file() for path in source_files
    ):
        raise RegistryConfigurationError(f"{source_path} must contain exactly source.yaml and views.sql")


def _require_sibling_view_reference(
    source_path: Path,
    parsed: _SourceFile,
    manifest_path: Path,
) -> None:
    try:
        config_index = max(index for index, part in enumerate(source_path.parts) if part == "config")
    except ValueError as error:
        raise RegistryConfigurationError(f"{manifest_path} source package must be below config") from error
    expected = PurePosixPath(*source_path.parts[config_index:], "views.sql")
    if PurePosixPath(parsed.provenance.database_migration_ref) != expected:
        raise RegistryConfigurationError(f"{manifest_path} database_migration_ref must reference sibling views.sql")


def _parse_model[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        with path.open(encoding="utf-8") as stream:
            return model.model_validate(yaml.safe_load(stream))
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise RegistryConfigurationError(f"Invalid configuration in {path}: {error}") from error


def load_budget_profiles(path: Path) -> dict[str, BudgetProfile]:
    parsed = _parse_model(path, _BudgetFile)
    return {
        name: BudgetProfile(name=name, version=parsed.version, **profile.model_dump())
        for name, profile in parsed.profiles.items()
    }


def _resolve_source(
    parsed: _SourceFile,
    budgets: dict[str, BudgetProfile],
    environment: Mapping[str, str],
    path: Path | str,
) -> SourceProfile:
    budget = budgets.get(parsed.budget_profile)
    if budget is None:
        raise RegistryConfigurationError(f"{path} references unknown budget profile: {parsed.budget_profile}")
    expected_secret = f"{parsed.source_id.replace('-', '_').upper()}_READER_PASSWORD"
    if parsed.connection.password_env != expected_secret:
        raise RegistryConfigurationError(f"{path} must use the source-scoped secret {expected_secret}")
    password = environment.get(parsed.connection.password_env)
    if not password:
        raise RegistryConfigurationError(f"{path} requires environment variable {parsed.connection.password_env}")

    raw_host = environment.get(parsed.connection.host_env) if parsed.connection.host_env is not None else None
    host = parsed.connection.host if raw_host is None else raw_host.strip()
    host_parts = host.split(",")
    normalized_hosts = tuple(item.strip() for item in host_parts)
    if (
        not host
        or len(host) > 253
        or any(
            not item or item != raw_item or item.startswith(("/", "@"))
            for raw_item, item in zip(host_parts, normalized_hosts, strict=True)
        )
    ):
        raise RegistryConfigurationError(f"{path} resolved an invalid host")

    raw_port = environment.get(parsed.connection.port_env) if parsed.connection.port_env is not None else None
    try:
        port = parsed.connection.port if raw_port is None else int(raw_port)
    except ValueError as error:
        raise RegistryConfigurationError(f"{path} resolved an invalid port") from error
    if not 1 <= port <= 65_535:
        raise RegistryConfigurationError(f"{path} resolved an invalid port")

    _require_unique(path, "allowed_schemas", parsed.allowed_schemas)
    for schema in parsed.allowed_schemas:
        if _is_forbidden_schema(schema):
            raise RegistryConfigurationError(f"{path} cannot publish PostgreSQL system schema: {schema}")

    return SourceProfile(
        source_id=parsed.source_id,
        name=parsed.name,
        description=parsed.description,
        connection=ResolvedConnection(
            host=host,
            port=port,
            database=parsed.connection.database,
            user=parsed.connection.user,
            password=password,
            sslmode=parsed.connection.sslmode,
        ),
        allowed_schemas=tuple(parsed.allowed_schemas),
        allowed_relation_kinds=("view",),
        view_contract_version=parsed.view_contract_version,
        budget=budget,
        provenance=SourceProvenance(
            owner=parsed.provenance.owner,
            environment=parsed.provenance.environment,
            database_migration_ref=parsed.provenance.database_migration_ref,
        ),
    )


def _require_unique(path: Path | str, kind: str, values: Sequence[str]) -> None:
    if len(set(values)) != len(values):
        raise RegistryConfigurationError(f"{path} contains duplicate {kind}")
