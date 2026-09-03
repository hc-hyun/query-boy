from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CatalogRelationKind = Literal["view"]
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
class CatalogRelation:
    schema: str
    name: str
    qualified_name: str
    sql_name: str
    kind: CatalogRelationKind
    columns: tuple[CatalogColumn, ...]
    comment: str | None = None
    definition_hash: str | None = None
    view_contract_source: str | None = None
    view_contract_version: int | None = None
    security_invoker: bool = False
    security_barrier: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))


@dataclass(frozen=True)
class CatalogSnapshot:
    relations: tuple[CatalogRelation, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "relations", tuple(self.relations))


@dataclass(frozen=True)
class PreparedMetadata:
    snapshot: CatalogSnapshot
    revision: str
