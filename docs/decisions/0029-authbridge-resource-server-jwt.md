# ADR 0029: AuthBridge Resource Server JWT Access Token

Status: Accepted — opt-in repository capability; protected cutover separately gated

Date: 2026-08-29

Decision ID: `QB-AUTHBRIDGE-RS-20260829`

Baseline: `fc6eab3`

## Context

Query Man의 기존 bearer access policy는 환경변수의 opaque token을 SHA-256 digest로 비교한다. 이 방식은
local Compose와 CI에 적합하지만 AuthBridge가 발급한 OAuth 2.0 JWT access token의 서명, issuer,
audience, expiry와 endpoint scope를 검증하지 않는다. Bearer header 형식만 같다고 두 인증 의미를 같은
것으로 취급하면 ID token, 다른 resource용 token 또는 단순 decode 결과를 신뢰할 위험이 있다.

AuthBridge의 company resource-server 경계는 다음 issuer와 Discovery를 사용한다.

```text
issuer: https://smart-dna.sec.samsung.net/ws2/30001/realms/authbridge
discovery: {issuer}/.well-known/openid-configuration
```

Resource server는 access token을 로컬에서 검증한다. Refresh token 저장과 token endpoint refresh는
Codex MCP client 또는 공용 company helper가 소유하며 Query Man server의 책임이 아니다.

## Decision

### Authentication mode

- Runtime은 기존 anonymous loopback, opaque API token, version 2 access policy와 별도로 OAuth resource
  server mode를 opt-in으로 제공한다.
- OAuth issuer, audience, query/MCP/operator scope 설정은 함께 있어야 한다. OAuth mode는 opaque API
  token 또는 access-policy file과 상호 배타적이다.
- Base Compose와 current CI는 기존 opaque query/operator token을 유지한다. 이 결정은 protected route나
  AuthBridge audience/scope provisioning을 실행하지 않는다.
- Managed mode도 OAuth를 authentication authority로 선택할 수 있다. Query scope와 별도 operator scope가
  모두 non-empty여야 하며 operator endpoint는 query permission도 먼저 요구한다.

### JWT validation

- `Authorization: Bearer <access_token>` 하나만 받으며 OAuth mode에서 누락, 중복, malformed header는
  invalid token이다.
- 서명 알고리즘은 `RS256`으로 고정한다. JWT header의 `alg`, bounded `kid`와 access-token-compatible
  `typ`을 확인하고 `none`, HMAC, 다른 RSA/EC algorithm과 refresh token type을 거부한다.
- Discovery document의 issuer는 configured issuer와 정확히 같아야 한다. `jwks_uri`는 credential,
  query와 fragment가 없는 absolute HTTPS URL이어야 한다.
- Discovery와 JWKS는 process memory에 5분간 cache한다. 알 수 없는 `kid`는 request당 최대 한 번,
  process 전체 30초 cooldown 안에서 한 번만 JWKS를 갱신한다.
- JWK는 exact `kid`, RSA key type, `RS256`, signature use와 optional verify key operation을 확인한다.
- Signature와 `iss`, `aud`, `exp`, optional `nbf`, non-empty `sub`를 검증한다. Clock skew allowance는
  60초다. RFC `at+jwt` header type은 대소문자 차이를 허용하고, Keycloak `typ=JWT` token은 payload의
  exact `typ=Bearer`를 추가로 요구한다. ID/refresh token type은 명시적으로 거부한다.
- Discovery/JWKS response는 최대 1 MiB이고 system TLS trust를 사용하며 HTTP로 내려가는 redirect를
  거부한다. TLS 검증을 끄거나 token introspection/refresh endpoint를 호출하지 않는다.

### Authorization and caller identity

- HTTP data route는 configured query scope/optional realm role/group을 모두 요구한다.
- `/mcp`는 query requirement에 configured MCP scope를 추가한다.
- Operator capability는 configured operator scope/optional realm role/group을 모두 가진 caller에게만
  부여한다. Admin/cancel route의 기존 `operator` check는 유지한다.
- 모든 인증 caller의 source visibility와 source-resolved budget은 계속 shared다. OAuth scope는 source
  grant나 caller-selected budget이 아니다.
- Raw `sub`와 token claim은 log나 response에 넣지 않는다. `caller_id`는 issuer와 subject의 SHA-256
  pseudonym이고 `tenant_id`는 현재 non-RLS shared-access authority를 나타내는 fixed `authbridge`다.
- OAuth token에는 Query Man의 server-side diagnostic consent receipt가 없다. OAuth mode와 diagnostic
  capture 설정은 startup에서 함께 사용할 수 없게 fail-closed한다.

### External error contract

토큰 누락, 위조, 만료, issuer/audience/type 불일치는 다음 결과다.

```text
401 Unauthorized
WWW-Authenticate: Bearer error="invalid_token"
```

JWT는 유효하지만 query/MCP scope·role·group이 부족하거나 operator capability가 없으면 다음 결과다.

```text
403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope"
```

기존 bounded JSON error envelope는 유지한다. Token, Authorization header, claim 원문, key document와
validator 내부 오류는 response 또는 log에 넣지 않는다.

## Module and compatibility impact

- Delivery는 async `BearerAuthenticator.authenticate(token, *, mcp)` interface와 opaque access-policy
  adapter, OAuth JWT implementation을 소유한다.
- Runtime은 세 authentication authority를 상호 배타적으로 검증하고 선택한다. Production composition
  외 module은 concrete JWT verifier를 조립하지 않는다.
- `CallerContext` shape, `GatewayService`, HTTP path/body/success response, MCP tool/schema/protocol과
  source/SQL safety policy는 바뀌지 않는다.
- `PyJWT[crypto]`는 direct runtime dependency다. Cryptographic operation은 library가 수행하며
  application은 algorithm, key와 claim policy를 고정한다.
- Access-policy v2와 Control DB/schema에는 migration이 없다.

## Rollout and rollback

Repository acceptance는 실제 AuthBridge cutover 승인이 아니다. Protected rollout 전에는 service-specific
audience와 scopes, optional role/group mapper, TLS CA, public Host/Origin, client token acquisition와
change-record owner를 확인한다. ID/refresh/다른 audience token 거부와 key rotation을 traffic 밖에서
검증한 뒤 route한다.

Rollback은 traffic을 이전 artifact로 되돌리고 OAuth 설정을 제거한 뒤 승인된 opaque access policy를
다시 선택한다. OAuth와 opaque 설정을 같은 process에 넣어 fallback하지 않는다. Persisted data나 token
store가 없으므로 database rollback은 없다.

## Verification

- Signed JWT success and cache reuse
- Wrong algorithm/signature/issuer/audience, expired/future token and wrong token type
- Query/MCP/operator scope, optional realm role/group and pseudonymous caller
- Unknown `kid` single refresh with cooldown, Discovery issuer and HTTPS JWKS boundary
- 401/403 exact Bearer challenge and token non-disclosure
- Runtime partial/ambiguous config, managed selection and diagnostic-capture incompatibility
- Existing opaque HTTP/MCP, managed authorization, Ruff, mypy and full pytest regression

## References

- RFC 6750, OAuth 2.0 Bearer Token Usage
- RFC 8725, JSON Web Token Best Current Practices
- RFC 9068, JWT Profile for OAuth 2.0 Access Tokens
