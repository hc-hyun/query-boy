# Active Development TODO

Status: Active — `DBENV-01`, `AUTHENV-01` 완료 후 `LAUNCH-02`

이 문서는 실제로 지금 남은 일만 보여줍니다. 일정에 없는 주제는 checkbox 없이 아래 표에만
요약합니다. 완료 이력과 과거 상세는 Git history에서 찾습니다.

## 현재 상태

[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 `LAUNCH-01-A` repository 구현과 local
acceptance는 완료됐습니다.
[ADR 0034](decisions/0034-source-view-package-and-direct-admission.md)에 따라 source authority는 source별
`source.yaml`과 `views.sql` package입니다. Reviewed package 집합이 startup inventory라는 기준은
[ADR 0035](decisions/0035-reviewed-source-package-inventory.md), budget은 ADR 0030의 Git-reviewed YAML을
따릅니다.
[ADR 0031](decisions/0031-no-pii-curated-view-boundary.md)에 따라 source data plane은 개인정보
탐지·분류·마스킹 기능을 제공하지 않으며, DB owner가 개인정보를 제거했다고 확인한 curated view만
source로 받습니다.

| 항목 | 현재 값 |
|---|---|
| Source | Approved Git revision의 reviewed `config/sources/` package 전체 |
| Runtime | 단일 Query Man replica |
| Source authority | Git-reviewed source package와 budget YAML |
| 개인정보 경계 | DB-owner-confirmed no-PII curated view |
| Database | PostgreSQL 18, server/client UTF-8 |
| Protected DB binding | 미실행; 현재 manifest는 development/loopback 기본값 |
| Protected authentication binding | 미실행; 실제 환경 authority 미확정(AuthBridge 선택 시 mapper·CA도 미확정) |
| RLS | 전면 차단 |
| 결과 type | OID `20, 21, 23, 25, 1082, 1184, 1700` |
| SQL policy | v3와 exact seven result OID |

Repository implementation과 local acceptance는 protected environment 전환 권한이 아니다.
DB adapter와 authentication verifier/policy 코드가 있다는 것은 실제 DB endpoint, reader secret 또는
authentication authority가 환경에 연결됐다는 뜻이 아니다. 코드와 로컬 검사가 끝났어도 실제 운영
서버에 배포해도 된다는 승인도 아직 없습니다.

## Protected Environment Execution

- [ ] `DBENV-01`: 승인된 source profile을 실제 protected DB 환경에 연결하고 검증합니다.
- [ ] `AUTHENV-01`: 실제 환경의 authentication authority 하나를 연결하고 검증합니다.
- [ ] `LAUNCH-02`: 위 두 환경 연결이 완료된 artifact를 배포하고 traffic을 전환합니다.

작업 순서는 다음과 같습니다.

```text
DBENV-01 ─┐
           ├─> LAUNCH-02
AUTHENV-01 ┘
```

`DBENV-01`과 `AUTHENV-01`은 이미 구현된 adapter를 실제 환경에 binding하는 선행 작업이다.
`LAUNCH-02`는 이를 전제로 한 배포·acceptance·cutover이며 DB role/view, source inventory,
authentication mapper 또는 application code를 현장에서 새로 구현하지 않는다. 필요한 capability나
versioned configuration이 없으면 해당 작업을 중단하고 별도 repository change set으로 돌아간다.

### DBENV-01: 실제 DB 환경 연결

Protected binding 전에는 approved revision의 모든 source manifest에서 environment, endpoint와 TLS를
확인한다. Development fixture manifest는 loopback host와 `sslmode=disable`을 사용할 수 있다.
Host·port·password는 환경변수로 resolve한다. Database와 reader user, required TLS mode(`disable`,
`require`, `verify-full`), allowed schema/relation kind와 semantic overlay는 reviewed manifest에 고정된다.
`prefer`, `allow`, `verify-ca`와 mode 생략은 거부한다. `require`의 no-plaintext·no-hostname-verification
경계와 CA/SAN 개선 조건은 [ADR 0033](decisions/0033-explicit-source-tls-modes.md)을 따른다. Source는 TCP
endpoint만 허용하고 pool은 `gssencmode=disable`로 reviewed TLS mode를 고정하며 SQL 전에 실제 TLS
state를 확인한다. Exact 공개 relation
set은 승인된 DB DDL inventory와 metadata revision으로 검증한다.

1. 대상 host·port·database·reader user, network path, TLS/CA, secret owner와 DBA를 확정한다.
2. DBA가 no-PII curated view, 최소 권한 reader role/grant, PostgreSQL 18/UTF-8과 RLS 0건을 준비한다.
3. Secret은 Git/YAML/image/log가 아닌 승인된 외부 store에서 주입한다.
4. Source·DDL·view/function/operator/type/collation/extension, role/grant와 DB semantic setting을 승인
   inventory와 대조하고 traffic 밖에서 marker/source/version, exact view privilege, Metadata direct
   admission, reader policy와 security/integration probe를 통과시킨다.
5. Source checkout에서 검증했다면 exact Git commit과 clean/reviewed config provenance를 기록한다.
   Application image를 사용했다면 OCI revision label이 그 commit과 일치하는 image digest를 기록한다.

실제 대상이 approved package의 database/user/view/TLS 의미와 다르거나 새 database/source가 필요하면
`DBENV-01`에서 manifest를 즉석 수정하지 않는다. [Source onboarding](source-extension-checklist.md)에 따라
두 파일 package 변경을 review한 뒤 이 작업을 다시 시작한다. Public view
definition/output/security/grant가 달라지면 DB 현장에서 즉석 수정하지 않고
source package의 `view_contract_version`, 모든 view marker와 `views.sql` 변경을 별도 승인·검증한다.

### AUTHENV-01: 실제 인증 환경 연결

JWT Discovery/JWKS, signature, issuer, audience, time, scope/role과 401/403 검증 코드는 구현돼 있다.
이 작업은 인증 코드를 새로 만드는 것이 아니라 실제 환경의 authority와 Query Man 설정을 연결한다.

1. OAuth, access-policy 또는 opaque API token 중 authority를 정확히 하나 선택한다.
2. 모든 mode에서 non-secret configuration, secret/policy owner, 선택한 mode가 지원하는 permission의
   성공, 잘못된 credential의 401과 credential 비로깅을 traffic 밖에서 확인한다. 선택한 mode가
   표현하는 권한 제한은 거부되는 요청의 403도 확인한다. Legacy API token에 operator 성공을 요구하지
   않는다.
3. AuthBridge를 선택하면 exact issuer, Query Man 전용 audience와 query/MCP/operator scope mapper,
   optional role/group, CA trust를 준비한다. Access token 취득·refresh는 Codex MCP client 또는 company
   helper의 owner를 지정하며 Query Man은 client secret이나 refresh token을 저장하지 않는다.
4. AuthBridge mode에서는 정상 access token과 위조·만료·다른 audience·ID/refresh token 거부,
   unknown `kid`와 signing-key rotation을 실제 Discovery/JWKS에서 추가 확인한다.
5. Source checkout에서 검증했다면 exact Git commit과 clean/reviewed config provenance를 기록한다.
   Application image를 사용했다면 OCI revision label이 그 commit과 일치하는 image digest를 기록한다.

실제 provider가 요구하는 authentication capability가 현재 구현에 없으면 mapper나 code를 현장에서
우회하지 않고 별도 repository change와 acceptance를 승인받는다. OAuth와 diagnostic capture를 함께
요구하는 환경도 현재 configuration contract 밖이므로 별도 설계 승인 전에는 중단한다.

### LAUNCH-02: 배포와 traffic 전환

`DBENV-01`과 `AUTHENV-01`의 exact inventory와 evidence가 완료되고 이후 변경되지 않은 상태에서만
시작한다. Launch artifact의 commit/config/image digest가 선행 검증 artifact와 다르면 영향을 받은
선행 acceptance를 다시 실행한다.

1. 승인된 Git commit, OCI revision label이 그 commit과 일치하는 application image digest와 upstream
   image digest를 고정한다.
2. TLS, secret, backup, 직전 image/config/SQL policy와 rollback route를 재확인한다.
3. Accepted 단일 replica를 traffic 밖에서 시작하고 exact readiness, approved package inventory 전체,
   RLS 0건, PostgreSQL 18/UTF-8, view marker/권한, Metadata와 SQL revision, seven-OID corpus,
   security/integration과 인증 acceptance를 다시 확인한다.
4. Old route를 닫고 신규 유입·active query·source connection을 drain한 뒤 accepted replica만 route한다.
5. 오류·resource·DB connection을 관찰하고 실행 결과와 rollback 가능 상태를 environment evidence로
   남긴다.

공통 검증 항목과 순서는 [Operations](operations.md#static-non-rls-first-launch)를 따릅니다. 대상별
deploy/probe 명령은 protected change record에서 exact 값으로 고정합니다. Repository fixture나 local
container 결과를 실제 환경 증거로 대신하지 않습니다.

## 시작 전에 필요한 승인

각 작업은 다음 내용을 특정한 protected-operation 실행 승인이 필요합니다.

- `DBENV-01`: 대상 DB와 접근 방법, DBA·secret/change-record owner, DDL/role/settings inventory,
  TLS·backup, probe, 중단·복구 조건
- `AUTHENV-01`: 선택한 단일 authority, 실행자/owner, 지원 permission과 negative test, credential 취급,
  중단·복구 조건. AuthBridge이면 IAM 실행자, audience/scope/optional role·group mapper, CA와 client
  token 취득·refresh owner
- `LAUNCH-02`: 승인된 Git commit과 application/upstream image digest, 배포 실행자, route·관찰 방법,
  직전 artifact와 rollback 순서, change-record owner

하나의 change record에 묶더라도 세 작업의 scope, access, target과 stop condition을 각각 구분한다.
Repository 문서나 한 작업의 procedure를 승인한 것만으로 다른 protected action까지 승인된 것은 아닙니다.

## 완료 조건

- `DBENV-01`: 승인 inventory와 실제 DB가 일치하고, 모든 source의 no-PII view·reader·TLS/secret·PG18/UTF-8·
  RLS 0건, marker/source/version, exact owner/ACL, dynamic column과 metadata/reader probe를 통과함.
  해당 artifact와 결과를 environment evidence로 남김
- `AUTHENV-01`: authority가 하나뿐이고 선택 mode가 지원하는 permission 성공, 해당하는 권한 거부 403,
  잘못된 credential 401과 비로깅을 통과함. AuthBridge를 선택하면 실제 positive/negative token·scope·
  key rotation, CA/client owner와 검증 artifact도 environment evidence로 남김
- `LAUNCH-02`: 승인한 exact artifact가 배포되고 `/ready`, marker/revision/safety probe, 현재 SQL policy와
  DB/auth 재검증이 통과하며, traffic 뒤 오류·resource·connection 상태와 rollback 가능 상태를
  immutable environment evidence로 남김

## 즉시 중단할 조건

- `DBENV-01` 또는 `AUTHENV-01`이 미완료이거나 완료 뒤 inventory가 변경됨
- 실제 대상이 current source inventory와 달라 새 source/config/code 변경이 필요함
- Source, RLS, DDL, role, DB 설정 또는 image가 승인 inventory와 다름
- Marker/source/version, view definition/owner/ACL 또는 metadata revision이 승인 inventory와 다름
- Readiness가 `degraded` 또는 `unavailable`
- 지원하지 않는 결과 type이 노출됨
- SQL policy v2와 v3 process가 동시에 요청을 받음
- Backup, rollback, 실행 책임이나 secret 취급이 불명확함
- Authentication authority가 둘 이상 설정됐거나 AuthBridge audience/scope/CA/refresh 책임이 불명확함
- OAuth와 diagnostic capture를 동시에 요구함

중단 후 LAUNCH 작업 안에서 DB DDL, source YAML, mapper나 application code를 즉석 변경해 baseline을
넓히지 않습니다. 원인을 정리하고 해당 선행 작업 또는 repository change의 범위와 영향을 다시
승인받습니다.

## 현재 일정에 없는 일

다음 주제는 active queue가 아닙니다. 일반적인 “이어서 구현”, refactor나 문서 정리는 이 작업들의
시작 승인이 아닙니다.

| ID | 주제 | 현재 상태와 다시 시작하는 조건 |
|---|---|---|
| `RLS-01`~`RLS-03` | RLS serving | 모든 RLS source를 DB 접근 전에 차단합니다. 실제 source 요구와 recursive policy/dependency attestation, migration, cross-tenant acceptance와 protected cutover의 정확한 승인이 필요합니다. |
| `ENC-01`~`ENC-02` | Result type 확대 | OID `20, 21, 23, 25, 1082, 1184, 1700`만 허용합니다. 이 범위로 답할 수 없는 실제 질문과 lossless encoding, SQL policy v4+, migration·rollback 승인이 필요합니다. |
| `DBAUTH-01`~`DBAUTH-03` | DB-backed source authority | Git-reviewed source package와 budget만 authority입니다. 새 authority·persisted format·credential/admin 경계, explicit import, dual-authority 없는 cutover와 backup/rollback을 새로 승인해야 합니다. 과거 managed code를 암묵적으로 복원하지 않습니다. |
| `COST-01`~`COST-05` | DB-native 비용·경보 | Query resource limit는 이미 강제하지만 통화 비용·authoritative usage collector는 없습니다. 실제 운영 요구와 monitoring 권한·retention·aggregate 의미를 승인하고 base evidence가 생긴 뒤 alert threshold를 별도 결정합니다. |
| `TRACE-01`~`TRACE-04` | Workflow trace | 현재 request/MCP/query ID로 부족한 실제 correlation 요구와 header trust, redaction·cardinality, retry/disconnect acceptance 범위를 승인해야 합니다. |

공통으로 prompt, Skill 또는 caller 관례가 authorization, SQL validation, reader privilege나 resource
limit을 대신할 수 없습니다. 실제 요구 없이 chargeback, distributed global quota와 management RBAC를
미리 만들지 않습니다.

## 관리 규칙

- 작업은 [module index](modules/README.md)와 primary module README에서 시작합니다.
- 한 agent는 지정된 module과 file allowlist만 수정하고 shared file·Git은 coordinator가 관리합니다.
- Internal Python interface는 provider/consumer를 함께 수정·검증할 수 있습니다. External/persisted/
  policy/lifecycle/ownership/procedure 의미는 정확한 사용자 승인 없이 바꾸지 않습니다.
- 완료한 ID는 이 파일에서 제거하고 exact commit/PR/CI provenance로 남깁니다. 현재 운영에 필요한
  결과만 owner 문서에 반영하고 날짜별 완료 원장을 새로 만들지 않습니다.
- Protected environment evidence/change record는 승인된 기록 시스템에 append-only/immutable하게
  보존합니다. Repository의 과거 서술 문서는 archive baseline을 남긴 뒤 current tree에서 정리할 수
  있지만 Git history를 rewrite하지 않습니다.
- 최소 repository gate는 Ruff, mypy와 full pytest입니다. DB·release 경계는 관련 security,
  integration, container, bounded load와 soak까지 실행합니다.
