# Runtime Module

Status: Physical package boundary active

## 목적

### 30초 요약

Runtime은 Git-reviewed source package와 budget authority를 한 번 load해 Source Catalog, Metadata, Guarded Query와
Delivery를 production process로 조립한다. Configuration, startup/readiness, safe logging, diagnostic
capture와 shutdown cleanup을 소유한다.

Load 대상은 `config/sources/`의 reviewed immediate child package 전체다. Runtime에 별도 source allowlist나
source별 composition branch는 없으며 inventory 변경은 새 artifact의 배포·재시작 뒤 반영된다.

Base `compose.yaml`은 application-only이며 source PostgreSQL을 provision하지 않는다. 재현 가능한
로컬·CI의 작은 합성 source database는 명시적인 `compose.fixture.yaml` overlay만 소유한다. 이
test-local source는 production package inventory를 복제하지 않는다.

`qm source list|show|validate`는 repository source package와 budget을 읽는 local read-only operator CLI다. Runtime에는
source mode selector, Control DB, managed fallback, hot reload 또는 source mutation API가 없다. Retired
managed environment가 남아 있으면 조용히 무시하지 않고 startup configuration error로 거부한다.

## 소유 책임

- Environment를 `RuntimeConfig`로 strict validation
- Production registry/catalog/query/gateway/HTTP/MCP concrete composition
- Startup probe, readiness와 current inventory/RLS 확인
- Operations counters/latency/source health와 secret-safe JSON logging
- Diagnostic capture storage lifecycle와 `qm` operator CLI
- Stop accepting, query drain/cancel, rollback과 dependency close 순서
- Container/image/Compose serving topology

## 소유하지 않는 책임

- Source package schema, `views.sql`, metadata/query/auth policy의 업무 의미
- Source/Control DB persistence, admin mutation, generation/reload/replica reporter
- Protected credential 설치, DB DDL, backup, route cutover와 deployment approval

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`runtime/config.py`](../../../src/query_man/runtime/config.py) | Environment parsing, cross-field validation, retired-setting rejection |
| [`runtime/composition.py`](../../../src/query_man/runtime/composition.py) | Production provider composition, lifespan, shared shutdown deadline와 startup probes |
| [`runtime/server.py`](../../../src/query_man/runtime/server.py) | Uvicorn entrypoint, shutdown deadline anchor와 stop-accepting signal handling |
| [`runtime/operations.py`](../../../src/query_man/runtime/operations.py) | Safe logging, counters, health/readiness projection |
| [`runtime/diagnostic_capture.py`](../../../src/query_man/runtime/diagnostic_capture.py) | Consent-gated encrypted local capture lifecycle |
| [`runtime/operator_shell.py`](../../../src/query_man/runtime/operator_shell.py) | `qm` UI, argument parsing, rendering과 entrypoint |
| [`runtime/operator_backend.py`](../../../src/query_man/runtime/operator_backend.py) | Operator HTTP/Docker/diagnostic I/O, settings와 local source-package loading |
| [`compose.yaml`](../../../compose.yaml), [`compose.fixture.yaml`](../../../compose.fixture.yaml), [`Dockerfile`](../../../Dockerfile) | Application-only serving artifact와 explicit local/CI fixture topology |
| [`test_runtime_config.py`](../../../tests/test_runtime_config.py), [`test_runtime_startup_cleanup.py`](../../../tests/test_runtime_startup_cleanup.py), [`test_operator_shell.py`](../../../tests/test_operator_shell.py) | Focused tests |

## 제공 인터페이스와 소유 경계

`OperationalState`의 `increment`, `observe`, `set_source_health`, `set_source_query_health`,
`reconcile_sources`, `set_component_health`, `set_accepting`, `public_status`, `snapshot`이 domain/Delivery가
소비하는 operations interface다. Metadata reporter와 query reporter는 독립 상태를 기록해 한쪽의 성공이
다른 쪽의 장애를 덮지 않는다. 외부·admin projection은 source별 집계 상태만 유지한다. 두 reporter 중
하나라도 unavailable이면 unavailable, 아니면 metadata stale, initializing, healthy 순으로 집계한다.
Inventory reconcile 때 query reporter는 아직 관찰된 장애가 없는 `healthy`로 시작하고, metadata reporter는
startup probe 전까지 `initializing`이다. Query reporter 장애는 성공한 query `COMMIT`만 복구한다.
이 process-wide sink는 허용된 cross-cutting dependency이며 core package의 독립 추출이나 별도 telemetry
Protocol 주입을 약속하지 않는다. 일반 request path는 operations sink 실패 때문에 query cleanup을
실패시키지 않는다. `/admin/metrics`의 source별 `query_request_started`는 authorization을 통과한 query
요청의 process-local 누적값이며 collector가 replica별 증가량과 counter reset을 처리해 QPS로 변환한다.

`build_app`은 production composition root다. `SourceRegistry.load`, concrete PostgreSQL catalog/query,
Metadata service, Gateway/Delivery와 OAuth/capture adapter를 연결한다. Ordinary consumer에는 concrete
registry 대신 `SourceReader`를 주입한다. 다른 module은 production concrete implementation을 조립하지
않으며 Runtime에도 `views.sql` 실행 adapter나 administrator credential을 주입하지 않는다.

Runtime은 `build_http_app`이 제공하는 `state.mcp_app` child-lifespan handle을 사용하고, 자신이 만든
idempotent `state.shutdown_trigger`를 server entrypoint에 전달한다. 나머지 FastAPI state 배치를 모듈 간
API로 확대하거나 이를 위한 별도 DTO를 만들지 않는다.

Startup은 configuration/YAML load, reviewed package inventory와 RLS quarantine, provider capability 및 bounded DB
probe, application/lifespan 진입 후 accepting/ready 순서다. 실패하면 ready가 되지 않으며 생성한 parent
resource를 역순으로 정확히 한 번 close 시도한다. Cleanup은 첫 probe 전에 등록하며 한 단계가 실패해도
나머지를 계속한다. Startup, parent lifespan body 또는 child lifespan 오류는 cleanup 오류보다 우선 보존하고,
정상 shutdown에서만 최초 cleanup 오류를 호출자에게 돌려준다.

Shutdown은 하나의 Runtime-private trigger로 deadline 시작, Runtime readiness admission 중단과 query
executor admission 중단을 먼저 수행한 뒤 active query bounded drain, 남은 query cancel/rollback,
capture flush/close, query executor, catalog와 metadata close 순서로 진행한다. Signal, signal 없는 server
shutdown과 direct lifespan cleanup이 같은 idempotent trigger를 사용한다. Uvicorn 대기에 사용된 wall-clock
time을 차감한 remaining만 query executor에 전달하고, capture는 query drain 뒤 다시 계산한
`min(remaining, 2초)`만 받는다. 반복 signal은 deadline을 연장하지 않는다. Optional method 탐색으로
required cleanup을 건너뛰지 않는다.

이 deadline은 graceful wait의 cutoff이지 hard process-exit 시각이 아니다. Remaining이 0이어도
cancel/rollback, capture interrupt/drop과 모든 resource close를 시도하며 이 cleanup, Uvicorn의 초 단위
반올림·handoff와 child lifespan exit는 deadline 뒤에도 실행될 수 있다. 두 번째 SIGINT의 Uvicorn
force-exit처럼 ASGI lifespan 자체가 시작되지 않는 경로와 SIGKILL은 cleanup 보장 범위 밖이다.

`qm source list`는 sanitized source summary, `show <source-id>`는 password 값을 제외한 human-readable
manifest, `validate`는 exact two-file package와 source/budget consistency를 표시한다. 이 명령은 SQL을
읽거나 실행하지 않고 파일이나 DB를 변경하지 않으며 실행 중 process를 reload하지 않는다.

Encrypted capture의 persisted/privacy/TTL/fail-open lifecycle은
[ADR 0027](../../decisions/0027-consent-gated-diagnostic-capture.md)이 exact contract입니다.
Capture admission과 close 전이는 하나의 lifecycle lock으로 직렬화한다. Close는 새 enqueue를 먼저
막고 전달받은 예산 안에서 accepted queue를 drain한다. 마지막 최대 100ms를 남겨 active SQLite
connection interrupt와 대기 항목 drop을 시도한 다음 다른 Runtime cleanup을 계속한다. 표준 `sqlite3`는
이미 commit에 진입한 작업을 원자적으로 강제 종료하지 못하므로 worker가 bounded close 반환 뒤에도
남을 수 있고 in-process 종료시간을 보장하지 않으며, 이 경우 process 종료가 최종 격리 경계다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | `SourceRegistry.load`, `SourceReader`, reader policy | Git working tree/release artifact가 authority; fallback 없음 |
| Metadata | Application/lifecycle와 concrete catalog | Required callable을 ready 전에 확인 |
| Guarded Query | Delivery executor lifecycle | Stop admission 뒤 drain/cancel/rollback/close 순서를 보존 |
| Delivery | `build_http_app`, auth/capture contracts | Wire semantics를 Runtime에서 재정의하지 않음 |

## 불변조건

- Git-reviewed source package와 budget만 authority이며 DB/managed/filesystem fallback 간 선택 모드는 없다.
- 모든 reviewed package를 process 시작 때 load하며 source 이름·개수의 별도 Runtime 목록은 없다.
- Base serving topology는 source database를 provision하지 않으며 단일 합성 fixture DB는 explicit
  overlay에서만 시작한다.
- Retired managed environment는 값이나 secret을 노출하지 않고 fail-closed한다.
- RLS 또는 required inventory/capability/probe 실패는 listener readiness 전에 거부한다.
- Startup partial failure와 shutdown은 owned resource를 정해진 순서로 leak 없이 정리한다.
- Log/status/error에 credential, Authorization header, SQL literal과 internal DB error를 남기지 않는다.
- `qm source`는 local read-only이며 mutation, credential resolution 출력, runtime reload를 하지 않는다.
- Protected environment action은 repository merge와 별도 승인이 필요하다.

## 모듈 내부 변경

Configuration/wire/lifecycle 의미를 보존하는 private parsing, composition helper, metric storage와 CLI
UI/backend 분리는 module 내부 변경이다. `operator_shell.py`는 입력·표시를,
`operator_backend.py`는 외부 I/O와 local source loading을 소유한다. 둘은 같은 logical Runtime module이므로
container diagnostic dispatch 같은 내부 연결을 별도 module interface나 DTO로 승격하지 않는다.

## 사용자 승인이 필요한 경계 변경

- Environment required/default/cross-field와 retired-setting behavior
- Production composition ownership, startup/readiness/shutdown/cancel/cleanup lifecycle
- Operations projection와 `/health`, `/ready`, metrics wire
- `qm` command/argument/output/exit semantics
- Serving topology, container health, source inventory와 deployment/rollback procedure
- Hot reload, managed authority, Control DB나 admin mutation 재도입

## 검증

```bash
uv run ruff check src/query_man/runtime --select C901 --config "lint.mccabe.max-complexity=19"
uv run pytest tests/test_runtime_config.py tests/test_runtime_startup_cleanup.py \
  tests/test_operations.py tests/test_operator_shell.py tests/test_server.py
```

Container/topology 변경은 container acceptance를, shutdown/socket 변경은 integration/soak를 함께
실행한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Environment | `runtime/config.py`, `.env.example`, `.env.fixture.example`, `test_runtime_config.py` |
| Composition/startup/shutdown cleanup | `runtime/composition.py`, `runtime/server.py`, provider lifecycle, `test_runtime_startup_cleanup.py`, `test_server.py` |
| Operator CLI UI/parser | `runtime/operator_shell.py`, `test_operator_shell.py` |
| Operator backend/I/O | `runtime/operator_backend.py`, SourceRegistry, `test_operator_shell.py` |
| Operations/logging | `runtime/operations.py`, direct consumer, focused test |
| Server/container | `runtime/server.py`, `Dockerfile`, `compose.yaml`, `compose.fixture.yaml`, operations guide와 acceptance |
