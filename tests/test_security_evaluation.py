from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from query_man.guarded_query.sql_validation import SqlValidationError, validate_sql

CORPUS_PATH = Path(__file__).parents[1] / "config" / "security-evaluation.yaml"
ALLOWED_RELATIONS = {"signal_schema.case_files_view"}
REQUIRED_CATEGORIES = {
    "write",
    "privilege_escalation",
    "system_object",
    "resource_limit_bypass",
}


def _load_corpus() -> dict[str, object]:
    loaded = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


CORPUS = _load_corpus()
CASES = CORPUS["validation_rejections"]


def test_security_corpus_contract_is_versioned_and_complete() -> None:
    assert CORPUS["version"] == 1
    assert isinstance(CASES, list)
    assert len({case["id"] for case in CASES}) == len(CASES)
    assert {case["category"] for case in CASES} == REQUIRED_CATEGORIES


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
def test_security_corpus_fails_closed(case: dict[str, object]) -> None:
    with pytest.raises(SqlValidationError) as captured:
        validate_sql(
            str(case["sql"]),
            allowed_relations=ALLOWED_RELATIONS,
            max_sql_bytes=int(case.get("max_sql_bytes", 100_000)),
        )

    assert captured.value.code == case["expected_code"]
