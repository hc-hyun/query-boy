from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from query_man.metadata_store import PostgresMetadataStore
from query_man.models import PreparedMetadata
from query_man.registry import load_budget_profiles, validate_source_manifest
from query_man.revision import create_metadata_revision
from query_man.secrets import SourceSecretCipher
from query_man.source_store import (
    PostgresSourceStore,
    SourceGenerationConflictError,
    SourcePublishPinnedError,
)
from query_man.verified import ExpectedResult, VerifiedQuery, create_result_hash
from tests.helpers import ROOT_DIRECTORY, minimal_development_snapshot


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_store_publishes_rotates_rolls_back_and_deactivates(
    disposable_control_dsn: str,
) -> None:
    dsn = disposable_control_dsn
    raw = yaml.safe_load(
        (ROOT_DIRECTORY / "config" / "sources" / "development-issues.yaml").read_text(
            encoding="utf-8"
        )
    )
    raw["source_id"] = "source-store-fixture"
    raw["connection"]["password_env"] = "SOURCE_STORE_FIXTURE_READER_PASSWORD"
    validated = validate_source_manifest(
        raw,
        load_budget_profiles(ROOT_DIRECTORY / "config" / "budget-profiles.yaml"),
        "first-reader-secret",
    )
    source = replace(validated.profile, minimum_quality_level="L0")
    snapshot = minimal_development_snapshot()
    metadata = PreparedMetadata(snapshot, create_metadata_revision(source, snapshot))
    cipher = SourceSecretCipher(b"s" * 32)
    store = PostgresSourceStore(dsn)
    metadata_store = PostgresMetadataStore(dsn)
    try:
        current = await store.get_active(source.source_id)
        if current is not None and not current.enabled:
            current = await store.rollback(
                source.source_id,
                current.generation,
                current.generation,
                expected_state_version=current.state_version,
            )
        if current is not None:
            await metadata_store.unpin(
                replace(
                    source,
                    control_generation=current.generation,
                    control_state_version=current.state_version,
                )
            )
        expected = 0 if current is None else current.generation
        first_generation = await store.next_generation(source.source_id)
        first_secret = cipher.encrypt(source.source_id, first_generation, "first-reader-secret")
        first = await store.publish(
            source.source_id,
            expected,
            first_generation,
            validated.document,
            first_secret,
            metadata,
            expected_state_version=0 if current is None else current.state_version,
        )
        assert first.generation == first_generation
        assert cipher.decrypt(source.source_id, first.generation, first.encrypted_secret) == (
            "first-reader-secret"
        )

        rotated_generation = await store.next_generation(source.source_id)
        rotated_secret = cipher.encrypt(source.source_id, rotated_generation, "rotated-secret")
        rotated = await store.publish(
            source.source_id,
            first.generation,
            rotated_generation,
            validated.document,
            rotated_secret,
            metadata,
            expected_state_version=first.state_version,
        )
        assert rotated.generation == rotated_generation
        assert rotated.state_version == first.state_version + 1
        assert cipher.decrypt(source.source_id, rotated.generation, rotated.encrypted_secret) == (
            "rotated-secret"
        )

        verified_query = VerifiedQuery(
            query_id="source-store-status-count",
            source_id=source.source_id,
            question="상태별 건수를 보여줘",
            sql="SELECT status, count(*) FROM ai.issue_overview GROUP BY status",
            metadata_revision=metadata.revision,
            relations=("ai.issue_overview",),
            expected=ExpectedResult(
                columns=("status", "count"),
                row_count=1,
                result_hash=create_result_hash(
                    ("status", "count"),
                    [{"status": "OPEN", "count": 1}],
                ),
            ),
        )
        await store.publish_verified_query(verified_query)
        assert metadata.revision in (
            await store.verified_revision_map()
        )[source.source_id]

        rolled_back = await store.rollback(
            source.source_id,
            first.generation,
            rotated.generation,
            expected_state_version=rotated.state_version,
        )
        assert rolled_back.generation == first.generation
        assert rolled_back.state_version == rotated.state_version + 1
        assert cipher.decrypt(source.source_id, rolled_back.generation, rolled_back.encrypted_secret) == (
            "first-reader-secret"
        )

        resumed_generation = await store.next_generation(source.source_id)
        resumed_secret = cipher.encrypt(
            source.source_id,
            resumed_generation,
            "resumed-secret",
        )
        with pytest.raises(SourceGenerationConflictError):
            await store.publish(
                source.source_id,
                rolled_back.generation,
                resumed_generation,
                validated.document,
                resumed_secret,
                metadata,
                expected_state_version=first.state_version,
            )
        with pytest.raises(SourcePublishPinnedError):
            await store.publish(
                source.source_id,
                rolled_back.generation,
                resumed_generation,
                validated.document,
                resumed_secret,
                metadata,
                expected_state_version=rolled_back.state_version,
            )

        await metadata_store.unpin(
            replace(
                source,
                control_generation=rolled_back.generation,
                control_state_version=rolled_back.state_version,
            )
        )
        resumed = await store.publish(
            source.source_id,
            rolled_back.generation,
            resumed_generation,
            validated.document,
            resumed_secret,
            metadata,
            expected_state_version=rolled_back.state_version,
        )
        assert resumed.state_version == rolled_back.state_version + 1

        inactive_state_version = await store.deactivate(
            source.source_id,
            resumed.generation,
            expected_state_version=resumed.state_version,
        )
        inactive = await store.get_active(source.source_id)
        assert inactive is not None
        assert inactive.enabled is False
        assert inactive.state_version == inactive_state_version
        assert inactive.state_version == resumed.state_version + 1
    finally:
        await metadata_store.close()
        await store.close()
