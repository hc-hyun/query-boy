# Active Development TODO

Status: Active

이 문서는 완료된 production baseline 이후의 개발 작업을 우선순위대로 관리한다. 완료된
역사는 [implementation roadmap](implementation-roadmap.md)에 보존하고, 여기에는 지금부터
실행할 작업과 검증 가능한 종료 조건만 둔다.

## Management Rules

- 위에서 아래로 진행하며, 더 높은 우선순위가 열려 있으면 낮은 우선순위를 먼저 시작하지
  않는다.
- `[x]`는 구현, 자동 테스트, 운영 문서와 실행 증거가 모두 있을 때만 표시한다.
- 각 항목은 하나의 검증 가능한 결과를 가지며 ID를 재사용하지 않는다.
- 비밀, 질문 원문, SQL text와 parameter는 비용·trace 수집에도 저장하지 않는다.
- MCP는 현재 지원 버전 `2026-07-28`만 받는다. 이전 handshake와 protocol version을 위한
  compatibility branch는 만들지 않으며 version 변경은 명시적인 upgrade 작업으로 처리한다.

## P0 — Multi-Replica MCP Soak

목표: 실제 Docker replica 두 개에서 protocol, query budget과 process resource 경계가
반복 가능한지 검증한다. 현재 fixture reader role의 connection budget은 정확히 두 replica만
지원하므로 세 번째 replica는 이 범위에 포함하지 않는다.

- [x] `SOAK-01` `/mcp`가 `2026-07-28` protocol header만 허용하고 누락·이전·미지원·중복
  version을 bounded error로 거부한다.
- [x] `SOAK-02` 기본 Compose는 한 replica를 유지하고 `soak` profile에서만 동일 image의 두
  번째 loopback replica를 시작한다.
- [x] `SOAK-03` 두 replica가 같은 tool schema와 metadata revision으로 exact verified query를
  실행하고 전체 `query_id`가 고유한지 검증한다.
- [x] `SOAK-04` 두 replica의 source별 concurrency를 동시에 포화시켜 각각 overload되며 다른
  source는 성공하고 timeout 뒤 즉시 복구되는지 검증한다.
- [x] `SOAK-05` 공식 client의 stateless session 1,000개를 동시성 20으로 실행해 replica별
  정확히 500개씩 성공하는지 검증한다.
- [x] `SOAK-06` session churn 전후 PID, restart, OOM, file descriptor와 RSS를 측정하고 bounded
  growth 기준을 CI assertion으로 고정한다.
- [x] `SOAK-07` 장시간 soak를 일반 PR gate와 분리한 주간·수동 CI workflow와 재현 가능한 실행
  기록을 제공한다.

실행 증거와 threshold는
[multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md)에 기록한다.

## P1 — Centralized Source Management Plane

목표: Production managed source의 정의, 활성 상태, 이력, 소유권, 규모와 비용 projection을
Control DB와 하나의 operator management API에서 관리한다. 실제 DB, secret과 raw metric은
분산돼도 관리자는 한 surface에서 authority와 freshness를 확인한다. Authority와 artifact
경계는 [ADR 0016](decisions/0016-centralized-source-management-plane.md), 상세 구현 계획은
[source management plane](source-management-plane.md)을 따른다.

- [ ] `CTRL-01` Control schema에 번호 기반 migration을 도입하고 production/development와
  disposable integration-test Control DB를 분리해 fixture generation이 운영 history에
  누적되지 않게 한다.
- [ ] `CTRL-02` Control DB lifecycle과 verified contract가 restart/rollback/deactivate 뒤에도
  bootstrap seed/contract보다 우선하는 managed-mode startup을 구현한다. 일회성 import가 필요한
  contract를 Control DB로 이관한 뒤 filesystem contract를 무시하게 하고 zero-bootstrap
  production 및 precedence 회귀를 격리 Control DB에서 검증한다.
- [ ] `CTRL-03` Source owner, environment, classification, cost center, DB migration과 secret
  provider의 opaque credential binding/version provenance를 immutable generation/lifecycle과
  연결해 저장한다.
- [ ] `CTRL-04` 외부 authenticator의 immutable issuer/subject와 분리된 management audience를
  검증하고 query token을 거부한다. Role/source-scope binding은 Control DB가 소유하며, 빈 binding
  table, 외부 one-time credential과 영구 unconsumed marker를 한 transaction/lock으로 확인해 최초
  admin binding, audit와 consumed 전이를 원자적으로 수행한다. Replay/동시 bootstrap과 마지막 admin
  삭제를 거부한다.
  Viewer와 scoped operator용 source list/detail/generation history API에
  pagination/filter/bounded redaction을 적용한다.
- [ ] `CTRL-05` Source state를 바꾸지 않는 validation/change plan과 authoritative change-result를
  구현한다. Server-owned allowlisted credential binding만 받아 exact provider version을 일시
  resolve한다. Binding의 source ID, host/port/database/user/TLS, `query_reader` purpose와 version이
  manifest와 일치할 때만 plan hash에 묶고 requester/reason/outcome의 append-only audit와
  lifecycle history를 남긴다.
- [ ] `CTRL-06` 기존 boolean mutation operator를 source operator, approver와 platform admin
  역할로 교체하고 role-binding mutation, approval-bound/idempotent apply, 자기 승인 금지와
  audited break-glass를 구현한다. External secret admin/DB owner만 binding을 생성·회전하며 apply는
  승인 plan의 binding target/purpose/version을 모두 다시 검증하고 달라지면 fail-closed한다.
- [ ] `CTRL-07` Query caller identity/tenant 인증은 기존 external/deployment authority에 남기고,
  access-policy의 source 범위를 Control DB의 versioned `allowed_sources|all_sources` grant로 한 번
  import한다. Explicit platform-admin migration만 canonical complete-seed digest를 받아 한 Control DB
  lock/transaction에서 전체 grant, actor/digest audit와 permanent consumed marker를 원자적으로
  commit한다. Replica startup import, partial grant, 다른 digest와 marker 이후 재-import를
  fail-closed하고 exact digest result만 idempotent하게 조회한다. Effective visibility를 management
  API에서 조회하되 source publish와 별도 승인한다.
- [ ] `CTRL-08` Replica별 applied generation/state version/metadata revision/health heartbeat를
  수집해 desired/applied drift와 freshness를 management API에서 조회한다.
- [ ] `CTRL-09` Representative volume metric, estimated rows, table/index/storage bytes와 growth를
  method/definition revision/time/freshness와 함께 bounded하게 수집하고 일반 view의 무제한
  `COUNT(*)`를 금지한다.
- [ ] `CTRL-10` Gateway usage rollup과 usage/cost projection의 bounded 입력 계약,
  allocation-method version 및 `not_configured|pending|available|stale|unavailable` 상태,
  last-attempt time과 bounded reason을 구현한다. Missing/failed 값을 0으로 표시하지 않는다.
  DB-native/provider connector 없이도 완료할 수 있게 하고 cardinality/retention, 전체 authority
  table의 backup/restore 및 multi-replica end-to-end acceptance를 완료한다.

현재 다음 작업은 `CTRL-01`이다. `CTRL-04`부터 `CTRL-06`까지 완료되기 전에는 AI production
executor를 활성화하거나 현재 direct publish API에 자동 retry를 추가하지 않는다.

## P2 — Source Onboarding Skill

목표: 기존 no-restart source onboarding 계약을 Codex가 반복 가능하게 orchestration하되,
Skill을 security boundary로 사용하거나 조회용 MCP token을 operator로 승격하지 않는다. 상세
설계와 단계별 gate는 [source onboarding Skill plan](source-onboarding-skill-plan.md)에서
관리한다.

- [ ] `SKILL-01` Skill scope, repository 위치, request/trigger 경계와 manual runbook·query
  Skill의 책임 분리를 설계 review로 확정한다.
- [ ] `SKILL-02` DB owner, source operator, reader credential과 조회 caller의 권한 분리,
  secret 전달, mutation 승인과 stop condition을 threat review한다.
- [ ] `SKILL-03` 신규 L0 evidence review/plan/handoff와 조건부 apply/verify 상태,
  PostgreSQL 18·RLS·TLS 검증, Control DB artifact authority/retention과 instruction-only 대
  deterministic helper 결정을 승인한다.
- [ ] `SKILL-04` Release mode를 확정한다. Executor면 `CTRL-04`부터 `CTRL-06`의
  non-publishing plan/state와 idempotent reconciliation 계약을 소비하고, plan-only면 mutation
  0건 경계와 handoff를 decision 및 test로 고정한다.
- [ ] `SKILL-05` 승인된 최소 구조로 repository Skill을 구현하고 기존 onboarding 문서를
  progressive disclosure reference로 연결한다.
- [ ] `SKILL-06` 선택한 mode의 실행 경계를 구현한다. Executor면 broker-owned
  target/management-auth, server-owned reader credential binding, plan-ID-only apply, bounded output과
  자동 retry 금지를 구현하고, plan-only면 admin/helper 호출 0건을 검증한다.
- [ ] `SKILL-07` Positive/negative/adversarial trigger와 mutation approval을 realistic prompt 및
  독립 forward test로 검증한다.
- [ ] `SKILL-08` `support-tickets` fixture에서 선택한 mode를 검증한다. Plan-only면 reviewed
  plan/handoff와 mutation 0건, executor면 plan→approval→publish→HTTP/MCP query를 재현하고
  조회용 token이 non-operator로 유지되는지 확인한다.
- [ ] `SKILL-09` 선택한 mode에서 관측 가능한 replica revision/quality, caller isolation, 비용
  경계와 secret/question/SQL 비노출 acceptance를 통과한다. Plan-only는 publish 성공을
  주장하지 않는다.
- [ ] `SKILL-10` Skill validation, 전체 관련 회귀, 운영 문서와 재현 증거를 완료한 뒤 기본
  onboarding workflow 채택 여부를 기록한다.

이 track의 다음 작업은 `SKILL-01`이다. `SKILL-01`부터 `SKILL-03`까지 승인되기 전에는
scaffold/helper를 구현하지 않고, `CTRL-04`부터 `CTRL-06`까지 완료되기 전에는 production
executor를 구현하지 않는다.

## P3 — Database-Native Cost Attribution

목표: 이미 적용 중인 timeout, concurrency, result, memory/temp/JIT hard limit에 더해 완료된
query의 실제 DB 자원 사용을 fingerprint 단위로 측정한다. 통화 단위 청구액은 provider billing
자료가 있을 때만 별도 계산한다.

- [ ] `COST-01` 대상 PostgreSQL의 monitoring identity, 최소 권한, 지원 extension과 reset/보존
  정책을 inventory하고 개발·production별 사용 가능 신호를 decision record로 확정한다.
- [ ] `COST-02` `pg_stat_statements` 기반 sample collector를 구현해 fingerprint별 calls,
  execution time, rows, shared/local/temp block과 WAL 수치를 수집하되 SQL text와 parameter는
  저장하지 않는다.
- [ ] `COST-03` source, tenant, gateway fingerprint와 DB-native 통계를 bounded-cardinality로
  연결하고 reset, eviction, replica 중복과 sampling 오차를 명시한다.
- [ ] `COST-04` 비용 급증 threshold, alert, retention과 operator 조회 계약을 정의하고 public
  endpoint·metric label에 source나 tenant를 노출하지 않는다.
- [ ] `COST-05` 실제 fixture에서 저비용·CPU·I/O·temp 사용 query를 구분하는 acceptance와
  chargeback 불가 시의 운영 판단 절차를 문서화한다.

이 track의 다음 작업은 `COST-01`이다. `CTRL-09`의 observation method/freshness와 `CTRL-10`의
usage/cost projection 입력 계약 없이 collector schema나 통화 단위 비용을 추정해서 구현하지
않는다.

## P4 — End-to-End Workflow Trace

목표: 한 transport 요청에서 여러 tool call과 retry로 이어지는 사용자 workflow를 민감 입력
없이 추적한다.

현재 server-generated MCP HTTP request ID는 하나의 POST lifecycle과 그 안의 tool/query만
연결한다. Client가 여러 POST와 model reasoning을 잇는 workflow trace는 아니므로 아래 항목을
대체하지 않는다.

- [ ] `TRACE-01` client 제공 trace ID의 문자 집합, 길이, 생성·신뢰 경계와 충돌 정책을
  decision record로 확정한다.
- [ ] `TRACE-02` 검증된 trace ID를 HTTP/MCP context, tool lifecycle과 query audit에
  전달하고 server-generated call/query ID와 연결한다.
- [ ] `TRACE-03` trace ID를 metric label로 사용하지 않으며 question, SQL, token과 비인가
  source가 log에 유입되지 않는지 검증한다.
- [ ] `TRACE-04` 병렬 tool call, revision retry, disconnect와 multi-replica 호출에서 correlation
  누락·혼선이 없는지 end-to-end로 검증한다.

## Explicit Non-Goals

- 이전 MCP handshake 또는 protocol version 지원, legacy cancellation, stateful compatibility
  session은 backlog에 두지 않는다.
- Source onboarding Skill이나 prompt를 reader privilege, authorization, SQL validation 또는
  query budget의 enforcement boundary로 사용하지 않는다.
- Production source YAML을 Control DB와 병렬 desired state로 관리하거나 publish 결과를 Git에
  자동 write-back하지 않는다. Repository source/onboarding YAML은 bootstrap/fixture만 담당한다.
- 조회용 MCP에 source publish, credential rotation, rollback 또는 deactivate tool을 추가하지
  않는다. 필요성이 검증되면 별도 threat model과 API decision부터 작성한다.
- 두 replica를 합친 distributed global query quota나 load balancer 선택은 현재 soak가
  보장하지 않는다. 필요하면 connection budget과 routing decision을 먼저 추가한다.
- Planner cost, gateway latency 또는 cloud vCPU 가격만으로 query별 통화 비용을 가장하지
  않는다.
