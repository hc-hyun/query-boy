# Verification Evidence Index

이 파일은 탐색용 색인이며 현재 구현의 단일 완료 증거가 아니다. 각 evidence 문서는 기록 당시
실행한 범위와 사실을 보존하는 immutable record다. 과거 기록을 현재 상태에 맞춰 수정·삭제하거나
상태를 재분류하지 않는다. 정정이 필요하면 원문을 유지하고 날짜, 대상 원문과 provenance를 명시한 새
기록을 추가한다.

`Complete`는 해당 문서가 명시한 범위에만 적용되며 이후 commit을 자동으로 증명하지 않는다. 이전 의미가 후속
기록으로 대체되어도 이전 실행 사실은 삭제하지 않는다. `Open` finding과 strict xfail은 PASS나 위험 수용이
아니다. 현재 지원 상태는 [architecture](../architecture.md), 활성 작업은 [development TODO](../development-todo.md),
완료 ledger는 [implementation roadmap](../implementation-roadmap.md)에서 확인한다.

| File | 문서 제목 | 문서 자체 상태 | 기록 범위 |
|---|---|---|---|
| [2026-08-22-safe-query.md](2026-08-22-safe-query.md) | Safe Query Verification — 2026-08-22 | 미기재 | PostgreSQL 18.6 local fixture의 당시 guarded-query 안전, 제한, 취소와 golden query 경계 |
| [2026-08-23-column-disclosure.md](2026-08-23-column-disclosure.md) | Column Disclosure Verification — 2026-08-23 | 미기재 | 73-column synthetic relation의 당시 question-scoped column disclosure와 필수 semantic column 보존 |
| [2026-08-23-completion-audit.md](2026-08-23-completion-audit.md) | Final Completion Audit — 2026-08-23 | `Complete` | 당시 100개 checklist production baseline의 종합 실행 증거; 이후 변경은 포괄하지 않음 |
| [2026-08-23-container-runtime.md](2026-08-23-container-runtime.md) | Container Runtime Audit — 2026-08-23 | `Complete` | Local loopback Compose의 단일 HTTP/MCP container, 인증, readiness와 guarded query |
| [2026-08-23-control-schema-migrations.md](2026-08-23-control-schema-migrations.md) | Control Schema Migration And Test Isolation Audit | `Complete` | Numbered migration, checksum drift, security reconciliation과 disposable Control DB test 격리 |
| [2026-08-23-managed-source-startup.md](2026-08-23-managed-source-startup.md) | Managed Source Startup Verification — 2026-08-23 | `Complete` | Bootstrap/managed authority 분리, zero-bootstrap, Control state 복원과 one-time cutover 경계 |
| [2026-08-23-mcp-multi-replica-soak.md](2026-08-23-mcp-multi-replica-soak.md) | MCP Multi-Replica Soak — 2026-08-23 | `Complete` | 두 Compose replica의 exact MCP 결과, 포화·복구와 1,000-session resource 경계 |
| [2026-08-23-mcp-server-assurance.md](2026-08-23-mcp-server-assurance.md) | MCP Server Assurance — 2026-08-23 | `Complete` | 실제 Docker MCP endpoint의 protocol, 보안, 품질, 병렬·포화와 disconnect |
| [2026-08-23-mcp.md](2026-08-23-mcp.md) | MCP MVP Verification — 2026-08-23 | 미기재 | 공식 MCP client가 HTTP와 동일한 Gateway와 PostgreSQL query 경계를 사용한 초기 MVP 증거 |
| [2026-08-23-metadata-publishing.md](2026-08-23-metadata-publishing.md) | Metadata Publishing Verification — 2026-08-23 | 미기재 | Immutable metadata snapshot, atomic active pointer, restart 복구, rollback pin과 resume |
| [2026-08-23-metadata-quality.md](2026-08-23-metadata-quality.md) | Metadata Quality Verification — 2026-08-23 | 미기재 | 16개 retrieval case의 relation accuracy, answerability recall과 context-byte gate |
| [2026-08-23-no-deploy-onboarding.md](2026-08-23-no-deploy-onboarding.md) | No-Deploy Source Onboarding Verification — 2026-08-23 | 미기재 | Admin staging부터 hot reload, rotation, rollback과 세 번째 source L2/MCP까지의 초기 acceptance |
| [2026-08-23-operations.md](2026-08-23-operations.md) | Operations Verification — 2026-08-23 | 미기재 | 당시 logging, health, shutdown, 5-table restore와 security automation 실행 결과 |
| [2026-08-23-physical-catalog.md](2026-08-23-physical-catalog.md) | Physical Catalog Verification — 2026-08-23 | 미기재 | Reader 권한 범위의 PK/FK/index를 제한된 metadata projection으로 제공하는지 검증 |
| [2026-08-23-quality-levels.md](2026-08-23-quality-levels.md) | Metadata Quality Level Verification — 2026-08-23 | 미기재 | L0/L1/L2 자동 판정과 declared minimum publish gate |
| [2026-08-23-refactoring-assurance.md](2026-08-23-refactoring-assurance.md) | Refactoring Assurance Audit — 2026-08-23 | `Complete` | Production baseline 이후 `REF-01`~`REF-15`의 상태 경쟁, 권한, 종료와 운영 보강 |
| [2026-08-23-release-acceptance.md](2026-08-23-release-acceptance.md) | Production Release Acceptance — 2026-08-23 | 미기재 | 당시 `REL-01`~`REL-08`의 세 source, 품질, 공격, 부하와 복구 acceptance |
| [2026-08-23-shared-access.md](2026-08-23-shared-access.md) | Shared Access Verification — 2026-08-23 | `Complete` | Access-policy v2의 shared active-source visibility와 query/operator capability 분리 |
| [2026-08-23-source-control-store.md](2026-08-23-source-control-store.md) | Source Control Store Verification — 2026-08-23 | `Historical baseline; manifest compatibility evidence superseded by CTRL-04` | Encrypted immutable source generation 저장의 역사적 baseline; 당시 manifest compatibility는 strict v2가 대체 |
| [2026-08-23-source-extension.md](2026-08-23-source-extension.md) | Source Extension Assurance — 2026-08-23 | 미기재 | 네 번째 database의 code-branch 없는 onboarding과 두-replica MCP 결과; 당시 caller scope는 shared-access 기록이 대체 |
| [2026-08-23-source-management-catalog.md](2026-08-23-source-management-catalog.md) | Source Management Catalog Audit — 2026-08-23 | `Complete` | Strict manifest v2 provenance와 secret-free operator inventory/detail/history |
| [2026-08-23-source-mutation-receipts.md](2026-08-23-source-mutation-receipts.md) | Source Mutation Receipt Audit — 2026-08-23 | `Complete` | Source mutation의 expected-state CAS, idempotency와 immutable terminal receipt/history |
| [2026-08-23-tenant-isolation.md](2026-08-23-tenant-isolation.md) | Tenant Isolation Verification — 2026-08-23 | 미기재 | Trusted tenant 전달, transaction-local context와 pool reset의 당시 표면; 후속 base-policy drift finding을 대체하지 않음 |
| [2026-08-25-canonical-time-stability.md](2026-08-25-canonical-time-stability.md) | Canonical Time Stability Verification | `Repository acceptance complete; production cutover pending environment evidence` | UTC canonical-time repository acceptance와 revision/hash 전환; production cutover는 미완료 |
| [2026-08-25-control-recovery-acceptance.md](2026-08-25-control-recovery-acceptance.md) | Control Recovery Acceptance — 2026-08-25 | `Complete` | PostgreSQL 18.4→18.6의 13-table archive, key, zero-bootstrap와 두-replica 복구 fixture |
| [2026-08-25-resource-and-gateway-observations.md](2026-08-25-resource-and-gateway-observations.md) | Resource And Gateway Observation Audit — 2026-08-25 | `Complete` | `CTRL-07` resource sample과 privacy-safe gateway hourly lower-bound persistence |
| [2026-08-25-runtime-replica-observations.md](2026-08-25-runtime-replica-observations.md) | Runtime Replica Observation Audit — 2026-08-25 | `Complete` | `CTRL-06` stable replica identity, desired/applied drift와 DB-clock freshness projection |
| [2026-08-25-source-database-corners.md](2026-08-25-source-database-corners.md) | Source Database Corner Acceptance — 2026-08-25 | `DBEDGE-01~DBEDGE-05 complete; separate RLS security finding open` | Disposable DB의 metadata/query corner, unresolved scalar/result gap과 별도 RLS finding 구분; last updated 2026-08-26 |
| [2026-08-25-source-onboarding-skill.md](2026-08-25-source-onboarding-skill.md) | Source Onboarding Skill Acceptance — 2026-08-25 | `Complete` | Plan-only onboarding Skill의 fresh-context routing, secret/mutation 거부와 zero-mutation handoff |
| [2026-08-25-usage-projection.md](2026-08-25-usage-projection.md) | Usage Projection Audit — 2026-08-25 | `Complete` | `CTRL-08` resource latest-attempt/last-success와 global gateway reporter 상태의 operator projection |
| [2026-08-26-lower-track-contract-prework.md](2026-08-26-lower-track-contract-prework.md) | Lower-Track Boundary Prework Verification | 미기재 | COST/TRACE 제안의 disposable read-only prework; 구현, 우선순위 시작 또는 사용자 승인이 아님 |
| [2026-08-26-rls-policy-drift.md](2026-08-26-rls-policy-drift.md) | RLS Policy Drift Security Finding — 2026-08-26 | `Open — boundary decision and fail-closed implementation required` | Hidden base-policy 완화 또는 RLS disable 뒤 cross-tenant row가 성공하는 열린 보안 결함과 strict xfail |
