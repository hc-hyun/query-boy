from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from math import log

from query_man.metadata.models import CatalogRelation
from query_man.source_catalog.models import RelationSemantic

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


@dataclass
class _IndexedRelation:
    relation: CatalogRelation
    semantic: RelationSemantic | None
    token_weights: dict[str, float]
    token_columns: dict[str, str]


class RelationRetrievalIndex:
    def __init__(
        self,
        relations: Sequence[CatalogRelation],
        semantics: Sequence[RelationSemantic],
        default_relation: str | None = None,
    ) -> None:
        semantic_by_name = {item.relation: item for item in semantics}
        self._documents = [
            _index_relation(relation, semantic_by_name.get(relation.qualified_name))
            for relation in relations
        ]
        self._default_relation = default_relation
        document_frequency: dict[str, int] = {}
        for document in self._documents:
            for token in document.token_weights:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        count = len(self._documents)
        self._idf = {
            token: log((count + 1) / (frequency + 0.5)) + 1
            for token, frequency in document_frequency.items()
        }

    def rank(self, question: str) -> list[RankedRelation]:
        normalized_question = _normalize(question)
        question_tokens = _search_tokens(question)
        ranked: list[RankedRelation] = []
        for document in self._documents:
            semantic = document.semantic
            reasons: list[SelectionReason] = []
            score = 0.0
            for token in question_tokens:
                weight = document.token_weights.get(token)
                if weight is not None:
                    score += weight * self._idf[token]
                    reasons.append(
                        SelectionReason(
                            "retrieval_token",
                            token,
                            document.token_columns.get(token),
                        )
                    )
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
            ranked.append(
                RankedRelation(
                    document.relation,
                    semantic,
                    score,
                    _deduplicate_reasons(reasons),
                )
            )
        ranked.sort(key=lambda item: (-item.score, item.relation.qualified_name))
        if ranked and ranked[0].score == 0:
            index = next(
                (
                    i
                    for i, item in enumerate(ranked)
                    if item.relation.qualified_name == self._default_relation
                ),
                0,
            )
            fallback = ranked.pop(index)
            fallback.reasons = [
                SelectionReason("default_relation", fallback.relation.qualified_name)
            ]
            ranked.insert(0, fallback)
        return ranked


def _index_relation(
    relation: CatalogRelation,
    semantic: RelationSemantic | None,
) -> _IndexedRelation:
    weights: dict[str, float] = {}
    columns: dict[str, str] = {}

    def add(value: str | None, weight: float, column: str | None = None) -> None:
        if value is None:
            return
        for token in _search_tokens(value):
            weights[token] = max(weights.get(token, 0), weight)
            if column is not None:
                columns.setdefault(token, column)

    add(relation.qualified_name, 4)
    add(relation.comment, 1)
    for catalog_column in relation.columns:
        add(catalog_column.name, 3, catalog_column.name)
        add(catalog_column.comment, 1, catalog_column.name)
    if semantic is not None:
        add(semantic.description, 2)
        for phrase in semantic.use_for:
            add(phrase, 5)
        for alias in semantic.aliases:
            add(alias, 4)
        for column_name, aliases in semantic.column_aliases.items():
            for alias in aliases:
                add(alias, 5, column_name)
        for column_name, hints in semantic.value_hints.items():
            for hint in hints:
                add(hint, 4, column_name)
    return _IndexedRelation(relation, semantic, weights, columns)


def select_ranked_relations(ranked: list[RankedRelation], max_objects: int) -> tuple[list[RankedRelation], bool]:
    if not ranked:
        return [], False
    threshold = max(8, ranked[0].score * 0.8)
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
