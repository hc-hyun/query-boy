# Active Development TODO

Status: Active — 현재 launch 작업은 `LAUNCH-02` 하나

이 문서는 실제로 지금 남은 일만 보여줍니다. 일정에 없는 주제는 checkbox 없이 아래 표에만
요약합니다. 완료 이력과 과거 상세는 Git history에서 찾습니다.

## 현재 상태

[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A` repository 구현과 local
acceptance는 완료됐습니다.
[ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md)에 따라 source·verified-query·budget의
유일한 authority는 Git-reviewed YAML입니다.

| 항목 | 현재 값 |
|---|---|
| Source | `development-issues`, `market-voc` |
| Runtime | 단일 Query Man replica |
| Source authority | Git-reviewed source·verified-query·budget YAML |
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
3. Authentication authority를 정합니다. AuthBridge를 선택하면 exact issuer, Query Man 전용
   audience/scope mapper, CA trust와 client token refresh owner를 확인합니다.
4. Source·DDL·reader role·PostgreSQL 설정·RLS 0건과 배포 image digest를 승인 inventory와 대조합니다.
5. Traffic을 받기 전에 readiness와 아홉 verified query를 확인하고, route 뒤 오류·사용량·DB 연결을
   관찰합니다.

정확한 명령, 기록 항목과 순서는 [Operations](operations.md#static-non-rls-first-launch)를 따릅니다.
Repository fixture나 local container 결과를 실제 환경 증거로 대신하지 않습니다.

## 시작 전에 필요한 승인

다음 내용이 들어간 실행 승인이 필요합니다.

- 대상 환경과 접근 방법
- 실행자와 change-record owner
- 승인된 Git commit과 application/upstream image digest
- TLS, secret, backup과 복구 확인 방법
- 선택한 authentication authority; AuthBridge이면 audience/scope mapper, CA와 token refresh owner
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
- AuthBridge 선택 시 access token 성공, ID/refresh/다른 audience/만료 token 거부, query/MCP/operator
  scope와 signing-key rotation이 traffic 밖에서 통과함
- Traffic 전환 뒤 오류·resource·connection 상태가 정상임
- 실제 실행 결과, 담당자와 rollback 가능 상태를 immutable environment evidence로 남김

## 즉시 중단할 조건

- Source, RLS, DDL, role, DB 설정 또는 image가 승인 inventory와 다름
- Metadata revision이나 verified result hash가 다름
- Readiness가 `degraded` 또는 `unavailable`
- 지원하지 않는 결과 type이 노출됨
- SQL policy v2와 v3 process가 동시에 요청을 받음
- Backup, rollback, 실행 책임이나 secret 취급이 불명확함
- Authentication authority가 둘 이상 설정됐거나 AuthBridge audience/scope/CA/refresh 책임이 불명확함

중단 후 임의로 baseline을 넓히지 않습니다. 원인을 정리하고 변경 범위와 영향을 다시 승인받습니다.

## 현재 일정에 없는 일

다음 주제는 active queue가 아닙니다. 일반적인 “이어서 구현”, refactor나 문서 정리는 이 작업들의
시작 승인이 아닙니다.

| ID | 주제 | 현재 상태와 다시 시작하는 조건 |
|---|---|---|
| `RLS-01`~`RLS-03` | RLS serving | 모든 RLS source를 DB 접근 전에 차단합니다. 실제 source 요구와 recursive policy/dependency attestation, migration, cross-tenant acceptance와 protected cutover의 정확한 승인이 필요합니다. |
| `ENC-01`~`ENC-02` | Result type 확대 | OID `20, 21, 23, 25, 1082, 1184, 1700`만 허용합니다. 이 범위로 답할 수 없는 실제 질문과 lossless encoding, SQL policy v4+, verified migration·rollback 승인이 필요합니다. |
| `DBAUTH-01`~`DBAUTH-03` | DB-backed source authority | Git-reviewed YAML만 authority입니다. 새 authority·persisted format·credential/admin 경계, explicit import, dual-authority 없는 cutover와 backup/rollback을 새로 승인해야 합니다. 과거 managed code를 암묵적으로 복원하지 않습니다. |
| `COST-01`~`COST-05` | DB-native 비용·경보 | Query resource limit는 이미 강제하지만 통화 비용·authoritative usage collector는 없습니다. 실제 운영 요구와 monitoring 권한·retention·aggregate 의미를 승인하고 base evidence가 생긴 뒤 alert threshold를 별도 결정합니다. |
| `TRACE-01`~`TRACE-04` | Workflow trace | 현재 request/MCP/query ID로 부족한 실제 correlation 요구와 header trust, redaction·cardinality, retry/disconnect acceptance 범위를 승인해야 합니다. |

공통으로 prompt, Skill 또는 caller 관례가 authorization, SQL validation, reader privilege나 resource
limit을 대신할 수 없습니다. 실제 요구 없이 chargeback, distributed global quota와 management RBAC를
미리 만들지 않습니다.

## 관리 규칙

- 작업은 [module index](modules/README.md)와 primary module README에서 시작합니다.
- 한 agent는 지정된 module과 file allowlist만 수정하고 shared file·Git은 coordinator가 관리합니다.
- Module interface나 external/persisted/policy/lifecycle/procedure 의미는 정확한 사용자 승인 없이
  바꾸지 않습니다.
- 완료한 ID는 이 파일에서 제거하고 exact commit/PR/CI provenance로 남깁니다. 현재 운영에 필요한
  결과만 owner 문서에 반영하고 날짜별 완료 원장을 새로 만들지 않습니다.
- Protected environment evidence/change record는 승인된 기록 시스템에 append-only/immutable하게
  보존합니다. Repository의 과거 서술 문서는 archive baseline을 남긴 뒤 current tree에서 정리할 수
  있지만 Git history를 rewrite하지 않습니다.
- 최소 repository gate는 Ruff, mypy와 full pytest입니다. DB·release 경계는 관련 integration과
  container·verified-query acceptance까지 실행합니다.
