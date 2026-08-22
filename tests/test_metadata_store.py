from __future__ import annotations

import os
from dataclasses import replace

import pytest
import yaml
from dotenv import load_dotenv
from psycopg import AsyncConnection, errors
from psycopg.conninfo import make_conninfo

from query_man.metadata_store import (
    PostgresMetadataStore,
    StoredMetadataInvalidError,
    StoredMetadataNotFoundError,
    StoredMetadataSupersededError,
)
from query_man.models import CatalogForeignKey, CatalogIndex, PreparedMetadata
from query_man.registry import load_budget_profiles, validate_source_manifest
from query_man.revision import create_metadata_revision
from query_man.secrets import SourceSecretCipher
from query_man.source_store import PostgresSourceStore
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
    first_by_name = {
        relation.qualified_name: relation for relation in first_snapshot.relations
    }
    first_by_name["ai.issue_overview"].primary_key = ["issue_id"]
    first_by_name["ai.issue_overview"].indexes = [
        CatalogIndex(["discovered_at"], unique=False, primary=False)
    ]
    first_by_name["ai.issue_comments"].foreign_keys = [
        CatalogForeignKey(["issue_id"], "ai.issue_overview", ["issue_id"])
    ]
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_metadata_publish_rejects_superseded_control_generation() -> None:
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
    raw = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["source_id"] = "metadata-generation-cas-fixture"
    raw["connection"]["password_env"] = (
        "METADATA_GENERATION_CAS_FIXTURE_READER_PASSWORD"
    )
    raw["minimum_quality_level"] = "L0"
    validated = validate_source_manifest(
        raw,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        "reader-secret",
    )
    source_store = PostgresSourceStore(dsn)
    metadata_store = PostgresMetadataStore(dsn)
    cipher = SourceSecretCipher(b"m" * 32)
    try:
        current = await source_store.get_active(validated.profile.source_id)
        unpin_source = (
            validated.profile
            if current is None
            else replace(
                validated.profile,
                control_generation=current.generation,
                control_state_version=current.state_version,
            )
        )
        try:
            await metadata_store.unpin(unpin_source)
        except StoredMetadataNotFoundError:
            pass
        expected_generation = 0 if current is None else current.generation

        first_generation = await source_store.next_generation(validated.profile.source_id)
        first_snapshot = minimal_development_snapshot()
        first_snapshot.relations[0].comment = f"CAS generation {first_generation}"
        first_metadata = PreparedMetadata(
            first_snapshot,
            create_metadata_revision(validated.profile, first_snapshot),
        )
        first = await source_store.publish(
            validated.profile.source_id,
            expected_generation,
            first_generation,
            validated.document,
            cipher.encrypt(
                validated.profile.source_id,
                first_generation,
                "reader-secret",
            ),
            first_metadata,
            expected_state_version=0 if current is None else current.state_version,
        )

        second_generation = await source_store.next_generation(validated.profile.source_id)
        second_snapshot = minimal_development_snapshot()
        second_snapshot.relations[0].comment = f"CAS generation {second_generation}"
        second_metadata = PreparedMetadata(
            second_snapshot,
            create_metadata_revision(validated.profile, second_snapshot),
        )
        second = await source_store.publish(
            validated.profile.source_id,
            first.generation,
            second_generation,
            validated.document,
            cipher.encrypt(
                validated.profile.source_id,
                second_generation,
                "reader-secret",
            ),
            second_metadata,
            expected_state_version=first.state_version,
        )
        stale_source = replace(
            validated.profile,
            control_generation=first.generation,
            control_state_version=first.state_version,
        )
        current_source = replace(
            validated.profile,
            control_generation=second.generation,
            control_state_version=second.state_version,
        )
        refreshed_snapshot = minimal_development_snapshot()
        refreshed_snapshot.relations[0].comment = (
            f"CAS active generation {second_generation} refresh"
        )
        refreshed_metadata = PreparedMetadata(
            refreshed_snapshot,
            create_metadata_revision(current_source, refreshed_snapshot),
        )

        assert await metadata_store.publish(current_source, refreshed_metadata) == (
            refreshed_metadata
        )

        with pytest.raises(StoredMetadataSupersededError):
            await metadata_store.publish(stale_source, first_metadata)
        with pytest.raises(StoredMetadataSupersededError):
            await metadata_store.publish(validated.profile, first_metadata)

        assert await metadata_store.get_active(current_source) == refreshed_metadata

        inactive_state_version = await source_store.deactivate(
            validated.profile.source_id,
            second.generation,
            expected_state_version=second.state_version,
        )
        rolled_back = await source_store.rollback(
            validated.profile.source_id,
            second.generation,
            second.generation,
            expected_state_version=inactive_state_version,
        )
        rolled_back_source = replace(
            validated.profile,
            control_generation=rolled_back.generation,
            control_state_version=rolled_back.state_version,
        )

        with pytest.raises(StoredMetadataSupersededError):
            await metadata_store.unpin(current_source)
        with pytest.raises(StoredMetadataSupersededError):
            await metadata_store.publish(current_source, refreshed_metadata)

        connection = await AsyncConnection.connect(dsn)
        try:
            pinned_cursor = await connection.execute(
                "SELECT pinned FROM control.active_metadata_revisions "
                "WHERE source_id = %s",
                (validated.profile.source_id,),
            )
            assert await pinned_cursor.fetchone() == (True,)
        finally:
            await connection.close()

        await metadata_store.unpin(rolled_back_source)
    finally:
        await metadata_store.close()
        await source_store.close()
