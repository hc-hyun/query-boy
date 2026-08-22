from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from pglast import ast, parse_sql
from pglast.parser import ParseError, fingerprint

DEFAULT_ALLOWED_FUNCTIONS = frozenset(
    {
        "abs",
        "age",
        "array_agg",
        "avg",
        "bit_and",
        "bit_or",
        "bool_and",
        "bool_or",
        "btrim",
        "ceil",
        "ceiling",
        "char_length",
        "concat",
        "concat_ws",
        "corr",
        "count",
        "covar_pop",
        "covar_samp",
        "date_part",
        "date_trunc",
        "every",
        "floor",
        "json_agg",
        "json_array_length",
        "json_extract_path_text",
        "json_object_agg",
        "jsonb_agg",
        "jsonb_array_length",
        "jsonb_extract_path_text",
        "jsonb_object_agg",
        "left",
        "length",
        "lower",
        "ltrim",
        "max",
        "min",
        "mod",
        "octet_length",
        "power",
        "regr_avgx",
        "regr_avgy",
        "regr_count",
        "regr_intercept",
        "regr_r2",
        "regr_slope",
        "replace",
        "right",
        "round",
        "rtrim",
        "split_part",
        "sqrt",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "string_agg",
        "substr",
        "substring",
        "sum",
        "to_char",
        "trunc",
        "upper",
        "variance",
        "var_pop",
        "var_samp",
    }
)

DEFAULT_ALLOWED_OPERATORS = frozenset(
    {
        "!=",
        "!~",
        "!~*",
        "!~~",
        "!~~*",
        "%",
        "&",
        "*",
        "+",
        "-",
        "/",
        "<",
        "<<",
        "<=",
        "<>",
        "=",
        ">",
        ">=",
        ">>",
        "#>",
        "#>>",
        "^",
        "|",
        "||",
        "~",
        "~*",
        "~~",
        "~~*",
        "->",
        "->>",
        "#-",
        "@>",
        "<@",
        "?",
        "?|",
        "?&",
        "@@",
        "@?",
    }
)

DEFAULT_ALLOWED_TYPES = frozenset(
    {
        "bit",
        "bool",
        "bpchar",
        "bytea",
        "date",
        "float4",
        "float8",
        "inet",
        "int2",
        "int4",
        "int8",
        "interval",
        "json",
        "jsonb",
        "numeric",
        "text",
        "time",
        "timestamp",
        "timestamptz",
        "timetz",
        "uuid",
        "varbit",
        "varchar",
    }
)

_ALLOWED_SQL_VALUE_FUNCTIONS = frozenset(
    {
        "SVFOP_CURRENT_DATE",
        "SVFOP_CURRENT_TIME",
        "SVFOP_CURRENT_TIME_N",
        "SVFOP_CURRENT_TIMESTAMP",
        "SVFOP_CURRENT_TIMESTAMP_N",
        "SVFOP_LOCALTIME",
        "SVFOP_LOCALTIME_N",
        "SVFOP_LOCALTIMESTAMP",
        "SVFOP_LOCALTIMESTAMP_N",
    }
)

_FORBIDDEN_NODE_CODES = {
    "ParamRef": "SQL_PARAMETER_NOT_ALLOWED",
    "RangeFunction": "SQL_TABLE_FUNCTION_NOT_ALLOWED",
    "RangeTableFunc": "SQL_TABLE_FUNCTION_NOT_ALLOWED",
    "RangeTableSample": "SQL_TABLESAMPLE_NOT_ALLOWED",
}

_ALLOWED_NODE_TAGS = frozenset(
    {
        "A_ArrayExpr",
        "A_Const",
        "A_Expr",
        "A_Indices",
        "A_Indirection",
        "A_Star",
        "Alias",
        "BitString",
        "Boolean",
        "BooleanTest",
        "BoolExpr",
        "CaseExpr",
        "CaseWhen",
        "CoalesceExpr",
        "CollateClause",
        "ColumnRef",
        "CommonTableExpr",
        "Float",
        "FuncCall",
        "GroupingFunc",
        "GroupingSet",
        "Integer",
        "JoinExpr",
        "MinMaxExpr",
        "NamedArgExpr",
        "Null",
        "NullTest",
        "ParamRef",
        "RangeFunction",
        "RangeSubselect",
        "RangeTableFunc",
        "RangeTableSample",
        "RangeVar",
        "ResTarget",
        "RowExpr",
        "SelectStmt",
        "SortBy",
        "SQLValueFunction",
        "String",
        "SubLink",
        "TypeCast",
        "TypeName",
        "WindowDef",
        "WithClause",
    }
)


@dataclass(frozen=True)
class ValidatedSql:
    fingerprint: str
    relations: tuple[str, ...]
    functions: tuple[str, ...]
    operators: tuple[str, ...]


class SqlValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def validate_sql(
    sql: str,
    *,
    allowed_relations: Iterable[str],
    allowed_functions: Iterable[str] = DEFAULT_ALLOWED_FUNCTIONS,
    allowed_operators: Iterable[str] = DEFAULT_ALLOWED_OPERATORS,
    allowed_types: Iterable[str] = DEFAULT_ALLOWED_TYPES,
    max_sql_bytes: int = 100_000,
) -> ValidatedSql:
    if not sql.strip():
        raise SqlValidationError("SQL_EMPTY", "SQL must contain one read-only SELECT statement.")
    if len(sql.encode("utf-8")) > max_sql_bytes:
        raise SqlValidationError("SQL_TOO_LARGE", "SQL exceeds the configured byte limit.")

    try:
        statements = parse_sql(sql)
    except ParseError as error:
        raise SqlValidationError("SQL_PARSE_ERROR", "SQL could not be parsed.") from error
    if len(statements) != 1:
        raise SqlValidationError(
            "SQL_MULTIPLE_STATEMENTS",
            "Exactly one SQL statement is required.",
        )
    statement = statements[0].stmt
    if not isinstance(statement, ast.SelectStmt):
        raise SqlValidationError(
            "SQL_STATEMENT_NOT_ALLOWED",
            "Only a read-only SELECT statement is allowed.",
        )

    tree = statement(skip_none=True)
    policy = _ValidationPolicy(
        allowed_relations=frozenset(allowed_relations),
        allowed_functions=frozenset(value.casefold() for value in allowed_functions),
        allowed_operators=frozenset(allowed_operators),
        allowed_types=frozenset(value.casefold() for value in allowed_types),
    )
    policy.validate(tree)
    return ValidatedSql(
        fingerprint=f"pg_query:{fingerprint(sql)}",
        relations=tuple(sorted(policy.relations)),
        functions=tuple(sorted(policy.functions)),
        operators=tuple(sorted(policy.operators)),
    )


class _ValidationPolicy:
    def __init__(
        self,
        *,
        allowed_relations: frozenset[str],
        allowed_functions: frozenset[str],
        allowed_operators: frozenset[str],
        allowed_types: frozenset[str],
    ) -> None:
        self.allowed_relations = allowed_relations
        self.allowed_functions = allowed_functions
        self.allowed_operators = allowed_operators
        self.allowed_types = allowed_types
        self.relations: set[str] = set()
        self.functions: set[str] = set()
        self.operators: set[str] = set()

    def validate(self, tree: Mapping[str, Any]) -> None:
        for node in _walk_nodes(tree):
            tag = str(node.get("@", ""))
            if tag.endswith("Stmt") and tag != "SelectStmt":
                raise SqlValidationError(
                    "SQL_NESTED_STATEMENT_NOT_ALLOWED",
                    "Only read-only SELECT statements are allowed inside CTEs.",
                )
            forbidden_code = _FORBIDDEN_NODE_CODES.get(tag)
            if forbidden_code:
                raise SqlValidationError(forbidden_code, "The SQL construct is not allowed.")
            if tag not in _ALLOWED_NODE_TAGS:
                raise SqlValidationError(
                    "SQL_CONSTRUCT_NOT_ALLOWED",
                    "The SQL contains a construct that has not been approved.",
                )
            handler = getattr(self, f"_validate_{tag}", None)
            if handler is not None:
                handler(node)
        self._validate_select_scope(tree, frozenset())

    def _validate_SelectStmt(self, node: Mapping[str, Any]) -> None:
        if node.get("intoClause") is not None:
            raise SqlValidationError(
                "SQL_SELECT_INTO_NOT_ALLOWED",
                "SELECT INTO is not allowed.",
            )
        if node.get("lockingClause"):
            raise SqlValidationError(
                "SQL_ROW_LOCK_NOT_ALLOWED",
                "Row locking clauses are not allowed.",
            )

    def _validate_WithClause(self, node: Mapping[str, Any]) -> None:
        if node.get("recursive"):
            raise SqlValidationError(
                "SQL_RECURSIVE_CTE_NOT_ALLOWED",
                "Recursive CTEs are not allowed.",
            )

    def _validate_range_var(self, node: Mapping[str, Any], visible_ctes: frozenset[str]) -> None:
        catalog = node.get("catalogname")
        schema = node.get("schemaname")
        relation = str(node.get("relname", ""))
        if catalog:
            raise SqlValidationError(
                "SQL_CROSS_DATABASE_REFERENCE",
                "Cross-database relation references are not allowed.",
            )
        if not schema:
            if relation in visible_ctes:
                return
            raise SqlValidationError(
                "SQL_RELATION_MUST_BE_QUALIFIED",
                "Physical relations must use a schema-qualified name.",
            )
        qualified = f"{schema}.{relation}"
        if qualified not in self.allowed_relations:
            raise SqlValidationError(
                "SQL_RELATION_NOT_ALLOWED",
                "The SQL references a relation that is not published for this source.",
            )
        self.relations.add(qualified)

    def _validate_select_scope(self, node: Mapping[str, Any], outer_ctes: frozenset[str]) -> None:
        visible = set(outer_ctes)
        with_clause = node.get("withClause")
        if isinstance(with_clause, Mapping):
            ctes = with_clause.get("ctes")
            if isinstance(ctes, (tuple, list)):
                for cte in ctes:
                    if not isinstance(cte, Mapping):
                        continue
                    query = cte.get("ctequery")
                    if isinstance(query, Mapping) and query.get("@") == "SelectStmt":
                        self._validate_select_scope(query, frozenset(visible))
                    name = cte.get("ctename")
                    if name:
                        visible.add(str(name))
        for key, value in node.items():
            if key != "withClause":
                self._validate_scoped_value(value, frozenset(visible))

    def _validate_scoped_value(self, value: Any, visible_ctes: frozenset[str]) -> None:
        if isinstance(value, Mapping):
            tag = value.get("@")
            if tag == "RangeVar":
                self._validate_range_var(value, visible_ctes)
                return
            if tag == "SelectStmt":
                self._validate_select_scope(value, visible_ctes)
                return
            for item in value.values():
                self._validate_scoped_value(item, visible_ctes)
        elif isinstance(value, (tuple, list)):
            for item in value:
                self._validate_scoped_value(item, visible_ctes)

    def _validate_FuncCall(self, node: Mapping[str, Any]) -> None:
        parts = _string_parts(node.get("funcname"))
        if not parts:
            raise SqlValidationError("SQL_FUNCTION_NOT_ALLOWED", "The SQL function is not allowed.")
        if len(parts) == 1:
            function = parts[0].casefold()
        elif len(parts) == 2 and parts[0].casefold() == "pg_catalog":
            function = parts[1].casefold()
        else:
            raise SqlValidationError(
                "SQL_FUNCTION_SCHEMA_NOT_ALLOWED",
                "Only approved built-in functions are allowed.",
            )
        if function not in self.allowed_functions:
            raise SqlValidationError(
                "SQL_FUNCTION_NOT_ALLOWED",
                "The SQL function is not approved.",
            )
        self.functions.add(f"pg_catalog.{function}")

    def _validate_A_Expr(self, node: Mapping[str, Any]) -> None:
        parts = _string_parts(node.get("name"))
        if not parts:
            return
        if len(parts) != 1 or parts[0] not in self.allowed_operators:
            raise SqlValidationError(
                "SQL_OPERATOR_NOT_ALLOWED",
                "The SQL operator is not approved.",
            )
        self.operators.add(parts[0])

    def _validate_TypeName(self, node: Mapping[str, Any]) -> None:
        parts = _string_parts(node.get("names"))
        if not parts:
            return
        if len(parts) != 2 or parts[0].casefold() != "pg_catalog":
            raise SqlValidationError(
                "SQL_TYPE_NOT_ALLOWED",
                "Only approved built-in cast types are allowed.",
            )
        if parts[1].casefold() not in self.allowed_types:
            raise SqlValidationError(
                "SQL_TYPE_NOT_ALLOWED",
                "The cast type is not approved.",
            )

    def _validate_SQLValueFunction(self, node: Mapping[str, Any]) -> None:
        operation = node.get("op")
        name = operation.get("name") if isinstance(operation, Mapping) else None
        if name not in _ALLOWED_SQL_VALUE_FUNCTIONS:
            raise SqlValidationError(
                "SQL_VALUE_FUNCTION_NOT_ALLOWED",
                "The SQL value function is not approved.",
            )

    def _validate_CollateClause(self, node: Mapping[str, Any]) -> None:
        raise SqlValidationError(
            "SQL_COLLATION_NOT_ALLOWED",
            "Explicit collations are not allowed.",
        )


def _walk_nodes(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if "@" in value:
            yield value
        for item in value.values():
            yield from _walk_nodes(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _walk_nodes(item)


def _string_parts(value: Any) -> list[str]:
    if not isinstance(value, (tuple, list)):
        return []
    return [
        str(item["sval"])
        for item in value
        if isinstance(item, Mapping) and item.get("@") == "String" and "sval" in item
    ]
