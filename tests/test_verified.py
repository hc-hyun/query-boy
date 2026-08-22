from pathlib import Path

import pytest

from query_man.verified import VerifiedQueryConfigurationError, VerifiedQueryRegistry
from tests.helpers import ROOT_DIRECTORY


def test_verified_registry_covers_every_mvp_golden_question() -> None:
    registry = VerifiedQueryRegistry.load(
        ROOT_DIRECTORY / "config" / "verified-queries.yaml",
        {"development-issues", "market-voc"},
    )

    assert len(registry.queries) == 9
    assert sum(query.source_id == "development-issues" for query in registry.queries) == 4
    assert sum(query.source_id == "market-voc" for query in registry.queries) == 5
    assert all(query.sql.strip().upper().startswith(("SELECT", "WITH")) for query in registry.queries)


def test_verified_registry_rejects_duplicate_ids(tmp_path: Path) -> None:
    query = """
  - query_id: duplicate-query
    source_id: development-issues
    question: deterministic question
    sql: SELECT count(*) FROM ai.issue_overview
    metadata_revision: sha256:0000000000000000000000000000000000000000000000000000000000000000
    relations: [ai.issue_overview]
    expected:
      columns: [count]
      row_count: 1
      result_hash: sha256:0000000000000000000000000000000000000000000000000000000000000000
"""
    path = tmp_path / "verified.yaml"
    path.write_text(f"version: 1\nqueries:\n{query}{query}", encoding="utf-8")

    with pytest.raises(VerifiedQueryConfigurationError):
        VerifiedQueryRegistry.load(path, {"development-issues"})


def test_verified_registry_rejects_unknown_source(tmp_path: Path) -> None:
    path = tmp_path / "verified.yaml"
    path.write_text(
        """
version: 1
queries:
  - query_id: unknown-source-query
    source_id: unknown-source
    question: deterministic question
    sql: SELECT count(*) FROM ai.issue_overview
    metadata_revision: sha256:0000000000000000000000000000000000000000000000000000000000000000
    relations: [ai.issue_overview]
    expected:
      columns: [count]
      row_count: 1
      result_hash: sha256:0000000000000000000000000000000000000000000000000000000000000000
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(VerifiedQueryConfigurationError, match="unknown source"):
        VerifiedQueryRegistry.load(path, {"development-issues"})
