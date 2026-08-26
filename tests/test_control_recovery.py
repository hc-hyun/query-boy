from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from psycopg import AsyncConnection, sql
from psycopg.conninfo import make_conninfo

from query_man.access import AccessPolicy
from query_man.managed.runtime import build_app
from query_man.managed.secrets import SecretDecryptionError, SourceSecretCipher
from query_man.managed.source_admin import (
    ControlGatewayUsageWriter,
    ControlReplicaObservationWriter,
    ControlResourceObservationWriter,
    GatewayUsageDelta,
    ReplicaSourceObservation,
    ResourceObservationSample,
)
from query_man.managed.source_store import PostgresSourceStore
from query_man.runtime_config import RuntimeConfig
from query_man.verified import create_result_hash
from tests.control_database import (
    CONTROL_TABLES,
    DisposableControlDatabase,
    control_table_fingerprint,
    disposable_control_database,
    postgres_environment,
    restore_control_backup,
)
from tests.helpers import ROOT_DIRECTORY

_SOURCE_ID = "support-tickets"
_REPLICA_IDS = ("control-recovery-a", "control-recovery-b")
_ADMIN_TOKEN = "recovery-admin-token-value-with-at-least-32-characters"
_QUERY_TOKEN = "recovery-query-token-value-with-at-least-32-characters"
_ADMIN_HEADERS = {"Authorization": f"Bearer {_ADMIN_TOKEN}"}
_QUERY_HEADERS = {"Authorization": f"Bearer {_QUERY_TOKEN}"}
_RECOVERY_SERVICE = "postgres-control-recovery-source"
_LOGIN_PREFIX = "qm_ctrl_recovery_"
_LOGIN_PATTERN = re.compile(r"^qm_ctrl_recovery_[0-9a-f]{24}$")
_RESOURCE_DEFINITION = "sha256:" + "1" * 64
_GATEWAY_DEFINITION = "sha256:" + "2" * 64
_AGED_BUDGET_PROFILE = "recovery_aged"


@dataclass(frozen=True)
class _SeededAuthority:
    generation: int
    state_version: int
    metadata_revision: str
    replay_headers: dict[str, str]
    replay_body: dict[str, object]
    replay_receipt: dict[str, object]
    replica_incarnations: dict[str, int]


def _access_policy(tmp_path: Path) -> AccessPolicy:
    policy_path = tmp_path / "recovery-access.yaml"
    policy_path.write_text(
        """
version: 2
callers:
  - caller_id: recovery-query
    tenant_id: engineering
    token_env: RECOVERY_QUERY_TOKEN
  - caller_id: recovery-admin
    tenant_id: operations
    token_env: RECOVERY_ADMIN_TOKEN
    operator: true
""".strip(),
        encoding="utf-8",
    )
    return AccessPolicy.load(
        policy_path,
        {
            "RECOVERY_QUERY_TOKEN": _QUERY_TOKEN,
            "RECOVERY_ADMIN_TOKEN": _ADMIN_TOKEN,
        },
    )


def _runtime(
    source_directory: Path,
    control_dsn: str,
    encryption_key: str,
    replica_id: str,
) -> RuntimeConfig:
    return RuntimeConfig(
        host="127.0.0.1",
        port=0,
        log_level="critical",
        api_token=None,
        source_directory=source_directory,
        budget_file=ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        access_policy_file=None,
        metadata_cache_ttl_ms=30_000,
        metadata_max_stale_ms=300_000,
        metadata_retry_delay_ms=5_000,
        source_mode="managed",
        control_dsn=control_dsn,
        source_encryption_key=encryption_key,
        replica_id=replica_id,
        source_reload_interval_ms=250,
        shutdown_grace_ms=2_000,
    )


def _mutation_headers(
    idempotency_key: str,
    reason: str,
    generation: int,
    state_version: int,
) -> dict[str, str]:
    return {
        "Idempotency-Key": idempotency_key,
        "X-Query-Man-Reason": reason,
        "X-Expected-Generation": str(generation),
        "X-Expected-State-Version": str(state_version),
    }


def _successful_mutation(response: httpx.Response) -> tuple[int, int]:
    assert response.status_code == 200
    document = response.json()
    assert document["outcome"] == "succeeded"
    assert document["http_status"] == 200
    resulting_state = document["resulting_state"]
    assert isinstance(resulting_state, dict)
    generation = resulting_state["generation"]
    state_version = resulting_state["state_version"]
    assert isinstance(generation, int)
    assert isinstance(state_version, int)
    return generation, state_version


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound:
        bound.bind(("127.0.0.1", 0))
        return int(bound.getsockname()[1])


@contextmanager
def _owned_recovery_service() -> Iterator[None]:
    compose = ["docker", "compose", "--profile", "recovery"]
    options = {
        "cwd": ROOT_DIRECTORY,
        "check": True,
        "capture_output": True,
        "text": True,
    }
    existing = subprocess.run(
        [*compose, "ps", "-a", "--services", _RECOVERY_SERVICE],
        **options,
    )
    if existing.stdout.strip():
        pytest.fail("The dedicated recovery source service is already owned")
    try:
        subprocess.run(
            [
                *compose,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "120",
                _RECOVERY_SERVICE,
            ],
            **options,
        )
        yield
    finally:
        subprocess.run(
            [
                *compose,
                "rm",
                "--stop",
                "--force",
                "--volumes",
                _RECOVERY_SERVICE,
            ],
            **options,
        )


@asynccontextmanager
async def _writer_login(
    database: DisposableControlDatabase,
    role_name: str,
    password: str,
) -> AsyncIterator[str]:
    if _LOGIN_PATTERN.fullmatch(role_name) is None:
        raise ValueError("Refusing to manage an unexpected recovery LOGIN")
    created = False
    try:
        connection = await AsyncConnection.connect(database.dsn)
        try:
            await connection.execute(
                sql.SQL(
                    "CREATE ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "INHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 12 PASSWORD {}"
                ).format(sql.Identifier(role_name), sql.Literal(password))
            )
            await connection.execute(
                sql.SQL("GRANT query_man_control_writer TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            attributes = await (
                await connection.execute(
                    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, "
                    "rolinherit, rolreplication, rolbypassrls, rolconnlimit "
                    "FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (role_name,),
                )
            ).fetchone()
            assert attributes == (True, False, False, False, True, False, False, 12)
            memberships = await (
                await connection.execute(
                    "SELECT parent.rolname FROM pg_catalog.pg_auth_members AS membership "
                    "JOIN pg_catalog.pg_roles AS parent ON parent.oid = membership.roleid "
                    "JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member "
                    "WHERE member.rolname = %s ORDER BY parent.rolname",
                    (role_name,),
                )
            ).fetchall()
            assert memberships == [("query_man_control_writer",)]
            await connection.commit()
            created = True
        finally:
            await connection.close()

        writer_dsn = make_conninfo(
            database.dsn,
            user=role_name,
            password=password,
            sslmode="disable",
        )
        yield writer_dsn
    finally:
        leaked_connections = 0
        if created:
            cleanup = await AsyncConnection.connect(database.dsn, autocommit=True)
            try:
                row = await (
                    await cleanup.execute(
                        "SELECT count(*) FROM pg_catalog.pg_stat_activity "
                        "WHERE usename = %s AND datname = %s",
                        (role_name, database.name),
                    )
                ).fetchone()
                leaked_connections = 0 if row is None else int(row[0])
                if leaked_connections:
                    await cleanup.execute(
                        "SELECT pg_catalog.pg_terminate_backend(pid) "
                        "FROM pg_catalog.pg_stat_activity "
                        "WHERE usename = %s AND datname = %s "
                        "AND pid <> pg_catalog.pg_backend_pid()",
                        (role_name, database.name),
                    )
                await cleanup.execute(
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name))
                )
                remaining = await (
                    await cleanup.execute(
                        "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = %s",
                        (role_name,),
                    )
                ).fetchone()
                assert remaining == (0,)
            finally:
                await cleanup.close()
        assert leaked_connections == 0


async def _server_version(dsn: str) -> str:
    connection = await AsyncConnection.connect(dsn)
    try:
        row = await (await connection.execute("SHOW server_version")).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        await connection.close()


async def _wait_for_converged_replicas(
    session: httpx.AsyncClient,
) -> dict[str, object]:
    for _ in range(240):
        response = await session.get(
            f"/admin/sources/{_SOURCE_ID}/replicas",
            headers=_ADMIN_HEADERS,
        )
        if response.status_code == 200:
            document = response.json()
            replicas = document.get("replicas")
            if isinstance(replicas, list) and {
                item.get("replica_id")
                for item in replicas
                if isinstance(item, dict)
            } == set(_REPLICA_IDS) and all(
                item.get("status") == "available" and item.get("drift") == []
                for item in replicas
            ):
                return document
        await asyncio.sleep(0.05)
    raise AssertionError("Managed replicas did not converge")


async def _open_apps(
    stack: AsyncExitStack,
    source_directory: Path,
    writer_dsn: str,
    encryption_key: str,
    access_policy: AccessPolicy,
) -> tuple[tuple[object, httpx.AsyncClient], ...]:
    opened: list[tuple[object, httpx.AsyncClient]] = []
    for replica_id in _REPLICA_IDS:
        app = build_app(
            _runtime(source_directory, writer_dsn, encryption_key, replica_id),
            access_policy=access_policy,
        )
        await stack.enter_async_context(app.router.lifespan_context(app))
        session = await stack.enter_async_context(
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test",
            )
        )
        opened.append((app, session))
    return tuple(opened)


async def _seed_authority(
    source_directory: Path,
    writer_dsn: str,
    encryption_key: str,
    access_policy: AccessPolicy,
    credential: str,
) -> _SeededAuthority:
    l0_manifest: dict[str, Any] = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets.yaml").read_text(
            encoding="utf-8"
        )
    )
    semantic_manifest: dict[str, Any] = yaml.safe_load(
        (
            ROOT_DIRECTORY / "config" / "onboarding" / "support-tickets-l2.yaml"
        ).read_text(encoding="utf-8")
    )
    verified_document: dict[str, Any] = yaml.safe_load(
        (
            ROOT_DIRECTORY
            / "config"
            / "onboarding"
            / "support-tickets-verified-query.yaml"
        ).read_text(encoding="utf-8")
    )
    async with AsyncExitStack() as stack:
        opened = await _open_apps(
            stack,
            source_directory,
            writer_dsn,
            encryption_key,
            access_policy,
        )
        first_session = opened[0][1]
        second_session = opened[1][1]

        l0_headers = _mutation_headers(
            "00000000-0000-4000-8000-000000000901",
            "recovery/seed-l0",
            0,
            0,
        )
        l0 = await first_session.put(
            f"/admin/sources/{_SOURCE_ID}",
            json={"manifest": l0_manifest, "credential": credential},
            headers={**_ADMIN_HEADERS, **l0_headers},
        )
        generation, state_version = _successful_mutation(l0)
        assert (generation, state_version) == (1, 1)

        l1_manifest = deepcopy(semantic_manifest)
        l1_manifest["minimum_quality_level"] = "L1"
        l1_headers = _mutation_headers(
            "00000000-0000-4000-8000-000000000902",
            "recovery/seed-l1",
            generation,
            state_version,
        )
        l1 = await second_session.put(
            f"/admin/sources/{_SOURCE_ID}",
            json={"manifest": l1_manifest, "credential": credential},
            headers={**_ADMIN_HEADERS, **l1_headers},
        )
        generation, state_version = _successful_mutation(l1)
        assert (generation, state_version) == (2, 2)
        metadata_revision = l1.json()["result"]["metadata_revision"]
        assert isinstance(metadata_revision, str)

        verified_body = deepcopy(verified_document)
        verified_body["metadata_revision"] = metadata_revision
        verified_headers = _mutation_headers(
            "00000000-0000-4000-8000-000000000903",
            "recovery/seed-verified",
            generation,
            state_version,
        )
        verified = await second_session.post(
            f"/admin/sources/{_SOURCE_ID}/verified-queries",
            json=verified_body,
            headers={**_ADMIN_HEADERS, **verified_headers},
        )
        verified_generation, verified_state_version = _successful_mutation(verified)
        assert (verified_generation, verified_state_version) == (
            generation,
            state_version,
        )

        l2_manifest = deepcopy(semantic_manifest)
        l2_manifest["minimum_quality_level"] = "L2"
        replay_headers = _mutation_headers(
            "00000000-0000-4000-8000-000000000904",
            "recovery/seed-l2",
            generation,
            state_version,
        )
        replay_body: dict[str, object] = {
            "manifest": l2_manifest,
            "credential": credential,
        }
        l2 = await second_session.put(
            f"/admin/sources/{_SOURCE_ID}",
            json=replay_body,
            headers={**_ADMIN_HEADERS, **replay_headers},
        )
        generation, state_version = _successful_mutation(l2)
        assert (generation, state_version) == (3, 3)
        replay_receipt = l2.json()

        await _wait_for_converged_replicas(first_session)
        for app, _session in opened:
            profile = app.state.registry.get(_SOURCE_ID)  # type: ignore[union-attr]
            assert profile is not None
            assert profile.control_generation == 3

    observation_store = PostgresSourceStore(writer_dsn)
    try:
        replica_writer = ControlReplicaObservationWriter(observation_store)
        resource_writer = ControlResourceObservationWriter(observation_store)
        gateway_writer = ControlGatewayUsageWriter(observation_store)
        incarnations: dict[str, int] = {}
        observation = ReplicaSourceObservation(
            source_id=_SOURCE_ID,
            applied_generation=generation,
            applied_state_version=state_version,
            applied_enabled=True,
            applied_metadata_revision=metadata_revision,
            source_health="healthy",
            reason_code=None,
        )
        for replica_id in _REPLICA_IDS:
            incarnation = await replica_writer.register_replica(replica_id, 5_000)
            incarnations[replica_id] = incarnation
            await replica_writer.report_replica(
                replica_id,
                incarnation,
                reason_code=None,
                sources=(observation,),
            )
        await resource_writer.report_resource_observations(
            _SOURCE_ID,
            generation,
            metadata_revision,
            (
                ResourceObservationSample(
                    "representative_records",
                    3,
                    "rows",
                    "postgres_catalog_estimate",
                    _RESOURCE_DEFINITION,
                ),
                ResourceObservationSample(
                    "table_bytes",
                    4_096,
                    "bytes",
                    "postgres_relation_size",
                    _RESOURCE_DEFINITION,
                ),
                ResourceObservationSample(
                    "index_bytes",
                    2_048,
                    "bytes",
                    "postgres_relation_size",
                    _RESOURCE_DEFINITION,
                ),
                ResourceObservationSample(
                    "total_storage_bytes",
                    6_144,
                    "bytes",
                    "postgres_relation_size",
                    _RESOURCE_DEFINITION,
                ),
            ),
        )
        current_bucket = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        delta = GatewayUsageDelta(
            source_id=_SOURCE_ID,
            budget_profile="interactive",
            metadata_revision=metadata_revision,
            definition_revision=_GATEWAY_DEFINITION,
            bucket_start=current_bucket,
            query_count=1,
            success_count=1,
            rejected_count=0,
            timeout_count=0,
            overloaded_count=0,
            cancelled_count=0,
            failed_count=0,
            queue_ms_sum=1,
            elapsed_ms_sum=2,
            returned_rows_sum=3,
            result_bytes_sum=4,
            truncated_count=0,
        )
        await gateway_writer.report_gateway_usage(
            _REPLICA_IDS[0],
            incarnations[_REPLICA_IDS[0]],
            1,
            (delta,),
        )
        await gateway_writer.report_gateway_usage(
            _REPLICA_IDS[1],
            incarnations[_REPLICA_IDS[1]],
            1,
            (),
        )
    finally:
        await observation_store.close()

    return _SeededAuthority(
        generation=generation,
        state_version=state_version,
        metadata_revision=metadata_revision,
        replay_headers=replay_headers,
        replay_body=replay_body,
        replay_receipt=replay_receipt,
        replica_incarnations=incarnations,
    )


async def _insert_aged_rollup(
    dsn: str,
    metadata_revision: str,
) -> datetime:
    aged_bucket = (datetime.now(UTC) - timedelta(days=32)).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    connection = await AsyncConnection.connect(dsn)
    try:
        await connection.execute(
            "INSERT INTO control.gateway_usage_rollups "
            "(source_id, budget_profile, metadata_revision, definition_revision, "
            "bucket_start, query_count, success_count, queue_ms_sum, elapsed_ms_sum, "
            "returned_rows_sum, result_bytes_sum, observed_at) "
            "VALUES (%s, %s, %s, %s, %s, 1, 1, 1, 1, 1, 1, clock_timestamp())",
            (
                _SOURCE_ID,
                _AGED_BUDGET_PROFILE,
                metadata_revision,
                _GATEWAY_DEFINITION,
                aged_bucket,
            ),
        )
        await connection.commit()
    finally:
        await connection.close()
    return aged_bucket


async def _assert_aged_rollup_exists(dsn: str, aged_bucket: datetime) -> None:
    connection = await AsyncConnection.connect(dsn)
    try:
        row = await (
            await connection.execute(
                "SELECT count(*) FROM control.gateway_usage_rollups "
                "WHERE source_id = %s AND budget_profile = %s AND bucket_start = %s",
                (_SOURCE_ID, _AGED_BUDGET_PROFILE, aged_bucket),
            )
        ).fetchone()
        assert row == (1,)
    finally:
        await connection.close()


async def _assert_recovered_secrets(
    writer_dsn: str,
    encryption_key: str,
    credential: str,
    generation: int,
) -> None:
    recovered_cipher = SourceSecretCipher.from_base64(encryption_key)
    raw_key = base64.urlsafe_b64decode(encryption_key)
    wrong_key = bytes([raw_key[0] ^ 1, *raw_key[1:]])
    wrong_cipher = SourceSecretCipher(wrong_key)
    store = PostgresSourceStore(writer_dsn)
    try:
        for current_generation in range(1, generation + 1):
            revision = await store.get_revision(_SOURCE_ID, current_generation)
            recovered = recovered_cipher.decrypt(
                _SOURCE_ID,
                current_generation,
                revision.encrypted_secret,
            )
            assert secrets.compare_digest(recovered, credential)
            with pytest.raises(SecretDecryptionError):
                wrong_cipher.decrypt(
                    _SOURCE_ID,
                    current_generation,
                    revision.encrypted_secret,
                )
    finally:
        await store.close()


async def _assert_query_invariant(
    session: httpx.AsyncClient,
    verified_document: dict[str, Any],
    metadata_revision: str,
) -> None:
    context = await session.post(
        "/meta",
        json={
            "source_id": _SOURCE_ID,
            "question": verified_document["question"],
        },
        headers=_QUERY_HEADERS,
    )
    assert context.status_code == 200
    context_document = context.json()
    assert context_document["metadata_revision"] == metadata_revision
    assert context_document["quality_level"] == "L2"
    queried = await session.post(
        "/query",
        json={
            "source_id": _SOURCE_ID,
            "metadata_revision": context_document["metadata_revision"],
            "sql_policy_revision": context_document["sql_policy_revision"],
            "sql": verified_document["sql"],
        },
        headers=_QUERY_HEADERS,
    )
    assert queried.status_code == 200
    result = queried.json()
    expected = verified_document["expected"]
    assert result["columns"] == expected["columns"]
    assert result["row_count"] == expected["row_count"]
    assert result["truncated"] is False
    assert create_result_hash(tuple(result["columns"]), result["rows"]) == expected[
        "result_hash"
    ]


async def _runtime_incarnations(dsn: str) -> dict[str, int]:
    connection = await AsyncConnection.connect(dsn)
    try:
        rows = await (
            await connection.execute(
                "SELECT replica_id, incarnation FROM control.runtime_replicas "
                "WHERE replica_id = ANY(%s) ORDER BY replica_id",
                (list(_REPLICA_IDS),),
            )
        ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}
    finally:
        await connection.close()


async def _source_and_receipt_state(dsn: str) -> tuple[int, int, int]:
    connection = await AsyncConnection.connect(dsn)
    try:
        row = await (
            await connection.execute(
                "SELECT active.generation, active.state_version, "
                "(SELECT max(event_id) FROM control.source_mutation_receipts) "
                "FROM control.active_source_profiles AS active "
                "WHERE active.source_id = %s",
                (_SOURCE_ID,),
            )
        ).fetchone()
        assert row is not None
        assert row[2] is not None
        return int(row[0]), int(row[1]), int(row[2])
    finally:
        await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_writer_login_creation_rolls_back_on_initialization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = postgres_environment()
    if environment is None:
        pytest.skip("local PostgreSQL credentials are not configured")
    role_name = _LOGIN_PREFIX + secrets.token_hex(12)
    password = secrets.token_urlsafe(24)

    async with disposable_control_database(environment) as database:
        original_execute = AsyncConnection.execute

        async def fail_after_create(
            connection: AsyncConnection[Any],
            query: Any,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            cursor = await original_execute(connection, query, *args, **kwargs)
            rendered = (
                query.as_string(connection)
                if isinstance(query, sql.Composable)
                else str(query)
            )
            if rendered.startswith("CREATE ROLE"):
                raise RuntimeError("forced writer LOGIN initialization failure")
            return cursor

        with monkeypatch.context() as scoped_patch:
            scoped_patch.setattr(AsyncConnection, "execute", fail_after_create)
            with pytest.raises(
                RuntimeError,
                match="forced writer LOGIN initialization failure",
            ):
                async with _writer_login(database, role_name, password):
                    raise AssertionError("unreachable")

        connection = await AsyncConnection.connect(database.dsn)
        try:
            remaining = await (
                await connection.execute(
                    "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = %s",
                    (role_name,),
                )
            ).fetchone()
            assert remaining == (0,)
        finally:
            await connection.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_restored_control_database_zero_bootstraps_two_managed_replicas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = postgres_environment()
    if environment is None:
        pytest.skip("local PostgreSQL credentials are not configured")
    recovery_port = _unused_local_port()
    monkeypatch.setenv("QUERY_MAN_RECOVERY_POSTGRES_PORT", str(recovery_port))
    encryption_key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
    login_name = _LOGIN_PREFIX + secrets.token_hex(12)
    login_password = secrets.token_urlsafe(24)
    credential = os.environ.get(
        "SUPPORT_TICKETS_READER_PASSWORD",
        "support-tickets-local-secret",
    )
    source_directory = tmp_path / "absent-source-directory" / "sources"
    access_policy = _access_policy(tmp_path)
    verified_document: dict[str, Any] = yaml.safe_load(
        (
            ROOT_DIRECTORY
            / "config"
            / "onboarding"
            / "support-tickets-verified-query.yaml"
        ).read_text(encoding="utf-8")
    )

    with _owned_recovery_service():
        async with disposable_control_database(
            environment,
            compose_service=_RECOVERY_SERVICE,
        ) as source_database:
            assert (await _server_version(source_database.dsn)).startswith("18.4")
            async with _writer_login(
                source_database,
                login_name,
                login_password,
            ) as source_writer_dsn:
                seeded = await _seed_authority(
                    source_directory,
                    source_writer_dsn,
                    encryption_key,
                    access_policy,
                    credential,
                )
                aged_bucket = await _insert_aged_rollup(
                    source_database.dsn,
                    seeded.metadata_revision,
                )
                await _assert_aged_rollup_exists(source_database.dsn, aged_bucket)
                source_fingerprint = await control_table_fingerprint(
                    source_database.dsn
                )
                assert tuple(item[0] for item in source_fingerprint) == CONTROL_TABLES
                assert all(item[1] > 0 for item in source_fingerprint)

            async with disposable_control_database(
                environment,
                compose_service="postgres",
                apply_migrations_on_create=False,
            ) as target_database:
                archive_path = tmp_path / "control-recovery.dump"
                archive_sha = restore_control_backup(
                    source_database,
                    target_database,
                    archive_path,
                )
                assert not archive_path.exists()
                assert re.fullmatch(r"sha256:[a-f0-9]{64}", archive_sha) is not None
                assert (await _server_version(target_database.dsn)).startswith("18.6")
                target_fingerprint = await control_table_fingerprint(
                    target_database.dsn
                )
                assert target_fingerprint == source_fingerprint
                await _assert_aged_rollup_exists(target_database.dsn, aged_bucket)

                async with _writer_login(
                    target_database,
                    login_name,
                    login_password,
                ) as target_writer_dsn:
                    await _assert_recovered_secrets(
                        target_writer_dsn,
                        encryption_key,
                        credential,
                        seeded.generation,
                    )
                    async with AsyncExitStack() as stack:
                        restored = await _open_apps(
                            stack,
                            source_directory,
                            target_writer_dsn,
                            encryption_key,
                            access_policy,
                        )
                        first_session = restored[0][1]
                        convergence = await _wait_for_converged_replicas(first_session)
                        assert convergence["desired"] == {
                            "enabled": True,
                            "generation": seeded.generation,
                            "state_version": seeded.state_version,
                            "metadata_revision": seeded.metadata_revision,
                        }
                        restored_incarnations = await _runtime_incarnations(
                            target_database.dsn
                        )
                        assert restored_incarnations == {
                            replica_id: seeded.replica_incarnations[replica_id] + 1
                            for replica_id in _REPLICA_IDS
                        }

                        replay = await first_session.put(
                            f"/admin/sources/{_SOURCE_ID}",
                            json=seeded.replay_body,
                            headers={**_ADMIN_HEADERS, **seeded.replay_headers},
                        )
                        assert replay.status_code == 200
                        assert replay.json() == seeded.replay_receipt
                        receipt_state = await _source_and_receipt_state(
                            target_database.dsn
                        )
                        assert receipt_state == (
                            seeded.generation,
                            seeded.state_version,
                            seeded.replay_receipt["event_id"],
                        )

                        rejected_body = deepcopy(seeded.replay_body)
                        rejected_manifest = rejected_body["manifest"]
                        assert isinstance(rejected_manifest, dict)
                        rejected_manifest["source_id"] = "different-source"
                        rejected = await first_session.put(
                            f"/admin/sources/{_SOURCE_ID}",
                            json=rejected_body,
                            headers={
                                **_ADMIN_HEADERS,
                                **_mutation_headers(
                                    "00000000-0000-4000-8000-000000000905",
                                    "recovery/verify-receipt-sequence",
                                    seeded.generation,
                                    seeded.state_version,
                                ),
                            },
                        )
                        assert rejected.status_code == 400
                        assert rejected.json()["error"]["code"] == (
                            "SOURCE_VALIDATION_FAILED"
                        )
                        assert await _source_and_receipt_state(target_database.dsn) == (
                            seeded.generation,
                            seeded.state_version,
                            receipt_state[2] + 1,
                        )

                        for _app, session in restored:
                            await _assert_query_invariant(
                                session,
                                verified_document,
                                seeded.metadata_revision,
                            )

                        usage = await first_session.get(
                            f"/admin/sources/{_SOURCE_ID}/usage",
                            headers=_ADMIN_HEADERS,
                        )
                        assert usage.status_code == 200
                        gateway = usage.json()["gateway"]
                        assert gateway["lower_bound"] is True
                        assert gateway["rollups"]
                        assert all(
                            row["budget_profile"] != _AGED_BUDGET_PROFILE
                            for row in gateway["rollups"]
                        )

                    await _assert_aged_rollup_exists(
                        target_database.dsn,
                        aged_bucket,
                    )

    assert not source_directory.exists()
