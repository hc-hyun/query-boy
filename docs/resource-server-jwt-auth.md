# Resource Server JWT Access Token 검증 계약

Status: Opt-in repository capability; protected deployment 별도 승인 필요

Query Man을 AuthBridge에 연결할 때 인증 방식은 **OAuth 2.0 Bearer Access Token 검증**이다.

> 모든 API 요청은 `Authorization: Bearer <access_token>` 형식으로 받습니다.
> 서비스는 AuthBridge가 발급한 JWT access token을 로컬 검증하고, refresh token을 저장하거나
> 인증서버에 refresh 요청하지 않습니다.

`/health`와 `/ready`는 현재 process 상태 확인용 public endpoint라 이 인증 경계의 예외다. Data,
MCP, admin과 cancel route는 bearer authentication을 통과해야 한다.

## AuthBridge 기준값

```text
Issuer:
https://smart-dna.sec.samsung.net/ws2/30001/realms/authbridge

Discovery:
{issuer}/.well-known/openid-configuration

Audience:
서비스별 발급값 (Query Man 예시: query-man)
```

Query Man의 예시 endpoint permission은 다음과 같다. 실제 값은 AuthBridge의 service-specific audience,
scope와 정확히 맞춰 발급·설정해야 한다.

| Route | Required scope |
|---|---|
| `/sources`, `/meta`, `/query` | `query-man.read` |
| `/mcp` | `query-man.read`, `mcp.tools` |
| `/admin/*`, `/queries/{query_id}` | `query-man.read`, `query-man.admin` |

Realm role/group restriction이 필요한 환경은 같은 capability별 optional 설정을 추가한다. 모든 인증
query caller는 같은 active source를 보며 scope나 role이 source grant 또는 budget 선택자가 되지는 않는다.

## 서비스 필수 검증

- JWT 서명 검증: exact issuer Discovery의 `jwks_uri` 사용; Discovery/JWKS response는 각각 최대 1 MiB
- 허용 서명 알고리즘 `RS256` 고정
- Discovery의 issuer와 token의 `iss`가 configured issuer와 정확히 일치
- Discovery/JWKS URL과 redirect가 HTTPS를 유지하고 TLS hostname 검증을 통과
- `aud`에 자기 서비스 audience 포함
- Exact `kid`의 RSA JWK만 사용하고 JWK `alg=RS256`, signature `use`, optional `key_ops`의 `verify`를 확인
- `exp`, optional `nbf`와 non-empty `sub`를 60초 clock-skew allowance로 확인
- RFC `at+jwt` header type은 대소문자 차이를 허용하고, Keycloak `typ=JWT` token은 payload
  exact `typ=Bearer`도 요구
- Endpoint별 scope와 configured realm role/group 확인
- ID token과 refresh token을 API 인증용으로 받지 않음
- Discovery/JWKS 5분 cache, 알 수 없는 `kid`는 request당 한 번이자 process-wide 30초 cooldown 안에서
  한 번만 갱신
- Token, Authorization header와 raw claim을 response 또는 log에 기록하지 않음

Query Man은 JWT를 단순 decode한 결과를 사용하지 않는다. `PyJWT[crypto]`로 cryptographic operation을
검증하고 application이 algorithm, issuer, audience, access-token type과 claim policy를 제한한다.

Raw `sub`와 claim은 log/response에 넣지 않습니다. Audit용 `caller_id`는 exact issuer와 subject를
결합한 SHA-256 pseudonym이고 `tenant_id`는 현재 non-RLS shared-access를 나타내는 fixed
`authbridge`입니다. 이 값은 source grant, quota 또는 budget selector가 아닙니다.

## Runtime 설정

OAuth mode를 선택할 때 다음 다섯 값은 모두 필요하다.

```dotenv
QUERY_MAN_OAUTH_ISSUER=https://smart-dna.sec.samsung.net/ws2/30001/realms/authbridge
QUERY_MAN_OAUTH_AUDIENCE=query-man
QUERY_MAN_OAUTH_QUERY_SCOPES=query-man.read
QUERY_MAN_OAUTH_MCP_SCOPES=mcp.tools
QUERY_MAN_OAUTH_OPERATOR_SCOPES=query-man.admin
```

Optional realm role/group은 comma-separated exact 값이다.

```dotenv
# QUERY_MAN_OAUTH_QUERY_ROLES=query-analyst
# QUERY_MAN_OAUTH_QUERY_GROUPS=/query-users
# QUERY_MAN_OAUTH_OPERATOR_ROLES=query-operator
# QUERY_MAN_OAUTH_OPERATOR_GROUPS=/query-admins
```

OAuth 설정은 `QUERY_MAN_API_TOKEN`, `QUERY_MAN_ACCESS_POLICY_FILE`과 함께 사용할 수 없다. OAuth token에는
Query Man의 server-side diagnostic consent receipt가 없으므로 diagnostic capture 설정과도 함께 시작하지
않는다. Base Compose는 local/CI 반복 검증을 위해 기존 opaque query/operator token을 유지한다.

## 오류 응답

Token 누락, 위조, 만료, 잘못된 type, issuer 또는 audience 불일치:

```text
401 Unauthorized
WWW-Authenticate: Bearer error="invalid_token"
```

Token은 유효하지만 scope, role 또는 group이 부족함:

```text
403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope"
```

Response body는 Query Man의 bounded JSON error envelope를 유지하며 validator 내부 원인, claim 또는 token을
반사하지 않는다.

## 서비스에서 하지 않는 일

- JWT payload를 서명 검증 없이 decode해 신뢰
- Refresh token 저장
- Access token 만료 시 AuthBridge/Keycloak token endpoint에 refresh 요청
- 다른 서비스 audience로 받은 token을 Query Man에 전달하거나 Query Man token을 downstream에 재사용
- 여러 서비스에 같은 audience 배정
- TLS hostname 검증 비활성화

Token 취득과 refresh는 Codex MCP client 또는 공용 company helper가 담당한다. Query Man에는 client
secret이 필요하지 않으며 issuer, audience, endpoint scopes와 필요한 경우 role/group만 설정한다.

## AUTHENV-01 Traffic-off 확인

이 절은 구현 작업이나 production route가 아니라 실제 AuthBridge 환경 binding과 acceptance다.
검증한 exact Git commit과 non-secret configuration을 evidence에 기록한다. Image를 사용했다면 그
commit과 OCI revision label이 일치하는 image digest도 기록한다. Route는 `DBENV-01`도 완료된 뒤 별도
승인된 `LAUNCH-02`에서 수행한다.

1. AuthBridge에 Query Man 전용 audience와 scope mapper가 발급됐는지 확인한다.
2. System trust 또는 승인된 CA bundle로 issuer와 `jwks_uri` HTTPS를 검증한다.
3. Access token success와 ID token, refresh token, 다른 audience, expired token rejection을 traffic 밖에서
   확인한다.
4. Signing key rotation에서 기존 `kid` cache와 새 `kid` 단일 refresh가 동작하는지 확인한다.
5. 401/403 challenge, token 비로깅과 Codex/company helper refresh를 확인하고 `AUTHENV-01` evidence를
   남긴다. 이 단계에서는 route하지 않는다.

실제 protected route/cutover는 `DBENV-01`과 `AUTHENV-01` 완료 뒤 target, artifact, TLS/secret,
stop condition, rollback과 change-record owner가 있는 `LAUNCH-02` 실행 승인을 따른다.

## 표준 참고

- [RFC 6750: OAuth 2.0 Bearer Token Usage](https://www.rfc-editor.org/rfc/rfc6750.html)
- [RFC 8725: JSON Web Token Best Current Practices](https://www.rfc-editor.org/rfc/rfc8725.html)
- [RFC 9068: JWT Profile for OAuth 2.0 Access Tokens](https://www.rfc-editor.org/rfc/rfc9068.html)
