from __future__ import annotations

import re

from tests.helpers import ROOT_DIRECTORY

ROADMAP = ROOT_DIRECTORY / "docs" / "implementation-roadmap.md"
ARCHITECTURE = ROOT_DIRECTORY / "docs" / "architecture.md"
AUDIT = ROOT_DIRECTORY / "docs" / "verification" / "2026-08-23-completion-audit.md"
EXPECTED_ID_COUNTS = {
    "BASE": 10,
    "DEC": 9,
    "SQL": 10,
    "EXEC": 13,
    "META": 10,
    "MCP": 8,
    "ONB": 9,
    "AUTH": 7,
    "OPS": 8,
    "REL": 8,
    "EXT": 8,
}


def test_roadmap_has_one_completed_checkbox_for_every_expected_id() -> None:
    text = ROADMAP.read_text(encoding="utf-8")
    matches = re.findall(r"^- \[([ x])\] `([A-Z]+)-(\d{2})`", text, re.MULTILINE)
    ids = [f"{prefix}-{number}" for _checked, prefix, number in matches]

    assert len(ids) == sum(EXPECTED_ID_COUNTS.values()) == 100
    assert len(ids) == len(set(ids))
    assert all(checked == "x" for checked, _prefix, _number in matches)
    for prefix, count in EXPECTED_ID_COUNTS.items():
        assert [item for item in ids if item.startswith(f"{prefix}-")] == [
            f"{prefix}-{number:02}" for number in range(1, count + 1)
        ]


def test_production_status_and_completion_audit_cover_every_roadmap_group() -> None:
    roadmap = ROADMAP.read_text(encoding="utf-8")
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    audit = AUDIT.read_text(encoding="utf-8")

    assert "Status: Production ready" in roadmap
    assert "Status: Production ready" in architecture
    assert "Status: Complete" in audit
    for prefix, count in EXPECTED_ID_COUNTS.items():
        assert f"`{prefix}-01`" in audit
        assert f"`{prefix}-{count:02}`" in audit


def test_runtime_has_no_fixture_source_specialization() -> None:
    forbidden = {
        "development-issues",
        "development_issues",
        "market-voc",
        "market_voc",
        "support-tickets",
        "support_tickets",
        "commerce-edges",
        "commerce_edges",
    }
    for path in (ROOT_DIRECTORY / "src" / "query_man").glob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert not any(value in content for value in forbidden), path
