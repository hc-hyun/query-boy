from __future__ import annotations

import pytest

from query_man.errors import MetadataUnavailableError
from query_man.metadata import MetadataService
from query_man.models import CatalogSnapshot, PreparedMetadata, SourceProfile
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


class MemoryMetadataStore:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, PreparedMetadata]] = {}
        self.active: dict[str, str] = {}
        self.closed = False
        self.pinned: set[str] = set()

    async def get_active(self, source: SourceProfile) -> PreparedMetadata | None:
        revision = self.active.get(source.source_id)
        return None if revision is None else self.values[source.source_id][revision]

    async def publish(self, source: SourceProfile, value: PreparedMetadata) -> PreparedMetadata:
        self.values.setdefault(source.source_id, {})[value.revision] = value
        if source.source_id not in self.pinned:
            self.active[source.source_id] = value.revision
        return self.values[source.source_id][self.active[source.source_id]]

    async def activate(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        value = self.values[source.source_id][revision]
        self.active[source.source_id] = revision
        self.pinned.add(source.source_id)
        return value

    async def unpin(self, source: SourceProfile) -> None:
        self.pinned.discard(source.source_id)

    async def close(self) -> None:
        self.closed = True


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


@pytest.mark.asyncio
async def test_publishes_and_restores_active_metadata_across_service_restart() -> None:
    registry = load_test_registry()
    store = MemoryMetadataStore()
    first_catalog = StaticCatalog(minimal_development_snapshot())
    first = MetadataService(registry, first_catalog, store=store)
    published = await first.get_published("development-issues")

    class NeverCalledCatalog(StaticCatalog):
        async def load(self, _source: SourceProfile) -> CatalogSnapshot:
            raise AssertionError("the persisted active snapshot should be loaded first")

    restarted = MetadataService(
        registry,
        NeverCalledCatalog(minimal_development_snapshot()),
        store=store,
    )
    restored = await restarted.get_published("development-issues")
    assert restored == published


@pytest.mark.asyncio
async def test_rolls_back_to_a_previous_published_revision() -> None:
    registry = load_test_registry()
    first_snapshot = minimal_development_snapshot()
    second_snapshot = minimal_development_snapshot()
    second_snapshot.relations[0].comment = "new catalog comment"
    store = MemoryMetadataStore()
    clock = [1_000]
    service = MetadataService(
        registry,
        SnapshotSequenceCatalog(
            [first_snapshot, second_snapshot, second_snapshot, second_snapshot]
        ),
        cache_ttl_ms=10,
        now=lambda: clock[0],
        store=store,
    )
    first = await service.get_published("development-issues")
    clock[0] = 1_011
    second = await service.get_published("development-issues")
    assert first.revision != second.revision

    rolled_back = await service.rollback("development-issues", first.revision)
    assert rolled_back.revision == first.revision
    assert (await service.get_published("development-issues")).revision == first.revision

    clock[0] = 1_022
    assert (await service.get_published("development-issues")).revision == first.revision
    await service.resume_automatic_publish("development-issues")
    clock[0] = 1_033
    assert (await service.get_published("development-issues")).revision == second.revision
