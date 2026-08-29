# Runtime Module

Status: Physical package boundary active

Current launch baseline: [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)
`LAUNCH-01-A`

## 목적

### 30초 요약

Runtime은 Query Man의 **조립·운영 담당**이다. 각 module이 만든 구현을 하나의 production process로
연결하고, 설정을 검사한 뒤 안전하게 시작·종료한다. Source나 SQL의 업무 규칙을 다시 만들지는 않는다.

```text
환경 설정 검사 -> module 구현 조립 -> startup/probe -> serving -> drain/cleanup
```

Query Man은 하나의 deployable process인 modular monolith다. Runtime이 모든 module을 알 수 있는 이유는
production 조립과 lifecycle 때문이며, 이 예외로 다른 module의 private 업무 규칙까지 가져오지는 않는다.

| 구분 | 현재 상태 |
|---|---|
| First launch | `development-issues`, `market-voc`의 static `bootstrap`, non-RLS, 단일 serving replica |
| Managed runtime | `query_man.managed` package에 보존됐으며 명시적 managed composition에서만 import·조립 |
| `soak` / `recovery` profile | 각각 두 replica와 PostgreSQL 18.4 복구를 검증하는 acceptance fixture; serving topology가 아님 |
| Protected environment | Repository acceptance는 완료. 실제 TLS·secret·route·cutover는 별도 승인 대상 `LAUNCH-02` |

## 소유 책임

- Environment를 `RuntimeConfig`로 검증하고 `bootstrap|managed` source authority를 하나만 선택
- Production registry, Metadata, Guarded Query, Control Plane, Delivery와 MCP 구현 조립
- Current launch inventory와 RLS를 adapter 생성 전에 fail-closed 검사
- Application/MCP child startup, 실패 cleanup, 정상 shutdown drain/close 순서
- Initial inventory reconcile, bounded metadata probe와 managed-only reload/report task 조립
- Process-local health, readiness, metric, structured logging과 redaction
- Consent-gated encrypted diagnostic capture의 key, bounded queue, maximum 7-day TTL, byte budget와 fail-open worker
- `qm` 대화형 operator shell, local Compose log provider와 bounded diagnostic offline composition
- Uvicorn signal/graceful shutdown, container topology, upstream image pin과 application revision label

## 소유하지 않는 책임

- Source manifest, metadata, SQL, result OID와 verified-query 업무 정책
- Caller 인증·인가와 HTTP/MCP request/response/error 형식
- Control DB schema, source mutation transaction과 PostgreSQL query 구현
- TLS termination, protected secret/backup, 실제 route/cutover와 environment change record 실행
- RLS serving 재개, broader result encoding, cost attribution 또는 workflow trace의 미래 설계

## 현재 코드 위치

| 위치 | 역할 |
|---|---|
| [`runtime/composition.py`](../../../src/query_man/runtime/composition.py) | Static `build_app`, provider 조립, lifespan과 startup probe |
| [`delivery/app.py`](../../../src/query_man/delivery/app.py) | Delivery-owned HTTP/MCP parent surface; Runtime composition이 provider와 lifespan을 주입 |
| [`managed/runtime.py`](../../../src/query_man/managed/runtime.py) | Managed `build_app`, Control/admin/reload/reporter/usage composition과 lifecycle |
| [`runtime/server.py`](../../../src/query_man/runtime/server.py) | 검증된 source mode별 composition-root 선택, Uvicorn process와 shutdown signal ordering |
| [`runtime/config.py`](../../../src/query_man/runtime/config.py) | Environment model/validation과 `RuntimeConfig` |
| [`runtime/operations.py`](../../../src/query_man/runtime/operations.py) | Process-local operations sink, health/metric state와 safe formatter/redaction |
| [`runtime/diagnostic_capture.py`](../../../src/query_man/runtime/diagnostic_capture.py) | AES-GCM SQLite capture, HMAC subject/consent, retention와 offline decrypt/purge |
| [`runtime/operator_shell.py`](../../../src/query_man/runtime/operator_shell.py) | `qm` interactive/one-shot command, HTTP·Compose log·diagnostic provider composition |
| [`Dockerfile`](../../../Dockerfile), [`compose.yaml`](../../../compose.yaml), [`.env.example`](../../../.env.example) | Current two-source static image, process, network, config와 health lifecycle |
| [`compose.acceptance.yaml`](../../../compose.acceptance.yaml) | 별도 project/container/volume의 Control/support/commerce managed acceptance overlay; base serving topology가 아님 |
| [`verify-container.sh`](../../../scripts/verify-container.sh) | Assurance 소유의 container acceptance; Runtime surface를 소비하는 shared transition artifact |
| [`pyproject.toml`](../../../pyproject.toml), [`uv.lock`](../../../uv.lock) | Entrypoint와 locked dependency; 여러 owner가 쓰는 shared transition artifact |
| [`test_runtime_config.py`](../../../tests/test_runtime_config.py), [`test_server.py`](../../../tests/test_server.py), [`test_operations.py`](../../../tests/test_operations.py), [`test_diagnostic_capture.py`](../../../tests/test_diagnostic_capture.py), [`test_operator_shell.py`](../../../tests/test_operator_shell.py), [`test_http.py`](../../../tests/test_http.py) | Source authority, process/common operations, encrypted capture, operator shell과 static composition tests |
| [`test_managed_mode.py`](../../../tests/test_managed_mode.py), [`test_managed_operations.py`](../../../tests/test_managed_operations.py), [`test_managed_runtime_startup_cleanup.py`](../../../tests/test_managed_runtime_startup_cleanup.py), [`test_managed_http.py`](../../../tests/test_managed_http.py) | Managed composition, observation, cleanup와 Delivery direct-consumer tests |

Static Runtime은 `src/query_man/runtime` physical package, managed implementation은
`src/query_man/managed` package에 있다. Delivery surface는 `delivery/app.py`, static provider/lifespan
조립은 `runtime/composition.py`로 분리돼 있고 static composition은 managed package를 import하지 않는다.
Runtime 작업에서는 composition/lifespan symbol만 수정하고 route나 wire schema 정리를 같은 diff에 섞지 않는다. Base `compose.yaml`과
`scripts/apply-db.sh`는 current 두 source만 준비한다. Managed Control/support/commerce fixture는
`compose.acceptance.yaml` overlay와 `scripts/apply-managed-acceptance-fixtures.sh`를 명시적으로 사용한
별도 `query-man-managed-acceptance` project에서만 준비하며 serving topology가 아니다.

## 제공 인터페이스와 소유 경계

이 절은 Python module interface, configuration, composition ownership, lifecycle invariant와 protected
operation을 구분한다. 같은 file에 있다는 이유로 모두 module interface가 되는 것은 아니다.

### Official operations interface

Runtime이 다른 logical module에 제공하는 공식 Python interface는 process-local `operations` sink와
아래 immutable reporting value다. 다음 표는 현재 직접 consumer와 실제로 호출하는 symbol이다.

| Consumer | 소비하는 exact symbol/method |
|---|---|
| Delivery | `operations.increment`, `operations.observe`, `operations.public_status`, `operations.snapshot` |
| Metadata | `operations.increment`, `operations.set_source_health`, `operations.set_replica_metadata_revision` |
| Guarded Query | `operations.increment`, `operations.observe` |
| Control Plane | `operations.increment`, `operations.set_component_health`, `operations.set_replica_scan_failed`, `operations.set_replica_source_applied`, `operations.set_replica_source_failure`, `operations.clear_replica_source_apply_failure`, `operations.reconcile_sources`, `operations.set_source_health`, `operations.suppress_source_health_updates` |
| Runtime lifecycle/reporter | `operations.set_accepting`, `operations.replica_runtime_snapshot`, `ReplicaRuntimeSnapshot`, `ReplicaSourceRuntimeState` |

아래는 현재 Python 선언에서 첫 `self`만 생략한 exact signature다. 긴 immutable value는 field 순서와
type을 생성자 형태로 적었다.

<details>
<summary>Operations interface의 exact signature 펼치기</summary>

```python
operations.increment(name: str, source_id: str | None = None, value: int = 1) -> None
operations.observe(name: str, value: float, source_id: str | None = None) -> None
operations.set_source_health(source_id: str, status: str) -> None
operations.set_component_health(component: str, status: str) -> None
operations.reconcile_sources(source_ids: Iterable[str]) -> None
operations.set_accepting(accepting: bool) -> None
operations.public_status() -> str
operations.snapshot() -> dict[str, Any]
operations.set_replica_scan_failed(failed: bool) -> None
operations.set_replica_source_applied(source_id: str, generation: int,
    state_version: int, enabled: bool) -> None
operations.set_replica_source_failure(source_id: str,
    reason_code: ReplicaSourceReason) -> None
operations.clear_replica_source_apply_failure(source_id: str) -> None
operations.set_replica_metadata_revision(source_id: str, revision: str | None) -> None
operations.replica_runtime_snapshot() -> ReplicaRuntimeSnapshot
operations.suppress_source_health_updates() -> Iterator[None]

ReplicaSourceRuntimeState(source_id: str, applied_generation: int | None,
    applied_state_version: int | None, applied_enabled: bool | None,
    applied_metadata_revision: str | None, source_health: ReplicaSourceHealth | None,
    reason_code: ReplicaSourceReason | None)  # frozen dataclass
ReplicaRuntimeSnapshot(reason_code: ReplicaRuntimeReason | None,
    sources: tuple[ReplicaSourceRuntimeState, ...])  # frozen dataclass
```

</details>

Managed `QueryService`에만 주입하는 `ManagedGatewayUsageRecorder`는 Guarded Query가 제공한
`GatewayUsageRecorder` Protocol과 `GatewayUsageOutcome`을 구현·소비하는 Runtime private adapter다.
이는 `operations` interface가 아니며 static composition에는 instance가 없다.

Python shape와 호출 단위 input/output/domain-error 의미만 module interface다. Metric label, readiness
판정, reporter cadence, failure 격리와 공개 projection은 각각 policy 또는 lifecycle/operational
boundary다. `RuntimeConfig`, `load_runtime_config()`와 `build_app()`은 Runtime 내부
configuration/composition entry이며 cross-module 업무 interface가 아니다. `operations.reset`, managed
gateway report snapshot/ack와 logging helper도 Runtime 내부다.

`EncryptedDiagnosticCapture`는 Delivery가 제공한 `DiagnosticCapture` port의 Runtime private adapter다.
`decrypt_diagnostic_records`, bounded `query_diagnostic_records`와 `purge_diagnostic_consent`는 protected offline operator workflow helper이며
serving module interface나 HTTP/MCP surface가 아니다. Local SQLite schema v1, TTL/key/budget 의미는
[ADR 0027](../../decisions/0027-consent-gated-diagnostic-capture.md)의 persisted/operational boundary다.

`qm`은 Runtime-owned external CLI/composition root다. Source 명령은 Delivery의 existing managed
HTTP API만 소비하고 Control Plane implementation이나 Control DB에 직접 접근하지 않는다. `logs`는 local
Compose stdout provider이고 `diag`는 위 protected helper를 최대 7일/100건으로 제한해 사용한다. Exact
command, confirmation과 rollback 의미는 [ADR 0028](../../decisions/0028-interactive-operator-shell.md)을 따른다.

### Production composition ownership

- Production server의 concrete PostgreSQL adapter는 Runtime만 조립한다. Control Plane candidate staging과
  Assurance offline CLI는 각 bounded workflow에서만 별도 composition root가 될 수 있다.
- Static composition은 `query_man.managed`를 import하지 않는다. 명시적으로 managed authority를 고른
  composition만 package를 지연 import하고 Control store, admin adapter, reloader와 reporter를 조립한다.
- Runtime은 concrete `SourceRegistry`를 ordinary consumer에는 `SourceReader`, managed reloader에는
  `SourceProjectionWriter`로 좁혀 주입한다.
- Catalog/query adapter는 provider의 `RuntimeCatalogProvider`와 `RuntimeQueryExecutor`를 만족해야 한다.
  Runtime은 required method가 callable인지 조립 직후 검사하고 누락되면 ready 전에 `TypeError`로
  fail-closed한다.
- Bootstrap Catalog에는 exposed scalar-domain OID erasure를 막는 static guard를 켠다. Managed/Control
  staging의 기본 Catalog에는 켜지 않으며, 그 경로는 current launch serving에 참여하지 않는다.
- Delivery에는 Gateway/application service만 전달한다. Persistence, catalog와 executor private 구현을
  직접 주입하지 않는다.

### Current launch composition and RLS quarantine

- Current first launch는 reviewed static 두 source, `bootstrap` authority와 단일 `query-man` replica다.
- Bootstrap manifest 또는 주입된 registry에 RLS가 하나라도 있으면 listener/provider 생성 전에
  composition이 실패한다.
- Managed cold RLS record는 `RUNTIME_VALIDATION_REJECTED`가 되고 registry에 projection되지 않는다.
  Managed publish/rotate도 기존 `SOURCE_VALIDATION_FAILED`로 끝난다.
- Bootstrap과 managed authority를 합치거나 실패 시 서로 fallback하지 않는다.
- Managed implementation과 RLS type/code/history는 보존하지만 first launch에는 managed package import,
  Control DB, admin route, mutation, hot onboarding, reload와 observation task가 참여하지 않는다.

이는 Python interface가 아니라 [ADR 0025의 RLS quarantine](../../decisions/0025-static-non-rls-first-launch.md#2-rls-quarantine)과
composition/lifecycle invariant다. RLS serving 또는 managed launch 활성화는 별도 영향,
migration과 rollback 승인이 필요하다.

### Startup sequence and failure cleanup

```text
config/logging/capture key validate -> operations reset -> source authority 하나 선택
-> RLS inventory guard -> catalog/query capability 검사
-> metadata/query/access/gateway 조립
-> managed only: managed package 지연 import, Control store/admin/reloader 조립과 initial sync
-> inventory reconcile -> source별 bounded metadata probe -> capture worker start
-> managed only: reload/report task 시작
-> MCP child lifespan enter -> ready/degraded serving
```

필수 config, inventory 또는 dependency 초기화가 실패하면 ready로 전환하지 않는다. Managed sync/probe와
background task 생성 뒤 MCP child `enter`가 실패하면 Runtime은 자신이 소유한 최상위 resource를 다음
순서로 정리한다.

```text
reload task cancel/await -> capture bounded close -> query executor immediate close -> catalog close
-> metadata close (소유한 store 포함) -> source store close
```

진입하지 못한 child에는 `exit`를 호출하지 않는다. Child는 `enter` 중 만든 partial resource를 스스로
정리한다. Runtime은 parent 최상위 resource만 같은 identity당 정확히 한 번 close/cancel 시도하고, 한
단계가 실패해도 나머지를 계속 정리한다. Warning에는 고정 step 이름만 남기고 최초 startup exception을
그대로 다시 발생시킨다.

### Shutdown sequence and drain rules

```text
signal/request -> readiness와 새 admission close -> listener graceful handling
-> managed task cancel -> executor bounded drain -> capture 최대 2초 bounded close
-> executor/catalog/metadata/store close
```

Grace 안에 끝나지 않은 queued/active query는 cancel되고 PostgreSQL rollback으로 끝난다. HTTP/MCP
disconnect도 같은 task-cancel 경로를 쓴다. 정상 shutdown은
`RuntimeQueryExecutor.drain(configured grace)`를 직접 호출하며 optional method 탐색으로 건너뛰지 않는다.
SQL policy나 metadata revision release에서는 old/new 값을 번역하거나 mixed serving fleet를 만들지 않고,
old process와 source connection을 drain한 뒤 route 밖에서 새 baseline을 검증한다.

### Health, configuration and artifact rules

- `/health`는 process liveness만, `/ready`는 source ID 없는 aggregate 상태만 공개한다.
- API `/ready`는 `ready|degraded`에 HTTP 200을 반환하지만 Compose는 body가 정확히
  `{"status":"ready"}`일 때만 healthy다.
- Source/component detail과 metric은 operator surface에만 있다. Counter와 health는 replica-local이며
  restart 후 초기화된다.
- Bootstrap loopback에 token/policy가 없으면 anonymous query-only compatibility caller를 쓴다. Legacy
  token, version 2 policy file과 OAuth resource-server settings는 상호 배타적이고 non-loopback에서 셋 다
  없으면 startup이 실패한다.
- OAuth는 issuer, audience, query/MCP/operator scope를 함께 요구하고 optional realm role/group을 받는다.
  Diagnostic capture는 access-policy consent authority가 없으므로 OAuth와 함께 설정하면 startup이 실패한다.
- Managed mode는 anonymous/legacy token을 거부하고 version 2 policy의 non-admin query+operator identity
  또는 OAuth query+operator scope를 요구한다. Visibility/admin/cancel 의미는 Delivery 소유다.
- PostgreSQL 18.6 serving, PostgreSQL 18.4 recovery fixture, Python 3.14 slim과 uv 0.9.18 upstream은
  readable tag와 OCI digest를 함께 고정한다. 정확한 pin authority는 `Dockerfile`과 `compose.yaml`이다.
- Release build는 approved Git revision을 `QUERY_MAN_VCS_REF`로 받아
  `org.opencontainers.image.revision`에 보존한다. `unknown` label/mutable tag만 있는 image는 release
  artifact가 아니며 실제 application image digest는 protected change record에 남긴다.

Build, exact-ready와 artifact 확인 절차는 [Operations Guide](../../operations.md#artifact-preparation)에
둔다. Repository 문서는 실제 protected-environment execution evidence를 대신하지 않는다.

### Preserved managed runtime outside first launch

Managed path는 same-repository `query_man.managed` package, `compose.acceptance.yaml` fixture overlay와
CI `managed-acceptance` lane에 보존한다. CI `core-static`과 container lane은 base static fixture만 쓴다. 별도
활성화 시 empty registry에서 시작하고 Control DB의 source와 verified membership만 authority로 사용하며
filesystem으로 복구하지 않는다. Version 2 access policy 또는 OAuth resource-server settings, stable replica ID, initial sync, inventory
reconcile, metadata probe와 reload task가 필요하다.

Runtime은 Control Plane의 공개 replica/resource/gateway writer만 소비한다. Reporter는 fixed Control pool과
process-local write lock을 재사용하며 startup, query result, readiness와 shutdown 성공을 바꾸지 않는
best-effort work다. Raw exception, SQL, credential, tenant와 query ID를 payload에 넣지 않는다. Exact
generation/fencing/cadence/reason/projection 의미는
[Control Plane observability](../control-plane/observability.md)에 두고 이 문서에 복제하지 않는다.

### Protected operational boundary (`LAUNCH-02`)

Repository는 static inventory, image pin, exact-ready acceptance와 freeze/rollback 절차만 정의한다. 실제
TLS, secret 설치·회전, backup/restore 확인, target route, cutover와 rollback은 `LAUNCH-02`다. 대상,
접근 권한, artifact digest, stop condition과 change-record owner를 확인한 별도 실행 승인 뒤 수행하고
append-only evidence를 남긴다.

Serving 중 source manifest/budget/access policy, verified dataset, source DDL/role/grant/settings와
application/PostgreSQL artifact를 동결하며 정상 business DML은 허용한다. 설명되지 않은 inventory drift,
RLS source, exact-ready 실패 또는 artifact 식별 불가는 route stop condition이다. 상세 절차는
[Operations Guide](../../operations.md#static-non-rls-first-launch)를 따른다.

## 소비 인터페이스와 전제

| Provider | Runtime이 소비하는 공개 interface | 의무와 금지선 |
|---|---|---|
| [Source Catalog](../source-catalog/README.md) | Registry construction capability, `SourceReader`, managed-only `SourceProjectionWriter` | Source 정책을 Runtime branch로 재정의하지 않음 |
| [Metadata](../metadata/README.md) | Service/store, immutable snapshot, `RuntimeCatalogProvider` lifecycle | Required callable을 ready 전에 검사; private cache에 의존하지 않음 |
| [Guarded Query](../guarded-query/README.md) | `RuntimeQueryExecutor` admission/drain/invalidate/close lifecycle | Direct drain/cancel/rollback 순서를 보존 |
| [Control Plane](../control-plane/README.md) | Managed-only store/reloader와 replica/resource/gateway writer | Current static launch에는 조립하지 않고 private table을 읽지 않음 |
| [Delivery](../delivery/README.md) | Gateway/routes/MCP factory, `BearerAuthenticator`, parent middleware와 transport lifespan | Wire/auth/error 의미를 Runtime이 재정의하지 않음 |
| [Assurance](../assurance/README.md) | Bootstrap verified membership 또는 managed Control membership, container acceptance | Runtime은 verified 결과를 판정하지 않음 |

Concrete implementation을 조립할 권한은 provider의 private table/type을 업무 호출에 사용할 권한이 아니다.

## 불변조건

- Production concrete 조립은 Runtime에 두고 Control staging/Assurance offline CLI 예외를 bounded workflow
  밖으로 확대하지 않는다.
- Current launch는 reviewed static 두 source, non-RLS와 단일 serving replica다.
- RLS는 provider 생성 전에 거부하고 managed cold RLS record는 registry에 projection하지 않는다.
- Bootstrap/managed authority를 섞거나 failure fallback하지 않는다.
- Required capability 누락/startup 실패는 ready 전에 끝내고 parent resource를 정해진 순서로 누출 없이
  정리한다.
- Shutdown은 readiness/admission을 먼저 닫고 active work를 bounded drain한다.
- Public health에 source ID, component detail과 credential state를 노출하지 않는다.
- Secret, token, DSN/password, SQL과 question을 일반 structured log에 넣지 않는다. Configured capture는
  active consent, authorized source, AES-GCM, HMAC subject, maximum 7-day TTL과 bounded fail-open 규칙을 모두
  만족할 때만 별도 SQLite에 저장한다.
- Source별 차이를 Runtime composition branch로 만들거나 shared graph를 in-place 변경하지 않는다.
- Release artifact는 pinned upstream과 approved VCS revision으로 식별하며 repository 상태를 실제 실행
  evidence로 주장하지 않는다.

## 모듈 내부 변경

Official interface와 별도 config/policy/lifecycle/operation 의미가 같다면 dependency-construction helper,
lifespan context, private task bookkeeping, process-local counter 내부와 Dockerfile layer/cache를 독립적으로
정리할 수 있다. Delivery/Runtime interface, container/toolchain artifact는 coordinating agent가 owner와 writer 순서를
지정한다.

## 사용자 승인이 필요한 경계 변경

다음 의미가 달라지면 구현을 멈추고 실제 범주와 provider/consumer 영향을 구분해 승인받는다.

| 변경 범주 | Runtime에서 멈춰야 하는 예 |
|---|---|
| Module interface | Operations sink/DTO shape·signature·호출 의미, 소비하는 provider lifecycle Protocol |
| External API/wire | Health/readiness status, HTTP code/body, public/operator disclosure |
| Policy/compatibility identity | Source authority, RLS admission, image pin/revision identity, exact-ready 의미 |
| Safety/lifecycle invariant | Startup/sync/probe, admission/drain/cancel/rollback/close와 failure cleanup 순서 |
| Ownership/composition boundary | Adapter wiring 위치, bootstrap/managed fallback, reporter ownership, serving topology |
| Protected operational procedure | Inventory, DDL/role/settings/artifact freeze, cutover/rollback과 stop condition |

Environment variable의 required/optional/default, managed replica identity/cadence/fencing, metric
name/label/cardinality와 log/redaction allowlist도 실제 interface·policy·invariant 영향을 밝혀 승인받는다.
Protected environment action은 repository/procedure 승인과 별도로 access, scope, target, stop condition과
change-record 책임을 확인한 실행 승인이 필요하다. 과거 evidence는 수정·삭제하지 않는다.

## 검증

기본 Runtime gate:

```text
uv run pytest tests/test_registry.py tests/test_runtime_config.py tests/test_operations.py \
  tests/test_server.py tests/test_oauth_authentication.py tests/test_http.py
uv run pytest tests/test_managed_mode.py tests/test_managed_operations.py \
  tests/test_managed_runtime_startup_cleanup.py tests/test_managed_http.py
```

| 변경 범위 | 추가 검증 |
|---|---|
| Lifecycle/disconnect | Query/MCP와 관련 integration test |
| Managed reload/reporter | `test_managed_mode.py`, `test_managed_operations.py`, source-admin/Control startup test |
| Container/artifact | `docker compose config --quiet`, approved revision build, `./scripts/verify-container.sh`, `uv run query-man-verify` |

Built image의 OCI revision label/digest와 Compose exact-ready를 확인한다. Protected deployment evidence는
별도 실행 승인 뒤에만 append하며, 완료 전 root 전체 gate는 coordinating agent가 실행한다.

## 집중해서 읽을 범위

| 작업 | 먼저 읽을 범위 |
|---|---|
| Environment/source authority | `runtime/config.py`, `test_runtime_config.py`, ADR 0025; OAuth이면 ADR 0029와 `delivery/authentication.py` |
| Production composition/startup cleanup | Static은 `runtime/composition.py`, managed는 `managed/runtime.py`의 composition/lifespan symbol, `delivery/app.py` child interface, `test_managed_runtime_startup_cleanup.py`, `test_managed_mode.py` |
| Health/logging/capture/operator shell/shutdown | `runtime/operations.py`, `runtime/diagnostic_capture.py`, `runtime/operator_shell.py`, `runtime/server.py`, 직접 consumer와 `test_operations.py`, `test_diagnostic_capture.py`, `test_operator_shell.py`, managed `test_managed_operations.py`, `test_server.py` |
| Container/image/readiness | `Dockerfile`, base `compose.yaml`, managed fixture면 `compose.acceptance.yaml`, `verify-container.sh`, [Operations Guide](../../operations.md)와 관련 acceptance |
| Preserved managed path | `managed/runtime.py`, [Control Plane](../control-plane/README.md), `test_managed_mode.py`, `test_managed_operations.py`, `test_managed_http.py`와 관련 Control test |
| Protected procedure/execution | [Operations Guide](../../operations.md); 실제 실행이면 승인 범위와 append-only evidence schema |

`delivery/app.py` route/middleware를 바꾸면 Delivery 문서와 external API test까지 읽는다. Metadata ranking, SQL AST
walker, Control private table과 parked RLS/encoding/cost/trace proposal은 소비 interface나 승인된 lifecycle을
바꾸지 않는 한 읽을 필요가 없다.
