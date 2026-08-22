from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from query_man.models import CatalogSnapshot, QualityLevel, SourceProfile

_LEVEL_ORDER: dict[QualityLevel, int] = {"L0": 0, "L1": 1, "L2": 2}


@dataclass(frozen=True)
class QualityLevelReport:
    level: QualityLevel
    required_level: QualityLevel
    violations: tuple[str, ...]

    @property
    def publishable(self) -> bool:
        return _LEVEL_ORDER[self.level] >= _LEVEL_ORDER[self.required_level]


def assess_quality_level(
    source: SourceProfile,
    snapshot: CatalogSnapshot,
    revision: str,
    verified_revisions: Mapping[str, frozenset[str]],
) -> QualityLevelReport:
    semantic_by_relation = {
        semantic.relation: semantic for semantic in source.semantic_overlay.relations
    }
    l1_violations: list[str] = []
    for relation in snapshot.relations:
        semantic = semantic_by_relation.get(relation.qualified_name)
        if semantic is None:
            l1_violations.append(f"missing semantic metadata: {relation.qualified_name}")
            continue
        if semantic.grain is None:
            l1_violations.append(f"missing grain: {relation.qualified_name}")
        if not (semantic.description or relation.comment):
            l1_violations.append(f"missing description: {relation.qualified_name}")
        if semantic.role in {"event", "comment", "population"} and not semantic.default_time_column:
            l1_violations.append(f"missing default time: {relation.qualified_name}")
    if l1_violations:
        return QualityLevelReport("L0", source.minimum_quality_level, tuple(l1_violations))

    if revision not in verified_revisions.get(source.source_id, frozenset()):
        return QualityLevelReport(
            "L1",
            source.minimum_quality_level,
            ("no verified query contract matches the metadata revision",),
        )
    return QualityLevelReport("L2", source.minimum_quality_level, ())
