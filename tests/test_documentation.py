from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

from tests.helpers import QUERY_CAVE_DIRECTORY, ROOT_DIRECTORY

DOCS = ROOT_DIRECTORY / "docs"
DECISIONS = DOCS / "decisions"
ROOT_README = ROOT_DIRECTORY / "README.md"
DOCUMENTATION_INDEX = DOCS / "README.md"
CURRENT_GUIDES = (
    DOCS / "architecture.md",
    DOCS / "glossary.md",
    DOCS / "operations.md",
    DOCS / "source-extension-checklist.md",
    DOCS / "database-certificate-authentication.md",
    DOCS / "query-cost-control.md",
    DOCS / "development-todo.md",
    DOCS / "development-guidelines.md",
    DOCS / "modules" / "README.md",
    DOCS / "decisions" / "README.md",
    DOCS / "verification" / "README.md",
    QUERY_CAVE_DIRECTORY / "README.md",
)
CURRENT_MARKDOWN = (
    ROOT_README,
    ROOT_DIRECTORY / "AGENTS.md",
    QUERY_CAVE_DIRECTORY / "README.md",
    *sorted(DOCS.rglob("*.md")),
)
EXTERNAL_LINK_PREFIXES = ("http://", "https://", "mailto:", "//")


def _markdown_destinations(path: Path) -> tuple[str, ...]:
    destinations: list[str] = []
    for match in re.finditer(
        r"!?\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")
    ):
        raw_target = match.group(1).strip()
        if raw_target.startswith("<") and ">" in raw_target:
            destinations.append(raw_target[1 : raw_target.index(">")])
        else:
            destinations.append(raw_target.split(maxsplit=1)[0])
    return tuple(destinations)


def _markdown_targets(path: Path) -> set[Path]:
    targets: set[Path] = set()
    for target in _markdown_destinations(path):
        if target.startswith((*EXTERNAL_LINK_PREFIXES, "#")):
            continue
        relative, _separator, _fragment = target.partition("#")
        targets.add((path.parent / unquote(relative.split("?", 1)[0])).resolve())
    return targets


def _markdown_heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for match in re.finditer(
        r"^#{1,6}\s+(.+?)\s*#*\s*$",
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    ):
        heading = re.sub(r"[`*_~]", "", match.group(1))
        base = re.sub(r"[^\w -]", "", heading.lower()).strip().replace(" ", "-")
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        anchors.add(base if occurrence == 0 else f"{base}-{occurrence}")
    return anchors


def test_documentation_index_links_every_current_guide() -> None:
    required_files = (ROOT_README, DOCUMENTATION_INDEX, *CURRENT_GUIDES)
    missing_files = [
        str(path.relative_to(ROOT_DIRECTORY)) for path in required_files if not path.is_file()
    ]
    assert not missing_files, "Missing current documentation:\n" + "\n".join(missing_files)

    root_targets = _markdown_targets(ROOT_README)
    assert DOCUMENTATION_INDEX.resolve() in root_targets, "README.md must link to docs/README.md"

    index_targets = _markdown_targets(DOCUMENTATION_INDEX)
    missing_index_targets = [
        str(path.relative_to(ROOT_DIRECTORY))
        for path in CURRENT_GUIDES
        if path.resolve() not in index_targets
    ]
    assert not missing_index_targets, "docs/README.md does not link to:\n" + "\n".join(
        missing_index_targets
    )


def test_decision_index_links_every_current_adr() -> None:
    index_targets = _markdown_targets(DECISIONS / "README.md")
    adr_files = set(DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
    missing_adrs = sorted(
        str(path.relative_to(ROOT_DIRECTORY))
        for path in adr_files
        if path.resolve() not in index_targets
    )
    assert not missing_adrs, "Decision index does not link to:\n" + "\n".join(missing_adrs)


def test_current_docs_do_not_reference_retired_artifacts() -> None:
    retired = (
        "config/domain-lab",
        "compose.domain-lab",
        ".env.domain-lab",
        "apply-domain-lab-comments",
        "verified-queries.yaml",
        "quality-evaluation.yaml",
        "config/sources/*.yaml",
        "compose.acceptance.yaml",
        "diagnostic capture",
        "diagnostic_consent",
        "QUERY_MAN_OAUTH_",
        "AuthBridge",
        "JWKS",
        "/mcp",
        "mcp_server.py",
        "mcp-soak",
        "query-man-text-to-sql",
        "query-man-source-onboarding",
        "semantic_overlay",
        "answerability",
        "relevance.py",
    )
    references = [
        f"{path.relative_to(ROOT_DIRECTORY)}: {term}"
        for path in CURRENT_MARKDOWN
        for term in retired
        if term.casefold() in path.read_text(encoding="utf-8").casefold()
    ]
    assert not references, "Current documentation references retired artifacts:\n" + "\n".join(
        references
    )


def test_source_onboarding_documents_exact_package_artifacts() -> None:
    contract = (DOCS / "source-extension-checklist.md").read_text(encoding="utf-8")
    missing_artifacts = [
        artifact for artifact in ("source.yaml", "views.sql") if artifact not in contract
    ]
    assert not missing_artifacts, "Source onboarding omits:\n" + "\n".join(missing_artifacts)


def test_local_markdown_links_resolve() -> None:
    missing: list[str] = []
    for path in CURRENT_MARKDOWN:
        for target in _markdown_destinations(path):
            if target.startswith(EXTERNAL_LINK_PREFIXES):
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
