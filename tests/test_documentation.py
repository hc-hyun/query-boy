from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import yaml

import query_man.guarded_query.sql_validation as sql_validation_module
from tests.helpers import ROOT_DIRECTORY

ROADMAP = ROOT_DIRECTORY / "docs" / "implementation-roadmap.md"
ARCHITECTURE = ROOT_DIRECTORY / "docs" / "architecture.md"
DEVELOPMENT_TODO = ROOT_DIRECTORY / "docs" / "development-todo.md"
FUTURE_WORK = ROOT_DIRECTORY / "docs" / "future-work.md"
MODULE_INDEX = ROOT_DIRECTORY / "docs" / "modules" / "README.md"
VERIFICATION_DIRECTORY = ROOT_DIRECTORY / "docs" / "verification"
VERIFICATION_INDEX = VERIFICATION_DIRECTORY / "README.md"
LAUNCH_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0025-static-non-rls-first-launch.md"
)

MODULE_NAMES = (
    "source-catalog",
    "metadata",
    "guarded-query",
    "control-plane",
    "delivery",
    "runtime",
    "assurance",
)
REQUIRED_MODULE_HEADINGS = (
    "## 목적",
    "## 소유 책임",
    "## 소유하지 않는 책임",
    "## 현재 코드 위치",
    "## 제공 인터페이스와 소유 경계",
    "## 소비 인터페이스와 전제",
    "## 불변조건",
    "## 모듈 내부 변경",
    "## 사용자 승인이 필요한 경계 변경",
    "## 검증",
    "## 집중해서 읽을 범위",
)

EXPECTED_BASELINE_ID_COUNTS = {
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
EXPECTED_ACTIVE_TODO_IDS = ("LAUNCH-02",)
EXPECTED_PARKED_IDS = (
    "RLS-01",
    "RLS-02",
    "RLS-03",
    "ENC-01",
    "ENC-02",
    "TIME-03",
    "COST-01",
    "COST-02",
    "COST-03",
    "COST-04",
    "COST-05",
    "TRACE-01",
    "TRACE-02",
    "TRACE-03",
    "TRACE-04",
)
LOCKED_BASELINE_DESCRIPTIONS = {
    "SQL-04": (
        "함수와 operator를 추출하고 `BETWEEN` 같은 grammar construct를 effective operator로 "
        "정규화하며 승인한 cast type과 분석 함수를 제한한다."
    ),
    "SQL-08": (
        "정책 거부와 수정 가능한 database 의미 오류를 안정적인 reason code와 bounded detail로 "
        "반환하고 parser/database 내부 오류를 공개하지 않는다."
    ),
    "EXEC-10": (
        "수정 가능한 고정 SQLSTATE만 bounded `QUERY_INVALID`로 분리하고 나머지 DB 오류, "
        "timeout, cancel과 serialization failure를 비공개 또는 전용 external error로 매핑한다."
    ),
}

CRITICAL_NON_PYTHON_MAPPINGS = (
    "`config/sources/`, `config/budget-profiles.yaml`",
    "`config/access-policies*.yaml`",
    "`config/quality-evaluation.yaml`, `config/verified-queries.yaml`, `config/security-evaluation.yaml`",
    "`Dockerfile`, `compose.yaml`, `.env.example`",
    "`scripts/verify-container.sh`",
    "`.github/workflows/ci.yml`, `.github/workflows/mcp-soak.yml`",
    "`skills/query-man-text-to-sql/`",
    "`skills/query-man-source-onboarding/`",
    "`pyproject.toml` package/dependency/entrypoint sections",
    "`uv.lock`",
)
CRITICAL_SHARED_WRITER_REFERENCES = (
    "tests/helpers.py",
    "tests/conftest.py",
    "tests/control_database.py",
    "tests/test_documentation.py",
    "docs/development-todo.md",
    "docs/implementation-roadmap.md",
    "docs/verification/",
)


def test_completed_baseline_ledger_is_not_rewritten() -> None:
    text = ROADMAP.read_text(encoding="utf-8")
    matches = re.findall(r"^- \[([ x])\] `([A-Z]+)-(\d{2})`", text, re.MULTILINE)
    ids = [f"{prefix}-{number}" for _checked, prefix, number in matches]

    assert len(ids) == sum(EXPECTED_BASELINE_ID_COUNTS.values()) == 131
    assert len(ids) == len(set(ids))
    assert all(checked == "x" for checked, _prefix, _number in matches)
    for prefix, count in EXPECTED_BASELINE_ID_COUNTS.items():
        assert [item for item in ids if item.startswith(f"{prefix}-")] == [
            f"{prefix}-{number:02}" for number in range(1, count + 1)
        ]
    for item_id, expected in LOCKED_BASELINE_DESCRIPTIONS.items():
        match = re.search(
            rf"^- \[x\] `{re.escape(item_id)}` (.*(?:\n  .*)*)$",
            text,
            re.MULTILINE,
        )
        assert match is not None, item_id
        assert " ".join(match.group(1).split()) == expected


def test_active_todo_is_small_open_work_only() -> None:
    todo = DEVELOPMENT_TODO.read_text(encoding="utf-8")
    matches = re.findall(r"^- \[([ x])\] `([A-Z]+)-(\d{2})`", todo, re.MULTILINE)
    ids = tuple(f"{prefix}-{number}" for _checked, prefix, number in matches)

    assert ids == EXPECTED_ACTIVE_TODO_IDS
    assert len(ids) == len(set(ids))
    assert all(checked == " " for checked, _prefix, _number in matches)
    assert "- [x]" not in todo
    assert "LAUNCH-01-A" in todo
    assert "Repository implementation과 local acceptance는 protected environment 전환 권한이 아니다" in todo
    for heading in (
        "## Protected Environment Execution",
        "## 시작 전에 필요한 승인",
        "## 완료 조건",
        "## 즉시 중단할 조건",
        "## 현재 일정에 없는 일",
    ):
        assert heading in todo

    future = FUTURE_WORK.read_text(encoding="utf-8")
    parked_ids = tuple(
        match.group(1)
        for match in re.finditer(
            r"^- `([A-Z]+-\d{2})`:",
            future,
            re.MULTILINE,
        )
    )
    assert parked_ids == EXPECTED_PARKED_IDS
    assert "- [ ]" not in future
    assert "현재 구현 일정이나 변경 승인이 아님" in future
    for heading in (
        "## RLS source 제공",
        "## 결과 type 확대",
        "## Managed canonical-time cutover",
        "## DB-native 비용과 사용량 경보",
        "## Workflow trace",
    ):
        assert heading in future


def test_adr_0025_is_the_narrow_current_launch_authority() -> None:
    adr = LAUNCH_ADR.read_text(encoding="utf-8")
    assert "Status: Accepted" in adr
    assert "Decision ID: `LAUNCH-01-A`" in adr
    assert "`development-issues`, `market-voc`" in adr
    assert "180000 <= server_version < 190000" in adr
    assert "server_encoding == \"UTF8\"" in adr
    assert "client_encoding == \"UTF8\"" in adr
    assert "tenant 유무와 무관한 details 없는 `503 QUERY_UNAVAILABLE`" in adr
    for type_name, oid in (
        ("int8", 20),
        ("int2", 21),
        ("int4", 23),
        ("text", 25),
        ("date", 1082),
        ("timestamptz", 1184),
        ("numeric", 1700),
    ):
        assert f"`{type_name}` | {oid}" in adr
    assert "SQL policy version은 2에서 3으로" in adr
    assert "9개 query를 새 SQL policy로 전부 재실행" in adr
    assert "protected execution" in adr

    assert sql_validation_module._SQL_POLICY_VERSION == 3
    assert sql_validation_module.SQL_POLICY_REVISION in adr
    assert {
        path.stem for path in (ROOT_DIRECTORY / "config" / "sources").glob("*.yaml")
    } == {"development-issues", "market-voc"}


def test_current_navigation_documents_agree_on_launch_scope() -> None:
    documents = {
        "README": ROOT_DIRECTORY / "README.md",
        "architecture": ARCHITECTURE,
        "module index": MODULE_INDEX,
        "operations": ROOT_DIRECTORY / "docs" / "operations.md",
        "verified queries": ROOT_DIRECTORY / "docs" / "verified-queries.md",
        "TODO": DEVELOPMENT_TODO,
    }
    for label, path in documents.items():
        content = path.read_text(encoding="utf-8")
        assert "0025-static-non-rls-first-launch.md" in content, label
        assert "development-issues" in content, label
        assert "market-voc" in content, label
        assert "RLS" in content, label

    readme = documents["README"].read_text(encoding="utf-8")
    architecture = documents["architecture"].read_text(encoding="utf-8")
    operations = documents["operations"].read_text(encoding="utf-8")
    verified = documents["verified queries"].read_text(encoding="utf-8")
    assert "단일 Query Man replica" in readme
    assert "exact seven result" in architecture
    assert "LAUNCH-02" in operations
    assert "20, 21, 23, 25, 1082, 1184, 1700" in verified


def test_parked_research_is_not_presented_as_current_implementation() -> None:
    expected_statuses = {
        "0020-lossless-interval-and-json-numeric-encoding.md": "Superseded research",
        "0021-database-native-cost-attribution.md": "Parked research",
        "0022-w3c-workflow-trace-context.md": "Parked research",
        "0023-database-native-usage-spike-alert.md": "Parked research",
        "0024-rls-policy-drift-attestation.md": "Deferred research",
    }
    decision_root = ROOT_DIRECTORY / "docs" / "decisions"
    for filename, classification in expected_statuses.items():
        first_lines = "\n".join(
            (decision_root / filename).read_text(encoding="utf-8").splitlines()[:10]
        )
        assert classification in first_lines, filename
        assert "ADR 0025" in first_lines, filename


def test_module_docs_cover_owners_interfaces_and_current_python_files() -> None:
    index = MODULE_INDEX.read_text(encoding="utf-8")
    agents = (ROOT_DIRECTORY / "AGENTS.md").read_text(encoding="utf-8")
    assert "## 승인 대상 변경 절차" in index
    assert "## 새 데이터베이스 추가 시 영향" in index
    assert "docs/modules/README.md" in agents
    assert "Module interface의 의미 변경은 additive change를 포함해" in agents
    assert "baseline commit" in agents
    assert "수정 가능한 file allowlist" in agents
    assert "여러 agent가 같은 worktree를 공유하면" in agents

    for module_name in MODULE_NAMES:
        path = MODULE_INDEX.parent / module_name / "README.md"
        content = path.read_text(encoding="utf-8")
        assert f"({module_name}/README.md)" in index
        assert "Status: Physical package boundary active" in content
        for heading in REQUIRED_MODULE_HEADINGS:
            assert content.count(heading) == 1, (
                f"{path.relative_to(ROOT_DIRECTORY)}: {heading}"
            )

    source_root = ROOT_DIRECTORY / "src" / "query_man"
    for path in source_root.rglob("*.py"):
        relative = path.relative_to(source_root)
        mapped = relative.as_posix()
        assert f"`{mapped}`" in index, f"Unmapped module owner: {relative}"
    for mapping in CRITICAL_NON_PYTHON_MAPPINGS:
        assert mapping in index, mapping
    for reference in CRITICAL_SHARED_WRITER_REFERENCES:
        assert reference in index, reference

    errors = (source_root / "errors.py").read_text(encoding="utf-8")
    for class_name in re.findall(r"^class (\w+Error)\(", errors, re.MULTILINE):
        assert f"`{class_name}`" in index, f"Missing error owner: {class_name}"


def test_interface_term_is_narrow_and_other_change_categories_remain_explicit() -> None:
    paths = (
        ROOT_DIRECTORY / "AGENTS.md",
        MODULE_INDEX,
        ROOT_DIRECTORY
        / "docs"
        / "decisions"
        / "0018-module-ownership-and-contract-governance.md",
    )
    categories = (
        "External API/wire format",
        "Persisted/versioned format",
        "Policy/compatibility identity",
        "Safety/lifecycle invariant",
        "Ownership/composition boundary",
        "Protected operational procedure",
    )
    for path in paths:
        content = " ".join(path.read_text(encoding="utf-8").split())
        assert "allowed dependency map" in content
        assert "shape/signature" in content
        assert "input/output/domain-error semantics" in content
        for category in categories:
            assert category in content, f"{path.name}: {category}"

    terminology_paths = (
        ROOT_DIRECTORY / "AGENTS.md",
        MODULE_INDEX,
        *(MODULE_INDEX.parent / name / "README.md" for name in MODULE_NAMES),
    )
    historical_filenames = (
        "0018-module-ownership-and-contract-governance.md",
        "0002-guarded-query-contract.md",
    )
    for path in terminology_paths:
        content = path.read_text(encoding="utf-8")
        for filename in historical_filenames:
            content = content.replace(filename, "")
        assert re.search(r"(?i)module\s+contract|모듈\s*간의?\s*계약", content) is None, path


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
    for path in (ROOT_DIRECTORY / "src" / "query_man").rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert not any(value in content for value in forbidden), path


def test_container_inputs_are_immutable_and_revision_labeled() -> None:
    dockerfile = (ROOT_DIRECTORY / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT_DIRECTORY / "compose.yaml").read_text(encoding="utf-8")
    workflow = (ROOT_DIRECTORY / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert len(re.findall(r"^FROM .*@sha256:[0-9a-f]{64}", dockerfile, re.MULTILINE)) == 2
    assert "COPY --from=ghcr.io/astral-sh/uv:0.9.18@sha256:" in dockerfile
    assert "ARG QUERY_MAN_VCS_REF" in dockerfile
    assert 'LABEL org.opencontainers.image.revision="${QUERY_MAN_VCS_REF}"' in dockerfile
    assert len(re.findall(r"image: postgres:[^\n]+@sha256:[0-9a-f]{64}", compose)) == 2
    assert 'payload == b\'{"status":"ready"}\'' in compose
    assert "QUERY_MAN_VCS_REF: ${{ github.sha }}" in workflow
    assert (
        'docker compose build --build-arg QUERY_MAN_VCS_REF="$QUERY_MAN_VCS_REF" query-man'
        in workflow
    )
    assert 'test "$revision" = "$QUERY_MAN_VCS_REF"' in workflow


def test_managed_acceptance_compose_uses_an_isolated_project() -> None:
    base = yaml.safe_load((ROOT_DIRECTORY / "compose.yaml").read_text(encoding="utf-8"))
    acceptance = yaml.safe_load(
        (ROOT_DIRECTORY / "compose.acceptance.yaml").read_text(encoding="utf-8")
    )
    workflow = (ROOT_DIRECTORY / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert base["name"] == "query-man"
    assert acceptance["name"] == "query-man-managed-acceptance"
    assert (
        base["services"]["postgres"]["container_name"]
        != acceptance["services"]["postgres"]["container_name"]
    )
    assert "COMPOSE_FILE: compose.yaml:compose.acceptance.yaml" in workflow


def test_verification_index_lists_every_immutable_record_once() -> None:
    index = VERIFICATION_INDEX.read_text(encoding="utf-8")
    evidence_names = {
        path.name
        for path in VERIFICATION_DIRECTORY.glob("*.md")
        if path != VERIFICATION_INDEX
    }
    rows = re.findall(
        r"^\| \[([^\]]+\.md)\]\(([^)]+\.md)\) \| ([^|]+) \| ([^|]+) \|",
        index,
        re.MULTILINE,
    )
    indexed_names = [target for _label, target, _title, _status in rows]
    assert set(indexed_names) == evidence_names
    assert len(indexed_names) == len(evidence_names)
    assert "immutable record" in index
    assert "현재 구현의 단일 완료 증거가 아니다" in index
    for required in (
        "2026-08-23-completion-audit.md",
        "2026-08-25-canonical-time-stability.md",
        "2026-08-25-source-database-corners.md",
        "2026-08-26-lower-track-contract-prework.md",
        "2026-08-26-rls-policy-drift.md",
    ):
        assert required in evidence_names

    for label, name, indexed_title, indexed_status in rows:
        assert label == name
        evidence = (VERIFICATION_DIRECTORY / name).read_text(encoding="utf-8")
        title = re.search(r"^# (.+)$", evidence, re.MULTILINE)
        assert title is not None
        assert indexed_title.strip() == title.group(1).strip()
        status = re.search(r"^Status: (.+)$", evidence, re.MULTILINE)
        expected = "미기재" if status is None else status.group(1).strip()
        assert indexed_status.replace("`", "").strip() == expected.replace(
            "`", ""
        ).strip()


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
    markdown_paths = [ROOT_DIRECTORY / "README.md", ROOT_DIRECTORY / "AGENTS.md"]
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
