from dataclasses import replace
from types import MappingProxyType

from query_man.models import CatalogForeignKey, CatalogIndex
from query_man.revision import _canonicalize, create_metadata_revision
from tests.helpers import load_test_registry, minimal_development_snapshot


def test_revision_is_stable_across_catalog_order_and_estimates() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    first = minimal_development_snapshot()
    first = replace(
        first,
        relations=(
            replace(first.relations[0], estimated_rows=10),
            *first.relations[1:],
        ),
    )
    second = minimal_development_snapshot()
    reversed_relations = tuple(reversed(second.relations))
    second = replace(
        second,
        relations=(
            replace(reversed_relations[0], estimated_rows=99_999),
            *reversed_relations[1:],
        ),
    )
    assert create_metadata_revision(source, first) == create_metadata_revision(source, second)


def test_revision_changes_with_comment() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    first = minimal_development_snapshot()
    second = minimal_development_snapshot()
    second = replace(
        second,
        relations=(
            replace(second.relations[0], comment="Changed semantics"),
            *second.relations[1:],
        ),
    )
    assert create_metadata_revision(source, first) != create_metadata_revision(source, second)


def test_revision_preserves_physical_key_and_index_column_order() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    baseline = minimal_development_snapshot()
    relation = replace(
        baseline.relations[0],
        primary_key=("comment_id", "issue_id"),
        foreign_keys=(
            CatalogForeignKey(
                columns=("comment_id", "issue_id"),
                referenced_relation="ai.issue_overview",
                referenced_columns=("issue_id", "discovered_at"),
            ),
        ),
        indexes=(
            CatalogIndex(
                columns=("comment_id", "issue_id"),
                unique=False,
                primary=False,
            ),
        ),
    )
    baseline = replace(
        baseline,
        relations=(relation, *baseline.relations[1:]),
    )

    reversed_primary = replace(
        baseline,
        relations=(
            replace(relation, primary_key=tuple(reversed(relation.primary_key))),
            *baseline.relations[1:],
        ),
    )
    key = relation.foreign_keys[0]
    reversed_reference = replace(
        baseline,
        relations=(
            replace(
                relation,
                foreign_keys=(
                    CatalogForeignKey(
                        columns=key.columns,
                        referenced_relation=key.referenced_relation,
                        referenced_columns=tuple(reversed(key.referenced_columns)),
                    ),
                ),
            ),
            *baseline.relations[1:],
        ),
    )
    index = relation.indexes[0]
    reversed_index = replace(
        baseline,
        relations=(
            replace(
                relation,
                indexes=(
                    CatalogIndex(
                        columns=tuple(reversed(index.columns)),
                        unique=index.unique,
                        primary=index.primary,
                    ),
                ),
            ),
            *baseline.relations[1:],
        ),
    )
    revision = create_metadata_revision(source, baseline)

    assert create_metadata_revision(source, reversed_primary) != revision
    assert create_metadata_revision(source, reversed_reference) != revision
    assert create_metadata_revision(source, reversed_index) != revision


def test_revision_matches_pre_immutability_golden() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None

    assert create_metadata_revision(source, minimal_development_snapshot()) == (
        "sha256:753f2d1e3f1e5f62de423e9180cb71dc2aed1869d5e4a9b5bd8da9955bad632b"
    )


def test_canonicalizer_treats_mutable_and_immutable_containers_identically() -> None:
    mutable = {
        "relations": [
            {"name": "second", "columns": ["z", "a"]},
            {"name": "first", "columns": ["b", "a"]},
        ]
    }
    immutable = MappingProxyType(
        {
            "relations": (
                MappingProxyType({"name": "second", "columns": ("z", "a")}),
                MappingProxyType({"name": "first", "columns": ("b", "a")}),
            )
        }
    )

    assert _canonicalize(immutable) == _canonicalize(mutable)


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
