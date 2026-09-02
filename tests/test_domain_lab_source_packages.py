from __future__ import annotations

import re

from query_man.source_catalog.registry import SourceRegistry
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
EXPECTED_COLUMN_COMMENT_COUNTS = {
    "clinical-operations": 20,
    "energy-telemetry": 21,
    "parcel-logistics": 22,
    "retail-commerce": 26,
    "saas-billing": 28,
}


def test_domain_lab_comments_cover_previously_unstructured_business_facts() -> None:
    comment_pattern = re.compile(
        r"^COMMENT ON COLUMN ai\.[a-z_]+\.[a-z_]+ IS '([^']*)';$",
        re.MULTILINE,
    )
    all_comments: list[str] = []
    for source_id, expected_count in EXPECTED_COLUMN_COMMENT_COUNTS.items():
        content = (DOMAIN_CONFIG / "sources" / source_id / "views.sql").read_text(
            encoding="utf-8"
        )
        comments = comment_pattern.findall(content)
        assert len(comments) == expected_count
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


def test_domain_lab_preserves_base_manifests_except_owned_view_path() -> None:
    for source_id in BASE_SOURCE_IDS:
        base = (
            ROOT_DIRECTORY / "config" / "sources" / source_id / "source.yaml"
        ).read_text(encoding="utf-8")
        domain_lab = (
            DOMAIN_CONFIG / "sources" / source_id / "source.yaml"
        ).read_text(encoding="utf-8")

        assert domain_lab == base.replace(
            f"config/sources/{source_id}/views.sql",
            f"config/domain-lab/sources/{source_id}/views.sql",
        )


def test_domain_lab_source_manifests_pass_current_registry_schema() -> None:
    source_ids = NEW_SOURCE_IDS | BASE_SOURCE_IDS
    environment = {
        "POSTGRES_PORT": "5432",
        **{
            f"{source_id.replace('-', '_').upper()}_READER_PASSWORD": "domain-lab-secret"
            for source_id in source_ids
        },
    }

    registry = SourceRegistry.load(
        DOMAIN_CONFIG / "sources",
        ROOT_DIRECTORY / "config" / "budget-profiles.yaml",
        environment,
    )

    assert registry.source_ids() == frozenset(source_ids)
    for source_id in source_ids:
        source = registry.get(source_id)
        assert source is not None
        assert source.connection.sslmode == "disable"
