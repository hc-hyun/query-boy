from copy import deepcopy
from dataclasses import replace

from query_man.models import CatalogForeignKey, CatalogIndex
from query_man.revision import create_metadata_revision
from tests.helpers import load_test_registry, minimal_development_snapshot


def test_revision_is_stable_across_catalog_order_and_estimates() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    first = minimal_development_snapshot()
    first.relations[0].estimated_rows = 10
    second = minimal_development_snapshot()
    second.relations.reverse()
    second.relations[0].estimated_rows = 99_999
    assert create_metadata_revision(source, first) == create_metadata_revision(source, second)


def test_revision_changes_with_comment() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    first = minimal_development_snapshot()
    second = minimal_development_snapshot()
    second.relations[0].comment = "Changed semantics"
    assert create_metadata_revision(source, first) != create_metadata_revision(source, second)


def test_revision_preserves_physical_key_and_index_column_order() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    baseline = minimal_development_snapshot()
    relation = baseline.relations[0]
    relation.primary_key = ["comment_id", "issue_id"]
    relation.foreign_keys = [
        CatalogForeignKey(
            columns=["comment_id", "issue_id"],
            referenced_relation="ai.issue_overview",
            referenced_columns=["issue_id", "discovered_at"],
        )
    ]
    relation.indexes = [
        CatalogIndex(columns=["comment_id", "issue_id"], unique=False, primary=False)
    ]

    reversed_primary = deepcopy(baseline)
    reversed_primary.relations[0].primary_key.reverse()
    reversed_reference = deepcopy(baseline)
    reversed_reference.relations[0].foreign_keys[0].referenced_columns.reverse()
    reversed_index = deepcopy(baseline)
    reversed_index.relations[0].indexes[0].columns.reverse()
    revision = create_metadata_revision(source, baseline)

    assert create_metadata_revision(source, reversed_primary) != revision
    assert create_metadata_revision(source, reversed_reference) != revision
    assert create_metadata_revision(source, reversed_index) != revision


def test_revision_changes_with_execution_budget() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    stricter = replace(
        source,
        budget=replace(source.budget, max_result_rows=source.budget.max_result_rows - 1),
    )

    assert create_metadata_revision(source, snapshot) != create_metadata_revision(
        stricter,
        snapshot,
    )


def test_revision_ignores_source_provenance() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    snapshot = minimal_development_snapshot()
    revision = create_metadata_revision(source, snapshot)
    changed_owner = replace(
        source,
        provenance=replace(source.provenance, owner="another-owner"),
    )
    changed_migration = replace(
        source,
        provenance=replace(
            source.provenance,
            database_migration_ref="migrations/9999_replacement.sql",
        ),
    )
    changed_environment = replace(
        source,
        provenance=replace(source.provenance, environment="production"),
    )

    assert create_metadata_revision(changed_owner, snapshot) == revision
    assert create_metadata_revision(changed_migration, snapshot) == revision
    assert create_metadata_revision(changed_environment, snapshot) == revision
