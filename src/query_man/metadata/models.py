from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from query_man.source_catalog.models import SourceProfile

CatalogRelationKind = Literal["table", "partitioned_table", "view", "materialized_view", "foreign_table"]
Nullability = bool | Literal["unknown"]


@dataclass(frozen=True)
class CatalogColumn:
    name: str
    sql_name: str
    ordinal: int
    data_type: str
    nullable: Nullability
    comment: str | None = None


@dataclass(frozen=True)
class CatalogForeignKey:
    columns: tuple[str, ...]
    referenced_relation: str
    referenced_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "referenced_columns", tuple(self.referenced_columns))


@dataclass(frozen=True)
class CatalogIndex:
    columns: tuple[str, ...]
    unique: bool
    primary: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))


@dataclass(frozen=True)
class CatalogRelation:
    schema: str
    name: str
    qualified_name: str
    sql_name: str
    kind: CatalogRelationKind
    columns: tuple[CatalogColumn, ...]
    comment: str | None = None
    estimated_rows: int | None = None
    definition_hash: str | None = None
    view_contract_source: str | None = None
    view_contract_version: int | None = None
    primary_key: tuple[str, ...] = field(default_factory=tuple)
    foreign_keys: tuple[CatalogForeignKey, ...] = field(default_factory=tuple)
    indexes: tuple[CatalogIndex, ...] = field(default_factory=tuple)
    security_invoker: bool = False
    security_barrier: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "primary_key", tuple(self.primary_key))
        object.__setattr__(self, "foreign_keys", tuple(self.foreign_keys))
        object.__setattr__(self, "indexes", tuple(self.indexes))


@dataclass(frozen=True)
class CatalogSnapshot:
    relations: tuple[CatalogRelation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "relations", tuple(self.relations))


@dataclass(frozen=True)
class PreparedMetadata:
    snapshot: CatalogSnapshot
    revision: str


class CatalogProvider(Protocol):
    async def load(self, source: SourceProfile) -> CatalogSnapshot: ...

    async def close(self) -> None: ...
