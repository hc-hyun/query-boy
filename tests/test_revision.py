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
