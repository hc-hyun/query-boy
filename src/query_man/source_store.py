from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from query_man.metadata_store import encode_snapshot
from query_man.models import PreparedMetadata
from query_man.secrets import EncryptedSecret
from query_man.verified import VerifiedQuery


class SourceGenerationConflictError(Exception):
    pass


class StoredSourceNotFoundError(Exception):
    pass


class SourcePublishPinnedError(Exception):
    pass


@dataclass(frozen=True)
class StoredSource:
    source_id: str
    generation: int
    manifest: dict[str, object]
    encrypted_secret: EncryptedSecret
    metadata_revision: str
    enabled: bool


class PostgresSourceStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: AsyncConnectionPool[Any] | None = None
        self._pool_lock = asyncio.Lock()

    async def list_active(self) -> list[StoredSource]:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT active.source_id, active.generation, active.enabled, "
                "revision.manifest, revision.secret_nonce, revision.secret_ciphertext, "
                "revision.metadata_revision "
                "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "ORDER BY active.source_id"
            )
            rows = await cursor.fetchall()
        return [_decode(row) for row in rows]

    async def get_active(self, source_id: str) -> StoredSource | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT active.source_id, active.generation, active.enabled, "
                "revision.manifest, revision.secret_nonce, revision.secret_ciphertext, "
                "revision.metadata_revision "
                "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "WHERE active.source_id = %s",
                (source_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _decode(row)

    async def get_revision(self, source_id: str, generation: int) -> StoredSource:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT source_id, generation, true AS enabled, manifest, secret_nonce, "
                "secret_ciphertext, metadata_revision "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s AND generation = %s",
                (source_id, generation),
            )
            row = await cursor.fetchone()
        if row is None:
            raise StoredSourceNotFoundError
        return _decode(row)

    async def next_generation(self, source_id: str) -> int:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT coalesce(max(generation), 0) + 1 AS generation "
                "FROM control.source_profile_revisions WHERE source_id = %s",
                (source_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            raise StoredSourceNotFoundError
        return int(row["generation"])

    async def publish(
        self,
        source_id: str,
        expected_generation: int,
        generation: int,
        manifest: dict[str, object],
        encrypted_secret: EncryptedSecret,
        metadata: PreparedMetadata,
    ) -> StoredSource:
        if generation <= 0:
            raise SourceGenerationConflictError
        snapshot = encode_snapshot(metadata.snapshot)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source_id)
            current_generation = await _lock_generation(connection, source_id)
            if current_generation != expected_generation:
                raise SourceGenerationConflictError
            await connection.execute(
                "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (source_id, metadata.revision, Jsonb(snapshot)),
            )
            cursor = await connection.execute(
                "SELECT snapshot FROM control.metadata_snapshots "
                "WHERE source_id = %s AND revision = %s",
                (source_id, metadata.revision),
            )
            stored_snapshot = await cursor.fetchone()
            if stored_snapshot is None or stored_snapshot["snapshot"] != snapshot:
                raise SourceGenerationConflictError
            try:
                await connection.execute(
                    "INSERT INTO control.source_profile_revisions "
                    "(source_id, generation, manifest, secret_nonce, secret_ciphertext, "
                    "metadata_revision) VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        source_id,
                        generation,
                        Jsonb(manifest),
                        encrypted_secret.nonce,
                        encrypted_secret.ciphertext,
                        metadata.revision,
                    ),
                )
            except errors.UniqueViolation as error:
                raise SourceGenerationConflictError from error
            cursor = await connection.execute(
                "INSERT INTO control.active_metadata_revisions "
                "(source_id, revision, pinned, activated_at) "
                "VALUES (%s, %s, false, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET revision = EXCLUDED.revision, activated_at = EXCLUDED.activated_at "
                "WHERE NOT control.active_metadata_revisions.pinned "
                "RETURNING revision",
                (source_id, metadata.revision),
            )
            if await cursor.fetchone() is None:
                raise SourcePublishPinnedError
            await connection.execute(
                "INSERT INTO control.active_source_profiles "
                "(source_id, generation, enabled, activated_at) "
                "VALUES (%s, %s, true, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET generation = EXCLUDED.generation, enabled = true, "
                "activated_at = EXCLUDED.activated_at",
                (source_id, generation),
            )
        return StoredSource(
            source_id,
            generation,
            manifest,
            encrypted_secret,
            metadata.revision,
            True,
        )

    async def deactivate(self, source_id: str, expected_generation: int) -> None:
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source_id)
            cursor = await connection.execute(
                "UPDATE control.active_source_profiles "
                "SET enabled = false, activated_at = clock_timestamp() "
                "WHERE source_id = %s AND generation = %s AND enabled "
                "RETURNING source_id",
                (source_id, expected_generation),
            )
            if await cursor.fetchone() is None:
                raise SourceGenerationConflictError

    async def rollback(
        self,
        source_id: str,
        generation: int,
        expected_generation: int,
    ) -> StoredSource:
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source_id)
            current_generation = await _lock_generation(connection, source_id)
            if current_generation != expected_generation:
                raise SourceGenerationConflictError
            cursor = await connection.execute(
                "SELECT source_id, generation, true AS enabled, manifest, secret_nonce, "
                "secret_ciphertext, metadata_revision "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s AND generation = %s FOR SHARE",
                (source_id, generation),
            )
            row = await cursor.fetchone()
            if row is None:
                raise StoredSourceNotFoundError
            value = _decode(row)
            await connection.execute(
                "UPDATE control.active_source_profiles "
                "SET generation = %s, enabled = true, activated_at = clock_timestamp() "
                "WHERE source_id = %s",
                (generation, source_id),
            )
            await connection.execute(
                "UPDATE control.active_metadata_revisions "
                "SET revision = %s, pinned = true, activated_at = clock_timestamp() "
                "WHERE source_id = %s",
                (value.metadata_revision, source_id),
            )
        return value

    async def publish_verified_query(self, query: VerifiedQuery) -> None:
        document = {
            "columns": list(query.expected.columns),
            "row_count": query.expected.row_count,
            "result_hash": query.expected.result_hash,
        }
        relations = list(query.relations)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO control.verified_query_contracts "
                "(source_id, query_id, metadata_revision, question, relations, sql, expected) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    query.source_id,
                    query.query_id,
                    query.metadata_revision,
                    query.question,
                    Jsonb(relations),
                    query.sql,
                    Jsonb(document),
                ),
            )
            cursor = await connection.execute(
                "SELECT question, relations, sql, expected "
                "FROM control.verified_query_contracts "
                "WHERE source_id = %s AND query_id = %s AND metadata_revision = %s",
                (query.source_id, query.query_id, query.metadata_revision),
            )
            stored = await cursor.fetchone()
            if (
                stored is None
                or stored["question"] != query.question
                or stored["relations"] != relations
                or stored["sql"] != query.sql
                or stored["expected"] != document
            ):
                raise SourceGenerationConflictError

    async def verified_revision_map(self) -> dict[str, frozenset[str]]:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT source_id, metadata_revision "
                "FROM control.verified_query_contracts "
                "GROUP BY source_id, metadata_revision"
            )
            rows = await cursor.fetchall()
        revisions: dict[str, set[str]] = {}
        for row in rows:
            revisions.setdefault(str(row["source_id"]), set()).add(
                str(row["metadata_revision"])
            )
        return {
            source_id: frozenset(source_revisions)
            for source_id, source_revisions in revisions.items()
        }

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
                        "application_name": "query-man-source-control",
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


async def _lock_generation(connection: Any, source_id: str) -> int:
    cursor = await connection.execute(
        "SELECT generation FROM control.active_source_profiles "
        "WHERE source_id = %s FOR UPDATE",
        (source_id,),
    )
    row = await cursor.fetchone()
    return 0 if row is None else int(row["generation"])


async def _lock_source_transition(connection: Any, source_id: str) -> None:
    await connection.execute(
        "SELECT pg_catalog.pg_advisory_xact_lock("
        "pg_catalog.hashtextextended(%s, 0))",
        (source_id,),
    )


def _decode(row: dict[str, Any]) -> StoredSource:
    manifest = row["manifest"]
    if not isinstance(manifest, dict):
        raise ValueError("Stored source manifest is invalid")
    return StoredSource(
        source_id=str(row["source_id"]),
        generation=int(row["generation"]),
        manifest=manifest,
        encrypted_secret=EncryptedSecret(
            bytes(row["secret_nonce"]),
            bytes(row["secret_ciphertext"]),
        ),
        metadata_revision=str(row["metadata_revision"]),
        enabled=bool(row["enabled"]),
    )
