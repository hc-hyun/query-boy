# Query Man 문서 안내

이 페이지는 `docs/`의 시작점입니다. 모든 문서를 순서대로 읽을 필요는 없습니다.

현재 첫 오픈 기준은 [ADR 0025](decisions/0025-static-non-rls-first-launch.md)입니다.
저장소 구현과 로컬 검증은 끝났고, 실제 대상 환경 전환인 `LAUNCH-02`는 아직 남아 있습니다.
Source·verified query·budget의 현재 authority는 Git-reviewed `config/sources/*.yaml`,
`config/verified-queries.yaml`, `config/budget-profiles.yaml`입니다. 결정 기준은
[ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md)입니다.

## 처음 보는 경우

다음 세 문서만 먼저 읽으면 됩니다.

1. [프로젝트 README](../README.md): 무엇을 하는 제품인지, 로컬에서 어떻게 실행하는지 설명합니다.
2. [용어 사전](glossary.md): 낯선 단어를 쉬운 말로 풉니다.
3. [Architecture](architecture.md): 현재 실행 구조와 여섯 모듈의 관계를 보여줍니다.

코드를 수정하려면 그다음 [활성 개발 지침](development-guidelines.md)의 공통 규칙을 확인하고
[모듈 안내](modules/README.md)에서 담당 모듈 하나를 고릅니다.

Core는 `query_man.source_catalog`, `metadata`, `guarded_query`, `delivery`, `runtime`,
`assurance`의 여섯 physical package로 나뉩니다. 모두
같은 repository·wheel·process에 속하고 package marker는 interface를 re-export하지 않으므로 module
README가 가리키는 leaf path에서 시작합니다.

## 문서 상태 읽는 법

| 표시 | 뜻 | 행동 |
|---|---|---|
| 현재 안내 | 지금 개발·운영할 때 따르는 문서 | 이 문서에서 시작합니다. |
| 상세 참고 | 특정 작업에만 필요한 기술 설명 | 필요할 때만 읽습니다. |
| 보류 | 실제 요구와 승인이 생기기 전까지 일정에 없는 연구 | 구현을 시작하지 않습니다. |
| 기록 | 과거 결정·완료·실행 사실 | 현재 지침으로 해석하거나 소급 수정하지 않습니다. |

현재 source·verified query·budget authority는 Git-reviewed YAML입니다. 변경은 review·test·배포로
반영하고 과거 ADR·verification의 완료 기록은 해당 commit의 사실로만 읽습니다.

## 하고 싶은 일로 찾기

| 하고 싶은 일 | 먼저 읽을 문서 | 필요할 때 추가로 읽을 문서 |
|---|---|---|
| 로컬에서 실행하기 | [프로젝트 README](../README.md) | [로컬·운영 안내](operations.md) |
| 현재 구조 이해하기 | [Architecture](architecture.md) | [모듈 안내](modules/README.md) |
| 코드를 수정하기 | [활성 개발 지침](development-guidelines.md) | [모듈 안내](modules/README.md)와 담당 module README |
| 모듈 하나 수정하기 | [모듈 안내](modules/README.md) | 해당 모듈의 `README.md` |
| 지금 남은 일 확인하기 | [Active TODO](development-todo.md) | [과거 완료 원장](implementation-roadmap.md) |
| 실제 첫 오픈 준비하기 | [Operations](operations.md) | [ADR 0025](decisions/0025-static-non-rls-first-launch.md) |
| 예제 데이터 이해하기 | [첫 오픈 데이터](mvp.md) | [Verified query](verified-queries.md) |
| 새 DB 추가 검토하기 | [Source onboarding 안내](source-onboarding.md) | [현재 체크리스트](source-extension-checklist.md) |
| 느리거나 거부된 query 조사하기 | [Query 제한과 자원](query-cost-control.md) | [Operations](operations.md) |
| 결정 이유 확인하기 | [ADR 안내](decisions/README.md) | 해당 ADR 원문 |
| 과거 실행 결과 확인하기 | [검증 기록 색인](verification/README.md) | 해당 날짜의 기록 원문 |

## 현재 안내

- [Architecture](architecture.md): 현재 static launch 구조, 요청 흐름과 안전 경계
- [Development guidelines](development-guidelines.md): root router와 함께 적용하는 구현·병렬 작업·테스트·handoff 규칙
- [Module index](modules/README.md): 모듈 owner, 허용 의존, 코드·테스트 지도와 승인 규칙
- [Operations](operations.md): 현재 첫 오픈, 로깅, 상태 확인, 로컬 컨테이너와 종료 절차
- [Active TODO](development-todo.md): 실제로 지금 남은 작업
- [Source onboarding](source-onboarding.md): 새 DB 요청을 어느 절차로 보낼지 결정하는 입구
- [Source extension checklist](source-extension-checklist.md): 현재 static launch의 새 DB 검토 기준
- [Query limits](query-cost-control.md): query 제한, 관측값과 조사 순서
- [Resource Server JWT Access Token 검증 계약](resource-server-jwt-auth.md): AuthBridge OAuth bearer 검증과 scope/error/rollout 경계

## 상세 참고

- [MVP data](mvp.md): 두 예제 source의 구조, row 의미와 아홉 가지 검증 질문
- [Verified queries](verified-queries.md): 대표 질문의 결과가 바뀌지 않았는지 검사하는 방법

## 기록과 연구

- [ADR index](decisions/README.md): 현재 결정과 보류된 연구를 구분한 색인
- [Implementation ledger](implementation-roadmap.md): 완료 ID와 증거를 보존하는 과거 원장
- [Verification evidence](verification/README.md): 실행 시점별 immutable record
- [Module boundary decision guide](module-boundary-decision-guide.md): 이미 승인·완료된 모듈 경계 선택의 frozen record
- [Source onboarding Skill plan](source-onboarding-skill-plan.md): plan-only Skill을 만든 당시 설계 기록

기록 문서의 `Complete`는 그 문서에 적힌 commit·환경·범위만 뜻합니다. 현재 지원 상태는 이 문서,
[Architecture](architecture.md), [Active TODO](development-todo.md), accepted ADR과 실행 가능한 테스트를
함께 확인해야 합니다.
