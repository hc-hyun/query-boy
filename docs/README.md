# Query Man 문서 안내

현재 tree에는 현재 동작, 안전 경계와 실행 가능한 절차만 둡니다. 완료 이력과 삭제한 설계 문서는
[Git 기록 안내](verification/README.md)에서 찾습니다.

## 독자별 시작점

| 독자 | 먼저 읽을 문서 | 필요할 때 |
|---|---|---|
| 사용자 | [프로젝트 README](../README.md) | [Architecture](architecture.md), [용어 사전](glossary.md) |
| 개발자·agent | [활성 개발 지침](development-guidelines.md), [Module index](modules/README.md) | Primary module README, [Active TODO](development-todo.md) |
| 운영자·DBA | [Operations](operations.md) | [Query 제한](query-cost-control.md), [Source extension](source-extension-checklist.md) |
| 결정·검증 근거를 찾는 사람 | [현재 결정](decisions/README.md) | [검증과 Git 기록](verification/README.md) |

이 표는 탐색을 돕는 안내일 뿐 변경·배포·DB 작업 권한을 주지 않습니다. Protected environment 작업은
[Operations](operations.md)의 별도 실행 승인, stop과 rollback 조건을 따라야 합니다.

## 현재 계약

- [Architecture](architecture.md): HTTP 요청 흐름과 trust boundary
- [Source onboarding과 extension checklist](source-extension-checklist.md): 두 파일 source package와 DBA apply
- [Query 제한과 자원](query-cost-control.md): 실행 상한과 장애 조사
- [Operations](operations.md): startup, 관측, cutover와 rollback
- [현재 결정](decisions/README.md): 현행 ADR index

현재 launch safety는 [ADR 0025](decisions/0025-static-non-rls-first-launch.md), source package는
[ADR 0034](decisions/0034-source-view-package-and-direct-admission.md), startup inventory는
[ADR 0035](decisions/0035-reviewed-source-package-inventory.md), budget YAML은
[ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md)이 정합니다.

실제 DB 연결 `DBENV-01`, 인증 연결 `AUTHENV-01`과 traffic 전환 `LAUNCH-02`는
[Active TODO](development-todo.md)에 남아 있습니다. Repository와 fixture의 PASS를 protected 실행 완료로
해석하지 않습니다.
