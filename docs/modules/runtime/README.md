# Runtime Module

Status: Active

## 30초 요약

Runtime는 environment를 검증하고 production registry, catalog, query, gateway와 HTTP lifecycle을 한 번
조립합니다. Startup admission, readiness, graceful shutdown과 local `qm source validate`를 소유합니다.

## 책임과 interface

- `load_runtime_config`: environment parsing, cross-field와 retired-setting rejection
- `build_app`: 유일한 production composition root
- Server signal handling, stop-admission, drain/cancel과 cleanup deadline
- Safe structured logging, component health와 bounded process metrics
- `qm source validate`: local package validation만 수행하는 read-only CLI

Config는 source directory와 budget file, bind/log, authentication, metadata cache, shutdown grace를
제공합니다. Password/token 값을 exception이나 representation에 포함하지 않습니다. Non-loopback bind의
인증 누락, conflicting authority와 retired setting은 startup을 fail-closed합니다.

## 코드 지도

| 위치 | 책임 |
|---|---|
| `runtime/config.py` | Environment validation |
| `runtime/composition.py` | Concrete providers, startup probes와 lifespan cleanup |
| `runtime/operations.py` | Safe events, counters, health/readiness projection |
| `runtime/operator_shell.py` | Local `source validate` command |
| `runtime/server.py` | Uvicorn entrypoint와 shutdown signal/deadline |

Runtime은 owner module의 public leaf interface만 조립합니다. Source YAML, metadata, SQL policy와 HTTP wire
의미를 재정의하지 않습니다.

## 불변조건과 승인

- Startup은 reviewed inventory 전체를 admission하며 partial serving하지 않습니다.
- Shutdown은 신규 admission을 막고 query cancel·rollback 뒤 모든 resource cleanup을 시도합니다.
- Cleanup 하나의 실패가 나머지 cleanup을 생략하지 않습니다.
- Config, composition ownership, readiness와 shutdown outcome, CLI command 의미는 별도 승인 대상입니다.

## 검증

```bash
uv run pytest tests/test_runtime_config.py tests/test_runtime_startup_cleanup.py \
  tests/test_operations.py tests/test_server.py tests/test_operator_shell.py
```

## 집중해서 읽을 범위

Config는 `config.py`와 runtime config tests, startup/cleanup은 `composition.py`, `server.py`와 lifecycle
tests, health/logging은 `operations.py`, CLI는 `operator_shell.py`와 focused test를 읽습니다.
