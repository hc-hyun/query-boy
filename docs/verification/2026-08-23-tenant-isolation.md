# Tenant Isolation Verification — 2026-08-23

## Scope

Authenticated caller tenant 전달, RLS source staging, transaction-local context, pool reset과
non-disclosing authentication/authorization audit를 검증했다.

## Evidence

| Scenario | Result |
|---|---|
| Query body의 `tenant_id` 입력 | `400 INVALID_REQUEST` |
| RLS source인데 server tenant 없음 | `QUERY_REJECTED / TENANT_CONTEXT_REQUIRED` |
| Owner-rights view를 RLS source로 publish | Metadata validation reject |
| `security_invoker` RLS view와 NOBYPASS reader | Catalog staging PASS |
| 같은 pool: engineering tenant | engineering 2행만 반환 |
| 같은 pool: context 비움 | 0행; 이전 engineering context 비누출 |
| 같은 pool: quality tenant | quality 1행만 반환 |
| 인증 실패 audit | Token과 credential 비기록 |
| 비인가 source audit/response | Requested source 비기록, unknown과 동일 404 |

Fixture는 `tenant_ai.private_records`에 강제 RLS를 적용하고, catalog에는
`tenant_ai.record_overview` security-invoker view만 공개한다. 일반 MVP source의 allowed schema에는
`tenant_ai`가 없으므로 기존 metadata surface에는 추가되지 않는다.

```text
uv run ruff check .              PASS
uv run mypy src                  PASS (23 source files)
uv run pytest -q                 PASS (113 unit tests)
uv run pytest -m integration -q  PASS (13 integration tests)
uv run query-man-evaluate        PASS (16/16 cases, max 13,509 bytes)
uv run query-man-verify          PASS (9/9 verified SQL contracts)
```
