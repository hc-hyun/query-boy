from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import query_man.guarded_query.sql_validation as sql_validation_module
from tests.helpers import ROOT_DIRECTORY

ARCHITECTURE = ROOT_DIRECTORY / "docs" / "architecture.md"
DEVELOPMENT_TODO = ROOT_DIRECTORY / "docs" / "development-todo.md"
MODULE_INDEX = ROOT_DIRECTORY / "docs" / "modules" / "README.md"
DECISION_DIRECTORY = ROOT_DIRECTORY / "docs" / "decisions"
DECISION_INDEX = DECISION_DIRECTORY / "README.md"
VERIFICATION_DIRECTORY = ROOT_DIRECTORY / "docs" / "verification"
VERIFICATION_INDEX = VERIFICATION_DIRECTORY / "README.md"
LAUNCH_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0025-static-non-rls-first-launch.md"
)
SOURCE_AUTHORITY_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0030-git-reviewed-yaml-source-authority.md"
)
PII_BOUNDARY_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0031-no-pii-curated-view-boundary.md"
)
TEMP_ADMISSION_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0032-reader-temp-admission-relaxation.md"
)

MODULE_NAMES = (
    "source-catalog",
    "metadata",
    "guarded-query",
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

EXPECTED_ACTIVE_TODO_IDS = ("DBENV-01", "AUTHENV-01", "LAUNCH-02")
EXPECTED_PARKED_ID_RANGES = (
    "`RLS-01`~`RLS-03`",
    "`ENC-01`~`ENC-02`",
    "`DBAUTH-01`~`DBAUTH-03`",
    "`COST-01`~`COST-05`",
    "`TRACE-01`~`TRACE-04`",
)

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
    "AGENTS.md",
    "tests/helpers.py",
    "tests/test_documentation.py",
    "docs/development-todo.md",
    "docs/decisions/README.md",
    "docs/verification/README.md",
)


def test_current_tree_keeps_current_decisions_and_git_archive_pointer() -> None:
    required_decisions = {
        "0001-postgresql-ast-validation.md",
        "0002-guarded-query-contract.md",
        "0003-reader-and-resolved-object-policy.md",
        "0006-mcp-transport-and-workflow.md",
        "0025-static-non-rls-first-launch.md",
        "0027-consent-gated-diagnostic-capture.md",
        "0030-git-reviewed-yaml-source-authority.md",
    }
    decision_files = {
        path.name for path in DECISION_DIRECTORY.glob("[0-9][0-9][0-9][0-9]-*.md")
    }
    assert required_decisions <= decision_files
    assert all(
        int(filename[:4]) in {1, 2, 3, 6, 25, 27, 30}
        or int(filename[:4]) > 30
        for filename in decision_files
    )

    decision_index = DECISION_INDEX.read_text(encoding="utf-8")
    assert "현행 세부 계약" in decision_index
    assert "1ff390ab67df215181810a84ac8b2ca8570eceee" in decision_index
    assert "Git history를 rewrite하지 않습니다" in decision_index

    for archived_name in (
        "implementation-roadmap.md",
        "future-work.md",
        "verified-queries.md",
    ):
        assert not (ROOT_DIRECTORY / "docs" / archived_name).exists()
    assert {path.name for path in VERIFICATION_DIRECTORY.glob("*.md")} == {
        "README.md"
    }


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
    assert "`DBENV-01`과 `AUTHENV-01`의 exact inventory와 evidence가 완료" in todo
    assert "authentication mapper 또는 application code를 현장에서 새로 구현하지 않는다" in todo
    for heading in (
        "## Protected Environment Execution",
        "## 시작 전에 필요한 승인",
        "## 완료 조건",
        "## 즉시 중단할 조건",
        "## 현재 일정에 없는 일",
    ):
        assert heading in todo

    for parked_range in EXPECTED_PARKED_ID_RANGES:
        assert parked_range in todo
    assert "active queue가 아닙니다" in todo
    assert "Git history를 rewrite하지 않습니다" in todo


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


def test_adr_0030_is_the_only_current_source_authority() -> None:
    adr = SOURCE_AUTHORITY_ADR.read_text(encoding="utf-8")
    assert "Status: Accepted" in adr
    assert "Decision ID: `QB-YAML-SOURCE-AUTHORITY-20260829`" in adr
    assert "config/sources/*.yaml" in adr
    assert "config/verified-queries.yaml" in adr
    assert "config/budget-profiles.yaml" in adr
    assert "fail-closed" in adr
    assert "Control DB" in adr
    assert "drop" in adr.lower()

    assert not any(
        (ROOT_DIRECTORY / "src" / "query_man" / "managed").glob("*.py")
    )
    assert not (ROOT_DIRECTORY / "compose.acceptance.yaml").exists()
    assert not any((ROOT_DIRECTORY / "config" / "onboarding").glob("*"))
    assert not any(
        (ROOT_DIRECTORY / "docker" / "postgres" / "init" / "control-migrations").glob("*")
    )


def test_adr_0031_moves_source_pii_boundary_to_db_owner_views() -> None:
    adr = PII_BOUNDARY_ADR.read_text(encoding="utf-8")

    assert "Status: Accepted" in adr
    assert "Decision ID: `QB-NO-PII-VIEW-BOUNDARY-20260830`" in adr
    assert "DB owner는 개인정보와 개인 민감정보를 제거한 reviewed curated view만" in adr
    assert "탐지, 분류, masking/pseudonymization 또는 column 단위로" in adr
    assert "verification 항목만 supersede한다" in adr
    assert "Git-reviewed YAML authority" in adr
    assert "Source manifest schema" in adr
    assert "database\nDDL은 바뀌지 않는다" in adr


def test_adr_0032_removes_only_database_temp_admission_check() -> None:
    adr = TEMP_ADMISSION_ADR.read_text(encoding="utf-8")

    assert "Status: Accepted" in adr
    assert "Decision ID: `QB-READER-TEMP-RELAX-20260830`" in adr
    assert "Reader가 database `TEMP` privilege를 보유하는지는 source admission 조건이 아니다" in adr
    assert "[ADR 0003]" in adr
    assert "[ADR 0001]" in adr
    assert "`SELECT INTO`" in adr
    assert "`pg_temp`" in adr
    assert "Source manifest/YAML schema" in adr
    assert "SQL policy\nrevision" in adr
    assert "database DDL" in adr
    assert "직접 사용하면 그 별도 session에서 temporary" in adr


def test_current_navigation_documents_agree_on_launch_scope() -> None:
    documents = {
        "README": ROOT_DIRECTORY / "README.md",
        "docs index": ROOT_DIRECTORY / "docs" / "README.md",
        "architecture": ARCHITECTURE,
        "module index": MODULE_INDEX,
        "operations": ROOT_DIRECTORY / "docs" / "operations.md",
        "assurance": MODULE_INDEX.parent / "assurance" / "README.md",
        "TODO": DEVELOPMENT_TODO,
    }
    for label, path in documents.items():
        content = path.read_text(encoding="utf-8")
        assert "0025-static-non-rls-first-launch.md" in content, label
        assert "development-issues" in content, label
        assert "market-voc" in content, label
        assert "RLS" in content, label

    readme = documents["README"].read_text(encoding="utf-8")
    docs_index = documents["docs index"].read_text(encoding="utf-8")
    architecture = documents["architecture"].read_text(encoding="utf-8")
    operations = documents["operations"].read_text(encoding="utf-8")
    assurance = documents["assurance"].read_text(encoding="utf-8")
    assert "단일 Query Man replica" in readme
    assert "exact seven result" in architecture
    for task_id in EXPECTED_ACTIVE_TODO_IDS:
        assert task_id in operations
    assert "20, 21, 23, 25, 1082, 1184, 1700" in assurance
    for heading in (
        "## 공통 제품 문서",
        "## 개발자 문서",
        "## 운영자·DBA 문서",
        "## 현재 결정과 Git 기록",
    ):
        assert heading in docs_index
    assert "독자 구분은 탐색을 돕는 표지일 뿐" in docs_index


def test_parked_research_is_not_presented_as_current_implementation() -> None:
    decisions = DECISION_INDEX.read_text(encoding="utf-8")
    assert "일정이나 구현 승인이 아닙니다" in decisions
    for topic in (
        "RLS serving",
        "Result type 확대",
        "DB-backed source authority",
        "DB-native 비용·경보",
        "Workflow trace",
    ):
        assert topic in decisions
    assert "현재 모든 RLS source를 DB 접근 전에 차단" in decisions
    assert "test_rls_source_requires_base_policy_drift_to_preserve_isolation" in decisions
    assert "test_enc_01_" in decisions


def test_consolidated_current_contracts_cover_archived_decisions() -> None:
    metadata = (MODULE_INDEX.parent / "metadata" / "README.md").read_text(
        encoding="utf-8"
    )
    for fragment in (
        "valid·ready·non-partial",
        "max_context_columns_per_relation` 기본값은 40",
        "`column_count`, `returned_column_count`, `columns_truncated`",
        "`L2`: L1 + source와 **현재 metadata revision**",
    ):
        assert fragment in metadata

    guarded = (MODULE_INDEX.parent / "guarded-query" / "README.md").read_text(
        encoding="utf-8"
    )
    for fragment in (
        "`REPEATABLE READ READ ONLY`",
        "`TimeZone=UTC`",
        "`+00:00`",
        "`date_trunc('month', received_at, 'Asia/Seoul')`",
    ):
        assert fragment in guarded

    delivery = (MODULE_INDEX.parent / "delivery" / "README.md").read_text(
        encoding="utf-8"
    )
    assert "`caller_id`, `tenant_id`, `token_env`," in delivery
    assert "optional `diagnostic_consent`" in delivery
    assert "`SOURCE_NOT_FOUND`" in delivery

    operations = (ROOT_DIRECTORY / "docs" / "operations.md").read_text(
        encoding="utf-8"
    )
    assert "30분/50건" in operations
    assert "7일/100건" in operations

    jwt = (ROOT_DIRECTORY / "docs" / "resource-server-jwt-auth.md").read_text(
        encoding="utf-8"
    )
    for fragment in (
        "최대 1 MiB",
        "60초 clock-skew allowance",
        "process-wide 30초 cooldown",
        "`authbridge`",
        "`AUTHENV-01`",
        "`LAUNCH-02`",
        "이 단계에서는 route하지 않는다",
    ):
        assert fragment in jwt


def test_module_docs_cover_owners_interfaces_and_current_python_files() -> None:
    index = MODULE_INDEX.read_text(encoding="utf-8")
    agents = (ROOT_DIRECTORY / "AGENTS.md").read_text(encoding="utf-8")
    normalized_agents = " ".join(agents.split())
    assert "## 승인 대상 변경 절차" in index
    assert "## 새 데이터베이스 추가 시 영향" in index
    assert "docs/modules/README.md" in agents
    assert "내부 Python shape/signature 변경" in agents
    assert "별도 사용자 승인 없이" in normalized_agents
    assert "모든 public Python symbol을 열거하지 않는다" in agents
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


def test_internal_interface_flexibility_keeps_material_change_categories_explicit() -> None:
    paths = (
        ROOT_DIRECTORY / "AGENTS.md",
        MODULE_INDEX,
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
        assert "allowed dependency map" in content.casefold()
        assert "shape/signature" in content
        assert "input/output/domain-error semantics" in content
        assert "모든 public Python symbol" in content
        assert "별도 사용자 승인" in content
        for category in categories:
            assert category in content, f"{path.name}: {category}"

    terminology_paths = (
        ROOT_DIRECTORY / "AGENTS.md",
        MODULE_INDEX,
        *(MODULE_INDEX.parent / name / "README.md" for name in MODULE_NAMES),
    )
    for path in terminology_paths:
        content = path.read_text(encoding="utf-8")
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
    assert len(re.findall(r"image: postgres:[^\n]+@sha256:[0-9a-f]{64}", compose)) == 1
    assert 'payload == b\'{"status":"ready"}\'' in compose
    assert "QUERY_MAN_VCS_REF: ${{ github.sha }}" in workflow
    assert (
        'docker compose build --build-arg QUERY_MAN_VCS_REF="$QUERY_MAN_VCS_REF" query-man'
        in workflow
    )
    assert 'test "$revision" = "$QUERY_MAN_VCS_REF"' in workflow


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


def test_ci_and_compose_use_only_the_yaml_authority() -> None:
    compose = (ROOT_DIRECTORY / "compose.yaml").read_text(encoding="utf-8")
    workflow = (ROOT_DIRECTORY / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "name: query-man" in compose
    assert "QUERY_MAN_SOURCE_MODE" not in compose
    assert "postgres-control-recovery-source" not in compose
    assert "managed-acceptance" not in workflow
    assert "--ignore=tests/test_managed" not in workflow


def test_verification_uses_commit_provenance_and_external_protected_records() -> None:
    index = VERIFICATION_INDEX.read_text(encoding="utf-8")
    assert "1ff390ab67df215181810a84ac8b2ca8570eceee" in index
    assert "uv run ruff check ." in index
    assert "uv run mypy src" in index
    assert "uv run pytest" in index
    for task_id in EXPECTED_ACTIVE_TODO_IDS:
        assert re.search(
            rf"\| `{re.escape(task_id)}` [^|]*\| [^|]*미실행",
            index,
        )
    assert "외부 Control DB inventory·보존·폐기" in index
    assert "append-only/immutable" in index
    assert "날짜별 PASS 요약 문서를 만들지 않습니다" in index
    assert "Git history를" in index and "rewrite" in index


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
    markdown_paths.extend(
        path
        for path in sorted((ROOT_DIRECTORY / "docs").rglob("*.md"))
    )
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
