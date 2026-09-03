from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SourceEnvironment = Literal["production", "staging", "development", "test"]
AllowedRelationKind = Literal["view"]
SSLMode = Literal["disable", "require", "verify-full"]


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
    sslmode: SSLMode


@dataclass(frozen=True)
class SourceProvenance:
    owner: str
    environment: SourceEnvironment
    database_migration_ref: str


@dataclass(frozen=True)
class SourceProfile:
    source_id: str
    name: str
    description: str
    connection: ResolvedConnection
    allowed_schemas: tuple[str, ...]
    allowed_relation_kinds: tuple[AllowedRelationKind, ...]
    view_contract_version: int
    budget: BudgetProfile
    provenance: SourceProvenance

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_schemas", tuple(self.allowed_schemas))
        object.__setattr__(self, "allowed_relation_kinds", tuple(self.allowed_relation_kinds))
