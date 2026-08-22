from __future__ import annotations

import os
from dataclasses import replace

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection, errors
from psycopg.conninfo import make_conninfo

from query_man.metadata_store import (
    PostgresMetadataStore,
    StoredMetadataInvalidError,
    StoredMetadataNotFoundError,
)
from query_man.models import PreparedMetadata
from query_man.revision import create_metadata_revision
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_metadata_store_publishes_immutable_revisions_and_rolls_back() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL control-plane credentials are not configured")
    dsn = make_conninfo(
        host="127.0.0.1",
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode="disable",
    )
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(source, source_id="metadata-store-fixture-v2")
    first_snapshot = minimal_development_snapshot()
    first_snapshot.relations[0].comment = "immutable revision one"
    second_snapshot = minimal_development_snapshot()
    second_snapshot.relations[0].comment = "immutable revision two"
    first = PreparedMetadata(first_snapshot, create_metadata_revision(source, first_snapshot))
    second = PreparedMetadata(second_snapshot, create_metadata_revision(source, second_snapshot))
    store = PostgresMetadataStore(dsn)
    try:
        await store.unpin(source)
        await store.publish(source, first)
        await store.publish(source, second)
        assert (await store.get_active(source)) == second

        rolled_back = await store.activate(source, first.revision)
        assert rolled_back == first
        assert (await store.get_active(source)) == first
        assert (await store.publish(source, second)) == first
        await store.unpin(source)
        assert (await store.publish(source, second)) == second

        with pytest.raises(StoredMetadataNotFoundError):
            await store.activate(source, f"sha256:{'0' * 64}")
        incompatible = replace(source, allowed_schemas=["other"])
        with pytest.raises(StoredMetadataInvalidError):
            await store.get_active(incompatible)

        connection = await AsyncConnection.connect(dsn)
        try:
            privileges = await connection.execute(
                "SELECT "
                "has_table_privilege('query_man_control_writer', "
                "'control.metadata_snapshots', 'SELECT,INSERT'), "
                "NOT has_table_privilege('query_man_control_writer', "
                "'control.metadata_snapshots', 'UPDATE,DELETE'), "
                "has_table_privilege('query_man_control_writer', "
                "'control.active_metadata_revisions', 'SELECT,INSERT,UPDATE'), "
                "NOT has_table_privilege('query_man_control_writer', "
                "'control.active_metadata_revisions', 'DELETE')"
            )
            assert await privileges.fetchone() == (True, True, True, True)
            with pytest.raises(errors.RaiseException):
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE control.metadata_snapshots SET snapshot = snapshot "
                        "WHERE source_id = %s AND revision = %s",
                        (source.source_id, first.revision),
                    )
        finally:
            await connection.close()
    finally:
        await store.close()
