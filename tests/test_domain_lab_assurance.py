from __future__ import annotations

import re
from collections import Counter

import yaml

from query_man.assurance.quality import QualityEvaluation
from query_man.assurance.verified import VerifiedQueryRegistry
from query_man.guarded_query.sql_validation import validate_sql
from tests.helpers import ROOT_DIRECTORY

DOMAIN_CONFIG = ROOT_DIRECTORY / "config" / "domain-lab"
NEW_SOURCE_IDS = {
    "clinical-operations",
    "energy-telemetry",
    "parcel-logistics",
    "retail-commerce",
    "saas-billing",
}
BASE_SOURCE_IDS = {"development-issues", "market-voc"}
SCHEMA_FILES = {
    "clinical-operations": "clinical-operations-schema.sql",
    "energy-telemetry": "energy-telemetry-schema.sql",
    "parcel-logistics": "parcel-logistics-schema.sql",
    "retail-commerce": "retail-commerce-schema.sql",
    "saas-billing": "saas-billing-schema.sql",
}
EXPECTED_COLUMN_COMMENT_COUNTS = {
    "clinical-operations": 20,
    "energy-telemetry": 21,
    "parcel-logistics": 22,
    "retail-commerce": 26,
    "saas-billing": 28,
}


def _manifests() -> dict[str, dict[str, object]]:
    manifests: dict[str, dict[str, object]] = {}
    for path in sorted((DOMAIN_CONFIG / "sources").glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(document, dict)
        source_id = document["source_id"]
        assert isinstance(source_id, str)
        manifests[source_id] = document
    return manifests


def _semantic_relations(manifest: dict[str, object]) -> set[str]:
    overlay = manifest["semantic_overlay"]
    assert isinstance(overlay, dict)
    relations = overlay["relations"]
    assert isinstance(relations, list)
    return {str(relation["relation"]) for relation in relations}


def test_domain_lab_quality_and_verified_artifacts_cover_every_new_view() -> None:
    manifests = _manifests()
    assert set(manifests) == NEW_SOURCE_IDS | BASE_SOURCE_IDS
    assert all(
        manifests[source_id]["minimum_quality_level"] == "L2"
        for source_id in NEW_SOURCE_IDS
    )

    evaluation = QualityEvaluation.load(
        DOMAIN_CONFIG / "quality-evaluation.yaml",
        set(manifests),
    )
    assert len(evaluation.cases) == 20
    assert Counter(case.source_id for case in evaluation.cases) == {
        source_id: 4 for source_id in NEW_SOURCE_IDS
    }
    assert Counter(
        case.source_id
        for case in evaluation.cases
        if case.expected_answerability is not None
    ) == {source_id: 1 for source_id in NEW_SOURCE_IDS}
    for source_id in NEW_SOURCE_IDS:
        expected_relations = {
            relation
            for case in evaluation.cases
            if case.source_id == source_id and case.expected_answerability is None
            for relation in case.expected_relations
        }
        assert expected_relations == _semantic_relations(manifests[source_id])

    verified = VerifiedQueryRegistry.load(
        DOMAIN_CONFIG / "verified-queries.yaml",
        set(manifests),
    )
    base_verified = VerifiedQueryRegistry.load(
        ROOT_DIRECTORY / "config" / "verified-queries.yaml",
        BASE_SOURCE_IDS,
    )
    assert verified.queries[: len(base_verified.queries)] == base_verified.queries
    new_queries = [query for query in verified.queries if query.source_id in NEW_SOURCE_IDS]
    assert len(new_queries) == 15
    assert Counter(query.source_id for query in new_queries) == {
        source_id: 3 for source_id in NEW_SOURCE_IDS
    }
    for source_id in NEW_SOURCE_IDS:
        source_queries = [query for query in new_queries if query.source_id == source_id]
        assert {relation for query in source_queries for relation in query.relations} == (
            _semantic_relations(manifests[source_id])
        )
        assert len({query.metadata_revision for query in source_queries}) == 1

    allowed_relations = {
        relation
        for source_id in NEW_SOURCE_IDS
        for relation in _semantic_relations(manifests[source_id])
    }
    for query in new_queries:
        validated = validate_sql(query.sql, allowed_relations=allowed_relations)
        assert validated.relations == tuple(sorted(query.relations))


def test_domain_lab_comments_cover_previously_unstructured_business_facts() -> None:
    comment_pattern = re.compile(
        r"^COMMENT ON COLUMN ai\.[a-z_]+\.[a-z_]+ IS '([^']*)';$",
        re.MULTILINE,
    )
    all_comments: list[str] = []
    for source_id, filename in SCHEMA_FILES.items():
        content = (
            ROOT_DIRECTORY / "docker" / "postgres" / "domain-lab" / filename
        ).read_text(encoding="utf-8")
        comments = comment_pattern.findall(content)
        assert len(comments) == EXPECTED_COLUMN_COMMENT_COUNTS[source_id]
        assert all(0 < len(comment) <= 2_000 for comment in comments)
        assert not re.search(r"\b(?:numeric|varchar|char)\s*\(\d", " ".join(comments))
        all_comments.extend(comments)

    combined = " ".join(all_comments)
    for required_boundary in (
        "no real-person identity or contact data",
        "no customer identity or service address",
        "sensitive in real clinical data but identifies no person",
        "no person identity or contact data",
        "aggregate only within one currency",
        "do not compare or sum values across different tests or units",
    ):
        assert required_boundary in combined


def test_domain_lab_preserves_base_source_manifests_byte_for_byte() -> None:
    for source_id in BASE_SOURCE_IDS:
        assert (DOMAIN_CONFIG / "sources" / f"{source_id}.yaml").read_bytes() == (
            ROOT_DIRECTORY / "config" / "sources" / f"{source_id}.yaml"
        ).read_bytes()
