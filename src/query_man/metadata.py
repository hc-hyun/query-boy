from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from query_man.errors import MetadataUnavailableError, SourceNotFoundError
from query_man.models import (
    BusinessTermDefinition,
    CatalogColumn,
    CatalogProvider,
    CatalogRelation,
    CatalogSnapshot,
    CompositionHint,
    JoinDefinition,
    MeasureDefinition,
    QuestionRule,
    RelationSemantic,
    SourceProfile,
)
from query_man.registry import SourceRegistry
from query_man.relevance import (
    RankedRelation,
    SelectionReason,
    normalize_business_text,
    rank_relations,
    select_ranked_relations,
)
from query_man.revision import create_metadata_revision


@dataclass(frozen=True)
class _PreparedMetadata:
    snapshot: CatalogSnapshot
    revision: str


@dataclass
class _CacheEntry:
    value: _PreparedMetadata
    loaded_at: int
    expires_at: int
    next_refresh_at: int


class MetadataService:
    def __init__(
        self,
        registry: SourceRegistry,
        catalog: CatalogProvider,
        *,
        cache_ttl_ms: int = 30_000,
        max_stale_ms: int = 300_000,
        refresh_retry_ms: int = 5_000,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._cache_ttl_ms = cache_ttl_ms
        self._max_stale_ms = max_stale_ms
        self._refresh_retry_ms = refresh_retry_ms
        self._now = now or (lambda: int(time.time() * 1000))
        self._cache: dict[str, _CacheEntry] = {}
        self._refreshes: dict[str, asyncio.Task[_PreparedMetadata]] = {}

    async def get_context(self, source_id: str, question: str, max_objects: int = 2) -> dict[str, object]:
        source = self._registry.get(source_id)
        if source is None:
            raise SourceNotFoundError
        prepared, stale = await self._get_prepared(source)
        ranked = rank_relations(
            question,
            prepared.snapshot.relations,
            source.semantic_overlay.relations,
            source.semantic_overlay.default_relation,
        )
        selected, truncated = select_ranked_relations(ranked, max_objects)
        selected_names = {item.relation.qualified_name for item in selected}
        composition_hints = _select_composition_hints(question, source.semantic_overlay.composition_hints)
        joins = (
            []
            if composition_hints
            else [
                join
                for join in source.semantic_overlay.joins
                if join.left_relation in selected_names and join.right_relation in selected_names
            ]
        )
        ambiguities = _build_ambiguities(selected, joins, stale, bool(composition_hints))
        response: dict[str, object] = {
            "source_id": source.source_id,
            "source_name": source.name,
            "source_description": source.description,
            "question": question,
            "metadata_revision": prepared.revision,
            "snapshot_status": "stale" if stale else "fresh",
            "answerability": _build_answerability(question, source.semantic_overlay.question_rules, ambiguities),
            "relations": [
                _to_relation_response(item, index + 1, source.semantic_overlay.joins)
                for index, item in enumerate(selected)
            ],
            "joins": [_to_join_response(join) for join in joins],
            "business_terms": _select_business_terms(question, source.semantic_overlay.business_terms),
            "composition_hints": composition_hints,
            "ambiguities": ambiguities,
            "truncated": truncated,
        }
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > source.budget.max_metadata_response_bytes:
            raise MetadataUnavailableError(
                {"contract_violations": ["Question-scoped metadata response exceeds its byte limit."]}
            )
        return response

    def invalidate(self, source_id: str | None = None) -> None:
        if source_id is None:
            self._cache.clear()
        else:
            self._cache.pop(source_id, None)

    async def _get_prepared(self, source: SourceProfile) -> tuple[_PreparedMetadata, bool]:
        cached = self._cache.get(source.source_id)
        now = self._now()
        if cached and cached.expires_at > now:
            return cached.value, False
        if cached and cached.next_refresh_at > now:
            if now - cached.loaded_at <= self._max_stale_ms:
                return cached.value, True
            raise MetadataUnavailableError
        try:
            return await self._refresh(source), False
        except MetadataUnavailableError:
            raise
        except Exception as error:
            failed_at = self._now()
            if cached and failed_at - cached.loaded_at <= self._max_stale_ms:
                cached.next_refresh_at = failed_at + self._refresh_retry_ms
                return cached.value, True
            raise MetadataUnavailableError from error

    async def _refresh(self, source: SourceProfile) -> _PreparedMetadata:
        active = self._refreshes.get(source.source_id)
        if active is not None:
            return await active
        task = asyncio.create_task(self._load_and_validate(source))
        self._refreshes[source.source_id] = task
        try:
            return await task
        finally:
            self._refreshes.pop(source.source_id, None)

    async def _load_and_validate(self, source: SourceProfile) -> _PreparedMetadata:
        snapshot = await self._catalog.load(source)
        issues = _validate_snapshot(source, snapshot)
        if issues:
            raise MetadataUnavailableError({"contract_violations": issues})
        value = _PreparedMetadata(snapshot, create_metadata_revision(source, snapshot))
        loaded_at = self._now()
        self._cache[source.source_id] = _CacheEntry(
            value=value,
            loaded_at=loaded_at,
            expires_at=loaded_at + self._cache_ttl_ms,
            next_refresh_at=loaded_at + self._cache_ttl_ms,
        )
        return value


def _validate_snapshot(source: SourceProfile, snapshot: CatalogSnapshot) -> list[str]:
    issues: list[str] = []
    if not snapshot.relations:
        return ["No selectable relations were discovered in the allowed schemas."]
    relations = {relation.qualified_name: relation for relation in snapshot.relations}
    for semantic in source.semantic_overlay.relations:
        relation = relations.get(semantic.relation)
        if relation is None:
            issues.append(f"Configured relation is missing or not selectable: {semantic.relation}")
            continue
        columns = {column.name: column for column in relation.columns}
        for key in semantic.grain.key_columns if semantic.grain else []:
            if key not in columns:
                issues.append(f"Missing grain key {semantic.relation}.{key}")
        if semantic.default_time_column:
            time_column = columns.get(semantic.default_time_column)
            if time_column is None:
                issues.append(f"Missing default time column {semantic.relation}.{semantic.default_time_column}")
            elif not re.match(r"^(date|time|timestamp)", time_column.data_type, re.IGNORECASE):
                issues.append(f"Default time column is not temporal: {semantic.relation}.{time_column.name}")
        for alias_column in semantic.column_aliases:
            if alias_column not in columns:
                issues.append(f"Column alias targets a missing column: {semantic.relation}.{alias_column}")
        for hint_column in semantic.value_hints:
            if hint_column not in columns:
                issues.append(f"Value hints target a missing column: {semantic.relation}.{hint_column}")
        for measure in semantic.measures:
            if measure.aggregation == "sum" and measure.column not in columns:
                issues.append(f"Measure targets a missing column: {semantic.relation}.{measure.column}")
    for join in source.semantic_overlay.joins:
        left = relations.get(join.left_relation)
        right = relations.get(join.right_relation)
        if left is None or right is None:
            issues.append(f"Approved join relation is unavailable: {join.left_relation} -> {join.right_relation}")
            continue
        left_columns = {column.name: column for column in left.columns}
        right_columns = {column.name: column for column in right.columns}
        for pair in join.column_pairs:
            left_column = left_columns.get(pair["left"])
            right_column = right_columns.get(pair["right"])
            key = f"{join.left_relation}.{pair['left']} -> {join.right_relation}.{pair['right']}"
            if left_column is None or right_column is None:
                issues.append(f"Approved join key is unavailable: {key}")
            elif left_column.data_type != right_column.data_type:
                issues.append(f"Approved join key type mismatch: {key}")
    for term in source.semantic_overlay.business_terms:
        for predicate in term.predicates:
            relation = relations.get(predicate.relation)
            if relation is None or not any(column.name == predicate.column for column in relation.columns):
                issues.append(f"Business predicate targets a missing column: {predicate.relation}.{predicate.column}")
    return issues


def _to_relation_response(candidate: RankedRelation, rank: int, all_joins: list[JoinDefinition]) -> dict[str, object]:
    relation, semantic = candidate.relation, candidate.semantic
    response: dict[str, object] = {
        "rank": rank,
        "name": relation.qualified_name,
        "sql_name": relation.sql_name,
        "kind": relation.kind,
        "role": semantic.role if semantic else "unclassified",
        "description": (semantic.description if semantic else None) or relation.comment,
        "database_comment": relation.comment,
        "grain": (
            {
                "name": semantic.grain.name,
                "description": semantic.grain.description,
                "key_columns": semantic.grain.key_columns,
            }
            if semantic and semantic.grain
            else None
        ),
        "default_time_column": semantic.default_time_column if semantic else None,
        "selection_reasons": [_reason_dict(reason) for reason in candidate.reasons],
        "measures": [_to_measure_response(measure) for measure in semantic.measures] if semantic else [],
        "columns": [_to_column_response(column, relation, semantic, all_joins) for column in relation.columns],
    }
    if relation.estimated_rows is not None:
        response["estimated_rows"] = relation.estimated_rows
    return response


def _reason_dict(reason: SelectionReason) -> dict[str, str]:
    value = {"kind": reason.kind, "term": reason.term}
    if reason.column is not None:
        value["column"] = reason.column
    return value


def _to_column_response(
    column: CatalogColumn,
    relation: CatalogRelation,
    semantic: RelationSemantic | None,
    all_joins: list[JoinDefinition],
) -> dict[str, object]:
    roles: list[str] = []
    if semantic and semantic.grain and column.name in semantic.grain.key_columns:
        roles.append("grain_key")
    if semantic and semantic.default_time_column == column.name:
        roles.append("default_time")
    if _is_join_key(relation.qualified_name, column.name, all_joins):
        roles.append("join_key")
    return {
        "name": column.name,
        "sql_name": column.sql_name,
        "ordinal": column.ordinal,
        "data_type": column.data_type,
        "nullable": column.nullable,
        "description": column.comment,
        "aliases": semantic.column_aliases.get(column.name, []) if semantic else [],
        "value_hints": semantic.value_hints.get(column.name, []) if semantic else [],
        "semantic_roles": roles,
    }


def _is_join_key(relation: str, column: str, joins: list[JoinDefinition]) -> bool:
    return any(
        (join.left_relation == relation and any(pair["left"] == column for pair in join.column_pairs))
        or (join.right_relation == relation and any(pair["right"] == column for pair in join.column_pairs))
        for join in joins
    )


def _to_join_response(join: JoinDefinition) -> dict[str, object]:
    return {
        "left_relation": join.left_relation,
        "right_relation": join.right_relation,
        "column_pairs": join.column_pairs,
        "cardinality": join.cardinality,
        "fanout": join.fanout,
        "guidance": join.guidance,
    }


def _to_measure_response(measure: MeasureDefinition) -> dict[str, object]:
    result: dict[str, object] = {
        "name": measure.name,
        "description": measure.description,
        "aliases": measure.aliases,
        "aggregation": measure.aggregation,
    }
    if measure.column:
        result["column"] = measure.column
    if measure.numerator_measure:
        result["numerator_measure"] = measure.numerator_measure
    if measure.denominator_measure:
        result["denominator_measure"] = measure.denominator_measure
    return result


def _build_ambiguities(
    selected: list[RankedRelation],
    joins: list[JoinDefinition],
    stale: bool,
    has_composition: bool,
) -> list[dict[str, str]]:
    ambiguities: list[dict[str, str]] = []
    first = selected[0] if selected else None
    used_default = bool(first and any(reason.kind == "default_relation" for reason in first.reasons))
    if first is None or used_default or not _has_meaningful_reason(first):
        ambiguities.append(
            {
                "code": "LOW_METADATA_RELEVANCE",
                "message": (
                    "The question had little overlap with the published metadata; the default relation was returned."
                    if used_default
                    else "The question had little semantic overlap with the published metadata; "
                    "treat the candidates as low confidence."
                ),
            }
        )
    ambiguities.extend({"code": "RAW_JOIN_FANOUT", "message": join.guidance} for join in joins if join.fanout)
    if len(selected) > 1 and not joins and not has_composition:
        ambiguities.append(
            {
                "code": "NO_APPROVED_JOIN_PATH",
                "message": "Multiple grains matched, but no approved raw join connects them. "
                "Aggregate each relation separately.",
            }
        )
    if stale:
        ambiguities.append(
            {
                "code": "STALE_METADATA_SNAPSHOT",
                "message": "Catalog refresh failed; the last valid metadata revision was returned.",
            }
        )
    return ambiguities


def _has_meaningful_reason(candidate: RankedRelation) -> bool:
    generic = {"id", "no", "name", "number", "key", "code"}
    return any(
        reason.kind in {"use_for", "relation_alias", "column_alias"}
        or (reason.kind == "column_name" and reason.term not in generic)
        for reason in candidate.reasons
    )


def _build_answerability(
    question: str, rules: list[QuestionRule], ambiguities: list[dict[str, str]]
) -> dict[str, object]:
    matched = [rule for rule in rules if any(_contains_business_phrase(question, phrase) for phrase in rule.phrases)]
    if matched:
        return {
            "status": "unsupported" if any(rule.status == "unsupported" for rule in matched) else "needs_clarification",
            "reason_codes": _unique([rule.code for rule in matched]),
            "messages": _unique([rule.message for rule in matched]),
            "missing_concepts": _unique([concept for rule in matched for concept in rule.missing_concepts]),
            "options": _unique([option for rule in matched for option in rule.options]),
        }
    if any(item["code"] == "LOW_METADATA_RELEVANCE" for item in ambiguities):
        return {
            "status": "low_confidence",
            "reason_codes": ["LOW_METADATA_RELEVANCE"],
            "messages": ["The selected relations are candidates, not a confirmed answer surface."],
            "missing_concepts": [],
            "options": [],
        }
    return {
        "status": "best_effort",
        "reason_codes": [],
        "messages": [],
        "missing_concepts": [],
        "options": [],
    }


def _select_business_terms(question: str, terms: list[BusinessTermDefinition]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for term in terms:
        aliases = [alias for alias in term.aliases if _contains_business_phrase(question, alias)]
        if not aliases:
            continue
        item: dict[str, object] = {
            "name": term.name,
            "description": term.description,
            "matched_aliases": aliases,
            "predicates": [asdict(predicate) for predicate in term.predicates],
        }
        if term.calculation:
            item["calculation"] = term.calculation
        result.append(item)
    return result


def _select_composition_hints(question: str, hints: list[CompositionHint]) -> list[dict[str, object]]:
    return [
        {
            "name": hint.name,
            "strategy": hint.strategy,
            "guidance": hint.guidance,
            "combine_keys": hint.combine_keys,
        }
        for hint in hints
        if any(_contains_business_phrase(question, phrase) for phrase in hint.phrases)
    ]


def _contains_business_phrase(question: str, phrase: str) -> bool:
    return normalize_business_text(phrase) in normalize_business_text(question)


def _unique[T](values: list[T]) -> list[T]:
    return list(dict.fromkeys(values))
