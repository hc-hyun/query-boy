from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from query_man.models import CatalogRelation, RelationSemantic

_KOREAN_SUFFIXES = [
    "으로부터",
    "에서부터",
    "에게서",
    "으로",
    "에서",
    "에게",
    "까지",
    "부터",
    "마다",
    "별로",
    "별",
    "당",
    "과",
    "와",
    "을",
    "를",
    "은",
    "는",
    "이",
    "가",
    "의",
    "에",
    "도",
]
_STOP_WORDS = {
    "보여줘",
    "알려줘",
    "동안",
    "순서",
    "높은",
    "낮은",
    "비교",
    "무엇",
    "어떤",
    "그리고",
    "대한",
    "the",
    "and",
    "for",
    "with",
    "row",
    "one",
}


@dataclass(frozen=True)
class SelectionReason:
    kind: str
    term: str
    column: str | None = None


@dataclass
class RankedRelation:
    relation: CatalogRelation
    semantic: RelationSemantic | None
    score: float
    reasons: list[SelectionReason]


def rank_relations(
    question: str,
    relations: list[CatalogRelation],
    semantics: list[RelationSemantic],
    default_relation: str | None = None,
) -> list[RankedRelation]:
    normalized_question = _normalize(question)
    question_tokens = _search_tokens(question)
    semantic_by_name = {item.relation: item for item in semantics}
    ranked: list[RankedRelation] = []
    for relation in relations:
        semantic = semantic_by_name.get(relation.qualified_name)
        reasons: list[SelectionReason] = []
        score = 0.0
        for phrase in semantic.use_for if semantic else []:
            if _contains_phrase(normalized_question, phrase):
                score += 28
                reasons.append(SelectionReason("use_for", phrase))
        for alias in semantic.aliases if semantic else []:
            if _contains_phrase(normalized_question, alias):
                score += 18
                reasons.append(SelectionReason("relation_alias", alias))
        for alias_column, aliases in semantic.column_aliases.items() if semantic else []:
            for alias in aliases:
                if _contains_phrase(normalized_question, alias):
                    score += 10
                    reasons.append(SelectionReason("column_alias", alias, alias_column))
        relation_matches = sorted(question_tokens & _search_tokens(relation.qualified_name))
        if relation_matches:
            score += len(relation_matches) * 8
            reasons.extend(SelectionReason("lexical_metadata", term) for term in relation_matches[:5])
        column_matches: dict[str, str] = {}
        for catalog_column in relation.columns:
            for term in question_tokens & _search_tokens(catalog_column.name):
                column_matches.setdefault(term, catalog_column.name)
        if column_matches:
            score += min(18, len(column_matches) * 3)
            reasons.extend(
                SelectionReason("column_name", term, column)
                for term, column in list(sorted(column_matches.items()))[:5]
            )
        descriptive_text = " ".join(
            value
            for value in [
                semantic.description if semantic else None,
                *(semantic.aliases if semantic else []),
                *(semantic.use_for if semantic else []),
                relation.comment,
                *(column.comment for column in relation.columns),
            ]
            if value
        )
        descriptive_matches = sorted(question_tokens & _search_tokens(descriptive_text))
        if descriptive_matches:
            score += min(15, len(descriptive_matches) * 1.5)
            reasons.extend(SelectionReason("lexical_metadata", term) for term in descriptive_matches[:5])
        ranked.append(RankedRelation(relation, semantic, score, _deduplicate_reasons(reasons)))
    ranked.sort(key=lambda item: (-item.score, item.relation.qualified_name))
    if ranked and ranked[0].score == 0:
        index = next(
            (i for i, item in enumerate(ranked) if item.relation.qualified_name == default_relation),
            0,
        )
        fallback = ranked.pop(index)
        fallback.reasons = [SelectionReason("default_relation", fallback.relation.qualified_name)]
        ranked.insert(0, fallback)
    return ranked


def select_ranked_relations(ranked: list[RankedRelation], max_objects: int) -> tuple[list[RankedRelation], bool]:
    if not ranked:
        return [], False
    threshold = max(8, ranked[0].score * 0.5)
    eligible = [
        item
        for index, item in enumerate(ranked)
        if index == 0
        or any(reason.kind == "use_for" for reason in item.reasons)
        or (item.score > 0 and item.score >= threshold)
    ]
    return eligible[:max_objects], len(eligible) > max_objects


def _contains_phrase(normalized_question: str, phrase: str) -> bool:
    normalized_phrase = _normalize(phrase)
    return len(normalized_phrase) >= 2 and normalized_phrase in normalized_question


def normalize_business_text(value: str) -> str:
    return _normalize(value)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("_", " ")


def _search_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for match in re.findall(r"[^\W_]+", _normalize(value), flags=re.UNICODE):
        if len(match) >= 2 and match not in _STOP_WORDS:
            tokens.add(match)
        for suffix in _KOREAN_SUFFIXES:
            if match.endswith(suffix) and len(match) - len(suffix) >= 2:
                root = match[: -len(suffix)]
                if root not in _STOP_WORDS:
                    tokens.add(root)
                break
    return tokens


def _deduplicate_reasons(reasons: list[SelectionReason]) -> list[SelectionReason]:
    seen: set[tuple[str, str | None, str]] = set()
    result: list[SelectionReason] = []
    for reason in reasons:
        key = (reason.kind, reason.column, reason.term)
        if key not in seen:
            seen.add(key)
            result.append(reason)
    return result
