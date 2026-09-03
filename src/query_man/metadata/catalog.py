from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from query_man.metadata.models import (
    CatalogColumn,
    CatalogRelation,
    CatalogRelationKind,
    CatalogSnapshot,
)
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.reader_policy import (
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
      coalesce(relation.reloptions @> ARRAY['security_barrier=true'], false)
        AS security_barrier
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
    relation.security_barrier,
    attribute.attnum::integer AS ordinal,
    attribute.attname::text AS column_name,
    pg_catalog.format_type(attribute.atttypid, attribute.atttypmod) AS data_type,
    type_row.typtype::text AS type_kind,
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

_VIEW_CONTRACT_MARKER = re.compile(
    r"query-man:source=(?P<source>[a-z0-9]+(?:-[a-z0-9]+)*);"
    r"view-contract=(?P<version>[1-9][0-9]*)"
)


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
    source: SourceProfile,
) -> None:
    try:
        require_reader_connection_policy(connection, source.connection.sslmode)
    except ReaderSessionPolicyError:
        try:
            await connection.close()
        except Exception:
            pass
        raise


class PostgresCatalog:
    def __init__(self) -> None:
        self._pools: dict[str, AsyncConnectionPool[Any]] = {}
        self._pool_lock = asyncio.Lock()

    async def load(self, source: SourceProfile) -> CatalogSnapshot:
        pool = await self._get_pool(source)
        async with pool.connection() as connection:
            await _require_catalog_connection_policy(connection, source)
            try:
                await _begin_catalog_transaction(connection, source)
                cursor = await connection.execute(
                    CATALOG_QUERY,
                    (
                        list(source.allowed_schemas),
                        ["v"],
                        source.budget.max_metadata_columns + 1,
                    ),
                )
                rows = await cursor.fetchall()
                if len(rows) > source.budget.max_metadata_columns:
                    raise _CatalogValidationError("Catalog column limit exceeded")
                _require_supported_catalog_types(rows)
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
        return CatalogSnapshot(
            relations=tuple(relation.freeze() for relation in relations)
        )

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        self._pools.clear()

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
                    "sslmode": connection.sslmode,
                    "gssencmode": "disable",
                    "application_name": f"query-man-meta:{source.source_id}",
                    "connect_timeout": 2,
                    "client_encoding": READER_CLIENT_ENCODING,
                    # ponytail: explicit BEGIN must not be preceded by psycopg's implicit BEGIN.
                    "autocommit": True,
                    "row_factory": dict_row,
                },
                min_size=0,
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
    definition_hash: str | None
    view_contract_source: str | None
    view_contract_version: int | None
    security_invoker: bool
    security_barrier: bool
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
            definition_hash=self.definition_hash,
            view_contract_source=self.view_contract_source,
            view_contract_version=self.view_contract_version,
            security_invoker=self.security_invoker,
            security_barrier=self.security_barrier,
        )


def _rows_to_relations(rows: list[dict[str, Any]]) -> list[_CatalogRelationBuilder]:
    relations: dict[str, _CatalogRelationBuilder] = {}
    for row in rows:
        qualified_name = f"{row['schema_name']}.{row['relation_name']}"
        relation = relations.get(qualified_name)
        if relation is None:
            if row["relation_kind"] != "v":
                raise _CatalogValidationError("Catalog returned a non-view relation")
            contract_source, contract_version, comment = _parse_view_comment(
                row["relation_comment"]
            )
            relation = _CatalogRelationBuilder(
                schema=str(row["schema_name"]),
                name=str(row["relation_name"]),
                qualified_name=qualified_name,
                sql_name=f"{_quote_identifier(str(row['schema_name']))}.{_quote_identifier(str(row['relation_name']))}",
                kind="view",
                comment=comment,
                definition_hash=row["view_definition_hash"],
                view_contract_source=contract_source,
                view_contract_version=contract_version,
                security_invoker=bool(row["security_invoker"]),
                security_barrier=bool(row.get("security_barrier", False)),
            )
            relations[qualified_name] = relation
        relation.columns.append(
            CatalogColumn(
                name=str(row["column_name"]),
                sql_name=_quote_identifier(str(row["column_name"])),
                ordinal=int(row["ordinal"]),
                data_type=str(row["data_type"]),
                nullable="unknown",
                comment=_sanitize_comment(row["column_comment"]),
            )
        )
    return sorted(relations.values(), key=lambda item: item.qualified_name)


def _require_supported_catalog_types(rows: Sequence[dict[str, Any]]) -> None:
    if any(row.get("type_kind") == "d" for row in rows):
        raise _CatalogValidationError("Catalog contains an unsupported domain column")


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _sanitize_comment(value: str | None) -> str | None:
    if value is None:
        return None
    sanitized = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
    return sanitized[:2000] or None


def _parse_view_comment(value: str | None) -> tuple[str | None, int | None, str | None]:
    if value is None:
        return None, None, None
    first_line, separator, description = value.partition("\n")
    if first_line.endswith("\r"):
        first_line = first_line[:-1]
    marker = _VIEW_CONTRACT_MARKER.fullmatch(first_line)
    if marker is None:
        raise _CatalogValidationError("View comment has an invalid contract marker")
    human_description = _sanitize_comment(description if separator else None)
    if human_description is None:
        raise _CatalogValidationError("View comment requires a human description")
    return (
        marker.group("source"),
        int(marker.group("version")),
        human_description,
    )
