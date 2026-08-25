# Active Development TODO

Status: Active

이 문서는 완료된 production baseline 이후에 **아직 끝나지 않은 작업만** 우선순위대로
관리한다. 완료 이력과 실행 증거는
[implementation roadmap](implementation-roadmap.md#14-post-baseline-completion-ledger-and-active-development)에
옮긴다. 따라서 이 문서에는 `[x]`가 없어야 한다.

## Management Rules

- 전역 순서는 위에서 아래다. 더 높은 priority의 항목이 열려 있으면 낮은 track은 설계와
  조사만 할 수 있고 구현을 먼저 시작하지 않는다.
- 단, higher track의 protected-environment 완료 항목이 lower track의 확정된 repository baseline을
  선행 조건으로 명시한 경우 그 운영 항목은 prerequisite repository 구현을 막지 않고 최종 production
  acceptance를 막는다. 이 예외와 선후 관계는 양쪽 Start gate에 모두 적어야 한다. 현재는 `RLS-03`과
  `ENC-01`~`TIME-03` 관계에만 적용한다.
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

## P2.3 — RLS Base-Policy Drift Attestation

목표: `tenant_isolation=rls` source의 authenticated tenant query가 private base relation의 RLS flag나
policy 변경 뒤에도 다른 tenant 행을 성공 응답으로 반환하지 않게 한다. 이 항목은 재현된 authorization
경계이므로 아래 encoding 작업보다 먼저 결정한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Metadata |
| Direct consumers | Guarded Query RLS admission과 all-managed transition cancel, Metadata Catalog load/resource-observation lifecycle, Control Plane source publish/apply, Runtime invalidator order, Delivery HTTP/MCP result/error |
| Affected providers/verifiers | Source Catalog reader/session-policy/profile identity impact, Assurance cross-database security와 all-managed transition acceptance. Recursive dependency/catalog introspection은 Primary owner인 Metadata가 담당한다. |
| Contract baseline | Accepted ADR 0014는 restricted reader, transaction-local trusted tenant, `row_security=on`과 public `security_invoker=true` view를 강제한다. 현재 snapshot/revision은 private dependency의 `relrowsecurity`와 `pg_policy`를 담지 않아 policy를 `USING (true)`로 바꾸거나 RLS를 disable해도 같은 revision 아래 cross-tenant row가 성공한다. [RLS policy drift finding](verification/2026-08-26-rls-policy-drift.md)이 PostgreSQL 18 disposable DB에서 독립 재현했다. |
| Approval gate | [Proposed ADR 0024](decisions/0024-rls-policy-drift-attestation.md)의 `RLS-01-A`는 ordinary invoker-view/table, ENABLE+FORCE, 정확히 하나의 PUBLIC 또는 exact-reader SELECT tenant equality, exact `_RETURN`과 policy `pg_depend`/database-scoped `pg_shdepend`, RLS pool startup client UTF8 및 PostgreSQL-18/server/client UTF8·strict text admission, hidden-chain-free deterministic zero-root/root-count/graph marker와 marker 없는 root-list race 503, RLS-only reader-setting deterministic/transient 분류, history-decode/offline-serving/live-attestation 분리, custom/partition 거부, canonical bound, lock-first query order, snapshot/revision v2, error/no-stale, 모든 managed source의 exact-profile fence와 Query-first Query/Catalog active-lease fixed drain 및 pool-cache-before-profile generation apply, existing-operator tenant mapping과 standalone v2 cutover/rollback을 묶은 implementation-ready 제안이다. Non-RLS active query도 source transition 때 unavailable로 정리될 수 있지만 RLS-only UTF8/graph/v2/reader-setting 분류를 non-RLS로 넓히지 않는다. 이 persisted/Python/policy/lifecycle 계약과 non-UTF8 RLS stop/re-onboarding, residual trusted-admin 경계를 정확히 승인한 뒤 구현한다. RLS-only UTF8 admission은 non-RLS/result/source-semantics를 바꾸는 broader `ENC-01` 승인이 아니며 combined direct-v3는 `ENC-01` exact 승인/`ENC-02` baseline 뒤에만 활성화한다. 일반적인 진행/승인이나 ID만으로는 선택 승인이 아니다. B는 별도 exact restatement가 필요하고 C는 production-blocking defer다. |
| Single writer | Coordinating agent가 Source Catalog의 RLS-only startup constant/no-SQL connection policy를 먼저 직렬화하고, Metadata policy identity provider와 snapshot/revision baseline을 그다음 동결한다. Guarded Query same-transaction verifier와 source transition fence/drain을 이어서 고정한 뒤 Control/Runtime/Assurance consumer를 갱신한다. Shared reader policy/catalog/query/source-admin/test/doc은 coordinating agent만 편집한다. |
| Start gate | Product/production state를 바꾸지 않은 isolated disposable-DB reproduction, snapshot-before-lock race probe, strict xfail sentinel, UTF8 libc/ICU/builtin 및 SQL_ASCII 5-database policy/dependency/text probe와 exact proposal 작성은 완료됐다. 추가 probe와 implementation-readiness audit에서 찾은 client-encoding별 same-Python-name/different-relation collision, policy shared-dependency, deterministic/transient error 분류, history/serving 및 pool/cache apply-order 공백을 proposal에 보완했지만 `RLS-01-A`의 정확한 사용자 승인은 아직 없으므로 `RLS-01`/`RLS-02`는 모두 open이고 제품 코드·schema/config·accepted contract는 바꾸지 않는다. Protected inventory 권한이 제공되면 읽기 전용으로 먼저 확인하고 검증되지 않은 RLS source의 route 상태를 사용자와 결정한다. `RLS-03` inventory·mutation freeze는 앞서 준비할 수 있다. Standalone v2 current/rollback 재발행·route drain은 승인·완료된 `RLS-02` 뒤 별도 environment 승인/change record로 진행할 수 있고 `ENC-02`를 기다리지 않는다. Combined direct-v3를 선택한 경우에만 `ENC-02` 최종 baseline을 기다려 `TIME-03` protected change record에서 함께 증명한다. |
| Verification | Policy-expression 및 RLS enable/force/owner/role drift, nested dependency와 custom function/operator, exact policy normal/shared dependency, stable zero/root-bound marker와 transient root-list race, UTF8 libc/ICU/builtin positive와 SQL_ASCII/non-UTF8 server/client negative, same-name encoding collision과 drop/recreate, snapshot-before-lock old-catalog/new-policy race와 lock-first concurrent DDL, cold/warm cache와 stale 금지, deterministic/transient direct cause/context None 및 hidden/password/raw-driver log canary 비공개, exact-profile transition/queued-active Query와 active Catalog lease fixed drain, terminal result fence와 cancellation-owner race, pre-fence 대 post-fence/pre-commit 대 registry-commit/post-commit bookkeeping·probe failure, observation `RESOURCE_READ_FAILED` 대 external cancellation, tenant 병렬/pool reset, bounded non-disclosure error, managed current/rollback reissue와 production inventory/cutover/rollback |

위 `RLS-01` approval gate는 repository contract/code/disposable fixture/operator artifact 구현용이다.
Protected inventory/freeze/DDL/reissue/fleet·route cutover/rollback 실행은 별도 `RLS-03`/`TIME-03` 환경
승인과 access/change record가 필요하다.

- [ ] `RLS-01` Proposed ADR 0024의 `RLS-01-A` exact recursive policy/dependency/UTF8 admission,
  deterministic-vs-root-race error secrecy, history/offline/live split, lock/snapshot, all-managed
  exact-profile transition과 cutover 계약을 사용자가 정확히 승인해 decision을 확정한다.
- [ ] `RLS-02` 승인된 Source Catalog connection provider, Metadata attestation/snapshot/Catalog lifecycle,
  Guarded Query lock/fence와 Control/Runtime transition을 구현하고 Delivery/Assurance consumer 및 strict
  xfail을 fail-closed passing regression으로 전환해 전체 계약을 검증한다.
- [ ] `RLS-03` Protected environment의 모든 RLS source를 read-only inventory하고 mutation freeze,
  current/rollback reissue, unverified route drain과 rollback change record를 남긴다.

## P2.4 — Lossless Scalar Encoding, Reader Formatting And Result Types

목표: PostgreSQL interval calendar-month, time 24시, JSON/JSONB fractional/duplicate-key, SQL
encoding과 array lower bound를 public row/verified hash까지 조용한 손실 없이 전달하거나 손실 전에
명시적으로 fail-closed한다. Empty multirange/range-array의 ordinary array 오인도 닫고, finite-float,
date/string/NULL/array-literal/timezone abbreviation과 database/column collation 의미가 role/source
default 및 live drift와 무관하게 결정적이어야 한다. Driver가 int/tuple/string/list로 평탄화한
record/composite와 unknown/non-allowlisted result OID도 supported SQL type으로 오인하지 않아야 한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Guarded Query |
| Direct consumers | Delivery public result, Assurance verified result hash와 Control Plane verified publish |
| Affected providers/verifiers | Source Catalog deterministic reader settings, Metadata revision material, Runtime coordinated cutover와 cross-database Assurance acceptance |
| Contract baseline | psycopg default loader가 month-bearing interval과 fractional JSON numeric을 평탄화하고 duplicate-key JSON의 앞 key를 버리며 time 24시를 decode하지 못한다. Interval infinity는 zero와 같은 `timedelta`/public hash로 합쳐지고 Python 범위 밖 date/timestamp와 4,301자리 JSON integer는 decode에 실패한다. SQL_ASCII text는 bytea와 같은 bytes/Base64가 되고 collation·timezone abbreviation·custom operator binding은 같은 revision SQL 의미를 바꾼다. Empty unsupported collection/array lower bound와 record/composite 및 `oid/name`·array/unknown OID identity도 Python 기본형에서 사라진다. [`DBEDGE-02`~`DBEDGE-05`](verification/2026-08-25-source-database-corners.md)가 이를 실제 PostgreSQL 18 disposable DB에서 재현했다. Infinity date, range와 nonempty multirange/range-array는 비공개 `QUERY_UNAVAILABLE` 후 rollback/recovery한다. |
| Approval gate | Loader, UTF8 source/client, reader semantic settings/fingerprint, collation snapshot/revision material, result OID allowlist, array type/lower-bound policy, canonical row, SQL policy/metadata revision과 verified migration은 persisted/public/policy 계약 변경이다. [ADR 0020](decisions/0020-lossless-interval-and-json-numeric-encoding.md)의 A는 fingerprint byte schema/bounds, private nested-view definition, direct type edge·declared/custom domain pre-erasure rejection, explicit COLLATE/active versionless rejection, provider/Protocol, RLS v2 shape/semantics를 fresh current-policy v3 attestation으로 누적하는 snapshot codec v1/v2/v3, exact result/error/policy golden, migration/rollback과 named-timezone·provider-artifact·custom procedure/operator residual limitation까지 묶은 implementation-ready 제안이다. 아직 사용자가 이 exact 범위를 명시적으로 승인하지 않았다. B는 별도 exact restatement가 필요하고 C는 production completion을 block하는 defer라 완료 선택이 아니다. 일반적인 진행/승인이나 ID만으로 구현하지 않는다. |
| Single writer | Coordinating agent가 symbol phase별로 Guarded Query immutable result/SQL-policy descriptor provider → Source Catalog shared `reader_policy.py` → Metadata fingerprint/snapshot codec/revision provider → Guarded Query result-cursor loader/encoder·fingerprint consumer 순으로 baseline을 직렬화한다. 각 provider baseline이 확정된 뒤 서로 다른 Delivery/Control Plane/Runtime/Assurance consumer 구현·검증만 병렬화하고 shared transition test/doc은 coordinating agent가 계속 single-writer로 다룬다. Guarded Query module을 두 agent가 동시에 쓰는 뜻이 아니며, descriptor provider commit 후 executor consumer phase를 같은 owner가 이어서 수행한다. |
| Start gate | `DBEDGE-02`~`DBEDGE-05` reproduction과 선택지 작성은 완료됐다. 최우선 `RLS-01`~`RLS-02` security boundary를 먼저 확정·해결한 뒤 `ENC-01`의 정확한 사용자 선택으로 간다. Protected RLS inventory/cutover인 `RLS-03`은 repository encoding 구현을 막지는 않지만 production acceptance 전에 완료해야 한다. 승인 전 loader/setting/encoder/source-semantics snapshot/revision/hash를 바꾸지 않는다. |
| Verification | Month/sign/day/subsecond interval, time/timetz 24시, large/scale/exponent/nested/duplicate JSON, UTF8/SQL_ASCII, timezone abbreviation/collation fingerprint, direct/whole-relation domain pre-erasure rejection, allowed/unknown result OID와 named composite, empty/nonempty unsupported collection, 1/non-1 lower-bound array, noncanonical defaults, managed inventory, cursor-only loader와 EXPLAIN 비회귀, pool reset, byte/hash, stale-token rejection, full verified reissue와 rollback |

- [ ] `ENC-01` ADR 0020의 exact lossless A, 별도 restatement가 필요한 손실 전 거부 B 또는 production
  미완료 defer C 중 canonical contract와 provider/consumer·migration·residual limitation 영향을 사용자의
  명시적 결정으로 확정한다.
- [ ] `ENC-02` 승인된 A/B를 구현하고 policy/revision/verified migration과 rollback을 검증한다.
  C를 선택하면 runtime guard가 없으므로 `ENC-02`/`TIME-03`과 production acceptance를 block하고 open
  defer로 유지한다.

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
| Start gate | `TIME-01`과 `TIME-02`는 완료됐다. `ENC-01` 결정과 `ENC-02`가 완료된 final encoding baseline에서 `TIME-03`을 수행한다. Standalone v2 `RLS-03`을 앞서 완료했다면 captured v2 current/rollback과 그 change record를 pre-ENC security baseline으로 사용하고, combined direct-v3를 선택했다면 승인·완료된 `RLS-02` 뒤 같은 protected change record에서 `RLS-03` inventory·mutation freeze·v3 current/rollback reissue·route drain을 먼저 완료하거나 함께 원자적으로 증명한다. Production inventory·권한·backup·route가 제공되어야 하며, 완료하거나 사용자가 명시적으로 defer하기 전에는 `COST-01` 구현을 시작하지 않는다. |
| Verification | Managed current/rollback-preserved contract 전량 v3 재실행·재발행, 실제 R1 및 RLS source-policy migration ref/checksum, old connection 0, replica convergence, current-v3→verified-rollback-v3 functional rollback, captured pre-ENC binary rollback에서 RLS deactivate/unroute와 environment change record |

- [ ] `TIME-03` Repository에서 이미 검증한 전환 절차를 실제 managed environment에 적용해
  current/rollback-preserved verified contract 전체를 v3 revision/hash로 재실행·재발행하고,
  R1/RLS migration ref·fleet drain·route, verified v3 functional rollback과 pre-ENC binary rollback 시
  RLS deactivate/unroute를 환경별 change record로 증명한다.

## P3 — Database-Native Cost Attribution

목표: 이미 적용 중인 hard limit에 더해 target reader role의 `pg_stat_statements` aggregate를
source/resource-tier 단위에서 관측한다. 이는 auxiliary statement와 같은 role의 외부 사용도 포함할 수
있어 Query Man business query별 CPU/비용이 아니다. 통화 단위 청구액은 provider billing 자료가 있을
때만 별도 계산한다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Control Plane |
| Direct consumers | Delivery admin projection과 operator workflow |
| Affected providers/verifiers | Runtime collector/composition, Source Catalog budget 의미, Metadata revision, 완료된 `CTRL-07` observation baseline과 `CTRL-08` projection, Assurance acceptance. Guarded Query의 reader-role workload는 관측 대상일 뿐 새 signal API provider가 아니다. |
| Contract baseline | [ADR 0016](decisions/0016-centralized-source-management-plane.md), [ADR 0017](decisions/0017-shared-source-access-and-resource-tier.md)와 [source management plane](source-management-plane.md), 완료된 `CTRL-07A` observation method/freshness/logical retention과 `CTRL-08` usage/cost state |
| Approval gate | Monitoring identity, collector/rollup schema, retention, status와 admin projection은 새 persisted/public 계약이다. Read-only prework인 [proposed ADR 0021](decisions/0021-database-native-cost-attribution.md)의 `COST-01-A|B|C` 중 A는 base collector/rollup/projection implementation-ready 제안, C는 exact defer, B는 direction-only다. A 구현 또는 C defer는 exact 범위를 사용자가 승인해야 하며 B는 lifecycle/wire/persistence/rollback을 다시 명시한 별도 승인이 먼저다. `COST-04`는 별도 [proposed ADR 0023](decisions/0023-database-native-usage-spike-alert.md)의 `COST-04-A|B|C`와 base evidence gate를 따르며 base A 승인은 alert 계약을 포함하지 않는다. ID 선택이나 포괄적 승인은 계약 승인이 아니다. |
| Single writer | Control Plane owner가 observation 계약을 먼저 확정하고 signal producer와 Delivery consumer는 이후 병렬화한다. |
| Start gate | 완료된 `CTRL-07` baseline과 `CTRL-08` projection 뒤에도 열린 `TIME-03`이 우선이다. 이를 완료하거나 사용자가 명시적으로 defer한 뒤 proposed ADR 0021의 정확한 monitoring 계약과 영향 범위를 별도 승인받는다. 현재 선택지 초안은 lower-track read-only prework일 뿐 `COST-01` 시작/완료가 아니다. 아래의 “다음”은 track-local 순서다. |
| Verification | 최소 권한 DB integration, reset/server-deallocation/entry/replica/cardinality 경계, redaction과 root gate |

- [ ] `COST-01` 대상 PostgreSQL의 monitoring identity, 최소 권한, 지원 extension과 reset/보존
  정책을 inventory하고 개발·production별 사용 가능 신호를 decision record로 확정한다.
- [ ] `COST-02` `pg_stat_statements` 기반 sample collector를 구현해 DB-native statement
  identity별 calls, execution time, rows, shared/local/temp block과 WAL 수치를 수집하되 SQL
  text와 parameter는 저장하지 않는다.
- [ ] `COST-03` DB-native aggregate를
  `source_id + budget_profile + metadata_revision + definition_revision + time bucket`의 bounded
  rollup으로 연결하고
  reset, server-wide deallocation/entry disappearance, replica 중복과 sampling 오차를 명시한다. PostgreSQL query ID와 gateway
  fingerprint의 정확한 대응이나 caller/tenant 비용 dimension을 만들지 않는다.
- [ ] `COST-04` Source/profile별 DB-native usage 급증 threshold, alert-event retention과 admin 조회 계약을
  정의하고 query-facing/non-admin public endpoint·metric label에 source를 노출하지 않는다. Operator-only
  `/admin/sources/{source_id}/...` path의 source ID는 이 금지에 포함되지 않는다. Proposed ADR 0021의
  base A 승인만으로 시작하지 않고 base explicit-zero/accepted-sample/identity evidence 뒤 proposed
  ADR 0023의 key/window/baseline/missing·stale/hysteresis/cooldown, lifecycle/polling delivery/90일
  logical event retention/redaction addendum를 사용자에게 exact 승인받는다. `accepted_samples>=10`은
  whole-hour coverage가 아닌 count-based heuristic이라는 한계도 승인 범위다. Base rollup의 inclusive
  31일 logical visibility/input window는 `COST-01-A` 범위이며 alert 90일은 아직 승인된 현재 계약이 아니다.
- [ ] `COST-05` 실제 fixture에서 낮은 사용량·execution-time-heavy·block read/write·temp/WAL
  statement aggregate를 구분하는 acceptance와
  provider 자료가 없거나 user/organization chargeback이 불가능할 때의 운영 판단 절차를
  문서화한다.

완료된 `CTRL-07` 입력과 `CTRL-08` projection 계약을 바꾸거나, 별도 승인 없이 collector schema나
통화 단위 비용을 추정해서 구현하지 않는다.

## P4 — End-to-End Workflow Trace

목표: 여러 transport 요청에 걸친 여러 tool call과 retry로 이어지는 사용자 workflow를 민감 입력
없이 추적한다. 현재 server-generated MCP HTTP request ID는 하나의 POST lifecycle만 연결하며
여러 POST와 model reasoning을 잇는 workflow trace는 아니다.

| 작업 경계 | 내용 |
|---|---|
| Primary module | Delivery |
| Direct consumers | Delivery/MCP lifecycle과 Guarded Query audit |
| Affected providers/verifiers | 승인될 경우 client trace input, Runtime process-local trace scope/counter/log allowlist, 현재 Delivery auth/HTTP/MCP context와 Assurance end-to-end verification |
| Contract baseline | 현재 HTTP/MCP correlation, audit/redaction과 supported MCP protocol |
| Approval gate | Client trace input, trust/collision policy, public header/schema와 audit field는 새 wire/observability 계약이다. Read-only prework인 [proposed ADR 0022](decisions/0022-w3c-workflow-trace-context.md)의 `TRACE-01-A|B|C`와 우선순위를 사용자가 정확히 승인한 뒤 구현한다. |
| Single writer | Coordinating agent가 Runtime scope와 Delivery wire 의미를 함께 동결한다. Runtime provider를 먼저 확정하고 Delivery set/reset 뒤 Guarded Query consumer를 연결하며, provider baseline 뒤 서로 다른 consumer 검증만 병렬화한다. |
| Start gate | 앞선 priority 완료 뒤 `TRACE-01` 결정을 수행하고, 사용자 승인 뒤에만 `TRACE-02`~`TRACE-04`를 구현한다. 현재 선택지 초안은 lower-track read-only prework일 뿐 P4 시작/완료가 아니며, 먼저 진행하려면 우선순위 변경도 명시적으로 승인받는다. |
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
