# Runtime Module

Status: Logical boundary; physical package split pending

Current launch baseline: [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)
`LAUNCH-01-A`

## 목적

Runtime은 production server의 concrete implementation을 하나의 process로 조립하고 안전하게
시작·운영·종료한다. 쉽게 말하면 각 module이 만든 부품의 업무 규칙을 다시 만드는 곳이 아니라,
검증된 부품을 연결하고 process lifecycle을 책임지는 composition root다.

Query Man은 하나의 deployable process인 modular monolith다. Runtime은 조립을 위해 모든 module을
알 수 있지만, 이 예외는 wiring과 lifecycle에만 사용하며 다른 module의 private 업무 규칙을 이곳에
옮기지 않는다.

## 소유 책임

- Environment를 `RuntimeConfig`로 검증하고 process-level configuration을 선택
- Production registry, Metadata, Guarded Query, Control Plane, Delivery와 MCP dependency 조립
- `bootstrap|managed` source authority의 mutually exclusive 선택과 fallback 금지
- Current launch inventory를 adapter 생성 전에 검사하고 RLS composition을 fail-closed
- Application/MCP child lifespan, startup failure cleanup과 정상 shutdown drain/close 순서
- Initial inventory reconcile, bounded metadata probe와 managed-mode reload/report task 조립
- Aggregate liveness/readiness, component/source health와 replica-local operational state
- Uvicorn signal/graceful shutdown integration, structured logging/redaction setup
- Container topology, upstream image pin과 application revision label의 repository definition

## 소유하지 않는 책임

- Source manifest, reader compatibility, metadata, SQL와 result OID 업무 정책
- Caller 인증·인가와 HTTP/MCP request/response/error schema
- Control DB schema, source mutation transaction과 PostgreSQL query implementation
- Verified query/hash의 작성·판정 또는 source별 Python branch
- TLS termination, protected secret/backup, 실제 route/cutover와 environment change record 실행
- RLS attestation, broader result encoding, cost attribution 또는 workflow trace의 미래 설계

## 현재 코드 위치

- [`app.py`](../../../src/query_man/app.py): `build_app`, dependency composition, lifespan, startup probe와
  managed reload/resource/usage reporter; middleware/routes/DTO symbol은 Delivery 소유
- [`server.py`](../../../src/query_man/server.py): Uvicorn process와 shutdown signal ordering
- [`runtime_config.py`](../../../src/query_man/runtime_config.py): environment model/validation과
  `RuntimeConfig`
- [`operations.py`](../../../src/query_man/operations.py): process-local operational state, safe formatter와
  redaction
- [`__init__.py`](../../../src/query_man/__init__.py): package identity/version
- [`Dockerfile`](../../../Dockerfile), [`compose.yaml`](../../../compose.yaml),
  [`.env.example`](../../../.env.example): image, process, network, configuration과 health lifecycle
- `compose.yaml`의 `recovery` profile: Assurance의 Control recovery fixture이며 serving topology가 아님
- `compose.yaml`의 `soak` profile: 두 번째 replica acceptance fixture이며 first-launch topology가 아님
- [`verify-container.sh`](../../../scripts/verify-container.sh): Assurance 소유의 container acceptance
  script; Runtime container surface를 소비하는 shared transition artifact
- [`pyproject.toml`](../../../pyproject.toml), [`uv.lock`](../../../uv.lock): application entrypoint와
  locked dependency; Assurance offline command와 test tooling 부분은 shared transition artifact
- Focused tests: [`test_runtime_config.py`](../../../tests/test_runtime_config.py),
  [`test_operations.py`](../../../tests/test_operations.py),
  [`test_server.py`](../../../tests/test_server.py),
  [`test_http.py`](../../../tests/test_http.py),
  [`test_managed_mode.py`](../../../tests/test_managed_mode.py),
  [`test_runtime_startup_cleanup.py`](../../../tests/test_runtime_startup_cleanup.py)

현재 코드는 `src/query_man`의 평면 구조다. `app.py`는 Delivery와 Runtime의 transition hot spot이므로
composition/lifespan symbol만 Runtime이 수정하고 route 또는 wire schema 정리를 같은 diff에 섞지 않는다.

## 제공 인터페이스와 소유 경계

이 절은 공식 Python module interface와 composition ownership, policy, lifecycle invariant 및 protected
operation을 구분한다. 같은 file에 있다는 이유로 이 의미들을 모두 module interface라고 부르지 않는다.

### Official Python interfaces

Runtime이 다른 logical module에 제공하는 공식 Python interface는 process-local operations sink와
그 immutable reporting value다. 현재 직접 consumer는 Metadata, Guarded Query, Control Plane과
Delivery다.

- `operations.increment`, `operations.observe`, source/component health와 admission 기록 capability
- `GatewayUsageOutcome`과 `operations.record_gateway_usage(...)`
- `ReplicaRuntimeSnapshot`, `ReplicaSourceRuntimeState`와 replica observation snapshot capability
- `operations.suppress_source_health_updates()` staging isolation capability

Python shape와 각 호출의 input/output/domain-error 의미만 module interface다. Metric label, readiness
판정, report cadence, failure 격리와 공개 projection은 각각 policy 또는 safety/operational 의미다.
`RuntimeConfig`, `load_runtime_config()`와 `build_app()`은 현재 Runtime 내부 configuration/composition
entry이며 다른 domain module에 공개한 업무 interface가 아니다.

### Production composition ownership

- Production server의 concrete PostgreSQL adapter 조립은 Runtime만 수행한다. Control Plane candidate
  staging과 Assurance offline CLI는 각 bounded workflow에서만 별도 composition root가 될 수 있다.
- Runtime은 concrete `SourceRegistry`를 만들고 ordinary consumer에는 `SourceReader`, managed reloader에는
  `SourceProjectionWriter` capability로 같은 instance를 주입한다.
- Catalog와 query adapter는 provider가 공개한 `RuntimeCatalogProvider`와 `RuntimeQueryExecutor`를
  만족해야 한다. Runtime은 adapter 선택 직후 required method가 callable인지 검사하고 누락되면
  ready 전에 `TypeError`로 fail-closed한다.
- Standard bootstrap composition은 scalar-domain OID erasure를 막는 static Catalog guard를
  활성화한다. Managed composition과 Control staging의 기본 Catalog는 이를 활성화하지 않으며 현재
  launch serving에는 참여하지 않는다.
- Delivery에는 Gateway/application service만 전달하며 persistence, catalog와 executor internals를
  직접 주입하지 않는다. Metadata와 Guarded Query도 Control DB implementation을 직접 알지 않는다.
- Runtime-only dependency edge는 wiring/lifecycle 외의 업무 호출이나 provider private API 사용을
  허용하지 않는다.

### Current launch composition and RLS quarantine

Current first launch는 repository가 검토한 `development-issues`, `market-voc` 두 source의 static
`bootstrap` authority와 단일 serving replica다.

- Bootstrap manifest의 RLS는 Source Catalog validation에서 listener 생성 전에 실패한다.
- 주입된 bootstrap registry도 Runtime inventory guard를 adapter/provider 생성 전에 검사하며 RLS가
  하나라도 있으면 composition에 실패한다.
- Managed mode 구현은 보존하지만 first launch에는 Control DB, admin mutation, hot onboarding,
  runtime reload와 observation task가 참여하지 않는다.
- Managed cold record의 RLS는 validator에서 `RUNTIME_VALIDATION_REJECTED`로 남고 registry에
  projection하지 않는다. Managed publish/rotate도 기존 `SOURCE_VALIDATION_FAILED`로 끝난다.
- Bootstrap과 managed authority를 한 process에서 합치거나 실패 시 서로 fallback하지 않는다.
- `query-man-replica`는 `soak` acceptance profile일 뿐 동시에 serving하는 두 번째 replica가 아니다.

RLS type, code, Control history와 managed implementation은 물리적으로 삭제하지 않는다. 위 결과는
module interface가 아니라 [ADR 0025의 RLS quarantine](../../decisions/0025-static-non-rls-first-launch.md#2-rls-quarantine)과
composition/lifecycle invariant다. RLS serving 재개와 managed launch 활성화는 현재 baseline을 기준으로
별도 영향·migration·rollback을 승인해야 한다.

### Startup sequence and failure cleanup

현재 startup의 의미상 순서는 다음과 같다.

```text
environment/configuration validate -> logging configure -> operations reset
-> source authority 선택
   bootstrap: filesystem source/verified dataset
   managed: empty registry/verified map, no filesystem fallback
-> RLS launch inventory guard
-> catalog/query adapter 선택과 required Runtime capability validate
-> metadata/query/access/gateway compose
-> managed only: Control stores/reloader compose
-> MCP compose/lifespan configure
-> managed only: Control reloader initial sync
-> active inventory reconcile -> source별 bounded metadata probe
-> managed only: reload/report background task start
-> MCP child lifespan enter -> ready/degraded serving state
```

필수 configuration, source inventory 또는 dependency 초기화가 실패하면 ready로 전환하지 않는다.
Managed sync/probe와 background task 생성 뒤 MCP child lifespan `enter` 자체가 실패하면 Runtime은 진입
전에 parent가 소유한 resource를 다음 고정 순서로 정리한다.

```text
reload task cancel/await
-> query executor immediate close
-> catalog close
-> metadata close (소유한 metadata store 포함)
-> source store close
```

진입하지 못한 child에는 `exit`를 호출하지 않는다. Child가 `enter` 도중 만든 partial resource는 child
lifespan이 정리하고 Runtime은 parent 최상위 resource만 같은 identity당 정확히 한 번 close/cancel을
시도한다. 한 단계가 실패해도 나머지를 계속 정리하고 고정 step 이름만 경고로 남긴 뒤 최초 startup
exception을 그대로 다시 발생시킨다.

### Shutdown sequence and drain rules

```text
signal/request shutdown
-> readiness와 새 query admission close
-> listener graceful request handling
-> managed reload/report task cancel
-> executor bounded drain
-> executor/catalog/metadata/store resource close
```

Grace 안에 끝나지 않은 queued/active query는 cancel되고 PostgreSQL rollback으로 끝나야 한다. HTTP와
MCP disconnect도 같은 task-cancel 경로를 사용한다. 정상 shutdown은
`RuntimeQueryExecutor.drain(configured grace)`를 직접 호출하고 optional lifecycle method 탐색으로
skip하지 않는다.

SQL policy나 metadata revision이 바뀌는 release는 Runtime이 old/new 값을 번역하지 않는다. Mixed
serving fleet를 만들지 않고 old process와 source connection을 drain한 뒤 route 밖에서 새 baseline을
검증하는 절차가 필요하다.

### Health, topology and artifact semantics

- `/health`는 process liveness만, `/ready`는 source ID 없는 aggregate 상태만 공개한다.
- API `/ready`는 기존처럼 `ready|degraded`에 HTTP 200을 반환한다. Compose launch healthcheck는 이보다
  엄격하게 body가 정확히 `{"status":"ready"}`일 때만 healthy다.
- Source/component detail과 metric은 operator surface에만 제공하며 credential state를 public health에
  노출하지 않는다. Counter와 health는 replica-local이고 restart 후 초기화된다.
- First-launch serving topology는 `query-man` 단일 replica다. `soak`/`recovery` profile은 acceptance
  fixture이며 serving topology가 아니다.
- Serving PostgreSQL 18.6, recovery fixture PostgreSQL 18.4, Python 3.14 slim과 uv 0.9.18 upstream image는
  tag와 OCI digest를 함께 고정한다.

현재 repository pin은 다음과 같다.

```text
postgres:18.6-bookworm@sha256:1c59e2c3c818eaa0f0628f695b36e7c9e362d6b219b36a54a32df645cbd7e1af
postgres:18.4-bookworm@sha256:882236b897e39051d2368c5ccc6cda944904723506b2dfc97f2a8f5bc9afa382
python:3.14-slim-bookworm@sha256:416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63
ghcr.io/astral-sh/uv:0.9.18@sha256:5713fa8217f92b80223bc83aac7db36ec80a84437dbc0d04bbc659cae030d8c9
```

Release image build는 `QUERY_MAN_VCS_REF`에 approved Git revision을 전달하고
`org.opencontainers.image.revision` OCI label로 보존한다. Default `unknown` label이나 mutable tag만으로
식별된 image는 release artifact가 아니다. 실제 built application image digest는 protected environment
change record에서 기록해야 하며 repository 문서가 실행 evidence를 대신하지 않는다.

### Preserved managed runtime outside first launch

Managed path는 구현된 상태로 보존한다. 별도 활성화 시 empty registry에서 시작하고 Control DB source와
verified membership만 authority로 사용하며 filesystem source/verified dataset으로 복구하지 않는다.
Version 2 shared access policy, stable replica ID, initial sync, inventory reconcile, metadata probe와 reload
task를 요구한다.

Runtime은 Control Plane의 공개 replica/resource/gateway writer만 소비한다. Reporting은 fixed Control
pool과 process-local write lock을 재사용하고 startup, query result, readiness와 shutdown 성공을 바꾸지
않는 best-effort work다. Raw exception, SQL, credential, tenant와 query ID는 report payload에 넣지 않는다.
Generation/metadata fencing, cadence, reason code와 projection 의미의 authority는
[Control Plane](../control-plane/README.md)에 두고 이 문서에 DDL이나 future transition 설계를 복제하지
않는다.

### Runtime authentication configuration rules

- Bootstrap loopback에서 token/access-policy가 없으면 Delivery의 anonymous query-only local
  compatibility caller를 사용한다.
- Legacy token과 access-policy file을 동시에 설정하지 않으며 non-loopback bind에서 둘 다 없으면
  startup을 fail-closed한다.
- Managed mode는 legacy token/anonymous를 거부하고 version 2 access policy의 non-admin query identity와
  explicit operator identity를 모두 요구한다.
- Identity별 visibility/admin/cancel 의미는 Delivery가 소유하며 Runtime은 mode와 configuration 선택만
  소유한다.

### Protected operational boundary (`LAUNCH-02`)

Repository는 static inventory와 image pin, exact-ready acceptance 및 freeze/rollback 절차만 정의한다.
실제 protected TLS, secret 설치·회전, backup/restore 확인, target route, cutover와 rollback 실행은
`LAUNCH-02`다. 대상·접근 권한·artifact digest·stop condition·change-record owner를 확인한 별도 실행
승인과 실행 뒤 append-only evidence가 있어야 한다.

Serving 중 source manifest/budget/access policy, verified dataset, source DDL/role/grant/settings와
application/PostgreSQL artifact를 동결한다. 정상 business DML은 허용한다. 설명되지 않은 inventory
drift, RLS source, exact-ready 실패 또는 artifact 식별 불가는 route stop condition이다. 상세 절차는
[operations](../../operations.md)를 따른다.

## 소비 인터페이스와 전제

- [Source Catalog](../source-catalog/README.md)의 concrete registry construction capability,
  `SourceReader`와 managed-only `SourceProjectionWriter`
- [Metadata](../metadata/README.md)의 service/store, immutable catalog snapshot과
  `RuntimeCatalogProvider` lifecycle
- [Guarded Query](../guarded-query/README.md)의 `RuntimeQueryExecutor`
  admission/drain/invalidate/close lifecycle
- [Control Plane](../control-plane/README.md)의 managed-only stores, reloader와 public observation writers
- [Delivery](../delivery/README.md)의 Gateway/routes/MCP factory, parent middleware와 transport lifespan
- [Assurance](../assurance/README.md)의 bootstrap filesystem 또는 managed Control verified membership
  interface와 container acceptance

Runtime은 provider의 public interface만 업무 호출에 사용한다. Concrete implementation을 조립할 권한이
provider private table/type 접근 권한을 뜻하지 않는다.

## 불변조건

- Production concrete dependency 조립은 Runtime에 두고 Control staging/Assurance offline CLI 예외를
  bounded workflow 밖으로 확대하지 않는다.
- Current launch는 reviewed static two-source bootstrap, non-RLS와 단일 serving replica다.
- Bootstrap/injected RLS source는 provider composition 전에 실패하고 managed cold RLS record는 registry에
  projection하지 않는다.
- Bootstrap과 managed authority를 섞거나 failure fallback하지 않는다.
- Required adapter capability 누락과 startup 실패는 ready 전에 끝내고 parent resource를 정해진 순서로
  누출 없이 정리한다.
- Shutdown은 readiness/admission을 먼저 닫고 active work를 bounded drain한다.
- Public liveness/readiness에 source ID, component detail과 credential state를 노출하지 않는다.
- Environment secret, bearer token, DSN/password, SQL과 question을 structured log에 넣지 않는다.
- Source별 동작 차이를 Runtime composition branch로 만들지 않는다.
- Runtime은 shared source/profile/snapshot graph를 in-place 변경하지 않고 owner가 제공한 projection과
  invalidation capability만 호출한다.
- Release artifact는 pinned upstream과 approved VCS revision으로 식별하며 protected execution evidence를
  repository 상태만으로 주장하지 않는다.

## 모듈 내부 변경

다음은 공식 interface와 별도 configuration/policy/lifecycle/operation 의미를 보존할 때 독립적으로
변경할 수 있다.

- Dependency construction helper와 local variable 정리
- 동일한 cleanup 결과를 만드는 lifespan context 정리
- 같은 poll/probe/report 결과를 만드는 private task bookkeeping 개선
- Public status/label/cardinality를 유지하는 process-local counter 내부 개선
- 같은 grace/exit 의미를 유지하는 Uvicorn integration 정리
- Pin과 revision-label 의미를 보존하는 Dockerfile layer/cache 개선

## 사용자 승인이 필요한 경계 변경

승인 요청은 실제 변경 범주를 구분하고 관련 없는 항목을 하나의 “Runtime interface 변경”으로 묶지
않는다.

- Module interface: operations sink/DTO의 Python shape와 호출 의미, 또는 Runtime이 소비하는 provider
  lifecycle Protocol 변경
- External API/wire: health/readiness status, HTTP code/body와 public/operator disclosure 변경
- Policy/compatibility identity: source authority mode, RLS launch admission, artifact identity/pin과
  exact-ready acceptance 의미 변경
- Safety/lifecycle invariant: startup/reloader/probe, admission/drain/cancel/rollback/close 순서와 failure
  cleanup 변경
- Ownership/composition boundary: concrete adapter wiring 위치, bootstrap/managed authority, fallback,
  reload/report ownership 또는 serving replica topology 변경
- Protected operational procedure: source inventory, DDL/role/settings/artifact freeze, cutover, rollback과
  stop condition 변경

Environment variable required/optional/default, managed replica identity/cadence/fencing, metric
name/label/cardinality와 structured log/redaction allowlist 변경도 해당 interface·policy·invariant 영향을
명시해 승인받는다. Protected environment의 실제 action은 repository/procedure 승인과 별도로 access,
scope, target, stop condition과 change-record 책임을 확인한 실행 승인이 필요하다. 과거 evidence는
현재 의미에 맞춰 수정·삭제하지 않는다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_runtime_config.py tests/test_operations.py \
  tests/test_server.py tests/test_http.py tests/test_managed_mode.py \
  tests/test_runtime_startup_cleanup.py
```

Lifecycle/disconnect 변경은 query/MCP/integration tests를, managed reload 변경은 source-admin과 Control
startup tests를 추가한다. Container/artifact 경계는 다음 acceptance를 함께 실행한다.

```text
docker compose config --quiet
docker build --build-arg QUERY_MAN_VCS_REF=<approved-commit> -t query-man:<approved-commit> .
./scripts/verify-container.sh
uv run query-man-verify
```

Built image의 OCI revision label과 digest를 검사하고 Compose health가 exact ready 이외 상태를
healthy로 보지 않는지 확인한다. Protected deployment evidence는 별도 실행 승인 뒤에만 append한다.
완료 전 root `AGENTS.md`의 전체 gate는 coordinating agent가 실행한다.

## 집중해서 읽을 범위

Runtime 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 composition/configuration/operations/server/container code와 focused tests
3. 조립하거나 lifecycle을 호출하는 provider의 공개 interface
4. Current authority인 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)와 변경에 직접
   관련된 accepted ADR
5. `app.py` route/middleware를 건드릴 때 Delivery external API와 test
6. Protected procedure를 바꿀 때 [operations](../../operations.md); 실제 execution이면 별도 승인 범위와
   append-only evidence schema

Metadata ranking, SQL AST walker, Control DB table과 parked RLS/encoding/cost/trace proposal body는
Runtime이 소비하는 interface 또는 승인된 launch lifecycle을 바꾸지 않는 한 읽을 필요가 없다.
