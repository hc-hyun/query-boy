from __future__ import annotations

import json
from dataclasses import replace

import pytest

from query_man.errors import MetadataUnavailableError, SourceNotFoundError
from query_man.guarded_query.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DEFAULT_ALLOWED_TYPES,
    DEFAULT_ALLOWED_UNQUALIFIED_TYPES,
    SQL_POLICY_REVISION,
)
from query_man.metadata.catalog import _CatalogValidationError
from query_man.metadata.models import CatalogSnapshot
from query_man.metadata.service import MetadataService
from query_man.source_catalog.models import SourceProfile
from query_man.source_catalog.registry import SourceRegistry
from tests.helpers import column, load_test_registry, minimal_query_cave_snapshot


def _described_snapshot() -> CatalogSnapshot:
    snapshot = minimal_query_cave_snapshot()
    return replace(
        snapshot,
        relations=tuple(
            replace(relation, comment=f"Description for {relation.qualified_name}") for relation in snapshot.relations
        ),
    )


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


@pytest.mark.asyncio
async def test_unknown_source_fails_closed() -> None:
    service = MetadataService(
        load_test_registry(),
        StaticCatalog(_described_snapshot()),
    )

    with pytest.raises(SourceNotFoundError):
        await service.get_context("unknown")


@pytest.mark.asyncio
async def test_requires_exact_view_contract_source_version_and_description() -> None:
    registry = load_test_registry()
    source = registry.get("query-cave")
    assert source is not None
    snapshot = _described_snapshot()
    relation = snapshot.relations[0]
    mismatches = (
        replace(relation, view_contract_source="another-source"),
        replace(relation, view_contract_version=source.view_contract_version + 1),
        replace(relation, view_contract_source=None, view_contract_version=None),
        replace(relation, comment=None),
    )

    for mismatch in mismatches:
        current = replace(
            snapshot,
            relations=(mismatch, *snapshot.relations[1:]),
        )
        service = MetadataService(registry, StaticCatalog(current))
        with pytest.raises(MetadataUnavailableError) as captured:
            await service.get_published(source.source_id)
        assert captured.value.details["contract_violations"]


@pytest.mark.asyncio
async def test_rejects_empty_duplicate_or_outside_allowlist_catalog() -> None:
    registry = load_test_registry()
    snapshot = _described_snapshot()
    invalid_snapshots = (
        CatalogSnapshot(),
        replace(snapshot, relations=(snapshot.relations[0], snapshot.relations[0])),
        replace(
            snapshot,
            relations=(
                replace(snapshot.relations[0], kind="table"),
                *snapshot.relations[1:],
            ),
        ),
    )

    for invalid in invalid_snapshots:
        service = MetadataService(registry, StaticCatalog(invalid))
        with pytest.raises(MetadataUnavailableError):
            await service.get_published("query-cave")


@pytest.mark.asyncio
async def test_context_is_complete_deterministic_catalog_projection() -> None:
    snapshot = _described_snapshot()
    reversed_snapshot = replace(
        snapshot,
        relations=tuple(
            replace(relation, columns=tuple(reversed(relation.columns))) for relation in reversed(snapshot.relations)
        ),
    )
    registry = load_test_registry()
    service = MetadataService(registry, StaticCatalog(reversed_snapshot))

    response = await service.get_context("query-cave")

    assert [item["name"] for item in response["relations"]] == [
        "signal_schema.case_files_view",
        "signal_schema.case_notes_view",
        "signal_schema.response_units_view",
    ]
    for relation in response["relations"]:
        assert list(relation) == [
            "name",
            "sql_name",
            "kind",
            "description",
            "column_count",
            "returned_column_count",
            "columns_truncated",
            "columns",
        ]
        assert [item["ordinal"] for item in relation["columns"]] == sorted(
            item["ordinal"] for item in relation["columns"]
        )
    assert response["sql_capabilities"] == {
        "functions": sorted(DEFAULT_ALLOWED_FUNCTIONS),
        "cast_types": sorted(DEFAULT_ALLOWED_TYPES),
        "unqualified_cast_types": sorted(DEFAULT_ALLOWED_UNQUALIFIED_TYPES),
    }
    assert response["sql_policy_revision"] == SQL_POLICY_REVISION
    assert response["snapshot_status"] == "fresh"
    assert response["truncated"] is False
    assert {
        "question",
        "answerability",
        "joins",
        "business_terms",
        "composition_hints",
        "ambiguities",
    }.isdisjoint(response)
    assert json.loads(json.dumps(response, ensure_ascii=False)) == response


@pytest.mark.asyncio
async def test_context_truncates_columns_by_stable_catalog_order() -> None:
    registry = load_test_registry()
    source = registry.get("query-cave")
    assert source is not None
    source = replace(
        source,
        budget=replace(source.budget, max_context_columns_per_relation=2),
    )
    snapshot = _described_snapshot()
    case_file = snapshot.relations[1]
    case_file = replace(
        case_file,
        columns=(
            *case_file.columns,
            replace(column("last_column"), ordinal=100),
        ),
    )
    service = MetadataService(
        SourceRegistry([source]),
        StaticCatalog(
            replace(
                snapshot,
                relations=(snapshot.relations[0], case_file, snapshot.relations[2]),
            )
        ),
    )

    response = await service.get_context(source.source_id)
    relation = response["relations"][0]

    assert [item["name"] for item in relation["columns"]] == [
        "case_id",
        "reported_at",
    ]
    assert relation["columns_truncated"] is True
    assert response["truncated"] is True


@pytest.mark.asyncio
async def test_context_response_byte_limit_fails_closed() -> None:
    registry = load_test_registry()
    source = registry.get("query-cave")
    assert source is not None
    source = replace(
        source,
        budget=replace(source.budget, max_metadata_response_bytes=1),
    )
    service = MetadataService(
        SourceRegistry([source]),
        StaticCatalog(_described_snapshot()),
    )

    with pytest.raises(MetadataUnavailableError) as captured:
        await service.get_context(source.source_id)

    assert captured.value.details == {"contract_violations": ["Metadata response exceeds its byte limit."]}


@pytest.mark.parametrize("failure_type", [RuntimeError, ValueError])
@pytest.mark.asyncio
async def test_transient_refresh_failure_returns_bounded_stale_snapshot(
    failure_type: type[Exception],
) -> None:
    catalog = SequencedCatalog(
        _described_snapshot(),
        failure=failure_type("temporary catalog failure"),
    )
    service = MetadataService(
        load_test_registry(),
        catalog,
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )

    fresh = await service.get_context("query-cave")
    stale = await service.get_context("query-cave")
    stale_during_backoff = await service.get_context("query-cave")

    assert fresh["snapshot_status"] == "fresh"
    assert stale["snapshot_status"] == "stale"
    assert stale["metadata_revision"] == fresh["metadata_revision"]
    assert stale_during_backoff["snapshot_status"] == "stale"
    assert catalog.load_count == 2


@pytest.mark.asyncio
async def test_catalog_policy_rejection_never_returns_stale_snapshot() -> None:
    catalog = SequencedCatalog(
        _described_snapshot(),
        failure=_CatalogValidationError("Catalog relation limit exceeded"),
    )
    service = MetadataService(
        load_test_registry(),
        catalog,
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )
    await service.get_context("query-cave")

    with pytest.raises(MetadataUnavailableError):
        await service.get_context("query-cave")

    assert catalog.load_count == 2


@pytest.mark.asyncio
async def test_stale_snapshot_expires() -> None:
    clock = [1_000]
    service = MetadataService(
        load_test_registry(),
        SequencedCatalog(_described_snapshot()),
        cache_ttl_ms=0,
        max_stale_ms=10,
        now=lambda: clock[0],
    )
    await service.get_context("query-cave")
    clock[0] = 1_011

    with pytest.raises(MetadataUnavailableError):
        await service.get_context("query-cave")


@pytest.mark.asyncio
async def test_same_contract_version_rejects_structure_drift_without_stale() -> None:
    snapshot = _described_snapshot()
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
    await service.get_published("query-cave")

    with pytest.raises(MetadataUnavailableError) as captured:
        await service.get_published("query-cave")

    assert captured.value.details == {
        "contract_violations": ["View structure changed without a view contract version change."]
    }


@pytest.mark.asyncio
async def test_marker_drift_never_returns_warm_stale() -> None:
    snapshot = _described_snapshot()
    changed = replace(
        snapshot,
        relations=(
            replace(snapshot.relations[0], view_contract_source="another-source"),
            *snapshot.relations[1:],
        ),
    )
    service = MetadataService(
        load_test_registry(),
        SnapshotSequenceCatalog([snapshot, changed]),
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )
    await service.get_published("query-cave")

    with pytest.raises(MetadataUnavailableError):
        await service.get_published("query-cave")


@pytest.mark.asyncio
async def test_descriptive_change_rotates_revision_without_structure_rejection() -> None:
    snapshot = _described_snapshot()
    changed = replace(
        snapshot,
        relations=(
            replace(snapshot.relations[0], comment="Improved description"),
            *snapshot.relations[1:],
        ),
    )
    service = MetadataService(
        load_test_registry(),
        SnapshotSequenceCatalog([snapshot, changed]),
        cache_ttl_ms=0,
        now=lambda: 1_000,
    )

    first = await service.get_published("query-cave")
    second = await service.get_published("query-cave")

    assert second.revision != first.revision
