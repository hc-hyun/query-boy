from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from query_man.errors import MetadataUnavailableError
from query_man.metadata import MetadataService
from query_man.models import (
    CatalogForeignKey,
    CatalogIndex,
    CatalogSnapshot,
    PreparedMetadata,
    SourceProfile,
)
from query_man.registry import SourceRegistry
from query_man.revision import create_metadata_revision
from tests.helpers import column, load_test_registry, minimal_development_snapshot


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


class FailingCatalog(StaticCatalog):
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        super().__init__(snapshot)
        self.load_count = 0

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        self.load_count += 1
        raise RuntimeError("temporary catalog failure")


class SnapshotSequenceCatalog:
    def __init__(self, snapshots: list[CatalogSnapshot]) -> None:
        self.snapshots = iter(snapshots)

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return next(self.snapshots)

    async def close(self) -> None:
        pass


class BarrierCatalog:
    def __init__(
        self,
        first: CatalogSnapshot,
        second: CatalogSnapshot,
    ) -> None:
        self._first = first
        self._second = second
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.load_count = 0

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        self.load_count += 1
        if self.load_count == 1:
            self.first_started.set()
            await self.release_first.wait()
            return self._first
        return self._second

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

    async def get_revision(self, source: SourceProfile, revision: str) -> PreparedMetadata:
        return self.values[source.source_id][revision]

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


def test_freshness_provenance_is_not_part_of_metadata_identity() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    revision = create_metadata_revision(source, snapshot)

    assert PreparedMetadata(snapshot, revision, 10) == PreparedMetadata(
        snapshot,
        revision,
        20,
    )


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
async def test_source_outage_fails_closed_after_stale_limit_expires() -> None:
    clock = [1_000]
    service = MetadataService(
        load_test_registry(),
        SequencedCatalog(minimal_development_snapshot()),
        cache_ttl_ms=0,
        max_stale_ms=10,
        now=lambda: clock[0],
    )
    await service.get_context("development-issues", "최근 문제")
    clock[0] = 1_011

    with pytest.raises(MetadataUnavailableError):
        await service.get_context("development-issues", "최근 문제")


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
async def test_invalidated_refresh_cannot_replace_new_profile_metadata() -> None:
    registry = load_test_registry()
    bootstrap = registry.get("development-issues")
    assert bootstrap is not None
    first_source = replace(bootstrap, control_generation=1)
    second_source = replace(
        first_source,
        control_generation=2,
        semantic_overlay=replace(first_source.semantic_overlay, business_terms=[]),
    )
    assert first_source != second_source
    registry = SourceRegistry([first_source])
    store = MemoryMetadataStore()
    catalog = BarrierCatalog(
        minimal_development_snapshot(),
        minimal_development_snapshot(),
    )
    service = MetadataService(
        registry,
        catalog,
        store=store,
        verified_revisions={},
    )

    old_refresh = asyncio.create_task(service.get_published(first_source.source_id))
    await catalog.first_started.wait()
    registry.upsert(second_source)
    service.invalidate(second_source.source_id)
    current_refresh = asyncio.create_task(service.get_published(second_source.source_id))
    await asyncio.sleep(0)
    catalog.release_first.set()

    with pytest.raises(MetadataUnavailableError):
        await old_refresh
    current = await current_refresh
    assert catalog.load_count == 2
    assert store.active[second_source.source_id] == current.revision
    assert await service.get_published(second_source.source_id) == current


@pytest.mark.asyncio
async def test_cached_metadata_must_match_current_source_contract() -> None:
    registry = load_test_registry()
    first_source = registry.get("development-issues")
    assert first_source is not None
    service = MetadataService(
        registry,
        StaticCatalog(minimal_development_snapshot()),
        verified_revisions={},
    )
    first = await service.get_published(first_source.source_id)
    second_source = replace(
        first_source,
        semantic_overlay=replace(first_source.semantic_overlay, business_terms=[]),
    )
    assert create_metadata_revision(second_source, first.snapshot) != first.revision
    registry.upsert(second_source)

    with pytest.raises(MetadataUnavailableError):
        await service.get_published(second_source.source_id)


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
async def test_exposes_physical_keys_without_approving_a_semantic_join() -> None:
    snapshot = minimal_development_snapshot()
    by_name = {relation.qualified_name: relation for relation in snapshot.relations}
    by_name["ai.issue_overview"].primary_key = ["issue_id"]
    by_name["ai.issue_overview"].indexes = [
        CatalogIndex(["discovered_at"], unique=False, primary=False)
    ]
    by_name["ai.issue_comments"].foreign_keys = [
        CatalogForeignKey(["issue_id"], "ai.issue_overview", ["issue_id"])
    ]
    service = MetadataService(load_test_registry(), StaticCatalog(snapshot))
    response = await service.get_context(
        "development-issues",
        "사용자별 등록 문제 수, 담당 문제 수와 작성 댓글 수를 비교해줘.",
    )
    relations = {relation["name"]: relation for relation in response["relations"]}
    assert relations["ai.issue_overview"]["primary_key"] == ["issue_id"]
    assert relations["ai.issue_comments"]["foreign_keys"][0] == {
        "columns": ["issue_id"],
        "referenced_relation": "ai.issue_overview",
        "referenced_columns": ["issue_id"],
    }
    assert relations["ai.issue_overview"]["indexes"] == [
        {"columns": ["discovered_at"], "unique": False, "primary": False}
    ]
    assert response["joins"] == []


@pytest.mark.asyncio
async def test_rls_source_rejects_non_security_invoker_relations() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    source = replace(source, tenant_isolation="rls")
    service = MetadataService(
        SourceRegistry([source]),
        StaticCatalog(minimal_development_snapshot()),
    )

    with pytest.raises(MetadataUnavailableError) as captured:
        await service.get_published(source.source_id)

    assert any(
        "security-invoker" in violation
        for violation in captured.value.details["contract_violations"]
    )


@pytest.mark.asyncio
async def test_scopes_wide_relation_columns_to_question_and_required_semantics() -> None:
    snapshot = minimal_development_snapshot()
    issue = next(
        relation
        for relation in snapshot.relations
        if relation.qualified_name == "ai.issue_overview"
    )
    for number in range(1, 61):
        extra = column(f"extra_attribute_{number:02d}")
        extra.ordinal = len(issue.columns) + 1
        issue.columns.append(extra)
    issue.indexes = [
        CatalogIndex(["extra_attribute_60"], unique=False, primary=False)
    ]
    registry = load_test_registry()
    sources = [
        replace(
            source,
            budget=replace(source.budget, max_context_columns_per_relation=20),
        )
        for source_id in ["development-issues", "market-voc"]
        if (source := registry.get(source_id)) is not None
    ]
    service = MetadataService(SourceRegistry(sources), StaticCatalog(snapshot))
    response = await service.get_context(
        "development-issues",
        "원인이 아직 입력되지 않은 문제를 보여줘",
    )
    relation = response["relations"][0]
    returned = {item["name"] for item in relation["columns"]}
    assert relation["column_count"] == 73
    assert relation["returned_column_count"] == 20
    assert relation["columns_truncated"] is True
    assert relation["indexes"] == []
    assert relation["indexes_truncated"] is True
    assert response["truncated"] is True
    assert {"issue_id", "discovered_at", "cause", "comment_count"} <= returned
    assert "extra_attribute_60" not in returned


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
async def test_restored_provenance_uses_remaining_ttl_and_stale_bound() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    revision = create_metadata_revision(source, snapshot)
    restored = PreparedMetadata(snapshot, revision, freshness_age_ms=9)
    store = MemoryMetadataStore()
    store.values[source.source_id] = {revision: restored}
    store.active[source.source_id] = revision
    catalog = FailingCatalog(snapshot)
    clock = [1_000]
    service = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=10,
        max_stale_ms=20,
        refresh_retry_ms=100,
        now=lambda: clock[0],
        store=store,
    )

    fresh = await service.get_context(source.source_id, "최근 문제")
    clock[0] = 1_002
    stale = await service.get_context(source.source_id, "최근 문제")
    clock[0] = 1_012

    assert fresh["snapshot_status"] == "fresh"
    assert stale["snapshot_status"] == "stale"
    assert catalog.load_count == 1
    with pytest.raises(MetadataUnavailableError):
        await service.get_context(source.source_id, "최근 문제")


@pytest.mark.parametrize(
    ("freshness_age_ms", "unavailable"),
    [(15, False), (21, True)],
)
@pytest.mark.asyncio
async def test_pinned_different_revision_uses_active_freshness(
    freshness_age_ms: int,
    unavailable: bool,
) -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    active_snapshot = minimal_development_snapshot()
    active_revision = create_metadata_revision(source, active_snapshot)
    active = PreparedMetadata(
        active_snapshot,
        active_revision,
        freshness_age_ms=freshness_age_ms,
    )
    candidate_snapshot = minimal_development_snapshot()
    candidate_snapshot.relations[0].comment = "different candidate"
    store = MemoryMetadataStore()
    store.values[source.source_id] = {active_revision: active}
    store.active[source.source_id] = active_revision
    store.pinned.add(source.source_id)
    service = MetadataService(
        registry,
        StaticCatalog(candidate_snapshot),
        cache_ttl_ms=10,
        max_stale_ms=20,
        now=lambda: 1_000,
        store=store,
    )

    if unavailable:
        with pytest.raises(MetadataUnavailableError):
            await service.get_context(source.source_id, "최근 문제")
    else:
        response = await service.get_context(source.source_id, "최근 문제")
        assert response["snapshot_status"] == "stale"
        assert response["metadata_revision"] == active_revision
    assert store.active[source.source_id] == active_revision


@pytest.mark.asyncio
async def test_resume_preserves_provenance_and_forces_refresh() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    revision = create_metadata_revision(source, snapshot)
    restored = PreparedMetadata(snapshot, revision, freshness_age_ms=15)
    store = MemoryMetadataStore()
    store.values[source.source_id] = {revision: restored}
    store.active[source.source_id] = revision
    store.pinned.add(source.source_id)
    catalog = FailingCatalog(snapshot)
    clock = [1_000]
    service = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=100,
        max_stale_ms=20,
        refresh_retry_ms=100,
        now=lambda: clock[0],
        store=store,
    )

    await service.resume_automatic_publish(source.source_id)
    stale = await service.get_context(source.source_id, "최근 문제")
    clock[0] = 1_006

    assert stale["snapshot_status"] == "stale"
    assert catalog.load_count == 1
    assert source.source_id not in store.pinned
    with pytest.raises(MetadataUnavailableError):
        await service.get_context(source.source_id, "최근 문제")


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
