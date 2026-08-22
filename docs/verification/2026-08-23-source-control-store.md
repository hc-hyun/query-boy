# Source Control Store Verification — 2026-08-23

## Scope

No-deploy onboarding의 manifest 호환 계층, encrypted secret과 immutable control-plane
source generation 저장 계약을 검증했다.

## Evidence

| Scenario | Result |
|---|---|
| v0 `budget` manifest를 v1 `budget_profile`로 migration | PASS |
| 알 수 없는 미래 manifest version | Fail-closed |
| Manifest와 저장 document에서 평문 secret 분리 | PASS |
| AES-GCM ciphertext를 source ID와 generation에 binding | PASS |
| Profile + metadata + active pointer atomic publish | PASS |
| 새 encrypted credential generation publish | PASS |
| 이전 profile/credential generation rollback | PASS |
| Active source deactivate | PASS |

```text
uv run ruff check .                                      PASS
uv run mypy src                                          PASS (22 source files)
uv run pytest tests/test_registry.py tests/test_secrets.py
                                                           PASS
uv run pytest -m integration tests/test_source_store.py -q PASS (1 test)
```

관리자 API와 runtime hot reload acceptance는 다음 ONB 작업에서 이 저장 계약을 사용해
별도로 검증한다.
