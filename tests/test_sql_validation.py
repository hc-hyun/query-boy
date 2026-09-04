from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pglast.parser import get_postgresql_version

import query_man.guarded_query.sql_validation as sql_validation_module
from query_man.guarded_query.sql_validation import (
    DEFAULT_ALLOWED_FUNCTIONS,
    DEFAULT_ALLOWED_TYPES,
    DEFAULT_ALLOWED_UNQUALIFIED_TYPES,
    SQL_POLICY_REVISION,
    SqlValidationError,
    validate_sql,
)
from query_man.source_catalog.reader_policy import READER_CLIENT_ENCODING

ALLOWED_RELATIONS = {
    "signal_schema.case_notes_view",
    "signal_schema.case_files_view",
    "signal_schema.response_units_view",
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
          date_trunc('month', reported_at) AS month,
          count(*) AS case_count,
          sum(note_count) AS note_count
        FROM signal_schema.case_files_view
        WHERE status <> 'RESOLVED'
        GROUP BY 1
        ORDER BY 1
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("signal_schema.case_files_view",)
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
          SELECT case_id, note_count
          FROM signal_schema.case_files_view
          WHERE reported_at >= current_date - interval '90 days'
        )
        SELECT count(*), sum(note_count)
        FROM recent
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("signal_schema.case_files_view",)


def test_accepts_date_between_and_records_effective_comparison_operators() -> None:
    result = validate_sql(
        """
        SELECT *
        FROM signal_schema.case_files_view
        WHERE discovered_on BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("signal_schema.case_files_view",)
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
          CAST(reported_at AS date) AS discovered_on,
          case_id::text AS case_id_text
        FROM signal_schema.case_files_view
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert DEFAULT_ALLOWED_UNQUALIFIED_TYPES == frozenset({"date", "text"})
    assert result.relations == ("signal_schema.case_files_view",)


@pytest.mark.parametrize(
    "type_name",
    ["signal_schema.date", "signal_schema.text", "pg_temp.text", '"PG_CATALOG".date', '"PG_CATALOG".text'],
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
        ("rank() OVER (ORDER BY case_id)", "rank"),
        ("lag(status) OVER (ORDER BY reported_at)", "lag"),
        ("lead(status) OVER (ORDER BY reported_at)", "lead"),
        ("extract(year FROM reported_at)", "extract"),
        ("regexp_replace(title, '[0-9]+', '#', 'g')", "regexp_replace"),
        ("position('error' IN title)", "position"),
        (
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY note_count)",
            "percentile_cont",
        ),
        ("dense_rank() OVER (ORDER BY severity)", "dense_rank"),
        (
            "jsonb_build_object('id', case_id, 'status', status)",
            "jsonb_build_object",
        ),
        ("to_jsonb(case_id)", "to_jsonb"),
    ],
)
def test_accepts_common_query_functions_and_records_resolved_dependency(
    expression: str,
    function: str,
) -> None:
    result = validate_sql(
        f"SELECT {expression} FROM signal_schema.case_files_view",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.functions == (f"pg_catalog.{function}",)


@pytest.mark.parametrize(
    ("expression", "function"),
    [
        ("rank() OVER (ORDER BY case_id)", "rank"),
        ("lag(status) OVER (ORDER BY reported_at)", "lag"),
        ("lead(status) OVER (ORDER BY reported_at)", "lead"),
        ("extract(year FROM reported_at)", "extract"),
        ("regexp_replace(title, '[0-9]+', '#', 'g')", "regexp_replace"),
        ("position('error' IN title)", "position"),
        (
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY note_count)",
            "percentile_cont",
        ),
        ("dense_rank() OVER (ORDER BY severity)", "dense_rank"),
        (
            "jsonb_build_object('id', case_id, 'status', status)",
            "jsonb_build_object",
        ),
        ("to_jsonb(case_id)", "to_jsonb"),
    ],
)
def test_common_query_functions_honor_the_function_allowlist(
    expression: str,
    function: str,
) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            f"SELECT {expression} FROM signal_schema.case_files_view",
            allowed_relations=ALLOWED_RELATIONS,
            allowed_functions=DEFAULT_ALLOWED_FUNCTIONS - {function},
        )

    assert captured.value.code == "SQL_FUNCTION_NOT_ALLOWED"


@pytest.mark.parametrize("schema", ["signal_schema", '"PG_CATALOG"'])
@pytest.mark.parametrize(
    "expression_template",
    [
        "{schema}.rank() OVER (ORDER BY case_id)",
        "{schema}.lag(status) OVER (ORDER BY reported_at)",
        "{schema}.lead(status) OVER (ORDER BY reported_at)",
        "{schema}.extract('year', reported_at)",
        "{schema}.regexp_replace(title, '[0-9]+', '#', 'g')",
        "{schema}.position('error', title)",
        "{schema}.percentile_cont(0.5) WITHIN GROUP (ORDER BY note_count)",
        "{schema}.dense_rank() OVER (ORDER BY severity)",
        "{schema}.jsonb_build_object('id', case_id, 'status', status)",
        "{schema}.to_jsonb(case_id)",
    ],
)
def test_rejects_untrusted_schema_qualified_common_functions(
    expression_template: str,
    schema: str,
) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            f"SELECT {expression_template.format(schema=schema)} FROM signal_schema.case_files_view",
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
             later AS (SELECT * FROM signal_schema.case_files_view)
        SELECT * FROM first
    """

    with pytest.raises(SqlValidationError) as captured:
        validate_sql(sql, allowed_relations=ALLOWED_RELATIONS)

    assert captured.value.code == "SQL_RELATION_MUST_BE_QUALIFIED"


def test_accepts_nested_selects_and_multiple_published_relations() -> None:
    result = validate_sql(
        """
        SELECT case_id
        FROM signal_schema.case_files_view
        WHERE case_id IN (
          SELECT case_id FROM signal_schema.case_notes_view WHERE note_type = 'DECISION'
        )
        """,
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == (
        "signal_schema.case_files_view",
        "signal_schema.case_notes_view",
    )


def test_fingerprint_ignores_literals_and_formatting() -> None:
    first = validate_sql(
        "SELECT * FROM signal_schema.case_files_view WHERE case_id = 1",
        allowed_relations=ALLOWED_RELATIONS,
    )
    second = validate_sql(
        "select * from signal_schema.case_files_view\nwhere case_id=999",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert first.fingerprint == second.fingerprint


def test_single_statement_with_comments_and_trailing_semicolon_is_allowed() -> None:
    result = validate_sql(
        "/* generated query */ SELECT count(*) FROM signal_schema.case_files_view; -- done",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert result.relations == ("signal_schema.case_files_view",)


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("", "SQL_EMPTY"),
        ("SELECT * FROM signal_schema.case_files_view; SELECT 1", "SQL_MULTIPLE_STATEMENTS"),
        ("SELECT * FROM", "SQL_PARSE_ERROR"),
        ("INSERT INTO signal_schema.case_files_view VALUES (1)", "SQL_STATEMENT_NOT_ALLOWED"),
        ("UPDATE signal_schema.case_files_view SET status = 'CLOSED'", "SQL_STATEMENT_NOT_ALLOWED"),
        ("DELETE FROM signal_schema.case_files_view", "SQL_STATEMENT_NOT_ALLOWED"),
        ("COPY signal_schema.case_files_view TO STDOUT", "SQL_STATEMENT_NOT_ALLOWED"),
        ("SET search_path = signal_schema", "SQL_STATEMENT_NOT_ALLOWED"),
        ("EXPLAIN SELECT * FROM signal_schema.case_files_view", "SQL_STATEMENT_NOT_ALLOWED"),
        (
            "WITH changed AS (DELETE FROM signal_schema.case_files_view RETURNING *) SELECT * FROM changed",
            "SQL_NESTED_STATEMENT_NOT_ALLOWED",
        ),
        (
            "SELECT * INTO TEMP snapshot FROM signal_schema.case_files_view",
            "SQL_SELECT_INTO_NOT_ALLOWED",
        ),
        ("SELECT * FROM signal_schema.case_files_view FOR UPDATE", "SQL_ROW_LOCK_NOT_ALLOWED"),
        ("SELECT * FROM signal_schema.case_files_view FOR SHARE", "SQL_ROW_LOCK_NOT_ALLOWED"),
        (
            "WITH RECURSIVE items AS (SELECT 1 UNION ALL SELECT 1 FROM items) SELECT * FROM items",
            "SQL_RECURSIVE_CTE_NOT_ALLOWED",
        ),
        ("SELECT * FROM case_files_view", "SQL_RELATION_MUST_BE_QUALIFIED"),
        ("SELECT * FROM pg_catalog.pg_roles", "SQL_RELATION_NOT_ALLOWED"),
        ("SELECT * FROM pg_temp.session_data", "SQL_RELATION_NOT_ALLOWED"),
        ("SELECT * FROM other.secret", "SQL_RELATION_NOT_ALLOWED"),
        ('SELECT * FROM signal_schema."이슈"', "SQL_RELATION_NOT_ALLOWED"),
        ("SELECT * FROM database.signal_schema.case_files_view", "SQL_CROSS_DATABASE_REFERENCE"),
        ("SELECT pg_sleep(10)", "SQL_FUNCTION_NOT_ALLOWED"),
        ("SELECT signal_schema.secret_function()", "SQL_FUNCTION_SCHEMA_NOT_ALLOWED"),
        ("SELECT nextval('secret_sequence')", "SQL_FUNCTION_NOT_ALLOWED"),
        ("SELECT * FROM generate_series(1, 100)", "SQL_TABLE_FUNCTION_NOT_ALLOWED"),
        (
            "SELECT * FROM signal_schema.case_files_view TABLESAMPLE SYSTEM(10)",
            "SQL_TABLESAMPLE_NOT_ALLOWED",
        ),
        ("SELECT current_user", "SQL_VALUE_FUNCTION_NOT_ALLOWED"),
        ("SELECT current_catalog", "SQL_VALUE_FUNCTION_NOT_ALLOWED"),
        (
            "SELECT * FROM signal_schema.case_files_view WHERE case_id = $1",
            "SQL_PARAMETER_NOT_ALLOWED",
        ),
        ("SELECT 1::custom.type", "SQL_TYPE_NOT_ALLOWED"),
        ("SELECT 1::int4", "SQL_TYPE_NOT_ALLOWED"),
        ("SELECT CAST('x' AS)", "SQL_PARSE_ERROR"),
        ("SELECT 'x' COLLATE \"C\"", "SQL_COLLATION_NOT_ALLOWED"),
        ("SELECT 1 OPERATOR(signal_schema.+) 2", "SQL_OPERATOR_NOT_ALLOWED"),
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
        "SELECT pg_catalog.width_bucket(note_count, 0, 100, 10) FROM signal_schema.case_files_view",
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
        f"SELECT * FROM signal_schema.case_files_view WHERE case_id = {first}",
        allowed_relations=ALLOWED_RELATIONS,
    )
    second_result = validate_sql(
        f"SELECT * FROM signal_schema.case_files_view WHERE case_id = {second}",
        allowed_relations=ALLOWED_RELATIONS,
    )

    assert first_result.fingerprint == second_result.fingerprint


@given(
    st.sampled_from(
        [
            "DELETE FROM signal_schema.case_files_view",
            "UPDATE signal_schema.case_files_view SET status = 'CLOSED'",
            "SELECT pg_sleep(1)",
            "COPY signal_schema.case_files_view TO STDOUT",
        ]
    ),
    st.text(alphabet=" \t\n", max_size=20),
)
def test_appended_second_statement_is_never_accepted(statement: str, whitespace: str) -> None:
    sql = f"SELECT * FROM signal_schema.case_files_view;{whitespace}{statement}"

    with pytest.raises(SqlValidationError) as captured:
        validate_sql(sql, allowed_relations=ALLOWED_RELATIONS)

    assert captured.value.code == "SQL_MULTIPLE_STATEMENTS"
