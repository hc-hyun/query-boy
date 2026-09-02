# Query Man 문서 안내

이 페이지가 `docs/`의 유일한 시작점입니다. 독자 역할에 맞는 절만 읽으면 됩니다.

현재 첫 오픈 범위는 [ADR 0025](decisions/0025-static-non-rls-first-launch.md), source package와 직접
metadata admission은 [ADR 0034](decisions/0034-source-view-package-and-direct-admission.md), budget
authority는 [ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md)이 정합니다. 저장소 구현과
로컬 검증은 끝났지만 실제 DB 환경 연결 `DBENV-01`, 인증 환경 연결 `AUTHENV-01`과 그 뒤의
대상 환경 전환 `LAUNCH-02`는 남아 있습니다. Current profile은 `development-issues`, `market-voc`
두 source만 제공하고 모든 RLS source를 차단합니다.
Source의 개인정보 공개 경계는
[ADR 0031](decisions/0031-no-pii-curated-view-boundary.md)을 따릅니다.
Reader의 database `TEMP` 보유 여부를 source admission 조건으로 사용하지 않는 경계는
[ADR 0032](decisions/0032-reader-temp-admission-relaxation.md)를 따릅니다.
Source manifest의 명시적 PostgreSQL TLS mode와 `require` compatibility 경계는
[ADR 0033](decisions/0033-explicit-source-tls-modes.md)을 따릅니다.

## 독자별 시작점

| 독자 | 먼저 읽을 문서 | 다음 문서 |
|---|---|---|
| 제품·일반 독자 | [프로젝트 README](../README.md), [용어 사전](glossary.md) | [Architecture](architecture.md), [MVP data](mvp.md) |
| 개발자·agent | [활성 개발 지침](development-guidelines.md), [Module index](modules/README.md) | Primary module README, [Active TODO](development-todo.md) |
| 운영자·DBA | [Operations](operations.md), [Active TODO](development-todo.md) | [Query 제한](query-cost-control.md), [Source extension](source-extension-checklist.md), [JWT 계약](resource-server-jwt-auth.md) |
| 결정·검증 근거를 찾는 사람 | [현재 결정 요약](decisions/README.md) | [검증과 Git 기록 안내](verification/README.md) |

독자 구분은 탐색을 돕는 표지일 뿐 변경·배포·DB 작업 권한을 주지 않습니다. 특히 protected
environment 작업은 [Operations](operations.md)의 별도 실행 승인과 stop/rollback 조건을 따라야 합니다.

## 공통 제품 문서

- [프로젝트 README](../README.md): 제품 범위, 5분 로컬 실행과 API 사용
- [용어 사전](glossary.md): 제품·DB·개발 용어
- [Architecture](architecture.md): 현재 실행 구조, 요청 흐름과 안전 경계
- [MVP data](mvp.md): 두 예제 source의 grain과 공개 view

## 개발자 문서

- [활성 개발 지침](development-guidelines.md): 승인 분류, 병렬 작업, 테스트와 handoff 규칙
- [Module index](modules/README.md): 여섯 module의 owner, 허용 의존과 코드·테스트 지도
- [Active TODO](development-todo.md): 승인돼 실제로 남은 작업과 보류 주제
- [Assurance module](modules/assurance/README.md): 보안·통합·container·load·soak repository gate
- [Source onboarding·extension checklist](source-extension-checklist.md): 새 DB 변경의 end-to-end 검토 기준
- [Scale fixture](query-boy-scale-fixture.md), [domain lab](query-boy-domain-lab.md): 격리된 개발·검증용 DB fixture

코드 작업은 module index에서 primary module 하나를 고른 뒤 해당 README의 `30초 요약`과
`집중해서 읽을 범위`에서 시작합니다. Package `__init__.py`는 marker-only이며 interface를
re-export하지 않습니다.

## 운영자·DBA 문서

- [Operations](operations.md): first launch, 상태·log·diagnostic, local Compose와 종료 절차
- [Query 제한과 자원](query-cost-control.md): 강제 limit, 관측값과 장애 조사 순서
- [Resource Server JWT 계약](resource-server-jwt-auth.md): AuthBridge access-token 검증과 rollout 경계
- [Source onboarding·extension checklist](source-extension-checklist.md): curated view, reader, inventory와 배포 전 stop 조건
- [Active TODO](development-todo.md#protected-environment-execution): 아직 승인·실행되지 않은 protected 작업

Fixture 문서는 개발·CI 재현용입니다. 그 명령이나 결과를 production DB 절차·증거로 사용하지 않습니다.

## 현재 결정과 Git 기록

- [현재 결정 요약](decisions/README.md): 핵심 방향, 상세 authority와 보류 주제
- [ADR 0025](decisions/0025-static-non-rls-first-launch.md): exact first-launch profile
- [ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md): Git-reviewed YAML 단일 authority
- [ADR 0031](decisions/0031-no-pii-curated-view-boundary.md): no-PII curated-view 공개 경계
- [ADR 0032](decisions/0032-reader-temp-admission-relaxation.md): reader `TEMP` admission 경계
- [ADR 0033](decisions/0033-explicit-source-tls-modes.md): source TLS mode와 transport 경계
- [ADR 0034](decisions/0034-source-view-package-and-direct-admission.md): source별 두 파일, view contract marker와 직접 admission
- [검증과 Git 기록 안내](verification/README.md): 현재 gate와 삭제한 과거 문서 조회 방법

현재 tree에는 현행 authority, 실행 가능한 절차와 테스트에 필요한 설명만 둡니다. 과거 ADR,
완료 원장과 날짜별 repository 검증 기록은 Git baseline
`1ff390ab67df215181810a84ac8b2ca8570eceee`에 보존합니다. Git history를 rewrite하지 않으며,
과거 문서의 `Complete`를 현재 지원 상태나 protected 실행 완료로 해석하지 않습니다.
