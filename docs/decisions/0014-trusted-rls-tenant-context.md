# ADR 0014: Trusted RLS Tenant Context

Status: Accepted

Date: 2026-08-23

## Context

RLS source는 같은 reader connection pool을 여러 tenant가 재사용한다. Tenant ID를 request
body, SQL text나 client-controlled setting에서 받으면 다른 tenant를 선택할 수 있고,
session-level setting이 pool에 남으면 다음 caller에게 행이 누출될 수 있다.

## Decision

Source manifest는 `tenant_isolation: none|rls`를 선언한다. RLS source는 `view` relation kind만
허용하며 catalog staging에서 모든 공개 view의 `security_invoker=true`, reader의
`NOSUPERUSER`와 `NOBYPASSRLS`를 확인한다. Owner-rights view는 RLS source로 publish하지
않는다.

Gateway는 bearer policy가 만든 `CallerContext.tenant_id`를 QueryService와 executor에 직접
전달한다. Query request와 MCP tool schema에는 tenant field를 추가하지 않는다. Executor는
read-only transaction 안에서 다음 값을 `set_config(..., true)`로 설정한다.

- `row_security=on`
- `query_man.tenant_id=<authenticated tenant>` for RLS source
- `query_man.tenant_id=''` for non-RLS source

Transaction-local setting은 commit, rollback, timeout과 cancel 뒤 자동 reset된다. Executor는
각 transaction에서 setting을 덮어쓰고 identity probe로 expected tenant와 `row_security=on`을
확인한 뒤에만 EXPLAIN/query를 실행한다. RLS source에 server tenant context가 없으면
`TENANT_CONTEXT_REQUIRED`로 거부한다.

Authentication과 authorization deny audit는 token, requested source와 credential을 기록하지
않고 caller/tenant와 operation만 기록한다. 허용되지 않은 source와 존재하지 않는 source의
public 응답은 계속 동일한 `404 SOURCE_NOT_FOUND`다.

## Consequences

- Pool connection 하나를 engineering → empty context → quality tenant 순서로 재사용해 각
  tenant 행만 반환하는 integration test가 reset 경계를 검증한다.
- RLS policy는 고정된 `query_man.tenant_id`를 읽어야 하며 manifest가 setting 이름을 선택할
  수 없다.
- Verified query admin 실행도 endpoint caller의 trusted tenant를 사용한다.
- Tenant 전체를 아우르는 운영 query는 별도 승인된 source/view로 모델링해야 하며 RLS
  bypass role을 gateway reader로 사용하지 않는다.
