from query_man.metadata.relevance import RelationRetrievalIndex, select_ranked_relations
from tests.helpers import column, load_test_registry, minimal_development_snapshot, relation


def _select(source_id: str, question: str, relations: list | None = None) -> list[str]:
    source = load_test_registry().get(source_id)
    assert source is not None
    ranked = RelationRetrievalIndex(
        relations or minimal_development_snapshot().relations,
        source.semantic_overlay.relations,
        source.semantic_overlay.default_relation,
    ).rank(question)
    selected, _ = select_ranked_relations(ranked, 2)
    return [item.relation.qualified_name for item in selected]


def test_selects_issue_grain() -> None:
    assert _select("development-issues", "최근 90일 동안 모델별 개발 문제 건수와 미해결 건수를 보여줘") == [
        "ai.issue_overview"
    ]


def test_selects_both_activity_grains() -> None:
    assert _select("development-issues", "보고자와 담당자, 댓글 작성자별 활동 건수를 비교해줘") == [
        "ai.issue_overview",
        "ai.issue_comments",
    ]


def test_selects_comment_grain() -> None:
    assert _select("development-issues", "댓글 본문을 작성자별로 보여줘") == ["ai.issue_comments"]


def test_token_index_handles_a_non_exact_comment_paraphrase() -> None:
    assert _select("development-issues", "작성자가 남긴 코멘트 내용을 읽고 싶어") == [
        "ai.issue_comments"
    ]


def test_selects_device_population_for_zero_voc() -> None:
    relations = [
        relation("ai.device_overview", [column("device_id", "bigint"), column("voc_count")]),
        relation("ai.voc_comments", [column("comment_id", "bigint"), column("voc_id")]),
        relation("ai.voc_overview", [column("voc_id", "bigint"), column("received_at")]),
    ]
    assert _select("market-voc", "VOC가 한 번도 없는 기기는 몇 대인가?", relations) == ["ai.device_overview"]
