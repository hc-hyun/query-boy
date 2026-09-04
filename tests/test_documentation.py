from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml

from tests.helpers import QUERY_CAVE_DIRECTORY, ROOT_DIRECTORY

DOCS = ROOT_DIRECTORY / "docs"
DECISIONS = DOCS / "decisions"
CURRENT_ENTRYPOINTS = (
    ROOT_DIRECTORY / "README.md",
    DOCS / "README.md",
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


def _markdown_targets(path: Path) -> set[Path]:
    targets: set[Path] = set()
    for match in re.finditer(
        r"!?\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")
    ):
        raw_target = match.group(1).strip()
        if raw_target.startswith("<") and ">" in raw_target:
            target = raw_target[1 : raw_target.index(">")]
        else:
            target = raw_target.split(maxsplit=1)[0]
        if target.startswith(("http://", "https://", "mailto:", "//", "#")):
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


def test_documentation_index_links_every_current_entrypoint() -> None:
    assert all(path.is_file() for path in CURRENT_ENTRYPOINTS)

    root_targets = _markdown_targets(ROOT_DIRECTORY / "README.md")
    assert (DOCS / "README.md").resolve() in root_targets

    index_targets = _markdown_targets(DOCS / "README.md")
    assert {path.resolve() for path in CURRENT_ENTRYPOINTS[2:]} <= index_targets


def test_decision_index_links_every_current_adr() -> None:
    index_targets = _markdown_targets(DECISIONS / "README.md")
    adr_files = set(DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))
    assert {path.resolve() for path in adr_files} <= index_targets


def test_current_docs_do_not_reference_retired_artifacts() -> None:
    current_docs = [ROOT_DIRECTORY / "README.md", *sorted(DOCS.rglob("*.md"))]
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
    references = {
        path.relative_to(ROOT_DIRECTORY): term
        for path in current_docs
        for term in retired
        if term.casefold() in path.read_text(encoding="utf-8").casefold()
    }
    assert not references


def test_source_contract_is_two_files_and_documented() -> None:
    assert not (ROOT_DIRECTORY / "config" / "database-profiles.yaml").exists()
    production_source_root = ROOT_DIRECTORY / "config" / "sources"
    assert not production_source_root.exists() or not tuple(production_source_root.iterdir())

    packages = sorted((QUERY_CAVE_DIRECTORY / "config" / "sources").iterdir())
    assert packages
    for package in packages:
        assert package.is_dir()
        assert {path.name for path in package.iterdir()} == {"source.yaml", "views.sql"}
        manifest = yaml.safe_load((package / "source.yaml").read_text(encoding="utf-8"))
        assert manifest["source_id"] == package.name
        assert set(manifest) >= {"database_profile", "reader_user"}

    databases = yaml.safe_load(
        (QUERY_CAVE_DIRECTORY / "config" / "database-profiles.yaml").read_text(encoding="utf-8")
    )
    assert databases["version"] == 1
    assert all(
        profile["authentication"] == {"type": "client-certificate"}
        and profile["sslmode"] == "verify-full"
        for profile in databases["profiles"].values()
    )

    contract = (DOCS / "source-extension-checklist.md").read_text(encoding="utf-8")
    assert "source.yaml" in contract
    assert "views.sql" in contract


def test_local_markdown_links_resolve() -> None:
    markdown_paths = [ROOT_DIRECTORY / "README.md", ROOT_DIRECTORY / "AGENTS.md"]
    markdown_paths.extend(sorted(DOCS.rglob("*.md")))
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


def test_bounded_pytest_traceback_does_not_render_argument_secrets(
    tmp_path: Path,
) -> None:
    secret = "synthetic-database-password-for-traceback-probe"
    probe = tmp_path / "test_traceback_secret_probe.py"
    probe.write_text(
        """
import os


def fail_with_secret_argument(secret: str) -> None:
    raise RuntimeError("bounded traceback probe")


def test_traceback_probe() -> None:
    fail_with_secret_argument(os.environ["QUERY_MAN_TRACEBACK_PROBE"])
""".lstrip(),
        encoding="utf-8",
    )
    environment = dict(os.environ)
    environment["QUERY_MAN_TRACEBACK_PROBE"] = secret
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--config-file",
            str(ROOT_DIRECTORY / "pyproject.toml"),
            "--quiet",
            str(probe),
        ],
        check=False,
        capture_output=True,
        cwd=ROOT_DIRECTORY,
        env=environment,
        text=True,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1
    assert "RuntimeError: bounded traceback probe" in output
    assert secret not in output
