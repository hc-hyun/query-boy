from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote

import query_man.sql_validation as sql_validation_module
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
RUNTIME_REPLICA_OBSERVATION_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-25-runtime-replica-observations.md"
)
RESOURCE_AND_GATEWAY_OBSERVATION_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-25-resource-and-gateway-observations.md"
)
USAGE_PROJECTION_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-25-usage-projection.md"
)
CONTROL_RECOVERY_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-25-control-recovery-acceptance.md"
)
SOURCE_ONBOARDING_SKILL_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-25-source-onboarding-skill.md"
)
SOURCE_DATABASE_CORNERS_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-25-source-database-corners.md"
)
RLS_POLICY_DRIFT_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-26-rls-policy-drift.md"
)
RLS_POLICY_ATTESTATION_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0024-rls-policy-drift-attestation.md"
)
CANONICAL_TIME_AUDIT = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-25-canonical-time-stability.md"
)
CANONICAL_TIME_ADR = (
    ROOT_DIRECTORY / "docs" / "decisions" / "0019-canonical-time-stability.md"
)
LOSSLESS_SCALAR_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0020-lossless-interval-and-json-numeric-encoding.md"
)
DATABASE_NATIVE_COST_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0021-database-native-cost-attribution.md"
)
QUERY_COST_CONTROL = ROOT_DIRECTORY / "docs" / "query-cost-control.md"
WORKFLOW_TRACE_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0022-w3c-workflow-trace-context.md"
)
DATABASE_NATIVE_ALERT_ADR = (
    ROOT_DIRECTORY
    / "docs"
    / "decisions"
    / "0023-database-native-usage-spike-alert.md"
)
LOWER_TRACK_CONTRACT_PREWORK = (
    ROOT_DIRECTORY
    / "docs"
    / "verification"
    / "2026-08-26-lower-track-contract-prework.md"
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
    "CTRL-06",
    "CTRL-07",
    "CTRL-08",
    "CTRL-09",
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
    "MOD-08",
    "SKILL-01",
    "SKILL-02",
    "SKILL-03",
    "SKILL-04",
    "SKILL-05",
    "SKILL-06",
    "DBEDGE-01",
    "DBEDGE-02",
    "DBEDGE-03",
    "DBEDGE-04",
    "DBEDGE-05",
    "TIME-01",
    "TIME-02",
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
    "| `skills/query-man-source-onboarding/` | Source Catalog |",
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
    "tests/test_assurance_cli.py",
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
    readme = (ROOT_DIRECTORY / "README.md").read_text(encoding="utf-8")
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
    runtime_replica_observation_audit = RUNTIME_REPLICA_OBSERVATION_AUDIT.read_text(
        encoding="utf-8"
    )
    resource_and_gateway_observation_audit = (
        RESOURCE_AND_GATEWAY_OBSERVATION_AUDIT.read_text(encoding="utf-8")
    )
    usage_projection_audit = USAGE_PROJECTION_AUDIT.read_text(encoding="utf-8")
    control_recovery_audit = CONTROL_RECOVERY_AUDIT.read_text(encoding="utf-8")

    current_status = (
        "Status: Baseline complete; RLS-enabled production serving blocked "
        "pending `RLS-*`"
    )
    assert current_status in roadmap
    assert current_status in architecture
    assert "Status: Complete" in baseline_audit
    assert "Status: Complete" in refactoring_audit
    assert "Status: Complete" in container_audit
    assert "Status: Complete" in mcp_server_audit
    assert "Status: Active" in development_todo
    assert "Status: Baseline complete; deferred extensions" in source_management_plan
    assert "Status: Accepted" in central_source_adr
    assert "Status: Accepted" in shared_access_adr
    assert "Status: Complete" in mcp_soak_audit
    assert "Status: Complete" in control_migration_audit
    assert "Status: Complete" in source_management_catalog_audit
    assert "Status: Complete" in source_mutation_receipt_audit
    assert "Status: Complete" in runtime_replica_observation_audit
    assert "Status: Complete" in resource_and_gateway_observation_audit
    assert "Status: Complete" in usage_projection_audit
    assert "Status: Complete" in control_recovery_audit
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
    assert RUNTIME_REPLICA_OBSERVATION_AUDIT.name in roadmap
    assert RUNTIME_REPLICA_OBSERVATION_AUDIT.name in architecture
    assert RUNTIME_REPLICA_OBSERVATION_AUDIT.name in source_management_plan
    assert RESOURCE_AND_GATEWAY_OBSERVATION_AUDIT.name in roadmap
    assert RESOURCE_AND_GATEWAY_OBSERVATION_AUDIT.name in architecture
    assert RESOURCE_AND_GATEWAY_OBSERVATION_AUDIT.name in source_management_plan
    assert USAGE_PROJECTION_AUDIT.name in roadmap
    assert USAGE_PROJECTION_AUDIT.name in architecture
    assert USAGE_PROJECTION_AUDIT.name in source_management_plan
    assert CONTROL_RECOVERY_AUDIT.name in roadmap
    assert CONTROL_RECOVERY_AUDIT.name in architecture
    assert CONTROL_RECOVERY_AUDIT.name in source_management_plan
    assert CONTROL_RECOVERY_AUDIT.name in readme
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
    runtime_replica_observation_audit = RUNTIME_REPLICA_OBSERVATION_AUDIT.read_text(
        encoding="utf-8"
    )
    resource_and_gateway_observation_audit = (
        RESOURCE_AND_GATEWAY_OBSERVATION_AUDIT.read_text(encoding="utf-8")
    )
    usage_projection_audit = USAGE_PROJECTION_AUDIT.read_text(encoding="utf-8")
    control_recovery_audit = CONTROL_RECOVERY_AUDIT.read_text(encoding="utf-8")
    source_onboarding_skill_audit = SOURCE_ONBOARDING_SKILL_AUDIT.read_text(
        encoding="utf-8"
    )
    cost_adr = DATABASE_NATIVE_COST_ADR.read_text(encoding="utf-8")
    alert_adr = DATABASE_NATIVE_ALERT_ADR.read_text(encoding="utf-8")
    lower_track_prework = LOWER_TRACK_CONTRACT_PREWORK.read_text(encoding="utf-8")
    query_cost = QUERY_COST_CONTROL.read_text(encoding="utf-8")
    trace_adr = WORKFLOW_TRACE_ADR.read_text(encoding="utf-8")
    assurance_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "assurance" / "README.md"
    ).read_text(encoding="utf-8")
    runtime_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "runtime" / "README.md"
    ).read_text(encoding="utf-8")
    delivery_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "delivery" / "README.md"
    ).read_text(encoding="utf-8")
    source_catalog_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "source-catalog" / "README.md"
    ).read_text(encoding="utf-8")
    metadata_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "metadata" / "README.md"
    ).read_text(encoding="utf-8")
    guarded_query_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "guarded-query" / "README.md"
    ).read_text(encoding="utf-8")
    control_plane_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "control-plane" / "README.md"
    ).read_text(encoding="utf-8")
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
    assert "## P0.5 — Module Contract Hardening" not in todo
    assert "offline composition `MOD-08`은 모두" in todo
    assert "## P2 — Source Onboarding Skill" not in todo
    assert "A는 fingerprint byte schema/bounds" in todo
    assert "B는 별도 exact restatement가 필요" in todo
    assert "C는 production completion을 block하는 defer" in todo
    assert "일반적인 진행/승인이나 ID만으로 구현하지 않는다" in todo
    assert "Source Catalog shared `reader_policy.py` → Metadata fingerprint" in todo
    assert "Exact A 제안에서 Source Catalog의 추가 역할" in source_catalog_contract
    assert "Metadata가 bounded source-semantics catalog probe" in metadata_contract
    assert "현재 executor 계약이 아니다" in guarded_query_contract
    assert "v1 row를 update/delete" in control_plane_contract
    assert "V1/V2 serving" in runtime_contract
    assert "recursive view dependency fingerprint" in assurance_contract
    assert "승인 전 loader/setting/encoder/source-semantics snapshot/revision/hash" in todo
    assert "Production inventory·권한·backup·route가 제공되어야" in todo
    assert "명시적으로 defer하기 전에는 `COST-01` 구현을 시작하지" in todo
    assert "proposed ADR 0021의 정확한 monitoring 계약과 영향 범위를 별도 승인" in todo
    assert "| `TIME-03` |" not in roadmap
    assert "재현된 authorization gap인 M13.5 `RLS-*`가 최우선 active" in roadmap
    assert "M14.5의 `ENC-*` 결정·구현과 M14" in roadmap
    assert "production 전환 `TIME-03`도 active" in roadmap
    assert "`CTRL-07A` observation method/freshness/logical retention" in todo
    assert "`CTRL-08` usage/cost state" in todo
    assert "현재 선택지 초안은 lower-track read-only prework" in todo
    assert "`COST-01-A|B|C`" in todo
    assert "`COST-04`는 별도 [proposed ADR 0023]" in todo
    assert "alert 90일은 아직 승인된 현재 계약이 아니다" in todo
    assert "`TRACE-01-A|B|C`" in todo
    assert (
        "Status: Proposed read-only prework — priority gate and user approval required"
        in cost_adr
    )
    assert "`COST-01-A` — dedicated sanitized monitor (recommended)" in cost_adr
    assert "`COST-04` Boundary — separate ADR 0023 approval required" in cost_adr
    assert "아래 A 문구는 proposed ADR 0023의 `COST-04`" in cost_adr
    assert "Status: Proposed read-only prework" in alert_adr
    assert "`COST-04-A` — durable polling-only execution-time spike alert" in alert_adr
    assert "accepted_samples >= 10" in alert_adr
    assert "sample-count-qualified" in alert_adr
    assert "whole-hour/continuous coverage" in alert_adr
    assert "CREATE TABLE control.source_db_alert_policy_revisions" in alert_adr
    assert "CREATE TABLE control.source_db_alert_policy_state" in alert_adr
    assert "CREATE TABLE control.source_db_alert_states" in alert_adr
    assert "CREATE TABLE control.source_db_alert_events" in alert_adr
    assert "migration **7**" in alert_adr
    assert "base 18+alert 4=22 tables" in alert_adr
    assert "source_db_alert_events_one_terminal_idx" in alert_adr
    assert "alert_policy_state_version bigint NOT NULL" in alert_adr
    assert "observation_started_bucket timestamptz NOT NULL" in alert_adr
    assert "firing_event_type text NOT NULL" in alert_adr
    assert "compatibility release" in alert_adr
    assert "모든 fenced committed monitoring attempt" in alert_adr
    assert "| not_configured/version 0 | configure |" in alert_adr
    assert "event_retention_days=90" in alert_adr
    assert "field도 생략하지 않고 JSON `null`" in alert_adr
    assert "reason_code IS DISTINCT FROM 'COOLDOWN_ACTIVE'" in alert_adr
    assert "evaluated_at < cooldown_until" in alert_adr
    assert "Webhook/email/push" in alert_adr
    assert "COST-04-A를" in alert_adr
    assert "disposable contract prework" in roadmap
    assert "Repository production code" in lower_track_prework
    assert "22 tables" in lower_track_prework
    assert "exact_target_only=true" in lower_track_prework
    assert "query-man-acl-probe-final-0021" in lower_track_prework
    assert "잔여 count는 모두 0" in lower_track_prework
    assert "exact contract 승인이나 production implementation evidence가 아니다" in (
        lower_track_prework
    )
    assert "pg_monitor" in cost_adr
    assert "query text" in cost_adr
    assert "effective capability matrix" in cost_adr
    assert "Query Man application은 shared PUBLIC ACL을 자동 revoke하지 않고" in cost_adr
    assert "raw non-reset statement/info function" in cost_adr
    assert "roleid=owner_oid" in cost_adr
    assert "roleid=monitor_oid" in cost_adr
    assert "target database의 PUBLIC `TEMPORARY`" in cost_adr
    assert "모든 `pg_database.datallowconn=true` database의 PUBLIC `CONNECT`" in cost_adr
    assert "true\n      count는 0" in cost_adr
    assert "CREATE DATABASE ... ALLOW_CONNECTIONS false" in cost_adr
    assert "ALLOW_CONNECTIONS false→ACL hardening→true" in cost_adr
    assert "Target CONNECT가 direct ACL item이면 grant option은 false" in cost_adr
    assert "identity/baseline/attempt/success만 전부 null" in cost_adr
    assert "shared_blks_hit, shared_blks_read, shared_blks_dirtied" in cost_adr
    assert "other-database PUBLIC CONNECT 및 PUBLIC/object ACL hardening" in cost_adr
    assert "row별 `stats_since`" in cost_adr
    assert "target reader role의 `pg_stat_statements` aggregate" in cost_adr
    assert "Query Man business query별 측정이나" in cost_adr
    assert "`sha256:<64 lower hex>`" in cost_adr
    definition_match = re.search(r"```json\n(.*?)\n   ```", cost_adr, re.DOTALL)
    assert definition_match is not None
    definition_text = "\n".join(
        line[3:] if line.startswith("   ") else line
        for line in definition_match.group(1).splitlines()
    )
    definition_material = json.loads(definition_text)
    definition_bytes = json.dumps(
        definition_material,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert definition_bytes.isascii()
    definition_revision = "sha256:" + hashlib.sha256(definition_bytes).hexdigest()
    assert definition_revision == (
        "sha256:f621a1815ad806d23d23f00fe0f0faa030c4083fbf49d184a4b8dd3d7e379160"
    )
    assert f"`{definition_revision}`" in cost_adr
    assert definition_material["attribution"] == {
        "bucket_start": "date_trunc('hour',accepted_at,'UTC')",
        "clock": "control_db_clock_timestamp",
        "delta": "whole",
        "prorate": False,
    }
    assert definition_material["delta"]["stale_control_fence"] == "no_write"
    assert definition_material["delta"]["counter_regression"] == (
        "discard_current_complete_rebaseline"
    )
    assert definition_material["delta"]["counter_overflow"] == (
        "discard_invalidate_wait_complete"
    )
    assert definition_material["delta"]["mid_scan_target_or_settings_change"] == (
        "fail_invalidate_wait_complete"
    )
    assert definition_material["delta"]["new_target_or_allowed_settings"] == (
        "baseline_new_observation_identity"
    )
    assert definition_material["binding"] == {
        "database_binding": "one_source_per_system_identifier_and_database_oid",
        "database_oid": "current_database_catalog_oid",
        "info_must_match_bound_source": True,
        "reader_oid": "exact_pg_roles_rolname(active_source_reader_username_parameter)",
        "statement_rows_must_match_info": True,
    }
    assert definition_material["failure_precedence"] == [
        ["bound_database_or_reader_or_mid_scan_target", "failed_TARGET_MISMATCH"],
        [
            "monitor_or_function_owner_privilege_or_projection_security_drift",
            "failed_MONITOR_PRIVILEGE_MISMATCH",
        ],
        [
            "extension_preload_version_or_projection_missing",
            "failed_EXTENSION_UNAVAILABLE",
        ],
        [
            "unsupported_or_mid_scan_unstable_setting",
            "failed_SETTINGS_MISMATCH",
        ],
        ["statement_rows_over_5000", "discarded_ROW_LIMIT_EXCEEDED"],
        [
            "mid_scan_reset_or_dealloc_duplicate_identity_shape_type_null_info_"
            "cardinality_decode_nonfinite_nonintegral_statement_binding_or_incomplete",
            "discarded_OBSERVATION_INCOMPLETE",
        ],
        [
            "finite_integral_counter_negative_or_over_max",
            "discarded_COUNTER_OVERFLOW",
        ],
        [
            "permission_transport_timeout_or_other_sql_execution",
            "failed_MONITOR_UNAVAILABLE",
        ],
    ]
    assert definition_material["projection"] == {
        "acl_profile": (
            "pgss_v1_21_2_owner_raw_nonreset_readstats_inbound0_monitor_wrappers_"
            "nomembership_targetdb_only_notemp_public_hardened"
        ),
        "body_template_encoding": "utf8_exact_lf_terminal_newline",
        "extension_namespace": "public",
        "function_language": "sql",
        "function_security": (
            "security_definer_stable_parallel_restricted_locked_search_path"
        ),
        "info_body_template_revision": (
            "sha256:093d8c24cf77b9a93e2e790fa01f2d190965be3a3864beb3dbdbbf5b7a19fa20"
        ),
        "info_function": "query_man_monitor.monitor_info_v1()",
        "overloads": False,
        "reader_literal_placeholder": "{{reader_sql_literal}}",
        "schema": "query_man_monitor",
        "statement_body_template_revision": (
            "sha256:728bbb12bec272b7a6bc31320fcd11ce55513281a06e076458ce1928097707f4"
        ),
        "statement_function": "query_man_monitor.monitor_statements_v1()",
    }
    source_sql_templates = re.findall(
        r"^   ```sql\n(.*?)\n   ```$", cost_adr, re.DOTALL | re.MULTILINE
    )
    assert len(source_sql_templates) >= 2
    template_revisions = []
    for template in source_sql_templates[:2]:
        material = (
            "\n".join(
                line[3:] if line.startswith("   ") else line
                for line in template.splitlines()
            )
            + "\n"
        ).encode("utf-8")
        template_revisions.append("sha256:" + hashlib.sha256(material).hexdigest())
    assert template_revisions == [
        definition_material["projection"]["info_body_template_revision"],
        definition_material["projection"]["statement_body_template_revision"],
    ]
    assert definition_material["session"] == {
        "date_style": "ISO, YMD",
        "time_zone": "UTC",
    }
    assert definition_material["info"]["fields"][0] == [
        "system_identifier",
        "text",
        "unsigned_decimal_ascii",
        False,
    ]
    assert definition_material["statement"]["fields"][-1] == [
        "wal_bytes",
        "numeric",
        "integral_decimal_string_max_38_digits",
        False,
    ]
    assert definition_material["statement"]["fields"][6] == [
        "execution_time_us",
        "numeric",
        "checked_int64_integer",
        False,
    ]
    assert definition_material["observation_identity"]["target_field"] == (
        "target_instance_id"
    )
    assert definition_material["observation_identity"]["settings_fields"] == [
        "server_version_num",
        "extension_version",
        "compute_query_id",
        "track",
        "track_planning",
        "track_utility",
        "save",
        "max",
    ]
    assert all(
        len(field) == 4 and field[3] is False
        for section in ("info", "statement")
        for field in definition_material[section]["fields"]
    )
    assert definition_material["timeouts"] == {
        "connect_seconds": 5,
        "idle_in_transaction_ms": 5000,
        "lock_ms": 250,
        "statement_ms": 20000,
        "transaction_ms": 75000,
    }
    assert definition_material["target_instance"]["template"].count("\n") == 6
    assert definition_material["target_instance"]["database_binding_template"] == (
        "pg18-db\n{system_identifier}\n{dbid}"
    )
    assert definition_material["bounds"]["collector_concurrency_per_replica"] == 4
    assert definition_material["bounds"]["accepted_samples_per_hour"] == 12
    assert definition_material["delta"]["explicit_zero"] == "persist_rollup_row"
    assert "`bucket_start=date_trunc('hour', accepted_at, 'UTC')`" in cost_adr
    assert "`lease_until=now+120 seconds`" in cost_adr
    assert "Source I/O 중 Control connection이나" in cost_adr
    assert "source_db_monitoring_revisions" in cost_adr
    assert "query-man/source/{source_id}/monitoring-revision/{monitoring_revision}" in cost_adr
    assert "GET    /admin/sources/{source_id}/database-native-monitoring" in cost_adr
    assert "`X-Expected-Monitoring-State-Version`" in cost_adr
    assert "monitor_rolled_back" in cost_adr
    assert "active pointer를 삭제하지 않고 `enabled=false`" in cost_adr
    assert "same-key/different-secret을 구분하는 in-memory" in cost_adr
    assert "Source I/O 동안 Control connection/lock은 0개" in cost_adr
    assert "canonical `failure_precedence`의 first matching outcome/reason" in cost_adr
    assert "role_row.rolname = $1::pg_catalog.name" in cost_adr
    assert "모든 statement row의 `dbid/userid`는 두 info와 같아야" in cost_adr
    assert "existing `SOURCE_VALIDATION_FAILED` 400" in cost_adr
    assert "| disabled | rotate | next revision" in cost_adr
    assert "| disabled | rollback to any valid revision including current pointer |" in cost_adr
    assert "독립 `source_db_monitoring_history` table은 만들지 않는다" in cost_adr
    assert "Availability에 사용할 current observation identity" in cost_adr
    assert "accepted success가 있고 `read_at <= fresh_until`이면" in cost_adr
    assert "Latest complete `baseline_at`이" in cost_adr
    assert "baseline/success/committed attempt가 모두 없는 initial configured state" in cost_adr
    assert "latest committed attempt가 `failed` 또는 `discarded`면" in cost_adr
    assert "현재 rowset은 baseline 자격이 없다" in cost_adr
    assert "다음 stable complete scan만" in cost_adr
    assert "canonical JSON의 `failure_precedence` array 순서" in cost_adr
    assert "finite `-0.5`는 여기서 끝나며" in cost_adr
    assert "SERVER_DEALLOCATION_DETECTED" in cost_adr
    assert "COUNTER_OVERFLOW" in cost_adr
    assert "read_at <= fresh_until" in cost_adr
    assert "source_db_usage_rollups" in cost_adr
    for table_name in (
        "source_db_monitoring_revisions",
        "source_db_monitoring_state",
        "source_db_usage_state",
        "source_db_statement_baselines",
        "source_db_usage_rollups",
    ):
        assert f"CREATE TABLE control.{table_name}" in cost_adr
    assert "additive migration **6**" in cost_adr
    assert "db_monitoring_state_database_unique UNIQUE (database_binding_id)" in cost_adr
    assert "accepted_samples BETWEEN 1 AND 12" in cost_adr
    assert "lease_epoch bigint NOT NULL DEFAULT 0" in cost_adr
    assert "source_db_usage_state_due_idx" in cost_adr
    assert "source_profile_revision_generation_metadata_unique" not in cost_adr
    assert "db_usage_state_metadata_exists" in cost_adr
    assert ") IS TRUE" in cost_adr
    assert "Compatibility-reader-first" in cost_adr
    assert "18-table/25-FK/5-trigger" in cost_adr
    assert "source_db_monitoring_revisions_are_immutable" in cost_adr
    assert "configure_database_native_monitoring" in cost_adr
    assert "기존 13개에\n    이 5개를 더한 18개 table" in cost_adr
    assert "MONITOR_PRIVILEGE_MISMATCH" in cost_adr
    assert "process당 source scan은 최대 4개" in cost_adr
    assert "같은 database의 binding을 **다른 source가**" in cost_adr
    assert "The requested monitoring revision was not found." in cost_adr
    assert "The source monitoring state changed; retry with current state." in cost_adr
    assert "database_native" in cost_adr
    assert "`database_native` section 자체를 추가하지 않는다" in cost_adr
    assert "이는 contract 선택이며 열린 ENC/TIME보다 구현을 먼저" in cost_adr
    assert "B는 direction-only" in cost_adr
    assert "source_id + budget_profile + metadata_revision + definition_revision" in query_cost
    assert "DBA가 수동 조사에만 쓰는 현재 외부 운영 선택지" in query_cost
    assert "Assurance는 sanitized source projection" in assurance_contract
    assert "현재 Runtime operations에는 workflow trace context/scope/counter가 없다" in runtime_contract
    assert "source DB-native statement collector도 없다" in runtime_contract
    assert "current `/usage` top-level shape" in delivery_contract
    assert "Control Plane의 공개 use case/projection만" in delivery_contract
    assert (
        "Status: Proposed read-only prework — priority gate and user approval required"
        in trace_adr
    )
    assert "`TRACE-01-A` — policy-admitted fail-soft `traceparent` (recommended)" in trace_adr
    assert "B/C를 선택하면 route, audit/counter" in trace_adr
    assert "configured authentication policy가 `CallerContext`" in trace_adr
    assert "`/queries/{query_id}` 한 segment인 `DELETE`" in trace_adr
    assert "decoded exact DELETE /queries/{query_id}에서만 ASGI raw" in trace_adr
    assert "admin source route, MCP GET" in trace_adr
    assert "policy-admitted 404/405" in trace_adr
    assert "accepted | generated_absent | restarted_invalid | restarted_duplicate" in trace_adr
    assert "Mixed-case duplicate도 duplicate" in trace_adr
    assert "visible US-ASCII `0x21..0x7e`" in trace_adr
    assert "ASGI-observable" in trace_adr
    assert "wire OWS" in trace_adr
    assert "`local-development`도 성공한 caller context" in trace_adr
    assert "Lone trailing `-`도 future-version opaque suffix" in trace_adr
    assert "`secrets.token_hex(16)`" in trace_adr
    assert "`current_trace_context() -> TraceContext | None`" in trace_adr
    assert "frozen+slots `TraceContext" in trace_adr
    assert "ASGI `scope[\"state\"]`" in trace_adr
    assert "ContextVar가 아니라 같은 ASGI state" in trace_adr
    assert "private MCP-call scope" in trace_adr
    assert "existing `mcp_call_id`를 query start/success/failure/interruption" in trace_adr
    assert "Runtime provider → Delivery parser/set-reset" in trace_adr
    assert "cancel request trace" in trace_adr
    assert "의미 있는 식별자를 32-hex에 encode해서는" in trace_adr
    assert "안 된다(MUST NOT)" in trace_adr
    assert "replica-local, process-restart-reset aggregate counter" in trace_adr
    assert "CallerContext-bearing request 하나마다 source-less counter" in trace_adr
    assert "잘못된 type/value면 stringify/redact해 내보내지 않고" in trace_adr
    assert "all-zero가 아닌 string" in trace_adr
    assert "P4의 TRACE-01 결정과 승인된" in trace_adr
    assert "tracestate/baggage/response/outbound propagation" in trace_adr
    assert "failed parallel MCP query" in delivery_contract
    assert "current process-local trace만" in guarded_query_contract
    assert "Delivery-private MCP call ID는 Guarded Query로 넘기지 않고" in guarded_query_contract
    assert "target query의 original trace" in guarded_query_contract
    assert "failed parallel MCP call→query mapping" in assurance_contract
    assert "real Uvicorn/h11 wire OWS normalization parity" in assurance_contract
    assert "unknown-source trace absence" in assurance_contract
    assert "two-process/replica soak correlation" in assurance_contract
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
    assert "`CTRL-06`" in runtime_replica_observation_audit
    assert "`CTRL-07`" in resource_and_gateway_observation_audit
    assert "`CTRL-07A`" in resource_and_gateway_observation_audit
    assert "`CTRL-08`" in usage_projection_audit
    assert "`CTRL-09`" in control_recovery_audit
    for number in range(1, 7):
        assert f"`SKILL-{number:02}`" in source_onboarding_skill_audit


def test_source_onboarding_skill_docs_record_plan_only_adoption_and_evidence() -> None:
    readme = (ROOT_DIRECTORY / "README.md").read_text(encoding="utf-8")
    onboarding = (ROOT_DIRECTORY / "docs" / "source-onboarding.md").read_text(
        encoding="utf-8"
    )
    plan = (ROOT_DIRECTORY / "docs" / "source-onboarding-skill-plan.md").read_text(
        encoding="utf-8"
    )
    source_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "source-catalog" / "README.md"
    ).read_text(encoding="utf-8")
    assurance_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "assurance" / "README.md"
    ).read_text(encoding="utf-8")
    audit = SOURCE_ONBOARDING_SKILL_AUDIT.read_text(encoding="utf-8")

    for document in (readme, onboarding, plan, source_contract, audit):
        assert "query-man-source-onboarding" in document
    assert "Status: Complete; adopted plan-only workflow" in plan
    assert SOURCE_ONBOARDING_SKILL_AUDIT.name in onboarding
    assert SOURCE_ONBOARDING_SKILL_AUDIT.name in assurance_contract
    assert "request log는 0건" in audit
    assert "mutation_count: 0" in audit


def test_source_database_corner_docs_record_canonical_time_resolution() -> None:
    audit = SOURCE_DATABASE_CORNERS_AUDIT.read_text(encoding="utf-8")
    rls_audit = RLS_POLICY_DRIFT_AUDIT.read_text(encoding="utf-8")
    rls_adr = RLS_POLICY_ATTESTATION_ADR.read_text(encoding="utf-8")
    onboarding = (ROOT_DIRECTORY / "docs" / "source-onboarding.md").read_text(
        encoding="utf-8"
    )
    extension_checklist = (
        ROOT_DIRECTORY / "docs" / "source-extension-checklist.md"
    ).read_text(encoding="utf-8")
    canonical_audit = CANONICAL_TIME_AUDIT.read_text(encoding="utf-8")
    canonical_adr = CANONICAL_TIME_ADR.read_text(encoding="utf-8")
    lossless_adr = LOSSLESS_SCALAR_ADR.read_text(encoding="utf-8")
    module_index = MODULE_INDEX.read_text(encoding="utf-8")
    assurance = (
        ROOT_DIRECTORY / "docs" / "modules" / "assurance" / "README.md"
    ).read_text(encoding="utf-8")
    source_catalog = (
        ROOT_DIRECTORY / "docs" / "modules" / "source-catalog" / "README.md"
    ).read_text(encoding="utf-8")
    metadata = (
        ROOT_DIRECTORY / "docs" / "modules" / "metadata" / "README.md"
    ).read_text(encoding="utf-8")
    control_plane = (
        ROOT_DIRECTORY / "docs" / "modules" / "control-plane" / "README.md"
    ).read_text(encoding="utf-8")
    development_todo = DEVELOPMENT_TODO.read_text(encoding="utf-8")

    assert "`DBEDGE-01`" in audit
    assert "`DBEDGE-02`" in audit
    assert "`DBEDGE-03`" in audit
    assert "`DBEDGE-04`" in audit
    assert "`DBEDGE-05`" in audit
    assert "test_source_database_corners.py" in module_index
    assert "domain/operator `pg_depend`는 raw driver/catalog probe" in module_index
    assert "raw-only driver/catalog probe" in audit
    assert SOURCE_DATABASE_CORNERS_AUDIT.name in assurance
    assert RLS_POLICY_DRIFT_AUDIT.name in assurance
    assert "Status: Open — contract decision and fail-closed implementation required" in rls_audit
    assert "strict=True" in rls_audit
    assert RLS_POLICY_ATTESTATION_ADR.name in rls_audit
    assert "Status: Proposed — exact user approval required before implementation" in rls_adr
    assert "Decision ID: `RLS-01-A`" in rls_adr
    assert "이 ADR은 exact 제안일 뿐 승인된 계약이나 구현이 아니다" in rls_adr
    assert "ACCESS SHARE MODE NOWAIT" in rls_adr
    assert "attest_rls_roots" in rls_adr
    assert "view_sql_policy_revision" in rls_adr
    assert "snapshot_contract_version\": 2" in rls_adr
    assert "old MVCC snapshot" in rls_adr
    assert "current_user=session_user=configured-reader" in rls_adr
    assert "rulename='_RETURN'" in rls_adr
    assert "dbid=current_database_oid" in rls_adr
    assert "objid IN P" in rls_adr
    assert "LIMIT N+1" in rls_adr
    assert 'RLS_READER_CLIENT_ENCODING: Final = "UTF8"' in rls_adr
    assert "require_rls_reader_connection_policy" in rls_adr
    assert "class RlsAttestationValidationError(ValueError)" in rls_adr
    assert "deterministic zero-root/root-count" in rls_adr
    assert "root-list add/drop/rename" in rls_adr
    assert "common reader-session identity/policy mismatch" in rls_adr
    assert "SQLSTATE `22023`/`42501`" in rls_adr
    assert "no-SQL connection invariant mismatch" in rls_adr
    assert "Candidate/active metadata/query/resource-observation" in rls_adr
    assert "query timeout `QUERY_TIMEOUT`" in rls_adr
    assert "Marker-free transient" in rls_adr
    assert "except block 밖에서" in rls_adr
    assert "raw exception을 public/log chain에 보존" in rls_adr
    assert "Active RLS resource-observation checkout" in rls_adr
    assert "StoredMetadataInvalidError" in rls_adr
    assert "`__cause__ is None`, `__context__ is None`" in rls_adr
    assert "Password canary" in rls_adr
    assert "private history decoder" in rls_adr
    assert "offline\nserving-compatibility check" in rls_adr
    assert "Metadata cache/epoch invalidate(source_id)" in rls_adr
    assert "resource-observation" in rls_adr
    assert "next_source: SourceProfile | None" in rls_adr
    assert "Query invalidator는 Runtime composition에서 반드시 첫 adapter" in rls_adr
    assert "cancel_safe(timeout=1)" in rls_adr
    assert "old-profile Catalog lease와 checked-out\nconnection이 0" in rls_adr
    assert "first-recorded-reason" in rls_adr
    assert "transition fence-first terminal old-result" in rls_adr
    assert "result-first completion-before-transition" in rls_adr
    assert "successful return만 transition commit point" in rls_adr
    assert "provider는 registry를 직접 읽지 않는다" in rls_adr
    assert "모든 managed source generation/state/disable transition" in rls_adr
    assert "exact `RESOURCE_READ_FAILED`" in rls_adr
    assert "External observation task" in rls_adr
    assert "SOURCE_CONTROL_UNAVAILABLE" in rls_adr
    assert "server_encoding=UTF8" in rls_adr
    assert "same-Python-name/different-relation" in rls_adr
    assert "oid < 16384" in rls_adr
    assert "reason_code=TENANT_CONTEXT_REQUIRED" in rls_adr
    assert "Historical decoder" in rls_adr
    assert "production v2 current/rollback row" in rls_adr
    assert "operator_subject" in rls_adr
    assert "metadata_phase_timeout_ms = min(30_000, 8 *" in rls_adr
    assert "Protected environment의 read-only\ninventory" in rls_adr
    assert (
        "Standalone v2 protected 실행은 `RLS-02` 완료 뒤 별도 `RLS-03`\n"
        "environment 승인/access/change record로 진행하며 `ENC-02`/`TIME-03`을 "
        "기다리지 않는다."
    ) in rls_adr
    assert (
        "Combined direct-v3\nprotected 실행만 `ENC-02` 완료 뒤 coordinated "
        "`RLS-03`/`TIME-03` environment 승인/access/change record를\n요구한다."
    ) in rls_adr
    canonical_start = rls_adr.index("각 root의 internal canonical material")
    canonical_end = rls_adr.index("`roles` array length", canonical_start)
    assert rls_adr[canonical_start:canonical_end].count('"definition_sha256"') == 1
    assert RLS_POLICY_ATTESTATION_ADR.name in development_todo
    assert "RLS_READER_CLIENT_ENCODING" in source_catalog
    assert "require_rls_reader_connection_policy" in source_catalog
    assert "checkout lease 직후 application `BEGIN`/SQL 전에" in source_catalog
    assert "Metadata는 Source\nCatalog의 no-SQL RLS connection policy를 소비" in metadata
    assert "RlsAttestationValidationError" in metadata
    assert "Private history decoder" in metadata
    assert "retired/transition profile fence" in metadata
    assert "successful return 전에 old checked-out connection을 0" in metadata
    assert "마지막에 registry projection을 교체" in control_plane
    assert "invalidate(source_id, *, next_source: SourceProfile | None)" in control_plane
    assert "old/new user route를 unavailable" in control_plane
    assert "Source Catalog의 RLS-only startup constant" in development_todo
    assert "history-decode/offline-serving/live-attestation" in development_todo
    assert (
        "Standalone v2 환경 작업은 별도 `RLS-03` 승인과 access/change record가 "
        "필요하며\n`ENC-02`/`TIME-03`을 기다리지 않는다."
    ) in development_todo
    assert (
        "Combined direct-v3 환경 작업만 coordinated\n`RLS-03`/`TIME-03` "
        "승인과\naccess/change record가 필요하다."
    ) in development_todo
    for module_name in MODULE_NAMES:
        module_contract = (
            ROOT_DIRECTORY / "docs" / "modules" / module_name / "README.md"
        ).read_text(encoding="utf-8")
        assert RLS_POLICY_ATTESTATION_ADR.name in module_contract
    for operator_document in (onboarding, extension_checklist):
        assert RLS_POLICY_DRIFT_AUDIT.name in operator_document
        assert RLS_POLICY_ATTESTATION_ADR.name in operator_document
        assert "`RLS-01`~`RLS-03`" in operator_document
        assert "production publish" in operator_document
        assert "non-UTF8 RLS" in operator_document
    assert "`RLS-01`" in rls_audit
    assert "cross-tenant" in rls_audit
    assert "UTF8, ICU `und`" in rls_audit
    assert "SQL_ASCII, libc `C`" in rls_audit
    assert "pg_shdepend" in rls_audit
    assert "같은 Python identifier" in rls_audit
    assert "Read-Only Implementation-Readiness Audit" in rls_audit
    assert "canary_in_traceback=True" in rls_audit
    assert "83 passed, 657 deselected, 2 xfailed" in rls_audit
    assert "public offline current-policy serving-compatibility" in rls_audit
    assert "모든 managed source transition" in rls_audit
    assert "non-RLS active query도 transition" in rls_audit
    assert "connection startup" in rls_audit
    assert (
        "Standalone v2 환경 작업은\naccess와 change record를 갖춘 별도 `RLS-03` "
        "승인을 요구하며 `ENC-02`/`TIME-03`을 기다리지 않는다."
    ) in rls_audit
    assert (
        "Combined direct-v3 환경 작업만 coordinated `RLS-03`/`TIME-03` 승인과 "
        "access/change record를 요구한다."
    ) in rls_audit
    assert "database `0`, role `0`" in audit
    assert "### Resolved follow-up: canonical `timestamptz`" in audit
    assert "role default `UTC`, `Asia/Seoul`, `America/New_York`" in audit
    assert CANONICAL_TIME_AUDIT.name in audit
    assert (
        "Repository implementation: Complete — production cutover remains an "
        "environment-specific change"
    ) in canonical_adr
    assert (
        "Status: Repository acceptance complete; production cutover pending "
        "environment evidence"
    ) in canonical_audit
    assert "production external state를 완료로 주장하지 않는다" in canonical_audit
    assert "열린 `TIME-03`" in audit
    assert (
        "Approval-required follow-up: lossless encoding and source semantics"
        in audit
    )
    assert "Status: Proposed — user approval required before implementation" in lossless_adr
    assert "`ENC-01-A` — lossless canonical values (recommended)" in lossless_adr
    assert "`DateStyle=ISO, YMD`" in lossless_adr
    assert "`IntervalStyle=iso_8601`" in lossless_adr
    assert "`extra_float_digits=1`" in lossless_adr
    assert "`standard_conforming_strings=on`" in lossless_adr
    assert "`transform_null_equals=off`" in lossless_adr
    assert "`array_nulls=on`" in lossless_adr
    assert "`client_encoding=UTF8`" in lossless_adr
    assert "`server_encoding=UTF8`" in lossless_adr
    assert "`timezone_abbreviations=Default`" in lossless_adr
    assert "`bytea_output=hex`" in lossless_adr
    assert "`default_text_search_config=pg_catalog.english`" in lossless_adr
    assert "`source_semantics_fingerprint`" in lossless_adr
    assert "Decoded key가 같은 duplicate" in lossless_adr
    assert 'exact `"24:00:00"`' in lossless_adr
    assert "이 ADR은 제안일 뿐 승인된 계약이 아니다" in lossless_adr
    assert "Exact failure and stale mapping" in lossless_adr
    assert "Result OID scalar allowlist는 exact 24개" in lossless_adr
    assert "snapshot_contract_version" in lossless_adr
    assert "Snapshot v1/v2/v3" in lossless_adr
    assert RLS_POLICY_ATTESTATION_ADR.name in lossless_adr
    assert "RLS-only admission" in lossless_adr
    assert "broader `ENC-01` 승인이 아니" in lossless_adr
    assert "snapshot_contract_version\": 3" in lossless_adr
    assert "stored v2 bytes/fingerprint를 복사하지 않고" in lossless_adr
    assert "첫 relation action" in lossless_adr
    assert LOSSLESS_SCALAR_ADR.name in module_index
    delivery_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "delivery" / "README.md"
    ).read_text(encoding="utf-8")
    assert LOSSLESS_SCALAR_ADR.name in delivery_contract
    assert "verified result hash" in delivery_contract
    assert "view_dependency_column_collations" in lossless_adr
    assert "view_dependency_definitions" in lossless_adr
    assert "direct pg_type edge" in lossless_adr
    assert re.search(
        r"declared/custom\s+domain pre-erasure rejection",
        lossless_adr,
    )
    assert "CollateClause" in lossless_adr
    assert "Function and operator dependency residual limitation" in lossless_adr
    assert "Text-search and order-sensitive aggregate residual limitation" in lossless_adr
    assert "IANA/POSIX named timezone rule drift" in lossless_adr
    assert "`pg_catalog.default` provider `database_default`" in lossless_adr
    assert "1,920 bytes" in lossless_adr
    assert (
        "sha256:60a62b61c6b1bb429987186730c9d24a6b0868c0cb0406ccad97a5698a900446"
        in lossless_adr
    )
    assert (
        "sha256:42b7b1da79339b115a950bc77c12b4178891be321b34701e072b5473e7b9b754"
        in lossless_adr
    )
    result_policy_match = re.search(
        r"#### Exact result policy v2 and SQL policy v3.*?```json\n(.*?)\n```",
        lossless_adr,
        re.DOTALL,
    )
    assert result_policy_match is not None
    result_policy = json.loads(result_policy_match.group(1))
    result_policy_bytes = json.dumps(
        result_policy,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    assert len(result_policy_bytes) == 1_920
    assert hashlib.sha256(result_policy_bytes).hexdigest() == (
        "60a62b61c6b1bb429987186730c9d24a6b0868c0cb0406ccad97a5698a900446"
    )
    sql_policy = {
        "version": 3,
        "result_encoding_policy": result_policy,
        "functions": sorted(sql_validation_module.DEFAULT_ALLOWED_FUNCTIONS),
        "operators": sorted(sql_validation_module.DEFAULT_ALLOWED_OPERATORS),
        "types": sorted(sql_validation_module.DEFAULT_ALLOWED_TYPES),
        "unqualified_types": sorted(
            sql_validation_module.DEFAULT_ALLOWED_UNQUALIFIED_TYPES
        ),
        "sql_value_functions": sorted(
            sql_validation_module._ALLOWED_SQL_VALUE_FUNCTIONS
        ),
        "forbidden_nodes": sorted(
            sql_validation_module._FORBIDDEN_NODE_CODES.items()
        ),
        "rejected_expressions": sorted(
            sql_validation_module._REJECTED_A_EXPR_CONSTRUCTS.items()
        ),
        "allowed_nodes": sorted(sql_validation_module._ALLOWED_NODE_TAGS),
    }
    sql_policy_bytes = json.dumps(
        sql_policy,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert hashlib.sha256(sql_policy_bytes).hexdigest() == (
        "42b7b1da79339b115a950bc77c12b4178891be321b34701e072b5473e7b9b754"
    )
    assert tuple(result_policy["result_oid"]["allowed_pg_catalog_scalar"]) == (
        "bit",
        "bool",
        "bpchar",
        "bytea",
        "cidr",
        "date",
        "float4",
        "float8",
        "inet",
        "int2",
        "int4",
        "int8",
        "interval",
        "json",
        "jsonb",
        "numeric",
        "text",
        "time",
        "timestamp",
        "timestamptz",
        "timetz",
        "uuid",
        "varbit",
        "varchar",
    )
    assert result_policy["result_oid"]["domain"] == (
        "reject_declared_domain_before_oid_erasure"
    )
    assert result_policy["reader_session"]["bytea_output"] == "hex"
    assert result_policy["reader_session"]["default_text_search_config"] == (
        "pg_catalog.english"
    )
    assert "user-result cursor scope" in lossless_adr
    assert "`EXPLAIN (FORMAT JSON)`" in lossless_adr
    assert "`IntervalStyle=postgres`를 설정·검사" in lossless_adr
    assert "Runtime이 type/setting exclusion을 강제하지 못" in lossless_adr
    assert "Range/Multirange 및 그 array" in lossless_adr
    assert "domain-over-approved-array" in lossless_adr
    assert "array-of-domain/enum" in lossless_adr
    assert "`bit|varbit`는" in lossless_adr
    for result_type in ("money", "XML", "geometric", "macaddr"):
        assert result_type in lossless_adr
    assert "`oid/name/reg*/xid*`" in lossless_adr
    assert "money/XML/geometric/macaddr/pg_lsn/tid" in lossless_adr
    assert "non-1 lower bound" in lossless_adr
    assert "`ENC-02`에서 final encoding baseline을 구현·검증한" in lossless_adr
    assert "B는 policy version, migration/cutover" in lossless_adr
    assert "하나의 coordinated `TIME-03`" in lossless_adr
    assert "SQL policy v3" in lossless_adr
    assert "sha256:a1d1217174eb9b0ebce121652ec50bec72411619310ca4f1fee427d55f412014" in audit
    assert "sha256:3b05810025aca001615bd4e78fdbb40763f9d3ea1ba257043625796ba3783ced" in audit
    assert "sha256:77f588e368495248abbd8eb87354efadbd31afa38d0ca675154506624470f06a" in audit
    assert "sha256:0a4513b560854f795950856ddcddcc1a5f8fac4b0341fce951944bbc8ba066dd" in audit
    assert "sha256:dadd5b0c8d9a51f5db4a5117d804c30dcbcc7f4cfa417a4df154de40d63de4f3" in audit
    assert "sha256:638b941219f3f2bbbd3a92acaf57a2cc5f14e026d386e161fd8b3d24afa32b43" in audit
    assert "sha256:64f407d6e0fcd189c2c7d4bed463c38771b2f31823d40ff9cb96886fae19ce76" in audit
    assert "sha256:c4692859cde38b3e26c3bc09be96cc3ae2db09442fb7e8e826deace60da05a64" in audit
    assert "sha256:24a658e9869ee578b8189b9e41242fe1521c1843bf2e4bae7ff64cca6c9c396f" in audit
    assert "sha256:a6e1781ce2c45d140ae02f09454591e2ce6dcbd16eb2d3ca699f1f86a10b678a" in audit
    assert "sha256:265de8ffe863aa833be5993c281f86ae00468a34e51345ab53e537622c071b48" in audit
    assert "sha256:f5990467cfa9498375afc2cab1363623590acfe5305370bf35dfc437c42704c8" in audit
    assert "sha256:675a9688aa730d64927d9a124cec8825eb6f87abf0da494410bb26576f9fc5a1" in audit
    assert "sha256:07714fda947fb9e09a2b6217b0fe0c4e53eb3d7032cce257e157acf1eb64b553" in audit
    assert "sha256:be10c695747100145649abc3d972028963c4cb6dd3fbf2ca34bee276516e7c61" in audit
    assert "sha256:650abf959c971b3fd503ca4db961b5e37d917207abde3500214ad23d64833b56" in audit
    assert "sha256:bc865c9c470c0a06cf4e957928f26fc1c3dc7d6ae1cfaebe271f53ace90b793a" in audit
    assert "sha256:50d6676ae9c55a3167bd4b59b6f3c31f3798157f8937cb8968219a7ca754f375" in audit
    assert "`47 passed`, 16 deselected" in audit
    assert "`14 passed`, 1 deselected" in audit
    assert "`47 passed`, 20 deselected" in audit
    assert "`18 passed`, 1 deselected" in audit
    assert "`24 passed`, 1 deselected" in audit
    assert "`28 passed`, 1 deselected, 2 xfailed" in audit
    assert "`645 passed`, 92 deselected" in audit
    assert "`80 passed`, 657 deselected" in audit
    assert "`645 passed`, 97 deselected" in audit
    assert "`83 passed`, 657 deselected, 2 xfailed" in audit


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


def test_runtime_replica_observation_docs_preserve_contract_boundaries() -> None:
    readme = (ROOT_DIRECTORY / "README.md").read_text(encoding="utf-8")
    environment_example = (ROOT_DIRECTORY / ".env.example").read_text(encoding="utf-8")
    onboarding = (ROOT_DIRECTORY / "docs" / "source-onboarding.md").read_text(
        encoding="utf-8"
    )
    operations = (ROOT_DIRECTORY / "docs" / "operations.md").read_text(
        encoding="utf-8"
    )
    management = SOURCE_MANAGEMENT_PLAN.read_text(encoding="utf-8")
    control_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "control-plane" / "README.md"
    ).read_text(encoding="utf-8")
    delivery_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "delivery" / "README.md"
    ).read_text(encoding="utf-8")
    audit = RUNTIME_REPLICA_OBSERVATION_AUDIT.read_text(encoding="utf-8")

    for document in (readme, environment_example, onboarding, operations):
        assert "QUERY_MAN_REPLICA_ID" in document
    for document in (management, delivery_contract, audit):
        assert "GET /admin/sources/{source_id}/replicas" in document
    assert "observed_at + 3 * heartbeat_interval_ms" in control_contract
    for reason in (
        "NOT_OBSERVED",
        "HEARTBEAT_EXPIRED",
        "CONTROL_SCAN_FAILED",
        "RUNTIME_VALIDATION_REJECTED",
        "RUNTIME_APPLY_FAILED",
        "METADATA_PROBE_FAILED",
    ):
        assert reason in audit
    assert "question과 SQL" in audit
    assert "data plane, readiness" in audit


def test_resource_and_gateway_observation_docs_preserve_contract_boundaries() -> None:
    decision = CENTRAL_SOURCE_ADR.read_text(encoding="utf-8")
    runtime_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "runtime" / "README.md"
    ).read_text(encoding="utf-8")
    control_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "control-plane" / "README.md"
    ).read_text(encoding="utf-8")
    audit = RESOURCE_AND_GATEWAY_OBSERVATION_AUDIT.read_text(encoding="utf-8")

    for document in (decision, control_contract, audit):
        assert "logical visibility/input window" in document
        assert (
            "나이만으로" in document
            or "나이만을 이유로" in document
            or "age-only" in document
        )
        assert "1,000" in document
    assert "process당 최대 4" in audit
    assert "process당 Control connection 최대" in runtime_contract
    for table_name in (
        "source_resource_observations",
        "gateway_usage_rollups",
        "gateway_usage_report_cursors",
    ):
        assert table_name in audit
    assert "새 HTTP/MCP endpoint" in audit
    assert "`CTRL-08`" in audit


def test_usage_projection_docs_preserve_contract_boundaries() -> None:
    decision = CENTRAL_SOURCE_ADR.read_text(encoding="utf-8")
    management = SOURCE_MANAGEMENT_PLAN.read_text(encoding="utf-8")
    control_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "control-plane" / "README.md"
    ).read_text(encoding="utf-8")
    delivery_contract = (
        ROOT_DIRECTORY / "docs" / "modules" / "delivery" / "README.md"
    ).read_text(encoding="utf-8")
    audit = USAGE_PROJECTION_AUDIT.read_text(encoding="utf-8")

    for document in (decision, management, control_contract, audit):
        assert "source_resource_observation_attempts" in document
        assert "OBSERVATION_INCOMPLETE" in document
        assert "REPORTER_UNAVAILABLE" in document
        assert "logical visibility/input window" in document
        assert "1,000" in document
    for document in (management, delivery_contract, audit):
        assert "GET /admin/sources/{source_id}/usage" in document
        assert "PROVIDER_NOT_CONFIGURED" in document
    assert "last_report_at" in control_contract
    assert "amount" in delivery_contract
    assert "currency" in delivery_contract
    assert "age-only" in audit or "나이만" in audit


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
    assert "Source Catalog onboarding workflow (plan-only)" in index
    source_catalog_contract = (
        MODULE_INDEX.parent / "source-catalog" / "README.md"
    ).read_text(encoding="utf-8")
    assert "Plan-only `query-man-source-onboarding` workflow" in source_catalog_contract
    assert "production dependency로 확대하지 않는다" in source_catalog_contract

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

    assert "Status: Accepted choices — implementation complete" in guide
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
    assert "D5 offline CLI composition 격리 (`MOD-08`) — 완료" in guide
    assert "### D5-A 구현 결과 (`MOD-08` 완료)" in guide
    assert "7. 전체 dependency/contract audit — 완료 (2026-08-25)" in guide
    assert "`TIME-01`과 `TIME-02`에서\n결정·구현" in guide
    assert "`TIME-03`은 열려 있다" in guide
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
    assert "`MOD-08`" in roadmap
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
