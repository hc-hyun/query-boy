from pathlib import Path

import yaml

from tests.helpers import ROOT_DIRECTORY

SKILL_PATH = ROOT_DIRECTORY / "skills" / "query-man-text-to-sql" / "SKILL.md"


def _skill_frontmatter(path: Path) -> dict[str, object]:
    content = path.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    _start, frontmatter, _body = content.split("---\n", 2)
    document = yaml.safe_load(frontmatter)
    assert isinstance(document, dict)
    return document


def test_text_to_sql_skill_requires_actual_query_man_tools_and_fails_closed() -> None:
    content = SKILL_PATH.read_text(encoding="utf-8")
    frontmatter = _skill_frontmatter(SKILL_PATH)

    assert frontmatter["name"] == "query-man-text-to-sql"
    assert "list_sources" in content
    assert "get_context" in content
    assert "query" in content
    assert "If any required\ntool is unavailable or disconnected" in content
    assert "state that the query was not executed and stop" in content
    assert "Do not start\na server, call HTTP directly, connect to PostgreSQL" in content
    assert "fixture SQL" in content
    assert "seed data" in content
    assert "Never present a value as a query result unless" in content
    assert "the `query` tool returned it" in content
