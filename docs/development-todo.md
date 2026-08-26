# Active Development TODO

Status: Active — 현재 launch 작업은 `LAUNCH-02` 하나

이 문서는 실제로 지금 남은 일만 보여줍니다. 일정에 없는 RLS, 결과 type, 비용·경보와 trace 연구는
[future work](future-work.md)에 분리했습니다. 완료 이력은
[implementation ledger](implementation-roadmap.md)에 보존합니다.

## 현재 상태

[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A` repository 구현과 local
acceptance는 완료됐습니다.

| 항목 | 현재 값 |
|---|---|
| Source | `development-issues`, `market-voc` |
| Runtime | 단일 Query Man replica |
| Managed | `query_man.managed` package와 별도 acceptance lane에 보존; static composition에는 미참여 |
| Database | PostgreSQL 18, server/client UTF-8 |
| RLS | 전면 차단 |
| 결과 type | OID `20, 21, 23, 25, 1082, 1184, 1700` |
| SQL policy | v3와 아홉 verified query |

Repository implementation과 local acceptance는 protected environment 전환 권한이 아니다.
쉽게 말하면 코드와 로컬 검사가 끝났어도 실제 운영 서버에 배포해도 된다는 승인은 아직 없습니다.

## Protected Environment Execution

- [ ] `LAUNCH-02`: 대상 환경에서 첫 오픈을 준비하고 승인·실행합니다.

해야 할 일을 네 단계로 줄이면 다음과 같습니다.

1. 대상 서버·DB, 접근 권한과 변경 기록 책임자를 확정합니다.
2. TLS, secret, backup과 직전 version rollback을 준비합니다.
3. Source·DDL·reader role·PostgreSQL 설정·RLS 0건과 배포 image digest를 승인 inventory와 대조합니다.
4. Traffic을 받기 전에 readiness와 아홉 verified query를 확인하고, route 뒤 오류·사용량·DB 연결을
   관찰합니다.

정확한 명령, 기록 항목과 순서는 [Operations](operations.md#static-non-rls-first-launch)를 따릅니다.
Repository fixture나 local container 결과를 실제 환경 증거로 대신하지 않습니다.

## 시작 전에 필요한 승인

다음 내용이 들어간 실행 승인이 필요합니다.

- 대상 환경과 접근 방법
- 실행자와 change-record owner
- 승인된 Git commit과 application/upstream image digest
- TLS, secret, backup과 복구 확인 방법
- Source·DB 설정 inventory
- Traffic route와 관찰 방법
- 즉시 중단할 조건과 rollback 순서

Repository 문서나 procedure를 승인한 것만으로 실제 protected action까지 승인된 것은 아닙니다.

## 완료 조건

다음이 모두 충족돼야 `LAUNCH-02`를 완료로 옮길 수 있습니다.

- 승인한 exact artifact와 설정이 대상 환경에 배포됨
- 두 source만 보이고 RLS source가 없음
- `/ready`가 exact ready이며 PostgreSQL 18/UTF-8 검사가 통과함
- 아홉 verified query와 현재 SQL policy가 통과함
- Traffic 전환 뒤 오류·resource·connection 상태가 정상임
- 실제 실행 결과, 담당자와 rollback 가능 상태를 immutable environment evidence로 남김

## 즉시 중단할 조건

- Source, RLS, DDL, role, DB 설정 또는 image가 승인 inventory와 다름
- Metadata revision이나 verified result hash가 다름
- Readiness가 `degraded` 또는 `unavailable`
- 지원하지 않는 결과 type이 노출됨
- SQL policy v2와 v3 process가 동시에 요청을 받음
- Backup, rollback, 실행 책임이나 secret 취급이 불명확함

중단 후 임의로 baseline을 넓히지 않습니다. 원인을 정리하고 변경 범위와 영향을 다시 승인받습니다.

## 현재 일정에 없는 일

다음 주제는 조사 기록만 있고 active queue가 아닙니다.

| 주제 | 상태 | 다시 시작하는 조건 |
|---|---|---|
| RLS serving | Parked | 실제 RLS source 요구와 attestation·migration 승인 |
| 결과 type 확대 | Parked | 일곱 OID로 답할 수 없는 실제 질문 |
| Managed canonical-time cutover | Parked | 실제 managed environment 전환 |
| DB-native 비용·경보 | Parked | 운영 의사결정에 필요한 aggregate와 권한 승인 |
| Workflow trace | Parked | 현재 ID로 부족한 실제 correlation 요구 |

ID와 상세 시작 조건은 [future work](future-work.md)에서 확인합니다. 일반적인 “이어서 구현”이나
문서 정리는 이 작업들의 시작 승인이 아닙니다.

## 관리 규칙

- 작업은 [module index](modules/README.md)와 primary module README에서 시작합니다.
- 한 agent는 지정된 module과 file allowlist만 수정하고 shared file·Git은 coordinator가 관리합니다.
- Module interface나 external/persisted/policy/lifecycle/procedure 의미는 정확한 사용자 승인 없이
  바꾸지 않습니다.
- 완료한 ID는 이 파일에서 제거하고 evidence와 함께 implementation ledger로 옮깁니다.
- 과거 verification record와 stored row를 현재 의미에 맞춰 수정·삭제하지 않습니다.
- 최소 repository gate는 Ruff, mypy와 full pytest입니다. DB·release 경계는 관련 integration과
  container·verified-query acceptance까지 실행합니다.
