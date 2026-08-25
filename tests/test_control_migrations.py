from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from psycopg import AsyncConnection, errors, sql
from psycopg.types.json import Jsonb

from tests.control_database import (
    DisposableControlDatabase,
    apply_control_migrations,
    authority_fingerprint,
    disposable_control_database,
    postgres_dsn,
    postgres_environment,
)
from tests.helpers import ROOT_DIRECTORY

_MIGRATION_DIRECTORY = (
    ROOT_DIRECTORY / "docker" / "postgres" / "init" / "control-migrations"
)
_V2_WRITER_GRANTS = """\
GRANT SELECT, INSERT ON control.source_mutation_receipts
  TO query_man_control_writer;
GRANT USAGE ON SEQUENCE control.source_mutation_receipts_event_id_seq
  TO query_man_control_writer;
"""
_V3_WRITER_GRANTS = """\
GRANT SELECT, INSERT, UPDATE ON control.runtime_replicas
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.runtime_source_observations
  TO query_man_control_writer;
"""
_V4_WRITER_GRANTS = """\
GRANT SELECT, INSERT, UPDATE ON control.source_resource_observations
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON control.gateway_usage_rollups
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.gateway_usage_report_cursors
  TO query_man_control_writer;
"""


def _copy_v1_migrations(tmp_path: Path) -> Path:
    migration_directory = tmp_path / "v1-control-migrations"
    shutil.copytree(_MIGRATION_DIRECTORY, migration_directory)
    numbered_migrations = sorted(
        migration_directory.glob("[0-9][0-9][0-9][0-9]_*.sql")
    )
    assert [path.name for path in numbered_migrations[:2]] == [
        "0001_baseline.sql",
        "0002_source_mutation_receipts.sql",
    ]
    for migration_path in numbered_migrations[1:]:
        migration_path.unlink()

    security_path = migration_directory / "reconcile-security.sql"
    security_sql = security_path.read_text(encoding="utf-8")
    assert _V2_WRITER_GRANTS in security_sql
    assert _V3_WRITER_GRANTS in security_sql
    assert _V4_WRITER_GRANTS in security_sql
    security_path.write_text(
        security_sql.replace(_V2_WRITER_GRANTS, "")
        .replace(_V3_WRITER_GRANTS, "")
        .replace(_V4_WRITER_GRANTS, ""),
        encoding="utf-8",
    )
    return migration_directory


def _copy_v2_migrations(tmp_path: Path) -> Path:
    migration_directory = tmp_path / "v2-control-migrations"
    shutil.copytree(_MIGRATION_DIRECTORY, migration_directory)
    numbered_migrations = sorted(
        migration_directory.glob("[0-9][0-9][0-9][0-9]_*.sql")
    )
    assert [path.name for path in numbered_migrations[:3]] == [
        "0001_baseline.sql",
        "0002_source_mutation_receipts.sql",
        "0003_runtime_replica_observations.sql",
    ]
    for migration_path in numbered_migrations[2:]:
        migration_path.unlink()

    security_path = migration_directory / "reconcile-security.sql"
    security_sql = security_path.read_text(encoding="utf-8")
    assert _V3_WRITER_GRANTS in security_sql
    assert _V4_WRITER_GRANTS in security_sql
    security_path.write_text(
        security_sql.replace(_V3_WRITER_GRANTS, "").replace(
            _V4_WRITER_GRANTS,
            "",
        ),
        encoding="utf-8",
    )
    return migration_directory


def _copy_v3_migrations(tmp_path: Path) -> Path:
    migration_directory = tmp_path / "v3-control-migrations"
    shutil.copytree(_MIGRATION_DIRECTORY, migration_directory)
    numbered_migrations = sorted(
        migration_directory.glob("[0-9][0-9][0-9][0-9]_*.sql")
    )
    assert [path.name for path in numbered_migrations[:4]] == [
        "0001_baseline.sql",
        "0002_source_mutation_receipts.sql",
        "0003_runtime_replica_observations.sql",
        "0004_source_resource_and_gateway_usage.sql",
    ]
    for migration_path in numbered_migrations[3:]:
        migration_path.unlink()

    security_path = migration_directory / "reconcile-security.sql"
    security_sql = security_path.read_text(encoding="utf-8")
    assert _V4_WRITER_GRANTS in security_sql
    security_path.write_text(
        security_sql.replace(_V4_WRITER_GRANTS, ""),
        encoding="utf-8",
    )
    return migration_directory


def _pause_before_security_reconciliation(migration_directory: Path) -> None:
    apply_path = migration_directory / "apply.sh"
    apply_script = apply_path.read_text(encoding="utf-8")
    marker = "# The final ledger check and security reconciliation must share"
    assert marker in apply_script
    barrier = """\
psql --no-psqlrc --quiet --set=ON_ERROR_STOP=1 --command="
  CREATE TABLE control.mixed_checkout_barrier (released boolean NOT NULL);
  INSERT INTO control.mixed_checkout_barrier VALUES (false);
" >/dev/null
released=f
for ((attempt = 0; attempt < 600; attempt += 1)); do
  released="$(psql --no-psqlrc --quiet --tuples-only --no-align \\
    --set=ON_ERROR_STOP=1 \\
    --command='SELECT released FROM control.mixed_checkout_barrier')"
  [[ "$released" == "t" ]] && break
  sleep 0.05
done
if [[ "$released" != "t" ]]; then
  echo 'Timed out waiting for the mixed-checkout test barrier.' >&2
  exit 4
fi

"""
    apply_path.write_text(
        apply_script.replace(marker, barrier + marker),
        encoding="utf-8",
    )


@pytest.fixture
async def v1_control_database_fixture(
    tmp_path: Path,
) -> AsyncIterator[DisposableControlDatabase]:
    environment = postgres_environment()
    if environment is None:
        pytest.skip("local PostgreSQL control-plane credentials are not configured")
    development_dsn = postgres_dsn(environment, environment["POSTGRES_DB"])
    development_before = await authority_fingerprint(development_dsn)
    async with disposable_control_database(
        environment,
        _copy_v1_migrations(tmp_path),
    ) as database:
        yield database
    assert await authority_fingerprint(development_dsn) == development_before


@pytest.fixture
async def v2_control_database_fixture(
    tmp_path: Path,
) -> AsyncIterator[DisposableControlDatabase]:
    environment = postgres_environment()
    if environment is None:
        pytest.skip("local PostgreSQL control-plane credentials are not configured")
    development_dsn = postgres_dsn(environment, environment["POSTGRES_DB"])
    development_before = await authority_fingerprint(development_dsn)
    async with disposable_control_database(
        environment,
        _copy_v2_migrations(tmp_path),
    ) as database:
        yield database
    assert await authority_fingerprint(development_dsn) == development_before


@pytest.fixture
async def v3_control_database_fixture(
    tmp_path: Path,
) -> AsyncIterator[DisposableControlDatabase]:
    environment = postgres_environment()
    if environment is None:
        pytest.skip("local PostgreSQL control-plane credentials are not configured")
    development_dsn = postgres_dsn(environment, environment["POSTGRES_DB"])
    development_before = await authority_fingerprint(development_dsn)
    async with disposable_control_database(
        environment,
        _copy_v3_migrations(tmp_path),
    ) as database:
        yield database
    assert await authority_fingerprint(development_dsn) == development_before


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
    migration_directory = (
        ROOT_DIRECTORY
        / "docker"
        / "postgres"
        / "init"
        / "control-migrations"
    )
    expected_migrations = [
        (
            version,
            path.name,
            f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}",
        )
        for version, path in enumerate(
            sorted(migration_directory.glob("[0-9][0-9][0-9][0-9]_*.sql")),
            start=1,
        )
    ]
    connection = await AsyncConnection.connect(database.dsn)
    try:
        first_contract = await _control_contract(connection)
        assert first_contract[0] == [
            f"{version}:{filename}:{checksum}"
            for version, filename, checksum in expected_migrations
        ]
        assert first_contract[2:] == (14, 4, True, True, True)
        assert len(first_contract[1]) == 12

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
            "UPDATE control.schema_migrations SET checksum = %s WHERE version = 4",
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
            "VALUES (5, '0005_future.sql', %s)",
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
            (2, "0002_source_mutation_receipts.sql"),
            (3, "0003_runtime_replica_observations.sql"),
            (4, "0004_source_resource_and_gateway_usage.sql"),
            (5, "0005_future.sql"),
        ]
    finally:
        await connection.close()


@pytest.mark.integration
async def test_v1_to_v2_upgrade_preserves_existing_authority_data(
    v1_control_database_fixture: DisposableControlDatabase,
    tmp_path: Path,
) -> None:
    database = v1_control_database_fixture
    revision = f"sha256:{'1' * 64}"
    connection = await AsyncConnection.connect(database.dsn)
    try:
        cursor = await connection.execute(
            "SELECT array_agg(version ORDER BY version), "
            "pg_catalog.to_regclass('control.source_mutation_receipts') "
            "FROM control.schema_migrations"
        )
        assert await cursor.fetchone() == ([1], None)
        await connection.execute(
            "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
            "VALUES ('upgrade-preserved', %s, '{\"relations\": []}'::jsonb)",
            (revision,),
        )
        await connection.execute(
            "INSERT INTO control.active_metadata_revisions "
            "(source_id, revision, pinned) VALUES ('upgrade-preserved', %s, true)",
            (revision,),
        )
        await connection.execute(
            "INSERT INTO control.source_profile_revisions "
            "(source_id, generation, manifest, secret_nonce, secret_ciphertext, "
            "metadata_revision) VALUES "
            "('upgrade-preserved', 1, '{\"schema_version\": 2}'::jsonb, %s, %s, %s)",
            (b"n" * 12, b"c" * 17, revision),
        )
        await connection.execute(
            "INSERT INTO control.active_source_profiles "
            "(source_id, generation, enabled, state_version) "
            "VALUES ('upgrade-preserved', 1, false, 7)"
        )
        await connection.execute(
            "INSERT INTO control.verified_query_contracts "
            "(source_id, query_id, metadata_revision, question, relations, sql, expected) "
            "VALUES ('upgrade-preserved', 'preserved-query', %s, 'preserved?', "
            "'[\"public.items\"]'::jsonb, 'SELECT 1', '{\"rows\": 1}'::jsonb)",
            (revision,),
        )
        await connection.commit()
    finally:
        await connection.close()

    before = await authority_fingerprint(database.dsn)
    assert before[-1] == ("source_mutation_receipts", -1, "missing")

    apply_control_migrations(database, _copy_v2_migrations(tmp_path))

    after = await authority_fingerprint(database.dsn)
    assert after[:-1] == before[:-1]
    assert after[-1][0:2] == ("source_mutation_receipts", 0)
    connection = await AsyncConnection.connect(database.dsn)
    try:
        cursor = await connection.execute(
            "SELECT version, filename FROM control.schema_migrations ORDER BY version"
        )
        assert await cursor.fetchall() == [
            (1, "0001_baseline.sql"),
            (2, "0002_source_mutation_receipts.sql"),
        ]
    finally:
        await connection.close()


@pytest.mark.integration
async def test_v2_to_v3_upgrade_is_additive_and_observation_acl_is_mutable_only(
    v2_control_database_fixture: DisposableControlDatabase,
    tmp_path: Path,
) -> None:
    database = v2_control_database_fixture
    revision = f"sha256:{'6' * 64}"
    connection = await AsyncConnection.connect(database.dsn)
    try:
        await connection.execute(
            "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
            "VALUES ('observation-upgrade', %s, '{\"relations\": []}'::jsonb)",
            (revision,),
        )
        await connection.execute(
            "INSERT INTO control.active_metadata_revisions "
            "(source_id, revision, pinned) VALUES ('observation-upgrade', %s, false)",
            (revision,),
        )
        await connection.execute(
            "INSERT INTO control.source_profile_revisions "
            "(source_id, generation, manifest, secret_nonce, secret_ciphertext, "
            "metadata_revision) VALUES "
            "('observation-upgrade', 1, '{\"schema_version\": 2}'::jsonb, "
            "%s, %s, %s)",
            (b"n" * 12, b"c" * 17, revision),
        )
        await connection.execute(
            "INSERT INTO control.active_source_profiles "
            "(source_id, generation, enabled, state_version) "
            "VALUES ('observation-upgrade', 1, true, 1)"
        )
        await connection.commit()
    finally:
        await connection.close()

    authority_before = await authority_fingerprint(database.dsn)
    apply_control_migrations(database, _copy_v3_migrations(tmp_path))
    assert await authority_fingerprint(database.dsn) == authority_before

    connection = await AsyncConnection.connect(database.dsn)
    try:
        cursor = await connection.execute(
            "SELECT version FROM control.schema_migrations ORDER BY version"
        )
        assert await cursor.fetchall() == [(1,), (2,), (3,)]

        cursor = await connection.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'control' AND table_name IN "
            "('runtime_replicas', 'runtime_source_observations') "
            "ORDER BY table_name, ordinal_position"
        )
        assert await cursor.fetchall() == [
            ("runtime_replicas", "replica_id"),
            ("runtime_replicas", "incarnation"),
            ("runtime_replicas", "heartbeat_interval_ms"),
            ("runtime_replicas", "report_reason_code"),
            ("runtime_replicas", "observed_at"),
            ("runtime_source_observations", "replica_id"),
            ("runtime_source_observations", "incarnation"),
            ("runtime_source_observations", "source_id"),
            ("runtime_source_observations", "applied_generation"),
            ("runtime_source_observations", "applied_state_version"),
            ("runtime_source_observations", "applied_enabled"),
            ("runtime_source_observations", "applied_metadata_revision"),
            ("runtime_source_observations", "source_health"),
            ("runtime_source_observations", "reason_code"),
        ]

        cursor = await connection.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = 'control' "
            "AND indexname = 'runtime_replicas_replica_id_c_idx'"
        )
        index_row = await cursor.fetchone()
        assert index_row is not None
        assert 'replica_id COLLATE "C"' in index_row[0]
    finally:
        await connection.close()

    writer = await AsyncConnection.connect(database.dsn)
    try:
        await writer.execute("SET ROLE query_man_control_writer")
        await writer.execute(
            "INSERT INTO control.runtime_replicas "
            "(replica_id, heartbeat_interval_ms) VALUES ('rolling-replica', 5000)"
        )
        await writer.execute(
            "INSERT INTO control.runtime_source_observations "
            "(replica_id, incarnation, source_id, source_health) "
            "VALUES ('rolling-replica', 1, 'observation-upgrade', 'initializing')"
        )
        await writer.execute(
            "UPDATE control.runtime_replicas "
            "SET report_reason_code = 'CONTROL_SCAN_FAILED' "
            "WHERE replica_id = 'rolling-replica'"
        )
        await writer.execute(
            "UPDATE control.runtime_source_observations SET source_health = 'healthy' "
            "WHERE replica_id = 'rolling-replica' "
            "AND source_id = 'observation-upgrade'"
        )
        await writer.execute(
            "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
            f"VALUES ('rolling-v2-writer', 'sha256:{'7' * 64}', '{{}}'::jsonb)"
        )
        await writer.commit()

        with pytest.raises(errors.InsufficientPrivilege):
            await writer.execute(
                "DELETE FROM control.runtime_source_observations "
                "WHERE replica_id = 'rolling-replica'"
            )
        await writer.rollback()
        with pytest.raises(errors.InsufficientPrivilege):
            await writer.execute("TRUNCATE control.runtime_replicas")
        await writer.rollback()

        cursor = await writer.execute(
            "SELECT report_reason_code, source_health "
            "FROM control.runtime_replicas AS replica "
            "JOIN control.runtime_source_observations AS observation "
            "USING (replica_id, incarnation) "
            "WHERE replica.replica_id = 'rolling-replica'"
        )
        assert await cursor.fetchone() == ("CONTROL_SCAN_FAILED", "healthy")
    finally:
        await writer.close()


@pytest.mark.integration
async def test_v3_to_v4_upgrade_is_additive_idempotent_and_least_privilege(
    v3_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = v3_control_database_fixture
    revision = f"sha256:{'8' * 64}"
    connection = await AsyncConnection.connect(database.dsn)
    try:
        await connection.execute(
            "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
            "VALUES ('usage-upgrade', %s, '{\"relations\": []}'::jsonb)",
            (revision,),
        )
        await connection.execute(
            "INSERT INTO control.active_metadata_revisions "
            "(source_id, revision, pinned) VALUES ('usage-upgrade', %s, false)",
            (revision,),
        )
        await connection.execute(
            "INSERT INTO control.source_profile_revisions "
            "(source_id, generation, manifest, secret_nonce, secret_ciphertext, "
            "metadata_revision) VALUES "
            "('usage-upgrade', 1, '{\"schema_version\": 2}'::jsonb, %s, %s, %s)",
            (b"n" * 12, b"c" * 17, revision),
        )
        await connection.execute(
            "INSERT INTO control.active_source_profiles "
            "(source_id, generation, enabled, state_version) "
            "VALUES ('usage-upgrade', 1, true, 1)"
        )
        await connection.execute(
            "INSERT INTO control.runtime_replicas "
            "(replica_id, heartbeat_interval_ms) VALUES ('usage-replica', 5000)"
        )
        await connection.execute(
            "INSERT INTO control.runtime_source_observations "
            "(replica_id, incarnation, source_id, source_health) "
            "VALUES ('usage-replica', 1, 'usage-upgrade', 'healthy')"
        )
        await connection.commit()
        authority_before = await authority_fingerprint(database.dsn)
        cursor = await connection.execute(
            "SELECT replica_id, incarnation, source_id, source_health "
            "FROM control.runtime_source_observations"
        )
        runtime_before = await cursor.fetchall()
    finally:
        await connection.close()

    apply_control_migrations(database)
    assert await authority_fingerprint(database.dsn) == authority_before

    connection = await AsyncConnection.connect(database.dsn)
    try:
        cursor = await connection.execute(
            "SELECT version, filename FROM control.schema_migrations ORDER BY version"
        )
        assert await cursor.fetchall() == [
            (1, "0001_baseline.sql"),
            (2, "0002_source_mutation_receipts.sql"),
            (3, "0003_runtime_replica_observations.sql"),
            (4, "0004_source_resource_and_gateway_usage.sql"),
        ]

        cursor = await connection.execute(
            "SELECT table_name, "
            "array_agg(column_name::text ORDER BY ordinal_position) "
            "FROM information_schema.columns WHERE table_schema = 'control' "
            "AND table_name IN ('source_resource_observations', "
            "'gateway_usage_rollups', 'gateway_usage_report_cursors') "
            "GROUP BY table_name ORDER BY table_name"
        )
        assert await cursor.fetchall() == [
            (
                "gateway_usage_report_cursors",
                [
                    "replica_id",
                    "incarnation",
                    "last_sequence",
                    "last_payload_hash",
                    "observed_at",
                    "fresh_until",
                ],
            ),
            (
                "gateway_usage_rollups",
                [
                    "source_id",
                    "budget_profile",
                    "metadata_revision",
                    "definition_revision",
                    "bucket_start",
                    "query_count",
                    "success_count",
                    "rejected_count",
                    "timeout_count",
                    "overloaded_count",
                    "cancelled_count",
                    "failed_count",
                    "queue_ms_sum",
                    "elapsed_ms_sum",
                    "returned_rows_sum",
                    "result_bytes_sum",
                    "truncated_count",
                    "observed_at",
                ],
            ),
            (
                "source_resource_observations",
                [
                    "source_id",
                    "metric",
                    "unit",
                    "method",
                    "definition_revision",
                    "value",
                    "metadata_revision",
                    "sample_bucket_start",
                    "observed_at",
                    "fresh_until",
                    "previous_value",
                    "previous_metadata_revision",
                    "previous_sample_bucket_start",
                    "previous_observed_at",
                    "previous_fresh_until",
                ],
            ),
        ]

        cursor = await connection.execute(
            "SELECT "
            "ARRAY["
            "has_table_privilege('query_man_control_writer', "
            "'control.source_resource_observations', 'SELECT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_resource_observations', 'INSERT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_resource_observations', 'UPDATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_resource_observations', 'DELETE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_resource_observations', 'TRUNCATE')], "
            "ARRAY["
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_rollups', 'SELECT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_rollups', 'INSERT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_rollups', 'UPDATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_rollups', 'DELETE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_rollups', 'TRUNCATE')], "
            "ARRAY["
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_report_cursors', 'SELECT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_report_cursors', 'INSERT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_report_cursors', 'UPDATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_report_cursors', 'DELETE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.gateway_usage_report_cursors', 'TRUNCATE')]"
        )
        assert await cursor.fetchone() == (
            [True, True, True, False, False],
            [True, True, True, True, False],
            [True, True, True, False, False],
        )

        with pytest.raises(errors.CheckViolation):
            await connection.execute(
                "INSERT INTO control.gateway_usage_rollups "
                "(source_id, budget_profile, metadata_revision, "
                "definition_revision, bucket_start, query_count, "
                "rejected_count, queue_ms_sum) VALUES "
                "('usage-upgrade', 'interactive', %s, %s, "
                "date_trunc('hour', clock_timestamp()), 1, 1, 1)",
                (revision, f"sha256:{'9' * 64}"),
            )
        await connection.rollback()

        cursor = await connection.execute(
            "SELECT replica_id, incarnation, source_id, source_health "
            "FROM control.runtime_source_observations"
        )
        assert await cursor.fetchall() == runtime_before
        first_contract = await _control_contract(connection)
    finally:
        await connection.close()

    apply_control_migrations(database)
    connection = await AsyncConnection.connect(database.dsn)
    try:
        assert await _control_contract(connection) == first_contract
    finally:
        await connection.close()


@pytest.mark.integration
async def test_failed_pending_migration_rolls_back_ddl_data_and_ledger(
    disposable_control_database_fixture: DisposableControlDatabase,
    tmp_path: Path,
) -> None:
    database = disposable_control_database_fixture
    failing_directory = tmp_path / "failing-control-migrations"
    shutil.copytree(_MIGRATION_DIRECTORY, failing_directory)
    (failing_directory / "0005_transaction_probe.sql").write_text(
        "CREATE TABLE control.failed_migration_probe (value integer NOT NULL);\n"
        "INSERT INTO control.failed_migration_probe VALUES (1);\n"
        "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot)\n"
        f"VALUES ('failed-migration', 'sha256:{'2' * 64}', '{{}}'::jsonb);\n"
        "SELECT 1 / 0;\n",
        encoding="utf-8",
    )
    before = await authority_fingerprint(database.dsn)

    with pytest.raises(subprocess.CalledProcessError):
        apply_control_migrations(database, failing_directory)

    assert await authority_fingerprint(database.dsn) == before
    connection = await AsyncConnection.connect(database.dsn)
    try:
        cursor = await connection.execute(
            "SELECT pg_catalog.to_regclass('control.failed_migration_probe'), "
            "EXISTS (SELECT 1 FROM control.metadata_snapshots "
            "WHERE source_id = 'failed-migration'), "
            "array_agg(version ORDER BY version) FROM control.schema_migrations"
        )
        assert await cursor.fetchone() == (None, False, [1, 2, 3, 4])
    finally:
        await connection.close()

    apply_control_migrations(database)
    assert await authority_fingerprint(database.dsn) == before


@pytest.mark.integration
async def test_concurrent_pending_v2_applies_serialize(
    v1_control_database_fixture: DisposableControlDatabase,
    tmp_path: Path,
) -> None:
    database = v1_control_database_fixture
    concurrent_directory = tmp_path / "concurrent-control-migrations"
    shutil.copytree(_copy_v2_migrations(tmp_path), concurrent_directory)
    v2_path = concurrent_directory / "0002_source_mutation_receipts.sql"
    v2_path.write_text(
        "SELECT pg_catalog.pg_sleep(1);\n" + v2_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    ready = threading.Barrier(2)

    def apply_when_both_workers_are_ready() -> None:
        ready.wait(timeout=10)
        apply_control_migrations(database, concurrent_directory)

    await asyncio.gather(
        asyncio.to_thread(apply_when_both_workers_are_ready),
        asyncio.to_thread(apply_when_both_workers_are_ready),
    )

    connection = await AsyncConnection.connect(database.dsn)
    try:
        cursor = await connection.execute(
            "SELECT version, filename FROM control.schema_migrations ORDER BY version"
        )
        assert await cursor.fetchall() == [
            (1, "0001_baseline.sql"),
            (2, "0002_source_mutation_receipts.sql"),
        ]
        cursor = await connection.execute(
            "SELECT count(*) FROM pg_catalog.pg_class AS relation_row "
            "JOIN pg_catalog.pg_namespace AS namespace_row "
            "ON namespace_row.oid = relation_row.relnamespace "
            "WHERE namespace_row.nspname = 'control' "
            "AND relation_row.relname = 'source_mutation_receipts' "
            "AND relation_row.relkind = 'r'"
        )
        assert await cursor.fetchone() == (1,)
    finally:
        await connection.close()


@pytest.mark.integration
async def test_old_checkout_cannot_reconcile_after_newer_ledger_commit(
    v1_control_database_fixture: DisposableControlDatabase,
    tmp_path: Path,
) -> None:
    database = v1_control_database_fixture
    old_directory = _copy_v1_migrations(tmp_path / "old-checkout")
    _pause_before_security_reconciliation(old_directory)
    connection = await AsyncConnection.connect(database.dsn)
    old_apply = asyncio.create_task(
        asyncio.to_thread(apply_control_migrations, database, old_directory)
    )
    try:
        async with asyncio.timeout(30):
            while True:
                if old_apply.done():
                    await old_apply
                cursor = await connection.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_class AS relation "
                    "JOIN pg_catalog.pg_namespace AS namespace "
                    "ON namespace.oid = relation.relnamespace "
                    "WHERE namespace.nspname = %s AND relation.relname = %s)",
                    ("control", "mixed_checkout_barrier"),
                )
                if await cursor.fetchone() == (True,):
                    break
                await connection.rollback()
                await asyncio.sleep(0.05)

        await connection.rollback()
        await asyncio.to_thread(apply_control_migrations, database)
        await connection.execute(
            "UPDATE control.mixed_checkout_barrier SET released = true"
        )
        await connection.commit()

        with pytest.raises(subprocess.CalledProcessError) as failure:
            await old_apply
        output = f"{failure.value.stdout}\n{failure.value.stderr}"
        assert (
            "Control migration ledger changed before security reconciliation."
            in output
        )

        cursor = await connection.execute(
            "SELECT array_agg(version ORDER BY version), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'SELECT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'INSERT'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts_event_id_seq', 'USAGE') "
            "FROM control.schema_migrations"
        )
        assert await cursor.fetchone() == ([1, 2, 3, 4], True, True, True)
    finally:
        await connection.rollback()
        try:
            await connection.execute(
                "UPDATE control.mixed_checkout_barrier SET released = true"
            )
            await connection.commit()
        except errors.UndefinedTable:
            await connection.rollback()
        await asyncio.gather(old_apply, return_exceptions=True)
        await connection.close()


@pytest.mark.integration
async def test_v2_is_additive_for_rolling_writer_and_receipt_acl_is_append_only(
    v1_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = v1_control_database_fixture
    writer = await AsyncConnection.connect(database.dsn)
    try:
        await writer.execute("SET ROLE query_man_control_writer")
        await writer.execute(
            "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
            f"VALUES ('rolling-before', 'sha256:{'3' * 64}', '{{}}'::jsonb)"
        )
        await writer.commit()

        apply_control_migrations(database)

        await writer.execute(
            "INSERT INTO control.metadata_snapshots (source_id, revision, snapshot) "
            f"VALUES ('rolling-after', 'sha256:{'4' * 64}', '{{}}'::jsonb)"
        )
        cursor = await writer.execute(
            "SELECT source_id FROM control.metadata_snapshots "
            "WHERE source_id LIKE 'rolling-%' ORDER BY source_id"
        )
        assert await cursor.fetchall() == [("rolling-after",), ("rolling-before",)]
        cursor = await writer.execute(
            "INSERT INTO control.source_mutation_receipts "
            "(idempotency_key, request_hash, operation, source_id, actor, reason, "
            "expected_generation, expected_state_version, resulting_generation, "
            "resulting_state_version, outcome, http_status, result) VALUES "
            "('00000000-0000-0000-0000-000000000001', %s, 'publish_source', "
            "'rolling-after', 'MigrationTest', 'rolling-upgrade', 0, 0, 1, 1, "
            "'succeeded', 200, %s) RETURNING event_id",
            (
                f"hmac-sha256:{'5' * 64}",
                Jsonb(
                    {
                        "status": "published",
                        "source_id": "rolling-after",
                        "generation": 1,
                        "metadata_revision": f"sha256:{'4' * 64}",
                        "quality_level": "L0",
                    }
                ),
            ),
        )
        event_row = await cursor.fetchone()
        assert event_row is not None
        event_id = int(event_row[0])
        await writer.commit()

        cursor = await writer.execute(
            "SELECT outcome, result FROM control.source_mutation_receipts "
            "WHERE event_id = %s",
            (event_id,),
        )
        assert await cursor.fetchone() == (
            "succeeded",
            {
                "status": "published",
                "source_id": "rolling-after",
                "generation": 1,
                "metadata_revision": f"sha256:{'4' * 64}",
                "quality_level": "L0",
            },
        )
        with pytest.raises(errors.InsufficientPrivilege):
            await writer.execute(
                "UPDATE control.source_mutation_receipts SET reason = 'forbidden' "
                "WHERE event_id = %s",
                (event_id,),
            )
        await writer.rollback()
        with pytest.raises(errors.InsufficientPrivilege):
            await writer.execute(
                "DELETE FROM control.source_mutation_receipts WHERE event_id = %s",
                (event_id,),
            )
        await writer.rollback()
    finally:
        await writer.close()

    connection = await AsyncConnection.connect(database.dsn)
    try:
        cursor = await connection.execute(
            "SELECT "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'SELECT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'INSERT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'UPDATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'DELETE'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts_event_id_seq', 'USAGE'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts_event_id_seq', 'SELECT'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts_event_id_seq', 'UPDATE')"
        )
        assert await cursor.fetchone() == (True, True, False, False, True, False, False)
        with pytest.raises(errors.RaiseException):
            await connection.execute(
                "UPDATE control.source_mutation_receipts SET reason = 'owner-forbidden' "
                "WHERE event_id = %s",
                (event_id,),
            )
        await connection.rollback()
        with pytest.raises(errors.RaiseException):
            await connection.execute(
                "DELETE FROM control.source_mutation_receipts WHERE event_id = %s",
                (event_id,),
            )
        await connection.rollback()
        cursor = await connection.execute(
            "SELECT count(*) FROM control.source_mutation_receipts WHERE event_id = %s",
            (event_id,),
        )
        assert await cursor.fetchone() == (1,)
    finally:
        await connection.close()


@pytest.mark.integration
async def test_security_reconciliation_removes_stale_writer_overgrants(
    disposable_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = disposable_control_database_fixture
    inherited_role = f"query_man_acl_probe_{database.name[-32:]}"
    connection = await AsyncConnection.connect(database.dsn)
    try:
        await connection.execute(
            "CREATE TABLE control.security_reconciliation_probe "
            "(value integer NOT NULL)"
        )
        await connection.execute(
            "CREATE SEQUENCE control.security_reconciliation_probe_seq"
        )
        await connection.execute(
            "CREATE FUNCTION control.security_reconciliation_probe() "
            "RETURNS integer LANGUAGE sql AS 'SELECT 1'"
        )
        await connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(inherited_role))
        )
        await connection.execute(
            sql.SQL("GRANT {} TO query_man_control_writer").format(
                sql.Identifier(inherited_role)
            )
        )
        await connection.execute(
            sql.SQL(
                "GRANT ALL PRIVILEGES ON DATABASE {} TO query_man_control_writer"
            ).format(sql.Identifier(database.name))
        )
        await connection.execute(
            sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO PUBLIC").format(
                sql.Identifier(database.name)
            )
        )
        await connection.execute(
            "GRANT ALL PRIVILEGES ON SCHEMA control TO query_man_control_writer"
        )
        await connection.execute("GRANT ALL PRIVILEGES ON SCHEMA control TO PUBLIC")
        await connection.execute(
            "GRANT ALL PRIVILEGES ON TABLE "
            "control.security_reconciliation_probe, "
            "control.source_mutation_receipts TO query_man_control_writer"
        )
        await connection.execute(
            "GRANT ALL PRIVILEGES ON TABLE "
            "control.security_reconciliation_probe TO PUBLIC"
        )
        await connection.execute(
            "GRANT SELECT, INSERT ON TABLE control.source_mutation_receipts "
            "TO PUBLIC"
        )
        await connection.execute(
            "GRANT SELECT, INSERT ON TABLE control.source_mutation_receipts "
            "TO query_man_control_writer WITH GRANT OPTION"
        )
        await connection.execute(
            "GRANT ALL PRIVILEGES ON SEQUENCE "
            "control.security_reconciliation_probe_seq, "
            "control.source_mutation_receipts_event_id_seq "
            "TO query_man_control_writer"
        )
        await connection.execute(
            "GRANT ALL PRIVILEGES ON SEQUENCE "
            "control.security_reconciliation_probe_seq TO PUBLIC"
        )
        await connection.execute(
            "GRANT USAGE ON SEQUENCE "
            "control.source_mutation_receipts_event_id_seq TO PUBLIC"
        )
        await connection.execute(
            "GRANT USAGE ON SEQUENCE "
            "control.source_mutation_receipts_event_id_seq "
            "TO query_man_control_writer WITH GRANT OPTION"
        )
        await connection.execute(
            "GRANT ALL PRIVILEGES ON FUNCTION "
            "control.security_reconciliation_probe() TO query_man_control_writer"
        )
        await connection.commit()

        cursor = await connection.execute(
            "SELECT "
            "has_database_privilege('query_man_control_writer', "
            "current_database(), 'CREATE'), "
            "has_database_privilege('query_man_control_writer', "
            "current_database(), 'TEMPORARY'), "
            "has_schema_privilege('query_man_control_writer', 'control', 'CREATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'UPDATE'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe_seq', 'UPDATE'), "
            "has_function_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe()', 'EXECUTE'), "
            "pg_has_role('query_man_control_writer', %s, 'MEMBER')",
            (inherited_role,),
        )
        assert await cursor.fetchone() == (True, True, True, True, True, True, True)

        apply_control_migrations(database)

        cursor = await connection.execute(
            "SELECT "
            "ARRAY["
            "has_database_privilege('query_man_control_writer', "
            "current_database(), 'CONNECT'), "
            "has_database_privilege('query_man_control_writer', "
            "current_database(), 'CREATE'), "
            "has_database_privilege('query_man_control_writer', "
            "current_database(), 'TEMPORARY')], "
            "ARRAY["
            "has_schema_privilege('query_man_control_writer', 'control', 'USAGE'), "
            "has_schema_privilege('query_man_control_writer', 'control', 'CREATE')], "
            "ARRAY["
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'SELECT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'INSERT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'UPDATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'DELETE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'TRUNCATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'REFERENCES'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'TRIGGER'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe', 'MAINTAIN')], "
            "ARRAY["
            "has_sequence_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe_seq', 'USAGE'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe_seq', 'SELECT'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe_seq', 'UPDATE')], "
            "has_function_privilege('query_man_control_writer', "
            "'control.security_reconciliation_probe()', 'EXECUTE'), "
            "ARRAY["
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'SELECT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'INSERT'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'UPDATE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'DELETE'), "
            "has_table_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts', 'TRUNCATE')], "
            "ARRAY["
            "has_sequence_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts_event_id_seq', 'USAGE'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts_event_id_seq', 'SELECT'), "
            "has_sequence_privilege('query_man_control_writer', "
            "'control.source_mutation_receipts_event_id_seq', 'UPDATE')], "
            "has_table_privilege('query_man_control_writer', "
            "'control.schema_migrations', 'SELECT'), "
            "pg_has_role('query_man_control_writer', %s, 'MEMBER')",
            (inherited_role,),
        )
        assert await cursor.fetchone() == (
            [True, False, False],
            [True, False],
            [False] * 8,
            [False, False, False],
            False,
            [True, True, False, False, False],
            [True, False, False],
            False,
            False,
        )
        cursor = await connection.execute(
            "WITH writer AS ("
            "SELECT oid FROM pg_catalog.pg_roles "
            "WHERE rolname = 'query_man_control_writer'"
            "), control_acl AS ("
            "SELECT acl.grantee, acl.is_grantable FROM pg_catalog.pg_database AS d "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce("
            "d.datacl, pg_catalog.acldefault('d', d.datdba))) AS acl "
            "WHERE d.datname = current_database() UNION ALL "
            "SELECT acl.grantee, acl.is_grantable FROM pg_catalog.pg_namespace AS n "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce("
            "n.nspacl, pg_catalog.acldefault('n', n.nspowner))) AS acl "
            "WHERE n.nspname = 'control' UNION ALL "
            "SELECT acl.grantee, acl.is_grantable FROM pg_catalog.pg_class AS c "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) AS acl "
            "WHERE n.nspname = 'control' UNION ALL "
            "SELECT acl.grantee, acl.is_grantable FROM pg_catalog.pg_attribute AS a "
            "JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(a.attacl) AS acl "
            "WHERE n.nspname = 'control' AND a.attnum > 0 "
            "AND NOT a.attisdropped UNION ALL "
            "SELECT acl.grantee, acl.is_grantable FROM pg_catalog.pg_proc AS p "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(coalesce("
            "p.proacl, pg_catalog.acldefault('f', p.proowner))) AS acl "
            "WHERE n.nspname = 'control'"
            ") SELECT count(*) FILTER (WHERE control_acl.grantee = 0), "
            "count(*) FILTER (WHERE control_acl.grantee = writer.oid "
            "AND control_acl.is_grantable) FROM control_acl CROSS JOIN writer"
        )
        assert await cursor.fetchone() == (0, 0)
    finally:
        await connection.rollback()
        cursor = await connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)",
            (inherited_role,),
        )
        if await cursor.fetchone() == (True,):
            await connection.execute(
                sql.SQL("REVOKE {} FROM query_man_control_writer").format(
                    sql.Identifier(inherited_role)
                )
            )
            await connection.execute(
                sql.SQL("DROP ROLE {}").format(sql.Identifier(inherited_role))
            )
            await connection.commit()
        await connection.close()


@pytest.mark.integration
async def test_security_reconciliation_fails_closed_on_writer_owned_objects(
    disposable_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = disposable_control_database_fixture
    connection = await AsyncConnection.connect(database.dsn)
    try:
        await connection.execute(
            "ALTER TABLE control.metadata_snapshots "
            "OWNER TO query_man_control_writer"
        )
        await connection.commit()

        with pytest.raises(subprocess.CalledProcessError) as failure:
            apply_control_migrations(database)
        output = f"{failure.value.stdout}\n{failure.value.stderr}"
        assert "Control writer must not own the database or control objects" in output

        cursor = await connection.execute(
            "SELECT owner.rolname, array_agg(migration.version ORDER BY migration.version) "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "JOIN pg_catalog.pg_roles AS owner ON owner.oid = relation.relowner "
            "CROSS JOIN control.schema_migrations AS migration "
            "WHERE namespace.nspname = 'control' "
            "AND relation.relname = 'metadata_snapshots' "
            "GROUP BY owner.rolname"
        )
        assert await cursor.fetchone() == (
            "query_man_control_writer",
            [1, 2, 3, 4],
        )
    finally:
        await connection.close()


@pytest.mark.integration
async def test_security_reconciliation_fails_closed_on_delegated_acl(
    disposable_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = disposable_control_database_fixture
    grantor_role = f"query_man_acl_grantor_{database.name[-32:]}"
    connection = await AsyncConnection.connect(database.dsn)
    try:
        await connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(grantor_role))
        )
        await connection.execute(
            sql.SQL(
                "GRANT SELECT ON control.source_mutation_receipts "
                "TO {} WITH GRANT OPTION"
            ).format(sql.Identifier(grantor_role))
        )
        await connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA control TO {}").format(
                sql.Identifier(grantor_role)
            )
        )
        await connection.execute(
            sql.SQL("SET ROLE {}").format(sql.Identifier(grantor_role))
        )
        await connection.execute(
            "GRANT SELECT ON control.source_mutation_receipts TO PUBLIC"
        )
        await connection.execute(
            "GRANT SELECT ON control.source_mutation_receipts "
            "TO query_man_control_writer WITH GRANT OPTION"
        )
        await connection.execute("RESET ROLE")
        await connection.commit()

        with pytest.raises(subprocess.CalledProcessError) as failure:
            apply_control_migrations(database)
        output = f"{failure.value.stdout}\n{failure.value.stderr}"
        assert "Control ACL retains an unexpected grantee or grant option" in output

        cursor = await connection.execute(
            "SELECT "
            "count(*) FILTER (WHERE acl.grantee = 0 "
            "AND acl.privilege_type = 'SELECT'), "
            "count(*) FILTER (WHERE acl.grantee = writer.oid "
            "AND acl.grantor = grantor.oid AND acl.is_grantable) "
            "FROM pg_catalog.pg_class AS relation "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = relation.relnamespace "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(relation.relacl) AS acl "
            "CROSS JOIN pg_catalog.pg_roles AS writer "
            "CROSS JOIN pg_catalog.pg_roles AS grantor "
            "WHERE namespace.nspname = 'control' "
            "AND relation.relname = 'source_mutation_receipts' "
            "AND writer.rolname = 'query_man_control_writer' "
            "AND grantor.rolname = %s",
            (grantor_role,),
        )
        assert await cursor.fetchone() == (1, 1)
    finally:
        await connection.rollback()
        await connection.execute("RESET ROLE")
        cursor = await connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)",
            (grantor_role,),
        )
        if await cursor.fetchone() == (True,):
            await connection.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA control FROM {}").format(
                    sql.Identifier(grantor_role)
                )
            )
            await connection.execute(
                sql.SQL(
                    "REVOKE ALL PRIVILEGES ON control.source_mutation_receipts "
                    "FROM {} CASCADE"
                ).format(sql.Identifier(grantor_role))
            )
            await connection.execute(
                sql.SQL("DROP ROLE {}").format(sql.Identifier(grantor_role))
            )
            await connection.commit()
        await connection.close()


@pytest.mark.integration
async def test_security_reconciliation_handles_delegated_membership_safely(
    disposable_control_database_fixture: DisposableControlDatabase,
) -> None:
    database = disposable_control_database_fixture
    suffix = database.name[-24:]
    parent_role = f"query_man_parent_{suffix}"
    grantor_role = f"query_man_grantor_{suffix}"
    connection = await AsyncConnection.connect(database.dsn)
    try:
        await connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(parent_role))
        )
        await connection.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(grantor_role))
        )
        await connection.execute(
            sql.SQL("GRANT {} TO {} WITH ADMIN OPTION").format(
                sql.Identifier(parent_role),
                sql.Identifier(grantor_role),
            )
        )
        await connection.execute(
            sql.SQL("SET ROLE {}").format(sql.Identifier(grantor_role))
        )
        await connection.execute(
            sql.SQL("GRANT {} TO query_man_control_writer").format(
                sql.Identifier(parent_role)
            )
        )
        await connection.execute("RESET ROLE")
        await connection.commit()

        failure: subprocess.CalledProcessError | None = None
        try:
            apply_control_migrations(database)
        except subprocess.CalledProcessError as error:
            failure = error

        cursor = await connection.execute(
            "SELECT EXISTS ("
            "SELECT 1 FROM pg_catalog.pg_auth_members AS membership "
            "JOIN pg_catalog.pg_roles AS parent "
            "ON parent.oid = membership.roleid "
            "JOIN pg_catalog.pg_roles AS member "
            "ON member.oid = membership.member "
            "WHERE parent.rolname = %s "
            "AND member.rolname = 'query_man_control_writer')",
            (parent_role,),
        )
        membership_remains = await cursor.fetchone() == (True,)
        if failure is None:
            assert not membership_remains
        else:
            output = f"{failure.stdout}\n{failure.stderr}"
            assert "Control writer retains an inherited role membership" in output
            assert membership_remains
    finally:
        await connection.rollback()
        cursor = await connection.execute(
            "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname IN (%s, %s)",
            (parent_role, grantor_role),
        )
        if await cursor.fetchone() == (2,):
            await connection.execute(
                sql.SQL("SET ROLE {}").format(sql.Identifier(grantor_role))
            )
            await connection.execute(
                sql.SQL("REVOKE {} FROM query_man_control_writer").format(
                    sql.Identifier(parent_role)
                )
            )
            await connection.execute("RESET ROLE")
            await connection.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(parent_role),
                    sql.Identifier(grantor_role),
                )
            )
            await connection.execute(
                sql.SQL("DROP ROLE {}, {}").format(
                    sql.Identifier(grantor_role),
                    sql.Identifier(parent_role),
                )
            )
            await connection.commit()
        await connection.close()
