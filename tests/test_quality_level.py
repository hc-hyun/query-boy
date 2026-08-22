from __future__ import annotations

from dataclasses import replace

import pytest

from query_man.errors import MetadataUnavailableError
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, PreparedMetadata, SemanticOverlay, SourceProfile
from query_man.quality_level import assess_quality_level
from query_man.registry import SourceRegistry
from query_man.revision import create_metadata_revision
from tests.helpers import load_test_registry, minimal_development_snapshot
from tests.test_metadata import MemoryMetadataStore


class StaticCatalog:
    def __init__(self) -> None:
        self.snapshot = minimal_development_snapshot()

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot

    async def close(self) -> None:
        pass


def test_assesses_l0_l1_and_l2_quality_levels() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    revision = create_metadata_revision(source, snapshot)

    l1 = assess_quality_level(source, snapshot, revision, {})
    assert l1.level == "L1"
    assert l1.publishable

    l2_source = replace(source, minimum_quality_level="L2")
    l2 = assess_quality_level(
        l2_source,
        snapshot,
        revision,
        {source.source_id: frozenset({revision})},
    )
    assert l2.level == "L2"
    assert l2.publishable

    l0_source = replace(
        source,
        minimum_quality_level="L1",
        semantic_overlay=SemanticOverlay(None, [], [], [], [], []),
    )
    l0 = assess_quality_level(l0_source, snapshot, revision, {})
    assert l0.level == "L0"
    assert not l0.publishable
    assert any("missing semantic metadata" in item for item in l0.violations)


@pytest.mark.asyncio
async def test_metadata_publish_fails_below_declared_quality_level() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(source, minimum_quality_level="L2")
    service = MetadataService(
        SourceRegistry([source]),
        StaticCatalog(),
        verified_revisions={},
    )
    with pytest.raises(MetadataUnavailableError) as captured:
        await service.get_published(source.source_id)
    assert captured.value.details == {
        "contract_violations": [
            "no verified query contract matches the metadata revision"
        ]
    }


@pytest.mark.asyncio
async def test_rejected_rollback_preserves_active_revision() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(source, minimum_quality_level="L2")
    snapshot = minimal_development_snapshot()
    old_revision = create_metadata_revision(source, snapshot)
    old_value = PreparedMetadata(snapshot, old_revision)

    current_snapshot = minimal_development_snapshot()
    current_snapshot.relations[0].comment = "verified revision"
    current_revision = create_metadata_revision(source, current_snapshot)
    current_value = PreparedMetadata(current_snapshot, current_revision)
    store = MemoryMetadataStore()
    store.values[source.source_id] = {
        old_revision: old_value,
        current_revision: current_value,
    }
    store.active[source.source_id] = current_revision
    service = MetadataService(
        SourceRegistry([source]),
        StaticCatalog(),
        store=store,
        verified_revisions={source.source_id: frozenset({current_revision})},
    )

    with pytest.raises(MetadataUnavailableError):
        await service.rollback(source.source_id, old_revision)

    assert store.active[source.source_id] == current_revision
