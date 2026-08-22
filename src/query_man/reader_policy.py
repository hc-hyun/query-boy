from __future__ import annotations

from typing import Any

from psycopg import AsyncConnection

from query_man.models import SourceProfile


class ReaderSessionPolicyError(RuntimeError):
    pass


_READER_SESSION_POLICY_QUERY = """
  SELECT
    pg_catalog.current_database() = %s AS database_matches,
    session_user = %s AS user_matches,
    pg_catalog.current_setting('transaction_read_only') = 'on' AS read_only,
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
    NOT pg_catalog.has_database_privilege(
      session_user,
      pg_catalog.current_database(),
      'TEMP'
    ) AS no_temp_privilege,
    NOT EXISTS (
      SELECT 1
      FROM pg_catalog.unnest(%s::text[]) AS schema_name
      WHERE pg_catalog.has_schema_privilege(session_user, schema_name, 'CREATE')
    ) AS no_allowed_schema_create_privilege
  FROM pg_catalog.pg_roles AS role
  WHERE role.rolname = session_user
"""


async def require_reader_session_policy(
    connection: AsyncConnection[Any],
    source: SourceProfile,
) -> None:
    cursor = await connection.execute(
        _READER_SESSION_POLICY_QUERY,
        (
            source.connection.database,
            source.connection.user,
            source.allowed_schemas,
        ),
    )
    policy = await cursor.fetchone()
    if not policy or not all(policy.values()):
        raise ReaderSessionPolicyError("Source reader session policy mismatch")
