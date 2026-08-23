from __future__ import annotations

import asyncio
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
_TEST_DATABASE_PREFIX = "query_man_control_test_"
_TEST_DATABASE_NAME = re.compile(rf"^{_TEST_DATABASE_PREFIX}[0-9a-f]{{32}}$")


@dataclass(frozen=True)
class DisposableControlDatabase:
    name: str
    dsn: str = field(repr=False)


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
                "postgres",
                "mkdir",
                "--mode=700",
                "--",
                container_directory,
            ],
            **run_options,
        )
        created = True
        subprocess.run(
            [*compose, "cp", f"{migration_directory}/.", f"postgres:{container_directory}"],
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
                "postgres",
                "bash",
                f"{container_directory}/apply.sh",
            ],
            **run_options,
        )
    finally:
        if created:
            subprocess.run(
                [*compose, "exec", "-T", "postgres", "rm", "-r", "--", container_directory],
                **run_options,
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


@asynccontextmanager
async def disposable_control_database(
    environment: dict[str, str],
    migration_directory: Path | None = None,
) -> AsyncIterator[DisposableControlDatabase]:
    database_name = f"{_TEST_DATABASE_PREFIX}{uuid.uuid4().hex}"
    if len(database_name) > 63:
        raise AssertionError("Generated test database name exceeds PostgreSQL's identifier limit")
    maintenance_dsn = postgres_dsn(environment, "postgres")
    database = DisposableControlDatabase(
        database_name,
        postgres_dsn(environment, database_name),
    )
    maintenance = await AsyncConnection.connect(maintenance_dsn, autocommit=True)
    created = False
    leaked_connections = 0
    try:
        await maintenance.execute(
            sql.SQL("CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0").format(
                sql.Identifier(database_name),
                sql.Identifier(environment["POSTGRES_USER"]),
            )
        )
        created = True
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
