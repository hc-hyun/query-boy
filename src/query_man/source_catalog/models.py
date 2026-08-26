from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

RelationRole = Literal["event", "comment", "population", "dimension", "other"]
QualityLevel = Literal["L0", "L1", "L2"]
TenantIsolation = Literal["none", "rls"]
SourceEnvironment = Literal["production", "staging", "development", "test"]
AllowedRelationKind = Literal["table", "partitioned_table", "view", "materialized_view"]


@dataclass(frozen=True)
class BudgetProfile:
    name: str
    version: int
    metadata_statement_timeout_ms: int
    query_statement_timeout_ms: int
    query_transaction_timeout_ms: int
    query_queue_timeout_ms: int
    lock_timeout_ms: int
    work_mem_kb: int
    temp_file_limit_kb: int
    max_parallel_workers_per_gather: int
    jit_enabled: bool
    max_pool_size: int
    max_concurrent_queries: int
    max_metadata_relations: int
    max_metadata_columns: int
    max_columns_per_relation: int
    max_context_columns_per_relation: int
    max_metadata_response_bytes: int
    max_result_rows: int
    max_result_bytes: int
    max_sql_bytes: int
    max_plan_total_cost: int
    max_plan_rows: int
    max_plan_nodes: int


@dataclass(frozen=True)
class ResolvedConnection:
    host: str
    port: int
    database: str
    user: str
    password: str
    ssl: bool


@dataclass(frozen=True)
class SourceProvenance:
    owner: str
    environment: SourceEnvironment
    database_migration_ref: str


@dataclass(frozen=True)
class RepresentativeRecordsTarget:
    grain: str
    physical_relation: str


@dataclass(frozen=True)
class ResourceObservationDefinition:
    representative_records: RepresentativeRecordsTarget
    storage_relations: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "storage_relations", tuple(self.storage_relations))


@dataclass(frozen=True)
class GrainDefinition:
    name: str
    description: str
    key_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_columns", tuple(self.key_columns))


@dataclass(frozen=True)
class MeasureDefinition:
    name: str
    description: str
    aliases: tuple[str, ...]
    aggregation: Literal["count_rows", "sum", "ratio"]
    column: str | None = None
    numerator_measure: str | None = None
    denominator_measure: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))


@dataclass(frozen=True)
class RelationSemantic:
    relation: str
    role: RelationRole
    description: str | None
    aliases: tuple[str, ...]
    grain: GrainDefinition | None
    default_time_column: str | None
    use_for: tuple[str, ...]
    column_aliases: Mapping[str, tuple[str, ...]]
    value_hints: Mapping[str, tuple[str, ...]]
    measures: tuple[MeasureDefinition, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "use_for", tuple(self.use_for))
        object.__setattr__(self, "column_aliases", _freeze_string_sequences(self.column_aliases))
        object.__setattr__(self, "value_hints", _freeze_string_sequences(self.value_hints))
        object.__setattr__(self, "measures", tuple(self.measures))


@dataclass(frozen=True)
class BusinessPredicate:
    relation: str
    column: str
    operator: Literal["equal", "not_equal", "in", "not_in", "is_null", "is_not_null"]
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", tuple(self.values))


@dataclass(frozen=True)
class BusinessTermDefinition:
    name: str
    description: str
    aliases: tuple[str, ...]
    predicates: tuple[BusinessPredicate, ...]
    calculation: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "predicates", tuple(self.predicates))


@dataclass(frozen=True)
class QuestionRule:
    code: str
    status: Literal["needs_clarification", "unsupported"]
    phrases: tuple[str, ...]
    message: str
    missing_concepts: tuple[str, ...]
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "phrases", tuple(self.phrases))
        object.__setattr__(self, "missing_concepts", tuple(self.missing_concepts))
        object.__setattr__(self, "options", tuple(self.options))


@dataclass(frozen=True)
class CompositionHint:
    name: str
    phrases: tuple[str, ...]
    strategy: Literal["aggregate_each_then_combine"]
    guidance: str
    combine_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "phrases", tuple(self.phrases))
        object.__setattr__(self, "combine_keys", tuple(self.combine_keys))


@dataclass(frozen=True)
class JoinDefinition:
    left_relation: str
    right_relation: str
    column_pairs: tuple[Mapping[str, str], ...]
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    fanout: bool
    guidance: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "column_pairs",
            tuple(MappingProxyType(dict(pair)) for pair in self.column_pairs),
        )


@dataclass(frozen=True)
class SemanticOverlay:
    default_relation: str | None
    relations: tuple[RelationSemantic, ...]
    joins: tuple[JoinDefinition, ...]
    business_terms: tuple[BusinessTermDefinition, ...]
    question_rules: tuple[QuestionRule, ...]
    composition_hints: tuple[CompositionHint, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "relations", tuple(self.relations))
        object.__setattr__(self, "joins", tuple(self.joins))
        object.__setattr__(self, "business_terms", tuple(self.business_terms))
        object.__setattr__(self, "question_rules", tuple(self.question_rules))
        object.__setattr__(self, "composition_hints", tuple(self.composition_hints))


@dataclass(frozen=True)
class SourceProfile:
    source_id: str
    name: str
    description: str
    connection: ResolvedConnection
    allowed_schemas: tuple[str, ...]
    allowed_relation_kinds: tuple[AllowedRelationKind, ...]
    budget: BudgetProfile
    semantic_overlay: SemanticOverlay
    provenance: SourceProvenance
    minimum_quality_level: QualityLevel = "L0"
    tenant_isolation: TenantIsolation = "none"
    control_generation: int | None = None
    control_state_version: int | None = None
    observability: ResourceObservationDefinition | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_schemas", tuple(self.allowed_schemas))
        object.__setattr__(self, "allowed_relation_kinds", tuple(self.allowed_relation_kinds))


def _freeze_string_sequences(
    values: Mapping[str, tuple[str, ...]],
) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({key: tuple(items) for key, items in values.items()})
