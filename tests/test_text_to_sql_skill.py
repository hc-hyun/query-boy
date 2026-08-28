import json
import re
from collections import Counter
from pathlib import Path

import yaml

from tests.helpers import ROOT_DIRECTORY

SKILL_PATH = ROOT_DIRECTORY / "skills" / "query-man-text-to-sql" / "SKILL.md"
SOURCE_SELECTION_CASES_PATH = (
    ROOT_DIRECTORY / "config" / "domain-lab" / "source-selection-cases.json"
)

EXPECTED_SOURCE_CATALOG = {
    "retail-commerce": (
        "리테일 커머스",
        "온라인·매장 주문, 주문 상품, 결제, 반품·환불과 고객 구매 이력",
    ),
    "parcel-logistics": (
        "택배 물류",
        "운송장, 출발·도착 권역, 허브 추적 스캔, 배송 약속·지연·분실·파손",
    ),
    "energy-telemetry": (
        "에너지 계측",
        "스마트 계량기의 전력 사용·발전·역송전 시계열, 검침 품질과 정전",
    ),
    "clinical-operations": (
        "가상 임상 운영",
        "완전 합성 환자 식별자 기반 진료 예약·취소·노쇼와 검사 결과 운영",
    ),
    "saas-billing": (
        "SaaS 구독·과금",
        "기업 tenant의 요금제·구독·MRR·invoice 결제 상태와 제품 사용량",
    ),
    "development-issues": (
        "개발 문제점",
        "개발 및 검증 과정에서 발견한 문제, 원인, 대책과 댓글",
    ),
    "market-voc": (
        "시장 VOC",
        "시장에서 접수된 불량, 제품 기기, 원인, 대응과 댓글",
    ),
}


def _skill_frontmatter(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    _start, frontmatter, _body = content.split("---\n", 2)
    document = yaml.safe_load(frontmatter)
    assert isinstance(document, dict)
    return document


def test_text_to_sql_skill_requires_actual_query_man_tools_and_fails_closed() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = _skill_frontmatter(SKILL_PATH)

    assert frontmatter["name"] == "query-man-text-to-sql"
    assert "list_sources" in content
    assert "get_context" in content
    assert "query" in content
    assert "If any required\ntool is unavailable or disconnected" in content
    assert "state that the query was not executed and stop" in content
    assert "Do not start\na server, call HTTP directly, connect to PostgreSQL" in content
    assert "fixture SQL" in content
    assert "seed data" in content
    assert "Never present a value as a query result unless" in content
    assert "the `query` tool returned it" in content


def test_text_to_sql_skill_selects_one_source_or_clarifies_before_context() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    step_one = content.split("1. Call `list_sources`", 1)[1].split(
        "2. Call `get_context`", 1
    )[0]
    normalized_step_one = " ".join(step_one.split())

    assert "source `name` and `description`" in normalized_step_one
    assert (
        "Select exactly one source only when a single source clearly matches"
        in normalized_step_one
    )
    assert "If multiple sources are plausible, or none is plausible" in normalized_step_one
    assert "ask one focused clarification" in normalized_step_one
    assert "stop before `get_context`" in normalized_step_one
    assert "Never call `get_context` to probe several sources" in normalized_step_one
    assert "never emulate federation" in normalized_step_one
    assert "cross-source federation is unsupported" in normalized_step_one
    assert "narrow the request to one source" in normalized_step_one
    assert (
        "follow-up only when the follow-up remains in the same domain"
        in normalized_step_one
    )
    assert "Otherwise call `list_sources` and select again" in normalized_step_one


def test_source_selection_corpus_is_strict_version_one_and_covers_all_sources() -> None:
    document = json.loads(SOURCE_SELECTION_CASES_PATH.read_text(encoding="utf-8"))

    assert isinstance(document, dict)
    assert set(document) == {"version", "source_catalog", "cases"}
    assert document["version"] == 1

    source_catalog = document["source_catalog"]
    assert isinstance(source_catalog, list)
    assert len(source_catalog) == len(EXPECTED_SOURCE_CATALOG)
    actual_catalog: dict[str, tuple[str, str]] = {}
    for source in source_catalog:
        assert isinstance(source, dict)
        assert set(source) == {"source_id", "name", "description"}
        source_id = source["source_id"]
        assert isinstance(source_id, str)
        assert source_id not in actual_catalog
        assert isinstance(source["name"], str) and source["name"]
        assert isinstance(source["description"], str) and source["description"]
        actual_catalog[source_id] = (source["name"], source["description"])
    assert actual_catalog == EXPECTED_SOURCE_CATALOG

    manifest_catalog = {}
    for path in sorted(
        (ROOT_DIRECTORY / "config" / "domain-lab" / "sources").glob("*.yaml")
    ):
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest_catalog[manifest["source_id"]] = (
            manifest["name"],
            manifest["description"],
        )
    assert manifest_catalog == EXPECTED_SOURCE_CATALOG

    cases = document["cases"]
    assert isinstance(cases, list)
    assert len(cases) == 25

    case_ids: set[str] = set()
    questions: set[str] = set()
    reason_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    selected_counts: Counter[str] = Counter()
    source_ids = set(EXPECTED_SOURCE_CATALOG)

    for case in cases:
        assert isinstance(case, dict)
        assert set(case) == {"case_id", "language", "question", "expected"}
        case_id = case["case_id"]
        question = case["question"]
        assert isinstance(case_id, str) and re.fullmatch(
            r"[a-z0-9]+(?:-[a-z0-9]+)*", case_id
        )
        assert case_id not in case_ids
        case_ids.add(case_id)
        assert isinstance(question, str) and question == question.strip() and question
        assert question not in questions
        questions.add(question)
        assert case["language"] in {"ko", "en"}
        language_counts[case["language"]] += 1

        expected = case["expected"]
        assert isinstance(expected, dict)
        assert set(expected) == {
            "action",
            "source_id",
            "reason",
            "candidate_source_ids",
        }
        reason = expected["reason"]
        candidates = expected["candidate_source_ids"]
        assert reason in {"single_match", "ambiguous", "unsupported", "cross_source"}
        assert isinstance(candidates, list)
        assert len(candidates) == len(set(candidates))
        assert set(candidates) <= source_ids
        reason_counts[reason] += 1

        if reason == "single_match":
            source_id = expected["source_id"]
            assert expected["action"] == "select"
            assert source_id in source_ids
            assert candidates == [source_id]
            selected_counts[source_id] += 1
        else:
            assert expected["action"] == "clarify"
            assert expected["source_id"] is None
            if reason == "unsupported":
                assert candidates == []
            else:
                assert len(candidates) >= 2

    assert reason_counts == {
        "single_match": 14,
        "ambiguous": 4,
        "unsupported": 3,
        "cross_source": 4,
    }
    assert selected_counts == {source_id: 2 for source_id in source_ids}
    assert language_counts["ko"] > language_counts["en"] >= 3
