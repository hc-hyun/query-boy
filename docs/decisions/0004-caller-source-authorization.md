# ADR 0004: Caller And Source Authorization

> [ADR 0025](0025-static-non-rls-first-launch.md) launch note: caller authentication,
> authorization and shared source visibility remain current, but every RLS source is quarantined before
> tenant/revision/metadata/queue/database work. RLS trusted-context consequences below are historical
> capability, not current launch serving behavior.
>
> [ADR 0029](0029-authbridge-resource-server-jwt.md) authentication note: the version 2 opaque
> policy remains the local/CI default, while an opt-in AuthBridge JWT authority may produce the same
> server-side `CallerContext`. Its managed-mode and token-validation rules supersede the policy-only
> wording below when OAuth mode is selected.

Status: Accepted; source-scope model superseded by ADR 0017

Date: 2026-08-22

## Context

Bearer token 하나만으로 caller와 tenant를 구분하지 못하면 audit와 RLS trust boundary가
무너지고, MCP와 HTTP가 서로 다른 authorization을 적용할 위험이 있다. Client가 tenant,
admin capability나 resource tier를 요청 값으로 지정하게 해도 같은 문제가 생긴다.

초기 구현은 caller별 `allowed_sources|all_sources`를 제공했지만, 초기 production 운영은 모든
query identity가 같은 active source 목록과 source-resolved budget을 쓰기로 결정했다. 차등
source scope를 유지하면 사용하지 않는 grant 모델과 hot-add synchronization만 남는다.

## Decision

- 인증 결과는 server-side `CallerContext(caller_id, tenant_id, operator)`다. 모든 인증 identity는
  모든 active source를 보고 `operator`만 admin API와 query cancel capability를 추가한다.
- Version 2 access-policy manifest는 caller ID, tenant ID, token 환경 변수 이름과 `operator`
  boolean만 연결한다. `allowed_sources`, `all_sources`, role enum과 caller-grant table은 없다.
  Version 1이나 scope field가 남은 policy는 조용히 권한을 넓히지 않고 startup에서 거부한다.
  Token 값은 manifest, response, log에 저장하지 않고 시작 시 환경 변수에서 읽어 SHA-256
  digest만 보관한다.
- `/sources`, `/meta`, `/query`는 `GatewayService`를 유일한 application boundary로
  사용한다. HTTP와 MCP adapter는 인증된 context만 이 service에 전달한다.
- 존재하지 않는 source는 metadata load, SQL validation과 concurrency slot 전에 동일한
  `404 SOURCE_NOT_FOUND`를 반환한다.
- Tenant ID는 SQL text, `search_path` 또는 client-controlled session setting에 넣지
  않는다. RLS source는 ADR 0014의 server-derived transaction-local trusted session context를
  사용한다.
- Bootstrap loopback에서 인증 설정이 없을 때만 query-only `local-development` caller를
  사용한다. Bootstrap의 단일 `QUERY_MAN_API_TOKEN`도 query-only shortcut이며 admin capability를
  주지 않는다.
- Managed mode는 version 2 `QUERY_MAN_ACCESS_POLICY_FILE`을 요구하고 단일 API token과 anonymous
  caller를 거부한다. Policy에는 최소 한 개의 non-admin query identity와 한 개의 explicit
  operator admin identity가 있어야 한다.
- Operator는 별도 exclusive role이 아니라 capability superset이다. Query credential은 모든
  admin endpoint와 cancel에서 거부되고 operator credential은 같은 query path도 사용할 수 있다.

[ADR 0017](0017-shared-source-access-and-resource-tier.md)의 shared-access 결정에 따라
source publish/deactivate는 모든 query identity의 visibility를 함께 바꾼다. Stable caller/tenant
identity는 audit와 source-native RLS에 계속 사용하지만 source access, budget, quota나 비용
dimension을 선택하지 않는다.

## Consequences

- Version 오류, legacy scope field, 중복 caller/token reference, 누락되거나 범위를 벗어난
  secret은 startup을 fail-closed시킨다.
- Source hot-add에는 caller grant 변경이나 service restart가 없다.
- 같은 source에서도 RLS tenant에 따라 row 결과는 달라질 수 있지만 source visibility와
  `budget_profile`은 같다.
- 이 전환은 application access-policy schema 변경이며 Control DB migration이나 dependency를
  추가하지 않는다.
