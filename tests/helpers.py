from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from query_man.metadata.models import CatalogColumn, CatalogRelation, CatalogSnapshot
from query_man.source_catalog.registry import SourceRegistry

ROOT_DIRECTORY = Path(__file__).resolve().parents[1]
DUMMY_ENVIRONMENT = {
    "POSTGRES_PORT": "5432",
    "DEVELOPMENT_ISSUES_READER_PASSWORD": "development-test-secret",
    "MARKET_VOC_READER_PASSWORD": "market-test-secret",
}


def load_test_registry(environment: Mapping[str, str] = DUMMY_ENVIRONMENT) -> SourceRegistry:
    return SourceRegistry.load(
        ROOT_DIRECTORY / "config" / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        environment,
    )


def column(name: str, data_type: str = "text") -> CatalogColumn:
    return CatalogColumn(name, f'"{name}"', 1, data_type, "unknown")


def relation(
    qualified_name: str,
    columns: Sequence[CatalogColumn],
    kind: str = "view",
) -> CatalogRelation:
    schema, name = qualified_name.split(".", 1)
    return CatalogRelation(
        schema=schema,
        name=name,
        qualified_name=qualified_name,
        sql_name=f'"{schema}"."{name}"',
        kind=kind,  # type: ignore[arg-type]
        columns=tuple(
            replace(item, ordinal=index) for index, item in enumerate(columns, 1)
        ),
        view_contract_source="development-issues",
        view_contract_version=1,
    )


def minimal_development_snapshot() -> CatalogSnapshot:
    return CatalogSnapshot(
        (
            relation(
                "ai.issue_comments",
                [
                    column("comment_id", "bigint"),
                    column("issue_id", "bigint"),
                    column("author_user_id"),
                    column("comment"),
                    column("commented_at", "timestamp with time zone"),
                    column("comment_type"),
                ],
            ),
            relation(
                "ai.issue_overview",
                [
                    column("issue_id", "bigint"),
                    column("discovered_at", "timestamp with time zone"),
                    column("reporter_user_id"),
                    column("assignee_user_id"),
                    column("problem_detail"),
                    column("cause"),
                    column("countermeasure"),
                    column("hw_version"),
                    column("sw_version"),
                    column("comment_count", "integer"),
                    column("issue_type"),
                    column("severity"),
                    column("status"),
                ],
            ),
            relation(
                "ai.test_unit_overview",
                [
                    column("test_unit_id", "bigint"),
                    column("manufactured_at", "date"),
                    column("serial_number"),
                    column("issue_count", "integer"),
                    column("unresolved_issue_count", "integer"),
                ],
            ),
        )
    )
