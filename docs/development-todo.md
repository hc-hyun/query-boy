# Active Development TODO

Status: Active — `LAUNCH-02` is the only active launch item; future tracks are parked

이 문서는 **아직 끝나지 않은 일만** 기록한다. 완료 이력과 실행 증거는
[implementation roadmap](implementation-roadmap.md#14-post-baseline-completion-ledger-and-active-development)에
남긴다. 세부 설계를 여러 module 문서에 복제하지 않고, 현재 의미는 accepted ADR과 runnable
test에서 확인한다.

## Management Rules

- 작업 시작점은 [module index](modules/README.md), primary module README와 해당 decision이다.
- 한 agent는 primary module 하나와 명시된 file allowlist만 맡는다. Shared file과 Git은 coordinator가
  single-writer로 처리한다.
- Module interface, external API, persisted format, policy, lifecycle, ownership 또는 protected
  procedure 의미는 정확한 사용자 승인 없이는 바꾸지 않는다.
- Parked item은 조사 기록이지 active implementation queue가 아니다. 실제 요구, 우선순위와 정확한
  변경 범위를 다시 승인받은 뒤에만 시작한다.
- 완료한 checkbox는 이 파일에서 제거하고 roadmap ledger에 evidence와 함께 옮긴다.
- 과거 verification record와 stored row를 현재 의미에 맞춰 수정·삭제하지 않는다.
- Root gate는 `ruff`, `mypy`, full pytest다. Source DB 경계는 integration, release 경계는 container와
  verified-query acceptance까지 실행한다.

## Current First-Launch Baseline

[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A`가 current repository
authority다.

- Static bootstrap source: `development-issues`, `market-voc`
- Single Query Man replica, PostgreSQL 18, server/client UTF-8
- RLS source 전면 quarantine
- Final result OID: `20, 21, 23, 25, 1082, 1184, 1700`
- SQL policy v3; metadata revision, persisted schema와 9개 expected result hash는 유지
- Managed mode, broader result type, RLS serving, HA, cost attribution와 workflow trace는 first launch
  serving 범위 밖

`LAUNCH-01-A` repository implementation과 local acceptance는
[완료됐다](verification/2026-08-26-static-first-launch.md).
Repository implementation과 local acceptance는 protected environment 전환 권한이 아니다.

## Protected Environment Execution

- [ ] `LAUNCH-02` 대상 환경, access, TLS/secret/backup, source·DDL·role/settings inventory, artifact
  digest, route, stop/rollback condition과 change-record owner를 승인받아 ADR 0025의 실제 cutover를
  수행한다. Repository fixture나 local container 결과를 production evidence로 대신하지 않는다.

## Parked: RLS Serving

Start gate: 실제 RLS source 제공 요구와 새로운 exact attestation/policy/migration/cutover 승인이
모두 있어야 한다. 현재는 RLS를 허용하는 작은 우회 변경을 만들지 않는다.

- [ ] `RLS-01` [ADR 0024](decisions/0024-rls-policy-drift-attestation.md)의 research를 launch v3
  baseline에 다시 맞추고 지원할 RLS relation/policy 범위를 결정한다.
- [ ] `RLS-02` 승인된 attestation, snapshot/codec, query-time admission과 provider/consumer test를
  구현한다.
- [ ] `RLS-03` protected source inventory, migration, rollback과 cross-tenant acceptance를 별도 실행
  승인 아래 수행한다.

## Parked: Broader Result Types

Start gate: 현재 7개 OID로 표현할 수 없는 실제 질문과 필요한 PostgreSQL type corpus가 있어야 한다.
Success domain을 넓힐 때는 launch SQL policy v3를 덮어쓰지 않고 v4 이상으로 전환한다.

- [ ] `ENC-01` [ADR 0020](decisions/0020-lossless-interval-and-json-numeric-encoding.md)의 research에서
  필요한 type만 다시 선택하고 exact lossless value/OID/reader policy를 제안한다.
- [ ] `ENC-02` 승인된 type의 loader, encoder, revision, verified migration과 rollback을 구현·검증한다.

## Parked: Managed Canonical-Time Cutover

- [ ] `TIME-03` 실제 managed environment가 canonical-time R2 또는 이후 cumulative policy로
  전환될 때 protected inventory, drain, current/rollback verified membership과 route evidence를
  별도 승인 아래 남긴다. Static first launch의 선행 조건은 아니다.

## Parked: Database-Native Cost And Alerting

Start gate: PostgreSQL statement-level aggregate가 실제 운영 의사결정에 필요하고, monitor privilege와
retention 범위가 다시 승인되어야 한다. 현재 plan cost, timeout, concurrency, row/byte/resource limit는
[query cost runbook](query-cost-control.md)의 existing boundary를 사용한다.

- [ ] `COST-01` [ADR 0021](decisions/0021-database-native-cost-attribution.md)의 monitoring identity와
  최소 권한 선택을 다시 결정한다.
- [ ] `COST-02` 승인된 bounded collector와 failure isolation을 구현한다.
- [ ] `COST-03` aggregate persistence와 operator projection을 구현한다.
- [ ] `COST-04` base evidence가 생긴 뒤에만 [ADR 0023](decisions/0023-database-native-usage-spike-alert.md)의
  threshold/event/retention을 별도 결정한다.
- [ ] `COST-05` protected fixture에서 reset, failover, explicit zero와 rollback을 검증한다.

## Parked: Workflow Trace

Start gate: server-generated request/call/query ID로 해결되지 않는 실제 end-to-end correlation 요구와
header trust boundary 승인이 있어야 한다.

- [ ] `TRACE-01` [ADR 0022](decisions/0022-w3c-workflow-trace-context.md)의 context 선택을 다시 결정한다.
- [ ] `TRACE-02` 승인된 HTTP/MCP context와 Runtime scope를 구현한다.
- [ ] `TRACE-03` redaction과 metric-cardinality 경계를 검증한다.
- [ ] `TRACE-04` parallel/retry/disconnect/multi-replica acceptance를 실행한다.

## Explicit Non-Goals

- Static first launch에 신규 DB, RLS, Control DB admin, 두 번째 replica 또는 public mutation MCP를
  슬쩍 추가하지 않는다.
- Prompt, Skill 또는 caller 관례를 authorization, SQL validation, reader privilege나 resource limit의
  enforcement로 사용하지 않는다.
- Query별 통화 비용, user별 chargeback, distributed global quota와 다단계 management RBAC는 실제
  요구와 별도 decision 전에는 만들지 않는다.
- Protected deployment를 repository test 통과만으로 완료 처리하지 않는다.
