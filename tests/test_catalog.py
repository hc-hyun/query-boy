from __future__ import annotations

import os
from dataclasses import replace

import pytest
from dotenv import load_dotenv

from query_man.catalog import PostgresCatalog, _apply_structures
from query_man.models import ResolvedConnection
from tests.helpers import ROOT_DIRECTORY, load_test_registry, minimal_development_snapshot


def test_applies_primary_foreign_key_and_index_structures() -> None:
    relations = minimal_development_snapshot().relations
    _apply_structures(
        relations,
        [
            {
                "structure_kind": "primary_key",
                "schema_name": "ai",
                "relation_name": "issue_overview",
                "column_names": ["issue_id"],
                "referenced_relation": None,
                "referenced_columns": None,
                "is_unique": None,
                "is_primary": True,
            },
            {
                "structure_kind": "foreign_key",
                "schema_name": "ai",
                "relation_name": "issue_comments",
                "column_names": ["issue_id"],
                "referenced_relation": "ai.issue_overview",
                "referenced_columns": ["issue_id"],
                "is_unique": None,
                "is_primary": False,
            },
            {
                "structure_kind": "index",
                "schema_name": "ai",
                "relation_name": "issue_overview",
                "column_names": ["discovered_at"],
                "referenced_relation": None,
                "referenced_columns": None,
                "is_unique": False,
                "is_primary": False,
            },
        ],
    )
    by_name = {relation.qualified_name: relation for relation in relations}
    assert by_name["ai.issue_overview"].primary_key == ["issue_id"]
    assert by_name["ai.issue_comments"].foreign_keys[0].referenced_relation == (
        "ai.issue_overview"
    )
    assert by_name["ai.issue_overview"].indexes[0].columns == ["discovered_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_catalog_collects_only_simple_visible_table_structures() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = ["POSTGRES_USER", "POSTGRES_PASSWORD"]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL administrator credentials are not configured")
    source = load_test_registry().get("development-issues")
    assert source is not None
    source = replace(
        source,
        connection=ResolvedConnection(
            host="127.0.0.1",
            port=int(os.environ.get("POSTGRES_PORT", "5432")),
            database="development_issues",
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            ssl=False,
        ),
        allowed_schemas=["development"],
        allowed_relation_kinds=["table"],
    )
    catalog = PostgresCatalog()
    try:
        snapshot = await catalog.load(source)
    finally:
        await catalog.close()

    by_name = {relation.qualified_name: relation for relation in snapshot.relations}
    issues = by_name["development.issues"]
    assert issues.primary_key == ["id"]
    assert {
        (tuple(key.columns), key.referenced_relation, tuple(key.referenced_columns))
        for key in issues.foreign_keys
    } == {
        (("assignee_id",), "development.users", ("id",)),
        (("reporter_id",), "development.users", ("id",)),
        (("test_unit_id",), "development.test_units", ("id",)),
    }
    index_columns = {tuple(index.columns) for index in issues.indexes}
    assert ("id",) in index_columns
    assert ("discovered_at",) in index_columns
    assert ("status", "discovered_at") in index_columns
    assert ("assignee_id", "status") not in index_columns
