# ADR 0004: Caller And Source Authorization

Status: Accepted

Date: 2026-08-22

## Context

하나의 bearer token이 모든 source를 볼 수 있으면 caller와 tenant를 구분할 수 없고,
MCP와 HTTP가 서로 다른 authorization을 적용할 위험이 있다. 반대로 client가 tenant나
허용 source를 요청 값으로 지정하게 하면 trust boundary가 무너진다.

## Decision

- 인증 결과는 server-side
  `CallerContext(caller_id, tenant_id, allowed_sources, operator, all_sources)`다.
- Production에서는 versioned access-policy manifest가 caller ID, tenant ID, token 환경
  변수 이름과 source 범위를 연결한다. Source 범위는 명시적인 `allowed_sources` 또는 현재와
  미래의 control-plane source를 모두 허용하는 `all_sources: true` 중 정확히 하나여야 한다.
  Token 값은 manifest, response, log에 저장하지 않고 시작 시 환경 변수에서 읽어 SHA-256
  digest만 보관한다.
- `/sources`, `/meta`, `/query`는 `GatewayService`를 유일한 application boundary로
  사용한다. HTTP와 MCP adapter는 인증된 context만 이 service에 전달한다.
- 허용되지 않은 source와 존재하지 않는 source는 동일한 `404 SOURCE_NOT_FOUND`를
  반환한다. Source authorization은 metadata load, SQL validation, concurrency slot보다
  먼저 수행한다.
- Tenant ID는 SQL text, `search_path` 또는 client-controlled session setting에 넣지
  않는다. RLS source는 ADR 0014의 server-derived transaction-local trusted session context를
  사용한다.
- Loopback에서 인증 설정이 없을 때만 모든 등록 source를 볼 수 있는
  `local-development` caller를 암시적으로 사용한다.
- 기존 단일 `QUERY_MAN_API_TOKEN`은 migration 호환성을 위해 모든 등록 source를 가진
  하나의 caller로 유지한다. 다중 caller는 `QUERY_MAN_ACCESS_POLICY_FILE`을 사용하며 두
  설정을 동시에 사용할 수 없다.
- `all_sources: true`는 신규 control-plane source에도 즉시 적용되는 넓은 권한이므로 미래
  source까지 신뢰할 수 있는 caller에만 명시적으로 부여한다. `operator: true` 자체는 일반
  source 접근 범위를 넓히지 않는다.

## Consequences

- Access policy의 source 오타, 중복 caller/token reference, source 범위의 누락·중복,
  누락되거나 32자 미만인 secret은 startup을 fail-closed시킨다.
- `operator` flag는 query cancel/운영 endpoint 권한에만 사용하고 일반 source 접근을
  넓히지 않는다.
- `allowed_sources`로 제한된 caller에게 개별 source를 hot-grant하는 기능은 제공하지 않는다.
  해당 목록은 startup에 로드되므로 변경 시 service restart가 필요하다. 재시작 없이 등록한
  source는 미리 `all_sources: true`를 부여받은 caller만 접근할 수 있다. 제한된 caller의
  동적 grant가 필요해질 때 access policy를 별도 control-plane revision으로 관리한다.
