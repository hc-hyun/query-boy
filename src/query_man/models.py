from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

RelationRole = Literal["event", "comment", "population", "dimension", "other"]
QualityLevel = Literal["L0", "L1", "L2"]
TenantIsolation = Literal["none", "rls"]
AllowedRelationKind = Literal["table", "partitioned_table", "view", "materialized_view"]
CatalogRelationKind = Literal["table", "partitioned_table", "view", "materialized_view", "foreign_table"]
Nullability = bool | Literal["unknown"]


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
class GrainDefinition:
    name: str
    description: str
    key_columns: list[str]


@dataclass(frozen=True)
class MeasureDefinition:
    name: str
    description: str
    aliases: list[str]
    aggregation: Literal["count_rows", "sum", "ratio"]
    column: str | None = None
    numerator_measure: str | None = None
    denominator_measure: str | None = None


@dataclass(frozen=True)
class RelationSemantic:
    relation: str
    role: RelationRole
    description: str | None
    aliases: list[str]
    grain: GrainDefinition | None
    default_time_column: str | None
    use_for: list[str]
    column_aliases: dict[str, list[str]]
    value_hints: dict[str, list[str]]
    measures: list[MeasureDefinition]


@dataclass(frozen=True)
class BusinessPredicate:
    relation: str
    column: str
    operator: Literal["equal", "not_equal", "in", "not_in", "is_null", "is_not_null"]
    values: list[str]


@dataclass(frozen=True)
class BusinessTermDefinition:
    name: str
    description: str
    aliases: list[str]
    predicates: list[BusinessPredicate]
    calculation: str | None


@dataclass(frozen=True)
class QuestionRule:
    code: str
    status: Literal["needs_clarification", "unsupported"]
    phrases: list[str]
    message: str
    missing_concepts: list[str]
    options: list[str]


@dataclass(frozen=True)
class CompositionHint:
    name: str
    phrases: list[str]
    strategy: Literal["aggregate_each_then_combine"]
    guidance: str
    combine_keys: list[str]


@dataclass(frozen=True)
class JoinDefinition:
    left_relation: str
    right_relation: str
    column_pairs: list[dict[str, str]]
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"]
    fanout: bool
    guidance: str


@dataclass(frozen=True)
class SemanticOverlay:
    default_relation: str | None
    relations: list[RelationSemantic]
    joins: list[JoinDefinition]
    business_terms: list[BusinessTermDefinition]
    question_rules: list[QuestionRule]
    composition_hints: list[CompositionHint]


@dataclass(frozen=True)
class SourceProfile:
    source_id: str
    name: str
    description: str
    connection: ResolvedConnection
    allowed_schemas: list[str]
    allowed_relation_kinds: list[AllowedRelationKind]
    budget: BudgetProfile
    semantic_overlay: SemanticOverlay
    minimum_quality_level: QualityLevel = "L0"
    tenant_isolation: TenantIsolation = "none"
    control_generation: int | None = None


@dataclass
class CatalogColumn:
    name: str
    sql_name: str
    ordinal: int
    data_type: str
    nullable: Nullability
    comment: str | None = None


@dataclass(frozen=True)
class CatalogForeignKey:
    columns: list[str]
    referenced_relation: str
    referenced_columns: list[str]


@dataclass(frozen=True)
class CatalogIndex:
    columns: list[str]
    unique: bool
    primary: bool


@dataclass
class CatalogRelation:
    schema: str
    name: str
    qualified_name: str
    sql_name: str
    kind: CatalogRelationKind
    columns: list[CatalogColumn]
    comment: str | None = None
    estimated_rows: int | None = None
    definition_hash: str | None = None
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[CatalogForeignKey] = field(default_factory=list)
    indexes: list[CatalogIndex] = field(default_factory=list)
    security_invoker: bool = False


@dataclass
class CatalogSnapshot:
    relations: list[CatalogRelation] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedMetadata:
    snapshot: CatalogSnapshot
    revision: str


class CatalogProvider(Protocol):
    async def load(self, source: SourceProfile) -> CatalogSnapshot: ...

    async def close(self) -> None: ...
