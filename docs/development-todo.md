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

## P2.5 — Canonical Timestamptz Stability

목표: 같은 PostgreSQL instant가 reader session `TimeZone`과 무관하게 같은 public canonical value와
verified result hash를 갖게 한다. Repository 구현과 격리 acceptance는 끝났지만 protected production
inventory와 실제 배포·rollback 증거는 환경별 change record로 남겨야 한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Guarded Query |
| Direct consumers | Delivery public result, Assurance verified result hash와 Control Plane verified publish |
| Affected providers/verifiers | Source Catalog reader-session policy, Metadata catalog/revision, Runtime composition, production Control/source operator |
| Contract baseline | 승인된 [`TIME-01` 결정](decisions/0019-canonical-time-stability.md)과 완료된 `TIME-02`가 UTC-first reader policy, aware datetime UTC `+00:00`, policy v2와 새 metadata revision을 고정한다. Repository fixture 11개, UTC/서울/뉴욕·DST, old/new Control row 공존과 local coordinated cutover는 통과했다. |
| Approval gate | 사용자가 2026-08-25 `TIME-01`의 정확한 정책·영향·cutover·rollback을 승인했다. 그 범위의 production 전환은 승인됐지만 환경 권한과 stop condition 증거 없이 실행하지 않으며, 승인 범위를 넘는 의미 변경은 다시 승인받는다. |
| Single writer | Production operator가 coordinating agent와 함께 protected inventory, DB migration ref, fleet drain, 재발행과 rollback change record를 하나의 직렬 전환으로 관리한다. |
| Start gate | `TIME-01`과 `TIME-02`는 완료됐다. `TIME-03`은 production inventory·권한·backup·route가 제공된 환경에서만 진행한다. 완료하거나 사용자가 명시적으로 defer하기 전에는 `COST-01` 구현을 시작하지 않는다. |
| Verification | Managed current/rollback-preserved contract 전량 재실행·재발행, 실제 R1 migration ref, old connection 0, replica convergence, R2→R1 rollback과 environment change record |

- [ ] `TIME-03` Repository에서 이미 검증한 전환 절차를 실제 managed environment에 적용해
  current/rollback-preserved verified contract 전체를 새 revision/hash로 재실행·재발행하고,
  R1 migration ref·fleet drain·route·rollback을 환경별 change record로 증명한다.

## P3 — Database-Native Cost Attribution

목표: 이미 적용 중인 hard limit에 더해 완료된 query의 실제 DB 자원 사용을 source/resource-tier
단위부터 측정한다. 통화 단위 청구액은 provider billing 자료가 있을 때만 별도 계산한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Control Plane |
| Direct consumers | Delivery admin projection과 operator workflow |
| Affected providers/verifiers | Guarded Query/Runtime usage signal, Source Catalog budget 의미, Metadata revision, 완료된 `CTRL-07` observation baseline과 `CTRL-08` projection, Assurance acceptance |
| Contract baseline | [ADR 0016](decisions/0016-centralized-source-management-plane.md), [ADR 0017](decisions/0017-shared-source-access-and-resource-tier.md)와 [source management plane](source-management-plane.md), 완료된 `CTRL-07A` observation method/freshness/logical retention과 `CTRL-08` usage/cost state |
| Approval gate | Monitoring identity, collector/rollup schema, retention, status와 admin projection은 새 persisted/public 계약이므로 각 단계 구현 전 사용자 승인이 필요하다. |
| Single writer | Control Plane owner가 observation 계약을 먼저 확정하고 signal producer와 Delivery consumer는 이후 병렬화한다. |
| Start gate | 완료된 `CTRL-07` baseline과 `CTRL-08` projection 뒤에도 열린 `TIME-03`이 우선이다. 이를 완료하거나 사용자가 명시적으로 defer한 뒤 `COST-01`의 정확한 monitoring 계약과 영향 범위를 제시하고 별도 승인받는다. 아래의 “다음”은 track-local 순서다. |
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

완료된 `CTRL-07` 입력과 `CTRL-08` projection 계약을 바꾸거나, 별도 승인 없이 collector schema나
통화 단위 비용을 추정해서 구현하지 않는다.

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

Startup cleanup `RTSAFE-01`, hidden dependency `MOD-04`, read/write capability `MOD-05`,
lifecycle Protocol `MOD-06`, deep immutability `MOD-07`과 offline composition `MOD-08`은 모두
완료되어 roadmap ledger로 이동했다. 2026-08-24 사용자는
[module contract decision guide](module-contract-decision-guide.md)의 `D0-A`~`D5-A`와 공통
불변조건을 명시적으로 승인했다. 이 baseline을 넘어서는 의미 변경은 다시 승인받는다.

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
