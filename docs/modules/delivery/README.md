# Delivery Module

Status: Physical package boundary active

## 목적

### 30초 요약

Delivery는 caller를 인증·인가하고 Source Catalog, Metadata와 Guarded Query application service를 동일한
HTTP/MCP 계약으로 제공한다. OAuth2 bearer access token은 Discovery의 `jwks_uri`에서 받은 key로 로컬
검증하며 issuer, audience, expiry와 endpoint scope/role을 함께 확인한다.

Source 관리 API는 없다. Source inventory 변경은 source-package pull request로 수행하고 HTTP/MCP는 현재
Runtime에 load된 read-only source projection만 제공한다.

## 소유 책임

- Caller/access policy, OAuth2 JWT bearer 인증과 source authorization
- HTTP/MCP request validation, application dispatch와 public response/error envelope
- Source list, metadata context, query와 query cancel surface
- MCP protocol/transport, Host/Origin/body/argument bounds와 disconnect propagation
- Consent-gated diagnostic capture port와 transport decision
- `/health`, `/ready`, `/admin/health`, `/admin/metrics` wire

## 소유하지 않는 책임

- Source package/manifest schema validation과 registry mutation
- Metadata catalog/revision implementation과 SQL validation/query executor
- Runtime composition/readiness policy와 capture storage
- Source admin/history/receipt/replica/usage API나 Control DB state transition

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`delivery/access.py`](../../../src/query_man/delivery/access.py) | Caller context, access policy, pseudonymous audit identity |
| [`delivery/authentication.py`](../../../src/query_man/delivery/authentication.py) | Access-policy bearer와 OAuth2 JWT resource-server verification |
| [`delivery/gateway.py`](../../../src/query_man/delivery/gateway.py) | Transport-neutral application facade |
| [`delivery/app.py`](../../../src/query_man/delivery/app.py) | HTTP request/response, middleware, routes와 parent lifespan |
| [`delivery/mcp_server.py`](../../../src/query_man/delivery/mcp_server.py) | MCP tools/resources와 bounded protocol handling |
| [`delivery/diagnostics.py`](../../../src/query_man/delivery/diagnostics.py) | Diagnostic capture Protocol |
| [`delivery/http_validation.py`](../../../src/query_man/delivery/http_validation.py) | Shared JSON Content-Type validation |
| [`test_http.py`](../../../tests/test_http.py), [`test_mcp.py`](../../../tests/test_mcp.py), [`test_oauth_authentication.py`](../../../tests/test_oauth_authentication.py) | Focused wire/auth tests |

## 제공 인터페이스와 소유 경계

`GatewayService`가 Runtime이 조립하는 transport-neutral application facade다. `build_http_app`, access
policy/authenticator configuration과 diagnostic capture port는 Runtime composition이 사용하는 주요
entrypoint다. 반환 FastAPI의 `state.mcp_app`은 Runtime이 parent/child lifespan을 연결하는 내부
composition handle이며, 다른 state 배치는 외부 module interface가 아니다. Delivery는 provider
interface인 `SourceReader`, Metadata application service, Guarded Query application/lifecycle와 Runtime
operations sink만 소비한다.

Opaque access-policy schema는 version 2이고 entry field는 `caller_id`, `tenant_id`, `token_env`,
`operator`와 optional `diagnostic_consent`만 허용합니다. Consent object는 `version`, `receipt_id`,
`expires_at`만 가지며 exact admission semantics는 ADR 0027을 따릅니다. Legacy version/scope나 다른
unknown field는 startup에서 거부하고 token 값은 environment에서만 읽어 SHA-256 digest로 비교합니다.
Loopback anonymous와 single-token mode는 query-only, `operator=true`는 query 권한을 포함하는
superset입니다. Authorized caller의 unknown source는 `SOURCE_NOT_FOUND`로 끝나며
metadata·SQL validation·queue 작업을 시작하지 않습니다.

현재 data surface는 source list, source metadata context, guarded query와 cancel이다. HTTP와 MCP의 field,
status/code/message, validation issue, authentication challenge와 protocol version은 external wire format이다.
Source mutation/history/receipt/replica/usage route와 MCP admin tool은 제공하지 않는다.

MCP protocol/version/tool schema와 bounded validation의 exact contract는
[ADR 0006](../../decisions/0006-mcp-transport-and-workflow.md), diagnostic consent와 transport admission의
exact contract는 [ADR 0027](../../decisions/0027-consent-gated-diagnostic-capture.md)에 있습니다.

AuthBridge OAuth resource-server mode는 `Authorization: Bearer <access_token>`만 받는다. Discovery 문서의
`jwks_uri`를 HTTPS로 읽어 JWKS를 cache하고 unknown `kid`에서 제한적으로 한 번 갱신한다. 허용
algorithm을 고정하고 `iss`, service-specific `aud`, `exp`, `nbf`, required scope와 role/group을 검증한다.
ID token과 refresh token은 API 인증에 사용하지 않으며 service가 refresh하거나 client secret을 요구하지
않는다. Token과 Authorization header는 log에 남기지 않는다.

Token 없음/위조/만료/issuer·audience mismatch는 `401`과 `WWW-Authenticate: Bearer
error="invalid_token"`, 유효하지만 scope/role 부족은 `403`과 `error="insufficient_scope"`로 응답한다.

Diagnostic capture는 active server-side consent receipt가 있을 때만 질문 원문과 literal-free SQL을
분리 저장소로 전달한다. Unknown source, auth failure, list/cancel/health 요청은 capture하지 않는다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | `SourceReader`와 `SourceNotFoundError` | Sanitized list/get만 사용; source package file/secret에 직접 접근하지 않음 |
| Metadata | Context/revision application behavior | Catalog private state를 읽지 않음 |
| Guarded Query | Query/cancel/stop-accepting public behavior | SQL safety/error 이유를 transport에서 재정의하지 않음 |
| Runtime | Operations/readiness와 capture adapter | Health projection은 Runtime state를 그대로 bounded rendering |

## 불변조건

- Caller 인증 뒤 source별 authorization을 metadata/query 전에 확인한다.
- Bearer JWT는 decode만 신뢰하지 않고 signature/issuer/audience/time/scope를 검증한다.
- Token, credential, SQL literal, internal database/parser error와 traceback을 공개하거나 log하지 않는다.
- HTTP/MCP input size, type, Host/Origin와 validation detail 수를 제한한다.
- Disconnect/cancel은 Guarded Query cleanup 경계까지 전파한다.
- HTTP와 MCP는 같은 application authorization/revision/query policy를 우회하지 않는다.
- Source 관리 route와 Control Plane capability는 없다.

## 모듈 내부 변경

Wire와 provider interface 의미를 보존하는 private serialization helper, handler layout, middleware 내부
구현과 focused test 정리는 module 내부 변경이다.

## 사용자 승인이 필요한 경계 변경

- HTTP/MCP path/tool/resource, request/response/error/status/header와 protocol version
- OAuth issuer/audience/algorithm/JWKS cache/scope/role와 401/403 contract
- Caller/source authorization, diagnostic consent와 audit identity policy
- Authorization 순서와 parent/child lifespan cleanup outcome
- Source management API 재도입

## 검증

```bash
uv run pytest tests/test_access.py tests/test_oauth_authentication.py \
  tests/test_http.py tests/test_mcp.py tests/test_mcp_server.py
```

Socket/disconnect/concurrency를 바꾸면 integration/load/soak test도 실행한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| OAuth/access | `access.py`, `authentication.py`, resource-server auth doc, focused tests |
| HTTP | `app.py`, `http_validation.py`, `gateway.py`, `test_http.py` |
| MCP | `mcp_server.py`, `gateway.py`, MCP/load tests |
| Query cancel/disconnect | Guarded Query lifecycle, app/MCP handler, integration test |
| Diagnostic capture | `diagnostics.py`, Runtime capture adapter, access/capture tests |
