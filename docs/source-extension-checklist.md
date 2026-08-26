# Source Extension Checklist

Status: Static first-launch inventory frozen by ADR 0025

## 목적

현재 launch inventory는 `development-issues`, `market-voc` 두 source뿐이다. 새 PostgreSQL
database나 source ID 추가는 평상시 hot onboarding이 아니라 새 inventory review와 재배포다.
[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 launch 범위를 바꾸므로 대상, 이유,
영향과 rollback을 사용자에게 제시하고 정확히 승인받기 전에는 manifest, code, schema와 route를
변경하지 않는다.

구현된 managed onboarding은 삭제하지 않았지만 static first launch에는 참여하지 않는다. 동적
source 운영이 실제로 필요해질 때 [source onboarding](source-onboarding.md)과
[source management plane](source-management-plane.md)을 별도 운영 결정 아래 사용한다.

## 항상 필요한 작업

승인된 새 inventory 작업은 다음 end-to-end slice를 함께 검토한다.

| 영역 | 확인할 내용 |
|---|---|
| Source database | PostgreSQL 18, server UTF-8, 최소 권한 `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, 유한 connection limit와 non-RLS curated view |
| Source Catalog | Strict manifest, credential environment reference, 기존 budget profile, semantic overlay, static source projection과 exposed domain column 0개 |
| Metadata | Pool checkout의 client UTF-8 요청, no-SQL PG18/server·client·driver UTF-8 검사, domain pre-publication rejection, bounded catalog와 revision publish |
| Guarded Query | 실제·광고 final result OID가 `20, 21, 23, 25, 1082, 1184, 1700` 안인지, read-only transaction·limit·cancel·rollback |
| Assurance | L0/L1/L2 quality, verified question/SQL/result invariant, unsupported OID와 drift negative corpus |
| Runtime/Delivery | 단일 replica, query-only identity, exact ready, HTTP/MCP parity, pinned artifact와 stop/rollback 절차 |

Manifest에는 host/port/database/user의 운영 locator와 password 환경 변수 이름만 둔다. Password,
token과 실제 secret은 Git, metadata, HTTP/MCP와 log에 넣지 않는다. Client나 모델은 DSN, schema,
role 또는 source credential을 선택할 수 없다.

## 조건에 따라 필요한 작업

- 새 비즈니스 의미가 physical catalog로 충분하지 않을 때만 grain, alias, representative time,
  approved join, measure와 predicate를 semantic overlay에 추가한다.
- 복잡한 join/fanout을 안전하게 캡슐화해야 할 때만 source DB owner가 `ai` curated view를 만든다.
- 현재 seven-OID 결과로 답할 수 없는 실제 질문이 있을 때는 source를 추가하지 않고 먼저
  `ENC-01`의 새 result policy/revision/migration을 별도로 승인받는다.
- RLS가 필요한 source는 현재 등록·publish·serving하지 않는다. `RLS-01`~`RLS-03`의 새
  attestation, migration과 protected cutover가 승인·완료되기 전 작은 예외를 만들지 않는다.
- Function/operator/type/collation/extension이나 reader semantic setting을 추가해야 하면 SQL policy와
  metadata/result identity 영향을 별도로 검토한다.

## 중단 조건

다음 중 하나라도 있으면 publish나 route를 진행하지 않는다.

- RLS source 또는 RLS 의존 view
- PostgreSQL 18/server·client·driver UTF-8 불일치
- 지원 밖 final result OID, exposed domain column 또는 설명할 수 없는 metadata/result drift
- 9개 기존 invariant나 새 source의 승인된 verified result 실패
- Reader privilege, DDL/settings inventory, image/config 또는 rollback이 불명확함
- SQL policy v2/v3 process의 mixed serving

## 보통 필요하지 않은 작업

- Python의 `source_id`별 분기
- Database별 HTTP/MCP tool이나 endpoint
- 새 framework, plugin, factory 또는 wrapper
- 새 budget tier나 caller별 source grant
- Control DB schema 변경

이 항목이 필요해 보이면 단순 source 추가가 아니라 module interface, external/persisted format,
policy, safety/lifecycle, composition 또는 protected procedure 변경일 가능성이 높다. 해당 범주와
provider/consumer 영향을 밝히고 먼저 승인받는다.

## MCP 정합성 경계

새 source가 승인되더라도 MCP tool은 `list_sources`, `get_context`, `query` 세 개를 유지한다.
`get_context`가 반환한 exact `metadata_revision`과 `sql_policy_revision`을 같은 `query`에 전달하고,
HTTP와 MCP에서 성공 결과와 details 없는 unavailable 오류가 같아야 한다. Client-side Skill은
workflow를 설명할 뿐 authorization, SQL validation, reader privilege와 resource limit을 대신하지
않는다.
