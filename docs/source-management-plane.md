# Source Management Plane

Status: Active implementation

Last updated: 2026-08-23

## Purpose

DB가 늘어나도 운영자가 한곳에서 source 정의, 적용 상태, 담당자, resource tier, 데이터 규모,
비용 신호와 변경 이력을 관리하게 한다. 실제 업무 데이터, secret, migration과 raw metric은
각 전문 시스템에 남을 수 있지만 Control Plane이 같은 `source_id`로 안전한 projection을
제공한다.

[ADR 0016](decisions/0016-centralized-source-management-plane.md)이 authority를,
[ADR 0017](decisions/0017-shared-source-access-and-resource-tier.md)이 초기 access와 resource
tier를 정한다. 이 문서는 현재 공백과 `CTRL-*` 구현 순서를 관리하며 아직 없는 API나 table을
현재 기능처럼 안내하지 않는다.

## Initial Operating Model

- DB 추가·변경·취소는 한 종류의 Query Man admin만 수행한다.
- 모든 인증된 query principal은 같은 active source 목록을 본다.
- 관리자는 source마다 기존 `budget_profile` 하나를 선택한다. 그 source의 모든 사용자는 같은
  profile 정의를 공유한다.
- `budget_profile`이 유일한 resource tier다. 별도 `cost_tier`나 user/organization binding은 없다.
- Stable caller/tenant identity는 audit와 source-native RLS에만 사용하고 source access, tier 또는
  비용 집계 기준으로 쓰지 않는다.
- Query MCP에는 admin tool을 추가하지 않는다.

## Current Baseline And Gaps

이미 구현된 기반:

- Immutable `source_profile_revisions`, `metadata_snapshots`, `verified_query_contracts`
- Active source/metadata pointer와 monotonic `state_version`
- Encrypted reader credential와 atomic stage/publish/rollback/deactivate
- Runtime polling과 no-restart registry reload
- Source-resolved `budget_profile`과 HTTP/MCP 공통 query 경계
- `operator`로 보호된 mutation/cancel endpoint
- Checksum을 기록하는 numbered Control DB migration과 반복 가능한 role/ACL reconciliation
- 개발 Control DB를 변경하지 않는 function-scoped disposable integration-test Control DB
- Version 2 shared-access policy와 explicit query/admin credential 분리
- 모든 authenticated identity의 implicit active-source visibility와 source-resolved budget
- Strict source manifest v2의 immutable owner/environment/DB migration provenance
- Secret-free admin source inventory, effective detail와 generation history read API
- 여섯 direct admin mutation의 공통 idempotency/state precondition과 immutable terminal receipt
- Operator-only receipt lookup과 source별 lifecycle event keyset history

아직 구현할 공백:

- Replica별 desired/applied state와 freshness
- Bounded record/storage/growth observation과 source/profile별 usage/cost projection
- Authority table backup/restore, retention과 encryption-key recovery 검증

다단계 management RBAC, caller grant import, 사용자별 quota와 AI mutation executor는 공백이
아니라 현재 non-goal이다.

## Authority And Artifacts

| Artifact | Authority | Repository role |
|---|---|---|
| Production source definition, generation and lifecycle | Control DB | YAML write-back 없음 |
| Metadata snapshot and hot-added verified contract | Control DB | Bootstrap/fixture contract만 유지 |
| Owner, environment and DB migration provenance | Control DB immutable manifest generation | Repository YAML은 fixture contract만 versioned |
| Curated view, reader role and grants | Source DB and DB-owner migration system | Migration reference만 기록 |
| Encrypted reader credential | Control DB generation | Plaintext 출력·Git 저장 금지 |
| Plaintext credential and master key | Runtime/external secret system | 값·provider path 저장 금지 |
| Query/admin authentication | Version 2 deployment access policy | Secret 값 저장 금지 |
| Shared source access policy | ADR 0017/platform configuration | Control DB caller-grant table 없음 |
| Budget profile catalog | `config/budget-profiles.yaml` and release | Source는 승인된 이름만 참조 |
| Mutation audit and authoritative receipt | Control DB | Sanitized export는 사본 |
| Raw metrics/provider bill | External system when configured | Bounded rollup/provenance만 연결 |
| Unified sanitized projection | Admin management API | 실제 authority를 대체하지 않음 |

Production managed mode는 source별 repository file을 요구하지 않는다. Managed runtime은 모든
source와 verified contract를 Control DB에서만 읽으므로 lifecycle row가 없는 file source도
absent다. Deactivate와 rollback을 포함한 Control DB state가 restart 뒤 그대로 복원되고 file은
source를 다시 활성화하거나 L2 gate를 충족하지 못한다.

## Runtime Authority And One-Time Import

| `QUERY_MAN_SOURCE_MODE` | Source/verified authority | Control settings |
|---|---|---|
| `bootstrap` (default) | `config/sources` and filesystem verified contract | Control DSN/key 모두 금지 |
| `managed` | Control DB lifecycle, metadata and verified contract only | Control DSN/key 모두 필수 |

Mode는 process 전체에 적용하며 runtime 중 바뀌지 않는다. `auto`, per-source hybrid와 Control DB
장애 시 file fallback은 없다. Budget profile catalog와 access policy는 두 mode에서 모두 deployment
configuration으로 읽는다. Managed mode는 version 2 policy file의 non-admin query identity와
explicit operator admin identity를 모두 요구하고 API-token/anonymous caller를 거부한다. Source
directory와 filesystem verified contract가 없어도 시작할 수 있다.

기존 bootstrap source는 다음 명시적 cutover로 한 번만 이관한다.

1. Control schema와 최소 권한 writer를 준비하고 serving traffic을 받지 않는 managed instance를
   시작한다. Empty inventory 또는 cold Control scan 실패의 `/ready`는 `unavailable`이다.
2. 기존 admin API로 source를 L0/L1 staged publish하고 반환된 generation과 metadata revision을
   확인한다.
3. Reviewed filesystem contract를 같은 revision의 verified-query admin endpoint로 실행·저장한다.
4. `minimum_quality_level: L2` generation을 publish하고 metadata/query invariant를 확인한다.
5. 필요한 source/contract coverage와 inactive state를 확인한 뒤 serving replica를 managed mode로
   재시작한다. 이후 repository seed는 제거하거나 남겨도 runtime authority가 아니다.

Reader plaintext credential은 admin이 external secret boundary에서 직접 전달한다. Startup importer,
bulk import endpoint, seed digest/marker와 filesystem write-back은 만들지 않는다. 모든 mutation은
현재 generation/state와 change reference를 명시하고 성공 응답의 receipt를 변경 기록에 남긴다.
Timeout은 아래 reconciliation 절차를 따르며 새 key로 blind retry하지 않는다.

## Admin View

### Inventory And History

- Source ID, name, description, owner와 environment
- Active/deactivated state, generation/state version, metadata revision/quality
- Effective `budget_profile`과 관련 metadata revision
- DB migration reference와 generation creation time처럼 비밀이 아닌 provenance
- Replica convergence, latest record/storage observation과 usage/cost availability
- Actor, reason, request hash, expected/resulting state, outcome과 timestamp

Host/database/user는 mutation 검토에 필요한 admin에게만 제한적으로 제공한다. Credential,
bearer, master key, provider secret path, raw database error, ad-hoc question과 SQL은 response와
audit에서 제외한다.

### Replica State

- Replica ID별 applied generation/state version/metadata revision
- Last observed time, health, stale age와 desired/applied drift
- Control-plane 장애와 runtime validation 거부를 구분한 bounded reason

### Data Size And Growth

관측값은 configuration revision과 분리하고 다음 bounded shape를 사용한다.

```text
source_id, scope, metric, value, unit, method,
definition_revision, metadata_revision?, observed_at, fresh_until
```

기본 method는 catalog estimate, 명시적으로 승인된 cheap counter, owner/provider reported 값이다.
일반 view에 무제한 `COUNT(*)` 또는 `EXPLAIN ANALYZE`를 실행하지 않는다. 대표 volume metric과
grain은 DB owner가 지정한다. Growth는 같은 metric, method와 `definition_revision` 사이에서만
계산한다.

### Usage And Cost

초기 집계 key는 bounded한 `source_id + budget_profile + metadata_revision + time bucket`이다.
Budget 정의가 metadata revision 재료이므로 별도 tier revision entity를 만들지 않는다.

- Gateway query/success/reject/timeout, queue/elapsed, rows/result bytes와 truncation
- 승인된 PostgreSQL execution/block/temp/WAL aggregate
- Database/table/index storage와 증가량
- 연결된 경우에만 provider amount, currency, period와 allocation method

Caller/tenant는 security audit에 남을 수 있지만 비용, quota와 metric label dimension으로 쓰지
않는다. Provider billing이 없으면 자원 사용과 추세만 표시한다. Availability는
`not_configured|pending|available|stale|unavailable`을 구분하고 last attempt, freshness와 bounded
reason을 함께 제공한다. Missing/failed 값은 0으로 표시하지 않는다.

## Access Boundary

| Principal | Can do | Cannot do |
|---|---|---|
| Query user | 모든 active source에서 metadata/query 사용 | Admin API, cancel, tier override |
| Query Man admin | Source list/detail/history, mutation과 cancel | DB-owner DDL/IAM 책임 위임, secret 조회 |
| DB owner | Curated view/reader/migration과 credential 준비 | Query Man production mutation |
| Platform developer | Schema/API/validator와 budget profile release | Production source를 임의로 적용 |

`operator` boolean을 admin capability superset으로 재사용한다. Query credential은 모든 admin
endpoint와 cancel에서 거부한다. Version 2 policy에는 source scope가 없고 모든 인증 identity가
같은 active source를 본다. Managed mode는 explicit query/admin identity를 요구하며 generic API
token과 anonymous local identity를 거부한다. 새 role enum, role-binding table, source scope와
bootstrap marker는 만들지 않는다.

Source-native RLS가 필요한 source는 ADR 0014의 trusted `tenant_id`를 계속 사용한다. 이는 모든
사용자가 같은 source를 보고 같은 resource tier를 쓴다는 결정과 독립된 row-isolation 경계다.
Control Plane이 user/organization별 RLS policy를 관리한다는 뜻은 아니다.

## Current And Planned Management Contract

아래는 구현 목표다. 기존 direct mutation endpoint는 유지하고 공통 idempotency/audit 계약을
덧붙인다.

| Operation | Status and purpose |
|---|---|
| `GET /admin/sources` | Implemented: exact filter와 source-ID keyset pagination을 쓰는 sanitized inventory |
| `GET /admin/sources/{source_id}` | Implemented: effective source/resource tier와 published/active metadata revision을 구분한 detail |
| `GET /admin/sources/{source_id}/history` | Implemented: generation-descending immutable manifest history |
| `GET /admin/sources/{source_id}/mutations` | Implemented: event-ID keyset pagination의 sanitized lifecycle receipt history |
| Existing `PUT/POST/DELETE /admin/...` | Implemented: admin-only staged validation, expected-state CAS와 atomic success receipt |
| `GET /admin/mutations/{idempotency_key}` | Implemented: timeout 뒤 terminal result/rejection reconciliation 조회 |

모든 mutation은 idempotency key, key 자체를 포함한 canonical request hash, actor, reason, expected generation/state와
bounded outcome을 기록한다. 같은 key와 같은 hash는 기존 결과를 반환하고 다른 hash는
fail-closed한다. 별도 request/approval table이나 approver endpoint는 만들지 않는다.

### Mutation Request And Receipt Contract

여섯 mutation은 다음 header를 모두 요구한다.

| Header | Contract |
|---|---|
| `Idempotency-Key` | Client가 생성한 canonical lowercase UUID. 재시도에서도 그대로 유지 |
| `X-Query-Man-Reason` | 1~128자의 ticket/change reference. 자유형 설명이나 secret 금지 |
| `X-Expected-Generation` | Client가 직전에 조회한 generation |
| `X-Expected-State-Version` | 같은 snapshot의 state version |
| `X-Expected-Metadata-Revision` | Metadata resume에만 필수인 현재 pinned revision |

새 source의 첫 publish만 generation/state `0/0`을 사용하고 기존 source mutation은 양의 현재값을
사용한다. 두 expected 값 중 하나만 0인 요청, 중복 header, 예상 범위를 넘는 값과 불필요한 metadata
header는 400이다. Actor는 인증된 operator의 `caller_id`에서 파생하며 body/header로 받지 않는다.
Query/anonymous caller는 path, query, header와 body 검증보다 먼저 403/401로 거부된다.

성공 response는 원래 operation 결과를 `result`에 담은 terminal receipt다. Receipt에는 event ID,
key, key-bound `hmac-sha256` request hash, operation/source, actor/change reference, expected/resulting state,
outcome/status와 recorded time만 있다. Credential, manifest, verified question/SQL, metadata snapshot과
내부 오류는 저장하거나 반환하지 않는다. 결정적인 validation/state conflict도 같은 key의 immutable
rejection receipt를 남기지만 인증 실패와 Control/source dependency의 unavailable 오류는 receipt로
성공처럼 고정하지 않는다.

같은 key와 canonical request의 terminal exact replay는 기존 receipt를 반환하고 staging/query나
state/generation 변경을 다시 수행하지 않는다. Credential, manifest array order, actor, reason, expected state 또는 operation
중 하나라도 다르면 409다. JSON object key order와 표현상 공백은 canonicalization으로 동일하게
취급한다.

### Timeout Reconciliation

Receipt table은 terminal-only이며 별도 pending row/table을 만들지 않는다. 따라서 404는 실패가
아니라 아직 staging 또는 transaction 중이라는 뜻일 수 있다.

1. 요청에 사용한 key로 `GET /admin/mutations/{idempotency_key}`를 bounded polling한다.
2. Receipt가 있으면 `outcome`, `resulting_state`와 source detail을 대조하고 그 결과를 authoritative로
   사용한다. Rejection이면 원인을 수정한 새 의도에 새 key를 발급한다.
3. Receipt가 없으면 source detail의 generation/state가 요청 전 expected state인지 확인하고 원래
   요청이 끝날 시간을 기다린다. 새 key나 변경된 payload로 보내지 않는다.
4. Bounded wait 뒤에도 receipt가 없고 state가 expected와 같음을 다시 확인한 경우에만 같은 key와
   같은 semantic 요청을 한 번 재전송한다. Fan-out 재시도는 하지 않는다. Terminal-only storage라
   서로 다른 replica가 receipt 생성 전에 같은 key를 동시에 받으면 catalog staging 또는 verified
   query가 중복될 수 있지만 store의 key/source lock과 atomic receipt가 중복 authority commit을
   막는다. State가 달라졌다면 먼저 source mutation history로 변경 주체를 확인한다.

현재 direct source publish는 credential을 request body에서 받는 trusted manual-admin
boundary다. TLS, access-log body 비기록, redaction, staged validation과 AES-256-GCM at-rest
encryption을 유지한다. Plan-only onboarding Skill은 credential을 읽거나 endpoint를 호출하지
않는다. AI executor가 실제 요구가 되면 target-bound credential broker와 plan-ID apply를 새
threat model/ADR 뒤에 설계한다.

## Storage Shape

기존 immutable revision/pointer table을 재사용하고 필요한 책임만 추가한다.

Owner, environment와 DB migration reference는 strict manifest v2의 `provenance` block에 포함해
`source_profile_revisions.manifest`에 generation과 함께 저장한다. Provenance를 위한 별도 catalog
table, 중복 column이나 `CTRL-04` schema migration은 만들지 않았다. `CTRL-05`의 두 번째 Control
schema migration은 이 provenance와 별개인 mutation receipt table만 추가한다. Provenance 값만
변경한 publish도 새 generation을 만들고 rollback은 당시 값을 그대로 복원한다. Query metadata
revision은 query contract에 영향을 주는 필드만 hash하므로 provenance 변경으로 달라지지 않는다.

Runtime mode는 deployment configuration이고 existing source/metadata pointer와 verified contract가
managed authority를 모두 표현한다. 따라서 mode, origin 또는 bootstrap import marker를 위한 table과
`CTRL-02` schema migration은 추가하지 않는다.

- Migration ledger: version, immutable filename/checksum, applied time와 migration identity
- Minimal catalog: owner, environment와 DB migration provenance
- Mutation event/receipt: idempotency, actor, reason, request hash와 outcome
- Runtime observation: replica별 desired/applied state와 freshness
- Source observation: record/storage/growth method, definition revision과 snapshot
- Usage/cost rollup: bounded source/profile time bucket, availability와 provider provenance

High-frequency raw event를 Control DB에 무제한 적재하지 않는다. 초기에는 latest snapshot과
hour/day rollup만 저장하고 retention/cardinality 상한을 둔다.

## Rollout Checklist

Canonical status는 [active development TODO](development-todo.md)의 `CTRL-*`가 관리한다.

1. **Complete:** versioned migration과 disposable test-store isolation
2. **Complete:** mutually exclusive source mode, Control DB precedence, zero-bootstrap과
   verified-contract admin import cutover
3. **Complete:** shared query access와 explicit admin/query credential separation
4. **Complete:** immutable provenance, minimal catalog와 admin list/detail/history
5. **Complete:** existing mutations의 idempotency, receipt와 durable audit
6. Replica convergence/drift observation
7. Bounded record/storage/usage/cost observation
8. Usage/cost availability와 cardinality/retention
9. Backup/restore, encryption-key recovery와 multi-replica end-to-end acceptance

DB-native collector와 provider connector는 rollout의 선행 조건이 아니다. 연결되지 않은 값은
`not_configured`로 표시하고 `COST-*`가 이후 aggregate를 추가한다.
첫 단계의 실행 증거는
[control schema migration audit](verification/2026-08-23-control-schema-migrations.md)에 있다.
두 번째 단계의 검증 계획과 실행 결과는
[managed source startup audit](verification/2026-08-23-managed-source-startup.md)에 기록한다.
세 번째 단계의 검증 계획과 실행 결과는
[shared access audit](verification/2026-08-23-shared-access.md)에 기록한다.
네 번째 단계의 strict manifest, redaction, pagination, revision 구분과 admin-only 증거는
[source management catalog audit](verification/2026-08-23-source-management-catalog.md)에 기록한다.
다섯 번째 단계의 atomic receipt, exact replay/conflict, migration/rolling compatibility와
operator-first validation 증거는
[source mutation receipt audit](verification/2026-08-23-source-mutation-receipts.md)에 기록한다.

## Release Acceptance

- 운영자는 Git checkout 없이 모든 managed source의 owner, active state, history, resource tier와
  freshness를 조회한다.
- Production source addition은 repository file이나 application deploy를 만들지 않는다.
- 서로 다른 두 query identity가 같은 active source 목록을 보고 caller override 없이 같은
  source-resolved budget 정의를 적용받는다. Public query API에 profile field를 새로 노출할
  필요는 없다.
- Query credential은 모든 admin endpoint와 cancel에서 거부되고 admin credential만 mutation한다.
- Single-token/local compatibility path가 source management 환경에서 암시적 admin을 만들지 않는다.
- 같은 idempotency key의 재호출은 새 generation을 만들지 않고 authoritative result를 반환한다.
- 모든 target replica의 desired/applied 차이를 source별로 확인한다.
- Record/storage 값은 method/definition revision/time/freshness를 포함하고 unbounded count를
  실행하지 않는다.
- Cost는 근거가 있을 때만 표시하며 미구성/미시도/오래됨/실패 상태를 구분한다.
- Secret, ad-hoc question/SQL과 내부 DB error가 management response, audit와 metric에 없다.
- Production/development/test Control DB가 분리되고 authority table과 encryption key의
  backup/restore 검증이 재현 가능하다.

## Explicitly Deferred

- User/organization별 source ACL, `budget_profile` override, quota와 fairness
- 별도 `cost_tier`, caller별 비용 dashboard와 chargeback ledger
- Viewer/operator/approver/platform-admin 역할 계층과 2인 approval workflow
- Control DB caller-grant table, seed digest/import/marker와 dynamic grant API
- AI production mutation executor, credential broker와 plan-ID apply
- Exact global row count나 근거 없는 query별 통화 원가

필요성이 생기면 현재 source authority, stable principal identity와 audit fact를 입력으로 별도
ADR에서 설계한다. 이를 위해 지금 nullable assignment table이나 추상 interface를 미리 만들지
않는다.
