# Shared Access Verification — 2026-08-23

Status: Complete

## Scope

`CTRL-03`의 access-policy version 2 fail-closed cutover, 모든 authenticated identity의 implicit
active-source visibility, source-resolved `budget_profile`과 explicit admin capability를 검증한다.
아래 명령과 acceptance를 최종 worktree에서 실행했다.

검증 대상은 다음과 같다.

- Version 2 policy에는 `caller_id`, `tenant_id`, `token_env`와 `operator`만 있고 source scope가 없다.
- Version 1, `allowed_sources`와 `all_sources`가 남은 policy는 권한을 자동 확대하지 않고 거부한다.
- Managed mode는 policy file, non-admin query identity와 operator admin identity를 모두 요구하며
  단일 `QUERY_MAN_API_TOKEN`과 anonymous caller를 거부한다.
- Bootstrap anonymous/API-token identity는 query-only이고 모든 active bootstrap source를 본다.
- 서로 다른 query identity는 hot-add/deactivate를 포함해 같은 source inventory와 같은
  source-resolved budget 정의를 사용하며 request/caller가 tier를 override할 수 없다.
- Query identity는 모든 admin endpoint와 query cancel에서 거부된다. Operator는 query 권한에
  admin/cancel capability를 더한 boolean superset이다.
- Caller/tenant identity는 audit와 trusted RLS context에 남지만 source visibility, budget, quota와
  비용 dimension을 선택하지 않는다.

## Commands And Result

최종 worktree에서 다음 gate를 실행하고 정확한 결과를 기록한다.

```text
uv run pytest -q tests/test_access.py tests/test_runtime_config.py tests/test_http.py \
  tests/test_managed_mode.py tests/test_mcp.py tests/test_query.py \
  tests/test_source_admin.py tests/test_documentation.py
  PASS (130 passed)
uv run pytest
  PASS (297 passed, 33 deselected)
uv run pytest -m integration
  PASS (21 passed, 309 deselected)
uv run ruff check .
  PASS
uv run mypy src
  PASS (26 source files)
uv run pytest tests/test_documentation.py -q
  PASS (6 passed)
uv run ruff check tests/test_documentation.py
  PASS
git diff --check
  PASS
```

Result: PASS

## Acceptance Matrix

| Boundary | Expected evidence | Result |
|---|---|---|
| Policy schema | Version 2 shared policy가 load되고 version 1/scope/unknown field가 fail-closed함 | PASS |
| Secret validation | Token length·duplicate 검증이 유지되고 error/log에 token이 없음 | PASS |
| Managed authentication | Policy/query/admin 중 하나라도 없거나 API token/anonymous이면 startup 전에 거부됨 | PASS |
| Bootstrap compatibility | Local/API-token identity가 query-only이고 admin/cancel을 수행하지 못함 | PASS |
| Shared visibility | 두 query identity가 startup, hot-add와 deactivate 뒤 같은 active source 목록을 봄 | PASS |
| HTTP/MCP parity | 두 transport의 list/context/query가 같은 shared-access 결과를 사용함 | PASS |
| Source existence | Unknown source가 metadata/SQL/slot 전에 bounded `SOURCE_NOT_FOUND`로 거부됨 | PASS |
| Budget parity | 두 identity에 같은 source profile budget이 적용되고 caller/request override가 없음 | PASS |
| Admin separation | Query token은 모든 current admin endpoint와 cancel에서 403이고 admin만 mutation/cancel함 | PASS |
| RLS/audit | Stable caller/tenant와 transaction-local trusted RLS isolation이 유지됨 | PASS |
| Change surface | Control DB migration과 dependency 추가가 없음 | PASS |

모든 admin endpoint 검증은 `/admin/health`, `/admin/metrics`, source publish, credential rotation,
verified contract, rollback, metadata resume와 deactivate를 포함한다. Query cancel은 `/admin` prefix가
아니지만 같은 admin capability 경계로 검증한다. Invalid body validation만 확인하지 말고 유효한
최소 request가 authorization gate에서 거부되는지 확인한다.

## Operational Cutover Acceptance

1. Version 2 policy에 서로 다른 token environment를 쓰는 query identity 둘과 operator admin
   identity를 준비한다.
2. Stale version 1/scope policy로 startup이 실패하고 secret이 출력되지 않는지 확인한다.
3. Traffic 밖의 managed replica가 Control DB inventory를 적용한 뒤 두 query identity의 `/sources`
   결과와 effective metadata revision이 같은지 확인한다.
4. Admin으로 source를 publish/deactivate하고 poll 뒤 두 query identity의 inventory가 함께 바뀌는지
   확인한다. Caller별 grant 변경이나 service restart를 수행하지 않는다.
5. 두 identity의 대표 query가 같은 source-resolved budget limits를 통과하거나 같은 reason으로
   거부되고, query token의 admin/cancel 요청은 모두 403인지 확인한다.

Token 값, question 원문, SQL, credential, DSN과 database error detail은 evidence에 기록하지 않는다.

통합 실행 뒤 `query_man_control_test_%` disposable database와 container migration 임시 directory가
각각 0개임을 확인했다.

## Intentionally Unchanged

- Access policy는 deployment configuration이며 Control DB caller-grant table을 만들지 않는다.
- `operator` boolean을 재사용하고 viewer/operator/approver role enum이나 exclusive admin route
  hierarchy를 추가하지 않는다.
- `budget_profile`이 유일한 resource tier이며 caller/user/organization binding과 별도
  `cost_tier`를 만들지 않는다.
- Source-native RLS row isolation과 audit identity는 유지한다. 같은 source visibility가 같은 row
  결과를 뜻하지 않는다.
- 이 cutover에는 Control DB migration, policy write-back/import helper와 새 dependency가 없다.

## Future Triggers

- User/organization별 source ACL이나 tier override가 실제로 필요해지면 stable principal identity,
  threat model, migration과 revocation semantics를 별도 ADR로 설계한다.
- Query/admin을 상호 배타적인 endpoint role로 분리해야 할 규제 요구가 생기면 boolean capability
  superset을 대체하는 최소 role model과 break-glass 절차를 별도로 검토한다.
- Access policy hot reload가 필요해지면 atomic revision, replica convergence와 rollback 계약을 먼저
  정의한다.
