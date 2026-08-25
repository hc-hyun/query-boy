# Control Plane Module

Status: Logical boundary; physical package split pending

## 목적

Control Plane은 “어떤 source 정의와 metadata/verified revision이 현재 사용 중인가”를 영속적으로
결정한다. 새 database 등록, credential 교체, 비활성화와 rollback을 검증된 상태 전이로 만들고,
각 runtime replica가 그 desired state를 안전하게 적용하도록 한다.

쉽게 말하면 Source Catalog가 source 정의의 형식을 소유하고 Control Plane은 그 정의의 이력과
현재 선택값을 소유한다.

## 소유 책임

- Numbered Control DB migration, immutable checksum ledger, schema, constraint, trigger, role와 grant
- Immutable source generation, encrypted credential과 active source pointer
- Immutable metadata snapshot, active revision pointer, pin/unpin과 rollback transaction
- Immutable verified query contract persistence
- Source-scoped advisory transaction lock와 generation/state-version CAS
- Publish/rotate/deactivate/rollback/resume/verified-publish application use case
- Idempotency key, keyed canonical request hash, expected state와 terminal mutation receipt/audit
- Secret-free source list/detail/generation history와 mutation lookup/history projection
- Candidate source의 manifest, connection, catalog, quality와 verified result staging
- Committed desired state를 runtime registry/cache/pool에 적용하는 `SourceReloader`
- Stable managed replica slot의 fenced registration, latest source observation과 DB-clock freshness
- Desired/applied drift의 secret-free admin projection
- Latest resource attempt/success와 bounded resource/gateway usage availability projection
- Control DB 장애, conflict와 validation failure를 비공개 application 오류로 변환하는 의미

## 소유하지 않는 책임

- Manifest/budget/semantic schema 자체의 정의
- Metadata snapshot shape, revision digest와 context assembly
- SQL validator, query result encoding과 executor safety policy
- Caller authentication, admin capability와 HTTP/MCP request/response shape
- Process startup/shutdown와 reload polling schedule
- Source DB의 reader role, curated view 또는 business schema migration

## 현재 코드 위치

- [`source_admin.py`](../../../src/query_man/source_admin.py): `SourceAdminService`, `SourceReloader`,
  public administration input/sequence, replica/resource/gateway observation writer, usage projection과
  persistence/invalidator ports
- [`source_store.py`](../../../src/query_man/source_store.py): `PostgresSourceStore`와 state transition
  transactions
- [`metadata_store.py`](../../../src/query_man/metadata_store.py): `PostgresMetadataStore` implementation
  및 Metadata가 소유하는 port/codec이 함께 있는 transition hot spot
- [`secrets.py`](../../../src/query_man/secrets.py): generation-bound AES-GCM credential encryption
- [`errors.py`](../../../src/query_man/errors.py): source validation/conflict/control-unavailable 의미;
  public rendering은 Delivery 계약
- [`05-control-plane.sh`](../../../docker/postgres/init/05-control-plane.sh): disposable container의
  numbered migration entrypoint
- [`control-migrations`](../../../docker/postgres/init/control-migrations): immutable numbered schema
  migration, checksum ledger, apply lock와 least-privilege reconciliation
- [`apply-control-schema.sh`](../../../scripts/apply-control-schema.sh),
  [`control-plane-drill.sh`](../../../scripts/control-plane-drill.sh): production-style schema
  apply와 recovery/acceptance drill
- Focused tests: [`test_source_admin.py`](../../../tests/test_source_admin.py),
  [`test_source_store.py`](../../../tests/test_source_store.py),
  [`test_metadata_store.py`](../../../tests/test_metadata_store.py),
  [`test_secrets.py`](../../../tests/test_secrets.py),
  [`test_control_migrations.py`](../../../tests/test_control_migrations.py),
  [`test_control_startup.py`](../../../tests/test_control_startup.py),
  [`test_managed_mode.py`](../../../tests/test_managed_mode.py),
  [`test_control_recovery.py`](../../../tests/test_control_recovery.py)

`metadata_store.py`에서 Metadata는 `MetadataStore` capability와 snapshot codec/compatibility를,
Control Plane은 PostgreSQL pool, SQL, lock과 transaction을 소유한다. 현재 shared file이라는 이유로
다른 쪽 계약을 함께 바꾸지 않는다.

`SourceAdminService._stage`는 candidate를 active runtime과 격리해 검증하려고 일시적인
`SourceRegistry + MetadataService + RuntimeCatalogProvider`를 조립하고 registry application reference는
`SourceReader`로 좁힌다. 이는 Control Plane에 한정된 staging composition root이며 production
HTTP/MCP wiring이나 Metadata 업무 규칙을 소유하지 않는다. Assurance offline composition을
`assurance_cli.py`로 옮겨도 이 staging root의 위치와 동작은 바뀌지 않는다.

Public admin route, operator-first request parsing, bounded JSON/header/query validation과 HTTP error
rendering은 [Delivery](../delivery/README.md)가 소유한다. Control Plane은 이미 검증된
`MutationContext`와 use-case input을 받고 persisted transition/result 의미를 소유한다.

## 제공 계약

### Source administration contract

Public 관리 mutation은 authenticated actor에서 만든 `MutationContext`를 받고 현재
generation/state version을 기준으로 검증한다. Resume은 expected metadata revision도 요구한다.
Delivery는 Control persistence나 Assurance DTO 대신 다음 public Python 계약만 사용한다.

```text
CONTROL_SEQUENCE_MAX = 9_223_372_036_854_775_807
VerifiedExpectedInput(columns, row_count, result_hash)
PublishVerifiedQueryInput(
  query_id, source_id, question, sql, metadata_revision, relations, expected
)
SourceAdminService.publish_verified_query(
  PublishVerifiedQueryInput, tenant_id, MutationContext | None
)
```

두 input은 frozen dataclass다. Control Plane 서비스만 이를 Assurance의 `VerifiedQuery`와
`ExpectedResult`로 변환하고 persistence port에는 그 verified contract를 전달한다. Sequence 상한은
현재 Control DB `bigint`와 HTTP validation 의미를 그대로 공개한 값이며 독립적으로 변경하지 않는다.
각 operation은 다음 persisted transition을 시도한다.

```text
publish -> new immutable generation + metadata revision + active pointers
rotate credential -> new immutable generation; same source connection identity
deactivate -> active source disabled with next state version
rollback -> historical generation + matching metadata revision activated and pinned
resume metadata publish -> current pinned metadata revision unpinned
publish verified query -> immutable revision-bound expected result
```

같은 query ID도 metadata revision이 다르면 별도 immutable contract row다. Global policy 전환은
current와 rollback-preserved contract 전체를 새 revision에서 재실행하고, 이전 snapshot/generation/
verified row를 update/delete하지 않는다.

Operation result status는 `published`, `deactivated`, `rolled_back`, `resumed`, `verified`다. 성공
public response는 이를 담은 authoritative terminal receipt이며 actor/reason, request hash,
expected/resulting state, outcome과 HTTP 의미를 함께 제공한다. 결정적 rejection은 safe public error로
반환하고 같은 terminal receipt를 lookup/history에서 확인할 수 있다. 동시 변경은 한쪽이 성공하고
다른 쪽은 conflict로 끝난다. Control DB transaction 안의 validation/SQL failure가 partial persisted
state를 남기면 안 된다. Commit 뒤 runtime apply 의미는 아래 acknowledgement 경계를 따른다.

### Persistence contract

- Source generation, metadata snapshot과 verified contract는 append-only/immutable이다.
- Canonical-time처럼 Control schema shape를 바꾸지 않는 global revision migration도 새 snapshot,
  generation과 verified row를 append하고 rollback 대상 old row를 보존한다.
- Active pointer만 명시된 transaction과 CAS 아래 변경한다.
- Numbered migration은 filename/checksum과 적용 이력을 immutable ledger에 남기고 advisory apply
  lock 아래 순서대로 실행한다. 과거 migration을 수정하거나 번호를 건너뛰지 않는다.
- Source publish는 snapshot 저장, generation 저장, metadata pointer와 source pointer를 하나의
  transaction으로 갱신한다.
- Rollback은 source pointer와 metadata pointer를 함께 바꾸고 metadata를 pin한다.
- `source_store.py`와 `metadata_store.py`는 같은 source에 정확히
  `pg_advisory_xact_lock(hashtextextended(source_id, 0))` key를 사용한다.
- Metadata refresh publish는 active source의 `control_generation`, `control_state_version`과
  `enabled`를 함께 확인해 이전 generation의 지연 refresh를 거부한다.
- 성공 mutation receipt는 source pointer 또는 verified contract와 같은 transaction에 commit한다.
  결정적인 validation/state rejection은 authority state를 바꾸지 않는 별도 transaction에 terminal
  receipt로 남긴다.
- FK, constraint와 trigger가 application validation을 보완하며 손상된 조합을 거부한다.

### Management catalog contract

Admin read surface는 source list/detail, immutable generation history, source mutation history와
idempotency-key receipt lookup을 제공한다. Projection은 owner, environment, DB migration reference,
effective budget, published/active metadata revision과 lifecycle state처럼 명시적으로 허용한 field만
반환한다. Raw manifest, encrypted credential, metadata snapshot, verified question/SQL과 expected
business value를 반환하지 않는다. Pagination/filter/order와 published-vs-active revision 의미는
public Delivery contract와 persisted projection contract다.

### Replica observation contract (`CTRL-06`)

Control Plane은 Runtime에 persistence-private row가 아니라 다음 public capability만 제공한다.

```text
ReplicaSourceObservation(
  source_id,
  applied_generation | null, applied_state_version | null, applied_enabled | null,
  applied_metadata_revision | null, source_health | null, reason_code | null
)

ReplicaObservationWriter.register_replica(replica_id, heartbeat_interval_ms) -> incarnation
ReplicaObservationWriter.report_replica(
  replica_id, incarnation, reason_code?, sources
)
```

`replica_id`는 1~80자의 lowercase stable slug이고 heartbeat interval은 5,000~300,000ms다. 같은
slot을 새 process가 등록하면 incarnation이 증가하며 이전 incarnation report는 거부된다. 한
process는 registration을 한 번만 수행하고 실패/fencing 뒤 재등록하지 않는다. Ever-registered
slot이 target set이며 이 단계에는 자동 expiry/delete/retirement와 shutdown deregistration이 없다.

Migration 3의 `runtime_replicas`와 `runtime_source_observations`는 replica 및 replica/source별
latest-only observation이다. Report는 DB clock으로 parent heartbeat와 source observation을 한
transaction에서 갱신한다. Global reason은 `CONTROL_SCAN_FAILED`, source reason은
`RUNTIME_VALIDATION_REJECTED|RUNTIME_APPLY_FAILED|METADATA_PROBE_FAILED`뿐이다. Global scan failure
report는 source tuple과 함께 저장하지 않는다. 공용 Control writer role의 observation-table
capability는 select/insert/update뿐이고 delete/truncate는 없다. 이 observation port를 통해 다른
authority table을 mutation하지 않는다.

Desired는 active source의 enabled/generation/state version과 active metadata revision에서 읽는다.
Disabled desired에는 metadata revision이 없고 metadata/source-health drift를 계산하지 않는다.
Freshness는 같은 query의 DB read clock과 `observed_at + 3 * heartbeat_interval_ms`로 계산한다.
`read_at == fresh_until`은 fresh이고 그 이후 `stale_age_ms`는 millisecond ceil이다.

`SourceAdminService.source_replicas(source_id, limit=50, after_replica_id=None)`는
`replica_id` C-collation 오름차순 exclusive keyset page를 제공한다. `limit`은 1~100이고 응답은
다음 exact shape다.

```text
source_id
desired: enabled, generation, state_version, metadata_revision | null
replicas[]:
  replica_id
  status: pending | available | stale | unavailable
  source_health: initializing | healthy | stale | unavailable | null
  applied: null | {enabled, generation, state_version, metadata_revision | null}
  drift: not_applied | enabled | generation | state_version | metadata_revision
  observed_at, fresh_until, stale_age_ms
  reason_code: NOT_OBSERVED | HEARTBEAT_EXPIRED | CONTROL_SCAN_FAILED |
               RUNTIME_VALIDATION_REJECTED | RUNTIME_APPLY_FAILED |
               METADATA_PROBE_FAILED | null
next_after_replica_id: replica_id | null
```

`drift`는 표시한 순서로만 조립한다. Heartbeat expiry가 다른 reason보다 우선하며 known source의
stale/unavailable observation은 조회 자체의 실패가 아니다. Unknown source는 404, Control read나
projection 실패는 내부 정보를 숨긴 503으로 매핑한다. Existing source list/detail/history,
health/metrics와 MCP response는 바꾸지 않는다.

### Resource and gateway observation baseline (`CTRL-07A`, implemented)

`CTRL-07A`가 도입한 persistence-private row 격리와 bounded sample/delta 의미는 아래와 같다. 당시의
resource writer signature는 `CTRL-08`에서 generation/failure fencing을 추가한 현재 signature로
대체됐으므로 소비자는 다음 subsection의 resource contract만 사용한다. Gateway writer signature는
그대로다.

```text
GatewayUsageWriter.report_gateway_usage(replica_id, incarnation, sequence, deltas)
```

Resource sample은 `representative_records|table_bytes|index_bytes|total_storage_bytes`와 exact
unit/method/definition revision/value만 허용한다. Migration 4의
`source_resource_observations`는 `(source_id, metric)` 네 row 이하에서 UTC daily current와 comparable
previous만 저장한다. Same bucket은 current만 교체하고 method/definition 변경은 previous를 지운다.
Source별 transaction advisory lock으로 여러 replica 보고를 직렬화한 뒤 DB clock observed time과
72시간 freshness를 사용하며 failure/missing은 기존 값을 0으로 덮지 않는다.

Gateway delta는 trusted `source_id + budget_profile + metadata_revision + definition_revision + UTC
hour` key와 fixed terminal counter/sum만 받는다. `gateway_usage_report_cursors`가 current replica
incarnation, sequence와 payload SHA-256을 fence/deduplicate하고 `gateway_usage_rollups`가 aggregate를
저장한다. Same sequence/hash는 exact no-op이며 different hash, gap과 fenced incarnation은 transaction
전체를 거부한다. Cursor가 없는 최초 보고도 replica ID별 transaction advisory lock으로 직렬화한다.
Reporter cursor freshness는 DB clock 180초다. 31일은 DB clock 기준
logical visibility/input window이므로 오래된/future input을 거부하고 `CTRL-08` read에서도 제외하되,
나이만으로 물리 삭제하지 않는다. Source당 최신 1,000행은 physical cap으로 유지한다.

Control writer는 resource/cursor table에 SELECT/INSERT/UPDATE, rollup에는 1,000행 cap 정리를 위한
DELETE까지 가진다. 다른 table의 DELETE/TRUNCATE와 schema ownership은 얻지 않는다. Rollup은
성공적으로 보고된 lower bound이며 observation write/cleanup 실패는 source authority, query data
plane, readiness, health, receipt와 shutdown 의미를 바꾸지 않는다. Public status/admin response는
아래 `CTRL-08` 계약만 제공한다.

### Resource and usage projection contract (`CTRL-08`, implemented)

Runtime resource write capability는 current generation fencing과 bounded failure를 추가한다.

```text
ResourceObservationWriter.report_resource_observations(
  source_id, generation, metadata_revision, samples
)
ResourceObservationWriter.report_resource_observation_failure(
  source_id, generation, METADATA_UNAVAILABLE | RESOURCE_READ_FAILED
)
```

Success batch는 `table_bytes|index_bytes|total_storage_bytes`가 필수이고
`representative_records`만 optional이다. Additive migration 5의
`source_resource_observation_attempts`는 source당 latest attempt outcome/reason/DB time,
last-success time과 optional representative presence만 저장한다. 같은 generation failure는 기존
last success를 보존하고 다른 generation failure는 이를 초기화한다. Success sample과 attempt는
resource advisory lock, active-row lock, current enabled generation 및 active metadata 확인 아래 같은
transaction/DB clock으로 기록한다. 지연된 generation/revision report는 전체 거부한다.

`SourceAdminService.source_usage(source_id)`는 한 DB read clock/snapshot에서 최대 1,000 rollup과
bounded resource projection을 읽고 다음 exact application result를 제공한다.

```text
source_id, enabled, read_at
resource:
  status, reason_code, last_attempt, fresh_until, metrics[]
gateway:
  status, reason_code, last_report_at, fresh_until, lower_bound=true,
  window_start, window_end, rollups[]
monetary_cost:
  status=not_configured, reason_code=PROVIDER_NOT_CONFIGURED, last_attempt=null
```

`last_attempt`는 nullable `{attempted_at,outcome,reason_code}`이고 metric은 method/definition revision,
current value/metadata revision/daily bucket/observed/fresh time과 nullable comparable previous를 가진다.
Metric order는 `representative_records|table_bytes|index_bytes|total_storage_bytes`다. Current generation
success가 없으면 empty이며 missing/failed를 0으로 만들지 않는다. Fresh last success가 있으면 latest
attempt 실패와 무관하게 `available`, 만료되면 `stale`, 성공 없이 실패하면 `unavailable`이다.

Resource status reason은 다음 exact set이다.

```text
NOT_CONFIGURED | NOT_OBSERVED | SOURCE_DISABLED |
METADATA_UNAVAILABLE | RESOURCE_READ_FAILED | OBSERVATION_INCOMPLETE |
OBSERVATION_EXPIRED | null
```

Status 우선순위는 observability 미설정 `not_configured/NOT_CONFIGURED`, configured disabled
`unavailable/SOURCE_DISABLED`, current-generation attempt 없음 `pending/NOT_OBSERVED`, last success 없는
실패 `unavailable/<attempt reason>`, 불완전 success marker, fresh success `available/null`, expired success
`stale/OBSERVATION_EXPIRED` 순서다. Disabled source는 current-generation `last_attempt`만 유지하고
metrics는 empty, `fresh_until`은 null이다. `read_at == fresh_until`은 fresh다.

Gateway status는 source traffic completeness가 아니라 global reporter pipeline health다. 같은 read
clock에서 heartbeat가 fresh한 live replica만 계산하고 current incarnation cursor를 요구한다. 모든
live cursor가 fresh하면 `available`; 하나라도 absent/expired면 startup grace 없이
`unavailable/REPORTER_UNAVAILABLE`; live replica 없이 과거 accepted cursor가 있으면
`stale/REPORTER_EXPIRED`; 둘 다 없으면 `pending/NOT_REPORTED`다. Stale ever-registered replica를
계산에서 제외해도 row를 delete/retire하지 않는다. `last_report_at`은 latest accepted cursor 시각이다.

Gateway row는 DB clock 기준 inclusive
`[UTC-hour(read_at)-31 days, UTC-hour(read_at)]`만 보이며 나이만으로 삭제하지 않는다. Source당
physical 1,000행 cap 안의 row를 `bucket_start DESC, observed_at DESC, budget_profile C ASC,
metadata_revision ASC, definition_revision ASC`로 모두 반환한다. Pagination과 gap-to-zero 합성은 없다.
1,000행 초과와 malformed persisted field/type/cardinality는 fail-closed read failure다. Decode 가능한
success marker와 mandatory/optional sample 또는 freshness가 서로 맞지 않으면 정상 200 projection의
`unavailable/OBSERVATION_INCOMPLETE`로 반환한다. Provider billing이 없으므로 monetary
amount/currency/provenance는 공개 계약에 만들지 않는다.

Delivery는 이 application result만 소비하고 table/private DTO를 읽지 않는다. Unknown source는
`SourceNotFoundError`, DB/decode/cardinality 오류는 `SourceControlUnavailableError`이며 stale 또는
unavailable observation 자체는 오류가 아니다. Writer role은 attempt table의 SELECT/INSERT/UPDATE만
가지며 code rollback은 migration/table/data를 보존한다.

### Runtime projection contract

Control DB commit은 desired-state 원자성을 보장하지만 모든 process의 in-memory 적용까지 하나의
분산 transaction으로 만들지는 않는다. 각 replica의 `SourceReloader`가 polling으로 수렴한다.
SQL policy/metadata revision을 함께 바꾸는 cutover에서는 old fleet를 완전히 drain한 뒤 route 밖의
new fleet에서 source별 L1→all verified→L2를 완료하고, replica convergence 뒤에만 route한다.
`SourceReloader`는 Source Catalog의 `SourceProjectionWriter`를 받아 read와 `upsert/remove` capability를
함께 소비하는 유일한 runtime projector다.
Pool/cache adapter에는 Control Plane 소유의 작은 `SourcePoolInvalidator.invalidate(source_id)` port만
요구한다. Runtime이 provider-owned composite lifecycle을 검증하고 catalog, query executor 순서로 두
invalidator를 주입하며 Control Plane은 그 composite Protocol을 소유하거나 optional하게 탐색하지 않는다.

Replica 적용 순서는 다음과 같다.

```text
stored state 검증 -> old source pools invalidate -> registry projection 교체/제거
-> metadata cache invalidate -> source probe/health 갱신
```

낮은 state version, 같은 version의 다른 payload, connection identity rebind와 검증 불가능한
revision은 적용하지 않는다. Control DB가 일시적으로 unavailable이어도 이미 적용된 data plane은
안전한 기존 state로 계속 동작하되 management operation은 실패한다.

### Administration acknowledgement contract

Public mutation은 caller가 고른 UUID idempotency key와 같은 semantic request에 대해 하나의 terminal
receipt를 authority로 사용한다. Same key/same canonical request는 staging이나 state transition을
반복하지 않고 기존 terminal outcome을 재현한다. Success는 기존 receipt를 반환하고 rejection은
같은 safe error를 다시 반환하며 receipt lookup으로 상세 outcome을 확인한다. Same key/different
request는 conflict다. Credential과 verified question/SQL을 포함한 canonical envelope는 keyed
HMAC으로만 식별하고 raw input이나 일반 digest를 audit에 저장하지 않는다.

Receipt table은 terminal-only이므로 lookup 404는 실패가 아니라 아직 staging/in-flight일 수 있다.
Timeout 뒤에는 같은 key의 receipt, source detail과 mutation history를 bounded하게 확인하고 blind
retry하지 않는다. Receipt가 없고 expected state가 그대로임을 재확인한 경우에만 같은 key와 같은
semantic request를 한 번 재전송할 수 있다.

Source pointer/contract와 성공 receipt가 commit된 뒤 같은 process의 `SourceReloader.apply`가 실패해도
receipt는 authoritative desired-state 성공이다. Apply failure는 component health를 unavailable로
표시하고 polling convergence에 맡기며 성공 receipt를 503으로 뒤집지 않는다. Receipt 성공과 모든
replica의 in-memory 적용 완료를 같은 distributed transaction으로 해석하지 않는다.

### Credential contract

Credential은 plaintext로 Control DB에 저장하지 않는다. Current persisted format은 AES-256-GCM,
32-byte key, 12-byte nonce와 다음 exact ASCII associated data를 사용한다.

```text
query-man/source/{source_id}/generation/{generation}
```

다른 source/generation으로 ciphertext를 옮겨 복호화할 수 없다. Algorithm, key/nonce size와 AAD
bytes 변경은 기존 ciphertext migration 없이는 호환되지 않는다. Plaintext는 validation/runtime
connection 구성의 필요한 범위를 넘어 log나 response에 남지 않는다.

## 소비 계약

- [Source Catalog](../source-catalog/README.md)의 strict manifest validator, budget와
  `SourceProjectionWriter`; 검증된 runtime profile graph는 immutable하고 projector에는 새 profile을
  통째로 전달한다.
- [Metadata](../metadata/README.md)의 candidate preparation, quality gate, store port와 snapshot codec.
  Codec은 immutable Python graph와 기존 Control DB JSON array/object 사이를 변환한다.
- [Guarded Query](../guarded-query/README.md)의 validated execution for verified publish
- [Assurance](../assurance/README.md)의 verified DTO, exact revision/relation와 result hash contract
- [Runtime](../runtime/README.md)의 operational state reporting contract

Runtime은 polling schedule과 production lifecycle을 호출하고 Delivery는 authenticated operator 및
trusted tenant를 확인한 뒤 administration use case를 호출한다. 이 caller obligation은 Control
Plane이 Delivery/Runtime private implementation에 의존한다는 뜻이 아니다.

## 불변조건

- 같은 `source_id`를 다른 host/port/database/user/TLS/environment identity로 재사용하지 않는다.
- Persisted generation/snapshot/verified history를 update 또는 delete하지 않는다.
- Metadata snapshot JSON의 array/object shape나 revision을 Python tuple representation 변화와 함께
  바꾸지 않는다.
- Source-scoped lock와 generation/state-version CAS 없이 active state를 변경하지 않는다.
- Source publish/rollback의 source와 metadata pointer를 서로 다른 transaction으로 나누지 않는다.
- Pin된 metadata를 암묵적으로 덮어쓰거나 rollback 뒤 자동 resume하지 않는다.
- Success receipt와 authority mutation을 서로 다른 transaction으로 나누거나 terminal receipt를
  update/delete하지 않는다.
- Idempotency hash, receipt와 management projection에 credential, raw manifest, question/SQL 또는
  expected business literal을 남기지 않는다.
- Replica report/projection에 manifest, credential, connection, question/SQL, raw 오류 또는
  Runtime timestamp를 남기지 않는다.
- Ever-registered replica slot을 TTL, report failure 또는 shutdown으로 자동 삭제하거나
  재등록 loop로 incarnation ownership을 경합시키지 않는다.
- Credential, SQL, question, expected literal과 내부 Control DB 오류를 일반 log/response에 노출하지 않는다.
- Control writer는 최소 권한 role이며 application owner나 reader role을 재사용하지 않는다.
- Runtime apply가 실패하면 desired state를 성공한 것처럼 process-local health에 표시하지 않는다.

## 모듈 내부 변경

다음은 state transition, schema와 외부 결과 의미를 보존할 때 독립적으로 변경할 수 있다.

- 같은 transaction을 생성하는 store query/helper 정리
- Lock/CAS 의미를 유지하는 connection/pool bookkeeping 개선
- Ciphertext format과 associated data를 유지하는 crypto wrapper 내부 정리
- 동일한 적용 순서와 오류를 만드는 reloader loop/helper 개선
- 같은 bounded field/order를 만드는 management projection query 정리
- Canonical request/receipt 의미를 보존하는 mutation orchestration 정리
- Public result shape를 바꾸지 않는 administration orchestration 정리

## 사용자 승인이 필요한 계약 변경

- `control` schema, migration ledger/checksum, constraint, trigger, role/grant 또는 migration 순서 변경
- Immutable history, FK, advisory lock, generation/state CAS와 transaction atomicity 변경
- Publish/rotate/deactivate/rollback/pin/resume 상태 전이 또는 결과/error 의미 변경
- Idempotency key, canonical request hash, actor/reason, expected/resulting state, terminal receipt,
  replay/conflict와 timeout reconciliation 의미 변경
- Management list/detail/history/receipt field, pagination/filter/order 또는 redaction 변경
- Replica identity/target set, registration fencing, observation field/reason, freshness, status/drift,
  pagination, retention 또는 writer privilege 변경
- Resource attempt generation/outcome/reason/last-success, usage status 산식, 31일 read window,
  1,000행 response/order와 monetary-cost placeholder 의미 변경
- Credential algorithm, key source, nonce/ciphertext format, associated data와 redaction 경계 변경
- Source connection/environment identity 재지정 또는 기존 generation mutation 허용
- Mutually exclusive bootstrap/managed authority, managed filesystem non-read/fallback와 replica
  convergence 의미 변경
- Pool/registry/metadata invalidation 및 health 적용 순서 변경
- Verified query persistence key, exact revision/result contract 변경
- `CONTROL_SEQUENCE_MAX`, public administration input의 field/frozen shape 또는
  `publish_verified_query` argument 의미 변경

승인 요청에는 기존 persisted data migration, rolling replica compatibility, rollback과 복구 절차,
Source Catalog/Metadata/Query/Delivery/Runtime/Assurance 영향을 포함한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_source_admin.py tests/test_secrets.py \
  tests/test_managed_mode.py
```

Persistence tests는 기본 pytest marker에서 제외되므로 다음을 별도로 실행한다.

```text
uv run pytest -m integration tests/test_source_store.py tests/test_metadata_store.py \
  tests/test_control_migrations.py tests/test_control_startup.py \
  tests/test_control_recovery.py
```

Schema, transaction, lock/CAS 또는 runtime projection 경계를 바꾸면 전체 integration gate와 관련
control-plane 복구 절차를 실행한다. 격리 Control recovery fixture는 13-table fingerprint,
encryption-key decrypt, logical retention, zero-bootstrap와 두 replica convergence를 같은
scenario에서 확인한다. 물리적 production host/network, source business DB와 실제 RPO/RTO를
대신하지 않는다. 완료 전 root `AGENTS.md`의 전체 gate도 실행한다.
Public administration input 또는 verified mapping을 바꾸면 Delivery `test_http.py`,
[`test_documentation.py`](../../../tests/test_documentation.py)의 import guard와
`test_control_startup.py`도 함께 실행한다.

## 집중해서 읽을 범위

Control Plane 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 admin/reloader/store/secret code, numbered migration과 focused tests
3. Source validator, MetadataStore/codec, verified/query의 소비 계약
4. [ADR 0012](../../decisions/0012-control-plane-source-revisions.md),
   [ADR 0013](../../decisions/0013-control-plane-verified-query-publishing.md),
   [ADR 0016](../../decisions/0016-centralized-source-management-plane.md)과
   [ADR 0017](../../decisions/0017-shared-source-access-and-resource-tier.md) 중 변경과 직접 관련된 결정
5. 변경되는 management catalog/mutation의 Delivery와 Runtime 계약

Metadata relevance algorithm, MCP SDK 내부와 query cursor 구현은 계약을 바꾸지 않는 한 읽을
필요가 없다.
