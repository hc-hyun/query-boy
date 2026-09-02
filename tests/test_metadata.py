from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace

import pytest

from query_man.errors import MetadataUnavailableError
from query_man.guarded_query.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DEFAULT_ALLOWED_TYPES,
    DEFAULT_ALLOWED_UNQUALIFIED_TYPES,
    SQL_POLICY_REVISION,
)
from query_man.metadata.catalog import _CatalogValidationError
from query_man.metadata.models import (
    CatalogForeignKey,
    CatalogIndex,
    CatalogSnapshot,
)
from query_man.metadata.relevance import RankedRelation, SelectionReason
from query_man.metadata.revision import create_metadata_revision
from query_man.metadata.service import MetadataService, _to_relation_response
from query_man.runtime.operations import operations
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import column, load_test_registry, minimal_development_snapshot


class StaticCatalog:
    def __init__(self, snapshot: CatalogSnapshot) -> None:
        self.snapshot = snapshot

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        return self.snapshot

    async def close(self) -> None:
        pass


class SequencedCatalog(StaticCatalog):
    def __init__(
        self,
        snapshot: CatalogSnapshot,
        *,
        failure: Exception | None = None,
    ) -> None:
        super().__init__(snapshot)
        self.load_count = 0
        self.failure = failure or RuntimeError("temporary catalog failure")

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        self.load_count += 1
        if self.load_count > 1:
            raise self.failure
        return self.snapshot


class SnapshotSequenceCatalog:
    def __init__(self, snapshots: list[CatalogSnapshot]) -> None:
        self.snapshots = iter(snapshots)
        self.load_count = 0

    async def load(self, _source: SourceProfile) -> CatalogSnapshot:
        self.load_count += 1
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


class MutableSourceReader:
    def __init__(self, source: SourceProfile) -> None:
        self.source = source

    def get(self, source_id: str) -> SourceProfile | None:
        return self.source if source_id == self.source.source_id else None

    def list(self) -> list[dict[str, str]]:
        return [
            {
                "source_id": self.source.source_id,
                "name": self.source.name,
                "description": self.source.description,
            }
        ]

    def source_ids(self) -> frozenset[str]:
        return frozenset({self.source.source_id})


@pytest.mark.asyncio
async def test_rejects_semantic_overlay_drift() -> None:
    snapshot = minimal_development_snapshot()
    snapshot = replace(
        snapshot,
        relations=tuple(
            item
            for item in snapshot.relations
            if item.qualified_name == "ai.issue_overview"
        ),
    )
    service = MetadataService(load_test_registry(), StaticCatalog(snapshot))
    with pytest.raises(MetadataUnavailableError):
        await service.get_context("development-issues", "최근 문제")


@pytest.mark.asyncio
async def test_requires_complete_semantic_metadata_before_publication() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    semantics = source.semantic_overlay.relations
    first = semantics[0]
    cases = (
        (
            replace(
                source,
                semantic_overlay=replace(
                    source.semantic_overlay,
                    relations=(first, first, *semantics[1:]),
                ),
            ),
            snapshot,
            "duplicate relations",
        ),
        (
            replace(
                source,
                semantic_overlay=replace(
                    source.semantic_overlay,
                    relations=semantics[1:],
                ),
            ),
            snapshot,
            "Missing semantic metadata",
        ),
        (
            replace(
                source,
                semantic_overlay=replace(
                    source.semantic_overlay,
                    relations=(replace(first, grain=None), *semantics[1:]),
                ),
            ),
            snapshot,
            "Missing grain",
        ),
        (
            replace(
                source,
                semantic_overlay=replace(
                    source.semantic_overlay,
                    relations=(
                        replace(first, description=None),
                        *semantics[1:],
                    ),
                ),
            ),
            replace(
                snapshot,
                relations=(
                    replace(snapshot.relations[0], comment=None),
                    *snapshot.relations[1:],
                ),
            ),
            "Missing description",
        ),
        (
            replace(
                source,
                semantic_overlay=replace(
                    source.semantic_overlay,
                    relations=(
                        replace(first, default_time_column=None),
                        *semantics[1:],
                    ),
                ),
            ),
            snapshot,
            "Missing default time",
        ),
    )

    for incomplete_source, current_snapshot, expected in cases:
        service = MetadataService(
            SourceRegistry([incomplete_source]),
            StaticCatalog(current_snapshot),
        )
        with pytest.raises(MetadataUnavailableError) as captured:
            await service.get_published(source.source_id)
        assert any(
            expected in violation
            for violation in captured.value.details["contract_violations"]
        )


@pytest.mark.asyncio
async def test_requires_exact_view_contract_comment_source_and_version() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    relation = snapshot.relations[0]
    mismatches = (
        replace(relation, view_contract_source="another-source"),
        replace(
            relation,
            view_contract_version=source.view_contract_version + 1,
        ),
        replace(
            relation,
            view_contract_source=None,
            view_contract_version=None,
        ),
    )

    for mismatch in mismatches:
        current = replace(
            snapshot,
            relations=(mismatch, *snapshot.relations[1:]),
        )
        service = MetadataService(registry, StaticCatalog(current))
        with pytest.raises(MetadataUnavailableError) as captured:
            await service.get_published(source.source_id)
        assert any(
            "View contract" in violation
            for violation in captured.value.details["contract_violations"]
        )


@pytest.mark.asyncio
async def test_rejects_catalog_relations_outside_source_allowlist() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    snapshot = replace(
        snapshot,
        relations=(
            replace(snapshot.relations[0], kind="table"),
            *snapshot.relations[1:],
        ),
    )
    service = MetadataService(registry, StaticCatalog(snapshot))

    with pytest.raises(MetadataUnavailableError) as captured:
        await service.get_published(source.source_id)

    assert any(
        "outside the source allowlist" in violation
        for violation in captured.value.details["contract_violations"]
    )


@pytest.mark.parametrize("failure_type", [RuntimeError, ValueError])
@pytest.mark.asyncio
async def test_returns_stale_revision_after_refresh_failure(
    failure_type: type[Exception],
) -> None:
    catalog = SequencedCatalog(
        minimal_development_snapshot(),
        failure=failure_type("temporary catalog failure"),
    )
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
async def test_catalog_policy_rejection_never_returns_a_stale_revision() -> None:
    catalog = SequencedCatalog(
        minimal_development_snapshot(),
        failure=_CatalogValidationError("Catalog relation limit exceeded"),
    )
    service = MetadataService(load_test_registry(), catalog, cache_ttl_ms=0, now=lambda: 1000)
    await service.get_context("development-issues", "최근 문제")

    with pytest.raises(MetadataUnavailableError):
        await service.get_context("development-issues", "최근 문제")

    assert catalog.load_count == 2


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
async def test_metadata_refresh_does_not_overwrite_query_health() -> None:
    clock = [1_000]
    registry = load_test_registry()
    service = MetadataService(
        registry,
        StaticCatalog(minimal_development_snapshot()),
        cache_ttl_ms=10,
        now=lambda: clock[0],
    )
    operations.reset()
    operations.reconcile_sources(registry.source_ids())
    try:
        await service.get_published("development-issues")
        operations.set_source_query_health("development-issues", "unavailable")

        await service.get_published("development-issues")
        assert operations.snapshot()["sources"]["development-issues"] == "unavailable"

        clock[0] = 1_011
        await service.get_published("development-issues")
        assert operations.snapshot()["sources"]["development-issues"] == "unavailable"
    finally:
        operations.reset()


@pytest.mark.asyncio
async def test_fails_closed_on_drift_even_with_cache() -> None:
    incomplete = minimal_development_snapshot()
    incomplete = replace(incomplete, relations=incomplete.relations[:1])
    catalog = SnapshotSequenceCatalog([minimal_development_snapshot(), incomplete])
    service = MetadataService(load_test_registry(), catalog, cache_ttl_ms=0, now=lambda: 1000)
    await service.get_context("development-issues", "최근 문제")
    with pytest.raises(MetadataUnavailableError):
        await service.get_context("development-issues", "최근 문제")


@pytest.mark.asyncio
async def test_same_view_contract_version_rejects_structure_drift_without_stale() -> None:
    snapshot = minimal_development_snapshot()
    relation = snapshot.relations[0]
    changed = replace(
        snapshot,
        relations=(
            replace(
                relation,
                columns=(
                    replace(relation.columns[0], data_type="numeric"),
                    *relation.columns[1:],
                ),
            ),
            *snapshot.relations[1:],
        ),
    )
    catalog = SnapshotSequenceCatalog([snapshot, changed])
    service = MetadataService(
        load_test_registry(),
        catalog,
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )
    await service.get_published("development-issues")

    with pytest.raises(MetadataUnavailableError) as captured:
        await service.get_published("development-issues")

    assert captured.value.details == {
        "contract_violations": [
            "View structure changed without a view contract version change."
        ]
    }
    assert catalog.load_count == 2


@pytest.mark.asyncio
async def test_view_contract_marker_drift_never_returns_warm_stale() -> None:
    snapshot = minimal_development_snapshot()
    changed = replace(
        snapshot,
        relations=(
            replace(snapshot.relations[0], view_contract_source="another-source"),
            *snapshot.relations[1:],
        ),
    )
    catalog = SnapshotSequenceCatalog([snapshot, changed])
    service = MetadataService(
        load_test_registry(),
        catalog,
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )
    await service.get_published("development-issues")

    with pytest.raises(MetadataUnavailableError) as captured:
        await service.get_published("development-issues")

    assert any(
        "View contract source" in violation
        for violation in captured.value.details["contract_violations"]
    )
    assert catalog.load_count == 2


@pytest.mark.asyncio
async def test_comment_change_rotates_request_revision_without_structure_rejection() -> None:
    snapshot = minimal_development_snapshot()
    changed = replace(
        snapshot,
        relations=(
            replace(snapshot.relations[0], comment="개선된 사람용 설명"),
            *snapshot.relations[1:],
        ),
    )
    service = MetadataService(
        load_test_registry(),
        SnapshotSequenceCatalog([snapshot, changed]),
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )

    first = await service.get_published("development-issues")
    second = await service.get_published("development-issues")

    assert second.revision != first.revision


@pytest.mark.asyncio
async def test_new_view_contract_version_accepts_reviewed_structure_change() -> None:
    registry = load_test_registry()
    first_source = registry.get("development-issues")
    assert first_source is not None
    first_snapshot = minimal_development_snapshot()
    second_source = replace(
        first_source,
        view_contract_version=first_source.view_contract_version + 1,
    )
    changed_relation = first_snapshot.relations[0]
    changed_relation = replace(
        changed_relation,
        view_contract_version=second_source.view_contract_version,
        columns=(
            *changed_relation.columns,
            replace(
                column("reviewed_attribute"),
                ordinal=len(changed_relation.columns) + 1,
            ),
        ),
    )
    second_snapshot = replace(
        first_snapshot,
        relations=tuple(
            replace(
                relation,
                view_contract_version=second_source.view_contract_version,
            )
            if relation.qualified_name != changed_relation.qualified_name
            else changed_relation
            for relation in first_snapshot.relations
        ),
    )
    reader = MutableSourceReader(first_source)
    catalog = SnapshotSequenceCatalog([first_snapshot, second_snapshot])
    service = MetadataService(reader, catalog)
    first = await service.get_published(first_source.source_id)

    reader.source = second_source
    service.invalidate(second_source.source_id)
    second = await service.get_published(second_source.source_id)

    assert second.revision != first.revision
    assert "reviewed_attribute" in {
        current.name for current in second.snapshot.relations[0].columns
    }


@pytest.mark.asyncio
async def test_invalidated_refresh_cannot_replace_new_profile_metadata() -> None:
    registry = load_test_registry()
    first_source = registry.get("development-issues")
    assert first_source is not None
    second_source = replace(
        first_source,
        semantic_overlay=replace(first_source.semantic_overlay, business_terms=()),
    )
    assert first_source != second_source
    reader = MutableSourceReader(first_source)
    catalog = BarrierCatalog(
        minimal_development_snapshot(),
        minimal_development_snapshot(),
    )
    service = MetadataService(reader, catalog)

    old_refresh = asyncio.create_task(service.get_published(first_source.source_id))
    await catalog.first_started.wait()
    reader.source = second_source
    service.invalidate(second_source.source_id)
    current_refresh = asyncio.create_task(service.get_published(second_source.source_id))
    await asyncio.sleep(0)
    catalog.release_first.set()

    with pytest.raises(MetadataUnavailableError):
        await old_refresh
    current = await current_refresh
    assert catalog.load_count == 2
    assert await service.get_published(second_source.source_id) == current


@pytest.mark.asyncio
async def test_cached_metadata_must_match_current_source_contract() -> None:
    registry = load_test_registry()
    first_source = registry.get("development-issues")
    assert first_source is not None
    reader = MutableSourceReader(first_source)
    service = MetadataService(reader, StaticCatalog(minimal_development_snapshot()))
    first = await service.get_published(first_source.source_id)
    second_source = replace(
        first_source,
        semantic_overlay=replace(first_source.semantic_overlay, business_terms=()),
    )
    assert create_metadata_revision(second_source, first.snapshot) != first.revision
    reader.source = second_source

    with pytest.raises(MetadataUnavailableError):
        await service.get_published(second_source.source_id)


@pytest.mark.asyncio
async def test_generic_identifier_question_is_low_confidence() -> None:
    service = MetadataService(load_test_registry(), StaticCatalog(minimal_development_snapshot()))
    response = await service.get_context("development-issues", "ID를 보여줘")
    assert response["answerability"]["status"] == "low_confidence"


@pytest.mark.asyncio
async def test_exposes_deterministic_sql_capabilities_from_validation_policy() -> None:
    registry = load_test_registry()
    source = registry.get("development-issues")
    assert source is not None
    service = MetadataService(registry, StaticCatalog(minimal_development_snapshot()))

    response = await service.get_context(source.source_id, "최근 문제")

    capabilities = response["sql_capabilities"]
    assert isinstance(capabilities, dict)
    assert capabilities == {
        "functions": sorted(DEFAULT_ALLOWED_FUNCTIONS),
        "cast_types": sorted(DEFAULT_ALLOWED_TYPES),
        "unqualified_cast_types": sorted(DEFAULT_ALLOWED_UNQUALIFIED_TYPES),
    }
    assert "quality_level" not in response
    assert response["sql_policy_revision"] == SQL_POLICY_REVISION
    assert {
        "date_part",
        "dense_rank",
        "extract",
        "jsonb_build_object",
        "lag",
        "lead",
        "percentile_cont",
        "position",
        "rank",
        "regexp_replace",
        "to_jsonb",
    } <= set(capabilities["functions"])
    assert len(json.dumps(response, ensure_ascii=False).encode()) <= (
        source.budget.max_metadata_response_bytes
    )


@pytest.mark.asyncio
async def test_context_projection_keeps_plain_json_arrays_and_objects() -> None:
    service = MetadataService(
        load_test_registry(),
        StaticCatalog(minimal_development_snapshot()),
    )

    for question in (
        "문제 원인과 조치를 보여줘",
        "문제별 댓글을 보여줘",
        "보고자와 담당자, 댓글 작성자별 활동 건수를 비교해줘",
    ):
        response = await service.get_context("development-issues", question)
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))

        assert json.loads(encoded) == response
        for join in response["joins"]:
            assert isinstance(join["column_pairs"], list)
            assert all(isinstance(pair, dict) for pair in join["column_pairs"])


def test_relation_projection_bytes_match_pre_immutability_golden() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    relation = next(
        item
        for item in minimal_development_snapshot().relations
        if item.qualified_name == "ai.issue_overview"
    )
    semantic = next(
        item
        for item in source.semantic_overlay.relations
        if item.relation == relation.qualified_name
    )
    response = _to_relation_response(
        RankedRelation(
            relation,
            semantic,
            1.0,
            [
                SelectionReason("relation_alias", "problem"),
                SelectionReason("column_alias", "원인", "cause"),
            ],
        ),
        1,
        source.semantic_overlay.joins,
        source.semantic_overlay.business_terms,
        "원인 문제",
        20,
    )
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()

    assert len(encoded) == 3_781
    assert hashlib.sha256(encoded).hexdigest() == (
        "a3362a4d0b23e7dd0d95053ce410474785cd2185a77b6f39f15e65f048b554ea"
    )


def test_common_question_matches_do_not_exceed_wide_relation_column_target() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    relation = next(
        item
        for item in minimal_development_snapshot().relations
        if item.qualified_name == "ai.issue_overview"
    )
    semantic = next(
        item
        for item in source.semantic_overlay.relations
        if item.relation == relation.qualified_name
    )
    common_columns = tuple(
        replace(
            column(f"common_value_{number:02d}"),
            ordinal=len(relation.columns) + number,
            comment="공통 값",
        )
        for number in range(12, 0, -1)
    )
    relation = replace(relation, columns=relation.columns + common_columns)
    candidate = RankedRelation(
        relation,
        semantic,
        1.0,
        [SelectionReason("relation_alias", "개발 문제")],
    )

    response = _to_relation_response(
        candidate,
        1,
        source.semantic_overlay.joins,
        source.semantic_overlay.business_terms,
        "공통 값",
        6,
    )

    assert response["returned_column_count"] == 6
    assert [item["name"] for item in response["columns"]] == [
        "issue_id",
        "discovered_at",
        "comment_count",
        "common_value_01",
        "common_value_02",
        "common_value_03",
    ]

    required_only = _to_relation_response(
        candidate,
        1,
        source.semantic_overlay.joins,
        source.semantic_overlay.business_terms,
        "공통 값",
        2,
    )
    assert required_only["returned_column_count"] == 3
    assert [item["name"] for item in required_only["columns"]] == [
        "issue_id",
        "discovered_at",
        "comment_count",
    ]


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
    snapshot = replace(
        snapshot,
        relations=tuple(
            replace(
                relation,
                primary_key=("issue_id",),
                indexes=(
                    CatalogIndex(
                        ("discovered_at",), unique=False, primary=False
                    ),
                ),
            )
            if relation.qualified_name == "ai.issue_overview"
            else replace(
                relation,
                foreign_keys=(
                    CatalogForeignKey(
                        ("issue_id",), "ai.issue_overview", ("issue_id",)
                    ),
                ),
            )
            if relation.qualified_name == "ai.issue_comments"
            else relation
            for relation in snapshot.relations
        ),
    )
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
    issue = replace(
        issue,
        columns=issue.columns
        + tuple(
            replace(
                column(f"extra_attribute_{number:02d}"),
                ordinal=len(issue.columns) + number,
            )
            for number in range(1, 61)
        ),
        indexes=(
            CatalogIndex(
                ("extra_attribute_60",), unique=False, primary=False
            ),
        ),
    )
    snapshot = replace(
        snapshot,
        relations=tuple(
            issue if relation.qualified_name == issue.qualified_name else relation
            for relation in snapshot.relations
        ),
    )
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
async def test_source_and_global_invalidate_force_process_local_refresh() -> None:
    registry = load_test_registry()
    snapshot = minimal_development_snapshot()
    catalog = SnapshotSequenceCatalog([snapshot, snapshot, snapshot])
    service = MetadataService(
        registry,
        catalog,
        cache_ttl_ms=10_000,
        now=lambda: 1_000,
    )
    first = await service.get_published("development-issues")
    assert catalog.load_count == 1

    service.invalidate("development-issues")
    second = await service.get_published("development-issues")
    assert second == first
    assert catalog.load_count == 2

    service.invalidate()
    third = await service.get_published("development-issues")
    assert third == first
    assert catalog.load_count == 3
