# ADR 0016: Centralized Source Management Plane

Status: Accepted

Date: 2026-08-23

## Context

Control DB는 hot-added source의 immutable generation, encrypted credential, metadata snapshot과
active pointer를 이미 저장한다. 그러나 운영자는 source 전체 목록, 담당자, 변경 이력, replica
적용 상태, 데이터 규모와 비용 신호를 한곳에서 조회할 수 없다. Bootstrap YAML, process-local
metric과 외부 DB 상태가 흩어져 있어 개발자와 운영자가 분리되면 현재 상태를 재구성하기 어렵다.

Production source를 Git manifest와 Control DB에 동시에 기록하면 기준과 rollback 의미가
불명확해진다. 반대로 실제 업무 데이터, secret과 고빈도 raw metric까지 Control DB로 복사하면
책임과 저장 비용이 커진다.

초기 운영자는 한 종류의 admin이고, query 사용자는 모두 같은 active source 목록과 source별
resource tier를 사용한다. 아직 필요하지 않은 다단계 RBAC, caller grant, 사용자별 quota와
chargeback을 미리 설계하지 않는다.

## Decision

Runtime은 `QUERY_MAN_SOURCE_MODE=bootstrap|managed`로 source authority를 process startup에
한 번만 선택한다. 기본값은 local/CI용 `bootstrap`이며 production은 `managed`를 명시한다. 두
mode를 source별로 섞거나 Control DB 설정 유무로 자동 선택하지 않는다.

- `bootstrap`은 local/CI 전용이다. `config/sources/*.yaml`과 filesystem verified-query data만 읽고
  Control DSN과 source encryption key를 모두 거부한다.
- `managed`는 production authority다. Control DSN, source encryption key와 stable
  `QUERY_MAN_REPLICA_ID`를 모두 요구하고 빈 registry/verified map에서 Control DB lifecycle과
  verified-query data만 load한다. Source directory와
  filesystem verified-query data가 없거나 같은 `source_id`를 담아도 열거나 합치지 않는다.

Managed source의 canonical manifest generation, active/deactivated state, metadata revision과
verified-query record는 Control DB만 기준으로 삼는다. Lifecycle row가 없는 source는 managed mode에서
absent이며 file로 fallback하지 않는다. Repository YAML로 write-back하거나 양방향 동기화하지
않는다. `config/onboarding/*.yaml`은 deterministic fixture다.

Control Plane은 admin-only management surface 하나를 제공한다. 관리자 HTTP API와 그 위의 UI,
CLI는 같은 `source_id`로 다음 정보를 조회한다.

- 비밀이 제거된 source 정의, owner, environment와 DB migration provenance
- Desired generation/state와 replica별 applied generation/revision, health와 freshness
- Source가 선택한 effective `budget_profile`과 관련 metadata revision
- Actor, reason, request hash, expected/resulting state, outcome과 rollback history
- Representative record volume, storage, growth와 측정 방법/freshness
- Source/profile별 gateway 및 DB-native usage와 비용 projection 상태

실제 저장 위치는 책임에 따라 분리할 수 있다.

| Data | Authority |
|---|---|
| Source definition, lifecycle and mutation audit | Control DB |
| Curated view, reader role and grants | Source DB and its migration system |
| Encrypted reader credential | Control DB source generation |
| Plaintext reader credential and master key | Runtime/external secret system; never Git, response or audit |
| Admin and query authentication credential | External authenticator or versioned deployment configuration |
| Shared query access and source resource-tier rule | ADR 0017 and versioned platform configuration |
| Budget hard-limit template | Versioned Query Man platform configuration |
| High-frequency raw metrics and provider billing | Metrics/billing system |
| Unified sanitized projection | Control Plane management API |

Management은 단일 admin capability로 제한한다. Query credential은 admin API와 cancel에서
거부되고 query MCP에는 관리 tool을 추가하지 않는다. Viewer, source operator, approver,
platform administrator 역할 계층이나 Control DB role/source-scope binding은 현재 만들지 않는다.
관리자 mutation은 인증된 actor와 reason을 append-only audit에 남긴다. 조직에서 2인 승인이
필요해지는 시점에 별도 ADR로 승인 workflow를 추가한다.

Query access와 resource tier는
[ADR 0017](0017-shared-source-access-and-resource-tier.md)을 따른다. 모든 인증된 query
principal은 같은 active source 목록을 보고 한 source의 같은 `budget_profile` 정의를
공유한다. 따라서 Control DB caller-grant table, grant seed import/marker, user/organization tier
binding과 별도 `cost_tier`를 만들지 않는다. Stable caller/tenant identity와 source-native RLS는
audit와 row isolation을 위해 유지할 수 있지만 source visibility나 tier 선택에는 쓰지 않는다.

Configuration revision과 operational observation은 lifecycle을 분리한다. Row count, storage,
growth와 usage 수집은 source generation이나 metadata revision을 만들지 않는다. 측정값은 scope,
value/unit, method, definition revision, observed time와 freshness를 가진다. 기본 수집은 bounded
catalog/provider estimate를 사용하고 일반 view에 unrestricted `COUNT(*)` 또는
`EXPLAIN ANALYZE`를 실행하지 않는다. 다른 method나 definition revision의 값은 같은 growth
series로 비교하지 않는다.

Observation availability는 `not_configured`, `pending`, `available`, `stale`, `unavailable`을
구분한다. Last attempt와 bounded reason을 기록하며 missing/failed 값을 0으로 표시하지 않는다.
Provider billing이 없으면 통화 비용 대신 source/profile별 resource usage와 추세만 제공한다.
User/organization별 allocation과 chargeback은 현재 범위가 아니다.

### CTRL-07A resource and gateway observation boundaries

`CTRL-07A`의 implementation change set은 2026-08-25에 승인됐다. Strict source manifest v2는 optional
`observability` object를 additive하게 받는다. Configured object는 representative record grain과
physical relation 하나, 그리고 그 relation을 포함하는 1~16개의 distinct physical storage
relation을 지정한다. 대상은 system schema가 아닌 같은 database의 ordinary table 또는
materialized view다. 이 이름은 DB owner가 제공하는 observation definition이며 query relation allowlist나
public projection에 추가되지 않는다. Staging은 기존 reader로 exact 대상의 catalog estimate와
size 함수를 bounded하게 검증하고 새 reader/monitoring privilege를 요구하지 않는다.

V1 resource method는 `postgres_catalog_estimate`와 `postgres_relation_size`뿐이다. 일반 view의
`COUNT(*)`, caller-provided SQL과 `EXPLAIN ANALYZE`는 실행하지 않는다. Source-level metric은 다음
네 개로 고정하며 per-relation/index 이름을 Control observation dimension으로 저장하지 않는다.

```text
representative_records (rows)
table_bytes (bytes)
index_bytes (bytes)
total_storage_bytes (bytes)
```

`observability` definition 변경은 immutable source generation을 만들지만 metadata revision 재료는
아니다. 관측값도 generation/revision을 만들지 않는다. Metric별 `definition_revision`은 metric,
method, grain 또는 정렬된 relation 목록과 `database_migration_ref`의 canonical SHA-256이다.
Resource는 UTC daily bucket의 current와 같은 definition의 previous sample만 유지한다. 같은 날의
중복 보고는 previous를 밀지 않고, definition/method 변경은 previous를 null로 초기화한다. Runtime은
source apply 뒤 한 번, 이후 24시간마다 best-effort 수집하며 Control DB clock의 `observed_at`과
`fresh_until = observed_at + 72 hours`를 저장한다. 이는 catalog를 읽은 시각의 freshness이며
PostgreSQL `ANALYZE` 정확성 보장은 아니다. 실패나 missing을 0으로 쓰지 않는다.

Gateway usage는 trusted active source/profile/published revision을 확인한 query의 canonical terminal
event를 다음 key로 UTC hourly rollup한다.

```text
source_id + budget_profile + metadata_revision + definition_revision + bucket_start
```

`query_count`는 `success|rejected|timeout|overloaded|cancelled|failed`의 합이다. Revision/policy,
SQL allowlist, plan admission과 allowlisted user-SQL 오류는 rejected, queue/pool 포화는 overloaded,
operator/disconnect/shutdown 취소는 cancelled다. 성공 query만 `queue_ms_sum`, `elapsed_ms_sum`,
`returned_rows_sum`, `result_bytes_sum`과 `truncated_count`에 기여한다. Attribution 전에 실패한
authentication, unknown source와 active revision read failure는 rollup에 넣지 않는다. Caller,
tenant, question, SQL, query/fingerprint/PG query ID와 raw 오류도 저장하지 않는다. 이 값은 모든
replica의 완전한 청구 원장이 아니라 Control DB에 성공적으로 보고된 사용량의 lower bound다.

Managed Runtime은 CTRL-06 payload와 별개로 60초마다 최대 100 delta를 report한다. Pending group은
process당 1,000개로 제한하고 overflow는 오래된 group을 버려 lower-bound 의미를 유지한다.
Stable replica ID와 incarnation, monotonic sequence와 canonical payload SHA-256으로 fence/deduplicate한다.
Same sequence/hash replay는 no-op이고 different hash, sequence gap과 old incarnation은 거부한다.
Reporter cursor freshness는 Control DB clock의 180초다. 2026-08-25 추가 승인에 따라 31일은
물리 삭제 기한이 아니라 DB clock 기준 logical visibility/input window다. Writer는 window 밖의
오래된/future bucket을 거부하고, `CTRL-08` 조회는 31일 초과 row를 반환하지 않는다. 나이만으로
row를 삭제하지 않지만 source당 최신 1,000행 physical cap은 유지한다. Common Control writer는
이 cap을 넘긴 non-authoritative rollup row만 정리하도록 rollup table에만 DELETE를 가지며 authority,
receipt, replica observation, resource/cursor table에는 DELETE를 얻지 않는다.

Additive migration 4는 `source_resource_observations`, `gateway_usage_rollups`와
`gateway_usage_report_cursors`만 추가한다. Resource는 source당 최대 네 current/previous row,
rollup은 위 aggregate, cursor는 replica incarnation/sequence/hash/DB-clock freshness를 저장한다.
Observation failure는 query data plane, source authority, readiness, health, mutation receipt와 shutdown
성공 의미를 바꾸지 않는다. 기존 process당 Control connection budget 4를 유지하고 resource/gateway
write를 process 안에서 직렬화해 source store의 두 connection을 동시에 점유하지 않는다. Code
rollback은 migration ledger/table/data를 drop하지 않는다.

`CTRL-07A`는 새 HTTP/MCP endpoint와 availability status, last attempt/reason을 추가하지 않는다.
`not_configured|pending|available|stale|unavailable` 및 exact admin response는 아래 `CTRL-08`에서
별도 승인됐다. DB-native statistics, provider billing, monetary cost와 caller/tenant allocation은
계속 이 change set 밖이다.

### CTRL-08 usage and availability projection boundaries

`CTRL-08`의 implementation change set은 2026-08-25에 승인됐다. 기존 성공 resource 값과 latest attempt를 서로 다른
책임으로 유지한다. Additive migration 5의
`source_resource_observation_attempts`는 source당 한 row에서 current generation의 latest attempt
outcome/reason/DB-clock time과 last successful attempt time, 그 성공에 optional representative estimate가
있었는지만 저장한다. 기존 `source_resource_observations`가 값과 comparable previous를 계속 소유한다.
같은 generation의 refresh failure는 last success를 지우지 않고 generation이 바뀐 failure는 이전
success 연결을 null로 초기화한다.

Runtime이 소비하는 exact write capability는 다음과 같다.

```text
ResourceObservationWriter.report_resource_observations(
  source_id, generation, metadata_revision, samples
)
ResourceObservationWriter.report_resource_observation_failure(
  source_id, generation, reason_code
)
reason_code = METADATA_UNAVAILABLE | RESOURCE_READ_FAILED
```

Success는 `table_bytes|index_bytes|total_storage_bytes` 세 sample을 모두 요구하고
`representative_records`만 optional이다. Resource advisory lock과 active-row lock 아래 current enabled
generation을 확인하며 success는 current active metadata revision도 확인한다. Success samples, latest
attempt와 last-success marker는 같은 DB clock/transaction에 기록한다. 이전 generation 또는 revision의
지연 report는 전체 거부한다. Control write 자체가 실패하면 raw exception을 attempt reason으로 저장하지
못하며 기존 값, freshness와 safe log 경계를 유지한다.

Operator-only public read는 기존 detail/replica/metrics/MCP response를 확장하지 않고 다음 전용
endpoint 하나로 제공한다.

```text
GET /admin/sources/{source_id}/usage
```

Query parameter는 받지 않는다. 한 DB read clock/snapshot에서 `source_id`, `enabled`, `read_at`과 다음
세 section을 반환한다.

```text
resource:
  status, reason_code, last_attempt, fresh_until, metrics[]
gateway:
  status, reason_code, last_report_at, fresh_until, lower_bound=true,
  window_start, window_end, rollups[]
monetary_cost:
  status=not_configured, reason_code=PROVIDER_NOT_CONFIGURED, last_attempt=null
```

Resource `last_attempt`는 nullable `{attempted_at,outcome,reason_code}`다. Metric은 고정 순서
`representative_records|table_bytes|index_bytes|total_storage_bytes`이며 method/definition revision과
current value/metadata revision/bucket/observed/fresh time, nullable comparable previous만 공개한다.
Current generation last success가 없으면 metric은 empty이고 missing/failed value를 0으로 합성하지
않는다. 최신 attempt가 실패해도 last success가 fresh하면 resource는 `available`이고 failure는
`last_attempt`에 남는다. Last success가 만료되면 `stale`, last success 없이 실패했으면
`unavailable`이다. Exact status reason은 다음 bounded set이다.

```text
NOT_CONFIGURED | NOT_OBSERVED | SOURCE_DISABLED |
METADATA_UNAVAILABLE | RESOURCE_READ_FAILED | OBSERVATION_INCOMPLETE |
OBSERVATION_EXPIRED | null
```

`read_at == fresh_until`은 fresh다. Success marker와 같은 observed time의 mandatory storage 세 row가
없거나 optional-presence marker, observed/freshness가 서로 충돌하면
`unavailable/OBSERVATION_INCOMPLETE`와 empty metrics/null fresh time으로 fail-closed한다. Status 우선순위는
observability 미설정 `not_configured/NOT_CONFIGURED`, configured disabled
`unavailable/SOURCE_DISABLED`, current-generation attempt 없음 `pending/NOT_OBSERVED`, last success 없는
실패 `unavailable/<attempt reason>`, 불완전 success marker, fresh success `available/null`, expired success
`stale/OBSERVATION_EXPIRED` 순서다. Disabled projection은 current-generation `last_attempt`만 보존하고
metrics는 empty, `fresh_until`은 null이다.

Gateway status는 source별 traffic completeness가 아니라 global managed reporter pipeline의 상태다.
같은 read clock에서 `read_at <= replica observed_at + 3 * heartbeat interval`인 replica만 live이고,
cursor incarnation이 current replica incarnation과 같아야 current다. Live replica가 하나 이상이고
모든 current cursor가 fresh하면 `available`; 하나라도 없거나 expired면 startup grace 없이
`unavailable/REPORTER_UNAVAILABLE`이다. Live replica가 없고 accepted cursor가 있으면
`stale/REPORTER_EXPIRED`, accepted cursor도 없으면 `pending/NOT_REPORTED`다. Ever-registered stale
replica는 이 live-set 계산에서만 제외하며 delete/retire하지 않는다. `last_report_at`은 Control이
받아들인 가장 최근 cursor 시각이지 관측할 수 없는 failed attempt 시각이 아니다.

Rollup은 migration 4의 fixed key/counter만 반환하며 gap이나 missing reporter를 0으로 채우지 않는다.
한 source의 physical cap 1,000행을 한 snapshot에서 다음 순서로 전부 반환하고 pagination/cursor를
추가하지 않는다.

```text
bucket_start DESC, observed_at DESC, budget_profile COLLATE C ASC,
metadata_revision ASC, definition_revision ASC
```

Visible window는 DB clock 기준 양 끝을 포함한
`[UTC-hour(read_at) - 31 days, UTC-hour(read_at)]`다. Window 밖 row는 response에서 제외하지만 나이만을
이유로 삭제하지 않고 기존 source당 최신 1,000행 physical cap만 유지한다. Provider connector가
없으므로 monetary amount/currency/method/provenance field를 미리 만들지 않는다.

Authentication/authorization은 path/query validation보다 먼저 수행한다. Unknown source는 404,
Control read/decode/cardinality failure는 secret-free 503이며 관측된 stale/unavailable은 200이다.
Credential/token/connection, relation/grain, replica identity/cursor internals, caller/tenant,
question/SQL/fingerprint/query ID와 raw error는 response/audit에 넣지 않는다. Migration-first rolling
deploy를 사용하고 code rollback은 migration 5 table/ledger/data를 drop하지 않는다. Writer는 새
attempt table에 SELECT/INSERT/UPDATE만 가지며 DELETE/TRUNCATE는 갖지 않는다.

### CTRL-06 replica observation boundaries

`CTRL-06`의 implementation change set은 2026-08-25에 다음과 같이 확정했다. Managed process는
`QUERY_MAN_REPLICA_ID`를 필수로 받고 `^[a-z0-9]+(?:-[a-z0-9]+)*$` 형식의 1~80자 stable slot으로
사용한다. Bootstrap process는 이 값이 있더라도 읽거나 검증하지 않는다. 한 번 등록된 slot은
운영자가 보는 target set에 계속 남으며 이 단계에는 TTL 기반 자동 삭제, retirement 또는 정상
shutdown deregistration이 없다. 따라서 아직 시작하지 않은 planned replica는 외부 deployment
inventory의 책임이고, scale-down한 slot은 stale로 남는다.

Control DB의 additive migration 3은 `runtime_replicas`와
`runtime_source_observations`의 latest-only 상태만 저장한다. 새 registration은 같은 slot의
monotonic incarnation을 올리고 이전 process의 report를 fencing한다. Writer는 register/report에
필요한 select/insert/update 권한만 가지며 관측 row를 삭제하거나 authoritative source/metadata history를 바꾸지
못한다. Report는 manifest, credential, connection, 질문, SQL과 raw 오류를 포함하지 않는다.

Runtime은 `max(source reload interval, 5_000ms)`를 실제 heartbeat/report 간격으로 등록하고 같은
주기로 best-effort report한다. Freshness는 Runtime timestamp가 아니라 Control DB clock으로
`observed_at + 3 * heartbeat interval`까지이며 그 경계를 지난 첫 millisecond부터 stale다.
Registration/report 실패, fenced incarnation과 shutdown은 query data plane, readiness, mutation
receipt 또는 기존 source health 의미를 바꾸지 않는다. 같은 process는 report 실패 뒤
재등록하지 않는다.

Desired는 active source pointer의 enabled/generation/state version과 active metadata pointer에서
매 조회 시 계산한다. Desired가 disabled면 desired/applied metadata와 source health는 drift 판단에
사용하지 않는다. Runtime은 적용한 generation/state/enabled, 실제 cache에 적용된 metadata revision,
source health와 다음 bounded failure만 latest observation으로 보낸다.

```text
CONTROL_SCAN_FAILED
RUNTIME_VALIDATION_REJECTED
RUNTIME_APPLY_FAILED
METADATA_PROBE_FAILED
```

Operator-only 조회는 기존 list/detail/health/metrics/MCP를 바꾸지 않고 다음 전용 endpoint만
추가한다.

```text
GET /admin/sources/{source_id}/replicas?limit&after_replica_id
```

응답은 C-collation `replica_id` 오름차순 exclusive keyset page와 한 번의 DB read clock을 사용한다.
각 replica는 `pending|available|stale|unavailable`, nullable `source_health`, nullable `applied`,
고정 순서 `drift`(`not_applied` 또는 `enabled`, `generation`, `state_version`,
`metadata_revision`), `observed_at`, `fresh_until`, `stale_age_ms`와 다음 bounded reason만 공개한다.

```text
NOT_OBSERVED | HEARTBEAT_EXPIRED | CONTROL_SCAN_FAILED |
RUNTIME_VALIDATION_REJECTED | RUNTIME_APPLY_FAILED | METADATA_PROBE_FAILED | null
```

Heartbeat expiry가 다른 failure보다 우선하고, scan/source failure는 `unavailable`, 아직 충분한
applied state가 없으면 `pending`, 나머지는 `available`이다. Stale/unavailable은 관측 결과이지
HTTP 실패가 아니므로 알려진 source에는 200으로 반환한다. 이 identity, target-set, schema,
freshness, status/drift/reason 또는 lifecycle 의미의 변경은 별도 사용자 승인을 받는다.

기존 admin mutation endpoint를 재사용한다. 별도 change-request/approval table과 endpoint 대신
idempotency key, canonical request hash, expected generation/state, authoritative mutation receipt와
append-only lifecycle audit를 추가한다. Timeout 뒤에는 receipt/state를 조회해 reconcile하고
blind retry하지 않는다. 기존 staged validation, immutable generation, atomic pointer, rollback
검증과 credential redaction/encryption을 유지한다.

Receipt와 lifecycle event는 `source_mutation_receipts` 한 table의 immutable terminal row로
통합한다. 별도 pending/request/event table은 만들지 않는다. 성공 receipt는 source pointer 또는
verified-query state 변경과 같은 transaction에 commit하고 결정적인 validation/state rejection은
state를 바꾸지 않은 별도 transaction에 남긴다. Receipt가 없는 동안은 staging/in-flight일 수
있으므로 404를 실패로 해석하지 않는다. Same key/same canonical request는 기존 terminal 결과를,
same key/different request는 409를 반환한다. Actor는 인증 caller에서 파생하고 reason은 bounded
change reference다. Idempotency key, credential과 verified question/SQL을 포함한 canonical envelope는
메모리에서만 keyed HMAC하며 raw body나 일반 SHA digest를 저장하지 않는다. Key를 digest domain에
묶어 서로 다른 key의 low-entropy payload가 같은 digest로 관측되는 것을 막는다. Terminal row가
생기기 전 같은 key가 여러 replica에 동시에 도착하면 준비 I/O가 중복될 수 있지만 source/key lock은
authority와 receipt commit을 한 번으로 제한한다.

현재 plaintext credential을 받는 direct admin API는 trusted manual-admin boundary다. Plan-only
onboarding Skill은 이 API를 호출하거나 credential을 읽지 않는다. AI 또는 다른 자동화에
production mutation 권한을 주기로 결정할 때만 target-bound credential broker, plan-ID apply와
별도 threat model을 선행한다.

기존 bootstrap source의 일회성 이관은 startup importer가 아니라 traffic 밖의 managed instance와
기존 admin API를 사용한다. Source를 L0/L1로 staged publish하고 현재 revision의 reviewed
verified-query record를 Control DB에 publish한 뒤 L2로 승격한다. Coverage 확인 뒤 serving replica를 managed
mode로 재시작한다. Import marker, seed digest, bulk import endpoint와 filesystem write-back은
만들지 않는다.

Control DB migration은 versioned이며 production, development와 disposable integration-test
store를 격리한다. Backup/restore, retention과 encryption-key recovery는 모든 authoritative
table을 포함한다. Management-plane outage는 새 mutation을 거부하고 data plane은 기존
availability policy에 따라 마지막 verified state를 유지할 수 있다. Cold-start scan이 실패하면
managed registry는 비어 있고 readiness는 unavailable이며 bootstrap file로 복구하지 않는다.
현재 immutable generation/pointer, metadata snapshot과 verified-query table이 이 authority를
이미 표현하므로 mode/import를 위한 schema migration이나 marker를 추가하지 않는다.

Minimal catalog도 기존 immutable manifest generation을 재사용한다. Strict manifest v2의
`provenance` block에 owner, environment와 DB migration reference를 필수로 두고, admin read API는
허용한 JSON path와 lifecycle/metadata pointer만 조회한다. Raw manifest, encrypted credential,
metadata snapshot과 verified query를 읽거나 반환하지 않는다. 이 단계는 별도 catalog table,
중복 column이나 Control schema migration을 추가하지 않는다.

상세 rollout은 [source management plane](../source-management-plane.md), 실행 순서는
[active development TODO](../development-todo.md)의 `CTRL-*`가 관리한다.

## Consequences

- 운영자는 Git checkout 없이 모든 managed source의 상태, 이력, resource tier, replica convergence와
  bounded resource/gateway usage projection을 한곳에서 확인한다. 내부 수집은 `CTRL-07A`, public
  availability projection은 `CTRL-08`로 구현됐다.
- Git은 platform schema와 fixture authority이며 production source catalog가 아니다.
- 단일 관리 화면을 위해 business data, plaintext secret 또는 raw metric을 Control DB에
  복제하지 않는다.
- Source 추가는 모든 query 사용자에게 동시에 공개되고 별도 grant 변경이 없다.
- 관리자 한 종류와 기존 `budget_profile`만 사용하므로 초기 schema와 API가 작다.
- Control DB availability, backup, audit integrity와 admin credential 분리는 production-critical
  boundary가 된다.
- Replica convergence, resource/gateway usage projection과 격리 PostgreSQL service 사이의
  18.4→18.6 recovery fixture acceptance는 구현됐다. DB-native/provider cost는 남은 구현 gap이다.
- Future per-user/org ACL, quota, tier override, multi-role approval, automated credential broker와
  chargeback은 실제 요구와 threat model이 생길 때 별도 결정으로 추가한다.
