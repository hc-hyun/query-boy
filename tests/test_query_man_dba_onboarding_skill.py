from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIRECTORY = ROOT / ".agents" / "skills" / "query-man-dba-onboarding"


def test_dba_skill_is_repo_scoped_and_explicit_only() -> None:
    configuration = yaml.safe_load((SKILL_DIRECTORY / "agents" / "openai.yaml").read_text(encoding="utf-8"))

    assert configuration["policy"]["allow_implicit_invocation"] is False
    assert "$query-man-dba-onboarding" in configuration["interface"]["default_prompt"]
    assert ".agents" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def test_dba_skill_references_are_packaged() -> None:
    assert (SKILL_DIRECTORY / "references" / "credential-boundary.md").is_file()
    assert (SKILL_DIRECTORY / "references" / "execution-checklist.md").is_file()
