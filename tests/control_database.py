from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from tests.helpers import ROOT_DIRECTORY

_AUTHORITY_TABLES = (
    "metadata_snapshots",
    "active_metadata_revisions",
    "source_profile_revisions",
    "active_source_profiles",
    "verified_query_contracts",
    "source_mutation_receipts",
)
CONTROL_TABLES = (
    "schema_migrations",
    *_AUTHORITY_TABLES,
    "runtime_replicas",
    "runtime_source_observations",
    "source_resource_observations",
    "source_resource_observation_attempts",
    "gateway_usage_rollups",
    "gateway_usage_report_cursors",
)
_TEST_DATABASE_PREFIX = "query_man_control_test_"
_TEST_DATABASE_NAME = re.compile(rf"^{_TEST_DATABASE_PREFIX}[0-9a-f]{{32}}$")
_CONTROL_DATABASE_SERVICES = {
    "postgres",
    "postgres-control-recovery-source",
}


@dataclass(frozen=True)
class DisposableControlDatabase:
    name: str
    dsn: str = field(repr=False)
    compose_service: str = "postgres"


def postgres_environment() -> dict[str, str] | None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
    if any(not os.environ.get(name) for name in required):
        return None
    return {name: os.environ[name] for name in required} | {
        "POSTGRES_PORT": os.environ.get("POSTGRES_PORT", "5432")
    }


def postgres_dsn(environment: dict[str, str], database: str) -> str:
    return make_conninfo(
        host="127.0.0.1",
        port=environment["POSTGRES_PORT"],
        dbname=database,
        user=environment["POSTGRES_USER"],
        password=environment["POSTGRES_PASSWORD"],
        sslmode="disable",
    )


def apply_control_migrations(
    database: DisposableControlDatabase,
    migration_directory: Path | None = None,
) -> None:
    if _TEST_DATABASE_NAME.fullmatch(database.name) is None:
        raise ValueError("Refusing to migrate an unmanaged test database")
    if database.compose_service not in _CONTROL_DATABASE_SERVICES:
        raise ValueError("Refusing to use an unmanaged PostgreSQL service")
    if migration_directory is None:
        migration_directory = (
            ROOT_DIRECTORY / "docker" / "postgres" / "init" / "control-migrations"
        )
    migration_directory = migration_directory.resolve(strict=True)
    if not migration_directory.is_dir():
        raise ValueError("Control migration path must be a directory")
    for required_name in ("apply.sh", "reconcile-security.sql"):
        if not (migration_directory / required_name).is_file():
            raise ValueError(f"Control migration directory is missing {required_name}")
    container_directory = (
        f"/tmp/query-man-control-migrations-{database.name}-{uuid.uuid4().hex}"
    )
    compose = ["docker", "compose"]
    run_options = {
        "cwd": ROOT_DIRECTORY,
        "check": True,
        "capture_output": True,
        "text": True,
    }
    created = False
    try:
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                database.compose_service,
                "mkdir",
                "--mode=700",
                "--",
                container_directory,
            ],
            **run_options,
        )
        created = True
        subprocess.run(
            [
                *compose,
                "cp",
                f"{migration_directory}/.",
                f"{database.compose_service}:{container_directory}",
            ],
            **run_options,
        )
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "--env",
                f"PGDATABASE={database.name}",
                "--env",
                f"PGUSER={os.environ['POSTGRES_USER']}",
                database.compose_service,
                "bash",
                f"{container_directory}/apply.sh",
            ],
            **run_options,
        )
    finally:
        if created:
            subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    database.compose_service,
                    "rm",
                    "-r",
                    "--",
                    container_directory,
                ],
                **run_options,
            )


def restore_control_backup(
    source: DisposableControlDatabase,
    target: DisposableControlDatabase,
    archive_path: Path,
) -> str:
    for database in (source, target):
        if _TEST_DATABASE_NAME.fullmatch(database.name) is None:
            raise ValueError("Refusing to restore an unmanaged test database")
        if database.compose_service not in _CONTROL_DATABASE_SERVICES:
            raise ValueError("Refusing to use an unmanaged PostgreSQL service")
    if (source.compose_service, source.name) == (
        target.compose_service,
        target.name,
    ):
        raise ValueError("Control backup source and restore target must differ")
    if (
        source.compose_service != "postgres-control-recovery-source"
        or target.compose_service != "postgres"
    ):
        raise ValueError("Control recovery must cross the isolated PostgreSQL services")
    if archive_path.exists() or not archive_path.parent.is_dir():
        raise ValueError("Control backup archive path must be new")

    archive_path = archive_path.resolve()
    source_stage = f"/tmp/query-man-control-source-{uuid.uuid4().hex}"
    target_stage = f"/tmp/query-man-control-target-{uuid.uuid4().hex}"
    source_archive = f"{source_stage}/control.dump"
    target_archive = f"{target_stage}/control.dump"
    compose = ["docker", "compose"]
    run_options = {
        "cwd": ROOT_DIRECTORY,
        "check": True,
        "capture_output": True,
        "text": True,
    }
    try:
        for service, path in (
            (source.compose_service, source_stage),
            (target.compose_service, target_stage),
        ):
            subprocess.run(
                [*compose, "exec", "-T", service, "mkdir", "--mode=700", "--", path],
                **run_options,
            )
        target_is_empty = subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "--env",
                f"PGUSER={os.environ['POSTGRES_USER']}",
                target.compose_service,
                "psql",
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                f"--dbname={target.name}",
                "--command=SELECT pg_catalog.to_regnamespace('control') IS NULL",
            ],
            **run_options,
        ).stdout.strip()
        if target_is_empty != "t":
            raise ValueError("Control restore target must not contain a control schema")
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "--env",
                f"PGUSER={os.environ['POSTGRES_USER']}",
                source.compose_service,
                "pg_dump",
                "--format=custom",
                "--schema=control",
                "--no-owner",
                "--no-privileges",
                f"--dbname={source.name}",
                f"--file={source_archive}",
            ],
            **run_options,
        )
        archive_path.touch(mode=0o600, exist_ok=False)
        with archive_path.open("wb") as archive:
            subprocess.run(
                [
                    *compose,
                    "exec",
                    "-T",
                    source.compose_service,
                    "cat",
                    "--",
                    source_archive,
                ],
                cwd=ROOT_DIRECTORY,
                check=True,
                stdout=archive,
                stderr=subprocess.PIPE,
            )
        subprocess.run(
            [
                *compose,
                "cp",
                str(archive_path),
                f"{target.compose_service}:{target_archive}",
            ],
            **run_options,
        )
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "--env",
                f"PGUSER={os.environ['POSTGRES_USER']}",
                target.compose_service,
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
                "--single-transaction",
                f"--dbname={target.name}",
                target_archive,
            ],
            **run_options,
        )
        apply_control_migrations(target)
        apply_control_migrations(target)
        with archive_path.open("rb") as archive:
            digest = hashlib.file_digest(archive, "sha256").hexdigest()
        return f"sha256:{digest}"
    finally:
        archive_path.unlink(missing_ok=True)
        for service, path in (
            (source.compose_service, source_stage),
            (target.compose_service, target_stage),
        ):
            subprocess.run(
                [*compose, "exec", "-T", service, "rm", "-r", "-f", "--", path],
                cwd=ROOT_DIRECTORY,
                check=False,
                capture_output=True,
                text=True,
            )


async def authority_fingerprint(dsn: str) -> tuple[tuple[str, int, str], ...]:
    connection = await AsyncConnection.connect(dsn)
    try:
        fingerprints: list[tuple[str, int, str]] = []
        for table_name in _AUTHORITY_TABLES:
            cursor = await connection.execute(
                "SELECT pg_catalog.to_regclass(%s)",
                (f"control.{table_name}",),
            )
            if await cursor.fetchone() == (None,):
                fingerprints.append((table_name, -1, "missing"))
                continue
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT count(*), md5(coalesce(string_agg(to_jsonb(row_value)::text, '' "
                    "ORDER BY to_jsonb(row_value)::text), '')) FROM control.{} AS row_value"
                ).format(sql.Identifier(table_name))
            )
            row = await cursor.fetchone()
            assert row is not None
            fingerprints.append((table_name, int(row[0]), str(row[1])))
        return tuple(fingerprints)
    finally:
        await connection.close()


async def control_table_fingerprint(dsn: str) -> tuple[tuple[str, int, str], ...]:
    connection = await AsyncConnection.connect(dsn)
    try:
        await connection.execute("SET TIME ZONE 'UTC'")
        fingerprints: list[tuple[str, int, str]] = []
        for table_name in CONTROL_TABLES:
            cursor = await connection.execute(
                sql.SQL(
                    "SELECT count(*), coalesce(string_agg(to_jsonb(row_value)::text, "
                    "E'\\n' ORDER BY to_jsonb(row_value)::text COLLATE \"C\"), '') "
                    "FROM control.{} AS row_value"
                ).format(sql.Identifier(table_name))
            )
            row = await cursor.fetchone()
            assert row is not None
            digest = hashlib.sha256(str(row[1]).encode("utf-8")).hexdigest()
            fingerprints.append((table_name, int(row[0]), f"sha256:{digest}"))
        return tuple(fingerprints)
    finally:
        await connection.close()


@asynccontextmanager
async def disposable_control_database(
    environment: dict[str, str],
    migration_directory: Path | None = None,
    *,
    compose_service: str = "postgres",
    apply_migrations_on_create: bool = True,
) -> AsyncIterator[DisposableControlDatabase]:
    if compose_service not in _CONTROL_DATABASE_SERVICES:
        raise ValueError("Refusing to use an unmanaged PostgreSQL service")
    connection_environment = dict(environment)
    if compose_service == "postgres-control-recovery-source":
        connection_environment["POSTGRES_PORT"] = os.environ.get(
            "QUERY_MAN_RECOVERY_POSTGRES_PORT",
            "55432",
        )
    database_name = f"{_TEST_DATABASE_PREFIX}{uuid.uuid4().hex}"
    if len(database_name) > 63:
        raise AssertionError("Generated test database name exceeds PostgreSQL's identifier limit")
    maintenance_dsn = postgres_dsn(connection_environment, "postgres")
    database = DisposableControlDatabase(
        database_name,
        postgres_dsn(connection_environment, database_name),
        compose_service,
    )
    maintenance = await AsyncConnection.connect(maintenance_dsn, autocommit=True)
    created = False
    leaked_connections = 0
    try:
        await maintenance.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
                sql.Identifier(database_name),
                sql.Identifier(connection_environment["POSTGRES_USER"]),
            )
        )
        created = True
        if apply_migrations_on_create:
            apply_control_migrations(database, migration_directory)
        yield database
    finally:
        try:
            if created:
                for _ in range(50):
                    cursor = await maintenance.execute(
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity WHERE datname = %s",
                        (database_name,),
                    )
                    row = await cursor.fetchone()
                    leaked_connections = 0 if row is None else int(row[0])
                    if leaked_connections == 0:
                        break
                    await asyncio.sleep(0.02)
                drop_statement = sql.SQL("DROP DATABASE {}{}").format(
                    sql.Identifier(database_name),
                    sql.SQL(" WITH (FORCE)") if leaked_connections else sql.SQL(""),
                )
                await maintenance.execute(drop_statement)
                cursor = await maintenance.execute(
                    "SELECT count(*) FROM pg_catalog.pg_database WHERE datname = %s",
                    (database_name,),
                )
                assert await cursor.fetchone() == (0,)
        finally:
            await maintenance.close()
        assert leaked_connections == 0, (
            f"Disposable Control DB leaked {leaked_connections} connection(s)"
        )
