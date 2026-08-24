# Delivery Module

Status: Logical boundary; physical package split pending

## 목적

Delivery는 외부 caller를 안전한 application 호출로 바꾸고 결과를 HTTP와 MCP로 전달한다. 쉽게
말하면 인증·인가를 거친 하나의 현관이며, Metadata와 Guarded Query의 업무 규칙을 transport마다
복제하지 않는다.

HTTP와 MCP parity는 모든 endpoint가 같다는 뜻이 아니다. 공통 data operation의 caller 권한,
성공 결과, 오류 의미와 cancellation이 같다는 뜻이다.

## 소유 책임

- Bearer policy 검증과 server-derived `CallerContext`
- Shared active-source visibility와 별도 operator capability
- `GatewayService` application facade와 authorization-before-work 순서
- HTTP request model, route, middleware, response status와 error handler
- MCP protocol/version, tool inventory, strict argument schema와 result envelope
- HTTP/MCP 공통 data operation의 성공/error 의미 parity
- HTTP Pydantic field/shape 제한과 MCP body/header/Host/Origin transport 제한
- Admin route의 operator-first parsing, bounded JSON/header/query/path validation과 idempotency headers
- Client disconnect를 query task cancellation으로 전달하는 transport lifecycle
- Request/caller/source content 중 기록하면 안 되는 정보와 transport audit event 의미

## 소유하지 않는 책임

- Source definition, registry mutation과 Control DB state transition
- Physical catalog, metadata revision/context selection algorithm
- SQL AST policy, query admission, transaction과 result encoding
- Concrete adapter 조립, process startup/shutdown와 health 상태 계산
- PostgreSQL schema, reader role와 credential 저장
- MCP를 이용한 source administration 또는 operator cancel tool

## 현재 코드 위치

- [`access.py`](../../../src/query_man/access.py): `CallerContext`와 `AccessPolicy`
- [`gateway.py`](../../../src/query_man/gateway.py): transport-independent `GatewayService`
- [`mcp_server.py`](../../../src/query_man/mcp_server.py): MCP server, tools, schemas, errors와 disconnect
- [`http_validation.py`](../../../src/query_man/http_validation.py): HTTP/MCP/admin이 공유하는 bounded
  JSON Content-Type validator
- [`source_admin_routes.py`](../../../src/query_man/source_admin_routes.py): admin catalog/mutation routes,
  operator-first parsing, request limits와 Control Plane public input/use-case 호출. Admin sequence
  validation은 `CONTROL_SEQUENCE_MAX`, verified publish mapping은 `PublishVerifiedQueryInput`과
  `VerifiedExpectedInput`을 소비한다. Admin path/query wire validation은 Source Catalog의
  `SourceEnvironment`, `Identifier`, `StableSlug` type을 소비한다.
- [`app.py`](../../../src/query_man/app.py): HTTP request DTO, parent auth middleware, routes, handlers와
  disconnect; composition/lifespan 부분은 Runtime 소유
- [`errors.py`](../../../src/query_man/errors.py): public `AppError` envelope/rendering contract;
  각 domain error의 업무 의미는 해당 producer module과 공동 소유
- `config/access-policies*.yaml`: bootstrap/Compose caller identity와 capability input
- [`skills/query-man-text-to-sql`](../../../skills/query-man-text-to-sql): MCP/context/query
  workflow를 설명하는 consumer Skill; 안전 enforcement boundary가 아님
- Focused tests: [`test_access.py`](../../../tests/test_access.py),
  [`test_http.py`](../../../tests/test_http.py),
  [`test_mcp.py`](../../../tests/test_mcp.py),
  [`test_mcp_server.py`](../../../tests/test_mcp_server.py)

현재 `GetContextSuccessOutput`은 `mcp_server.py`에 있지만 HTTP `/meta`도 이를 사용한다. 위치와
달리 의미상 Delivery 공통 wire contract다. `app.py`와 `errors.py`를 수정할 때는
[Runtime](../runtime/README.md) 및 오류를 생산하는 domain module 계약도 확인한다.

## 제공 계약

### Common application contract

HTTP와 MCP의 세 data operation은 동일한 `GatewayService`와 `CallerContext`를 사용한다.

| 기능 | HTTP | MCP |
|---|---|---|
| Source 목록 | `GET /sources` | `list_sources()` |
| 질문별 metadata | `POST /meta` | `get_context(...)` |
| Guarded query | `POST /query` | `query(...)` |

양쪽은 동일한 shared active source inventory, metadata와 SQL policy revision, Metadata/Query 성공
payload, `AppError` code/message/safe detail 및 disconnect-to-cancel 의미를 유지한다.

Transport별 HTTP status 대 MCP `isError`, validation issue 형식, health/admin/cancel endpoint와 MCP
discovery/serialization은 의도적으로 다를 수 있다.

### Caller and authorization contract

- `/health`와 `/ready`를 제외한 HTTP/MCP request는 parent bearer policy를 통과한다.
- Authorization header는 정확히 하나이고 token 원문은 저장하지 않는다.
- Caller ID, tenant ID와 operator 여부는 server가 결정하며 client field로 받지 않는다.
- Active source existence는 metadata load, SQL validation과 admission보다 먼저 확인한다.
- 모든 인증 query identity와 operator는 같은 active source 목록을 본다. Caller별
  `allowed_sources|all_sources` contract는 없고 operator 여부도 source visibility를 바꾸지 않는다.
- Operator만 administration endpoint와 query cancel을 사용할 수 있다. `operator`는 shared query
  capability에 admin/cancel을 더하는 boolean superset이다.
- Bootstrap loopback compatibility의 anonymous `local-development`와 legacy single API token의
  `legacy-api-token` caller는 query-only다. Bootstrap에서 admin이 필요하면 version 2 access policy의
  explicit operator를 사용한다.
- Managed mode는 version 2 policy file, authenticated non-admin query identity와 explicit operator
  identity를 모두 요구한다. Anonymous와 single API token, version 1/scope field는 startup에서
  fail-closed한다.

### Public error contract

HTTP 오류는 `{error: {code, message, details?}}` envelope을 사용하고 MCP는 같은 업무 의미를
structured tool result로 표현한다. 예상하지 못한 오류는 고정된 internal error로 축약하며 5xx
detail, raw PostgreSQL error, SQL literal, credential과 token을 반사하지 않는다.

MCP argument validation은 `INVALID_REQUEST`와 다음 exact bounded detail을 structured result로
반환한다.

```text
{
  action: CALL_GET_CONTEXT | CORRECT_ARGUMENTS,
  retryable: true,
  issues: [{path, reason_code, message}],
  truncated: boolean
}
```

Revision 누락·형식 오류는 `CALL_GET_CONTEXT`, 나머지는 `CORRECT_ARGUMENTS`다.
Issue는 최대 8개이고 `path`는 공개 tool argument 이름 또는 `arguments`만 사용한다.
Reason은 `ARGUMENT_REQUIRED`, `ARGUMENT_FORMAT_INVALID`, `ARGUMENT_LENGTH_INVALID`,
`ARGUMENT_TYPE_INVALID`, `ARGUMENT_OUT_OF_RANGE`, `ARGUMENT_NOT_ALLOWED`, `ARGUMENT_INVALID`의
고정 집합이다. Input value, Pydantic 세부 정보와 알 수 없는 private field 이름을
반사하지 않고 output validation failure는 generic `INTERNAL_ERROR`로 숨긴다.

HTTP `RequestValidationError`는 최대 32개의 bounded path/code와 고정 message만 반환한다.
Public data routes에는 별도 전역 body-size limit이 없지만 admin mutation은 exact JSON
Content-Type, 1 MiB body, 최대 1,024 object member, duplicate key/non-finite number 거부와
bounded header/query/path parsing을 적용한다. MCP와 admin의 더 강한 제한을 다른 HTTP route
보장으로 확대 해석하지 않는다.

### MCP contract

- Protocol version은 현재 `2026-07-28`이다.
- Stateless Streamable HTTP JSON mode를 `/mcp`에서 제공한다.
- 공개 tool은 `list_sources`, `get_context`, `query` 세 개뿐이다.
- Extra argument와 암묵적 type coercion을 거부한다.
- `query`는 `metadata_revision`과 `sql_policy_revision`을 모두 필수로 받고 tool
  description은 같은 `get_context`가 반환한 exact 두 값을 전달하도록 요구한다.
  `METADATA_REVISION_MISMATCH`에서는 context를 다시 받고 SQL을 재생성해 한 번만
  재시도하는 workflow를 안내한다.
- MCP POST에 1 MiB body limit, single JSON Content-Type/protocol-version header, Host/Origin policy와
  DNS rebinding protection을 적용한다.

### Child lifespan ownership contract

- Delivery가 제공하는 MCP child lifespan은 자신의 `enter` 도중 만든 partial resource를 정리할
  책임을 가진다. Runtime parent의 cleanup에 그 책임을 넘기지 않는다.
- Child `enter`가 실패하면 Runtime parent는 진입하지 못한 child의 `exit`를 호출하지 않는다.
- Runtime은 child 진입을 시도하기 전에 parent composition이 만든 최상위 resource만 자신의
  startup-failure 계약에 따라 정리한다. 이 경계는 HTTP/MCP wire와 정상 shutdown 순서를 바꾸지
  않는다.

## 소비 계약

- [Source Catalog](../source-catalog/README.md)의 `SourceReader` sanitized source summaries와 admin wire
  validation에 사용하는 `SourceEnvironment`, `Identifier`, `StableSlug`
- [Metadata](../metadata/README.md)의 context application result
- [Guarded Query](../guarded-query/README.md)의 query/cancel result와 safe domain errors
- [Control Plane](../control-plane/README.md)의 management projection, mutation receipt/use cases와
  conflict/error meaning 및 public sequence/verified-publish input
- [Runtime](../runtime/README.md)의 aggregate health/operations state와 lifecycle context

Delivery는 domain module의 concrete PostgreSQL adapter나 Control DB table을 직접 호출하지 않는다.
`GatewayService`는 concrete `SourceRegistry`가 아니라 `SourceReader`를 받아 `list/get`만 사용한다.
Admin route는 Control Plane의 `source_store.py`와 Assurance의 `verified.py`를 import하지 않고 Control
Plane이 공개한 administration input만 만든다.
위 Source Catalog validation type의 pattern/range를 바꾸면 admin path/query wire acceptance도 바뀌므로
두 module의 계약과 기존 client compatibility를 함께 확인한다.

## 불변조건

- HTTP와 MCP는 공통 operation을 하나의 Gateway/application service path로 호출한다.
- Authentication/authorization 뒤에만 source-specific work를 수행한다.
- Caller가 DSN, credential, database role 또는 tenant context를 선택할 수 없다.
- Unknown source, SQL/question/token/credential과 내부 오류를 log/metric label로 새지 않는다.
- Client disconnect는 실행 task cancellation을 통해 database cancel/rollback으로 전파된다.
- MCP SDK workaround가 protocol contract나 domain policy를 대신하지 않는다.
- Query-facing `GET /sources`/`list_sources()` projection에는 connection endpoint와 internal
  control state가 포함되지 않는다. Operator-only admin detail의 제한된 connection projection은
  Control Plane management contract를 따른다.
- Admin sequence/verified payload는 Control persistence나 Assurance DTO가 아니라 Control Plane
  public administration input을 통해 전달한다.

## 모듈 내부 변경

다음은 wire schema와 application 의미를 보존할 때 독립적으로 변경할 수 있다.

- Middleware/route helper와 request parsing 내부 정리
- 같은 caller를 만드는 token lookup/data structure 개선
- 같은 payload/status를 만드는 HTTP/MCP serialization 정리
- Protocol 동작을 보존하는 MCP SDK adapter/workaround 정리
- 기존 공개 field만 사용하는 audit logging 구현 개선

## 사용자 승인이 필요한 계약 변경

- HTTP path/method, request/response field, status 또는 size/range/default 변경
- MCP tool 이름/개수/schema/description, protocol version 또는 transport mode 변경
- `GatewayService` method, authorization 순서와 HTTP/MCP parity 범위 변경
- `CallerContext`, shared source visibility, tenant trust와 operator capability 의미 변경
- Public error code/message/detail/envelope 또는 unknown-vs-unauthorized 처리 변경
- Bearer/header, Host/Origin, body size와 unauthenticated endpoint 정책 변경
- Source summary projection과 admin/cancel surface 변경
- Admin idempotency/expected-state header, query/path/body limit, validation issue 또는 receipt/catalog
  projection 변경
- Transport audit에 기록 가능한 request/caller/source field 또는 disconnect/cancel 의미 변경

Delivery는 “무엇을 기록하면 안 되는지”와 audit event 의미를 소유한다. Runtime은
`SafeJsonFormatter`, structured field allowlist와 방어적 문자열 redaction 구현을 소유하고 각 domain
module은 자신이 생성하는 audit field의 비노출을 책임진다.

승인 요청에는 Source Catalog, Metadata, Guarded Query, Control Plane과 Runtime consumer 영향 및
기존 HTTP/MCP client compatibility를 포함한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_access.py tests/test_http.py tests/test_mcp.py \
  tests/test_runtime_startup_cleanup.py
```

실제 MCP server tests는 기본 pytest marker에서 제외되므로 다음을 별도로 실행한다.

```text
uv run pytest -m 'mcp_server and not soak' -s tests/test_mcp_server.py
```

Protocol/socket 경계를 바꾸면 integration disconnect test를, admin mutation parsing/receipt를 바꾸면
source-admin/control-store tests를, concurrency/session 경계를 바꾸면 MCP load/soak test를 추가한다.
Control public administration input 소비를 바꾸면 `tests/test_documentation.py`의 hidden-import guard와
`tests/test_source_admin.py`의 provider mapping test도 실행한다.
완료 전 root `AGENTS.md`의 전체 gate를 실행한다.

## 집중해서 읽을 범위

Delivery 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 access/gateway/HTTP/MCP/admin-route/error code와 focused tests
3. 호출하는 domain operation의 input/output/error 계약
4. [ADR 0002](../../decisions/0002-guarded-query-contract.md),
   [ADR 0004](../../decisions/0004-caller-source-authorization.md),
   [ADR 0006](../../decisions/0006-mcp-transport-and-workflow.md),
   [ADR 0015](../../decisions/0015-containerized-local-runtime.md)와
   [ADR 0017](../../decisions/0017-shared-source-access-and-resource-tier.md) 중 변경과 직접 관련된 결정
5. `app.py` lifecycle을 건드릴 때 Runtime 계약

Catalog SQL, source persistence transaction과 query executor 내부는 계약을 바꾸지 않는 한 읽을
필요가 없다.
