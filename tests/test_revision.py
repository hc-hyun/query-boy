from dataclasses import replace
from types import MappingProxyType

import pytest

import query_man.guarded_query.sql_validation as sql_validation_module
import query_man.metadata.revision as revision_module
from query_man.guarded_query.result_encoding import CANONICAL_TIME_POLICY_MATERIAL
from query_man.metadata.revision import (
    create_metadata_revision,
    create_view_structure_signature,
)
from tests.helpers import load_test_registry, minimal_development_snapshot


def test_revision_is_stable_across_catalog_order() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    first = minimal_development_snapshot()
    second = minimal_development_snapshot()
    second = replace(
        second,
        relations=tuple(reversed(second.relations)),
    )

    assert create_metadata_revision(source, first) == create_metadata_revision(
        source,
        second,
    )


def test_revision_changes_with_every_published_query_contract_input() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    relation = snapshot.relations[0]
    column = relation.columns[0]
    baseline = create_metadata_revision(source, snapshot)
    source_variants = (
        replace(source, description="Changed source description"),
        replace(source, view_contract_version=source.view_contract_version + 1),
        replace(
            source,
            budget=replace(
                source.budget,
                max_result_rows=source.budget.max_result_rows - 1,
            ),
        ),
    )
    snapshot_variants = (
        replace(
            snapshot,
            relations=(replace(relation, comment="Changed"), *snapshot.relations[1:]),
        ),
        replace(
            snapshot,
            relations=(
                replace(relation, definition_hash="changed"),
                *snapshot.relations[1:],
            ),
        ),
        replace(
            snapshot,
            relations=(
                replace(relation, security_barrier=True),
                *snapshot.relations[1:],
            ),
        ),
        replace(
            snapshot,
            relations=(
                replace(
                    relation,
                    columns=(replace(column, data_type="numeric"), *relation.columns[1:]),
                ),
                *snapshot.relations[1:],
            ),
        ),
    )

    assert all(create_metadata_revision(variant, snapshot) != baseline for variant in source_variants)
    assert all(create_metadata_revision(source, variant) != baseline for variant in snapshot_variants)


def test_revision_ignores_transport_provenance_and_admission_markers() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    baseline = create_metadata_revision(source, snapshot)
    source_variants = (
        replace(source, connection=replace(source.connection, sslmode="require")),
        replace(source, provenance=replace(source.provenance, owner="another-owner")),
        replace(
            source,
            provenance=replace(source.provenance, environment="production"),
        ),
    )
    structure_only = replace(
        snapshot,
        relations=(
            replace(
                snapshot.relations[0],
                view_contract_source="another-source",
                view_contract_version=999,
            ),
            *snapshot.relations[1:],
        ),
    )

    assert all(create_metadata_revision(variant, snapshot) == baseline for variant in source_variants)
    assert create_metadata_revision(source, structure_only) == baseline


def test_revision_changes_with_canonical_time_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    baseline = create_metadata_revision(source, snapshot)
    changed = dict(revision_module.CANONICAL_TIME_POLICY_MATERIAL)
    changed["reader_session_timezone"] = "Asia/Seoul"
    monkeypatch.setattr(
        revision_module,
        "CANONICAL_TIME_POLICY_MATERIAL",
        MappingProxyType(changed),
    )

    assert create_metadata_revision(source, snapshot) != baseline


def test_sql_and_metadata_revisions_share_canonical_time_material() -> None:
    assert revision_module.CANONICAL_TIME_POLICY_MATERIAL is (CANONICAL_TIME_POLICY_MATERIAL)
    assert sql_validation_module.CANONICAL_TIME_POLICY_MATERIAL is (CANONICAL_TIME_POLICY_MATERIAL)


def test_view_structure_signature_tracks_exact_query_surface() -> None:
    snapshot = minimal_development_snapshot()
    relation = snapshot.relations[0]
    first_column = relation.columns[0]
    baseline = create_view_structure_signature(snapshot)
    variants = (
        replace(snapshot, relations=snapshot.relations[1:]),
        replace(
            snapshot,
            relations=(
                replace(relation, definition_hash="changed"),
                *snapshot.relations[1:],
            ),
        ),
        replace(
            snapshot,
            relations=(
                replace(relation, columns=tuple(reversed(relation.columns))),
                *snapshot.relations[1:],
            ),
        ),
        replace(
            snapshot,
            relations=(
                replace(
                    relation,
                    columns=(
                        replace(first_column, nullable=False),
                        *relation.columns[1:],
                    ),
                ),
                *snapshot.relations[1:],
            ),
        ),
        replace(
            snapshot,
            relations=(
                replace(relation, security_invoker=True),
                *snapshot.relations[1:],
            ),
        ),
    )

    assert all(create_view_structure_signature(variant) != baseline for variant in variants)


def test_view_structure_signature_ignores_descriptive_and_marker_data() -> None:
    snapshot = minimal_development_snapshot()
    relation = snapshot.relations[0]
    descriptive = replace(
        snapshot,
        relations=(
            replace(
                relation,
                comment="Changed description",
                view_contract_source="another-source",
                view_contract_version=999,
            ),
            *snapshot.relations[1:],
        ),
    )

    assert create_view_structure_signature(descriptive) == (create_view_structure_signature(snapshot))
