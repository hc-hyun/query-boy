# Source Management Plane

Status: Accepted design; implementation pending

Last updated: 2026-08-23

## Purpose

DB가 늘어나도 운영자가 한곳에서 source 정의, 적용 상태, 담당자, 접근 범위, 데이터 규모,
비용 신호와 변경 이력을 관리하게 한다. 실제 업무 데이터, secret, migration과 raw metric은
각 전문 시스템에 남을 수 있지만 Control Plane이 같은 `source_id`로 안전한 projection을
제공한다.

[ADR 0016](decisions/0016-centralized-source-management-plane.md)이 authority와 책임 경계를
정하고, 이 문서는 현재 공백, 목표 관리 계약과 `CTRL-*` 구현 순서를 관리한다. 구현되지 않은
API나 table을 현재 기능처럼 안내하지 않는다.

## Current Baseline

현재 구현된 기반:

- Immutable `source_profile_revisions`, `metadata_snapshots`, `verified_query_contracts`
- Active source/metadata pointer와 monotonic `state_version`
- Encrypted reader credential와 atomic stage/publish/rollback/deactivate
- Runtime polling과 no-restart registry reload
- Static budget profile, caller policy와 replica-local health/metric

현재 관리 공백:

- Operator용 source list/detail/generation history API가 없다.
- `/sources`는 query caller에게 허용된 active source 목록이지 운영 catalog가 아니다.
- Owner, environment, classification, cost center, migration/opaque credential-binding provenance를
  저장하지 않는다.
- Requester, approver, reason, plan hash와 mutation outcome을 남기는 durable audit가 없다.
- Replica별 desired/applied state를 중앙에서 비교할 수 없다.
- Row/storage/growth observation과 source별 durable usage/cost rollup이 없다.
- Filesystem과 Control DB verified contract를 합치는 현재 runtime은 bootstrap source를 managed
  lifecycle로 import한 뒤의 authority 분리를 보장하지 않는다.
- `operator: bool`만 있어 viewer/operator/approver 역할을 분리할 수 없다.
- External management authentication, Control DB role binding과 one-time bootstrap이 구현되지 않았다.
- 현재 direct publish는 reader credential을 request body로 받으며 plan-bound external credential
  broker가 없다.
- Query caller의 source grant는 아직 startup access-policy manifest authority라 동적 변경과
  Control DB precedence/import가 없다.
- Bootstrap YAML이 필수이고 integration fixture가 일반 local Control DB history에 누적된다.
- 기본 Compose는 Control DB source management를 활성화하지 않는다.

## Authority And Artifacts

| Artifact | Authority | Repository role |
|---|---|---|
| Production source definition and generation | Control DB | 자동 YAML 생성·역동기화 없음 |
| Active/deactivated state and history | Control DB | 없음 |
| Metadata snapshot and L2 verified contract | Control DB | Bootstrap/fixture contract만 예제로 유지 |
| Platform manifest schema and hard budget template | Query Man repository | Code review와 release 필요 |
| Bootstrap source | Local/CI seed, imported state | `config/sources/*.yaml` |
| Onboarding acceptance input | Test fixture only | `config/onboarding/*.yaml` |
| DB view/role/grant | Source DB and DB-owner migration system | Commit/revision reference만 Control Plane에 기록 |
| Plaintext reader credential and binding registry | External credential broker/secret system | 값·provider path 저장 금지 |
| Management credential | External authenticator/secret system | Token 저장 금지 |
| Management role/source-scope binding | Control DB | 일회성 bootstrap 이후 변경 감사 |
| Query caller identity/tenant authentication | External authenticator/deployment identity config | Stable identity만 전달 |
| Query caller source grant after one-time import | Control DB | Access-policy source scope는 seed only |
| Change approval and audit | Control DB | Sanitized export는 사본일 뿐 authority가 아님 |
| Raw metric/provider bill | External system when needed | Control Plane은 rollup과 link를 제공 |

Production managed mode는 source별 repository file을 요구하지 않는다. 파일 seed와 같은
`source_id`의 Control DB state가 존재하면 Control DB가 우선하며, deactivated state도 파일이
재활성화하지 못한다. Import한 source의 filesystem verified contract도 Control DB로 이관한 뒤
runtime에서 무시한다. 구현 전까지는 이 규칙을 acceptance로 고정하고 production에서 dual
origin이나 bootstrap source ID의 managed 재사용을 허용하지 않는다.

`CTRL-07` 이후 query caller의 stable identity와 tenant authentication은 기존 external/deployment
authority에 남고, source grant만 Control DB가 authority가 된다. Access-policy source scope는 한 번
import한 뒤 영구 marker를 남기는 seed이며 restart에서 다시 합치지 않는다. Import owner는 explicit
platform-admin migration 하나다. Complete canonical seed digest를 고정하고 한 Control DB
lock/transaction에서 전체 grants, actor/digest audit와 consumed marker를 원자적으로 commit한다.
Replica startup은 import하지 않으며 marker 없는 managed mode, partial failure, 다른 digest와
재-import를 fail-closed한다. Source publish와 grant mutation은 별도 plan/승인을 사용하고 effective
visibility는 active source와 active grant의 교집합이다.

## Unified Operator View

### Role-Scoped Inventory

- Source ID, name, owner, environment, classification와 cost center
- Active generation/state version, metadata revision/quality와 pin 상태
- Effective budget/version, allowed schema/kind와 caller visibility summary
- DB migration 적용 상태와 credential rotation freshness

Management viewer는 위의 logical inventory와 sanitized 상태·규모·비용 summary만 조회한다.
내부 host/database/user, endpoint와 secret-provider reference/path는 받지 않는다. 해당 source
scope를 가진 operator와 approver만 exact change plan을 검토하는 데 필요한 resolved
host/database/user/TLS, DB migration change ID와 opaque credential binding/provider version을 조회한다.
어느 역할에도 credential 값, bearer 또는 master key는 반환하지 않는다.

### Runtime State

- Replica ID별 applied generation/state version/metadata revision
- Last observed time, health, stale age와 desired/applied drift
- Control-plane 장애와 runtime validation 거부를 구분한 bounded reason

### Change History

- Requester, approver, action, reason/change ticket와 canonical plan hash
- Expected generation/state version, outcome generation/revision과 timestamp
- Validate/reject/apply/unknown/reconcile/rollback/deactivate event
- Credential, bearer, raw database error와 ad-hoc question/SQL은 제외

### Data Size And Growth

관측값은 configuration revision과 분리한다. 다음 필드를 가진 bounded snapshot/rollup으로
관리한다.

```text
source_id, scope, metric, value, unit, method,
definition_revision, metadata_revision?, observed_at, fresh_until
```

지원할 method:

- `catalog_estimate`: table/materialized view의 통계 기반 estimate
- `bounded_exact`: 명시적으로 승인된 작은 counter relation
- `owner_reported`: 일반 view 또는 source owner의 reviewed metric
- `provider_reported`: database/cluster storage와 billing metric

일반 view를 합친 “전체 row 수”는 중복 grain 때문에 의미가 없을 수 있다. Source owner가 대표
volume metric과 grain을 지정하고 Control Plane은 method와 freshness를 함께 보여준다. Collector는
기본적으로 무제한 `COUNT(*)`나 `EXPLAIN ANALYZE`를 실행하지 않는다. Growth는 같은 metric,
method와 `definition_revision`끼리만 계산한다. Definition revision은 scope, grain, method와 metric
의미의 canonical hash다. Catalog 기반 metric은 관련 `metadata_revision`도 definition에 포함하고,
owner/provider-reported metric은 metadata와 독립된 provider/owner definition version을 사용한다.

### Usage And Cost

- Gateway: query/reject/timeout 수, queue/elapsed 합계, rows/result bytes와 truncation
- PostgreSQL: calls/execution time/rows/shared-local-temp block/WAL 등 승인된 DB-native aggregate
- Storage: database/table/index bytes와 증가량
- Billing: provider 금액, 통화, 기간, 배분 방법/version과 confidence

SQL text, literal, ad-hoc question과 parameter는 저장하지 않는다. Planner cost나 row count만으로
통화 비용을 만들지 않으며 provider 자료가 없으면 자원 사용량과 추세만 표시한다. 상세 collector
계약은 [query cost control](query-cost-control.md)과 `COST-*`가 담당한다. 수집기가 없거나
provider billing이 연결되지 않은 상태는 0으로 표시하지 않고 `not_configured`, 수집 기한을 넘긴
값은 `stale`로 표시한다. 전체 availability 상태는 다음과 같다.

- `not_configured`: collector/provider 연결이 없음
- `pending`: 구성됐지만 아직 시도하지 않음
- `available`: fresh한 성공 값이 있음
- `stale`: 마지막 성공 값이 있지만 freshness가 지났음
- `unavailable`: 시도했지만 usable value가 한 번도 없거나 유지할 값 없이 실패함

각 상태는 적용 가능한 `last_attempt_at`과 bounded reason code를 제공한다. Missing/failed 값은 0으로
표시하지 않는다.

## Role Boundary

| Role | Can do | Cannot do |
|---|---|---|
| Platform developer | Schema/API/validator와 budget template 개발·배포 | Production source apply/approve |
| DB owner | Curated view/reader와 migration/secret evidence 준비 | Query Man apply/approve |
| Management viewer | Sanitized catalog, status와 cost 조회 | Plan/approve/apply |
| Source operator | Draft/validate 요청, 승인된 plan apply, recovery 실행 | 자기 변경 승인, platform policy 완화 |
| Approver | Exact plan hash와 영향 승인/거절 | Apply와 secret 접근 |
| Platform admin | Role binding와 audited break-glass | 사유 없는 mutation |

Management principal과 query caller token은 분리한다. 조회용 HTTP/MCP에 admin tool이나 권한을
추가하지 않는다. 외부 authenticator가 전용 management audience의 immutable issuer/subject를
확인하고 Control DB가 role/source-scope binding을 결정한다. Binding table이 비어 있을 때만
explicit bootstrap mode와 외부 one-time secret으로 최초 platform admin을 만든다. 같은
transaction/lock에서 영구 bootstrap marker 확인, binding/event 생성과 consumed 전이를 수행해
동시·replay 요청을 거부한다. Binding을 모두 지워도 bootstrap은 다시 열리지 않으며 마지막 active
platform admin 삭제도 거부한다. 이후 deployment config는 binding을 덮어쓸 수 없다. Production
approval separation과 role-binding mutation의 세부 정책은 `CTRL-06`에서 확정한다.

## Planned Management Contract

현재 endpoint가 아니라 구현 목표다.

| Operation | Purpose |
|---|---|
| `GET /admin/sources` | Management role/scope별 filter·pagination이 있는 sanitized inventory와 최신 status/size/cost summary |
| `GET /admin/sources/{source_id}` | Role-scoped effective manifest, ownership, provenance, limits와 freshness |
| `GET /admin/sources/{source_id}/history` | `CTRL-04`의 generation history; `CTRL-05` 이후 lifecycle/change event까지 확장 |
| `POST /admin/source-changes` | Source state를 바꾸지 않는 validation과 canonical plan 생성 |
| `GET /admin/source-changes/{change_id}` | Timeout 후 authoritative result/reconciliation 조회 |
| `POST /admin/source-changes/{change_id}/approval` | Exact plan hash와 expiry에 묶인 승인/거절 |
| `POST /admin/source-changes/{change_id}/apply` | 승인된 plan을 idempotent하게 적용하고 기존 결과 재사용 |

Mutation API는 arbitrary credential/URL을 model argument로 받지 않는다. Production AI executor는
Codex 일반 shell과 분리된 execution broker를 통해 plan ID만 적용한다. 현재 direct
`PUT /admin/sources/{source_id}`는 새 change contract가 완성되기 전까지 manual operator
boundary이며 blind retry하지 않는다.

새 change contract는 plaintext credential 대신 server-owned allowlist의 opaque
`credential_binding_id`만 받는다. Credential broker가 외부 secret의 exact provider
reference/version과 attested target/purpose를 일시적으로 resolve한다. External secret admin/DB
owner만 binding을 만들거나 회전하며 하나의 source ID, host/port/database/user/TLS와
`query_reader` purpose에 고정한다. Server는 manifest와 모두 일치할 때 binding ID, target,
purpose/version을 canonical plan hash에 넣는다. Apply도 전부 다시 확인해 같을 때만 기존 방식으로
암호화해 generation에 저장한다. Drift, unknown binding 또는 provider 오류는 plan을 폐기하며 값,
provider path와 일부 secret 문자열도 response/audit/log에 남기지 않는다.

## Storage Shape

기존 immutable revision/pointer table을 재사용한다. 정확한 migration은 구현 단계에서
정규화와 retention을 검토하되 다음 책임을 분리한다.

- Catalog: ownership/environment/classification/provenance
- Authorization: immutable management principal, management-bootstrap marker, versioned
  role/source-scope binding, query-caller grant와 grant-import marker/digest
- Change request/approval/event: plan binding, idempotency, actor와 outcome
- Runtime observation: replica별 desired/applied state와 freshness
- Relation/source observation: row/storage/growth method, definition revision과 snapshot
- Usage/cost rollup: bounded time bucket, availability, allocation-method version과 external provider
  reference

High-frequency raw event를 Control DB에 무제한 적재하지 않는다. 초기에는 latest snapshot과
hour/day rollup을 저장하고 retention/cardinality 한계를 둔다. 외부 metrics/billing store를
도입해도 같은 management API가 projection과 link를 제공한다.

## Rollout Checklist

Canonical status는 [active development TODO](development-todo.md)의 `CTRL-*`가 관리한다.

1. Versioned migration과 test-store isolation
2. Authority precedence, managed-mode startup/seed import 규칙
3. Catalog ownership/provenance
4. Management identity/bootstrap, viewer/scoped-operator scope와 sanitized list/detail/generation read model
5. Credential-bound plan, durable lifecycle audit와 authoritative reconciliation
6. Mutation RBAC, approval separation와 idempotent apply
7. Atomic query-caller grant seed cutover/precedence, versioned dynamic grant와 effective visibility
8. Replica convergence/drift observation
9. Bounded size/growth collection
10. Gateway usage와 usage/cost projection의 availability/freshness·입력 계약, retention,
    backup/restore와 end-to-end acceptance

실제 PostgreSQL resource collector는 이 rollout의 선행 조건이 아니다. 중앙 projection 계약이
완료된 뒤 [active development TODO](development-todo.md)의 `COST-*`가 DB-native aggregate를
추가한다. Provider connector도 이 단계의 완료 조건이 아니며 연결되지 않으면
`not_configured`로 표시한다.

## Release Acceptance

- Operator는 Git checkout 없이 모든 managed source의 owner, active state, history와 freshness를
  조회할 수 있다.
- Production source addition은 repository file이나 application deploy를 만들지 않는다.
- Source operator, approver, query caller와 platform developer 권한이 분리된다.
- Query caller grant seed는 한 canonical digest로 grants/audit/consumed marker가 원자적으로
  import되고 모든 replica가 Control DB의 같은 active grant를 사용한다.
- Query token은 management endpoint에서 거부되고 최초 admin bootstrap은 빈 binding table에서 한
  번만 원자적으로 가능하며 consumed marker는 binding 삭제 뒤에도 유지된다.
- 같은 change ID의 재호출은 새 generation을 만들지 않고 authoritative result를 반환한다.
- Validation/apply는 allowlisted credential binding의 승인된 provider version만 사용하고 binding의
  source/target/purpose가 manifest와 일치해야 하며 secret 값은 request/response/audit/log에 남지
  않는다.
- 모든 target replica의 desired/applied 차이를 source별로 확인할 수 있다.
- Row/storage 값은 method/definition revision/time/freshness를 포함하고 unbounded count를 실행하지
  않는다.
- Cost는 연결된 경우 관측 가능한 resource/billing 근거와 allocation version/confidence를
  표시하고, 연결되지 않음/미시도/오래됨/수집 실패를 각각
  `not_configured|pending|stale|unavailable`로 표시한다.
- Secret, ad-hoc question/SQL과 내부 DB error가 management response, audit와 metric에 없다.
- Production/development/test Control DB가 분리되고 전체 authority table의 backup/restore와
  retention 검증이 재현 가능하다.

## Non-Goals

- Source의 업무 데이터를 Control DB로 복사하지 않는다.
- 관리 편의를 위해 DB owner의 DDL/IAM 책임을 Query Man으로 가져오지 않는다.
- Git과 Control DB를 production source의 병렬 desired state로 만들지 않는다.
- 조회용 MCP를 admin surface로 사용하지 않는다.
- Exact global row count나 query별 정확한 통화 원가를 근거 없이 제공하지 않는다.
