from __future__ import annotations

import ast
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
SOURCE_MANAGEMENT_PLAN = ROOT_DIRECTORY / "docs" / "source-management-plane.md"
CENTRAL_SOURCE_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0016-centralized-source-management-plane.md"
)
SHARED_ACCESS_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0017-shared-source-access-and-resource-tier.md"
)
MODULE_INDEX = ROOT_DIRECTORY / "docs" / "modules" / "README.md"
MODULE_CONTRACT_DECISION_GUIDE = (
    ROOT_DIRECTORY / "docs" / "module-contract-decision-guide.md"
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
    "## 제공 계약",
    "## 소비 계약",
    "## 불변조건",
    "## 모듈 내부 변경",
    "## 사용자 승인이 필요한 계약 변경",
    "## 검증",
    "## 집중해서 읽을 범위",
)
MCP_SOAK_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-23-mcp-multi-replica-soak.md"
)
CONTROL_MIGRATION_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-23-control-schema-migrations.md"
)
MANAGED_SOURCE_STARTUP_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-23-managed-source-startup.md"
)
SHARED_ACCESS_AUDIT = (
    ROOT_DIRECTORY / "docs" / "verification" / "2026-08-23-shared-access.md"
)
SOURCE_MANAGEMENT_CATALOG_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-23-source-management-catalog.md"
)
SOURCE_MUTATION_RECEIPT_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-23-source-mutation-receipts.md"
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
EXPECTED_OPEN_TODO_IDS = (
    "MOD-08",
    "CTRL-06",
    "CTRL-07",
    "CTRL-08",
    "CTRL-09",
    "SKILL-01",
    "SKILL-02",
    "SKILL-03",
    "SKILL-04",
    "SKILL-05",
    "SKILL-06",
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
EXPECTED_POST_BASELINE_COMPLETED_IDS = (
    "SOAK-01",
    "SOAK-02",
    "SOAK-03",
    "SOAK-04",
    "SOAK-05",
    "SOAK-06",
    "SOAK-07",
    "CTRL-01",
    "CTRL-02",
    "CTRL-03",
    "CTRL-04",
    "CTRL-05",
    "SQLX-01",
    "QCORR-01",
    "MOD-01",
    "MOD-02",
    "MOD-03",
    "RTSAFE-01",
    "MOD-04",
    "MOD-05",
    "MOD-06",
    "MOD-07",
)
CRITICAL_NON_PYTHON_MODULE_MAPPINGS = (
    "| `config/sources/`, `config/budget-profiles.yaml` | Source Catalog |",
    "| `config/access-policies*.yaml` | Delivery |",
    "| `config/quality-evaluation.yaml`, `config/verified-queries.yaml`, "
    "`config/security-evaluation.yaml` | Assurance |",
    "| `config/onboarding/<source>.yaml`, `config/onboarding/<source>-l2.yaml` | "
    "Source Catalog |",
    "| `config/onboarding/<source>-verified-query.yaml` | Assurance |",
    "| `Dockerfile`, `compose.yaml`, `.env.example` | Runtime |",
    "| `scripts/verify-container.sh` | Assurance |",
    "| `scripts/apply-control-schema.sh`, `scripts/control-plane-drill.sh` | "
    "Control Plane |",
    "| `scripts/apply-db.sh` | Assurance |",
    "| `.github/workflows/ci.yml`, `.github/workflows/mcp-soak.yml` | Assurance |",
    "| `skills/query-man-text-to-sql/` | Delivery |",
    "| `pyproject.toml` package/dependency/entrypoint sections | Runtime |",
    "| `pyproject.toml` Ruff/mypy/pytest sections | Assurance |",
    "| `uv.lock` | Runtime |",
    "| `.python-version`, `.dockerignore` | Runtime |",
    "| `.gitleaksignore`, `.trivyignore.yaml` | Assurance |",
    "| `.github/dependabot.yml` | Runtime |",
)
CRITICAL_SHARED_WRITER_REFERENCES = (
    "tests/helpers.py",
    "tests/conftest.py",
    "tests/control_database.py",
    "tests/test_documentation.py",
    "tests/test_http.py",
    "tests/test_runtime_config.py",
    "tests/test_metadata_store.py",
    "tests/test_quality_level.py",
    "tests/test_result_encoding.py",
    "docs/development-todo.md",
    "docs/implementation-roadmap.md",
    "docs/module-contract-decision-guide.md",
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
    "SQL-09": (
        "`DATE BETWEEN`, cast와 분석 함수를 포함한 허용·거부 corpus, 우회 문법, nested query, "
        "CTE와 Unicode identifier 회귀 테스트를 추가한다."
    ),
    "EXEC-10": (
        "수정 가능한 고정 SQLSTATE만 bounded `QUERY_INVALID`로 분리하고 나머지 DB 오류, "
        "timeout, cancel과 serialization failure를 비공개 또는 전용 오류 계약으로 매핑한다."
    ),
    "MCP-02": (
        "고정 schema의 `list_sources`, `get_context`, `query` tool, bounded argument description과 "
        "SQL capability를 제공한다."
    ),
    "MCP-03": (
        "MCP 요청에도 동일한 caller authorization, budget와 오류 reason code를 적용한다."
    ),
    "MCP-06": (
        "Grain, fanout, composition, business predicate와 SQL capability를 준수하고 bounded "
        "invalid-query correction을 한 번만 수행하는 공통 Text-to-SQL Skill을 작성한다."
    ),
    "MCP-08": (
        "Tool schema 호환성, 응답 크기와 `/mcp` request arrival부터 final ASGI body까지의 "
        "bounded lifecycle timing 회귀 테스트를 추가한다."
    ),
}


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


def test_completed_baseline_descriptions_are_not_retroactively_expanded() -> None:
    text = ROADMAP.read_text(encoding="utf-8")

    for item_id, expected in LOCKED_BASELINE_DESCRIPTIONS.items():
        match = re.search(
            rf"^- \[x\] `{re.escape(item_id)}` (.*(?:\n  .*)*)$",
            text,
            re.MULTILINE,
        )
        assert match is not None, item_id
        assert " ".join(match.group(1).split()) == expected


def test_production_status_and_completion_audits_cover_every_roadmap_group() -> None:
    roadmap = ROADMAP.read_text(encoding="utf-8")
    architecture = ARCHITECTURE.read_text(encoding="utf-8")
    baseline_audit = BASELINE_AUDIT.read_text(encoding="utf-8")
    refactoring_audit = REFACTORING_AUDIT.read_text(encoding="utf-8")
    container_audit = CONTAINER_AUDIT.read_text(encoding="utf-8")
    mcp_server_audit = MCP_SERVER_AUDIT.read_text(encoding="utf-8")
    development_todo = DEVELOPMENT_TODO.read_text(encoding="utf-8")
    source_management_plan = SOURCE_MANAGEMENT_PLAN.read_text(encoding="utf-8")
    central_source_adr = CENTRAL_SOURCE_ADR.read_text(encoding="utf-8")
    shared_access_adr = SHARED_ACCESS_ADR.read_text(encoding="utf-8")
    mcp_soak_audit = MCP_SOAK_AUDIT.read_text(encoding="utf-8")
    control_migration_audit = CONTROL_MIGRATION_AUDIT.read_text(encoding="utf-8")
    source_management_catalog_audit = SOURCE_MANAGEMENT_CATALOG_AUDIT.read_text(
        encoding="utf-8"
    )
    source_mutation_receipt_audit = SOURCE_MUTATION_RECEIPT_AUDIT.read_text(
        encoding="utf-8"
    )

    assert "Status: Production ready" in roadmap
    assert "Status: Production ready" in architecture
    assert "Status: Complete" in baseline_audit
    assert "Status: Complete" in refactoring_audit
    assert "Status: Complete" in container_audit
    assert "Status: Complete" in mcp_server_audit
    assert "Status: Active" in development_todo
    assert "Status: Active implementation" in source_management_plan
    assert "Status: Accepted" in central_source_adr
    assert "Status: Accepted" in shared_access_adr
    assert "Status: Complete" in mcp_soak_audit
    assert "Status: Complete" in control_migration_audit
    assert "Status: Complete" in source_management_catalog_audit
    assert "Status: Complete" in source_mutation_receipt_audit
    assert REFACTORING_AUDIT.name in roadmap
    assert REFACTORING_AUDIT.name in architecture
    assert CONTAINER_AUDIT.name in roadmap
    assert CONTAINER_AUDIT.name in architecture
    assert MCP_SERVER_AUDIT.name in roadmap
    assert MCP_SERVER_AUDIT.name in architecture
    assert DEVELOPMENT_TODO.name in roadmap
    assert DEVELOPMENT_TODO.name in architecture
    assert SOURCE_MANAGEMENT_PLAN.name in architecture
    assert SOURCE_MANAGEMENT_PLAN.name in development_todo
    assert CENTRAL_SOURCE_ADR.name in architecture
    assert CENTRAL_SOURCE_ADR.name in development_todo
    assert SHARED_ACCESS_ADR.name in architecture
    assert SHARED_ACCESS_ADR.name in development_todo
    assert MCP_SOAK_AUDIT.name in roadmap
    assert MCP_SOAK_AUDIT.name in architecture
    assert CONTROL_MIGRATION_AUDIT.name in roadmap
    assert CONTROL_MIGRATION_AUDIT.name in source_management_plan
    assert MANAGED_SOURCE_STARTUP_AUDIT.name in roadmap
    assert MANAGED_SOURCE_STARTUP_AUDIT.name in source_management_plan
    assert SHARED_ACCESS_AUDIT.name in roadmap
    assert SHARED_ACCESS_AUDIT.name in source_management_plan
    assert SHARED_ACCESS_AUDIT.name in architecture
    assert SOURCE_MANAGEMENT_CATALOG_AUDIT.name in roadmap
    assert SOURCE_MANAGEMENT_CATALOG_AUDIT.name in source_management_plan
    assert SOURCE_MUTATION_RECEIPT_AUDIT.name in roadmap
    assert SOURCE_MUTATION_RECEIPT_AUDIT.name in source_management_plan
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


def test_active_todo_contains_only_open_work_and_roadmap_preserves_completed_work() -> None:
    todo = DEVELOPMENT_TODO.read_text(encoding="utf-8")
    roadmap = ROADMAP.read_text(encoding="utf-8")
    soak_audit = MCP_SOAK_AUDIT.read_text(encoding="utf-8")
    control_migration_audit = CONTROL_MIGRATION_AUDIT.read_text(encoding="utf-8")
    managed_source_startup_audit = MANAGED_SOURCE_STARTUP_AUDIT.read_text(encoding="utf-8")
    shared_access_audit = SHARED_ACCESS_AUDIT.read_text(encoding="utf-8")
    source_management_catalog_audit = SOURCE_MANAGEMENT_CATALOG_AUDIT.read_text(
        encoding="utf-8"
    )
    source_mutation_receipt_audit = SOURCE_MUTATION_RECEIPT_AUDIT.read_text(
        encoding="utf-8"
    )
    matches = re.findall(r"^- \[([ x])\] `([A-Z]+)-(\d{2})`", todo, re.MULTILINE)
    ids = [f"{prefix}-{number}" for _checked, prefix, number in matches]

    assert tuple(ids) == EXPECTED_OPEN_TODO_IDS
    assert len(ids) == len(set(ids))
    assert all(checked == " " for checked, _prefix, _number in matches)
    assert "- [x]" not in todo
    for field in (
        "Primary module",
        "Direct consumers",
        "Affected providers/verifiers",
        "Contract baseline",
        "Approval gate",
        "Single writer",
        "Start gate",
        "Verification",
    ):
        assert todo.count(f"| {field} |") == 5

    assert "Lower-track의 `read-only prework`" in todo
    assert "**plan 승인은 contract 선택" in todo
    assert "승인이 아니다**" in todo
    assert "다음 구현 작업은 `MOD-08`" in todo
    assert "2026-08-24 사용자가 `D5-A`" in todo
    assert "deep immutability `MOD-07`은 완료" in todo
    assert "Ledger의 `RTSAFE-01` 완료 및 `MOD-04`~`MOD-08`과 `CTRL-*` 완료" in todo
    assert "`RTSAFE-*`, `MOD-*`" not in todo

    for item_id in EXPECTED_POST_BASELINE_COMPLETED_IDS:
        assert f"`{item_id}`" in roadmap
        assert not re.search(rf"^- \[[ x]\] `{re.escape(item_id)}`", todo, re.MULTILINE)

    for number in range(1, 8):
        assert f"`SOAK-{number:02}`" in soak_audit
    assert "`CTRL-01`" in control_migration_audit
    assert "`CTRL-02`" in managed_source_startup_audit
    assert "`CTRL-03`" in shared_access_audit
    assert "`CTRL-04`" in source_management_catalog_audit
    assert "`CTRL-05`" in source_mutation_receipt_audit


def test_mutation_receipt_docs_preserve_terminal_and_secret_boundaries() -> None:
    plan = SOURCE_MANAGEMENT_PLAN.read_text(encoding="utf-8")
    operations = (ROOT_DIRECTORY / "docs" / "operations.md").read_text(
        encoding="utf-8"
    )
    audit = SOURCE_MUTATION_RECEIPT_AUDIT.read_text(encoding="utf-8")

    for header in (
        "Idempotency-Key",
        "X-Query-Man-Reason",
        "X-Expected-Generation",
        "X-Expected-State-Version",
        "X-Expected-Metadata-Revision",
    ):
        assert header in plan
    assert "terminal-only" in plan
    assert "404를 실패" in operations
    assert "같은 transaction" in audit
    assert "question/SQL" in audit


def test_initial_access_and_resource_tier_decision_stays_minimal() -> None:
    decision = SHARED_ACCESS_ADR.read_text(encoding="utf-8")
    management_plan = SOURCE_MANAGEMENT_PLAN.read_text(encoding="utf-8")

    assert "`budget_profile`을 유일한 resource tier" in decision
    assert "별도 `cost_tier`" in decision
    assert "인증된 query principal은 모두 같은 active source 목록" in decision
    assert "User/organization별" in management_plan
    assert "caller-grant table" in management_plan
    assert "source-changes/{change_id}/approval" not in management_plan


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


def test_delivery_admin_routes_only_import_public_control_contract() -> None:
    route_path = ROOT_DIRECTORY / "src" / "query_man" / "source_admin_routes.py"
    tree = ast.parse(route_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    public_control_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_modules.add(module)
            imported_modules.update(
                f"{module}.{alias.name}" if module else alias.name
                for alias in node.names
            )
            if module in {"query_man.source_admin", "source_admin"}:
                public_control_names.update(alias.name for alias in node.names)

    forbidden_modules = (
        "query_man.source_store",
        "query_man.verified",
        "source_store",
        "verified",
    )
    assert not any(
        imported == forbidden or imported.startswith(f"{forbidden}.")
        for imported in imported_modules
        for forbidden in forbidden_modules
    )
    assert {
        "CONTROL_SEQUENCE_MAX",
        "PublishVerifiedQueryInput",
        "VerifiedExpectedInput",
    } <= public_control_names


def test_module_boundary_docs_cover_owners_contracts_and_current_python_files() -> None:
    index = MODULE_INDEX.read_text(encoding="utf-8")
    agents = (ROOT_DIRECTORY / "AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT_DIRECTORY / "README.md").read_text(encoding="utf-8")
    architecture = ARCHITECTURE.read_text(encoding="utf-8")

    assert "## 계약 변경 승인 절차" in index
    assert "## 새 데이터베이스 추가 시 영향" in index
    assert "docs/modules/README.md" in agents
    assert "Module contract는 사용자의 명시적 승인 없이 변경하지 않는다." in agents
    assert "수정 가능한 file allowlist" in agents
    assert "여러 agent가 같은 worktree를 공유하면" in agents
    assert "docs/modules/README.md" in readme
    assert "modules/README.md" in architecture

    for module_name in MODULE_NAMES:
        path = MODULE_INDEX.parent / module_name / "README.md"
        content = path.read_text(encoding="utf-8")
        assert f"({module_name}/README.md)" in index
        assert "Status: Logical boundary; physical package split pending" in content
        for heading in REQUIRED_MODULE_HEADINGS:
            assert heading in content, f"{path.relative_to(ROOT_DIRECTORY)}: {heading}"

    source_root = ROOT_DIRECTORY / "src" / "query_man"
    for path in source_root.rglob("*.py"):
        relative_path = path.relative_to(source_root)
        mapped_path = path.name if relative_path.parent == Path(".") else relative_path.as_posix()
        assert f"`{mapped_path}`" in index, f"Unmapped module owner: {relative_path}"

    for mapping in CRITICAL_NON_PYTHON_MODULE_MAPPINGS:
        assert mapping in index, f"Missing or changed module mapping: {mapping}"

    for reference in CRITICAL_SHARED_WRITER_REFERENCES:
        assert f"`{reference}`" in index, f"Missing shared-writer mapping: {reference}"

    errors = (ROOT_DIRECTORY / "src" / "query_man" / "errors.py").read_text(
        encoding="utf-8"
    )
    for class_name in re.findall(r"^class (\w+Error)\(", errors, re.MULTILINE):
        assert f"`{class_name}`" in index, f"Missing error owner: {class_name}"

    assert "test function이 다르다는 이유로 병렬 편집하지 않고" in index
    assert "병렬 agent가 직접 priority를 재배열하지 않는다" in index


def test_module_contract_decision_guide_records_approval_and_implementation_status() -> None:
    guide = MODULE_CONTRACT_DECISION_GUIDE.read_text(encoding="utf-8")
    metadata_module = (MODULE_INDEX.parent / "metadata" / "README.md").read_text(
        encoding="utf-8"
    )
    readme = (ROOT_DIRECTORY / "README.md").read_text(encoding="utf-8")
    index = MODULE_INDEX.read_text(encoding="utf-8")
    todo = DEVELOPMENT_TODO.read_text(encoding="utf-8")
    roadmap = ROADMAP.read_text(encoding="utf-8")

    assert "Status: Accepted choices — implementation in progress" in guide
    assert "Approved: 2026-08-24" in guide
    assert "## 용어사전" in guide
    assert "## Wave 0: 승인 전 read-only prework (완료)" in guide
    assert "## 승인 회신 방법" in guide
    for decision in range(6):
        assert f"## D{decision}." in guide
        for option in "ABC":
            assert f"D{decision}-{option}" in guide
    assert "D0-A, D1-A, D2-A, D3-A, D4-A, D5-A" in guide
    for decision, item_id in (
        ("D0", "RTSAFE-01"),
        ("D1", "MOD-04"),
        ("D2", "MOD-05"),
        ("D4", "MOD-06"),
        ("D3", "MOD-07"),
        ("D5", "MOD-08"),
    ):
        assert f"| {decision} " in guide
        assert f"`{item_id}`" in guide
    assert "Wave 0는 아래 행위를 허용하지 않는다" in guide
    assert "이 plan, Wave 0 또는 Active TODO 순서만 승인하는 것은" in guide
    assert "`D1`/`D5`의 B/C와 `D2`/`D3`/`D4`의 B" in guide
    assert "P0.5 ID가 자동으로 완료되거나 P1 gate가 자동으로 열리지 않는다" in guide
    assert "D0 startup failure cleanup (`RTSAFE-01`) — 완료" in guide
    assert "Startup enter-failure cleanup 보장" in guide
    assert "`RTSAFE-01`" in roadmap
    assert "D1 숨은 dependency 제거 (`MOD-04`) — 완료" in guide
    assert "D2 read/write capability 분리 (`MOD-05`) — 완료" in guide
    assert "D4 lifecycle Protocol 명시 (`MOD-06`) — 완료" in guide
    assert "D3 immutable snapshot 전환 (`MOD-07`) — 완료" in guide
    assert "D5 offline CLI composition 격리 (`MOD-08`) — 다음" in guide
    assert "남은 `MOD-08`" in guide
    assert (
        guide.count(
            "Source/Metadata ──> 외부 JSON은 유지하면서 published graph를 deep immutable로 제공"
        )
        == 2
    )
    assert "tests/test_revision.py tests/test_metadata_store.py" in metadata_module
    assert "unmarked snapshot codec·legacy compatibility test" in metadata_module
    assert "Delivery의 public Control administration input 경계" in guide
    assert "`MOD-04`" in roadmap
    assert "`MOD-05`" in roadmap
    assert "`MOD-06`" in roadmap
    assert "`MOD-07`" in roadmap
    assert "`D5-A`" in todo
    assert "`D3-A`, `D5-A`" not in todo
    for document in (readme, index, todo, roadmap):
        assert MODULE_CONTRACT_DECISION_GUIDE.name in document


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
