from dataclasses import replace
from pathlib import Path

import pytest

from query_man.errors import QueryUnavailableError
from query_man.models import PreparedMetadata
from query_man.query import QueryService
from query_man.registry import SourceRegistry
from query_man.verified import (
    ExpectedResult,
    VerifiedQuery,
    VerifiedQueryConfigurationError,
    VerifiedQueryRegistry,
)
from tests.helpers import (
    ROOT_DIRECTORY,
    load_test_registry,
    minimal_development_snapshot,
)


def test_verified_registry_covers_every_mvp_golden_question() -> None:
    registry = VerifiedQueryRegistry.load(
        ROOT_DIRECTORY / "config" / "verified-queries.yaml",
        {"development-issues", "market-voc"},
    )

    assert len(registry.queries) == 9
    assert sum(query.source_id == "development-issues" for query in registry.queries) == 4
    assert sum(query.source_id == "market-voc" for query in registry.queries) == 5
    assert all(query.sql.strip().upper().startswith(("SELECT", "WITH")) for query in registry.queries)


@pytest.mark.asyncio
async def test_offline_verification_supplies_no_tenant_and_rls_fails_closed() -> None:
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(source, tenant_isolation="rls")
    snapshot = minimal_development_snapshot()
    revision = f"sha256:{'0' * 64}"
    published = PreparedMetadata(snapshot, revision)

    class StaticMetadata:
        async def get_published(self, _source_id: str) -> PreparedMetadata:
            return published

    class NeverCalledExecutor:
        def __init__(self) -> None:
            self.called = False

        async def execute(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> dict[str, object]:
            self.called = True
            raise AssertionError("RLS verification reached the query executor")

        async def cancel(self, _query_id: str) -> bool:
            return False

        async def close(self) -> None:
            pass

    metadata = StaticMetadata()
    executor = NeverCalledExecutor()
    service = QueryService(SourceRegistry([source]), metadata, executor)  # type: ignore[arg-type]
    verified = VerifiedQueryRegistry(
        [
            VerifiedQuery(
                query_id="rls-offline-query",
                source_id=source.source_id,
                question="RLS source must fail closed",
                sql="SELECT count(*) AS issue_count FROM ai.issue_overview",
                metadata_revision=revision,
                relations=("ai.issue_overview",),
                expected=ExpectedResult(
                    columns=("issue_count",),
                    row_count=1,
                    result_hash=f"sha256:{'0' * 64}",
                ),
            )
        ]
    )

    with pytest.raises(QueryUnavailableError) as captured:
        await verified.verify_all(metadata, service)  # type: ignore[arg-type]

    assert captured.value.details is None
    assert not executor.called


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
