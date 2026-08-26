from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pglast.parser import get_postgresql_version

import query_man.sql_validation as sql_validation_module
from query_man.reader_policy import READER_CLIENT_ENCODING
from query_man.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DEFAULT_ALLOWED_TYPES,
    DEFAULT_ALLOWED_UNQUALIFIED_TYPES,
    SQL_POLICY_REVISION,
    SqlValidationError,
    validate_sql,
)

ALLOWED_RELATIONS = {
    "ai.issue_comments",
    "ai.issue_overview",
    "ai.test_unit_overview",
}


def test_parser_matches_postgresql_18_grammar() -> None:
    assert get_postgresql_version()[0] == 18


def test_sql_policy_revision_is_a_stable_digest() -> None:
    assert SQL_POLICY_REVISION == (
        "sha256:2e94db36095f11f2e9cc4e804666598f79a2ee956002ffa60dbe26bc6ee81388"
    )
    assert SQL_POLICY_REVISION != (
        "sha256:6b68458319a21416e51bf4be059fc55c4e053b45e38e7219956c4ac3725637a6"
    )
    assert SQL_POLICY_REVISION != (
        "sha256:83729139d7ccedbe8e299b0c4a8bdefb97d42ca870d5fc3b9c227578c65855d9"
    )


def test_sql_policy_v3_connection_material_is_exact_and_immutable() -> None:
    material = sql_validation_module._READER_CONNECTION_POLICY_MATERIAL

    assert dict(material) == {
        "version": 1,
        "postgresql_major": 18,
        "server_encoding": READER_CLIENT_ENCODING,
        "client_encoding": READER_CLIENT_ENCODING,
        "driver_encoding": "utf-8",
    }
    with pytest.raises(TypeError):
        material["version"] = 2  # type: ignore[index]


def test_accepts_question_answering_select_and_extracts_dependencies() -> None:
    result = validate_sql(
        """
        SELECT
          date_trunc('month', discovered_at) AS month,
          count(*) AS issue_count,
          sum(comment_count) AS comment_count
        FROM ai.issue_overview
        WHERE status <> 'RESOLVED'
        GROUP BY 1
        ORDER BY 1
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("ai.issue_overview",)
    assert result.functions == (
        "pg_catalog.count",
        "pg_catalog.date_trunc",
        "pg_catalog.sum",
    )
    assert result.operators == ("<>",)
    assert result.fingerprint.startswith("pg_query:")


def test_accepts_non_recursive_read_only_cte() -> None:
    result = validate_sql(
        """
        WITH recent AS (
          SELECT issue_id, comment_count
          FROM ai.issue_overview
          WHERE discovered_at >= current_date - interval '90 days'
        )
        SELECT count(*), sum(comment_count)
        FROM recent
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("ai.issue_overview",)


def test_accepts_date_between_and_records_effective_comparison_operators() -> None:
    result = validate_sql(
        """
        SELECT *
        FROM ai.issue_overview
        WHERE discovered_on BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("ai.issue_overview",)
    assert result.operators == ("<=", ">=")


def test_between_requires_both_effective_comparison_operators() -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            "SELECT 2 BETWEEN 1 AND 3",
            allowed_relations=ALLOWED_RELATIONS,
            allowed_operators={">="},
        )

    assert captured.value.code == "SQL_OPERATOR_NOT_ALLOWED"
    assert captured.value.rejected_construct == "BETWEEN"


@pytest.mark.parametrize(
    ("expression", "rejected_construct"),
    [
        ("2 NOT BETWEEN 1 AND 3", "NOT BETWEEN"),
        ("2 BETWEEN SYMMETRIC 1 AND 3", "BETWEEN SYMMETRIC"),
        ("2 NOT BETWEEN SYMMETRIC 1 AND 3", "NOT BETWEEN SYMMETRIC"),
    ],
)
def test_rejects_unapproved_between_variants(
    expression: str,
    rejected_construct: str,
) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(f"SELECT {expression}", allowed_relations=ALLOWED_RELATIONS)

    assert captured.value.code == "SQL_OPERATOR_NOT_ALLOWED"
    assert captured.value.rejected_construct == rejected_construct


@pytest.mark.parametrize(
    ("sql", "type_name"),
    [
        ("SELECT DATE '2026-05-01'", "date"),
        ("SELECT 'comment'::text", "text"),
    ],
)
def test_unqualified_date_and_text_casts_honor_the_type_allowlist(
    sql: str,
    type_name: str,
) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            sql,
            allowed_relations=ALLOWED_RELATIONS,
            allowed_types=DEFAULT_ALLOWED_TYPES - {type_name},
        )

    assert captured.value.code == "SQL_TYPE_NOT_ALLOWED"


def test_accepts_unqualified_date_and_text_casts() -> None:
    result = validate_sql(
        """
        SELECT
          CAST(discovered_at AS date) AS discovered_on,
          issue_id::text AS issue_id_text
        FROM ai.issue_overview
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert DEFAULT_ALLOWED_UNQUALIFIED_TYPES == frozenset({"date", "text"})
    assert result.relations == ("ai.issue_overview",)


@pytest.mark.parametrize(
    "type_name",
    ["ai.date", "ai.text", "pg_temp.text", '"PG_CATALOG".date', '"PG_CATALOG".text'],
)
def test_rejects_untrusted_schema_qualified_date_and_text_casts(type_name: str) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            f"SELECT '2026-05-01'::{type_name}",
            allowed_relations=ALLOWED_RELATIONS,
        )

    assert captured.value.code == "SQL_TYPE_NOT_ALLOWED"


@pytest.mark.parametrize(
    ("expression", "function"),
    [
        ("rank() OVER (ORDER BY issue_id)", "rank"),
        ("lag(status) OVER (ORDER BY discovered_at)", "lag"),
        ("lead(status) OVER (ORDER BY discovered_at)", "lead"),
        ("extract(year FROM discovered_at)", "extract"),
        ("regexp_replace(title, '[0-9]+', '#', 'g')", "regexp_replace"),
        ("position('error' IN title)", "position"),
        (
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY comment_count)",
            "percentile_cont",
        ),
        ("dense_rank() OVER (ORDER BY severity)", "dense_rank"),
        (
            "jsonb_build_object('id', issue_id, 'status', status)",
            "jsonb_build_object",
        ),
        ("to_jsonb(issue_id)", "to_jsonb"),
    ],
)
def test_accepts_common_query_functions_and_records_resolved_dependency(
    expression: str,
    function: str,
) -> None:
    result = validate_sql(
        f"SELECT {expression} FROM ai.issue_overview",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.functions == (f"pg_catalog.{function}",)


@pytest.mark.parametrize(
    ("expression", "function"),
    [
        ("rank() OVER (ORDER BY issue_id)", "rank"),
        ("lag(status) OVER (ORDER BY discovered_at)", "lag"),
        ("lead(status) OVER (ORDER BY discovered_at)", "lead"),
        ("extract(year FROM discovered_at)", "extract"),
        ("regexp_replace(title, '[0-9]+', '#', 'g')", "regexp_replace"),
        ("position('error' IN title)", "position"),
        (
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY comment_count)",
            "percentile_cont",
        ),
        ("dense_rank() OVER (ORDER BY severity)", "dense_rank"),
        (
            "jsonb_build_object('id', issue_id, 'status', status)",
            "jsonb_build_object",
        ),
        ("to_jsonb(issue_id)", "to_jsonb"),
    ],
)
def test_common_query_functions_honor_the_function_allowlist(
    expression: str,
    function: str,
) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            f"SELECT {expression} FROM ai.issue_overview",
            allowed_relations=ALLOWED_RELATIONS,
            allowed_functions=DEFAULT_ALLOWED_FUNCTIONS - {function},
        )

    assert captured.value.code == "SQL_FUNCTION_NOT_ALLOWED"


@pytest.mark.parametrize("schema", ["ai", '"PG_CATALOG"'])
@pytest.mark.parametrize(
    "expression_template",
    [
        "{schema}.rank() OVER (ORDER BY issue_id)",
        "{schema}.lag(status) OVER (ORDER BY discovered_at)",
        "{schema}.lead(status) OVER (ORDER BY discovered_at)",
        "{schema}.extract('year', discovered_at)",
        "{schema}.regexp_replace(title, '[0-9]+', '#', 'g')",
        "{schema}.position('error', title)",
        "{schema}.percentile_cont(0.5) WITHIN GROUP (ORDER BY comment_count)",
        "{schema}.dense_rank() OVER (ORDER BY severity)",
        "{schema}.jsonb_build_object('id', issue_id, 'status', status)",
        "{schema}.to_jsonb(issue_id)",
    ],
)
def test_rejects_untrusted_schema_qualified_common_functions(
    expression_template: str,
    schema: str,
) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            f"SELECT {expression_template.format(schema=schema)} FROM ai.issue_overview",
            allowed_relations=ALLOWED_RELATIONS,
        )

    assert captured.value.code == "SQL_FUNCTION_SCHEMA_NOT_ALLOWED"


@pytest.mark.parametrize(
    "function_call",
    [
        "regexp_replace('abc123', '[0-9]+', '#', 'g')",
        "position('b' IN 'abc')",
        "percentile_cont(0.5)",
        "dense_rank()",
        "jsonb_build_object('id', 1)",
        "to_jsonb(1)",
    ],
)
def test_rejects_approved_functions_as_table_functions(
    function_call: str,
) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            f"SELECT * FROM {function_call}",
            allowed_relations=ALLOWED_RELATIONS,
        )

    assert captured.value.code == "SQL_TABLE_FUNCTION_NOT_ALLOWED"


def test_cte_visibility_follows_nested_query_scope() -> None:
    sql = """
        SELECT * FROM hidden_relation
        WHERE EXISTS (
          WITH hidden_relation AS (SELECT 1)
          SELECT * FROM hidden_relation
        )
    """

    with pytest.raises(SqlValidationError) as captured:
        validate_sql(sql, allowed_relations=ALLOWED_RELATIONS)

    assert captured.value.code == "SQL_RELATION_MUST_BE_QUALIFIED"


def test_cte_cannot_reference_a_later_cte_name() -> None:
    sql = """
        WITH first AS (SELECT * FROM later),
             later AS (SELECT * FROM ai.issue_overview)
        SELECT * FROM first
    """

    with pytest.raises(SqlValidationError) as captured:
        validate_sql(sql, allowed_relations=ALLOWED_RELATIONS)

    assert captured.value.code == "SQL_RELATION_MUST_BE_QUALIFIED"


def test_accepts_nested_selects_and_multiple_published_relations() -> None:
    result = validate_sql(
        """
        SELECT issue_id
        FROM ai.issue_overview
        WHERE issue_id IN (
          SELECT issue_id FROM ai.issue_comments WHERE comment_type = 'DECISION'
        )
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("ai.issue_comments", "ai.issue_overview")


def test_fingerprint_ignores_literals_and_formatting() -> None:
    first = validate_sql(
        "SELECT * FROM ai.issue_overview WHERE issue_id = 1",
        allowed_relations=ALLOWED_RELATIONS,
    )
    second = validate_sql(
        "select * from ai.issue_overview\nwhere issue_id=999",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert first.fingerprint == second.fingerprint


def test_single_statement_with_comments_and_trailing_semicolon_is_allowed() -> None:
    result = validate_sql(
        "/* generated query */ SELECT count(*) FROM ai.issue_overview; -- done",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("ai.issue_overview",)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("", "SQL_EMPTY"),
        ("SELECT * FROM ai.issue_overview; SELECT 1", "SQL_MULTIPLE_STATEMENTS"),
        ("SELECT * FROM", "SQL_PARSE_ERROR"),
        ("INSERT INTO ai.issue_overview VALUES (1)", "SQL_STATEMENT_NOT_ALLOWED"),
        ("UPDATE ai.issue_overview SET status = 'CLOSED'", "SQL_STATEMENT_NOT_ALLOWED"),
        ("DELETE FROM ai.issue_overview", "SQL_STATEMENT_NOT_ALLOWED"),
        ("COPY ai.issue_overview TO STDOUT", "SQL_STATEMENT_NOT_ALLOWED"),
        ("SET search_path = ai", "SQL_STATEMENT_NOT_ALLOWED"),
        ("EXPLAIN SELECT * FROM ai.issue_overview", "SQL_STATEMENT_NOT_ALLOWED"),
        (
            "WITH changed AS (DELETE FROM ai.issue_overview RETURNING *) SELECT * FROM changed",
            "SQL_NESTED_STATEMENT_NOT_ALLOWED",
        ),
        (
            "SELECT * INTO TEMP snapshot FROM ai.issue_overview",
            "SQL_SELECT_INTO_NOT_ALLOWED",
        ),
        ("SELECT * FROM ai.issue_overview FOR UPDATE", "SQL_ROW_LOCK_NOT_ALLOWED"),
        ("SELECT * FROM ai.issue_overview FOR SHARE", "SQL_ROW_LOCK_NOT_ALLOWED"),
        (
            "WITH RECURSIVE items AS (SELECT 1 UNION ALL SELECT 1 FROM items) SELECT * FROM items",
            "SQL_RECURSIVE_CTE_NOT_ALLOWED",
        ),
        ("SELECT * FROM issue_overview", "SQL_RELATION_MUST_BE_QUALIFIED"),
        ("SELECT * FROM pg_catalog.pg_roles", "SQL_RELATION_NOT_ALLOWED"),
        ("SELECT * FROM pg_temp.session_data", "SQL_RELATION_NOT_ALLOWED"),
        ("SELECT * FROM other.secret", "SQL_RELATION_NOT_ALLOWED"),
        ('SELECT * FROM ai."이슈"', "SQL_RELATION_NOT_ALLOWED"),
        ("SELECT * FROM database.ai.issue_overview", "SQL_CROSS_DATABASE_REFERENCE"),
        ("SELECT pg_sleep(10)", "SQL_FUNCTION_NOT_ALLOWED"),
        ("SELECT ai.secret_function()", "SQL_FUNCTION_SCHEMA_NOT_ALLOWED"),
        ("SELECT nextval('secret_sequence')", "SQL_FUNCTION_NOT_ALLOWED"),
        ("SELECT * FROM generate_series(1, 100)", "SQL_TABLE_FUNCTION_NOT_ALLOWED"),
        (
            "SELECT * FROM ai.issue_overview TABLESAMPLE SYSTEM(10)",
            "SQL_TABLESAMPLE_NOT_ALLOWED",
        ),
        ("SELECT current_user", "SQL_VALUE_FUNCTION_NOT_ALLOWED"),
        ("SELECT current_catalog", "SQL_VALUE_FUNCTION_NOT_ALLOWED"),
        (
            "SELECT * FROM ai.issue_overview WHERE issue_id = $1",
            "SQL_PARAMETER_NOT_ALLOWED",
        ),
        ("SELECT 1::custom.type", "SQL_TYPE_NOT_ALLOWED"),
        ("SELECT 1::int4", "SQL_TYPE_NOT_ALLOWED"),
        ("SELECT CAST('x' AS)", "SQL_PARSE_ERROR"),
        ("SELECT 'x' COLLATE \"C\"", "SQL_COLLATION_NOT_ALLOWED"),
        ("SELECT 1 OPERATOR(ai.+) 2", "SQL_OPERATOR_NOT_ALLOWED"),
        ("SELECT XMLPARSE(DOCUMENT '<root/>')", "SQL_CONSTRUCT_NOT_ALLOWED"),
    ],
)
def test_rejects_unapproved_sql(sql: str, code: str) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(sql, allowed_relations=ALLOWED_RELATIONS)

    assert captured.value.code == code


def test_rejects_query_over_byte_limit_without_echoing_sql() -> None:
    secret = "sensitive-value"
    sql = f"SELECT '{secret}'"
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(sql, allowed_relations=ALLOWED_RELATIONS, max_sql_bytes=10)

    assert captured.value.code == "SQL_TOO_LARGE"
    assert secret not in captured.value.message


def test_supports_explicit_policy_extension() -> None:
    result = validate_sql(
        "SELECT pg_catalog.width_bucket(comment_count, 0, 100, 10) FROM ai.issue_overview",
        allowed_relations=ALLOWED_RELATIONS,
        allowed_functions={"width_bucket"},
    )

    assert result.functions == ("pg_catalog.width_bucket",)


@given(st.text(max_size=200))
@settings(max_examples=200, deadline=None)
def test_arbitrary_input_always_fails_closed(value: str) -> None:
    try:
        result = validate_sql(value, allowed_relations=ALLOWED_RELATIONS)
    except SqlValidationError:
        return

    assert set(result.relations) <= ALLOWED_RELATIONS
    assert result.fingerprint.startswith("pg_query:")


@given(st.integers(), st.integers())
def test_fingerprint_normalizes_generated_integer_literals(first: int, second: int) -> None:
    first_result = validate_sql(
        f"SELECT * FROM ai.issue_overview WHERE issue_id = {first}",
        allowed_relations=ALLOWED_RELATIONS,
    )
    second_result = validate_sql(
        f"SELECT * FROM ai.issue_overview WHERE issue_id = {second}",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert first_result.fingerprint == second_result.fingerprint


@given(
    st.sampled_from(
        [
            "DELETE FROM ai.issue_overview",
            "UPDATE ai.issue_overview SET status = 'CLOSED'",
            "SELECT pg_sleep(1)",
            "COPY ai.issue_overview TO STDOUT",
        ]
    ),
    st.text(alphabet=" \t\n", max_size=20),
)
def test_appended_second_statement_is_never_accepted(statement: str, whitespace: str) -> None:
    sql = f"SELECT * FROM ai.issue_overview;{whitespace}{statement}"

    with pytest.raises(SqlValidationError) as captured:
        validate_sql(sql, allowed_relations=ALLOWED_RELATIONS)

    assert captured.value.code == "SQL_MULTIPLE_STATEMENTS"
