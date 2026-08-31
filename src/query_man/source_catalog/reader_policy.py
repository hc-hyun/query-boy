from __future__ import annotations

from typing import Any, Final

from psycopg import AsyncConnection

from query_man.source_catalog.models import SourceProfile, SSLMode


class ReaderSessionPolicyError(RuntimeError):
    pass


READER_CLIENT_ENCODING: Final = "UTF8"

READER_SESSION_TIMEZONE_SETTER = (
    "SELECT pg_catalog.set_config('TimeZone', 'UTC', true)"
)

READER_SESSION_BUDGET_SETTERS = """
  pg_catalog.set_config('work_mem', %s, true),
  pg_catalog.set_config('temp_file_limit', %s, true),
  pg_catalog.set_config('max_parallel_workers_per_gather', %s, true),
  pg_catalog.set_config('jit', %s, true)
"""

_READER_SESSION_POLICY_QUERY = """
  SELECT
    pg_catalog.current_database() = %s AS database_matches,
    session_user = %s AS user_matches,
    pg_catalog.current_setting('transaction_read_only') = 'on' AS read_only,
    pg_catalog.current_setting('transaction_isolation') = 'repeatable read'
      AS repeatable_read,
    pg_catalog.current_setting('TimeZone') = 'UTC' AS utc_timezone,
    pg_catalog.current_setting('default_transaction_read_only') = 'on'
      AS defaults_read_only,
    role.rolcanlogin
      AND NOT role.rolsuper
      AND NOT role.rolcreatedb
      AND NOT role.rolcreaterole
      AND NOT role.rolinherit
      AND NOT role.rolreplication
      AND NOT role.rolbypassrls
      AND role.rolconnlimit > 0 AS restricted_role,
    NOT EXISTS (
      SELECT 1
      FROM pg_catalog.unnest(%s::text[]) AS schema_name
      WHERE pg_catalog.has_schema_privilege(session_user, schema_name, 'CREATE')
    ) AS no_allowed_schema_create_privilege,
    pg_catalog.pg_size_bytes(pg_catalog.current_setting('work_mem'))
      = %s::bigint * 1024 AS work_mem_matches,
    pg_catalog.pg_size_bytes(pg_catalog.current_setting('temp_file_limit'))
      = %s::bigint * 1024 AS temp_file_limit_matches,
    pg_catalog.current_setting('max_parallel_workers_per_gather')::integer
      = %s AS parallel_workers_match,
    pg_catalog.current_setting('jit')::boolean = %s AS jit_matches,
    pg_catalog.current_schemas(false) = ARRAY['pg_catalog']::name[]
      AS trusted_search_path,
    pg_catalog.current_setting('row_security') = 'on' AS row_security_enabled,
    coalesce(
      pg_catalog.current_setting('query_man.tenant_id', true), ''
    ) = %s AS trusted_tenant_context
  FROM pg_catalog.pg_roles AS role
  WHERE role.rolname = session_user
"""


def require_reader_connection_policy(
    connection: AsyncConnection[Any],
    sslmode: SSLMode,
) -> None:
    info = connection.info
    if (
        not 180_000 <= info.server_version < 190_000
        or info.parameter_status("server_encoding") != READER_CLIENT_ENCODING
        or info.parameter_status("client_encoding") != READER_CLIENT_ENCODING
        or info.encoding != "utf-8"
    ):
        raise ReaderSessionPolicyError("Source reader connection policy mismatch")
    pgconn = connection.pgconn
    if (
        sslmode not in ("disable", "require", "verify-full")
        or pgconn.ssl_in_use != (sslmode != "disable")
    ):
        raise ReaderSessionPolicyError("Source reader connection policy mismatch")


def reader_session_budget_values(
    source: SourceProfile,
) -> tuple[str, str, str, str]:
    return (
        f"{source.budget.work_mem_kb}kB",
        f"{source.budget.temp_file_limit_kb}kB",
        str(source.budget.max_parallel_workers_per_gather),
        "on" if source.budget.jit_enabled else "off",
    )


async def require_reader_session_policy(
    connection: AsyncConnection[Any],
    source: SourceProfile,
    trusted_tenant: str = "",
) -> None:
    cursor = await connection.execute(
        _READER_SESSION_POLICY_QUERY,
        (
            source.connection.database,
            source.connection.user,
            list(source.allowed_schemas),
            source.budget.work_mem_kb,
            source.budget.temp_file_limit_kb,
            source.budget.max_parallel_workers_per_gather,
            source.budget.jit_enabled,
            trusted_tenant,
        ),
    )
    policy = await cursor.fetchone()
    if not policy or not all(policy.values()):
        raise ReaderSessionPolicyError("Source reader session policy mismatch")
