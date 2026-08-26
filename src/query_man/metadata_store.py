from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from query_man.models import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogIndex,
    CatalogRelation,
    CatalogRelationKind,
    CatalogSnapshot,
    PreparedMetadata,
    SourceProfile,
)
from query_man.revision import create_metadata_revision


class MetadataStore(Protocol):
    async def get_active(self, source: SourceProfile) -> PreparedMetadata | None: ...

    async def get_revision(self, source: SourceProfile, revision: str) -> PreparedMetadata: ...

    async def publish(self, source: SourceProfile, value: PreparedMetadata) -> PreparedMetadata: ...

    async def activate(self, source: SourceProfile, revision: str) -> PreparedMetadata: ...

    async def unpin(self, source: SourceProfile) -> None: ...

    async def close(self) -> None: ...


class StoredMetadataNotFoundError(Exception):
    pass


class StoredMetadataInvalidError(Exception):
    pass


class StoredMetadataSupersededError(Exception):
    pass


class _StoredModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _StoredColumn(_StoredModel):
    name: str = Field(min_length=1, max_length=63)
    ordinal: int = Field(ge=1, le=1_600)
    data_type: str = Field(min_length=1, max_length=1_000)
    nullable: bool | Literal["unknown"]
    comment: str | None = Field(None, max_length=2_000)


class _StoredForeignKey(_StoredModel):
    columns: list[str] = Field(min_length=1, max_length=1_600)
    referenced_relation: str = Field(min_length=3, max_length=127)
    referenced_columns: list[str] = Field(min_length=1, max_length=1_600)


class _StoredIndex(_StoredModel):
    columns: list[str] = Field(min_length=1, max_length=1_600)
    unique: bool
    primary: bool


class _StoredRelation(_StoredModel):
    schema_name: str = Field(min_length=1, max_length=63)
    relation_name: str = Field(min_length=1, max_length=63)
    kind: CatalogRelationKind
    columns: list[_StoredColumn] = Field(min_length=1, max_length=1_600)
    comment: str | None = Field(None, max_length=2_000)
    definition_hash: str | None = Field(None, pattern=r"^[a-f0-9]{32}$")
    security_invoker: bool = False
    primary_key: list[str] = Field(default_factory=list, max_length=1_600)
    foreign_keys: list[_StoredForeignKey] = Field(default_factory=list, max_length=10_000)
    indexes: list[_StoredIndex] = Field(default_factory=list, max_length=10_000)


class _SnapshotDocument(_StoredModel):
    relations: list[_StoredRelation] = Field(min_length=1, max_length=10_000)


def encode_snapshot(snapshot: CatalogSnapshot) -> dict[str, object]:
    return {
        "relations": [
            {
                "schema_name": relation.schema,
                "relation_name": relation.name,
                "kind": relation.kind,
                "comment": relation.comment,
                "definition_hash": relation.definition_hash,
                **({"security_invoker": True} if relation.security_invoker else {}),
                **(
                    {"primary_key": list(relation.primary_key)}
                    if relation.primary_key
                    else {}
                ),
                **(
                    {
                        "foreign_keys": [
                            {
                                "columns": list(key.columns),
                                "referenced_relation": key.referenced_relation,
                                "referenced_columns": list(key.referenced_columns),
                            }
                            for key in relation.foreign_keys
                        ]
                    }
                    if relation.foreign_keys
                    else {}
                ),
                **(
                    {
                        "indexes": [
                            {
                                "columns": list(index.columns),
                                "unique": index.unique,
                                "primary": index.primary,
                            }
                            for index in relation.indexes
                        ]
                    }
                    if relation.indexes
                    else {}
                ),
                "columns": [
                    {
                        "name": column.name,
                        "ordinal": column.ordinal,
                        "data_type": column.data_type,
                        "nullable": column.nullable,
                        "comment": column.comment,
                    }
                    for column in sorted(relation.columns, key=lambda item: item.ordinal)
                ],
            }
            for relation in sorted(snapshot.relations, key=lambda item: item.qualified_name)
        ]
    }


def decode_snapshot(
    source: SourceProfile,
    revision: str,
    raw: object,
    *,
    freshness_age_ms: int | None = None,
) -> PreparedMetadata:
    try:
        document = _SnapshotDocument.model_validate(raw)
        snapshot = CatalogSnapshot(
            relations=tuple(
                CatalogRelation(
                    schema=relation.schema_name,
                    name=relation.relation_name,
                    qualified_name=f"{relation.schema_name}.{relation.relation_name}",
                    sql_name=(
                        f"{_quote_identifier(relation.schema_name)}."
                        f"{_quote_identifier(relation.relation_name)}"
                    ),
                    kind=relation.kind,
                    columns=tuple(
                        CatalogColumn(
                            name=column.name,
                            sql_name=_quote_identifier(column.name),
                            ordinal=column.ordinal,
                            data_type=column.data_type,
                            nullable=column.nullable,
                            comment=column.comment,
                        )
                        for column in relation.columns
                    ),
                    comment=relation.comment,
                    estimated_rows=None,
                    definition_hash=relation.definition_hash,
                    security_invoker=relation.security_invoker,
                    primary_key=tuple(relation.primary_key),
                    foreign_keys=tuple(
                        CatalogForeignKey(
                            columns=tuple(key.columns),
                            referenced_relation=key.referenced_relation,
                            referenced_columns=tuple(key.referenced_columns),
                        )
                        for key in relation.foreign_keys
                    ),
                    indexes=tuple(
                        CatalogIndex(
                            columns=tuple(index.columns),
                            unique=index.unique,
                            primary=index.primary,
                        )
                        for index in relation.indexes
                    ),
                )
                for relation in document.relations
            )
        )
    except (TypeError, ValueError) as error:
        raise StoredMetadataInvalidError("Stored metadata document is invalid") from error
    relations = {relation.qualified_name: relation for relation in snapshot.relations}
    for relation in snapshot.relations:
        columns = {column.name for column in relation.columns}
        if any(column not in columns for column in relation.primary_key):
            raise StoredMetadataInvalidError("Stored primary key columns are invalid")
        for key in relation.foreign_keys:
            referenced = relations.get(key.referenced_relation)
            referenced_columns = (
                set() if referenced is None else {column.name for column in referenced.columns}
            )
            if (
                len(key.columns) != len(key.referenced_columns)
                or any(column not in columns for column in key.columns)
                or any(column not in referenced_columns for column in key.referenced_columns)
            ):
                raise StoredMetadataInvalidError("Stored foreign key columns are invalid")
        if any(
            column not in columns
            for index in relation.indexes
            for column in index.columns
        ):
            raise StoredMetadataInvalidError("Stored index columns are invalid")
    if create_metadata_revision(source, snapshot) != revision:
        raise StoredMetadataInvalidError("Stored metadata is incompatible with the source contract")
    if freshness_age_ms is not None and freshness_age_ms < 0:
        raise StoredMetadataInvalidError("Stored metadata activation time is in the future")
    return PreparedMetadata(snapshot, revision, freshness_age_ms)


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'
