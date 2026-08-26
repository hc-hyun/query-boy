# Control Plane Managed Observability Reference

Status: `CTRL-06`, `CTRL-07A` and `CTRL-08` implemented; inactive in ADR 0025 first launch

이 문서는 managed replica convergence, source resource observation, gateway usage와 operator projection의
정확한 interface, persisted format, freshness/status 산식과 privacy boundary를 보존한다. 이 capability는
구현되어 있지만 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의 static first launch에는
Control DB와 reporter를 조립하지 않는다.

작업 시작점과 ownership은 [Control Plane README](README.md)다. Accepted authority는
[ADR 0016](../../decisions/0016-centralized-source-management-plane.md)의 `CTRL-06`, `CTRL-07A`,
`CTRL-08` 범위다. Observation은 source desired state, query result, readiness, mutation receipt와 shutdown
성공을 바꾸지 않는 best-effort operational projection이다.

## 한눈에 보는 세 관측

| 관측 | 답하는 질문 | 저장 방식 |
|---|---|---|
| Replica (`CTRL-06`) | 각 managed replica가 desired generation/revision을 적용했는가? | Stable slot과 replica/source별 latest-only row |
| Resource (`CTRL-07A` + `CTRL-08`) | DB owner가 지정한 relation의 record/storage estimate가 얼마나 되고 마지막 시도는 성공했는가? | Source/metric별 daily current/comparable previous와 source별 latest attempt |
| Gateway (`CTRL-07A` + `CTRL-08`) | Query Man이 성공적으로 보고한 bounded terminal usage가 얼마나 되는가? | Source/profile/revision/hour lower-bound rollup과 replica reporter cursor |

이 값들은 billing 원장, source traffic completeness, exact row count 또는 source authority가 아니다.
Missing/failure를 0으로 만들지 않으며 credential, relation 이름, caller/tenant, question/SQL와 raw error를
저장·공개하지 않는다.

## Public writer interface

Runtime은 persistence-private table/row가 아니라 `source_admin.py`의 다음 Python interface만 소비한다.
모든 method는 async다.

```text
ReplicaSourceObservation(
  source_id: str,
  applied_generation: int | None,
  applied_state_version: int | None,
  applied_enabled: bool | None,
  applied_metadata_revision: str | None,
  source_health: initializing | healthy | stale | unavailable | None,
  reason_code:
    RUNTIME_VALIDATION_REJECTED |
    RUNTIME_APPLY_FAILED |
    METADATA_PROBE_FAILED |
    None
)

ReplicaObservationWriter:
  register_replica(
    replica_id: str,
    heartbeat_interval_ms: int
  ) -> int  # incarnation

  report_replica(
    replica_id: str,
    incarnation: int,
    *,
    reason_code: CONTROL_SCAN_FAILED | None,
    sources: tuple[ReplicaSourceObservation, ...]
  ) -> None
```

```text
ResourceObservationSample(
  metric:
    representative_records | table_bytes | index_bytes | total_storage_bytes,
  value: int,
  unit: rows | bytes,
  method: postgres_catalog_estimate | postgres_relation_size,
  definition_revision: str
)

ResourceObservationWriter:
  report_resource_observations(
    source_id: str,
    generation: int,
    metadata_revision: str,
    samples: tuple[ResourceObservationSample, ...]
  ) -> None

  report_resource_observation_failure(
    source_id: str,
    generation: int,
    reason_code: METADATA_UNAVAILABLE | RESOURCE_READ_FAILED
  ) -> None
```

```text
GatewayUsageDelta(
  source_id: str,
  budget_profile: str,
  metadata_revision: str,
  definition_revision: str,
  bucket_start: datetime,
  query_count: int,
  success_count: int,
  rejected_count: int,
  timeout_count: int,
  overloaded_count: int,
  cancelled_count: int,
  failed_count: int,
  queue_ms_sum: int,
  elapsed_ms_sum: int,
  returned_rows_sum: int,
  result_bytes_sum: int,
  truncated_count: int
)

GatewayUsageWriter:
  report_gateway_usage(
    replica_id: str,
    incarnation: int,
    sequence: int,
    deltas: tuple[GatewayUsageDelta, ...]
  ) -> None
```

Python shape와 호출 단위 error 의미가 module interface다. 아래 identity, retention, freshness, status,
ordering과 failure isolation은 별도 policy/persisted/safety boundary다.

## Replica observation (`CTRL-06`)

### Identity와 registration fencing

- `replica_id`는 `^[a-z0-9]+(?:-[a-z0-9]+)*$` 형식의 1~80자 stable managed deployment slot이다.
- Bootstrap process는 `QUERY_MAN_REPLICA_ID`가 있어도 읽거나 검증하지 않는다.
- Heartbeat interval은 5,000~300,000ms이며 Runtime은 실제
  `max(source reload interval, 5_000ms)`를 등록한다.
- 같은 slot을 새 process가 등록하면 monotonic incarnation이 증가하고 이전 incarnation report를
  fencing한다.
- 한 process는 registration을 한 번만 수행한다. Registration/report failure나 fencing 뒤 재등록 loop를
  만들지 않는다.
- Ever-registered slot이 target set이다. 자동 expiry/delete/retirement와 shutdown deregistration은 없다.
  Scale-down slot은 stale로 남고 never-started planned target은 외부 deployment inventory가 관리한다.

### Persistence와 report

Migration 3의 `runtime_replicas`와 `runtime_source_observations`는 replica 및 replica/source별
latest-only state다. Report는 Control DB clock으로 parent heartbeat와 source observation을 한 transaction에
갱신한다.

Global report reason은 `CONTROL_SCAN_FAILED` 하나다. 이 report는 source tuple과 함께 저장하지 않는다.
Source reason은 `RUNTIME_VALIDATION_REJECTED`, `RUNTIME_APPLY_FAILED`, `METADATA_PROBE_FAILED`뿐이다.
Manifest, credential, connection, question, SQL, Runtime timestamp와 raw error는 payload에 넣지 않는다.

Observation writer role은 두 table의 SELECT/INSERT/UPDATE만 가지며 DELETE/TRUNCATE나 source/metadata
authority mutation capability가 없다. Report 실패는 query data plane, readiness, mutation receipt,
existing source health와 shutdown을 바꾸지 않는다.

### Desired state와 freshness

Desired는 active source pointer의 enabled/generation/state version과 active metadata pointer에서 매 조회
계산한다. Disabled desired에는 metadata revision이 없고 metadata/source-health drift를 계산하지 않는다.

Freshness는 Runtime clock이 아니라 같은 query의 Control DB `read_at`을 사용한다.

```text
fresh_until = observed_at + 3 * heartbeat_interval_ms
```

`read_at == fresh_until`은 fresh다. 그 이후 `stale_age_ms`는 elapsed milliseconds의 ceil이다.
Heartbeat expiry는 stored failure reason보다 우선한다.

### `source_replicas` application result

```text
SourceAdminService.source_replicas(
  source_id,
  limit=50,
  after_replica_id=None
)
```

`limit`은 1~100이다. `replica_id` C-collation 오름차순 exclusive keyset page를 반환한다.

```text
source_id
desired:
  enabled, generation, state_version, metadata_revision | null
replicas[]:
  replica_id
  status: pending | available | stale | unavailable
  source_health: initializing | healthy | stale | unavailable | null
  applied: null | {
    enabled, generation, state_version, metadata_revision | null
  }
  drift:
    not_applied | enabled | generation | state_version | metadata_revision
  observed_at, fresh_until, stale_age_ms
  reason_code:
    NOT_OBSERVED | HEARTBEAT_EXPIRED | CONTROL_SCAN_FAILED |
    RUNTIME_VALIDATION_REJECTED | RUNTIME_APPLY_FAILED |
    METADATA_PROBE_FAILED | null
next_after_replica_id: replica_id | null
```

`drift`는 표시한 순서로만 조립한다. Scan/source failure는 `unavailable`, 충분한 applied state가 아직
없으면 `pending`, fresh하고 필요한 state가 있으면 `available`이다. Heartbeat expiry 뒤에는 `stale`이다.
Known source의 stale/unavailable observation은 조회 실패가 아니어서 application result를 반환한다.
Unknown source는 `SourceNotFoundError`, Control read/projection failure는
`SourceControlUnavailableError`다.

Delivery의 operator-only HTTP projection은 다음 path를 사용한다.

```text
GET /admin/sources/{source_id}/replicas?limit&after_replica_id
```

Authentication/authorization, query validation, status와 wire serialization은 Delivery 소유다. Existing
source list/detail/history, `/health`, `/ready`, `/admin/metrics`와 MCP inventory는 바뀌지 않는다.

## Resource observation (`CTRL-07A` + `CTRL-08`)

### Source definition과 collection

Strict manifest v2의 optional `observability`는 다음을 지정한다.

- Representative record grain과 exact physical relation 하나
- Representative relation을 포함하는 1~16개 distinct physical storage relation
- 같은 database의 system schema가 아닌 ordinary table 또는 materialized view

이 relation/grain은 DB owner가 제공한 bounded observation target이다. Query relation allowlist, metadata
revision material과 public source summary가 아니다. Runtime provider는 exact 대상만 조회하며 대상 전체를
열거하지 않는다. Existing catalog의 max-one reader pool, read-only transaction과 metadata timeout을
재사용한다. 일반 view `COUNT(*)`, caller-provided SQL과 `EXPLAIN ANALYZE`를 실행하지 않는다.

Allowed V1 metric은 다음 네 개다.

| Metric | Unit | Method |
|---|---|---|
| `representative_records` | rows | `postgres_catalog_estimate` |
| `table_bytes` | bytes | `postgres_relation_size` |
| `index_bytes` | bytes | `postgres_relation_size` |
| `total_storage_bytes` | bytes | `postgres_relation_size` |

`representative_records`는 non-negative `pg_class.reltuples`의 rounded estimate다. Relation 이름, OID,
catalog row와 SQL은 Control payload에 넣지 않는다. `observability` definition 변경은 새 immutable source
generation을 만들지만 metadata revision 재료가 아니며 observation 값도 generation/revision을 만들지
않는다.

Metric별 `definition_revision`은 metric, method, grain/relation 목록과
`database_migration_ref`의 canonical SHA-256이다. 다른 method/definition 값을 같은 growth series로
비교하지 않는다.

### Current/previous sample persistence

Migration 4의 `source_resource_observations`는 `(source_id, metric)` 네 row 이하에서 UTC daily current와
comparable previous만 저장한다.

- 같은 daily bucket report는 current만 교체하고 previous를 밀지 않는다.
- Method/definition이 바뀌면 previous를 null로 초기화한다.
- Control DB clock의 `observed_at`과 `fresh_until = observed_at + 72 hours`를 사용한다.
- 이 freshness는 catalog read 시각이며 PostgreSQL `ANALYZE` 정확성을 보장하지 않는다.
- Failure/missing은 기존 success를 0으로 덮지 않는다.

Source별 transaction advisory lock으로 여러 replica 보고를 직렬화한다. Success는
`table_bytes`, `index_bytes`, `total_storage_bytes`를 모두 요구하고 `representative_records`만 optional이다.
Active-row lock 아래 current enabled generation과 active metadata revision을 확인한 뒤 samples와 attempt를
같은 DB clock/transaction에 기록한다. Delayed generation/revision report는 전체 거부한다.

### Latest attempt와 last success

Migration 5의 `source_resource_observation_attempts`는 source당 한 row에 다음만 저장한다.

- Current generation latest attempt outcome/reason/DB time
- Last successful attempt time
- 그 success에 optional representative estimate가 있었는지 여부

같은 generation refresh failure는 last success를 보존한다. 다른 generation failure는 이전 success
연결을 초기화한다. Control write 자체가 실패하면 raw exception을 attempt reason으로 저장하지 못하며
기존 값/freshness를 유지한다. Attempt writer는 SELECT/INSERT/UPDATE만 가지고 DELETE/TRUNCATE는 없다.

### Resource projection status

`source_usage`의 resource section은 다음 priority로 status를 계산한다.

1. Observability 미설정: `not_configured/NOT_CONFIGURED`
2. Configured source disabled: `unavailable/SOURCE_DISABLED`
3. Current-generation attempt 없음: `pending/NOT_OBSERVED`
4. Last success 없는 failure: `unavailable/METADATA_UNAVAILABLE|RESOURCE_READ_FAILED`
5. Success marker/sample 불일치: `unavailable/OBSERVATION_INCOMPLETE`
6. Fresh success: `available/null`
7. Expired success: `stale/OBSERVATION_EXPIRED`

`read_at == fresh_until`은 fresh다. Latest attempt가 실패했더라도 last success가 fresh하면 status는
`available`이고 failure는 `last_attempt`에 남는다. Current generation success가 없으면 metrics는 empty다.
Disabled source는 current-generation `last_attempt`만 유지하고 metrics는 empty, `fresh_until`은 null이다.

Success marker와 같은 observed time의 mandatory storage 세 row, optional-presence marker 또는 freshness가
서로 충돌하면 metrics를 공개하지 않고 `OBSERVATION_INCOMPLETE`로 fail-closed한다.

## Gateway usage (`CTRL-07A` + `CTRL-08`)

### Trusted rollup identity와 counters

Gateway delta key는 terminal event 시점의 trusted active source/profile/revision으로 만든다.

```text
source_id + budget_profile + metadata_revision +
definition_revision + UTC hour bucket_start
```

`query_count`는 `success|rejected|timeout|overloaded|cancelled|failed`의 합이다.

- Revision/policy, AST/allowlist, plan과 allowlisted user-SQL invalid는 rejected다.
- Queue/pool saturation은 overloaded다.
- Operator/disconnect/shutdown cancellation은 cancelled다.
- 성공 query만 queue/elapsed/returned rows/result bytes sum과 truncated count에 기여한다.
- Attribution 전 authentication failure, unknown source와 active revision read failure는 rollup에 넣지 않는다.

Caller, tenant, question, SQL, fingerprint, query/PG query ID, credential과 raw error는 저장하지 않는다.
이는 모든 replica의 완전한 traffic/billing 원장이 아니라 Control DB에 성공적으로 보고된 lower bound다.

### Reporter fencing과 persistence

Runtime reporter는 replica observation과 별도로 60초마다 최대 100 delta를 전송한다. Process-local pending
group은 1,000개로 제한하며 overflow는 오래된 group을 버려 lower-bound 의미를 유지한다.

Migration 4의 `gateway_usage_report_cursors`는 current replica incarnation, monotonic sequence와 canonical
payload SHA-256을 fence/deduplicate한다. Same sequence/hash replay는 exact no-op이다. Different hash,
sequence gap과 fenced incarnation은 transaction 전체를 거부한다. Cursor가 없는 최초 report도 replica
ID별 transaction advisory lock으로 직렬화한다. Cursor freshness는 Control DB clock 180초다.

`gateway_usage_rollups`는 fixed key와 terminal counters/success sums만 저장한다. Source당 최신 1,000행은
physical cap이다. Common writer는 cap을 넘긴 non-authoritative rollup row를 정리하도록 이 table에만
DELETE를 가진다. Cursor/resource/attempt, replica, receipt와 authority table에는 DELETE/TRUNCATE를 갖지
않는다.

31일은 DB clock 기준 logical visibility/input window다. Writer는 오래된/future bucket을 거부하고 read는
다음 inclusive window 밖 row를 제외한다.

```text
[UTC-hour(read_at) - 31 days, UTC-hour(read_at)]
```

나이만으로 row를 물리 삭제하지 않는다. Source당 최신 1,000행 cap만 물리 retention으로 유지한다.

### Global reporter status

Gateway status는 특정 source traffic completeness가 아니라 global managed reporter pipeline health다.
같은 Control DB read clock에서 heartbeat가 fresh한 replica만 live set에 포함하고 cursor incarnation이
current replica incarnation과 일치해야 한다.

| 상태 | 조건 |
|---|---|
| `available` | Live replica가 하나 이상이고 모든 current cursor가 fresh |
| `unavailable/REPORTER_UNAVAILABLE` | Live replica의 current cursor가 하나라도 absent/expired; startup grace 없음 |
| `stale/REPORTER_EXPIRED` | Live replica는 없지만 과거 accepted cursor가 있음 |
| `pending/NOT_REPORTED` | Live replica와 accepted cursor 모두 없음 |

Ever-registered stale replica는 live-set 계산에서만 제외하고 row를 delete/retire하지 않는다.
`last_report_at`은 Control DB가 받아들인 가장 최근 cursor 시각이며 관측할 수 없는 failed attempt 시각이
아니다.

## `source_usage` application result (`CTRL-08`)

```text
SourceAdminService.source_usage(source_id) -> dict[str, object]
```

한 Control DB read clock/snapshot에서 최대 1,000 rollup과 bounded resource projection을 읽는다.

```text
source_id, enabled, read_at

resource:
  status, reason_code, last_attempt, fresh_until, metrics[]

gateway:
  status, reason_code, last_report_at, fresh_until, lower_bound=true,
  window_start, window_end, rollups[]

monetary_cost:
  status=not_configured,
  reason_code=PROVIDER_NOT_CONFIGURED,
  last_attempt=null
```

Resource `last_attempt`은 null 또는 `{attempted_at,outcome,reason_code}`다. Metric order는
`representative_records`, `table_bytes`, `index_bytes`, `total_storage_bytes`다. 각 metric은
method/definition revision, current value/metadata revision/daily bucket/observed/fresh time과 nullable
comparable previous만 가진다.

Gateway rollup은 다음 순서로 inclusive 31일 window의 최대 1,000행을 pagination 없이 모두 반환한다.

```text
bucket_start DESC, observed_at DESC,
budget_profile COLLATE C ASC,
metadata_revision ASC, definition_revision ASC
```

Gap/missing reporter/hour를 0으로 합성하지 않는다. Malformed persisted field/type/cardinality와 1,000행
초과는 fail-closed read failure다. Monetary provider가 없으므로 amount, currency, method와 provenance
field를 만들지 않는다.

Unknown source는 `SourceNotFoundError`, Control DB read/decode/cardinality 오류는
`SourceControlUnavailableError`다. Resource/gateway가 stale/unavailable인 known source는 application
조회 실패가 아니다.

Delivery는 다음 operator-only path에 이 application result를 투영한다.

```text
GET /admin/sources/{source_id}/usage
```

Query parameter는 받지 않는다. Authentication/authorization을 path/query validation보다 먼저 한다.
HTTP status와 exact serialization은 Delivery 소유다. Existing admin list/detail/history/replica/mutation,
`/admin/metrics`, query-facing HTTP와 MCP three-tool inventory는 바뀌지 않는다.

## Privacy, failure isolation과 non-goal

Observation payload, persisted row, response와 audit에는 다음을 넣지 않는다.

- Credential, token, connection endpoint와 raw manifest
- Observability relation/grain, relation OID와 catalog row
- Replica incarnation/cursor internals
- Caller/tenant, question/SQL, fingerprint와 query ID
- Raw driver/Control/source database error

Resource/gateway write, cleanup과 reporter failure는 source authority, query data plane, query result,
readiness, public health, receipt와 shutdown 결과를 바꾸지 않는다. Missing/failed sample이나 rollup gap을
0으로 만들지 않는다.

현재 baseline에는 DB-native statement usage, provider monetary billing, spike alert와 workflow trace가
없다. Parked research를 현재 schema/API로 복제하지 않는다. 추가하려면 module interface, persisted
format, policy, privacy, migration과 operational 영향을 각각 제안해 별도 승인받는다.

## 변경 중단 조건과 검증

다음 의미는 사용자 승인 없이 변경하지 않는다.

- Replica identity/target set, incarnation fencing, registration/report lifecycle와 writer privilege
- Observation table/field/reason, DB-clock freshness와 status/drift/order/pagination
- Resource definition/metric/method, generation fencing, attempt/last-success와 current/previous 비교
- Gateway key/counter, sequence/hash dedup, lower-bound, 31일 logical window와 1,000행 physical cap
- `source_replicas`/`source_usage` application result와 Delivery HTTP projection
- Privacy/redaction, missing-to-zero 금지와 failure isolation

Focused tests:

```text
uv run pytest tests/test_source_admin.py tests/test_managed_mode.py tests/test_http.py
uv run pytest -m integration tests/test_source_store.py tests/test_control_migrations.py \
  tests/test_control_startup.py tests/test_control_recovery.py
```

Evidence는 [runtime replica observations](../../verification/2026-08-25-runtime-replica-observations.md),
[resource and gateway observations](../../verification/2026-08-25-resource-and-gateway-observations.md),
[usage projection](../../verification/2026-08-25-usage-projection.md)과
[control recovery acceptance](../../verification/2026-08-25-control-recovery-acceptance.md)에 있다. 각
record는 해당 commit/fixture/command만 증명하며 현재 protected deployment를 자동으로 증명하지 않는다.
