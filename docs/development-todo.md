# Active Development TODO

Status: Active

이 문서는 완료된 production baseline 이후에 **아직 끝나지 않은 작업만** 우선순위대로
관리한다. 완료 이력과 실행 증거는
[implementation roadmap](implementation-roadmap.md#14-post-baseline-completion-ledger-and-active-development)에
옮긴다. 따라서 이 문서에는 `[x]`가 없어야 한다.

## Management Rules

- 전역 순서는 위에서 아래다. 더 높은 priority의 항목이 열려 있으면 낮은 track은 설계와
  조사만 할 수 있고 구현을 먼저 시작하지 않는다.
- Lower-track의 `read-only prework`는 현재 code/test/운영 근거 조사, 선택지 비교와
  초안 작성까지만 뜻한다. Start gate 전에는 해당 item을 공식적으로 시작하거나
  완료했다고 기록하지 않고, accepted baseline을 바꾸거나 code/schema/config/
  contract 의미를 변경하지 않는다.
- 항목을 완료하면 같은 변경에서 이 문서에서 제거하고 roadmap의 post-baseline completion
  ledger에 결과와 실행 증거를 기록한다. 완료 ID는 재사용하지 않는다.
- 각 track은 `Primary module`, `Direct consumers`, `Affected providers/verifiers`,
  `Contract baseline`, `Approval gate`, `Single writer`, `Start gate`, `Verification`을 명시한다.
  `Direct consumers`는 결과를 쓰는 쪽, `Affected providers/verifiers`는 필요한 입력·계약을
  제공하거나 결과를 검증하는 쪽이다. Agent는 이 범위부터 읽고 다른 module로 넘어가는
  complete end-to-end slice만 추가로 읽는다.
- TODO에 항목을 적는 것은 module contract 변경 승인이 아니다. Public Python/wire/persisted
  schema, lifecycle 또는 정책 의미가 바뀌면 정확한 제안과 영향을 사용자에게 제시하고 별도
  승인을 받은 뒤 시작한다.
- 이 TODO, 실행 wave 또는 결정 guide를 포함한 **plan 승인은 contract 선택
  승인이 아니다**. 사용자가 implementation-ready 선택 ID와 영향 범위를 명시해야
  해당 contract 변경을 시작할 수 있다.
- 공통 contract, shared transition file과 migration은 coordinating agent가 single-writer로
  직렬화한다. 고정된 계약을 소비하는 서로 다른 module implementation만 병렬화한다.
- 비밀, 질문 원문, SQL text와 parameter는 비용·trace 수집에도 저장하지 않는다.
- MCP는 현재 지원 버전 `2026-07-28`만 받는다. 이전 handshake와 protocol version을 위한
  compatibility branch는 만들지 않으며 version 변경은 명시적인 upgrade 작업으로 처리한다.

## P0 — Runtime Startup Failure Cleanup

목표: MCP child application의 lifespan 진입 중 실패해도 parent composition이 그 전에 만든
resource를 역순으로 정리해 connection pool이나 background task를 남기지 않게 한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Runtime |
| Direct consumers | Runtime server/process lifecycle과 readiness; 새 cross-module consumer는 없음 |
| Affected providers/verifiers | Delivery MCP child lifespan과 이미 조립된 Control Plane/Metadata/Guarded Query resource의 cleanup capability; Runtime/Delivery failure-path tests |
| Contract baseline | [Runtime startup contract](modules/runtime/README.md#startup-contract)은 child `enter` 실패 시 역순 cleanup을 현재 보장하지 않는 debt로 명시한다. |
| Approval gate | Exactly-once 역순 cleanup과 원래 startup error 보존은 새 lifecycle 보장이다. 선택지와 provider 영향을 사용자에게 제시해 승인받기 전에는 구현하지 않는다. Method, grace, startup/shutdown 순서나 public health 의미까지 바뀌면 그 범위도 별도 승인받는다. |
| Single writer | Runtime owner가 `app.py` lifespan symbol을 수정하고 Delivery owner는 route/wire 부분을 동시 편집하지 않는다. |
| Start gate | 계약 선택 승인 전 blocked; 승인되면 repository 전체의 다음 구현 작업 |
| Verification | Runtime/Delivery focused failure-path test, `ruff`, `mypy`, root `pytest`; DB resource 경계를 건드리면 integration test |

- [ ] `RTSAFE-01` MCP child lifespan의 `__aenter__`가 실패하면 진입하지 못한 child에
  `__aexit__`를 호출하지 않고, parent가 그 전에 만든 reload task/pool/service resource만
  정확히 한 번 역순 정리하며 원래 startup error를 보존하는 계약·회귀 테스트와 구현을
  완료한다. Child의 partial-enter cleanup은 child lifespan 자체의 책임으로 유지한다.

## P0.5 — Module Contract Hardening

목표: 문서로만 막고 있는 hidden dependency, read/write capability, Runtime lifecycle,
shared snapshot과 offline composition 경계를 승인된 공개 contract로 강제한다. 선택지와
정확한 영향은 [module contract decision guide](module-contract-decision-guide.md)를 따른다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | 권장 A choice의 ID별 contract provider: `MOD-04` Control Plane, `MOD-05` Source Catalog, `MOD-06` Guarded Query/Metadata, `MOD-07` Source Catalog/Metadata, `MOD-08` Assurance |
| Direct consumers | `MOD-04` Delivery, `MOD-05` ordinary source readers/Control projector, `MOD-06` Runtime/Control reloader, `MOD-07` source/metadata snapshot consumers, `MOD-08` offline CLI entrypoint |
| Affected providers/verifiers | Delivery, Runtime, Control Plane, Metadata, Guarded Query, Source Catalog과 Assurance의 각 focused/contract test |
| Contract baseline | ADR 0018, 현재 module index/README와 decision guide에 적은 현재 동작·공통 불변조건 |
| Approval gate | `D1-A`, `D2-A`, `D4-A`, `D3-A`, `D5-A`는 모두 권장 초안이며 미승인이다. 각 exact A choice를 사용자가 명시적으로 승인하기 전에는 해당 ID를 구현하지 않는다. `D1`/`D5`의 B/C와 `D2`/`D3`/`D4`의 B는 구현 전 exact follow-up contract를 다시 승인받아야 한다. `D2-C`/`D3-C`/`D4-C`는 현재 상태와 debt를 유지해 구현할 내용이 없다. 이를 선택하면 사용자가 해당 P0.5 ID의 bypass/deferral과 P1 Start gate를 별도로 재결정해야 한다. |
| Single writer | Coordinating agent가 `MOD-04` → `MOD-05` → `MOD-06` → `MOD-07` → `MOD-08` 순서로 contract/provider를 직렬화한다. Provider baseline 확정 뒤 서로 다른 consumer implementation만 병렬화한다. |
| Start gate | `RTSAFE-01` 완료 후, 각 exact choice 승인을 받은 ID만 위 순서로 시작한다. Lower-track read-only prework는 지금 가능하지만 item 시작이나 baseline 변경은 아니다. |
| Verification | Decision guide의 각 exact contract test, provider와 모든 직접 consumer focused test, `ruff`, `mypy`, root `pytest`, DB 경계의 integration test |

- [ ] `MOD-04` 사용자가 `D1-A` 또는 후속 exact contract를 승인한 뒤 Delivery의
  Control Plane persistence/Assurance DTO hidden import를 승인된 public contract/dependency 방향으로
  회수하고 external wire/storage 의미 불변을 검증한다.
- [ ] `MOD-05` 사용자가 `D2-A` 또는 후속 exact contract를 승인한 뒤 ordinary source
  consumer와 Control projector의 read/write capability를 승인된 type 경계로 분리하고 runtime
  output 불변을 검증한다.
- [ ] `MOD-06` 사용자가 `D4-A` 또는 후속 exact contract를 승인한 뒤 Guarded Query와
  Metadata가 Runtime에 필요한 lifecycle capability를 승인된 Protocol로 제공하고 누락 adapter가
  ready 전 fail-closed하는지 검증한다.
- [ ] `MOD-07` 사용자가 `D3-A` 또는 후속 exact contract를 승인한 뒤 Source Catalog과
  Metadata의 public snapshot을 승인된 immutability 경계로 전환하고 serialized JSON, revision과
  verified hash 불변을 검증한다.
- [ ] `MOD-08` 사용자가 `D5-A` 또는 후속 exact contract를 승인한 뒤 Assurance
  offline CLI concrete composition을 승인된 owner/경계로 격리하고 command/output/exit와 Guarded Query
  safety path 불변을 검증한다.

## P1 — Centralized Source Management Plane

목표: Production managed source의 authority, replica 상태, 규모와 비용 projection을 Control DB와
하나의 admin surface에서 관리한다. `CTRL-01`~`CTRL-05`의 완료 이력은 roadmap ledger에 있고,
현재 track은 `CTRL-06`부터 시작한다. 상세 목표는
[source management plane](source-management-plane.md), authority 경계는
[ADR 0016](decisions/0016-centralized-source-management-plane.md), shared access/resource tier는
[ADR 0017](decisions/0017-shared-source-access-and-resource-tier.md)을 따른다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Control Plane |
| Direct consumers | Delivery admin API와 Runtime authority/recovery composition |
| Affected providers/verifiers | Runtime replica heartbeat/reloader, Source Catalog/Metadata/Guarded Query observation signal과 Assurance integration/recovery acceptance |
| Contract baseline | `CTRL-01`~`CTRL-05` ledger와 ADR 0016/0017, 현재 Control Plane/Delivery/Runtime module contract |
| Approval gate | `CTRL-06`~`CTRL-08`의 새 Control DB schema, observation shape, freshness와 admin response 의미는 구현 전 정확한 계약 승인이 필요하다. `CTRL-09`가 기존 backup/restore 절차만 재현하면 새 승인이 없지만 schema·retention·key recovery 의미를 바꾸면 승인받는다. |
| Single writer | Control Plane owner가 schema/contract를 직렬화하고 baseline 확정 뒤 Runtime reporter와 Delivery projection을 병렬화한다. |
| Start gate | `RTSAFE-01`과 `MOD-04`~`MOD-08` 완료 뒤 `CTRL-06`부터 순서대로 진행 |
| Verification | Provider와 직접 consumer contract test, root gate, Control DB integration, migration/rollback/recovery와 scoped verification record |

- [ ] `CTRL-06` Replica별 applied generation/state version/metadata revision/health heartbeat를
  수집해 desired/applied drift와 freshness를 management API에서 조회한다.
- [ ] `CTRL-07` Representative record-volume metric, estimated rows, table/index/storage bytes와
  gateway usage를 `source_id + budget_profile + metadata_revision + time bucket` 기준으로 bounded
  수집한다. Method/definition revision/time/freshness를 남기고 일반 view의 무제한 `COUNT(*)`,
  caller/tenant 비용 dimension과 별도 `cost_tier`를 금지한다.
- [ ] `CTRL-08` Usage/cost projection의
  `not_configured|pending|available|stale|unavailable` 상태, last-attempt time과 bounded reason을
  구현해 missing/failed 값을 0으로 표시하지 않는다. DB-native/provider connector 없이도
  완료할 수 있게 하고 cardinality/retention 계약을 검증한다.
- [ ] `CTRL-09` 전체 authority table의 backup/restore와 retention, encryption-key recovery,
  zero-bootstrap 복구 및 multi-replica management-plane release acceptance를 재현한다.

이 track에서는 다단계 RBAC, caller grant storage, user/organization별 tier·quota와 AI production
executor를 구현하지 않는다.

## P2 — Source Onboarding Skill

Status: Accepted planning baseline; `SKILL-01`/`SKILL-02` release reviews pending; implementation pending

목표: 기존 no-restart source onboarding 계약을 Codex가 반복 가능한 plan과 관리자 handoff로
정리한다. V1은 plan-only이며 credential을 읽거나 admin API를 호출하지 않는다. 상세 설계와
단계별 gate는 [source onboarding Skill plan](source-onboarding-skill-plan.md)에서 관리한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Source Catalog |
| Direct consumers | DB owner와 Query Man admin/operator handoff workflow; production module runtime dependency는 없음 |
| Affected providers/verifiers | Source onboarding/config 계약, Control Plane 공개 admin 계약, Delivery의 기존 query Skill negative-routing 경계와 Assurance evaluation |
| Contract baseline | Source onboarding/runbook, ADR 0016/0017과 accepted plan-only Skill boundary |
| Approval gate | 현재 accepted plan의 구현은 가능하다. Output schema, trigger/scope, secret·mutation 경계 또는 production authority를 바꾸면 사용자 승인을 먼저 받는다. |
| Single writer | Source Catalog owner가 Skill과 plan을 쓰고 shared onboarding/config 문서는 coordinating agent가 직렬화한다. |
| Start gate | `RTSAFE-*`, `MOD-04`~`MOD-08`과 `CTRL-*`가 닫힌 뒤 `SKILL-01`부터 진행; 아래의 “다음”은 track-local 순서다. |
| Verification | Positive/negative/adversarial Skill eval, mutation 0 증거, 관련 onboarding/Delivery/Assurance 회귀와 root gate |

- [ ] `SKILL-01` Skill scope, repository 위치, request/trigger 경계와 manual runbook·query
  Skill의 책임 분리를 설계 review로 확정한다.
- [ ] `SKILL-02` 입력/evidence/output schema, DB owner와 Query Man admin handoff, secret·mutation
  금지, shared-access 영향과 source `budget_profile` 선택을 threat review한다.
- [ ] `SKILL-03` 최소 repository Skill을 구현하고 source onboarding, extension checklist,
  budget/cost 문서를 progressive-disclosure reference로 연결한다.
- [ ] `SKILL-04` Positive/negative/adversarial trigger와 credential·admin mutation 거부를
  realistic prompt 및 독립 forward test로 검증한다.
- [ ] `SKILL-05` `support-tickets` fixture 입력으로 L0 review plan, 누락 정보와 admin handoff를
  생성하되 repository/DB/Control DB/admin API mutation이 0건임을 검증한다.
- [ ] `SKILL-06` Skill validation, 관련 회귀, 운영 문서와 재현 증거를 완료한 뒤 기본
  onboarding planning workflow 채택 여부를 기록한다.

Production mutation executor는 이 track에 없다. 필요해지면 credential broker와 admin apply
계약을 별도 ADR에서 먼저 설계하고 승인받는다.

## P3 — Database-Native Cost Attribution

목표: 이미 적용 중인 hard limit에 더해 완료된 query의 실제 DB 자원 사용을 source/resource-tier
단위부터 측정한다. 통화 단위 청구액은 provider billing 자료가 있을 때만 별도 계산한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Control Plane |
| Direct consumers | Delivery admin projection과 operator workflow |
| Affected providers/verifiers | Guarded Query/Runtime usage signal, Source Catalog budget 의미, Metadata revision, `CTRL-07`/`CTRL-08` observation baseline과 Assurance acceptance |
| Contract baseline | ADR 0016/0017, `CTRL-07` observation method/freshness와 `CTRL-08` usage/cost state |
| Approval gate | Monitoring identity, collector/rollup schema, retention, status와 admin projection은 새 persisted/public 계약이므로 각 단계 구현 전 사용자 승인이 필요하다. |
| Single writer | Control Plane owner가 observation 계약을 먼저 확정하고 signal producer와 Delivery consumer는 이후 병렬화한다. |
| Start gate | `CTRL-07`/`CTRL-08` baseline과 앞선 priority 완료 뒤 `COST-01`부터 진행; 아래의 “다음”은 track-local 순서다. |
| Verification | 최소 권한 DB integration, reset/eviction/replica/cardinality 경계, redaction과 root gate |

- [ ] `COST-01` 대상 PostgreSQL의 monitoring identity, 최소 권한, 지원 extension과 reset/보존
  정책을 inventory하고 개발·production별 사용 가능 신호를 decision record로 확정한다.
- [ ] `COST-02` `pg_stat_statements` 기반 sample collector를 구현해 DB-native statement
  identity별 calls, execution time, rows, shared/local/temp block과 WAL 수치를 수집하되 SQL
  text와 parameter는 저장하지 않는다.
- [ ] `COST-03` DB-native aggregate를
  `source_id + budget_profile + metadata_revision + time bucket`의 bounded rollup으로 연결하고
  reset, eviction, replica 중복과 sampling 오차를 명시한다. PostgreSQL query ID와 gateway
  fingerprint의 정확한 대응이나 caller/tenant 비용 dimension을 만들지 않는다.
- [ ] `COST-04` Source/profile별 비용 급증 threshold, alert, retention과 admin 조회 계약을
  정의하고 public endpoint·metric label에 source를 노출하지 않는다.
- [ ] `COST-05` 실제 fixture에서 저비용·CPU·I/O·temp 사용 query를 구분하는 acceptance와
  provider 자료가 없거나 user/organization chargeback이 불가능할 때의 운영 판단 절차를
  문서화한다.

`CTRL-07`/`CTRL-08` 입력 계약 없이 collector schema나 통화 단위 비용을 추정해서 구현하지
않는다.

## P4 — End-to-End Workflow Trace

목표: 한 transport 요청에서 여러 tool call과 retry로 이어지는 사용자 workflow를 민감 입력
없이 추적한다. 현재 server-generated MCP HTTP request ID는 하나의 POST lifecycle만 연결하며
여러 POST와 model reasoning을 잇는 workflow trace는 아니다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Delivery |
| Direct consumers | Runtime operations와 Guarded Query audit |
| Affected providers/verifiers | 승인될 경우 client trace input, 현재 Delivery auth/HTTP/MCP context, Runtime operations sink와 Assurance end-to-end verification |
| Contract baseline | 현재 HTTP/MCP correlation, audit/redaction과 supported MCP protocol |
| Approval gate | Client trace input, trust/collision policy, public header/schema와 audit field는 새 wire/observability 계약이다. `TRACE-01`에서 선택지를 제시하고 사용자 승인을 받은 뒤 구현한다. |
| Single writer | Delivery owner가 wire 계약을 직렬화하고 baseline 확정 뒤 Runtime/Guarded Query propagation을 병렬화한다. |
| Start gate | 앞선 priority 완료 뒤 `TRACE-01` 조사·결정을 수행하고, 사용자 승인 뒤에만 `TRACE-02`~`TRACE-04`를 구현한다. |
| Verification | HTTP/MCP contract, redaction/cardinality, parallel/retry/disconnect/multi-replica end-to-end와 root gate |

- [ ] `TRACE-01` client 제공 trace ID의 문자 집합, 길이, 생성·신뢰 경계와 충돌 정책을
  decision record로 확정한다.
- [ ] `TRACE-02` 검증된 trace ID를 HTTP/MCP context, tool lifecycle과 query audit에
  전달하고 server-generated call/query ID와 연결한다.
- [ ] `TRACE-03` trace ID를 metric label로 사용하지 않으며 question, SQL, token과 비인가
  source가 log에 유입되지 않는지 검증한다.
- [ ] `TRACE-04` 병렬 tool call, revision retry, disconnect와 multi-replica 호출에서 correlation
  누락·혼선이 없는지 end-to-end로 검증한다.

## Approval-Gated Contract Work

Startup cleanup은 `RTSAFE-01`, hidden dependency/read-write/lifecycle/immutability/offline composition은
`MOD-04`~`MOD-08`이 순서와 완료 조건만 추적한다. 이 scheduling 또는 Wave 0
prework에 대한 승인은 [module contract decision guide](module-contract-decision-guide.md)의
`D0-A`~`D5-A` 계약 선택 승인이 아니다. 사용자가 각 exact choice와 공통 불변조건
범위를 명시적으로 승인하기 전에는 관련 code/schema/config/contract 문서의
의미를 변경하지 않는다.

## Explicit Non-Goals

- 이전 MCP handshake 또는 protocol version 지원, legacy cancellation, stateful compatibility
  session은 backlog에 두지 않는다.
- Source onboarding Skill이나 prompt를 reader privilege, authorization, SQL validation 또는
  query budget의 enforcement boundary로 사용하지 않는다.
- Production source YAML을 Control DB와 병렬 desired state로 관리하거나 publish 결과를 Git에
  자동 write-back하지 않는다. Repository source/onboarding YAML은 bootstrap/fixture만 담당한다.
- 조회용 MCP에 source publish, credential rotation, rollback 또는 deactivate tool을 추가하지
  않는다. 필요성이 검증되면 별도 threat model과 API decision부터 작성한다.
- 초기 운영에 별도 `cost_tier`, user/organization별 source ACL·budget override·quota·fairness,
  caller별 비용 dashboard나 chargeback ledger를 추가하지 않는다.
- 다단계 management RBAC, Control DB caller-grant import와 AI production mutation executor는
  실제 요구가 생기기 전까지 만들지 않는다.
- 두 replica를 합친 distributed global query quota나 load balancer 선택은 현재 soak가
  보장하지 않는다. 필요하면 connection budget과 routing decision을 먼저 추가한다.
- Planner cost, gateway latency 또는 cloud vCPU 가격만으로 query별 통화 비용을 가장하지
  않는다.
