# ADR 0017: Shared Source Access And Source Resource Tier

Status: Accepted

Date: 2026-08-23

## Context

초기 운영에서는 데이터베이스 등록을 관리자만 수행하고, 인증된 query 사용자는 모두 같은
source 목록을 조회한다. 사용자나 조직마다 source 권한, quota 또는 비용 정책을 다르게 적용할
요구는 아직 없다.

Query Man에는 이미 source가 중앙의 `budget_profile` 하나를 선택하고 runtime이 그 profile의
실행 시간, 동시성, memory/temp, plan과 결과 크기 제한을 강제하는 경로가 있다. 이 요구를 위해
별도 `cost_tier`, caller별 tier binding 또는 Control DB grant 계층을 추가하면 같은 결정을 두
곳에서 관리하게 된다.

## Decision

- `budget_profile`을 유일한 resource tier로 사용한다. 관리자는 source를 추가하거나 변경할 때
  승인된 기존 profile 중 하나를 선택한다.
- 한 source의 모든 query principal은 같은 effective `budget_profile` 정의를 사용한다.
  Query request, caller, user 또는 organization은 profile을 선택하거나 override할 수 없다.
- 인증된 query principal은 모두 같은 active source 목록을 본다. Source publish/deactivate는
  전체 query principal의 visibility를 한 번에 바꾼다.
- Source 관리 API와 query cancel은 별도 admin credential만 사용할 수 있다. Query credential은
  source를 조회할 수 있어도 admin 권한을 갖지 않는다. 초기 관리 권한은 하나의 boolean
  capability로 충분하며 viewer/operator/approver 역할 계층은 만들지 않는다.
- Stable `caller_id`와 `tenant_id`는 audit와 기존 RLS source의 trusted session context를 위해
  유지한다. 다만 source visibility, resource tier, quota 또는 비용 집계의 선택 key로 사용하지
  않는다. Source-native RLS는 이 결정과 별개의 row-isolation 계약이다.
- 사용자/조직별 source ACL, tier override, quota, fairness와 chargeback은 요구가 생긴 뒤 별도
  ADR로 설계한다. 지금은 이를 위한 assignment table, nullable scope column 또는 확장용 API를
  미리 만들지 않는다.
- 통화 단위 비용과 `budget_profile`을 혼동하지 않는다. Profile은 실행 피해의 hard limit이며,
  provider billing이 없는 경우 비용 화면은 source/profile별 자원 사용량과 추세만 보여준다.

## Access Policy Cutover

`CTRL-03`은 access-policy manifest를 version 2로 올리고 caller별 source scope를 제거한다.

- Version 2 caller는 `caller_id`, `tenant_id`, `token_env`와 `operator`만 선언한다. 모든 인증
  identity의 active source access는 implicit하며 `allowed_sources`와 `all_sources`를 받지 않는다.
- Version 1 또는 legacy scope field가 남은 policy는 자동 변환하거나 권한을 확대하지 않고
  startup에서 fail-closed한다. Runtime write-back이나 migration helper는 없다.
- Managed mode는 version 2 policy file, 최소 한 개의 non-admin query identity와 한 개의 explicit
  operator admin identity를 요구한다. 단일 `QUERY_MAN_API_TOKEN`과 anonymous caller는 거부한다.
- Bootstrap의 anonymous local identity와 단일 API token은 query-only다. Explicit admin이 필요한
  bootstrap 운영은 version 2 policy를 사용한다.
- `operator`는 admin API와 query cancel을 추가하는 boolean capability superset이다. 별도 role
  enum이나 exclusive admin/query route hierarchy를 만들지 않는다.

이는 deployment configuration의 fail-closed cutover이며 Control DB schema migration이나 새
dependency를 추가하지 않는다.

## Consequences

- Source를 hot-add하면 별도 caller grant나 service restart 없이 모든 query 사용자가 즉시 본다.
- DB 추가 시 사람이 고르는 자원 정책은 기존 `budget_profile` 하나뿐이다. 별도 tier catalog와
  user/organization binding이 없다.
- Source마다 서로 다른 profile을 선택할 수 있지만 같은 source 안에서 사용자를 차등하지 않는다.
- Caller별 quota와 공정성을 보장하지 않는다. 필요성이 관측되기 전에는 distributed limiter나
  identity별 counter를 추가하지 않는다.
- Control Plane은 source, effective `budget_profile`, 관련 metadata revision, 규모, 사용량과
  freshness를 한곳에서 보여주되 caller별 권한표나 chargeback ledger를 관리하지 않는다.

이 결정은 ADR 0004의 미래 dynamic source-grant 계획과 ADR 0016의 다단계 관리 RBAC 및
caller-grant 관리 목표를 대체한다. Caller/tenant audit와 RLS 안전 경계는 그대로 유지한다.
