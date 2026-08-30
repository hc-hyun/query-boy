# Runtime Module

Status: Physical package boundary active

## 목적

### 30초 요약

Runtime은 Git-reviewed YAML source authority를 한 번 load해 Source Catalog, Metadata, Guarded Query와
Delivery를 production process로 조립한다. Configuration, startup/readiness, safe logging, diagnostic
capture와 shutdown cleanup을 소유한다.

`qm source list|show|validate`는 repository YAML을 읽는 local read-only operator CLI다. Runtime에는
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

- Source YAML schema, metadata/query/auth policy의 업무 의미
- Source/Control DB persistence, admin mutation, generation/reload/replica reporter
- Protected credential 설치, DB DDL, backup, route cutover와 deployment approval

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`runtime/config.py`](../../../src/query_man/runtime/config.py) | Environment parsing, cross-field validation, retired-setting rejection |
| [`runtime/composition.py`](../../../src/query_man/runtime/composition.py) | Production provider composition, lifespan와 startup probes |
| [`runtime/server.py`](../../../src/query_man/runtime/server.py) | Uvicorn entrypoint와 stop-accepting signal handling |
| [`runtime/operations.py`](../../../src/query_man/runtime/operations.py) | Safe logging, counters, health/readiness projection |
| [`runtime/diagnostic_capture.py`](../../../src/query_man/runtime/diagnostic_capture.py) | Consent-gated encrypted local capture lifecycle |
| [`runtime/operator_shell.py`](../../../src/query_man/runtime/operator_shell.py) | `qm` status/logs/diag와 local YAML source commands |
| [`compose.yaml`](../../../compose.yaml), [`Dockerfile`](../../../Dockerfile) | Serving artifact/topology |
| [`test_runtime_config.py`](../../../tests/test_runtime_config.py), [`test_runtime_startup_cleanup.py`](../../../tests/test_runtime_startup_cleanup.py), [`test_operator_shell.py`](../../../tests/test_operator_shell.py) | Focused tests |

## 제공 인터페이스와 소유 경계

`OperationalState`의 `increment`, `observe`, `set_source_health`, `reconcile_sources`,
`set_component_health`, `set_accepting`, `public_status`, `snapshot`이 domain/Delivery가 소비하는 operations
interface다. 일반 request path는 operations sink 실패 때문에 query cleanup을 실패시키지 않는다.

`build_app`은 production composition root다. `SourceRegistry.load`, concrete PostgreSQL catalog/query,
Metadata service, Gateway/Delivery와 OAuth/capture adapter를 연결한다. Ordinary consumer에는 concrete
registry 대신 `SourceReader`를 주입한다. Assurance offline CLI 이외의 코드가 production concrete
implementation을 조립하지 않는다.

Startup은 configuration/YAML load, exact inventory와 RLS quarantine, provider capability 및 bounded DB
probe, application/lifespan 진입 후 accepting/ready 순서다. 실패하면 ready가 되지 않으며 생성한 parent
resource를 역순으로 정확히 한 번 close 시도하고 최초 오류를 보존한다.

Shutdown은 accepting 중단, active query bounded drain, 남은 query cancel/rollback, capture flush/close,
query executor, catalog와 metadata close 순서다. Optional method 탐색으로 required cleanup을 건너뛰지
않는다.

`qm source list`는 sanitized source summary, `show <source-id>`는 password 값을 제외한 human-readable
manifest, `validate`는 source/budget/verified YAML의 consistency를 표시한다. 이 명령은 파일이나 DB를
변경하지 않으며 실행 중 process를 reload하지 않는다.

Encrypted capture의 persisted/privacy/TTL/fail-open lifecycle은
[ADR 0027](../../decisions/0027-consent-gated-diagnostic-capture.md)이 exact contract입니다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | `SourceRegistry.load`, `SourceReader`, reader policy | Git working tree/release artifact가 authority; fallback 없음 |
| Metadata | Application/lifecycle와 concrete catalog | Required callable을 ready 전에 확인 |
| Guarded Query | Delivery executor lifecycle | Direct drain/cancel/rollback/close 순서를 보존 |
| Delivery | `build_http_app`, auth/capture contracts | Wire semantics를 Runtime에서 재정의하지 않음 |
| Assurance | Verified YAML parser for local `qm source validate`, container acceptance | Runtime은 expected result를 판정하지 않음 |

## 불변조건

- Git-reviewed YAML만 source authority이며 DB/managed/filesystem fallback 간 선택 모드는 없다.
- Retired managed environment는 값이나 secret을 노출하지 않고 fail-closed한다.
- RLS 또는 required inventory/capability/probe 실패는 listener readiness 전에 거부한다.
- Startup partial failure와 shutdown은 owned resource를 정해진 순서로 leak 없이 정리한다.
- Log/status/error에 credential, Authorization header, SQL literal과 internal DB error를 남기지 않는다.
- `qm source`는 local read-only이며 mutation, credential resolution 출력, runtime reload를 하지 않는다.
- Protected environment action은 repository merge와 별도 승인이 필요하다.

## 모듈 내부 변경

Configuration/wire/lifecycle 의미를 보존하는 private parsing, composition helper, metric storage와 CLI
rendering 정리는 module 내부 변경이다.

## 사용자 승인이 필요한 경계 변경

- Environment required/default/cross-field와 retired-setting behavior
- Production composition ownership, startup/readiness/shutdown/cancel/cleanup lifecycle
- Operations projection와 `/health`, `/ready`, metrics wire
- `qm` command/argument/output/exit semantics
- Serving topology, container health, source inventory와 deployment/rollback procedure
- Hot reload, managed authority, Control DB나 admin mutation 재도입

## 검증

```bash
uv run pytest tests/test_runtime_config.py tests/test_runtime_startup_cleanup.py \
  tests/test_operations.py tests/test_operator_shell.py tests/test_server.py
```

Container/topology 변경은 container acceptance를, shutdown/socket 변경은 integration/soak를 함께
실행한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| Environment | `runtime/config.py`, `.env.example`, `test_runtime_config.py` |
| Composition/startup cleanup | `runtime/composition.py`, provider lifecycle, `test_runtime_startup_cleanup.py` |
| Operator CLI | `runtime/operator_shell.py`, SourceRegistry/VerifiedQueryRegistry, `test_operator_shell.py` |
| Operations/logging | `runtime/operations.py`, direct consumer, focused test |
| Server/container | `runtime/server.py`, `Dockerfile`, `compose.yaml`, operations guide와 acceptance |
