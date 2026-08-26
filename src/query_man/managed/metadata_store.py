from __future__ import annotations

import asyncio
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from query_man.metadata.models import PreparedMetadata
from query_man.metadata.store import (
    StoredMetadataInvalidError,
    StoredMetadataNotFoundError,
    StoredMetadataSupersededError,
    decode_snapshot,
    encode_snapshot,
)
from query_man.source_catalog.models import SourceProfile


class PostgresMetadataStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: AsyncConnectionPool[Any] | None = None
        self._pool_lock = asyncio.Lock()

    async def get_active(self, source: SourceProfile) -> PreparedMetadata | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            row = await _read_active(connection, source.source_id)
        if row is None:
            return None
        return decode_snapshot(
            source,
            str(row["revision"]),
            row["snapshot"],
            freshness_age_ms=int(row["freshness_age_ms"]),
        )

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
        return decode_snapshot(source, revision, row["snapshot"])

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
                "WHERE NOT control.active_metadata_revisions.pinned "
                "OR control.active_metadata_revisions.revision = EXCLUDED.revision",
                (source.source_id, value.revision),
            )
            active = await _read_active(connection, source.source_id)
            if active is None:
                raise StoredMetadataInvalidError("Active metadata revision is unavailable")
        return decode_snapshot(
            source,
            str(active["revision"]),
            active["snapshot"],
            freshness_age_ms=int(active["freshness_age_ms"]),
        )

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
            decode_snapshot(source, revision, row["snapshot"])
            await connection.execute(
                "INSERT INTO control.active_metadata_revisions "
                "(source_id, revision, pinned, activated_at) "
                "VALUES (%s, %s, true, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET revision = EXCLUDED.revision, pinned = true, "
                "activated_at = EXCLUDED.activated_at",
                (source.source_id, revision),
            )
            active = await _read_active(connection, source.source_id)
            if active is None:
                raise StoredMetadataInvalidError("Active metadata revision is unavailable")
        return decode_snapshot(
            source,
            str(active["revision"]),
            active["snapshot"],
            freshness_age_ms=int(active["freshness_age_ms"]),
        )

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


async def _read_active(connection: Any, source_id: str) -> dict[str, Any] | None:
    cursor = await connection.execute(
        "SELECT active.revision, snapshot.snapshot, "
        "floor(extract(epoch FROM (clock_timestamp() - active.activated_at)) "
        "* 1000)::bigint AS freshness_age_ms "
        "FROM control.active_metadata_revisions AS active "
        "JOIN control.metadata_snapshots AS snapshot "
        "ON snapshot.source_id = active.source_id "
        "AND snapshot.revision = active.revision "
        "WHERE active.source_id = %s",
        (source_id,),
    )
    row: dict[str, Any] | None = await cursor.fetchone()
    return row


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
