from __future__ import annotations

import asyncio
import re
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from query_man.models import (
    CatalogColumn,
    CatalogForeignKey,
    CatalogIndex,
    CatalogRelation,
    CatalogSnapshot,
    SourceProfile,
)
from query_man.reader_policy import apply_reader_session_budget, require_reader_session_policy

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
    attribute.attnotnull AS is_not_null,
    pg_catalog.left(pg_catalog.col_description(relation.relation_oid, attribute.attnum), 2000)
      AS column_comment
  FROM eligible_relations AS relation
  JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid = relation.relation_oid
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


class PostgresCatalog:
    def __init__(self) -> None:
        self._pools: dict[str, AsyncConnectionPool[Any]] = {}
        self._pool_lock = asyncio.Lock()

    async def load(self, source: SourceProfile) -> CatalogSnapshot:
        pool = await self._get_pool(source)
        async with pool.connection() as connection:
            try:
                await connection.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                await connection.execute(
                    "SELECT pg_catalog.set_config('statement_timeout', %s, true), "
                    "pg_catalog.set_config('lock_timeout', %s, true)",
                    (
                        f"{source.budget.metadata_statement_timeout_ms}ms",
                        f"{source.budget.lock_timeout_ms}ms",
                    ),
                )
                await apply_reader_session_budget(connection, source)
                await require_reader_session_policy(connection, source)
                cursor = await connection.execute(
                    CATALOG_QUERY,
                    (
                        source.allowed_schemas,
                        [_POSTGRES_KINDS[kind] for kind in source.allowed_relation_kinds],
                        source.budget.max_metadata_columns + 1,
                    ),
                )
                rows = await cursor.fetchall()
                if len(rows) > source.budget.max_metadata_columns:
                    raise RuntimeError("Catalog column limit exceeded")
                structure_cursor = await connection.execute(
                    STRUCTURE_QUERY,
                    (
                        source.allowed_schemas,
                        [_POSTGRES_KINDS[kind] for kind in source.allowed_relation_kinds],
                        source.budget.max_metadata_columns + 1,
                    ),
                )
                structures = await structure_cursor.fetchall()
                if len(structures) > source.budget.max_metadata_columns:
                    raise RuntimeError("Catalog structure limit exceeded")
                await connection.execute("COMMIT")
            except Exception:
                await connection.rollback()
                raise

        relations = _rows_to_relations(rows)
        _apply_structures(relations, structures)
        if len(relations) > source.budget.max_metadata_relations:
            raise RuntimeError("Catalog relation limit exceeded")
        if any(len(relation.columns) > source.budget.max_columns_per_relation for relation in relations):
            raise RuntimeError("Catalog per-relation column limit exceeded")
        return CatalogSnapshot(relations=relations)

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


def _rows_to_relations(rows: list[dict[str, Any]]) -> list[CatalogRelation]:
    relations: dict[str, CatalogRelation] = {}
    for row in rows:
        qualified_name = f"{row['schema_name']}.{row['relation_name']}"
        relation = relations.get(qualified_name)
        if relation is None:
            kind = str(row["relation_kind"])
            estimated = row["estimated_rows"]
            relation = CatalogRelation(
                schema=str(row["schema_name"]),
                name=str(row["relation_name"]),
                qualified_name=qualified_name,
                sql_name=f"{_quote_identifier(str(row['schema_name']))}.{_quote_identifier(str(row['relation_name']))}",
                kind=_CATALOG_KINDS[kind],  # type: ignore[arg-type]
                comment=_sanitize_comment(row["relation_comment"]),
                definition_hash=row["view_definition_hash"],
                security_invoker=bool(row["security_invoker"]),
                estimated_rows=None if estimated is None else max(0, round(float(estimated))),
                columns=[],
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


def _apply_structures(
    relations: list[CatalogRelation],
    rows: list[dict[str, Any]],
) -> None:
    by_name = {relation.qualified_name: relation for relation in relations}
    for row in rows:
        qualified_name = f"{row['schema_name']}.{row['relation_name']}"
        relation = by_name.get(qualified_name)
        if relation is None:
            raise RuntimeError("Catalog structure references an unavailable relation")
        columns = [str(column) for column in row["column_names"]]
        kind = row["structure_kind"]
        if kind == "primary_key":
            if relation.primary_key:
                raise RuntimeError("Catalog relation has multiple primary keys")
            relation.primary_key = columns
        elif kind == "foreign_key":
            relation.foreign_keys.append(
                CatalogForeignKey(
                    columns=columns,
                    referenced_relation=str(row["referenced_relation"]),
                    referenced_columns=[str(column) for column in row["referenced_columns"]],
                )
            )
        elif kind == "index":
            relation.indexes.append(
                CatalogIndex(
                    columns=columns,
                    unique=bool(row["is_unique"]),
                    primary=bool(row["is_primary"]),
                )
            )
        else:
            raise RuntimeError("Catalog returned an unknown structure kind")


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _sanitize_comment(value: str | None) -> str | None:
    if value is None:
        return None
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
    return sanitized[:2000] or None
