from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from query_man.models import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogIndex,
    CatalogRelation,
    CatalogRelationKind,
    CatalogSnapshot,
    ResourceObservation,
    SourceProfile,
)
from query_man.reader_policy import (
    READER_CLIENT_ENCODING,
    READER_SESSION_BUDGET_SETTERS,
    READER_SESSION_TIMEZONE_SETTER,
    ReaderSessionPolicyError,
    reader_session_budget_values,
    require_reader_connection_policy,
    require_reader_session_policy,
)


class _CatalogValidationError(ValueError):
    pass


_CATALOG_SESSION_SETTINGS = (
    "SELECT pg_catalog.set_config('statement_timeout', %s, true), "
    "pg_catalog.set_config('lock_timeout', %s, true), "
    "pg_catalog.set_config('search_path', 'pg_catalog', true), "
    "pg_catalog.set_config('row_security', 'on', true), "
    "pg_catalog.set_config('query_man.tenant_id', '', true), "
    + READER_SESSION_BUDGET_SETTERS
)

CATALOG_QUERY = """
  WITH eligible_relations AS MATERIALIZED (
    SELECT
      relation.oid AS relation_oid,
      namespace.nspname::text AS schema_name,
      relation.relname::text AS relation_name,
      relation.relkind AS relation_kind,
      pg_catalog.left(pg_catalog.obj_description(relation.oid, 'pg_class'), 2000)
        AS relation_comment,
      CASE WHEN relation.relkind IN ('v', 'm')
        THEN pg_catalog.md5(pg_catalog.pg_get_viewdef(relation.oid, false))
        ELSE NULL END AS view_definition_hash,
      coalesce(relation.reloptions @> ARRAY['security_invoker=true'], false)
        AS security_invoker,
      relation.reltuples
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname::text = ANY(%s::text[])
      AND relation.relkind::text = ANY(%s::text[])
      AND pg_catalog.has_schema_privilege(current_user, namespace.oid, 'USAGE')
      AND pg_catalog.has_table_privilege(current_user, relation.oid, 'SELECT')
  )
  SELECT
    relation.schema_name,
    relation.relation_name,
    relation.relation_kind,
    relation.relation_comment,
    relation.view_definition_hash,
    relation.security_invoker,
    CASE WHEN relation.relation_kind IN ('r', 'p', 'm', 'f') AND relation.reltuples >= 0
      THEN relation.reltuples::double precision ELSE NULL END AS estimated_rows,
    attribute.attnum::integer AS ordinal,
    attribute.attname::text AS column_name,
    pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
    type_row.typtype::text AS type_kind,
    attribute.attnotnull AS is_not_null,
    pg_catalog.left(pg_catalog.col_description(relation.relation_oid, attribute.attnum), 2000)
      AS column_comment
  FROM eligible_relations AS relation
  JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid = relation.relation_oid
  JOIN pg_catalog.pg_type AS type_row ON type_row.oid = attribute.atttypid
  WHERE attribute.attnum > 0
    AND NOT attribute.attisdropped
    AND pg_catalog.has_column_privilege(
      current_user, relation.relation_oid, attribute.attnum, 'SELECT'
    )
  ORDER BY relation.schema_name, relation.relation_name, attribute.attnum
  LIMIT %s
"""

STRUCTURE_QUERY = """
  WITH eligible_relations AS MATERIALIZED (
    SELECT
      relation.oid AS relation_oid,
      namespace.nspname::text AS schema_name,
      relation.relname::text AS relation_name
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname::text = ANY(%s::text[])
      AND relation.relkind::text = ANY(%s::text[])
      AND pg_catalog.has_schema_privilege(current_user, namespace.oid, 'USAGE')
      AND pg_catalog.has_table_privilege(current_user, relation.oid, 'SELECT')
  ),
  primary_keys AS (
    SELECT
      local_relation.schema_name,
      local_relation.relation_name,
      key_columns.column_names
    FROM pg_catalog.pg_constraint AS constraint_row
    JOIN eligible_relations AS local_relation
      ON local_relation.relation_oid = constraint_row.conrelid
    JOIN LATERAL (
      SELECT
        pg_catalog.array_agg(attribute.attname::text ORDER BY key.position) AS column_names,
        pg_catalog.bool_and(
          pg_catalog.has_column_privilege(
            current_user, local_relation.relation_oid, attribute.attnum, 'SELECT'
          )
        ) AS columns_visible,
        pg_catalog.count(*) = pg_catalog.cardinality(constraint_row.conkey) AS complete
      FROM pg_catalog.unnest(constraint_row.conkey) WITH ORDINALITY AS key(attnum, position)
      JOIN pg_catalog.pg_attribute AS attribute
        ON attribute.attrelid = local_relation.relation_oid
        AND attribute.attnum = key.attnum
    ) AS key_columns ON key_columns.columns_visible AND key_columns.complete
    WHERE constraint_row.contype = 'p'
  ),
  foreign_keys AS (
    SELECT
      local_relation.schema_name,
      local_relation.relation_name,
      key_columns.local_columns AS column_names,
      referenced_relation.schema_name || '.' || referenced_relation.relation_name
        AS referenced_relation,
      key_columns.referenced_columns
    FROM pg_catalog.pg_constraint AS constraint_row
    JOIN eligible_relations AS local_relation
      ON local_relation.relation_oid = constraint_row.conrelid
    JOIN eligible_relations AS referenced_relation
      ON referenced_relation.relation_oid = constraint_row.confrelid
    JOIN LATERAL (
      SELECT
        pg_catalog.array_agg(local_attribute.attname::text ORDER BY local_key.position)
          AS local_columns,
        pg_catalog.array_agg(
          referenced_attribute.attname::text ORDER BY local_key.position
        )
          AS referenced_columns,
        pg_catalog.bool_and(
          pg_catalog.has_column_privilege(
            current_user, local_relation.relation_oid, local_attribute.attnum, 'SELECT'
          )
          AND pg_catalog.has_column_privilege(
            current_user,
            referenced_relation.relation_oid,
            referenced_attribute.attnum,
            'SELECT'
          )
        ) AS columns_visible,
        pg_catalog.count(*) = pg_catalog.cardinality(constraint_row.conkey) AS complete
      FROM pg_catalog.unnest(constraint_row.conkey)
        WITH ORDINALITY AS local_key(attnum, position)
      JOIN pg_catalog.unnest(constraint_row.confkey)
        WITH ORDINALITY AS referenced_key(attnum, position)
        ON referenced_key.position = local_key.position
      JOIN pg_catalog.pg_attribute AS local_attribute
        ON local_attribute.attrelid = local_relation.relation_oid
        AND local_attribute.attnum = local_key.attnum
      JOIN pg_catalog.pg_attribute AS referenced_attribute
        ON referenced_attribute.attrelid = referenced_relation.relation_oid
        AND referenced_attribute.attnum = referenced_key.attnum
    ) AS key_columns ON key_columns.columns_visible AND key_columns.complete
    WHERE constraint_row.contype = 'f'
  ),
  simple_indexes AS (
    SELECT
      local_relation.schema_name,
      local_relation.relation_name,
      key_columns.column_names,
      index_row.indisunique AS is_unique,
      index_row.indisprimary AS is_primary
    FROM pg_catalog.pg_index AS index_row
    JOIN eligible_relations AS local_relation
      ON local_relation.relation_oid = index_row.indrelid
    JOIN LATERAL (
      SELECT
        pg_catalog.array_agg(attribute.attname::text ORDER BY key.position) AS column_names,
        pg_catalog.bool_and(
          pg_catalog.has_column_privilege(
            current_user, local_relation.relation_oid, attribute.attnum, 'SELECT'
          )
        ) AS columns_visible,
        pg_catalog.count(*) = index_row.indnkeyatts AS complete
      FROM pg_catalog.unnest(index_row.indkey::smallint[])
        WITH ORDINALITY AS key(attnum, position)
      JOIN pg_catalog.pg_attribute AS attribute
        ON attribute.attrelid = local_relation.relation_oid
        AND attribute.attnum = key.attnum
      WHERE key.position <= index_row.indnkeyatts
    ) AS key_columns ON key_columns.columns_visible AND key_columns.complete
    WHERE index_row.indisvalid
      AND index_row.indisready
      AND index_row.indpred IS NULL
  )
  SELECT
    'primary_key'::text AS structure_kind,
    schema_name,
    relation_name,
    column_names,
    NULL::text AS referenced_relation,
    NULL::text[] AS referenced_columns,
    NULL::boolean AS is_unique,
    true AS is_primary
  FROM primary_keys
  UNION ALL
  SELECT
    'foreign_key',
    schema_name,
    relation_name,
    column_names,
    referenced_relation,
    referenced_columns,
    NULL::boolean,
    false
  FROM foreign_keys
  UNION ALL
  SELECT
    'index',
    schema_name,
    relation_name,
    column_names,
    NULL::text,
    NULL::text[],
    is_unique,
    is_primary
  FROM simple_indexes
  ORDER BY schema_name, relation_name, structure_kind, column_names
  LIMIT %s
"""

RESOURCE_OBSERVATION_QUERY = """
  WITH requested_relations AS MATERIALIZED (
    SELECT
      schema_target.schema_name,
      relation_target.relation_name,
      schema_target.position
    FROM pg_catalog.unnest(%s::text[])
      WITH ORDINALITY AS schema_target(schema_name, position)
    JOIN pg_catalog.unnest(%s::text[])
      WITH ORDINALITY AS relation_target(relation_name, position)
      USING (position)
  ),
  resolved_relations AS MATERIALIZED (
    SELECT
      requested.position,
      relation.oid AS relation_oid,
      relation.reltuples
    FROM requested_relations AS requested
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.nspname::text = requested.schema_name
    JOIN pg_catalog.pg_class AS relation
      ON relation.relnamespace = namespace.oid
      AND relation.relname::text = requested.relation_name
    WHERE relation.relkind IN ('r', 'm')
  )
  SELECT
    pg_catalog.max(
      CASE
        WHEN resolved.position = %s AND resolved.reltuples >= 0
          THEN resolved.reltuples::double precision
        ELSE NULL
      END
    ) AS representative_records,
    pg_catalog.sum(pg_catalog.pg_table_size(resolved.relation_oid)) AS table_bytes,
    pg_catalog.sum(pg_catalog.pg_indexes_size(resolved.relation_oid)) AS index_bytes,
    pg_catalog.sum(pg_catalog.pg_total_relation_size(resolved.relation_oid))
      AS total_storage_bytes
  FROM resolved_relations AS resolved
  HAVING NOT EXISTS (
    SELECT 1
    FROM requested_relations AS requested
    WHERE NOT EXISTS (
      SELECT 1
      FROM resolved_relations AS matched
      WHERE matched.position = requested.position
    )
  )
"""

_POSTGRES_KINDS = {
    "table": "r",
    "partitioned_table": "p",
    "view": "v",
    "materialized_view": "m",
}
_CATALOG_KINDS = {
    "r": "table",
    "p": "partitioned_table",
    "v": "view",
    "m": "materialized_view",
    "f": "foreign_table",
}


async def _begin_catalog_transaction(
    connection: AsyncConnection[Any],
    source: SourceProfile,
) -> None:
    await connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        await connection.execute(READER_SESSION_TIMEZONE_SETTER)
        await connection.execute(
            _CATALOG_SESSION_SETTINGS,
            (
                f"{source.budget.metadata_statement_timeout_ms}ms",
                f"{source.budget.lock_timeout_ms}ms",
                *reader_session_budget_values(source),
            ),
        )
    except Exception as error:
        raise ReaderSessionPolicyError(
            "Source reader session budget could not be applied"
        ) from error
    await require_reader_session_policy(connection, source)


async def _require_catalog_connection_policy(
    connection: AsyncConnection[Any],
) -> None:
    try:
        require_reader_connection_policy(connection)
    except ReaderSessionPolicyError:
        try:
            await connection.close()
        except Exception:
            pass
        raise


class PostgresCatalog:
    def __init__(self, *, reject_domain_columns: bool = False) -> None:
        self._pools: dict[str, AsyncConnectionPool[Any]] = {}
        self._pool_lock = asyncio.Lock()
        self._reject_domain_columns = reject_domain_columns

    async def load(self, source: SourceProfile) -> CatalogSnapshot:
        pool = await self._get_pool(source)
        async with pool.connection() as connection:
            await _require_catalog_connection_policy(connection)
            try:
                await _begin_catalog_transaction(connection, source)
                cursor = await connection.execute(
                    CATALOG_QUERY,
                    (
                        list(source.allowed_schemas),
                        [_POSTGRES_KINDS[kind] for kind in source.allowed_relation_kinds],
                        source.budget.max_metadata_columns + 1,
                    ),
                )
                rows = await cursor.fetchall()
                if len(rows) > source.budget.max_metadata_columns:
                    raise _CatalogValidationError("Catalog column limit exceeded")
                if self._reject_domain_columns:
                    _require_supported_catalog_types(rows)
                structure_cursor = await connection.execute(
                    STRUCTURE_QUERY,
                    (
                        list(source.allowed_schemas),
                        [_POSTGRES_KINDS[kind] for kind in source.allowed_relation_kinds],
                        source.budget.max_metadata_columns + 1,
                    ),
                )
                structures = await structure_cursor.fetchall()
                if len(structures) > source.budget.max_metadata_columns:
                    raise _CatalogValidationError("Catalog structure limit exceeded")
                await connection.execute("COMMIT")
            except Exception as error:
                try:
                    await connection.rollback()
                except Exception as rollback_error:
                    raise error from rollback_error
                raise

        relations = _rows_to_relations(rows)
        if len(relations) > source.budget.max_metadata_relations:
            raise _CatalogValidationError("Catalog relation limit exceeded")
        if any(len(relation.columns) > source.budget.max_columns_per_relation for relation in relations):
            raise _CatalogValidationError("Catalog per-relation column limit exceeded")
        frozen_relations = tuple(relation.freeze() for relation in relations)
        return CatalogSnapshot(
            relations=_apply_structures(frozen_relations, structures)
        )

    async def observe_resources(self, source: SourceProfile) -> ResourceObservation:
        definition = source.observability
        if definition is None:
            raise RuntimeError("Resource observation is not configured")

        storage_relations = definition.storage_relations
        representative_relation = definition.representative_records.physical_relation
        if (
            not 1 <= len(storage_relations) <= 16
            or len(set(storage_relations)) != len(storage_relations)
            or representative_relation not in storage_relations
        ):
            raise RuntimeError("Resource observation definition is invalid")

        relation_parts: list[tuple[str, str]] = []
        for qualified_name in storage_relations:
            parts = qualified_name.split(".")
            if len(parts) != 2 or not all(parts):
                raise RuntimeError("Resource observation definition is invalid")
            schema_name, relation_name = parts
            folded_schema = schema_name.casefold()
            if folded_schema == "information_schema" or folded_schema.startswith("pg_"):
                raise RuntimeError("Resource observation definition is invalid")
            relation_parts.append((schema_name, relation_name))

        representative_position = storage_relations.index(representative_relation) + 1
        pool = await self._get_pool(source)
        async with pool.connection() as connection:
            await _require_catalog_connection_policy(connection)
            try:
                await _begin_catalog_transaction(connection, source)
                cursor = await connection.execute(
                    RESOURCE_OBSERVATION_QUERY,
                    (
                        [schema_name for schema_name, _ in relation_parts],
                        [relation_name for _, relation_name in relation_parts],
                        representative_position,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise RuntimeError("Resource observation targets are unavailable")
                try:
                    estimated = row["representative_records"]
                    representative_records = (
                        None if estimated is None else round(float(estimated))
                    )
                    table_bytes = int(row["table_bytes"])
                    index_bytes = int(row["index_bytes"])
                    total_storage_bytes = int(row["total_storage_bytes"])
                except (KeyError, TypeError, ValueError, OverflowError) as error:
                    raise RuntimeError("Resource observation result is invalid") from error
                if representative_records is not None and representative_records < 0:
                    raise RuntimeError("Resource observation result is invalid")
                if min(table_bytes, index_bytes, total_storage_bytes) < 0:
                    raise RuntimeError("Resource observation result is invalid")
                observation = ResourceObservation(
                    representative_records=representative_records,
                    table_bytes=table_bytes,
                    index_bytes=index_bytes,
                    total_storage_bytes=total_storage_bytes,
                )
                await connection.execute("COMMIT")
            except Exception:
                await connection.rollback()
                raise
        return observation

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()

    async def invalidate(self, source_id: str) -> None:
        async with self._pool_lock:
            pool = self._pools.pop(source_id, None)
        if pool is not None:
            await pool.close()

    async def _get_pool(self, source: SourceProfile) -> AsyncConnectionPool[Any]:
        existing = self._pools.get(source.source_id)
        if existing is not None:
            return existing
        async with self._pool_lock:
            existing = self._pools.get(source.source_id)
            if existing is not None:
                return existing
            connection = source.connection
            pool = AsyncConnectionPool(
                conninfo="",
                kwargs={
                    "host": connection.host,
                    "port": connection.port,
                    "dbname": connection.database,
                    "user": connection.user,
                    "password": connection.password,
                    "sslmode": "verify-full" if connection.ssl else "disable",
                    "application_name": f"query-man-meta:{source.source_id}",
                    "connect_timeout": 2,
                    "client_encoding": READER_CLIENT_ENCODING,
                    # ponytail: explicit BEGIN must not be preceded by psycopg's implicit BEGIN.
                    "autocommit": True,
                    "row_factory": dict_row,
                },
                min_size=0,
                # MetadataService coalesces refreshes per source, so one catalog connection is sufficient.
                max_size=1,
                timeout=2,
                max_idle=10,
                open=False,
            )
            await pool.open()
            self._pools[source.source_id] = pool
            return pool


@dataclass
class _CatalogRelationBuilder:
    schema: str
    name: str
    qualified_name: str
    sql_name: str
    kind: CatalogRelationKind
    comment: str | None
    estimated_rows: int | None
    definition_hash: str | None
    security_invoker: bool
    columns: list[CatalogColumn] = field(default_factory=list)

    def freeze(self) -> CatalogRelation:
        return CatalogRelation(
            schema=self.schema,
            name=self.name,
            qualified_name=self.qualified_name,
            sql_name=self.sql_name,
            kind=self.kind,
            columns=tuple(self.columns),
            comment=self.comment,
            estimated_rows=self.estimated_rows,
            definition_hash=self.definition_hash,
            security_invoker=self.security_invoker,
        )


def _rows_to_relations(rows: list[dict[str, Any]]) -> list[_CatalogRelationBuilder]:
    relations: dict[str, _CatalogRelationBuilder] = {}
    for row in rows:
        qualified_name = f"{row['schema_name']}.{row['relation_name']}"
        relation = relations.get(qualified_name)
        if relation is None:
            kind = str(row["relation_kind"])
            estimated = row["estimated_rows"]
            relation = _CatalogRelationBuilder(
                schema=str(row["schema_name"]),
                name=str(row["relation_name"]),
                qualified_name=qualified_name,
                sql_name=f"{_quote_identifier(str(row['schema_name']))}.{_quote_identifier(str(row['relation_name']))}",
                kind=_CATALOG_KINDS[kind],  # type: ignore[arg-type]
                comment=_sanitize_comment(row["relation_comment"]),
                definition_hash=row["view_definition_hash"],
                security_invoker=bool(row["security_invoker"]),
                estimated_rows=None if estimated is None else max(0, round(float(estimated))),
            )
            relations[qualified_name] = relation
        relation.columns.append(
            CatalogColumn(
                name=str(row["column_name"]),
                sql_name=_quote_identifier(str(row["column_name"])),
                ordinal=int(row["ordinal"]),
                data_type=str(row["data_type"]),
                nullable=("unknown" if row["relation_kind"] in {"v", "m"} else not bool(row["is_not_null"])),
                comment=_sanitize_comment(row["column_comment"]),
            )
        )
    return sorted(relations.values(), key=lambda item: item.qualified_name)


def _require_supported_catalog_types(rows: Sequence[dict[str, Any]]) -> None:
    if any(row.get("type_kind") == "d" for row in rows):
        raise _CatalogValidationError("Catalog contains an unsupported domain column")


def _apply_structures(
    relations: Sequence[CatalogRelation],
    rows: list[dict[str, Any]],
) -> tuple[CatalogRelation, ...]:
    by_name = {relation.qualified_name for relation in relations}
    primary_keys = {
        relation.qualified_name: relation.primary_key
        for relation in relations
        if relation.primary_key
    }
    foreign_keys = {
        relation.qualified_name: list(relation.foreign_keys) for relation in relations
    }
    indexes = {
        relation.qualified_name: list(relation.indexes) for relation in relations
    }
    for row in rows:
        qualified_name = f"{row['schema_name']}.{row['relation_name']}"
        if qualified_name not in by_name:
            raise _CatalogValidationError("Catalog structure references an unavailable relation")
        columns = tuple(str(column) for column in row["column_names"])
        kind = row["structure_kind"]
        if kind == "primary_key":
            if qualified_name in primary_keys:
                raise _CatalogValidationError("Catalog relation has multiple primary keys")
            primary_keys[qualified_name] = columns
        elif kind == "foreign_key":
            foreign_keys[qualified_name].append(
                CatalogForeignKey(
                    columns=columns,
                    referenced_relation=str(row["referenced_relation"]),
                    referenced_columns=tuple(
                        str(column) for column in row["referenced_columns"]
                    ),
                )
            )
        elif kind == "index":
            indexes[qualified_name].append(
                CatalogIndex(
                    columns=columns,
                    unique=bool(row["is_unique"]),
                    primary=bool(row["is_primary"]),
                )
            )
        else:
            raise _CatalogValidationError("Catalog returned an unknown structure kind")
    return tuple(
        replace(
            relation,
            primary_key=primary_keys.get(relation.qualified_name, ()),
            foreign_keys=tuple(foreign_keys[relation.qualified_name]),
            indexes=tuple(indexes[relation.qualified_name]),
        )
        for relation in relations
    )


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _sanitize_comment(value: str | None) -> str | None:
    if value is None:
        return None
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
    return sanitized[:2000] or None
