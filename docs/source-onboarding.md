# Source Onboarding

Status: ADR 0025 static first-launch routing guide; managed onboarding is inactive

## 먼저 알아둘 점

Query Man에서 **database**는 PostgreSQL 서버 안의 실제 데이터 저장 공간이고, **source**는 그
database를 안전하게 조회하기 위해 검토한 연결 정보, 공개 view, 설명과 사용량 제한을 묶은
등록 단위다.

현재 첫 오픈에서 사용할 source는 `development-issues`, `market-voc` 두 개뿐이다.
[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 이 범위는 다음을 뜻한다.

- 새 database나 source를 실행 중에 바로 추가하지 않는다.
- 추가가 필요하면 inventory, 안전성, 품질과 rollback을 검토하고 사용자의 정확한 승인을 받은 뒤
  repository와 배포를 함께 변경한다.
- RLS를 사용하는 source나 RLS에 의존하는 view는 현재 등록하거나 제공하지 않는다.
- 동적 source 관리 기능은 코드에 보존돼 있지만 현재 serving process에는 연결하지 않는다.

쉽게 말하면, 지금은 “관리 화면에서 DB 하나를 즉시 추가”하는 단계가 아니라 “검토된 DB 목록을
바꾸는 작은 배포” 단계다.

## 어떤 문서를 읽어야 하나요?

| 하려는 일 | 지금 따라야 할 문서와 결과 |
|---|---|
| 현재 첫 오픈에 새 DB/source를 추가 | [Source Extension Checklist](source-extension-checklist.md)로 검토한다. 정확한 inventory·배포 승인을 받기 전에는 manifest, 코드, schema와 route를 바꾸지 않는다. |
| 추가 가능 여부와 필요한 준비만 정리 | [`query-man-source-onboarding` Skill](../skills/query-man-source-onboarding/SKILL.md)로 계획을 만든다. Skill은 DB, repository, Control DB와 admin API를 변경하지 않는다. |
| 보존된 managed mode를 실제로 활성화 | 활성화 범위가 별도로 승인된 뒤에만 [Managed Source Onboarding](managed-source-onboarding.md)과 [Source Management Plane](source-management-plane.md)을 사용한다. |
| 현재 두 source를 이용해 데이터 질문에 답하기 | Onboarding이 아니라 [`query-man-text-to-sql` Skill](../skills/query-man-text-to-sql/SKILL.md)의 query workflow를 사용한다. |

Managed mode 활성화 승인은 RLS serving 승인이나 protected environment 실행 승인이 아니다. RLS는
별도 attestation·migration·cutover 결정이 있기 전까지 차단하며, 실제 운영 변경은 대상 환경,
접근 권한, 중단 조건과 변경 기록 책임을 확인한 별도 실행 승인이 필요하다.

## 핵심 용어

| 용어 | 쉬운 뜻 |
|---|---|
| Source ID | Query Man이 source를 구분하는 바뀌지 않는 이름이다. 예: `market-voc`. |
| Curated view | DB owner가 조회 목적에 맞는 column과 한 가지 데이터 단위만 공개한 읽기용 view다. |
| Reader | Curated view를 `SELECT`할 최소 권한만 가진 PostgreSQL 로그인 계정이다. |
| Manifest | Source ID, DB 위치, 공개 범위, reader secret의 환경 변수 이름과 제한 profile을 적은 등록 문서다. Secret 값 자체는 넣지 않는다. |
| Budget profile | Query 시간, 동시 실행 수, 임시 자원과 결과 크기의 기존 제한 묶음이다. |
| Verified query | 대표 질문의 SQL과 예상 결과가 변경 뒤에도 유지되는지 확인하는 회귀 시험이다. 사용자 SQL 허용 목록은 아니다. |
| Static 또는 bootstrap | Repository에서 검토한 source를 process 시작 때 읽는 현재 방식이다. |
| Managed mode | Control DB의 source 이력과 현재 상태를 사용해 재시작 없이 반영할 수 있는 보존된 방식이다. 현재 첫 오픈에서는 비활성이다. |
| RLS | PostgreSQL이 사용자·tenant별로 볼 수 있는 행을 나누는 정책이다. 현재 Query Man source에서는 전면 차단한다. |

다른 낯선 표현은 [Query Man 용어 사전](glossary.md)에서 먼저 확인할 수 있다.

## 현재 새 source 검토 흐름

정확한 항목과 중단 조건은 [extension checklist](source-extension-checklist.md)가 기준이다. 전체
흐름은 다음과 같다.

1. 사용할 질문, source owner, 환경과 database 변경 이력을 정한다.
2. DB owner가 PostgreSQL 18/UTF-8, non-RLS curated view와 최소 권한 reader를 준비한다.
3. 기존 `budget_profile` 중 workload에 맞는 하나를 선택한다. Source 하나를 위해 제한을 임의로
   완화하거나 Python에 `source_id` 분기문을 만들지 않는다.
4. Source Catalog, Metadata와 Guarded Query 경계에서 manifest, catalog, revision, SQL와 결과 타입을
   검증한다.
5. 대표 질문과 verified query로 결과·품질을 확인하고 HTTP/MCP가 같은 의미를 제공하는지 확인한다.
6. 대상 inventory, artifact, 배포, 중단 조건과 rollback을 정확히 승인받은 뒤 traffic 밖에서
   acceptance를 실행하고 재배포한다.

## 누가 무엇을 준비하나요?

| 담당 | 준비하는 것 | 하지 않는 것 |
|---|---|---|
| DB owner | Curated view, reader role/grant, TLS, connection 여유와 DB migration reference | Query Man production source를 직접 활성화하지 않는다. |
| Query Man 관리자 | Source ID 충돌, 기존 budget 적합성, secret 전달 위치, inventory와 rollback 계획 | Credential 값을 문서·Git·응답에 남기지 않는다. |
| 개발자 | 기존 module interface 안에서 manifest/configuration과 검증을 연결 | Source별 Python branch, 새 endpoint나 임시 우회 정책을 만들지 않는다. |
| 검토자 | 대표 질문, 공개 grain·column, verified 결과와 운영 중단 조건 확인 | 확인하지 않은 결과를 통과했다고 기록하지 않는다. |

## 즉시 멈춰야 하는 경우

다음 중 하나라도 있으면 등록이나 배포를 진행하지 않는다.

- RLS source 또는 RLS에 의존하는 view다.
- PostgreSQL 18, server/client/driver UTF-8 조건이 맞지 않는다.
- Reader 권한, 공개 relation, 데이터 단위나 join 의미가 불명확하다.
- 현재 지원하는 최종 결과 타입으로 대표 질문에 답할 수 없다.
- 기존 budget profile이 workload에 맞는다는 근거가 없다.
- Verified query, revision, container 또는 rollback 검증이 실패하거나 실행되지 않았다.
- Password, token, 전체 DSN, encryption key 또는 provider secret 경로가 planning 문서에 들어왔다.
- 정확한 inventory·배포 승인이나 protected environment 실행 승인이 없다.

## Plan-only Skill

반복 가능한 준비 계획이 필요하면
[`query-man-source-onboarding`](../skills/query-man-source-onboarding/SKILL.md)을 사용한다. 이 Skill은
비밀이 아닌 사실을 정리하고 DB owner와 Query Man 관리자의 할 일을 나누지만 다음 작업은 하지 않는다.

- SQL, DDL 또는 role/grant 실행
- Credential 조회·검증·전달
- Admin API 호출이나 source publish
- Production manifest 생성·commit
- 새 budget profile, 사용자별 source 권한 또는 RLS 예외 설계

출력의 `mutation_count: 0`은 실제 변경을 하지 않았다는 뜻이다. 계획이 준비됐다는 사실을 source가
등록됐거나 운영 준비가 끝났다는 의미로 해석하지 않는다.

## Managed mode는 언제 읽나요?

[Managed Source Onboarding](managed-source-onboarding.md)은 구현된 동적 source lifecycle을 보존하는
고급 운영 문서다. 다음 조건이 모두 충족되기 전에는 실행 절차로 사용하지 않는다.

- Static inventory 변경이 아니라 managed mode 활성화가 실제 요구다.
- Control DB authority, admin access, secret, replica와 rollback 범위를 별도로 결정하고 승인했다.
- 실제 환경 변경을 수행할 접근 권한, 대상, 중단 조건과 change-record 담당자가 정해졌다.
- Source가 non-RLS이며 현재 PostgreSQL/result policy를 그대로 만족한다.

현재 두 source를 유지하거나 static inventory에 source 하나를 추가하는 작업에는 managed Control DB,
admin mutation이나 hot reload를 끼워 넣지 않는다.

## 관련 문서

| 더 알고 싶은 내용 | 문서 |
|---|---|
| 현재 추가 검토의 정확한 항목 | [Source Extension Checklist](source-extension-checklist.md) |
| 낯선 용어의 쉬운 설명 | [Query Man 용어 사전](glossary.md) |
| 첫 오픈의 승인된 범위 | [ADR 0025](decisions/0025-static-non-rls-first-launch.md) |
| Source Catalog의 역할과 interface | [Source Catalog module](modules/source-catalog/README.md) |
| 보존된 managed 상세 절차 | [Managed Source Onboarding](managed-source-onboarding.md) |
| Managed authority와 admin surface | [Source Management Plane](source-management-plane.md) |
| Query 제한과 resource 의미 | [Query Cost And Resource Control](query-cost-control.md) |
| Verified query의 현재 dataset | [Verified Query Baseline](verified-queries.md) |
