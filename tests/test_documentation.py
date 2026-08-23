from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from tests.helpers import ROOT_DIRECTORY

ROADMAP = ROOT_DIRECTORY / "docs" / "implementation-roadmap.md"
ARCHITECTURE = ROOT_DIRECTORY / "docs" / "architecture.md"
BASELINE_AUDIT = (
    ROOT_DIRECTORY / "docs" / "verification" / "2026-08-23-completion-audit.md"
)
REFACTORING_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-23-refactoring-assurance.md"
)
CONTAINER_AUDIT = (
    ROOT_DIRECTORY / "docs" / "verification" / "2026-08-23-container-runtime.md"
)
MCP_SERVER_AUDIT = (
    ROOT_DIRECTORY / "docs" / "verification" / "2026-08-23-mcp-server-assurance.md"
)
DEVELOPMENT_TODO = ROOT_DIRECTORY / "docs" / "development-todo.md"
MCP_SOAK_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-23-mcp-multi-replica-soak.md"
)
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
    "REF": 15,
    "DEP": 8,
    "MCPX": 8,
}
EXPECTED_ACTIVE_ID_COUNTS = {"SOAK": 7, "SKILL": 10, "COST": 5, "TRACE": 4}


def test_roadmap_has_one_completed_checkbox_for_every_expected_id() -> None:
    text = ROADMAP.read_text(encoding="utf-8")
    matches = re.findall(r"^- \[([ x])\] `([A-Z]+)-(\d{2})`", text, re.MULTILINE)
    ids = [f"{prefix}-{number}" for _checked, prefix, number in matches]

    assert len(ids) == sum(EXPECTED_ID_COUNTS.values()) == 131
    assert len(ids) == len(set(ids))
    assert all(checked == "x" for checked, _prefix, _number in matches)
    for prefix, count in EXPECTED_ID_COUNTS.items():
        assert [item for item in ids if item.startswith(f"{prefix}-")] == [
            f"{prefix}-{number:02}" for number in range(1, count + 1)
        ]


def test_production_status_and_completion_audits_cover_every_roadmap_group() -> None:
    roadmap = ROADMAP.read_text(encoding="utf-8")
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    baseline_audit = BASELINE_AUDIT.read_text(encoding="utf-8")
    refactoring_audit = REFACTORING_AUDIT.read_text(encoding="utf-8")
    container_audit = CONTAINER_AUDIT.read_text(encoding="utf-8")
    mcp_server_audit = MCP_SERVER_AUDIT.read_text(encoding="utf-8")
    development_todo = DEVELOPMENT_TODO.read_text(encoding="utf-8")
    mcp_soak_audit = MCP_SOAK_AUDIT.read_text(encoding="utf-8")

    assert "Status: Production ready" in roadmap
    assert "Status: Production ready" in architecture
    assert "Status: Complete" in baseline_audit
    assert "Status: Complete" in refactoring_audit
    assert "Status: Complete" in container_audit
    assert "Status: Complete" in mcp_server_audit
    assert "Status: Active" in development_todo
    assert "Status: Complete" in mcp_soak_audit
    assert REFACTORING_AUDIT.name in roadmap
    assert REFACTORING_AUDIT.name in architecture
    assert CONTAINER_AUDIT.name in roadmap
    assert CONTAINER_AUDIT.name in architecture
    assert MCP_SERVER_AUDIT.name in roadmap
    assert MCP_SERVER_AUDIT.name in architecture
    assert DEVELOPMENT_TODO.name in roadmap
    assert DEVELOPMENT_TODO.name in architecture
    assert MCP_SOAK_AUDIT.name in roadmap
    assert MCP_SOAK_AUDIT.name in architecture
    for prefix, count in EXPECTED_ID_COUNTS.items():
        audit = {
            "DEP": container_audit,
            "MCPX": mcp_server_audit,
            "REF": refactoring_audit,
        }.get(prefix, baseline_audit)
        if prefix in {"DEP", "MCPX", "REF"}:
            for number in range(1, count + 1):
                assert f"`{prefix}-{number:02}`" in audit
        else:
            assert f"`{prefix}-01`" in audit
            assert f"`{prefix}-{count:02}`" in audit


def test_active_todo_has_prioritized_unique_checklists_and_soak_evidence() -> None:
    todo = DEVELOPMENT_TODO.read_text(encoding="utf-8")
    soak_audit = MCP_SOAK_AUDIT.read_text(encoding="utf-8")
    matches = re.findall(r"^- \[([ x])\] `([A-Z]+)-(\d{2})`", todo, re.MULTILINE)
    ids = [f"{prefix}-{number}" for _checked, prefix, number in matches]

    assert len(ids) == sum(EXPECTED_ACTIVE_ID_COUNTS.values()) == 26
    assert len(ids) == len(set(ids))
    for prefix, count in EXPECTED_ACTIVE_ID_COUNTS.items():
        prefix_matches = [
            (checked, f"{matched_prefix}-{number}")
            for checked, matched_prefix, number in matches
            if matched_prefix == prefix
        ]
        assert [item for _checked, item in prefix_matches] == [
            f"{prefix}-{number:02}" for number in range(1, count + 1)
        ]
        expected_status = "x" if prefix == "SOAK" else " "
        assert all(checked == expected_status for checked, _item in prefix_matches)

    for number in range(1, EXPECTED_ACTIVE_ID_COUNTS["SOAK"] + 1):
        assert f"`SOAK-{number:02}`" in soak_audit


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


def _markdown_heading_anchors(path: Path) -> set[str]:
    content = path.read_text(encoding="utf-8")
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for match in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", content, re.MULTILINE):
        heading = re.sub(r"[`*_~]", "", match.group(1))
        base = re.sub(r"[^\w -]", "", heading.lower()).strip().replace(" ", "-")
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return anchors


def test_local_markdown_links_resolve() -> None:
    markdown_paths = [ROOT_DIRECTORY / "README.md"]
    markdown_paths.extend(sorted((ROOT_DIRECTORY / "docs").rglob("*.md")))
    missing: list[str] = []

    for path in markdown_paths:
        content = path.read_text(encoding="utf-8")
        for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", content):
            raw_target = match.group(1).strip()
            if raw_target.startswith("<") and ">" in raw_target:
                target = raw_target[1 : raw_target.index(">")]
            else:
                target = raw_target.split(maxsplit=1)[0]
            if target.startswith(("http://", "https://", "mailto:", "//")):
                continue
            path_target, _separator, fragment = target.partition("#")
            relative_target = unquote(path_target.split("?", 1)[0])
            resolved = path if not relative_target else path.parent / relative_target
            if not resolved.exists():
                missing.append(f"{path.relative_to(ROOT_DIRECTORY)} -> {target}")
            elif fragment and resolved.suffix.lower() == ".md":
                if unquote(fragment) not in _markdown_heading_anchors(resolved):
                    missing.append(
                        f"{path.relative_to(ROOT_DIRECTORY)} -> {target} (missing anchor)"
                    )

    assert not missing, "Missing local Markdown links:\n" + "\n".join(missing)
