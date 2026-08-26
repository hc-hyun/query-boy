# Control Plane Module

Status: Logical boundary; physical package split pending for shared adapters — managed package extracted

> **현재 launch에서는 꺼져 있다.** Control Plane의 managed capability는 구현되어 있고
> 보존되지만, [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)의 첫 launch는
> `development-issues`, `market-voc` 두 source를 static bootstrap으로만 조립한다. 따라서 첫 launch에는
> Control DB, admin mutation, hot onboarding, `SourceReloader`와 observation reporter가 참여하지 않는다.

## 목적

### 30초 요약

Control Plane은 managed mode에서 **어떤 source generation과 metadata revision을 현재값으로 쓸지**
결정하고 그 이력을 Control DB에 보존한다. 쉽게 말하면 Source Catalog가 “source 정의의 올바른
형식”을 정한다면, Control Plane은 “검증된 정의 중 지금 선택된 것은 무엇인가”를 정하는 관리실이다.

| 질문 | 답 |
|---|---|
| 언제 이 module을 고르는가? | Source publish·credential 교체·비활성화·rollback, Control DB migration, runtime convergence 또는 managed observation을 바꿀 때 |
| 현재 첫 launch 요청을 처리하는가? | 아니요. 구현은 보존하지만 ADR 0025 static launch에는 조립하지 않는다. |
| 실제 업무 data도 저장하는가? | 아니요. Source lifecycle, metadata/verified artifact와 bounded observation만 저장한다. |
| 가장 중요한 경계는? | Immutable history, source-scoped lock/CAS, atomic pointer, secret redaction과 runtime apply 순서다. |

Managed capability의 운영자 관점은 [source management plane](../../source-management-plane.md),
영속성의 정확한 의미는 [persistence and recovery](persistence-and-recovery.md), replica/resource/gateway
관측의 정확한 의미는 [observability](observability.md)를 따른다.

## 소유 책임

- Numbered Control DB migration, immutable checksum ledger, schema, constraint, trigger, role와 grant
- Immutable source generation, encrypted credential, metadata snapshot, verified-query artifact와 active pointer
- Source-scoped advisory transaction lock, generation/state-version CAS와 atomic state transition
- Publish, credential rotate, deactivate, rollback, resume와 verified-query publish use case
- Idempotency key, keyed canonical request hash와 immutable terminal mutation receipt
- Secret-free source inventory/history와 mutation, replica, resource/usage application projection
- Candidate source를 active runtime과 격리해 검증하는 staging composition
- Committed desired state를 registry/cache/pool에 적용하는 `SourceReloader`
- Stable managed replica와 bounded resource/gateway observation의 persistence 및 freshness 판정

## 소유하지 않는 책임

- Source manifest, budget, semantic overlay와 reader-policy schema 자체의 정의
- Metadata snapshot shape, revision digest, context assembly와 publish quality 판정
- SQL validator, query transaction, final-result OID와 canonical result encoding
- Caller authentication, operator capability와 HTTP/MCP request/response/error wire
- Process startup/shutdown, polling/report cadence와 production server composition
- Source DB의 reader role, curated view, grant와 business schema migration
- Static first-launch source inventory, protected deployment와 cutover 실행

## 현재 코드 위치

| 위치 | 이 module이 소유하는 범위 | 주의점 |
|---|---|---|
| [`managed/source_admin.py`](../../../src/query_man/managed/source_admin.py) | Public administration input/use case, `SourceReloader`, observation writer와 usage projection | Cross-module application interface가 있는 managed package 핵심 파일 |
| [`managed/source_store.py`](../../../src/query_man/managed/source_store.py) | PostgreSQL state transition, projection query와 persistence-private type | Delivery나 다른 module이 직접 import하지 않음 |
| [`managed/metadata_store.py`](../../../src/query_man/managed/metadata_store.py) | `PostgresMetadataStore`의 pool/SQL/lock/transaction | Core `metadata_store.py`의 Metadata port와 codec을 구현함 |
| [`managed/secrets.py`](../../../src/query_man/managed/secrets.py) | Generation-bound AES-GCM encryption | Plaintext와 key를 log/response/DB에 남기지 않음 |
| [`errors.py`](../../../src/query_man/errors.py) | Control administration domain-error 발생 의미 | Public envelope은 Delivery 소유인 shared transition file |
| [`05-control-plane.sh`](../../../docker/postgres/init/05-control-plane.sh), [`control-migrations`](../../../docker/postgres/init/control-migrations) | Numbered migration, checksum ledger와 least-privilege reconciliation | Migration-first, 과거 migration 수정 금지 |
| [`apply-control-schema.sh`](../../../scripts/apply-control-schema.sh), [`control-plane-drill.sh`](../../../scripts/control-plane-drill.sh) | Schema apply와 recovery drill | Protected 실행은 별도 승인 필요 |
| [`compose.acceptance.yaml`](../../../compose.acceptance.yaml), [`apply-managed-acceptance-fixtures.sh`](../../../scripts/apply-managed-acceptance-fixtures.sh) | 격리된 `query-man-managed-acceptance` project의 local/CI Control·onboarding acceptance 조립 | Base static container/volume/apply에는 참여하지 않음 |
| [`test_source_admin.py`](../../../tests/test_source_admin.py), [`test_source_store.py`](../../../tests/test_source_store.py), [`test_metadata_store.py`](../../../tests/test_metadata_store.py) | Application/persistence focused tests | Provider와 직접 consumer를 함께 확인 |
| [`test_managed_http.py`](../../../tests/test_managed_http.py), [`test_managed_operations.py`](../../../tests/test_managed_operations.py) | Managed admin Delivery와 Runtime observation direct-consumer tests | External wire와 Runtime private accumulator owner도 함께 확인 |
| [`test_control_migrations.py`](../../../tests/test_control_migrations.py), [`test_control_startup.py`](../../../tests/test_control_startup.py), [`test_managed_mode.py`](../../../tests/test_managed_mode.py), [`test_managed_runtime_startup_cleanup.py`](../../../tests/test_managed_runtime_startup_cleanup.py), [`test_control_recovery.py`](../../../tests/test_control_recovery.py) | Migration, composition, convergence, cleanup와 recovery acceptance | Integration marker가 필요한 test가 있음 |

Administration, source/metadata persistence와 secret 구현은 `src/query_man/managed` same-repository package로
격리했다. 이는 별도 repository나 service가 아니며 external/persisted/policy/lifecycle 의미도
바꾸지 않는다. Core `metadata_store.py`는 Metadata port/error/codec만 남긴다. 공통 `errors.py`,
Runtime의 managed composition과 cross-module tests는 아직 shared transition artifact이므로
coordinating agent가 single-writer로 다룬다.

### Repository 분리 준비 경계

`query_man.managed`에는 package marker, source administration/store, concrete metadata store, secret
cipher, managed-only Delivery route와 Runtime composition을 둔다. `managed/runtime.py`의 owner는
Runtime이고 나머지 file도 위 코드 지도처럼 기능 owner가 다를 수 있다. 이 package는 core의 공개
interface를 소비할 수 있지만 static bootstrap composition과 ordinary core provider는 managed
implementation을 import하지 않는다. Managed authority를 명시적으로 선택한 server만 managed Runtime
composition을 import한다.

Control migration, acceptance fixture와 tests는 각각 기존 owner에 따라 repository root에 남는다. 따라서
현재 상태는 **별도 repository가 아니라 추출 가능한 코드 경계**다. Application install/image에는 아직
managed package가 포함된다. 실제 repository 이동 전에는 managed source tree 없이 static build/test가
성립하는지, versioned core interface와 compatibility CI, Control DB migration/recovery owner를 별도로
확정해야 한다.

## 제공 인터페이스와 소유 경계

이 절의 Python symbol과 호출 단위 input/output/domain-error 의미만 official module interface다.
Control DB row, HTTP path, revision/freshness 산식, transaction 순서와 protected procedure는 각각
persisted format, external API, policy 또는 safety/operational boundary이며 Python interface와 같은
범주가 아니다.

### Source administration application interface

Managed Delivery가 소비하는 다음 상수와 frozen input은 `managed/source_admin.py`가 제공한다.

```text
CONTROL_SEQUENCE_MAX = 9_223_372_036_854_775_807

MutationContext(
  idempotency_key, actor, reason,
  expected_generation, expected_state_version,
  expected_metadata_revision=None
)

VerifiedExpectedInput(columns, row_count, result_hash)
PublishVerifiedQueryInput(
  query_id, source_id, question, sql,
  metadata_revision, relations, expected
)
```

Delivery가 호출하는 `SourceAdminService`의 public async use case는 다음과 같다. 아래 표기는 현재
argument 이름, optional/default와 result type을 그대로 요약한다.

```text
list_sources(
  limit=50, after_source_id=None, enabled=None,
  owner=None, environment=None, budget_profile=None
) -> dict[str, object]
get_source(source_id) -> dict[str, object]
source_history(source_id, limit=50, before_generation=None) -> dict[str, object]
get_mutation(idempotency_key) -> dict[str, object]
source_mutations(source_id, limit=50, before_event_id=None) -> dict[str, object]
source_replicas(source_id, limit=50, after_replica_id=None) -> dict[str, object]
source_usage(source_id) -> dict[str, object]

publish(source_id, manifest, credential, mutation=None) -> dict[str, object]
rotate_credential(source_id, credential, mutation=None) -> dict[str, object]
deactivate(source_id, mutation=None) -> dict[str, object]
rollback(source_id, generation, mutation=None) -> dict[str, object]
resume_automatic_publish(source_id, mutation=None) -> dict[str, object]
publish_verified_query(query_input, tenant_id, mutation=None) -> dict[str, object]
```

성공 mutation status는 `published`, `deactivated`, `rolled_back`, `resumed`, `verified`다. Control
Plane은 `SourceNotFoundError`, `SourceValidationError`, `SourceGenerationConflictError`,
`SourceControlUnavailableError`, `MutationNotFoundError`, `MutationIdempotencyConflictError`의 domain
의미를 제공하고 Delivery가 이를 external error envelope로 rendering한다. Exact transition, receipt와
stored-history 의미는 [persistence and recovery](persistence-and-recovery.md)를 따른다.

### Runtime composition에서 사용하는 reload lifecycle

`SourceReloader`는 Control Plane의 concrete implementation이다. Managed Runtime은 production composition
권한으로 이를 조립하고 `sync()` lifecycle을 호출한다. 이 예외는 다른 module에 `apply(record)`, stored
row나 store private API를 일반 dependency로 공개하지 않는다. Source apply의 exact invalidation 순서는
[persistence and recovery](persistence-and-recovery.md#desired-state-적용과-convergence)에 기록한다.

### Runtime이 소비하는 observation writer interface

```text
ReplicaSourceObservation(
  source_id,
  applied_generation | None, applied_state_version | None, applied_enabled | None,
  applied_metadata_revision | None, source_health | None, reason_code | None
)

ReplicaObservationWriter:
  register_replica(replica_id, heartbeat_interval_ms) -> incarnation
  report_replica(replica_id, incarnation, *, reason_code, sources) -> None

ResourceObservationSample(metric, value, unit, method, definition_revision)

ResourceObservationWriter:
  report_resource_observations(
    source_id, generation, metadata_revision, samples
  ) -> None
  report_resource_observation_failure(
    source_id, generation, reason_code
  ) -> None

GatewayUsageDelta(
  source/profile/revision/hour identity,
  fixed terminal counters and success-only sums
)

GatewayUsageWriter:
  report_gateway_usage(replica_id, incarnation, sequence, deltas) -> None
```

모든 writer method는 async다. Exact field type, fencing, freshness, allowed reason, public status와
cardinality는 [observability](observability.md)에 있다. Observation은 source desired state, query
result, readiness, mutation receipt나 shutdown 성공을 바꾸지 않는 best-effort work다.

### Persisted format과 external API

- Control DB schema, lock/CAS, receipt, encryption과 recovery는
  [persistence and recovery](persistence-and-recovery.md)의 persisted/safety boundary다.
- Admin HTTP path, header, validation order, status와 response shape는 Delivery가 소유한다.
  운영자용 현재 표면은 [source management plane](../../source-management-plane.md#current-management-operations)에
  요약한다.
- Replica/usage application result는 Control Plane이 제공하지만 이를 HTTP로 직렬화하는 wire 의미는
  Delivery가 소유한다. MCP administration tool은 없다.

## 소비 인터페이스와 전제

| Provider | 소비하는 공개 interface | 사용하는 이유와 금지선 |
|---|---|---|
| [Source Catalog](../source-catalog/README.md) | Strict manifest validator, budget, `SourceReader`, `SourceProjectionWriter` | Candidate와 stored state를 검증하고 projection한다. Manifest 의미를 재정의하지 않는다. |
| [Metadata](../metadata/README.md) | Candidate preparation/quality use case, `MetadataStore` port/codec, `RuntimeCatalogProvider` | Control Plane은 port를 구현하고 staging에 공개 provider만 조립한다. Metadata private cache/algorithm에 의존하지 않는다. |
| [Guarded Query](../guarded-query/README.md) | Published SQL policy와 guarded query use case | Verified publish도 동일한 query safety path를 우회하지 않는다. |
| [Assurance](../assurance/README.md) | `VerifiedQuery`, `ExpectedResult`, result hash identity | Control Plane만 public admin input을 Assurance DTO로 exact mapping한다. Delivery는 Assurance DTO를 직접 import하지 않는다. |
| [Runtime](../runtime/README.md) | Operations/health reporting interface | Apply·observation failure를 secret-free하게 알린다. Runtime private composition에 의존하지 않는다. |

Runtime은 managed polling/report schedule과 production lifecycle을 호출한다. Delivery는 operator와 trusted
tenant를 확인한 뒤 administration use case를 호출한다. 이 caller obligation은 Control Plane이
Delivery/Runtime private implementation에 의존한다는 뜻이 아니다.

## 불변조건

- ADR 0025 first launch에는 Control DB, admin mutation, hot reload와 managed observation을 조립하지 않는다.
- 같은 `source_id`를 다른 host/port/database/user/TLS/environment identity로 재사용하지 않는다.
- Generation, snapshot, verified artifact와 terminal receipt history를 update/delete하지 않는다.
- Source-scoped lock와 generation/state-version CAS 없이 active state를 변경하지 않는다.
- Source와 metadata pointer, 성공 receipt와 authority mutation을 각각 다른 transaction으로 나누지 않는다.
- Pin된 metadata를 암묵적으로 덮어쓰거나 rollback 뒤 자동 resume하지 않는다.
- Credential, raw manifest, question/SQL, expected business literal과 내부 DB 오류를 response, audit,
  observation 또는 일반 log에 넣지 않는다.
- Runtime apply가 실패해도 committed desired-state receipt를 뒤집지 않고 process-local health에 성공처럼
  표시하지 않는다.
- Observation의 missing/failure를 0으로 만들거나 source authority·query result·readiness에 사용하지 않는다.
- Control writer는 최소 권한 role이며 application owner나 source reader role을 재사용하지 않는다.

정확한 persisted/transaction 불변조건은 [persistence and recovery](persistence-and-recovery.md),
replica/resource/gateway 불변조건은 [observability](observability.md)에 이어진다.

## 모듈 내부 변경

다음은 official interface와 별도 persisted/wire/policy/lifecycle 의미를 보존할 때 독립적으로 바꿀 수 있다.

- 같은 transaction과 projection을 만드는 store query/helper 정리
- Lock/CAS와 pool budget을 보존하는 connection bookkeeping 개선
- Ciphertext format과 associated data를 유지하는 crypto wrapper 내부 정리
- 동일한 validation/apply 순서와 오류를 만드는 reloader helper 개선
- 같은 bounded field/order/status를 만드는 projection query 정리
- Canonical request, exact replay와 receipt 의미를 보존하는 orchestration 정리

Shared transition file이나 cross-module test를 수정해야 하면 coordinating agent가 writer와 순서를 정한다.

## 사용자 승인이 필요한 경계 변경

다음 중 하나라도 의미가 달라지면 구현을 멈추고 실제 범주, provider/consumer, compatibility,
migration/rollback, 안전 영향과 검증 계획을 사용자에게 제시한다.

| 변경 범주 | Control Plane에서 멈춰야 하는 예 |
|---|---|
| Module interface | 위 public input/DTO/Protocol/use case의 shape, signature, result 또는 domain-error 의미 |
| Persisted/versioned format | `control` schema, migration ledger/checksum/order, immutable key/history, credential ciphertext/AAD |
| External API/wire | Admin path/header/request/response/status, pagination/filter/order, replica/usage projection |
| Policy/compatibility identity | Source identity, expected-state/idempotency hash, freshness/status/reason, usage window와 cardinality |
| Safety/lifecycle invariant | Lock/CAS/transaction atomicity, stage/publish/rollback/pin/resume, invalidation/apply/convergence 순서 |
| Ownership/composition boundary | Bootstrap/managed authority, filesystem fallback, staging/reloader/observation 조립 위치 |
| Protected operational procedure | Migration apply, backup/restore/key recovery, managed activation, inventory/cutover/rollback/stop condition |

Existing persisted data, rolling replica와 recovery 절차 및 Source Catalog, Metadata, Guarded Query,
Delivery, Runtime, Assurance 영향을 함께 제시한다. Protected environment의 실제 action은 repository나
procedure 변경 승인과 별도로 target, access, artifact, stop condition과 change-record 책임을 확인한
실행 승인이 필요하다. 과거 evidence와 persisted history를 현재 의미에 맞춰 수정·삭제하지 않는다.

## 검증

기본 application/reloader gate:

```text
uv run pytest tests/test_registry.py tests/test_source_admin.py tests/test_secrets.py \
  tests/test_managed_mode.py
```

Persistence와 managed onboarding DB fixture는 base static Compose에 없다. 필요한 local/CI test는
[managed acceptance fixture](../../development-guidelines.md#managed-acceptance-fixture)를 먼저 준비하고
별도로 실행한다.

```text
uv run pytest -m integration tests/test_source_store.py tests/test_metadata_store.py \
  tests/test_control_migrations.py tests/test_control_startup.py \
  tests/test_control_recovery.py
```

| 변경 영역 | 추가 검증 |
|---|---|
| Public admin input/verified mapping | `tests/test_managed_http.py`, `tests/test_documentation.py`, `tests/test_control_startup.py` |
| Replica/resource/usage projection | `tests/test_source_admin.py`, `tests/test_source_store.py`, `tests/test_managed_operations.py`, `tests/test_managed_http.py` |
| Schema, transaction, lock/CAS, recovery | 전체 integration gate와 `scripts/control-plane-drill.sh` |
| DB/reader trust boundary | `uv run pytest -m integration` 전체 |

완료 전 [활성 개발 지침](../../development-guidelines.md#tests)의 `ruff`, `mypy`, full pytest gate를
coordinating agent가 실행한다. Protected
deployment evidence는 별도 실행 승인 뒤에만 append한다.

## 집중해서 읽을 범위

먼저 이 문서와 [module index](../README.md)를 읽고, 작업 종류에 맞는 한 행만 확장한다.

| 작업 | 추가로 읽을 code/reference/test |
|---|---|
| Admin read/mutation | `managed/source_admin.py`, Delivery의 managed-only admin route, `test_source_admin.py`, `test_managed_http.py`, [persistence and recovery](persistence-and-recovery.md) |
| Store/migration/secret/recovery | `managed/source_store.py`, `managed/metadata_store.py`, `managed/secrets.py`, numbered migrations, integration tests, [persistence and recovery](persistence-and-recovery.md) |
| Managed reload/convergence | `SourceReloader` symbol, provider lifecycle interface, `test_managed_mode.py`, `test_control_startup.py` |
| Replica/resource/gateway observation | Writer/projection symbol, Runtime reporter와 Delivery endpoint, `test_managed_operations.py`, `test_managed_http.py`, [observability](observability.md) |
| Launch/authority 변경 | [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md), [ADR 0016](../../decisions/0016-centralized-source-management-plane.md), Runtime composition과 operations runbook |

Source revision과 verified lifecycle은 [ADR 0012](../../decisions/0012-control-plane-source-revisions.md),
[ADR 0013](../../decisions/0013-control-plane-verified-query-publishing.md)을 따른다. Metadata relevance,
MCP SDK 내부와 query cursor 구현은 위 interface나 승인 대상 의미가 바뀌지 않는 한 읽을 필요가 없다.
