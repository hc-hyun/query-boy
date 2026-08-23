from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from query_man.metadata_store import encode_snapshot
from query_man.models import PreparedMetadata
from query_man.registry import (
    IDENTIFIER_PATTERN,
    POSTGRES_IDENTIFIER_MAX_LENGTH,
    QUALIFIED_RELATION_MAX_LENGTH,
    RELATION_NAME_PATTERN,
    STABLE_SLUG_MAX_LENGTH,
    STABLE_SLUG_PATTERN,
)
from query_man.secrets import EncryptedSecret
from query_man.verified import VerifiedQuery

POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807


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
    state_version: int = 1


@dataclass(frozen=True)
class SourceCatalogConnection:
    host: str
    port: int
    database: str
    user: str
    ssl: bool


@dataclass(frozen=True)
class SourceCatalogRecord:
    source_id: str
    generation: int
    enabled: bool
    state_version: int
    activated_at: datetime
    generation_created_at: datetime
    name: str
    description: str
    owner: str
    environment: str
    database_migration_ref: str
    budget_profile: str
    minimum_quality_level: str
    tenant_isolation: str
    connection: SourceCatalogConnection
    allowed_schemas: tuple[str, ...]
    allowed_relation_kinds: tuple[str, ...]
    semantic_default_relation: str | None
    semantic_relation_count: int
    semantic_join_count: int
    semantic_business_term_count: int
    semantic_question_rule_count: int
    semantic_composition_hint_count: int
    published_metadata_revision: str
    active_metadata_revision: str | None
    metadata_pinned: bool | None
    metadata_activated_at: datetime | None
    is_current: bool


@dataclass(frozen=True)
class SourceCatalogPage:
    items: tuple[SourceCatalogRecord, ...]
    next_after_source_id: str | None


@dataclass(frozen=True)
class SourceGenerationPage:
    current: SourceCatalogRecord
    items: tuple[SourceCatalogRecord, ...]
    next_before_generation: int | None


_CATALOG_PROJECTION = (
    "active.source_id, revision.generation, active.enabled, active.state_version, "
    "active.activated_at, revision.created_at AS generation_created_at, "
    "revision.manifest -> 'version' AS manifest_version, "
    "revision.manifest ->> 'name' AS name, "
    "revision.manifest ->> 'description' AS description, "
    "revision.manifest #>> '{provenance,owner}' AS owner, "
    "revision.manifest #>> '{provenance,environment}' AS environment, "
    "revision.manifest #>> '{provenance,database_migration_ref}' "
    "AS database_migration_ref, "
    "revision.manifest ->> 'budget_profile' AS budget_profile, "
    "revision.manifest ->> 'minimum_quality_level' AS minimum_quality_level, "
    "revision.manifest ->> 'tenant_isolation' AS tenant_isolation, "
    "revision.manifest #>> '{connection,host}' AS connection_host, "
    "revision.manifest #> '{connection,port}' AS connection_port, "
    "revision.manifest #>> '{connection,database}' AS connection_database, "
    "revision.manifest #>> '{connection,user}' AS connection_user, "
    "revision.manifest #> '{connection,ssl}' AS connection_ssl, "
    "revision.manifest -> 'allowed_schemas' AS allowed_schemas, "
    "revision.manifest -> 'allowed_relation_kinds' AS allowed_relation_kinds, "
    "revision.manifest #>> '{semantic_overlay,default_relation}' "
    "AS semantic_default_relation, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,relations}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,relations}') END AS semantic_relation_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,joins}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,joins}') END AS semantic_join_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,business_terms}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,business_terms}') END AS semantic_business_term_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,question_rules}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,question_rules}') END AS semantic_question_rule_count, "
    "CASE WHEN jsonb_typeof(revision.manifest #> "
    "'{semantic_overlay,composition_hints}') = 'array' "
    "THEN jsonb_array_length(revision.manifest #> "
    "'{semantic_overlay,composition_hints}') END AS semantic_composition_hint_count, "
    "revision.metadata_revision AS published_metadata_revision, "
    "metadata.revision AS active_metadata_revision, metadata.pinned AS metadata_pinned, "
    "metadata.activated_at AS metadata_activated_at, "
    "active.generation = revision.generation AS is_current "
)
_IDENTIFIER = re.compile(IDENTIFIER_PATTERN)
_RELATION_NAME = re.compile(RELATION_NAME_PATTERN)
_STABLE_SLUG = re.compile(STABLE_SLUG_PATTERN)


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
                "active.state_version, "
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
                "active.state_version, "
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
                "SELECT source_id, generation, true AS enabled, 0 AS state_version, "
                "manifest, secret_nonce, "
                "secret_ciphertext, metadata_revision "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s AND generation = %s",
                (source_id, generation),
            )
            row = await cursor.fetchone()
        if row is None:
            raise StoredSourceNotFoundError
        return _decode(row)

    async def list_catalog(
        self,
        *,
        after_source_id: str | None = None,
        limit: int = 50,
        enabled: bool | None = None,
        owner: str | None = None,
        environment: str | None = None,
        budget_profile: str | None = None,
    ) -> SourceCatalogPage:
        _validate_page_limit(limit)
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT "
                + _CATALOG_PROJECTION
                + "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = active.source_id "
                "WHERE (%s::text IS NULL OR active.source_id > %s) "
                "AND (%s::boolean IS NULL OR active.enabled = %s) "
                "AND (%s::text IS NULL OR "
                "revision.manifest #>> '{provenance,owner}' = %s) "
                "AND (%s::text IS NULL OR "
                "revision.manifest #>> '{provenance,environment}' = %s) "
                "AND (%s::text IS NULL OR revision.manifest ->> 'budget_profile' = %s) "
                "ORDER BY active.source_id ASC LIMIT %s",
                (
                    after_source_id,
                    after_source_id,
                    enabled,
                    enabled,
                    owner,
                    owner,
                    environment,
                    environment,
                    budget_profile,
                    budget_profile,
                    limit + 1,
                ),
            )
            rows = await cursor.fetchall()
        records = tuple(_decode_catalog(row) for row in rows[:limit])
        return SourceCatalogPage(
            items=records,
            next_after_source_id=(records[-1].source_id if len(rows) > limit else None),
        )

    async def get_catalog(self, source_id: str) -> SourceCatalogRecord | None:
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "SELECT "
                + _CATALOG_PROJECTION
                + "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = active.source_id "
                "WHERE active.source_id = %s",
                (source_id,),
            )
            row = await cursor.fetchone()
        return None if row is None else _decode_catalog(row)

    async def list_generation_history(
        self,
        source_id: str,
        *,
        before_generation: int | None = None,
        limit: int = 50,
    ) -> SourceGenerationPage | None:
        _validate_page_limit(limit)
        _validate_before_generation(before_generation)
        pool = await self._get_pool()
        async with pool.connection() as connection:
            cursor = await connection.execute(
                "WITH selected_generations AS MATERIALIZED ("
                "SELECT source_id, generation "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s "
                "AND (%s::bigint IS NULL OR generation < %s) "
                "ORDER BY generation DESC LIMIT %s"
                ") "
                "SELECT 0 AS catalog_row_order, "
                + _CATALOG_PROJECTION
                + "FROM control.active_source_profiles AS active "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = active.source_id "
                "AND revision.generation = active.generation "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = active.source_id "
                "WHERE active.source_id = %s "
                "UNION ALL "
                "SELECT 1 AS catalog_row_order, "
                + _CATALOG_PROJECTION
                + "FROM selected_generations AS selected "
                "JOIN control.source_profile_revisions AS revision "
                "ON revision.source_id = selected.source_id "
                "AND revision.generation = selected.generation "
                "JOIN control.active_source_profiles AS active "
                "ON active.source_id = revision.source_id "
                "LEFT JOIN control.active_metadata_revisions AS metadata "
                "ON metadata.source_id = revision.source_id "
                "ORDER BY catalog_row_order ASC, generation DESC",
                (
                    source_id,
                    before_generation,
                    before_generation,
                    limit + 1,
                    source_id,
                ),
            )
            rows = await cursor.fetchall()
        if not rows:
            return None
        current_rows = [row for row in rows if row.get("catalog_row_order") == 0]
        history_rows = [row for row in rows if row.get("catalog_row_order") == 1]
        if len(current_rows) != 1 or len(current_rows) + len(history_rows) != len(rows):
            raise ValueError("Stored source history shape is invalid")
        current = _decode_catalog(current_rows[0])
        if not current.is_current:
            raise ValueError("Stored source history current pointer is invalid")
        records = tuple(_decode_catalog(row) for row in history_rows[:limit])
        return SourceGenerationPage(
            current=current,
            items=records,
            next_before_generation=(
                records[-1].generation if len(history_rows) > limit else None
            ),
        )

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
        *,
        expected_state_version: int,
    ) -> StoredSource:
        if generation <= 0:
            raise SourceGenerationConflictError
        snapshot = encode_snapshot(metadata.snapshot)
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source_id)
            current_generation, current_state_version = await _lock_state(
                connection,
                source_id,
            )
            if (
                current_generation != expected_generation
                or current_state_version != expected_state_version
            ):
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
            cursor = await connection.execute(
                "INSERT INTO control.active_source_profiles "
                "(source_id, generation, enabled, state_version, activated_at) "
                "VALUES (%s, %s, true, 1, clock_timestamp()) "
                "ON CONFLICT (source_id) DO UPDATE "
                "SET generation = EXCLUDED.generation, enabled = true, "
                "state_version = control.active_source_profiles.state_version + 1, "
                "activated_at = EXCLUDED.activated_at "
                "RETURNING state_version",
                (source_id, generation),
            )
            state_row = await cursor.fetchone()
            if state_row is None:
                raise SourceGenerationConflictError
        return StoredSource(
            source_id,
            generation,
            manifest,
            encrypted_secret,
            metadata.revision,
            True,
            int(state_row["state_version"]),
        )

    async def deactivate(
        self,
        source_id: str,
        expected_generation: int,
        *,
        expected_state_version: int,
    ) -> int:
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source_id)
            cursor = await connection.execute(
                "UPDATE control.active_source_profiles "
                "SET enabled = false, state_version = state_version + 1, "
                "activated_at = clock_timestamp() "
                "WHERE source_id = %s AND generation = %s "
                "AND state_version = %s AND enabled "
                "RETURNING state_version",
                (source_id, expected_generation, expected_state_version),
            )
            row = await cursor.fetchone()
            if row is None:
                raise SourceGenerationConflictError
        return int(row["state_version"])

    async def rollback(
        self,
        source_id: str,
        generation: int,
        expected_generation: int,
        *,
        expected_state_version: int,
    ) -> StoredSource:
        pool = await self._get_pool()
        async with pool.connection() as connection, connection.transaction():
            await _lock_source_transition(connection, source_id)
            current_generation, current_state_version = await _lock_state(
                connection,
                source_id,
            )
            if (
                current_generation != expected_generation
                or current_state_version != expected_state_version
            ):
                raise SourceGenerationConflictError
            cursor = await connection.execute(
                "SELECT source_id, generation, true AS enabled, 0 AS state_version, "
                "manifest, secret_nonce, "
                "secret_ciphertext, metadata_revision "
                "FROM control.source_profile_revisions "
                "WHERE source_id = %s AND generation = %s FOR SHARE",
                (source_id, generation),
            )
            row = await cursor.fetchone()
            if row is None:
                raise StoredSourceNotFoundError
            value = _decode(row)
            cursor = await connection.execute(
                "UPDATE control.active_source_profiles "
                "SET generation = %s, enabled = true, state_version = state_version + 1, "
                "activated_at = clock_timestamp() "
                "WHERE source_id = %s AND state_version = %s "
                "RETURNING state_version",
                (generation, source_id, expected_state_version),
            )
            state_row = await cursor.fetchone()
            if state_row is None:
                raise SourceGenerationConflictError
            await connection.execute(
                "UPDATE control.active_metadata_revisions "
                "SET revision = %s, pinned = true, activated_at = clock_timestamp() "
                "WHERE source_id = %s",
                (value.metadata_revision, source_id),
            )
        return StoredSource(
            value.source_id,
            value.generation,
            value.manifest,
            value.encrypted_secret,
            value.metadata_revision,
            value.enabled,
            int(state_row["state_version"]),
        )

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


async def _lock_state(connection: Any, source_id: str) -> tuple[int, int]:
    cursor = await connection.execute(
        "SELECT generation, state_version FROM control.active_source_profiles "
        "WHERE source_id = %s FOR UPDATE",
        (source_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0, 0
    return int(row["generation"]), int(row["state_version"])


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
        state_version=int(row["state_version"]),
    )


def _decode_catalog(row: dict[str, Any]) -> SourceCatalogRecord:
    if _bounded_int(row, "manifest_version", 2, 2) != 2:
        raise ValueError("Stored source catalog manifest version is invalid")
    environment = _required_text(row, "environment", 11)
    if environment not in {"production", "staging", "development", "test"}:
        raise ValueError("Stored source catalog environment is invalid")
    minimum_quality_level = _required_text(row, "minimum_quality_level", 2)
    if minimum_quality_level not in {"L0", "L1", "L2"}:
        raise ValueError("Stored source catalog quality level is invalid")
    tenant_isolation = _required_text(row, "tenant_isolation", 4)
    if tenant_isolation not in {"none", "rls"}:
        raise ValueError("Stored source catalog tenant isolation is invalid")
    allowed_relation_kinds = _text_tuple(row, "allowed_relation_kinds", 1, 4, 30)
    if not set(allowed_relation_kinds) <= {
        "table",
        "partitioned_table",
        "view",
        "materialized_view",
    }:
        raise ValueError("Stored source catalog relation kinds are invalid")

    active_metadata_revision = _optional_revision(row, "active_metadata_revision")
    metadata_pinned = _optional_bool(row, "metadata_pinned")
    metadata_activated_at = _optional_timestamp(row, "metadata_activated_at")
    if active_metadata_revision is None:
        if metadata_pinned is not None or metadata_activated_at is not None:
            raise ValueError("Stored source catalog metadata state is invalid")
    elif metadata_pinned is None or metadata_activated_at is None:
        raise ValueError("Stored source catalog metadata state is invalid")

    return SourceCatalogRecord(
        source_id=_stable_slug(row, "source_id"),
        generation=_bounded_int(row, "generation", 1, POSTGRES_BIGINT_MAX),
        enabled=_required_bool(row, "enabled"),
        state_version=_bounded_int(row, "state_version", 1, POSTGRES_BIGINT_MAX),
        activated_at=_required_timestamp(row, "activated_at"),
        generation_created_at=_required_timestamp(row, "generation_created_at"),
        name=_required_text(row, "name", 200),
        description=_required_text(row, "description", 2_000),
        owner=_stable_slug(row, "owner"),
        environment=environment,
        database_migration_ref=_database_migration_ref(row),
        budget_profile=_identifier(row, "budget_profile"),
        minimum_quality_level=minimum_quality_level,
        tenant_isolation=tenant_isolation,
        connection=SourceCatalogConnection(
            host=_required_text(row, "connection_host", 253),
            port=_bounded_int(row, "connection_port", 1, 65_535),
            database=_identifier(row, "connection_database"),
            user=_identifier(row, "connection_user"),
            ssl=_required_bool(row, "connection_ssl"),
        ),
        allowed_schemas=_identifier_tuple(row, "allowed_schemas", 1, 20),
        allowed_relation_kinds=allowed_relation_kinds,
        semantic_default_relation=_optional_relation_name(
            row,
            "semantic_default_relation",
        ),
        semantic_relation_count=_bounded_int(row, "semantic_relation_count", 0, 200),
        semantic_join_count=_bounded_int(row, "semantic_join_count", 0, 500),
        semantic_business_term_count=_bounded_int(
            row,
            "semantic_business_term_count",
            0,
            200,
        ),
        semantic_question_rule_count=_bounded_int(
            row,
            "semantic_question_rule_count",
            0,
            200,
        ),
        semantic_composition_hint_count=_bounded_int(
            row,
            "semantic_composition_hint_count",
            0,
            200,
        ),
        published_metadata_revision=_required_revision(row, "published_metadata_revision"),
        active_metadata_revision=active_metadata_revision,
        metadata_pinned=metadata_pinned,
        metadata_activated_at=metadata_activated_at,
        is_current=_required_bool(row, "is_current"),
    )


def _validate_page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("Catalog page limit must be between 1 and 100")


def _validate_before_generation(before_generation: int | None) -> None:
    if before_generation is None:
        return
    if (
        isinstance(before_generation, bool)
        or not isinstance(before_generation, int)
        or not 1 <= before_generation <= POSTGRES_BIGINT_MAX
    ):
        raise ValueError("Generation cursor must be a positive PostgreSQL bigint")


def _required_text(row: dict[str, Any], key: str, maximum_length: int) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= maximum_length or "\x00" in value:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_text(row: dict[str, Any], key: str, maximum_length: int) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    return _required_text(row, key, maximum_length)


def _identifier(row: dict[str, Any], key: str) -> str:
    value = _required_text(row, key, POSTGRES_IDENTIFIER_MAX_LENGTH)
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_relation_name(row: dict[str, Any], key: str) -> str | None:
    value = _optional_text(row, key, QUALIFIED_RELATION_MAX_LENGTH)
    if value is not None and _RELATION_NAME.fullmatch(value) is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _stable_slug(row: dict[str, Any], key: str) -> str:
    value = _required_text(row, key, STABLE_SLUG_MAX_LENGTH)
    if _STABLE_SLUG.fullmatch(value) is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _database_migration_ref(row: dict[str, Any]) -> str:
    value = _required_text(row, "database_migration_ref", 255)
    if value != value.strip() or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("Stored source catalog database_migration_ref is invalid")
    return value


def _text_tuple(
    row: dict[str, Any],
    key: str,
    minimum_items: int,
    maximum_items: int,
    maximum_item_length: int,
) -> tuple[str, ...]:
    value = row.get(key)
    if not isinstance(value, list) or not minimum_items <= len(value) <= maximum_items:
        raise ValueError(f"Stored source catalog {key} is invalid")
    items = tuple(value)
    if any(
        not isinstance(item, str)
        or not 1 <= len(item) <= maximum_item_length
        or "\x00" in item
        for item in items
    ):
        raise ValueError(f"Stored source catalog {key} is invalid")
    if len(set(items)) != len(items):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return items


def _identifier_tuple(
    row: dict[str, Any],
    key: str,
    minimum_items: int,
    maximum_items: int,
) -> tuple[str, ...]:
    items = _text_tuple(
        row,
        key,
        minimum_items,
        maximum_items,
        POSTGRES_IDENTIFIER_MAX_LENGTH,
    )
    if any(_IDENTIFIER.fullmatch(item) is None for item in items):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return items


def _bounded_int(row: dict[str, Any], key: str, minimum: int, maximum: int) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _required_bool(row: dict[str, Any], key: str) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_bool(row: dict[str, Any], key: str) -> bool | None:
    if row.get(key) is None:
        return None
    return _required_bool(row, key)


def _required_timestamp(row: dict[str, Any], key: str) -> datetime:
    value = row.get(key)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_timestamp(row: dict[str, Any], key: str) -> datetime | None:
    if row.get(key) is None:
        return None
    return _required_timestamp(row, key)


def _required_revision(row: dict[str, Any], key: str) -> str:
    value = _required_text(row, key, 71)
    digest = value.removeprefix("sha256:")
    if len(value) != 71 or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"Stored source catalog {key} is invalid")
    return value


def _optional_revision(row: dict[str, Any], key: str) -> str | None:
    if row.get(key) is None:
        return None
    return _required_revision(row, key)
