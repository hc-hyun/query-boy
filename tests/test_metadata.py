from __future__ import annotations

import pytest

from query_man.errors import MetadataUnavailableError
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, SourceProfile
from tests.helpers import load_test_registry, minimal_development_snapshot


class StaticCatalog:
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot

    async def close(self) -> None:
        pass


class SequencedCatalog(StaticCatalog):
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        super().__init__(snapshot)
        self.load_count = 0

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        self.load_count += 1
        if self.load_count > 1:
            raise RuntimeError("temporary catalog failure")
        return self.snapshot


class SnapshotSequenceCatalog:
    def __init__(self, snapshots: list[CatalogSnapshot]) -> None:
        self.snapshots = iter(snapshots)

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return next(self.snapshots)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_rejects_semantic_overlay_drift() -> None:
    snapshot = minimal_development_snapshot()
    snapshot.relations = [item for item in snapshot.relations if item.qualified_name == "ai.issue_overview"]
    service = MetadataService(load_test_registry(), StaticCatalog(snapshot))
    with pytest.raises(MetadataUnavailableError):
        await service.get_context("development-issues", "최근 문제")


@pytest.mark.asyncio
async def test_returns_stale_revision_after_refresh_failure() -> None:
    catalog = SequencedCatalog(minimal_development_snapshot())
    service = MetadataService(load_test_registry(), catalog, cache_ttl_ms=0, now=lambda: 1000)
    fresh = await service.get_context("development-issues", "최근 문제")
    stale = await service.get_context("development-issues", "최근 문제")
    stale_during_backoff = await service.get_context("development-issues", "최근 문제")
    assert fresh["snapshot_status"] == "fresh"
    assert stale["snapshot_status"] == "stale"
    assert stale["metadata_revision"] == fresh["metadata_revision"]
    assert stale_during_backoff["snapshot_status"] == "stale"
    assert catalog.load_count == 2
    assert "STALE_METADATA_SNAPSHOT" in [item["code"] for item in stale["ambiguities"]]


@pytest.mark.asyncio
async def test_fails_closed_on_drift_even_with_cache() -> None:
    incomplete = minimal_development_snapshot()
    incomplete.relations = incomplete.relations[:1]
    catalog = SnapshotSequenceCatalog([minimal_development_snapshot(), incomplete])
    service = MetadataService(load_test_registry(), catalog, cache_ttl_ms=0, now=lambda: 1000)
    await service.get_context("development-issues", "최근 문제")
    with pytest.raises(MetadataUnavailableError):
        await service.get_context("development-issues", "최근 문제")


@pytest.mark.asyncio
async def test_generic_identifier_question_is_low_confidence() -> None:
    service = MetadataService(load_test_registry(), StaticCatalog(minimal_development_snapshot()))
    response = await service.get_context("development-issues", "ID를 보여줘")
    assert response["answerability"]["status"] == "low_confidence"


@pytest.mark.asyncio
async def test_returns_verified_user_activity_composition() -> None:
    service = MetadataService(load_test_registry(), StaticCatalog(minimal_development_snapshot()))
    response = await service.get_context("development-issues", "보고자와 담당자, 댓글 작성자별 활동 건수를 비교해줘")
    assert [item["name"] for item in response["relations"]] == [
        "ai.issue_overview",
        "ai.issue_comments",
    ]
    assert response["composition_hints"][0]["name"] == "user_activity_by_role"
    assert response["joins"] == []
