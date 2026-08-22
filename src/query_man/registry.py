from __future__ import annotations

import os
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from query_man.models import (
    BudgetProfile,
    BusinessPredicate,
    BusinessTermDefinition,
    CompositionHint,
    GrainDefinition,
    JoinDefinition,
    MeasureDefinition,
    QualityLevel,
    QuestionRule,
    RelationSemantic,
    ResolvedConnection,
    SemanticOverlay,
    SourceProfile,
    TenantIsolation,
)

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*$")]
RelationName = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*$")]
ShortText = Annotated[str, Field(min_length=1, max_length=200)]
Description = Annotated[str, Field(min_length=1, max_length=2000)]
_FORBIDDEN_SCHEMA = re.compile(
    r"^(?:information_schema|pg_catalog|pg_toast|pg_temp(?:_\d+)?|pg_toast_temp_\d+)$",
    re.IGNORECASE,
)


class RegistryConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class ValidatedSourceManifest:
    profile: SourceProfile
    document: dict[str, object]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _Budget(_StrictModel):
    metadata_statement_timeout_ms: int = Field(ge=100, le=30_000)
    query_statement_timeout_ms: int = Field(ge=100, le=60_000)
    query_transaction_timeout_ms: int = Field(ge=100, le=300_000)
    query_queue_timeout_ms: int = Field(ge=1, le=60_000)
    lock_timeout_ms: int = Field(ge=1, le=10_000)
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


class _BudgetFile(_StrictModel):
    version: int
    profiles: dict[str, _Budget]

    @model_validator(mode="after")
    def require_version_one(self) -> _BudgetFile:
        if self.version != 1:
            raise ValueError("version must be 1")
        return self


class _Connection(_StrictModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65_535)
    port_env: Identifier | None = None
    database: Identifier
    user: Identifier
    password_env: Identifier
    ssl: bool = False


class _Grain(_StrictModel):
    name: Identifier
    description: Description
    key_columns: list[Identifier] = Field(min_length=1, max_length=10)


class _Measure(_StrictModel):
    name: Identifier
    description: Description
    aliases: list[ShortText] = Field(default_factory=list, max_length=30)
    aggregation: str
    column: Identifier | None = None
    numerator_measure: Identifier | None = None
    denominator_measure: Identifier | None = None

    @model_validator(mode="after")
    def valid_shape(self) -> _Measure:
        if self.aggregation not in {"count_rows", "sum", "ratio"}:
            raise ValueError("invalid aggregation")
        if self.aggregation == "sum" and self.column is None:
            raise ValueError("sum requires column")
        if self.aggregation == "ratio" and (self.numerator_measure is None or self.denominator_measure is None):
            raise ValueError("ratio requires numerator_measure and denominator_measure")
        if self.aggregation != "sum" and self.column is not None:
            raise ValueError("column is only valid for sum")
        if self.aggregation != "ratio" and (self.numerator_measure is not None or self.denominator_measure is not None):
            raise ValueError("measure references are only valid for ratio")
        return self


class _RelationSemantic(_StrictModel):
    relation: RelationName
    role: str
    description: Description | None = None
    aliases: list[ShortText] = Field(default_factory=list, max_length=50)
    grain: _Grain | None = None
    default_time_column: Identifier | None = None
    use_for: list[ShortText] = Field(default_factory=list, max_length=50)
    column_aliases: dict[Identifier, list[ShortText]] = Field(default_factory=dict)
    value_hints: dict[Identifier, list[ShortText]] = Field(default_factory=dict)
    measures: list[_Measure] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_role(self) -> _RelationSemantic:
        if self.role not in {"event", "comment", "population", "dimension", "other"}:
            raise ValueError("invalid relation role")
        return self


class _Predicate(_StrictModel):
    relation: RelationName
    column: Identifier
    operator: str
    values: list[ShortText] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def valid_shape(self) -> _Predicate:
        valued = {"equal", "not_equal", "in", "not_in"}
        unary = {"is_null", "is_not_null"}
        if self.operator in valued and not self.values:
            raise ValueError("predicate values are required")
        if self.operator in unary and self.values:
            raise ValueError("unary predicate cannot contain values")
        if self.operator not in valued | unary:
            raise ValueError("invalid predicate operator")
        return self


class _BusinessTerm(_StrictModel):
    name: Identifier
    description: Description
    aliases: list[ShortText] = Field(min_length=1, max_length=50)
    predicates: list[_Predicate] = Field(default_factory=list, max_length=20)
    calculation: Description | None = None


class _QuestionRule(_StrictModel):
    code: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)]
    status: str
    phrases: list[ShortText] = Field(min_length=1, max_length=50)
    message: Description
    missing_concepts: list[ShortText] = Field(default_factory=list, max_length=30)
    options: list[Description] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_status(self) -> _QuestionRule:
        if self.status not in {"needs_clarification", "unsupported"}:
            raise ValueError("invalid question rule status")
        return self


class _CompositionHint(_StrictModel):
    name: Identifier
    phrases: list[ShortText] = Field(min_length=1, max_length=50)
    strategy: str
    guidance: Description
    combine_keys: list[Identifier] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid_strategy(self) -> _CompositionHint:
        if self.strategy != "aggregate_each_then_combine":
            raise ValueError("invalid composition strategy")
        return self


class _JoinPair(_StrictModel):
    left: Identifier
    right: Identifier


class _Join(_StrictModel):
    left_relation: RelationName
    right_relation: RelationName
    column_pairs: list[_JoinPair] = Field(min_length=1, max_length=10)
    cardinality: str
    fanout: bool
    guidance: Description

    @model_validator(mode="after")
    def valid_cardinality(self) -> _Join:
        if self.cardinality not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}:
            raise ValueError("invalid cardinality")
        return self


class _SemanticOverlay(_StrictModel):
    default_relation: RelationName | None = None
    relations: list[_RelationSemantic] = Field(default_factory=list, max_length=200)
    joins: list[_Join] = Field(default_factory=list, max_length=500)
    business_terms: list[_BusinessTerm] = Field(default_factory=list, max_length=200)
    question_rules: list[_QuestionRule] = Field(default_factory=list, max_length=200)
    composition_hints: list[_CompositionHint] = Field(default_factory=list, max_length=200)


class _SourceFile(_StrictModel):
    version: int
    source_id: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)]
    name: ShortText
    description: Description
    connection: _Connection
    allowed_schemas: list[Identifier] = Field(min_length=1, max_length=20)
    allowed_relation_kinds: list[str] = Field(default_factory=lambda: ["view"], min_length=1, max_length=4)
    budget_profile: Identifier
    minimum_quality_level: QualityLevel = "L0"
    tenant_isolation: TenantIsolation = "none"
    semantic_overlay: _SemanticOverlay = Field(default_factory=_SemanticOverlay)

    @model_validator(mode="after")
    def valid_values(self) -> _SourceFile:
        if self.version != 1:
            raise ValueError("version must be 1")
        allowed = {"table", "partitioned_table", "view", "materialized_view"}
        if any(kind not in allowed for kind in self.allowed_relation_kinds):
            raise ValueError("invalid relation kind")
        if self.tenant_isolation == "rls" and self.allowed_relation_kinds != ["view"]:
            raise ValueError("RLS sources must expose security-invoker views only")
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
            files = sorted(
                path
                for path in source_directory.iterdir()
                if path.is_file() and path.suffix.lower() in {".yaml", ".yml"}
            )
        except OSError as error:
            raise RegistryConfigurationError(f"Cannot read {source_directory}: {error}") from error
        if not files:
            raise RegistryConfigurationError(f"No source manifests found in {source_directory.resolve()}")
        sources: list[SourceProfile] = []
        seen: set[str] = set()
        for path in files:
            parsed = _parse_source_file(path)
            if parsed.source_id in seen:
                raise RegistryConfigurationError(f"Duplicate source_id: {parsed.source_id}")
            seen.add(parsed.source_id)
            sources.append(_resolve_source(parsed, budgets, env, path))
        return cls(sources)

    def list(self) -> list[dict[str, str]]:
        return sorted(
            (
                {"source_id": source.source_id, "name": source.name, "description": source.description}
                for source in self._sources.values()
            ),
            key=lambda item: item["source_id"],
        )

    def get(self, source_id: str) -> SourceProfile | None:
        return self._sources.get(source_id)

    def upsert(self, source: SourceProfile) -> None:
        self._sources = {**self._sources, source.source_id: source}

    def remove(self, source_id: str) -> None:
        self._sources = {
            current_id: source
            for current_id, source in self._sources.items()
            if current_id != source_id
        }

    def source_ids(self) -> frozenset[str]:
        return frozenset(self._sources)

    def __len__(self) -> int:
        return len(self._sources)


def _parse_model[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        with path.open(encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
        return model.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise RegistryConfigurationError(f"Invalid configuration in {path}: {error}") from error


def _parse_source_file(path: Path) -> _SourceFile:
    try:
        with path.open(encoding="utf-8") as stream:
            return _SourceFile.model_validate(migrate_source_manifest(yaml.safe_load(stream)))
    except (OSError, yaml.YAMLError, ValidationError, ValueError) as error:
        raise RegistryConfigurationError(f"Invalid configuration in {path}: {error}") from error


def load_budget_profiles(path: Path) -> dict[str, BudgetProfile]:
    parsed = _parse_model(path, _BudgetFile)
    return {name: BudgetProfile(name=name, **profile.model_dump()) for name, profile in parsed.profiles.items()}


def validate_source_manifest(
    raw: object,
    budgets: Mapping[str, BudgetProfile],
    secret: str,
    *,
    origin: str = "control-plane source manifest",
) -> ValidatedSourceManifest:
    try:
        migrated = migrate_source_manifest(raw)
        parsed = _SourceFile.model_validate(migrated)
    except (ValidationError, ValueError) as error:
        raise RegistryConfigurationError(f"Invalid configuration in {origin}: {error}") from error
    environment = dict(os.environ)
    environment[parsed.connection.password_env] = secret
    profile = _resolve_source(
        parsed,
        dict(budgets),
        environment,
        origin,
    )
    document = parsed.model_dump(mode="json", exclude_none=True)
    connection = document["connection"]
    if not isinstance(connection, dict):
        raise RegistryConfigurationError("Control-plane connection must be an object")
    connection["port"] = profile.connection.port
    connection.pop("port_env", None)
    return ValidatedSourceManifest(
        profile,
        document,
    )


def migrate_source_manifest(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        raise ValueError("source manifest must be an object")
    document = deepcopy(raw)
    version = document.get("version")
    if version == 0:
        if "budget" in document and "budget_profile" not in document:
            document["budget_profile"] = document.pop("budget")
        document["version"] = 1
    elif version != 1:
        raise ValueError("unsupported source manifest version")
    return document


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
    raw_port = environment.get(parsed.connection.port_env) if parsed.connection.port_env is not None else None
    try:
        port = parsed.connection.port if raw_port is None else int(raw_port)
    except ValueError as error:
        raise RegistryConfigurationError(f"{path} resolved an invalid port") from error
    if not 1 <= port <= 65_535:
        raise RegistryConfigurationError(f"{path} resolved an invalid port")
    _require_unique(path, "allowed_schemas", parsed.allowed_schemas)
    _require_unique(path, "allowed_relation_kinds", parsed.allowed_relation_kinds)
    for schema in parsed.allowed_schemas:
        if _FORBIDDEN_SCHEMA.match(schema) or schema.casefold().startswith("pg_"):
            raise RegistryConfigurationError(f"{path} cannot publish PostgreSQL system schema: {schema}")

    overlay = _build_overlay(parsed.semantic_overlay)
    _validate_overlay(path, parsed.allowed_schemas, overlay)
    return SourceProfile(
        source_id=parsed.source_id,
        name=parsed.name,
        description=parsed.description,
        connection=ResolvedConnection(
            host=parsed.connection.host,
            port=port,
            database=parsed.connection.database,
            user=parsed.connection.user,
            password=password,
            ssl=parsed.connection.ssl,
        ),
        allowed_schemas=list(dict.fromkeys(parsed.allowed_schemas)),
        allowed_relation_kinds=list(dict.fromkeys(parsed.allowed_relation_kinds)),  # type: ignore[arg-type]
        budget=budget,
        semantic_overlay=overlay,
        minimum_quality_level=parsed.minimum_quality_level,
        tenant_isolation=parsed.tenant_isolation,
    )


def _build_overlay(raw: _SemanticOverlay) -> SemanticOverlay:
    relations = [
        RelationSemantic(
            relation=item.relation,
            role=item.role,  # type: ignore[arg-type]
            description=item.description,
            aliases=_unique(item.aliases),
            grain=(
                GrainDefinition(
                    name=item.grain.name,
                    description=item.grain.description,
                    key_columns=_unique(item.grain.key_columns),
                )
                if item.grain
                else None
            ),
            default_time_column=item.default_time_column,
            use_for=_unique(item.use_for),
            column_aliases={key: _unique(value) for key, value in item.column_aliases.items()},
            value_hints={key: _unique(value) for key, value in item.value_hints.items()},
            measures=[
                MeasureDefinition(
                    name=measure.name,
                    description=measure.description,
                    aliases=_unique(measure.aliases),
                    aggregation=measure.aggregation,  # type: ignore[arg-type]
                    column=measure.column,
                    numerator_measure=measure.numerator_measure,
                    denominator_measure=measure.denominator_measure,
                )
                for measure in item.measures
            ],
        )
        for item in raw.relations
    ]
    return SemanticOverlay(
        default_relation=raw.default_relation,
        relations=relations,
        joins=[
            JoinDefinition(
                left_relation=item.left_relation,
                right_relation=item.right_relation,
                column_pairs=[pair.model_dump() for pair in item.column_pairs],
                cardinality=item.cardinality,  # type: ignore[arg-type]
                fanout=item.fanout,
                guidance=item.guidance,
            )
            for item in raw.joins
        ],
        business_terms=[
            BusinessTermDefinition(
                name=item.name,
                description=item.description,
                aliases=_unique(item.aliases),
                predicates=[
                    BusinessPredicate(
                        relation=predicate.relation,
                        column=predicate.column,
                        operator=predicate.operator,  # type: ignore[arg-type]
                        values=_unique(predicate.values),
                    )
                    for predicate in item.predicates
                ],
                calculation=item.calculation,
            )
            for item in raw.business_terms
        ],
        question_rules=[
            QuestionRule(
                code=item.code,
                status=item.status,  # type: ignore[arg-type]
                phrases=_unique(item.phrases),
                message=item.message,
                missing_concepts=_unique(item.missing_concepts),
                options=_unique(item.options),
            )
            for item in raw.question_rules
        ],
        composition_hints=[
            CompositionHint(
                name=item.name,
                phrases=_unique(item.phrases),
                strategy="aggregate_each_then_combine",
                guidance=item.guidance,
                combine_keys=_unique(item.combine_keys),
            )
            for item in raw.composition_hints
        ],
    )


def _validate_overlay(path: Path | str, allowed_schemas: list[str], overlay: SemanticOverlay) -> None:
    relation_names = [item.relation for item in overlay.relations]
    _require_unique(path, "relation semantics", relation_names)
    names = set(relation_names)
    for relation in overlay.relations:
        if relation.relation.split(".", 1)[0] not in allowed_schemas:
            raise RegistryConfigurationError(f"{path} relation {relation.relation} is outside allowed_schemas")
        measure_names = [measure.name for measure in relation.measures]
        _require_unique(path, f"measure {relation.relation}", measure_names)
        for measure in relation.measures:
            if measure.aggregation == "ratio" and (
                measure.numerator_measure not in measure_names or measure.denominator_measure not in measure_names
            ):
                raise RegistryConfigurationError(
                    f"{path} ratio measure {relation.relation}.{measure.name} references an unknown measure"
                )
    if overlay.default_relation and overlay.default_relation not in names:
        raise RegistryConfigurationError(f"{path} default_relation has no semantic definition")
    for join in overlay.joins:
        if join.left_relation not in names or join.right_relation not in names:
            raise RegistryConfigurationError(f"{path} join references a relation without semantic definition")
    _require_unique(path, "business term names", [item.name for item in overlay.business_terms])
    _require_unique(path, "question rule names", [item.code for item in overlay.question_rules])
    _require_unique(path, "composition hint names", [item.name for item in overlay.composition_hints])
    for term in overlay.business_terms:
        if any(predicate.relation not in names for predicate in term.predicates):
            raise RegistryConfigurationError(f"{path} business term {term.name} references an unknown relation")


def _require_unique(path: Path | str, kind: str, values: list[str]) -> None:
    if len(set(values)) != len(values):
        raise RegistryConfigurationError(f"{path} contains duplicate {kind}")


def _unique[T](values: list[T]) -> list[T]:
    return list(dict.fromkeys(values))
