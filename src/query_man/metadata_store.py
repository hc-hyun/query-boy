from __future__ import annotations

import asyncio
from typing import Any, Literal, Protocol

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
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


class PostgresMetadataStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: AsyncConnectionPool[Any] | None = None
        self._pool_lock = asyncio.Lock()

    async def get_active(self, source: SourceProfile) -> PreparedMetadata | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT active.revision, snapshot.snapshot "
                "FROM control.active_metadata_revisions AS active "
                "JOIN control.metadata_snapshots AS snapshot "
                "ON snapshot.source_id = active.source_id "
                "AND snapshot.revision = active.revision "
                "WHERE active.source_id = %s",
                (source.source_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return _decode(source, str(row["revision"]), row["snapshot"])

    async def get_revision(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT snapshot FROM control.metadata_snapshots "
                "WHERE source_id = %s AND revision = %s",
                (source.source_id, revision),
            )
            row = await cursor.fetchone()
        if row is None:
            raise StoredMetadataNotFoundError("Stored metadata revision was not found")
        return _decode(source, revision, row["snapshot"])

    async def publish(self, source: SourceProfile, value: PreparedMetadata) -> PreparedMetadata:
        document = encode_snapshot(value.snapshot)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source.source_id)
            await _require_current_source(connection, source)
            await connection.execute(
                "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (source.source_id, value.revision, Jsonb(document)),
            )
            cursor = await connection.execute(
                "SELECT snapshot FROM control.metadata_snapshots "
                "WHERE source_id = %s AND revision = %s",
                (source.source_id, value.revision),
            )
            stored = await cursor.fetchone()
            if stored is None or stored["snapshot"] != document:
                raise StoredMetadataInvalidError("Stored metadata revision payload does not match")
            await connection.execute(
                "INSERT INTO control.active_metadata_revisions "
                "(source_id, revision, pinned, activated_at) "
                "VALUES (%s, %s, false, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET revision = EXCLUDED.revision, activated_at = EXCLUDED.activated_at "
                "WHERE NOT control.active_metadata_revisions.pinned",
                (source.source_id, value.revision),
            )
            cursor = await connection.execute(
                "SELECT active.revision, snapshot.snapshot "
                "FROM control.active_metadata_revisions AS active "
                "JOIN control.metadata_snapshots AS snapshot "
                "ON snapshot.source_id = active.source_id "
                "AND snapshot.revision = active.revision "
                "WHERE active.source_id = %s",
                (source.source_id,),
            )
            active = await cursor.fetchone()
            if active is None:
                raise StoredMetadataInvalidError("Active metadata revision is unavailable")
        return _decode(source, str(active["revision"]), active["snapshot"])

    async def activate(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source.source_id)
            await _require_current_source(connection, source)
            cursor = await connection.execute(
                "SELECT snapshot FROM control.metadata_snapshots "
                "WHERE source_id = %s AND revision = %s FOR SHARE",
                (source.source_id, revision),
            )
            row = await cursor.fetchone()
            if row is None:
                raise StoredMetadataNotFoundError("Stored metadata revision was not found")
            value = _decode(source, revision, row["snapshot"])
            await connection.execute(
                "INSERT INTO control.active_metadata_revisions "
                "(source_id, revision, pinned, activated_at) "
                "VALUES (%s, %s, true, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET revision = EXCLUDED.revision, pinned = true, "
                "activated_at = EXCLUDED.activated_at",
                (source.source_id, revision),
            )
        return value

    async def unpin(self, source: SourceProfile) -> None:
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source.source_id)
            await _require_current_source(connection, source)
            cursor = await connection.execute(
                "UPDATE control.active_metadata_revisions SET pinned = false "
                "WHERE source_id = %s RETURNING source_id",
                (source.source_id,),
            )
            if await cursor.fetchone() is None:
                raise StoredMetadataNotFoundError("Active metadata revision is unavailable")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def _get_pool(self) -> AsyncConnectionPool[Any]:
        if self._pool is not None:
            return self._pool
        async with self._pool_lock:
            if self._pool is None:
                pool = AsyncConnectionPool(
                    conninfo=self._dsn,
                    kwargs={
                        "application_name": "query-man-control",
                        "connect_timeout": 2,
                        "row_factory": dict_row,
                    },
                    min_size=0,
                    max_size=2,
                    timeout=2,
                    max_idle=10,
                    open=False,
                )
                await pool.open()
                self._pool = pool
        return self._pool


async def _lock_source_transition(connection: Any, source_id: str) -> None:
    await connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock("
        "pg_catalog.hashtextextended(%s, 0))",
        (source_id,),
    )


async def _require_current_source(connection: Any, source: SourceProfile) -> None:
    if source.control_generation is None:
        if source.control_state_version is not None:
            raise StoredMetadataSupersededError
        cursor = await connection.execute(
            "SELECT 1 FROM control.active_source_profiles WHERE source_id = %s",
            (source.source_id,),
        )
        if await cursor.fetchone() is not None:
            raise StoredMetadataSupersededError
        return
    if source.control_state_version is None:
        raise StoredMetadataSupersededError
    cursor = await connection.execute(
        "SELECT 1 FROM control.active_source_profiles "
        "WHERE source_id = %s AND generation = %s AND state_version = %s AND enabled",
        (
            source.source_id,
            source.control_generation,
            source.control_state_version,
        ),
    )
    if await cursor.fetchone() is None:
        raise StoredMetadataSupersededError


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
                **({"primary_key": relation.primary_key} if relation.primary_key else {}),
                **(
                    {
                        "foreign_keys": [
                            {
                                "columns": key.columns,
                                "referenced_relation": key.referenced_relation,
                                "referenced_columns": key.referenced_columns,
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
                                "columns": index.columns,
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


def _decode(source: SourceProfile, revision: str, raw: object) -> PreparedMetadata:
    try:
        document = _SnapshotDocument.model_validate(raw)
        snapshot = CatalogSnapshot(
            relations=[
                CatalogRelation(
                    schema=relation.schema_name,
                    name=relation.relation_name,
                    qualified_name=f"{relation.schema_name}.{relation.relation_name}",
                    sql_name=(
                        f"{_quote_identifier(relation.schema_name)}."
                        f"{_quote_identifier(relation.relation_name)}"
                    ),
                    kind=relation.kind,
                    columns=[
                        CatalogColumn(
                            name=column.name,
                            sql_name=_quote_identifier(column.name),
                            ordinal=column.ordinal,
                            data_type=column.data_type,
                            nullable=column.nullable,
                            comment=column.comment,
                        )
                        for column in relation.columns
                    ],
                    comment=relation.comment,
                    estimated_rows=None,
                    definition_hash=relation.definition_hash,
                    security_invoker=relation.security_invoker,
                    primary_key=relation.primary_key,
                    foreign_keys=[
                        CatalogForeignKey(
                            columns=key.columns,
                            referenced_relation=key.referenced_relation,
                            referenced_columns=key.referenced_columns,
                        )
                        for key in relation.foreign_keys
                    ],
                    indexes=[
                        CatalogIndex(
                            columns=index.columns,
                            unique=index.unique,
                            primary=index.primary,
                        )
                        for index in relation.indexes
                    ],
                )
                for relation in document.relations
            ]
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
    return PreparedMetadata(snapshot, revision)


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'
