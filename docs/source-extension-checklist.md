# Source Onboarding And Extension Checklist

Status: Git-reviewed YAML source authority; first-launch inventory frozen by ADR 0025

Source authority의 결정 기준은 [ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md)이다.
Source·verified query·budget의 유일한 authority는 각각 다음 Git-reviewed YAML이다.

- `config/sources/*.yaml`
- `config/verified-queries.yaml`
- `config/budget-profiles.yaml`

Runtime admin mutation, Control DB와 hot reload 경로는 없다. `QUERY_MAN_SOURCE_MODE`,
`QUERY_MAN_CONTROL_DSN`, `QUERY_MAN_SOURCE_ENCRYPTION_KEY`, `QUERY_MAN_REPLICA_ID`,
`QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS`는 폐기된 설정이며 하나라도 존재하면 startup을 fail-closed한다.
변경 반영에는 review·test·배포가 필요하다.

## 30초 경로 선택

| 하려는 일 | 사용할 경로 |
|---|---|
| 새 DB/source 추가 또는 기존 source 변경 | 이 문서로 end-to-end 영향과 승인·검증 범위를 확인한다. |
| 추가 가능성과 준비 항목만 정리 | [`query-man-source-onboarding` Skill](../skills/query-man-source-onboarding/SKILL.md)로 plan-only handoff를 만든다. |
| 현재 source YAML 조회·검증 | `uv run qm source list`, `show`, `validate`를 사용한다. 이 명령은 변경하지 않는다. |
| 현재 source의 데이터 질문 | [`query-man-text-to-sql` Skill](../skills/query-man-text-to-sql/SKILL.md)의 query workflow를 사용한다. |

## 이 문서는 언제 사용하나요?

현재 첫 오픈에 사용할 source는 `development-issues`, `market-voc` 두 개뿐이다. 새 PostgreSQL
database나 source ID를 추가하려면 이 checklist로 영향 범위를 먼저 확인한다.

지금의 source 추가와 변경은 Git-reviewed `config/sources/*.yaml`로 처리한다. 검토된 inventory를
바꾸고 traffic 밖에서 검증한 뒤 재배포하는 변경이다. 따라서
[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 대상, 이유, 영향과 rollback을 사용자에게
제시하고 정확히 승인받기 전에는 manifest, 코드, schema와 route를 변경하지 않는다.

## 먼저 알아둘 용어

| 용어 | 이 checklist에서 뜻하는 것 |
|---|---|
| Source | DB 연결 정보, 공개할 view, 업무 설명과 query 제한을 묶은 Query Man 등록 단위다. |
| Curated view | DB owner가 한 가지 데이터 단위와 필요한 column만 안전하게 공개한 읽기용 view다. |
| Reader | Curated view에 필요한 `SELECT` 권한만 가진 PostgreSQL 로그인 계정이다. |
| Manifest | Git-reviewed `config/sources/*.yaml`이다. Source ID, DB 위치, 공개 범위, 기존 budget profile과 secret 환경 변수 이름을 적고 secret 값은 넣지 않는다. |
| Budget profile | Timeout, 동시 실행 수, 임시 자원과 결과 크기를 함께 제한하는 기존 설정 묶음이다. |
| Metadata revision | 공개 DB 구조와 업무 설명 등 SQL 생성에 필요한 context의 정확한 버전이다. |
| SQL policy revision | 허용 SQL, 함수, 연산자와 최종 결과 타입 규칙의 정확한 버전이다. |
| OID | PostgreSQL이 데이터 타입을 구분하는 번호다. 현재 최종 결과는 아래 일곱 타입만 허용한다. |
| Verified query | 대표 질문의 SQL, revision과 예상 결과를 묶은 회귀 시험이다. 사용자 SQL 허용 목록은 아니다. |
| RLS | PostgreSQL이 tenant·사용자별로 볼 수 있는 행을 나누는 정책이다. 현재 source에서는 전면 차단한다. |
| MCP parity | 같은 질문과 SQL이 HTTP와 MCP에서 같은 revision, 성공 결과와 공개 오류 의미를 제공하는 상태다. |
| Pinned artifact | 검증한 것과 같은 application/container image인지 digest나 revision으로 고정한 배포물이다. |

더 많은 용어는 [Query Man 용어 사전](glossary.md)에서 확인할 수 있다.

현재 최종 결과로 허용하는 PostgreSQL 타입은 다음과 같다.

| 타입 | OID |
|---|---:|
| 큰 정수 `int8` | 20 |
| 작은 정수 `int2` | 21 |
| 정수 `int4` | 23 |
| 문자열 `text` | 25 |
| 날짜 `date` | 1082 |
| 시간대가 있는 시각 `timestamptz` | 1184 |
| 정확한 소수 `numeric` | 1700 |

Boolean은 filter와 중간 계산에 사용할 수 있지만 최종 결과 column으로 반환할 수 없다. JSON,
float, UUID, array 등 다른 타입도 현재 final result 범위 밖이다. 상세 정책은
[Guarded Query module](modules/guarded-query/README.md#현재-launch-policy-identity)을 따른다.

## 항상 필요한 검토

승인된 새 inventory 작업은 다음 end-to-end 흐름을 함께 확인한다. 한 행만 통과했다고 source를
추가할 수 있는 것이 아니다.

| 영역 | 확인할 결과 | 쉽게 말하면 |
|---|---|---|
| Source database | PostgreSQL 18, server UTF-8, 최소 권한 `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, 유한 connection limit와 non-RLS curated view | DB와 읽기 계정 자체가 안전해야 한다. |
| Source Catalog | Strict YAML manifest, credential 환경 변수 이름, 기존 budget profile, 필요한 semantic overlay, source projection과 공개 domain column 0개 | 등록 문서에 비밀이나 지원 밖 DB 타입이 없어야 한다. |
| Metadata | Pool checkout에서 client UTF-8 요청, SQL 없는 PG18/server·client·driver UTF-8 검사, domain 사전 거부, bounded catalog와 revision publish | SQL을 만들기 전에 읽는 DB 지도가 정확하고 제한돼야 한다. |
| Guarded Query | 실제·광고 final result OID가 `20, 21, 23, 25, 1082, 1184, 1700` 안인지, read-only transaction·limit·cancel·rollback | 안전한 SQL과 결과 타입만 제한 안에서 실행돼야 한다. |
| Assurance | L0/L1/L2 품질, verified question/SQL/result expectation, unsupported OID와 drift negative case | 대표 질문의 답과 실패 경로가 회귀 시험을 통과해야 한다. |
| Runtime/Delivery | 단일 replica, query-only identity, 정확한 readiness 응답, HTTP/MCP parity, pinned artifact와 stop/rollback 절차 | 검증한 한 개의 배포물이 같은 외부 동작을 제공하고 되돌릴 수 있어야 한다. |

Manifest에는 host, port, database와 user 같은 운영 locator와 password 환경 변수 이름만 둔다.
Password, token과 실제 secret은 Git, metadata, HTTP/MCP와 log에 넣지 않는다. Client나 AI model은
DSN, schema, role 또는 source credential을 선택할 수 없다.

PostgreSQL catalog에서 type과 numeric precision/scale을 자동 수집한다. Table·column `COMMENT`는
grain, 단위, 상태값, nullable 의미와 집계 주의를 설명하는 사람-readable metadata로 활용하되
비신뢰 입력으로 검증한다. Comment의 PII 표시는 검토 정보일 뿐 노출 허가가 아니며,
실제 보호는 curated view, reader grant, source policy와 검증이 강제한다.

## 조건에 따라 필요한 작업

다음 작업은 실제 source가 요구할 때만 한다.

- Physical catalog만으로 업무 의미가 충분하지 않을 때 grain, alias, 대표 시간, 승인된 join,
  measure와 predicate를 semantic overlay에 추가한다.
- 복잡한 join이나 중복 집계 위험을 DB 안에서 안전하게 감춰야 할 때만 DB owner가 `ai` curated
  view를 만든다.
- 현재 일곱 결과 타입으로 답할 수 없는 실제 질문이 있으면 source 추가를 멈춘다. 먼저
  [parked `ENC-01`~`ENC-02`](development-todo.md#현재-일정에-없는-일)의 새 result policy, revision과
  migration 범위를 별도로 결정하고 승인받아야 한다.
- RLS가 필요한 source는 현재 등록, publish 또는 serving하지 않는다.
  [`RLS-01`~`RLS-03`](development-todo.md#현재-일정에-없는-일)의 attestation, migration과 protected
  cutover가 별도로 승인·완료되기 전 작은 예외를 만들지 않는다.
- 새 function, operator, type, collation, extension 또는 reader setting이 필요하면 SQL policy,
  metadata revision과 result identity 영향을 별도로 검토한다.

## 즉시 중단할 조건

다음 중 하나라도 있으면 publish나 route를 진행하지 않는다.

- RLS source 또는 RLS에 의존하는 view
- PostgreSQL 18 또는 server/client/driver UTF-8 불일치
- 지원 범위 밖 final result OID나 공개된 domain column
- 설명할 수 없는 metadata revision 또는 결과 변화
- 현재 static dataset의 [9개 verified query expectation](modules/assurance/README.md#verified-query-회귀검사)이나 새 source의 승인된
  verified result 실패
- Reader privilege, DDL/settings inventory, image/config 또는 rollback이 불명확함
- SQL policy v2와 v3 process가 같은 serving fleet에 섞임
- 정확한 inventory·배포 승인이나 protected environment 실행 승인이 없음

실패를 기존 값으로 자동 보정하거나 “이번 source만” 예외로 통과시키지 않는다. 현재 정상 source와
revision은 그대로 유지하고 원인과 필요한 변경 범위를 다시 검토한다.

## 보통 필요하지 않은 작업

단순 source 추가에는 보통 다음 작업이 필요하지 않다.

- Python의 `source_id`별 분기
- Database별 HTTP/MCP tool이나 endpoint
- 새 framework, plugin, factory 또는 wrapper
- 새 budget tier나 caller별 source grant
- 새 persisted authority나 runtime mutation surface

이 항목이 필요해 보이면 단순 source 추가가 아니라 module interface, external/persisted format,
policy, safety/lifecycle, composition 또는 protected procedure 변경일 가능성이 높다. 실제 변경 범주,
provider/consumer 영향과 rollback을 밝히고 먼저 정확한 승인을 받는다.

## HTTP와 MCP가 지켜야 할 동일한 흐름

새 source가 승인돼도 MCP tool은 `list_sources`, `get_context`, `query` 세 개를 유지한다. Database마다
새 tool이나 endpoint를 만들지 않는다.

1. `get_context`로 질문에 맞는 metadata를 받는다.
2. 응답의 exact `metadata_revision`과 `sql_policy_revision`으로 SQL을 만든다.
3. 같은 두 revision을 `query`에 전달한다.
4. HTTP와 MCP의 성공 결과와 details 없는 unavailable 오류 의미가 같은지 확인한다.

Client-side Skill은 이 순서를 안내할 뿐 authorization, SQL validation, reader privilege와 resource
limit을 대신하지 않는다.

## 검토 결과에 남길 내용

승인을 요청할 때는 최소한 다음을 짧게 정리한다.

- Source ID, owner, environment와 추가 이유
- 공개할 curated view의 데이터 단위와 대표 질문
- 선택한 기존 budget profile과 connection capacity
- Reader/TLS/non-RLS, PostgreSQL 18/UTF-8 확인 결과
- Metadata와 SQL policy revision, verified result와 HTTP/MCP 검증 계획
- 배포 artifact, traffic 전환, 중단 조건과 rollback 계획
- 확인하지 못한 항목과 각 항목을 결정할 담당자

계획만 필요한 경우에는
[`query-man-source-onboarding` Skill](../skills/query-man-source-onboarding/SKILL.md)을 사용한다. Skill의
결과는 검토용 handoff이며 실제 등록, 승인 또는 실행 증거가 아니다. Skill은 credential 조회·전달,
SQL/DDL/role/grant, repository 수정·commit·push를 수행하지 않는다. `mutation_count: 0`은 계획만
준비됐다는 뜻이며 source 등록 완료를 뜻하지 않는다.
