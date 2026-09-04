from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml
from pglast.parser import parse_sql_json

from tests.helpers import QUERY_CAVE_CONFIG_DIRECTORY, ROOT_DIRECTORY

SOURCE_ROOT = QUERY_CAVE_CONFIG_DIRECTORY / "sources"
SOURCE_PACKAGES = tuple(
    package
    for package in sorted(SOURCE_ROOT.iterdir())
    if package.is_dir()
)

ALLOWED_TOP_LEVEL_STATEMENTS = frozenset(
    {
        "AlterTableStmt",
        "CommentStmt",
        "GrantStmt",
        "TransactionStmt",
        "VariableSetStmt",
        "ViewStmt",
    }
)
FORBIDDEN_QUERY_NODES = frozenset(
    {
        "CallStmt",
        "CopyStmt",
        "CreateStmt",
        "CreateTableAsStmt",
        "DeleteStmt",
        "DoStmt",
        "DropStmt",
        "IndexStmt",
        "InsertStmt",
        "MergeStmt",
        "TruncateStmt",
        "UpdateStmt",
        "intoClause",
    }
)
FORBIDDEN_TEXT_PATTERNS = {
    "psql meta-command": r"(?m)^\s*\\",
    "broad relation grant": r"(?i)\bALL\s+(?:TABLES|SEQUENCES)\b",
    "future/default grant": r"(?i)\bALTER\s+DEFAULT\s+PRIVILEGES\b",
    "GRANT ALL": r"(?i)\bGRANT\s+ALL(?:\s+PRIVILEGES)?\b",
}
SECRET_PATTERNS = {
    "password": r"(?i)\b(?:[a-z][a-z0-9_-]*[-_])?password(?:[-_][a-z0-9]+)?\b",
    "token": r"(?i)\b(?:[a-z][a-z0-9_-]*[-_])?token\b",
    "DSN": r"(?i)\b(?:[a-z][a-z0-9_-]*[-_])?dsn\b|\b(?:jdbc:)?postgres(?:ql)?://",
    "private key": r"(?i)\bprivate[-_ ]+key\b|-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----",
    "secret": r"(?i)\b(?:[a-z][a-z0-9_-]*[-_])?secret(?:[-_][a-z0-9]+)?\b",
}


class AclOperation(NamedTuple):
    action: str
    object_type: str
    object_name: str
    grantee: str
    privilege: str


def _parse_statements(sql: str) -> list[tuple[str, dict[str, Any]]]:
    parsed = json.loads(parse_sql_json(sql))
    statements: list[tuple[str, dict[str, Any]]] = []
    for raw_statement in parsed["stmts"]:
        statement = raw_statement["stmt"]
        assert len(statement) == 1
        kind, payload = next(iter(statement.items()))
        assert isinstance(payload, dict)
        statements.append((kind, payload))
    return statements


def _find_ast_nodes(value: Any, kind: str) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        node = value.get(kind)
        if isinstance(node, dict):
            yield node
        for child in value.values():
            yield from _find_ast_nodes(child, kind)
    elif isinstance(value, list):
        for child in value:
            yield from _find_ast_nodes(child, kind)


def _ast_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        keys.update(value)
        for child in value.values():
            keys.update(_ast_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_ast_keys(child))
    return keys


def _list_strings(node: dict[str, Any]) -> tuple[str, ...]:
    items = node["List"]["items"]
    values = tuple(item["String"]["sval"] for item in items)
    assert all(isinstance(value, str) and value for value in values)
    return values


def _range_var_name(node: dict[str, Any]) -> str:
    schema = node.get("schemaname")
    relation = node.get("relname")
    assert not node.get("catalogname")
    assert isinstance(schema, str) and schema
    assert isinstance(relation, str) and relation
    return f"{schema}.{relation}"


def _grant_objects(statement: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    assert statement.get("targtype") == "ACL_TARGET_OBJECT"
    object_type = statement.get("objtype")
    objects = statement.get("objects")
    assert isinstance(objects, list) and objects
    if object_type == "OBJECT_SCHEMA":
        return tuple(("schema", item["String"]["sval"]) for item in objects)
    assert object_type == "OBJECT_TABLE"
    return tuple(("relation", _range_var_name(item["RangeVar"])) for item in objects)


def _grant_grantees(statement: dict[str, Any]) -> tuple[str, ...]:
    grantees = statement.get("grantees")
    assert isinstance(grantees, list) and grantees
    names: list[str] = []
    for grantee in grantees:
        role = grantee["RoleSpec"]
        if role.get("roletype") == "ROLESPEC_PUBLIC":
            assert not role.get("rolename")
            names.append("PUBLIC")
            continue
        assert role.get("roletype") == "ROLESPEC_CSTRING"
        name = role.get("rolename")
        assert isinstance(name, str) and name
        names.append(name)
    return tuple(names)


def _grant_privileges(statement: dict[str, Any]) -> tuple[str, ...]:
    privileges = statement.get("privileges")
    if not privileges:
        assert not statement.get("is_grant"), "GRANT ALL is not an exact privilege"
        return ("ALL",)
    names: list[str] = []
    for privilege in privileges:
        access = privilege["AccessPriv"]
        assert not access.get("cols"), "column-level grants are outside the approved surface"
        name = access.get("priv_name")
        assert isinstance(name, str) and name
        names.append(name.upper())
    return tuple(names)


def _grant_operations(statement: dict[str, Any]) -> list[AclOperation]:
    assert not statement.get("grant_option")
    assert not statement.get("grantor")
    assert statement.get("behavior", "DROP_RESTRICT") == "DROP_RESTRICT"
    action = "GRANT" if statement.get("is_grant") else "REVOKE"
    return [
        AclOperation(action, object_type, object_name, grantee, privilege)
        for object_type, object_name in _grant_objects(statement)
        for grantee in _grant_grantees(statement)
        for privilege in _grant_privileges(statement)
    ]


def _expected_acl_operations(
    *,
    published_schema: str,
    owner: str,
    reader: str,
    base_relations: set[str],
    views: set[str],
) -> Counter[AclOperation]:
    operations: list[AclOperation] = [
        AclOperation("REVOKE", "schema", published_schema, "PUBLIC", "ALL"),
        AclOperation("REVOKE", "schema", published_schema, owner, "ALL"),
        AclOperation("GRANT", "schema", published_schema, owner, "USAGE"),
        AclOperation("GRANT", "schema", published_schema, owner, "CREATE"),
        AclOperation("REVOKE", "schema", published_schema, owner, "CREATE"),
        AclOperation("REVOKE", "schema", published_schema, reader, "ALL"),
        AclOperation("GRANT", "schema", published_schema, reader, "USAGE"),
    ]
    for schema in sorted({relation.partition(".")[0] for relation in base_relations}):
        operations.extend(
            (
                AclOperation("REVOKE", "schema", schema, owner, "ALL"),
                AclOperation("GRANT", "schema", schema, owner, "USAGE"),
                AclOperation("REVOKE", "schema", schema, reader, "ALL"),
            )
        )
    for relation in sorted(base_relations):
        operations.extend(
            (
                AclOperation("REVOKE", "relation", relation, owner, "ALL"),
                AclOperation("GRANT", "relation", relation, owner, "SELECT"),
                AclOperation("REVOKE", "relation", relation, reader, "ALL"),
            )
        )
    for view in sorted(views):
        operations.extend(
            (
                AclOperation("REVOKE", "relation", view, "PUBLIC", "ALL"),
                AclOperation("REVOKE", "relation", view, reader, "ALL"),
                AclOperation("GRANT", "relation", view, reader, "SELECT"),
            )
        )
    return Counter(operations)


def _final_acl_surface(
    operations: list[AclOperation],
) -> dict[tuple[str, str, str], frozenset[str]]:
    surface: dict[tuple[str, str, str], set[str]] = {}
    for operation in operations:
        target = (operation.object_type, operation.object_name, operation.grantee)
        privileges = surface.setdefault(target, set())
        if operation.action == "REVOKE" and operation.privilege == "ALL":
            privileges.clear()
        elif operation.action == "REVOKE":
            privileges.discard(operation.privilege)
        else:
            privileges.add(operation.privilege)
    return {target: frozenset(privileges) for target, privileges in surface.items()}


def _expected_final_acl_surface(
    *,
    published_schema: str,
    owner: str,
    reader: str,
    base_relations: set[str],
    views: set[str],
) -> dict[tuple[str, str, str], frozenset[str]]:
    surface: dict[tuple[str, str, str], frozenset[str]] = {
        ("schema", published_schema, "PUBLIC"): frozenset(),
        ("schema", published_schema, owner): frozenset({"USAGE"}),
        ("schema", published_schema, reader): frozenset({"USAGE"}),
    }
    for schema in {relation.partition(".")[0] for relation in base_relations}:
        surface[("schema", schema, owner)] = frozenset({"USAGE"})
        surface[("schema", schema, reader)] = frozenset()
    for relation in base_relations:
        surface[("relation", relation, owner)] = frozenset({"SELECT"})
        surface[("relation", relation, reader)] = frozenset()
    for view in views:
        surface[("relation", view, "PUBLIC")] = frozenset()
        surface[("relation", view, reader)] = frozenset({"SELECT"})
    return surface


def test_query_cave_source_packages_match_reviewed_two_file_contract() -> None:
    source_entries = tuple(sorted(SOURCE_ROOT.iterdir()))
    assert source_entries

    for package in source_entries:
        assert package.is_dir(), f"Unexpected source entry: {package.relative_to(ROOT_DIRECTORY)}"
        assert {path.name for path in package.iterdir()} == {"source.yaml", "views.sql"}
        manifest = yaml.safe_load((package / "source.yaml").read_text(encoding="utf-8"))
        assert manifest["source_id"] == package.name
        assert set(manifest) >= {"database_profile", "reader_user"}


@pytest.mark.parametrize(
    "package",
    SOURCE_PACKAGES,
    ids=lambda package: str(package.relative_to(ROOT_DIRECTORY)),
)
def test_source_view_sql_is_a_bounded_standalone_desired_artifact(
    package: Path,
) -> None:
    manifest = yaml.safe_load((package / "source.yaml").read_text(encoding="utf-8"))
    sql = (package / "views.sql").read_text(encoding="utf-8")
    source_id = manifest["source_id"]
    version = manifest["view_contract_version"]
    published_schemas = manifest["allowed_schemas"]
    assert isinstance(published_schemas, list) and len(published_schemas) == 1
    published_schema = published_schemas[0]
    role_prefix = source_id.replace("-", "_")

    assert package.name == source_id
    assert manifest["version"] == 6
    assert manifest["allowed_relation_kinds"] == ["view"]
    assert isinstance(version, int) and not isinstance(version, bool) and version > 0
    assert sql.startswith("BEGIN;\n")
    assert sql.rstrip().endswith("COMMIT;")

    for label, pattern in {**FORBIDDEN_TEXT_PATTERNS, **SECRET_PATTERNS}.items():
        assert not re.search(pattern, sql), f"views.sql contains forbidden {label} material"

    statements = _parse_statements(sql)
    statement_kinds = [kind for kind, _statement in statements]
    unexpected_kinds = set(statement_kinds) - ALLOWED_TOP_LEVEL_STATEMENTS
    assert not unexpected_kinds, f"unsupported top-level SQL statements: {sorted(unexpected_kinds)}"

    transaction_kinds = [
        statement["kind"]
        for kind, statement in statements
        if kind == "TransactionStmt"
    ]
    assert transaction_kinds == ["TRANS_STMT_BEGIN", "TRANS_STMT_COMMIT"]
    assert statement_kinds[0] == "TransactionStmt"
    assert statement_kinds[-1] == "TransactionStmt"

    settings = []
    for kind, statement in statements:
        if kind != "VariableSetStmt":
            continue
        args = statement.get("args")
        assert isinstance(args, list) and len(args) == 1
        value = args[0]["A_Const"]["sval"]["sval"]
        settings.append((statement.get("name"), statement.get("is_local"), value))
    assert Counter(settings) == Counter(
        (
            ("search_path", True, "pg_catalog"),
            ("lock_timeout", True, "250ms"),
        )
    )

    view_statements = [statement for kind, statement in statements if kind == "ViewStmt"]
    assert view_statements
    views: dict[str, tuple[str, ...]] = {}
    for statement in view_statements:
        view = _range_var_name(statement["view"])
        assert view.startswith(f"{published_schema}.")
        assert statement.get("replace") is True
        aliases = tuple(alias["String"]["sval"] for alias in statement.get("aliases", ()))
        assert aliases and len(aliases) == len(set(aliases))
        assert view not in views
        views[view] = aliases

    base_relations: set[str] = set()
    for statement in view_statements:
        query = statement.get("query")
        assert isinstance(query, dict) and set(query) == {"SelectStmt"}
        forbidden_nodes = _ast_keys(query) & FORBIDDEN_QUERY_NODES
        assert not forbidden_nodes, f"view query contains write/DDL nodes: {sorted(forbidden_nodes)}"
        assert not tuple(_find_ast_nodes(query, "A_Star")), "view output must not use wildcard projection"

        select = query["SelectStmt"]
        view = _range_var_name(statement["view"])
        assert len(views[view]) == len(select.get("targetList", ()))
        cte_names = {
            node["ctename"]
            for node in _find_ast_nodes(query, "CommonTableExpr")
        }
        for relation in _find_ast_nodes(query, "RangeVar"):
            relation_name = relation.get("relname")
            assert isinstance(relation_name, str) and relation_name
            schema = relation.get("schemaname")
            if schema is None:
                assert relation_name in cte_names, f"unqualified base relation: {relation_name}"
                continue
            qualified_name = _range_var_name(relation)
            if schema == published_schema:
                assert qualified_name in views, f"view references unpublished relation: {qualified_name}"
            else:
                base_relations.add(qualified_name)
    assert base_relations

    view_comments: dict[str, str] = {}
    column_comments: set[tuple[str, str]] = set()
    for kind, statement in statements:
        if kind != "CommentStmt":
            continue
        objtype = statement.get("objtype")
        parts = _list_strings(statement["object"])
        comment = statement.get("comment")
        assert isinstance(comment, str) and comment.strip()
        if objtype == "OBJECT_VIEW":
            assert len(parts) == 2
            view = ".".join(parts)
            assert view in views and view not in view_comments
            view_comments[view] = comment
            continue
        assert objtype == "OBJECT_COLUMN"
        assert len(parts) == 3
        view = ".".join(parts[:2])
        column = parts[2]
        assert view in views and column in views[view]
        assert (view, column) not in column_comments
        column_comments.add((view, column))

    assert set(view_comments) == set(views)
    marker = f"query-man:source={source_id};view-contract={version}"
    for comment in view_comments.values():
        marker_line, separator, description = comment.partition("\n")
        assert separator and marker_line == marker
        assert description.strip()

    owner = f"{role_prefix}_view_owner"
    reader = f"{role_prefix}_reader"
    owned_views: set[str] = set()
    owner_change_positions: list[int] = []
    acl_operations: list[AclOperation] = []
    grant_create_positions: list[int] = []
    revoke_create_positions: list[int] = []
    for position, (kind, statement) in enumerate(statements):
        if kind == "AlterTableStmt":
            assert statement.get("objtype") == "OBJECT_VIEW"
            view = _range_var_name(statement["relation"])
            assert view in views and view not in owned_views
            commands = statement.get("cmds")
            assert isinstance(commands, list) and len(commands) == 1
            command = commands[0]["AlterTableCmd"]
            assert command.get("subtype") == "AT_ChangeOwner"
            new_owner = command.get("newowner")
            assert isinstance(new_owner, dict)
            assert new_owner == {
                "roletype": "ROLESPEC_CSTRING",
                "rolename": owner,
                "location": new_owner["location"],
            }
            owned_views.add(view)
            owner_change_positions.append(position)
        elif kind == "GrantStmt":
            operations = _grant_operations(statement)
            acl_operations.extend(operations)
            if AclOperation("GRANT", "schema", published_schema, owner, "CREATE") in operations:
                grant_create_positions.append(position)
            if AclOperation("REVOKE", "schema", published_schema, owner, "CREATE") in operations:
                revoke_create_positions.append(position)

    assert owned_views == set(views)
    assert len(grant_create_positions) == len(revoke_create_positions) == 1
    assert grant_create_positions[0] < min(owner_change_positions)
    assert max(owner_change_positions) < revoke_create_positions[0]

    actual_acl = Counter(acl_operations)
    expected_acl = _expected_acl_operations(
        published_schema=published_schema,
        owner=owner,
        reader=reader,
        base_relations=base_relations,
        views=set(views),
    )
    assert actual_acl == expected_acl, (
        f"ACL operations differ: missing={expected_acl - actual_acl}; "
        f"unexpected={actual_acl - expected_acl}"
    )
    assert _final_acl_surface(acl_operations) == _expected_final_acl_surface(
        published_schema=published_schema,
        owner=owner,
        reader=reader,
        base_relations=base_relations,
        views=set(views),
    )
