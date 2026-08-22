from __future__ import annotations

import re
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from query_man.models import CatalogColumn, CatalogRelation, CatalogSnapshot, SourceProfile

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
                cursor = await connection.execute(
                    "SELECT pg_catalog.current_database() = %s AS database_matches, "
                    "session_user = %s AS user_matches, "
                    "pg_catalog.current_setting('transaction_read_only') = 'on' AS read_only",
                    (source.connection.database, source.connection.user),
                )
                identity = await cursor.fetchone()
                if not identity or not all(identity.values()):
                    raise RuntimeError("Source session identity or read-only policy mismatch")
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
                await connection.execute("COMMIT")
            except Exception:
                await connection.rollback()
                raise

        relations = _rows_to_relations(rows)
        if len(relations) > source.budget.max_metadata_relations:
            raise RuntimeError("Catalog relation limit exceeded")
        if any(len(relation.columns) > source.budget.max_columns_per_relation for relation in relations):
            raise RuntimeError("Catalog per-relation column limit exceeded")
        return CatalogSnapshot(relations=relations)

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()

    async def _get_pool(self, source: SourceProfile) -> AsyncConnectionPool[Any]:
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
                "row_factory": dict_row,
            },
            min_size=0,
            max_size=source.budget.max_pool_size,
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


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _sanitize_comment(value: str | None) -> str | None:
    if value is None:
        return None
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
    return sanitized[:2000] or None
