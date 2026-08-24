from __future__ import annotations

import inspect
import os
from dataclasses import FrozenInstanceError, replace
from typing import get_type_hints

import pytest
from dotenv import load_dotenv
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo

from query_man.catalog import PostgresCatalog, _apply_structures
from query_man.metadata import MetadataService
from query_man.models import (
    CatalogForeignKey,
    CatalogIndex,
    CatalogProvider,
    CatalogSnapshot,
    PreparedMetadata,
    RuntimeCatalogProvider,
)
from tests.helpers import (
    ROOT_DIRECTORY,
    column,
    load_test_registry,
    minimal_development_snapshot,
    relation,
)


def test_runtime_catalog_provider_protocol_has_exact_lifecycle_shape() -> None:
    application_methods = {
        name
        for name, value in vars(CatalogProvider).items()
        if not name.startswith("_") and callable(value)
    }
    runtime_methods = {
        name
        for name, value in vars(RuntimeCatalogProvider).items()
        if not name.startswith("_") and callable(value)
    }

    assert application_methods == {"close", "load"}
    assert CatalogProvider in RuntimeCatalogProvider.__mro__
    assert runtime_methods == {"invalidate"}
    assert get_type_hints(RuntimeCatalogProvider.invalidate) == {
        "source_id": str,
        "return": type(None),
    }
    assert inspect.iscoroutinefunction(RuntimeCatalogProvider.invalidate)
    parameters = tuple(
        inspect.signature(RuntimeCatalogProvider.invalidate).parameters.values()
    )
    assert tuple(parameter.name for parameter in parameters) == ("self", "source_id")
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        and parameter.default is inspect.Parameter.empty
        for parameter in parameters
    )
    assert get_type_hints(MetadataService.__init__)["catalog"] is CatalogProvider


def test_published_catalog_graph_is_recursively_immutable_and_alias_free() -> None:
    base = relation("ai.example", [column("id")])
    columns = list(base.columns)
    primary_key = ["id"]
    foreign_key_columns = ["id"]
    referenced_columns = ["id"]
    index_columns = ["id"]
    foreign_keys = [
        CatalogForeignKey(
            foreign_key_columns,  # type: ignore[arg-type]
            "ai.example",
            referenced_columns,  # type: ignore[arg-type]
        )
    ]
    indexes = [
        CatalogIndex(index_columns, unique=True, primary=True)  # type: ignore[arg-type]
    ]
    published_relation = replace(  # type: ignore[arg-type]
        base,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
        indexes=indexes,
    )
    relations = [published_relation]
    snapshot = CatalogSnapshot(relations)  # type: ignore[arg-type]
    prepared = PreparedMetadata(snapshot, f"sha256:{'0' * 64}")

    columns.append(column("mutated"))
    primary_key.append("mutated")
    foreign_key_columns.append("mutated")
    referenced_columns.append("mutated")
    index_columns.append("mutated")
    foreign_keys.clear()
    indexes.clear()
    relations.clear()

    assert isinstance(snapshot.relations, tuple)
    assert isinstance(published_relation.columns, tuple)
    assert published_relation.primary_key == ("id",)
    assert published_relation.foreign_keys[0].columns == ("id",)
    assert published_relation.foreign_keys[0].referenced_columns == ("id",)
    assert published_relation.indexes[0].columns == ("id",)
    assert prepared.snapshot is snapshot
    with pytest.raises(FrozenInstanceError):
        snapshot.relations = ()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        published_relation.comment = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        published_relation.columns[0].name = "mutated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        prepared.revision = "mutated"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        published_relation.columns.append(column("mutated"))  # type: ignore[attr-defined]


def test_applies_primary_foreign_key_and_index_structures() -> None:
    relations = minimal_development_snapshot().relations
    relations = _apply_structures(
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
    assert by_name["ai.issue_overview"].primary_key == ("issue_id",)
    assert by_name["ai.issue_comments"].foreign_keys[0].referenced_relation == (
        "ai.issue_overview"
    )
    assert by_name["ai.issue_overview"].indexes[0].columns == ("discovered_at",)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_catalog_collects_only_simple_visible_table_structures() -> None:
    load_dotenv(ROOT_DIRECTORY / ".env")
    required = [
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DEVELOPMENT_ISSUES_READER_PASSWORD",
        "MARKET_VOC_READER_PASSWORD",
    ]
    if any(not os.environ.get(name) for name in required):
        pytest.skip("local PostgreSQL administrator credentials are not configured")
    source = load_test_registry(os.environ).get("development-issues")
    assert source is not None
    source = replace(
        source,
        allowed_schemas=("development",),
        allowed_relation_kinds=("table",),
    )
    admin = await AsyncConnection.connect(
        make_conninfo(
            host="127.0.0.1",
            port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname="development_issues",
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
            sslmode="disable",
        )
    )
    catalog = PostgresCatalog()
    try:
        await admin.execute(
            "GRANT USAGE ON SCHEMA development TO development_issues_reader"
        )
        await admin.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA development "
            "TO development_issues_reader"
        )
        await admin.commit()
        snapshot = await catalog.load(source)
    finally:
        await catalog.close()
        await admin.rollback()
        await admin.execute(
            "REVOKE SELECT ON ALL TABLES IN SCHEMA development "
            "FROM development_issues_reader"
        )
        await admin.execute(
            "REVOKE USAGE ON SCHEMA development FROM development_issues_reader"
        )
        await admin.commit()
        await admin.close()

    by_name = {relation.qualified_name: relation for relation in snapshot.relations}
    issues = by_name["development.issues"]
    assert issues.primary_key == ("id",)
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
