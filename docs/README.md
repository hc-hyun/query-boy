# Query Man 문서 안내

이 문서는 current documentation의 단일 탐색 시작점입니다. 완료 이력과 삭제한 설계 문서는
[검증과 Git 기록](verification/README.md)에서 찾고, current tree에는 계속 적용되는 계약과 실행 가능한
절차만 둡니다. 전체 문서를 순서대로 읽지 말고 아래에서 목적에 맞는 한 행을 선택합니다.

## 목적별 바로가기

| 하려는 일 | 먼저 읽기 | 이어서 읽기 |
|---|---|---|
| 제품 이해·API 사용 | [프로젝트 README](../README.md) | [Architecture](architecture.md), [용어 사전](glossary.md) |
| 코드 변경 | [활성 개발 지침](development-guidelines.md) | [Module index](modules/README.md)에서 primary module 선택 |
| Repository skill 사용 | [Skill 사용 가이드](skills.md) | 요청에 맞는 skill을 `$이름`으로 명시 호출 |
| 기존 DB에 source 추가 | [Source onboarding checklist](source-extension-checklist.md) | 해당 DB의 기존 profile과 certificate 재사용 |
| 새 물리 DB 연결 | [Source onboarding checklist](source-extension-checklist.md) | [Database client certificate guide](database-certificate-authentication.md) |
| 시작·배포·rollback | [Operations](operations.md) | [Query 제한과 자원](query-cost-control.md) |
| 남은 작업 확인 | [Active TODO](development-todo.md) | 해당 작업의 procedure 문서 |
| 결정 이유 확인 | [현재 결정](decisions/README.md) | 연결된 accepted ADR |
| local DB/container 검증 | [Query Cave](../query-cave/README.md) | [검증과 Git 기록](verification/README.md) |

이 표는 읽을 순서를 안내할 뿐 변경·배포·DB 작업 권한을 주지 않습니다. Protected environment 작업은
[Operations](operations.md)의 실행 승인, stop과 rollback 조건을 따라야 합니다.

## 문서별 책임

| 문서 | 한 곳에서 소유하는 내용 |
|---|---|
| [Architecture](architecture.md) | 현재 요청 흐름, trust boundary와 system 범위 |
| [활성 개발 지침](development-guidelines.md) | 개발, 승인 분류, 테스트와 handoff 규칙 |
| [Module index](modules/README.md)와 module README | 코드 ownership, interface와 집중해서 읽을 범위 |
| [Operations](operations.md) | protected startup, acceptance, cutover와 rollback 순서 |
| [Source onboarding checklist](source-extension-checklist.md) | source package 작성, review와 DB apply 절차 |
| [Database client certificate guide](database-certificate-authentication.md) | DB별 인증서 발급·mapping·rotation 절차 |
| [Skill 사용 가이드](skills.md) | Repository 전용 skill의 선택, 호출, credential과 승인 경계 |
| [Query 제한과 자원](query-cost-control.md) | budget 강제 계층과 장애 조사 |
| [Active TODO](development-todo.md) | 현재 baseline과 아직 완료되지 않은 작업 |
| [현재 결정](decisions/README.md) | accepted policy와 ADR index |
| [검증과 Git 기록](verification/README.md) | repository gate, protected evidence의 구분과 archive 탐색 |

정확한 설정 값은 versioned config, 정책 근거는 accepted ADR, 실제 동작은 code와 runnable test를 함께
확인합니다. 서로 충돌하면 임의로 문서를 고치지 않고 [승인 규칙](development-guidelines.md#승인-규칙)에
따라 불일치를 보고합니다.

## 문서 유지 원칙

- 요약 문서는 상세 규칙을 복제하지 않고 owner 문서로 연결합니다.
- 현재 상태는 [Active TODO](development-todo.md), 과거 상태는 Git history에만 둡니다.
- 새 문서는 계속 적용되는 별도 계약이나 절차가 기존 owner 문서에 들어가기 어려울 때만 만듭니다.
- 완료 보고와 PASS는 날짜별 문서가 아니라 exact commit·CI provenance에 남깁니다.
