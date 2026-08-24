from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
import yaml
from psycopg import AsyncConnection, errors

from query_man.metadata_store import (
    PostgresMetadataStore,
    StoredMetadataInvalidError,
    StoredMetadataNotFoundError,
    StoredMetadataSupersededError,
    _decode,
    encode_snapshot,
)
from query_man.models import CatalogForeignKey, CatalogIndex, PreparedMetadata
from query_man.registry import load_budget_profiles, validate_source_manifest
from query_man.revision import create_metadata_revision
from query_man.secrets import SourceSecretCipher
from query_man.source_store import PostgresSourceStore
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot


def test_snapshot_codec_preserves_legacy_json_and_freezes_decoded_graph() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    snapshot = replace(
        snapshot,
        relations=tuple(
            replace(
                relation,
                primary_key=("issue_id",)
                if relation.qualified_name == "ai.issue_overview"
                else relation.primary_key,
                indexes=(
                    CatalogIndex(
                        ("discovered_at",), unique=False, primary=False
                    ),
                )
                if relation.qualified_name == "ai.issue_overview"
                else relation.indexes,
                foreign_keys=(
                    CatalogForeignKey(
                        ("issue_id",), "ai.issue_overview", ("issue_id",)
                    ),
                )
                if relation.qualified_name == "ai.issue_comments"
                else relation.foreign_keys,
            )
            for relation in snapshot.relations
        ),
    )
    revision = create_metadata_revision(source, snapshot)
    encoded = encode_snapshot(snapshot)
    legacy_json = json.loads(json.dumps(encoded))

    decoded = _decode(source, revision, legacy_json)

    assert encode_snapshot(decoded.snapshot) == encoded
    assert create_metadata_revision(source, decoded.snapshot) == revision
    relations = encoded["relations"]
    assert isinstance(relations, list)
    by_name = {
        f"{relation['schema_name']}.{relation['relation_name']}": relation
        for relation in relations
    }
    assert by_name["ai.issue_overview"]["primary_key"] == ["issue_id"]
    assert by_name["ai.issue_overview"]["indexes"][0]["columns"] == [
        "discovered_at"
    ]
    assert by_name["ai.issue_comments"]["foreign_keys"][0]["columns"] == [
        "issue_id"
    ]
    assert by_name["ai.issue_comments"]["foreign_keys"][0][
        "referenced_columns"
    ] == ["issue_id"]
    assert isinstance(decoded.snapshot.relations, tuple)
    assert isinstance(decoded.snapshot.relations[0].columns, tuple)

    legacy_json["relations"][0]["comment"] = "mutated after decode"
    assert decoded.snapshot.relations[0].comment != "mutated after decode"


def test_minimal_snapshot_json_matches_pre_immutability_golden() -> None:
    encoded = json.dumps(
        encode_snapshot(minimal_development_snapshot()),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    assert len(encoded) == 2_609
    assert hashlib.sha256(encoded).hexdigest() == (
        "1f392b10b1b505430920e95d07549ed2dbc51e20cece984a4528e2abf406dbc7"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_metadata_store_publishes_immutable_revisions_and_rolls_back(
    disposable_control_dsn: str,
) -> None:
    dsn = disposable_control_dsn
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(source, source_id="metadata-store-fixture-v2")
    first_snapshot = minimal_development_snapshot()
    first_snapshot = replace(
        first_snapshot,
        relations=tuple(
            replace(
                relation,
                comment=(
                    "immutable revision one" if index == 0 else relation.comment
                ),
                primary_key=("issue_id",)
                if relation.qualified_name == "ai.issue_overview"
                else relation.primary_key,
                indexes=(
                    CatalogIndex(
                        ("discovered_at",), unique=False, primary=False
                    ),
                )
                if relation.qualified_name == "ai.issue_overview"
                else relation.indexes,
                foreign_keys=(
                    CatalogForeignKey(
                        ("issue_id",), "ai.issue_overview", ("issue_id",)
                    ),
                )
                if relation.qualified_name == "ai.issue_comments"
                else relation.foreign_keys,
            )
            for index, relation in enumerate(first_snapshot.relations)
        ),
    )
    second_snapshot = minimal_development_snapshot()
    second_snapshot = replace(
        second_snapshot,
        relations=(
            replace(second_snapshot.relations[0], comment="immutable revision two"),
            *second_snapshot.relations[1:],
        ),
    )
    first = PreparedMetadata(first_snapshot, create_metadata_revision(source, first_snapshot))
    second = PreparedMetadata(second_snapshot, create_metadata_revision(source, second_snapshot))
    store = PostgresMetadataStore(dsn)
    try:
        try:
            await store.unpin(source)
        except StoredMetadataNotFoundError:
            pass
        published_first = await store.publish(source, first)
        published_second = await store.publish(source, second)
        assert published_first.freshness_age_ms is not None
        assert published_first.freshness_age_ms >= 0
        assert published_second.freshness_age_ms is not None
        assert published_second.freshness_age_ms >= 0
        assert (await store.get_active(source)) == second

        rolled_back = await store.activate(source, first.revision)
        assert rolled_back == first
        assert rolled_back.freshness_age_ms is not None
        assert rolled_back.freshness_age_ms >= 0
        assert (await store.get_active(source)) == first

        connection = await AsyncConnection.connect(dsn)
        try:
            await connection.execute(
                "UPDATE control.active_metadata_revisions "
                "SET activated_at = clock_timestamp() - interval '10 seconds' "
                "WHERE source_id = %s",
                (source.source_id,),
            )
            await connection.commit()
            aged = await store.get_active(source)
            assert aged is not None
            assert aged.freshness_age_ms is not None
            assert aged.freshness_age_ms >= 9_000

            same_revision = await store.publish(source, first)
            assert same_revision == first
            assert same_revision.freshness_age_ms is not None
            assert same_revision.freshness_age_ms < 5_000
            pinned = await connection.execute(
                "SELECT pinned FROM control.active_metadata_revisions "
                "WHERE source_id = %s",
                (source.source_id,),
            )
            assert await pinned.fetchone() == (True,)

            await connection.execute(
                "UPDATE control.active_metadata_revisions "
                "SET activated_at = clock_timestamp() - interval '10 seconds' "
                "WHERE source_id = %s",
                (source.source_id,),
            )
            await connection.commit()
            pinned_different = await store.publish(source, second)
            assert pinned_different == first
            assert pinned_different.freshness_age_ms is not None
            assert pinned_different.freshness_age_ms >= 9_000

            await connection.execute(
                "UPDATE control.active_metadata_revisions "
                "SET activated_at = clock_timestamp() + interval '10 seconds' "
                "WHERE source_id = %s",
                (source.source_id,),
            )
            await connection.commit()
            with pytest.raises(StoredMetadataInvalidError):
                await store.get_active(source)

            await store.unpin(source)
            refreshed = await store.publish(source, second)
            assert refreshed == second
            assert refreshed.freshness_age_ms is not None
            assert refreshed.freshness_age_ms < 5_000

            with pytest.raises(StoredMetadataNotFoundError):
                await store.activate(source, f"sha256:{'0' * 64}")
            incompatible = replace(source, allowed_schemas=["other"])
            with pytest.raises(StoredMetadataInvalidError):
                await store.get_active(incompatible)

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
async def test_metadata_publish_rejects_superseded_control_generation(
    disposable_control_dsn: str,
) -> None:
    dsn = disposable_control_dsn
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
        if current is None or current.enabled:
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
        first_snapshot = replace(
            first_snapshot,
            relations=(
                replace(
                    first_snapshot.relations[0],
                    comment=f"CAS generation {first_generation}",
                ),
                *first_snapshot.relations[1:],
            ),
        )
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
        second_snapshot = replace(
            second_snapshot,
            relations=(
                replace(
                    second_snapshot.relations[0],
                    comment=f"CAS generation {second_generation}",
                ),
                *second_snapshot.relations[1:],
            ),
        )
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
        refreshed_snapshot = replace(
            refreshed_snapshot,
            relations=(
                replace(
                    refreshed_snapshot.relations[0],
                    comment=f"CAS active generation {second_generation} refresh",
                ),
                *refreshed_snapshot.relations[1:],
            ),
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
        active = await source_store.get_active(validated.profile.source_id)
        if active is not None and active.enabled:
            await source_store.deactivate(
                active.source_id,
                active.generation,
                expected_state_version=active.state_version,
            )
        await metadata_store.close()
        await source_store.close()
