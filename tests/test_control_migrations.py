from __future__ import annotations

import hashlib
import subprocess

import pytest
from psycopg import AsyncConnection

from tests.control_database import (
    DisposableControlDatabase,
    apply_control_migrations,
)
from tests.helpers import ROOT_DIRECTORY


async def _control_contract(connection: AsyncConnection[object]) -> tuple[object, ...]:
    cursor = await connection.execute(
        "SELECT "
        "(SELECT array_agg(version::text || ':' || filename || ':' || checksum "
        " ORDER BY version) FROM control.schema_migrations), "
        "(SELECT array_agg(relation_row.relname || ':' || relation_row.oid::text "
        " ORDER BY relation_row.relname) "
        " FROM pg_catalog.pg_class AS relation_row "
        " JOIN pg_catalog.pg_namespace AS namespace_row "
        " ON namespace_row.oid = relation_row.relnamespace "
        " WHERE namespace_row.nspname = 'control' AND relation_row.relkind = 'r'), "
        "(SELECT count(*) FROM pg_catalog.pg_constraint AS constraint_row "
        " JOIN pg_catalog.pg_namespace AS namespace_row "
        " ON namespace_row.oid = constraint_row.connamespace "
        " WHERE namespace_row.nspname = 'control' AND constraint_row.contype = 'f'), "
        "(SELECT count(*) FROM pg_catalog.pg_trigger AS trigger_row "
        " JOIN pg_catalog.pg_class AS relation_row ON relation_row.oid = trigger_row.tgrelid "
        " JOIN pg_catalog.pg_namespace AS namespace_row "
        " ON namespace_row.oid = relation_row.relnamespace "
        " WHERE namespace_row.nspname = 'control' AND NOT trigger_row.tgisinternal), "
        "NOT has_table_privilege('query_man_control_writer', "
        "'control.schema_migrations', 'SELECT,INSERT,UPDATE,DELETE'), "
        "has_table_privilege('query_man_control_writer', "
        "'control.metadata_snapshots', 'SELECT,INSERT'), "
        "NOT has_table_privilege('query_man_control_writer', "
        "'control.metadata_snapshots', 'UPDATE,DELETE')"
    )
    row = await cursor.fetchone()
    assert row is not None
    return tuple(row)


@pytest.mark.integration
async def test_control_migrations_are_versioned_idempotent_and_fail_on_drift(
    disposable_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = disposable_control_database_fixture
    migration_path = (
        ROOT_DIRECTORY
        / "docker"
        / "postgres"
        / "init"
        / "control-migrations"
        / "0001_baseline.sql"
    )
    expected_checksum = f"sha256:{hashlib.sha256(migration_path.read_bytes()).hexdigest()}"
    connection = await AsyncConnection.connect(database.dsn)
    try:
        first_contract = await _control_contract(connection)
        assert first_contract[0] == [
            f"1:0001_baseline.sql:{expected_checksum}"
        ]
        assert first_contract[2:] == (4, 3, True, True, True)
        assert len(first_contract[1]) == 6

        await connection.execute(
            "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
            "VALUES ('migration-sentinel', %s, '{\"relations\": []}'::jsonb)",
            (f"sha256:{'0' * 64}",),
        )
        await connection.commit()
        cursor = await connection.execute(
            "SELECT published_at, snapshot FROM control.metadata_snapshots "
            "WHERE source_id = 'migration-sentinel'"
        )
        sentinel_before = await cursor.fetchone()

        apply_control_migrations(database)

        assert await _control_contract(connection) == first_contract
        cursor = await connection.execute(
            "SELECT published_at, snapshot FROM control.metadata_snapshots "
            "WHERE source_id = 'migration-sentinel'"
        )
        assert await cursor.fetchone() == sentinel_before

        await connection.execute(
            "UPDATE control.schema_migrations SET checksum = %s WHERE version = 1",
            (f"sha256:{'f' * 64}",),
        )
        await connection.commit()
        with pytest.raises(subprocess.CalledProcessError):
            apply_control_migrations(database)
    finally:
        await connection.close()


@pytest.mark.integration
async def test_control_migrations_reject_a_database_ahead_of_the_checkout(
    disposable_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = disposable_control_database_fixture
    connection = await AsyncConnection.connect(database.dsn)
    try:
        await connection.execute(
            "INSERT INTO control.schema_migrations (version, filename, checksum) "
            "VALUES (2, '0002_future.sql', %s)",
            (f"sha256:{'0' * 64}",),
        )
        await connection.commit()
        with pytest.raises(subprocess.CalledProcessError):
            apply_control_migrations(database)
        cursor = await connection.execute(
            "SELECT version, filename FROM control.schema_migrations ORDER BY version"
        )
        assert await cursor.fetchall() == [
            (1, "0001_baseline.sql"),
            (2, "0002_future.sql"),
        ]
    finally:
        await connection.close()
