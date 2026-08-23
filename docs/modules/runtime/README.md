# Runtime Module

Status: Logical boundary; physical package split pending

## 목적

Runtime은 production server의 concrete implementation을 한 process로 조립하고, 안전하게
시작·운영·종료한다.
쉽게 말하면 업무 규칙을 만드는 곳이 아니라 이미 정해진 부품의 배선과 process lifecycle을
책임지는 composition root다.

Query Man은 현재 하나의 deployable process인 modular monolith다. Runtime이 모든 module을
알 수는 있지만 이 예외는 wiring과 lifecycle에만 사용하며 domain 업무 규칙을 이곳에 두지 않는다.

## 소유 책임

- Environment를 `RuntimeConfig`로 검증하고 안전한 기본값을 적용
- Process 전체 source authority를 mutually exclusive `bootstrap|managed` mode로 선택
- Registry, Metadata, Query, Control, Delivery와 Assurance dependency 조립
- Application/MCP child lifespan과 정상 진입 뒤 cleanup
- Initial Control DB sync, source inventory reconciliation과 bounded metadata probe
- Background source reload polling task
- Shutdown admission close, task cancel, bounded drain과 resource close 순서
- Uvicorn signal/graceful shutdown integration
- Aggregate liveness/readiness, component/source health와 replica-local operational counters
- Structured logging field allowlist, redaction과 process-level audit setup

## 소유하지 않는 책임

- Source/manifest, metadata, SQL와 authorization 업무 규칙
- HTTP/MCP request/response schema와 public error taxonomy
- Control DB schema/persistence 또는 source DB query implementation
- Source별 Python 분기와 미래 plugin/factory abstraction
- Public health endpoint에서 credential이나 source inventory를 공개하는 일

## 현재 코드 위치

- [`app.py`](../../../src/query_man/app.py): `build_app`, dependency composition, lifespan, reload loop와
  startup probe; middleware/routes/DTO는 Delivery 소유
- [`server.py`](../../../src/query_man/server.py): Uvicorn process와 shutdown signal ordering
- [`runtime_config.py`](../../../src/query_man/runtime_config.py): environment model/validation과
  `RuntimeConfig`
- [`operations.py`](../../../src/query_man/operations.py): `OperationalState`, safe formatter와 redaction
- [`__init__.py`](../../../src/query_man/__init__.py): package identity/version
- Focused tests: [`test_runtime_config.py`](../../../tests/test_runtime_config.py),
  [`test_operations.py`](../../../tests/test_operations.py),
  [`test_server.py`](../../../tests/test_server.py),
  [`test_http.py`](../../../tests/test_http.py)

`app.py`는 Delivery와 Runtime의 transition hot spot이다. Composition/lifespan symbol만 Runtime
소유이며 route 또는 wire schema를 함께 정리하지 않는다. `operations.py`의 상태를 다른 module이
기록할 수 있지만 상태 의미, metric label 허용 범위와 public projection은 Runtime 계약이다.

## 제공 계약

### Composition contract

- Production server의 concrete PostgreSQL adapters는 이 composition root에서 capability에 주입한다.
- Delivery는 Gateway/application service만 받고 persistence/executor internals를 직접 받지 않는다.
- Metadata와 Guarded Query는 Control DB implementation을 직접 import하지 않는다.
- Runtime-only dependency edge는 wiring/lifecycle 외의 업무 호출을 허용하지 않는다.
- Control Plane의 isolated candidate staging과 Assurance의 standalone offline CLI도 bounded composition
  root지만 production HTTP/MCP wiring을 소유하지 않는다.

### Startup contract

현재 startup의 의미상 순서는 다음과 같다.

```text
environment/configuration validate -> logging configure -> operations reset
-> source authority 선택
   bootstrap: filesystem source + verified contract load; Control dependency 없음
   managed: empty registry + verified map; Control DB/store/reloader compose
-> concrete modules compose
-> managed Control reloader initial sync
-> active inventory reconcile
-> source별 bounded metadata probe
-> managed background reload polling start
-> MCP child lifespan start
```

Bootstrap과 managed source/verified authority를 한 process에서 합치거나 서로 fallback하지 않는다.
Managed cold-start scan이 실패하면 empty inventory로 unavailable이며 filesystem source를 읽어 복구하지
않는다. Bootstrap은 Control DSN/encryption key를 거부하고 managed는 둘과 version 2 access policy를
요구한다.

필수 configuration 또는 dependency 초기화가 실패하면 ready로 전환하지 않는다. 현재는 Control
sync/probe와 reload task 생성 뒤 MCP child lifespan `enter` 자체가 실패하면 application cleanup
`finally`에 도달하지 않아 이미 연 pool/task의 역순 close를 보장하지 못하는 contract debt가 있다.
Startup failure cleanup을 보장한 것으로 가정하지 않으며 이 gap을 고치는 작업은 lifecycle test와
문서 갱신을 함께 한다.

### Shutdown contract

```text
signal/request shutdown
-> readiness와 query admission close
-> listener의 graceful request handling
-> reload task cancel
-> executor bounded drain
-> executor/catalog/metadata/store resources close
```

Grace 안에 끝나지 않은 queued/active query는 cancel되고 PostgreSQL rollback으로 끝나야 한다.
HTTP와 MCP disconnect도 같은 task-cancel 경로를 사용한다. Shutdown 중 새 non-health request는
service-shutting-down 의미로 거부한다.

현재 Query executor의 `stop_accepting`, `drain`, `invalidate`와 catalog의 `invalidate`는 실제
production lifecycle이 사용하는 capability지만 일부 Protocol에 완전히 표현되지 않은 contract
debt다. Type에 없다는 이유로 제거하지 않으며 정식 interface 변경은 사용자 승인을 받는다.

### Health and operations contract

- `/health`는 process liveness만 나타낸다.
- `/ready`는 source ID 없는 aggregate 상태만 공개한다.
- 상세 source/component health와 metric은 operator surface에서만 제공한다.
- 하나 이상의 usable source가 있는 degraded 상태는 service 가능으로 볼 수 있지만 initializing,
  unavailable과 shutting down은 ready가 아니다.
- Persisted metadata 복원은 readiness 근거가 될 수 있으나 source query connection의 실시간
  liveness 보장은 아니다.
- Counter/health는 replica-local이고 restart 후 초기화된다. Control DB authority로 사용하지 않는다.

### Runtime authentication configuration contract

- Bootstrap loopback에서 token/access-policy 설정이 없으면 Delivery의 anonymous query-only local
  compatibility caller를 사용한다.
- Legacy single token과 access-policy file을 동시에 설정하지 않으며 non-loopback bind에서 둘 다
  없으면 startup을 fail-closed한다.
- Managed mode는 single API token/anonymous를 거부하고 version 2 access policy의 non-admin query와
  explicit operator identity를 모두 요구한다.
- 모든 identity의 shared active-source visibility와 local/legacy query-only, operator admin/cancel
  의미는 Delivery 계약이며 Runtime은 source mode와 configuration 선택만 소유한다.

## 소비 계약

- [Source Catalog](../source-catalog/README.md)의 registry/configuration capability
- [Metadata](../metadata/README.md)의 service/catalog/store lifecycle
- [Guarded Query](../guarded-query/README.md)의 executor admission/drain/invalidate/close lifecycle
- [Control Plane](../control-plane/README.md)의 stores, reloader와 convergence semantics
- [Delivery](../delivery/README.md)의 Gateway/routes/MCP factory, parent middleware와 transport lifespan
- [Assurance](../assurance/README.md)의 bootstrap filesystem 또는 managed Control verified membership
  contract

## 불변조건

- Production concrete dependency 조립은 Runtime에 두고 Control staging/Assurance CLI 예외를 해당
  bounded workflow 밖으로 확대하지 않는다.
- MCP child lifespan에 정상 진입한 뒤 shutdown은 resource/task/pool을 누출하지 않는다. 위 startup
  enter failure gap을 현재 보장으로 오해하지 않는다.
- Shutdown에서는 readiness와 새 query admission을 먼저 닫고 active work를 bounded drain한다.
- Source metadata probe/staging은 source budget/time limit을 우회하지 않는다. Control scan/poll
  자체는 query budget이 아니라 configured reload interval과 Control DB pool 경계를 따른다.
- Environment secret, bearer token, DSN/password와 SQL/question을 structured log에 넣지 않는다.
- Public liveness/readiness에 source ID, internal component detail과 credential state를 노출하지 않는다.
- Source별 동작 차이를 composition branch로 만들지 않는다.
- Source authority mode를 source별로 섞거나 managed failure를 bootstrap file로 fallback하지 않는다.

## 모듈 내부 변경

다음은 configuration 이름/default, lifecycle 순서와 public health 의미를 보존할 때 독립적으로
변경할 수 있다.

- Dependency construction helper와 local variable 정리
- 동일한 cleanup을 만드는 lifespan context 정리
- 같은 poll/probe 동작을 만드는 task bookkeeping 개선
- Public state/label을 유지하는 operational counter 내부 개선
- Uvicorn integration의 같은 grace/exit 의미를 유지하는 정리

## 사용자 승인이 필요한 계약 변경

- Module 의존 방향, concrete adapter wiring 위치 또는 composition root 분산
- Environment variable, required/optional/default와 secure-mode validation 변경
- Bootstrap/managed authority, filesystem non-read/fallback와 access-policy requirement 변경
- Startup, reloader sync/probe와 shutdown admission/drain/close 순서 변경
- Executor/catalog lifecycle hook 이름과 의미 또는 disconnect propagation 변경
- Readiness/health status, HTTP status와 public/operator disclosure 변경
- Metric name/label/cardinality와 structured log/redaction allowlist 변경
- Reload interval, authority/convergence와 failed-apply 동작 변경
- Shutdown grace의 단위, timeout과 active/queued query 처리 변경

승인 요청에는 모든 affected module, in-flight request와 rolling process compatibility, failure cleanup 및
운영 runbook/test 영향을 포함한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_runtime_config.py tests/test_operations.py \
  tests/test_server.py tests/test_http.py tests/test_managed_mode.py
```

Lifecycle/disconnect 변경은 query/MCP/integration tests를, reload 변경은 source-admin tests를,
managed startup/authority 변경은 `tests/test_control_startup.py` integration을, 실제 process signal이나
container 경계는 container shutdown smoke를 추가한다. 완료 전 root `AGENTS.md`의 전체 gate를
실행한다.

## 집중해서 읽을 범위

Runtime 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 composition/config/operations/server code와 focused tests
3. 조립하거나 lifecycle을 호출하는 module의 공개 계약
4. Container runtime, mutually exclusive management authority와 MCP lifecycle ADR
5. `app.py` route/middleware를 건드릴 때 Delivery 계약

Metadata ranking, SQL AST walker와 store transaction 내부는 lifecycle 계약을 바꾸지 않는 한 읽을
필요가 없다.
