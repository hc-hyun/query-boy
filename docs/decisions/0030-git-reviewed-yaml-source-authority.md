# ADR 0030: Git-Reviewed YAML Source Authority

Status: Accepted

Date: 2026-08-29

Decision ID: `QB-YAML-SOURCE-AUTHORITY-20260829`

Baseline: `7b4e717c7775ff262c716d36f6f172aadc162892`

## Context

Query Man은 static YAML source와 별도로 Control DB 기반 managed source lifecycle을 구현해 보존해
왔다. Managed 경로에는 별도 persistence, encryption, admin mutation API, hot reload, replica
convergence, resource observation, recovery fixture와 운영 절차가 필요했다. 현재 운영 요구는 GitHub
Enterprise review를 거친 사람이 읽을 수 있는 YAML이면 충분하며, 두 authority를 함께 유지하는
복잡도가 협업과 변경 판단을 어렵게 한다.

PostgreSQL catalog의 physical type, precision/scale와 table/column comment는 source metadata를
풍부하게 하는 입력이다. 그러나 이 정보가 source 등록 authority, credential store 또는 PII 접근
허가가 되지는 않는다. SQL AST validation, allowlist, 최소 권한 reader, read-only transaction,
timeout/concurrency/row/byte limit, cancel/rollback과 drift fail-closed도 authority 단순화와 무관하게
계속 강제해야 한다.

## Decision

### One source authority

- Git review를 거친 `config/sources/*.yaml`이 source definition의 유일한 authority다.
- `config/verified-queries.yaml`과 `config/budget-profiles.yaml`도 각각 verified query와 resource
  budget의 유일한 authority다. Runtime은 release artifact에 포함된 이 파일들을 startup에 strict
  validation하고, 서로 다른 authority를 merge하거나 장애 시 fallback하지 않는다.
- 새 source와 source definition 변경은 정확한 YAML diff, external-secret wiring, traffic-off
  acceptance, 사용자 승인, repository review, 배포와 rollback 계획으로 진행한다.
- Credential 값과 complete DSN은 Git, plan, log, metadata comment에 저장하지 않는다. YAML에는 기존
  외부 secret 경계가 해석할 식별자만 둔다.
- PostgreSQL comment는 relation grain, column business meaning, null/derivation, semantic unit/scale와
  PII 검토 상태를 설명할 수 있다. Physical type과 precision/scale은 PostgreSQL catalog 사실로
  수집하며 prose에 중복하지 않는다. Comment는 untrusted text이고 allowlist, masking, grant 또는 PII
  노출 승인을 대체하지 않는다.

### Retired managed capability

다음 capability와 그것만을 위한 코드·fixture·절차를 repository에서 제거한다.

- `query_man.managed` package와 Control DB schema/migration/store
- source admin read/mutation routes, receipt와 lifecycle history
- managed publish/rollback/deactivate, runtime polling과 hot reload
- managed replica convergence, resource/gateway observation과 reporter
- Control DB backup/restore drill과 managed acceptance Compose overlay

Runtime과 CLI는 managed mode를 선택하거나 Control DB에 연결하지 않는다. `qm source`는 local YAML의
`list`, `show`, `validate`만 제공한다. 다음 retired environment setting 중 하나라도 존재하면 값을
읽거나 표시하지 않고 startup을 fail-closed한다.

- `QUERY_MAN_SOURCE_MODE`
- `QUERY_MAN_CONTROL_DSN`
- `QUERY_MAN_SOURCE_ENCRYPTION_KEY`
- `QUERY_MAN_REPLICA_ID`
- `QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS`

이 변경은 repository code와 deployment contract를 정리하는 작업이다. 실제 Control DB를 drop하거나
data를 mutation하는 작업을 수행하지 않는다. 어떤 live Control DB에도
접속하지 않고, database/schema/table/row, credential, secret 또는 backup을 drop, delete, migrate,
decrypt하거나 수정하지 않는다. 남아 있는 외부 Control DB의 보존·폐기는 해당 환경의 별도 inventory,
retention, 접근 범위, target, rollback과 실행 승인을 갖춘 운영 작업이다.

### Authentication boundary

Source authority 변경은 [ADR 0029](0029-authbridge-resource-server-jwt.md)의 **Resource Server JWT
Access Token 검증 계약**을 바꾸지 않는다. OAuth mode는 issuer Discovery의 `jwks_uri`에서 JWKS를
받아 cache하고, 허용 알고리즘, 서명, exact issuer, service audience, expiry/nbf와 endpoint별
scope/role/group을 검증한다. API 인증에는 access token만 받고 ID token과 refresh token을 받지
않는다. Refresh와 token acquisition은 client/helper 책임이며 service에는 client secret이 필요하지
않다.

## Compatibility and supersession

- HTTP/MCP data surface, source manifest의 현재 version, metadata/SQL revision 의미, reader policy와
  query safety contract는 유지한다.
- Managed admin HTTP wire와 Control persisted format은 더 이상 supported interface/format이 아니다.
  Retired endpoint나 setting을 bootstrap/YAML 동작으로 조용히 해석하지 않는다.
- 이 ADR은 ADR 0012, 0013, 0016과 ADR 0025의 managed authority 보존·활성화 조항을 현재 동작에
  한해 supersede한다. 해당 ADR과 dated verification은 당시 사실을 보존하는 immutable history다.
- ADR 0025의 두-source non-RLS launch, PostgreSQL/encoding/result-OID/RLS 격리와 protected execution
  gate는 이 결정으로 확대되지 않는다.

## Change and rollback

Repository 변경은 managed-only package, migration, fixture, route, configuration과 procedure를 함께
제거하고 active 문서·test를 YAML authority와 맞춘다. Historical ADR, dated evidence와 기존
roadmap row는 변경하지 않고 새 완료 기록을 append한다.

Rollback은 승인된 이전 Git commit/artifact로 전체 application을 되돌리고, 그 artifact가 요구하는
설정과 외부 Control DB의 사용 가능성을 별도 검증한 뒤 재배포하는 것이다. 새 YAML만 되돌리려면
reviewed revert를 배포한다. Runtime fallback, repository에서의 live DB 복원 또는 자동 data migration은
rollback이 아니다.

향후 DB-backed source authority가 실제로 필요해지면 기존 managed code를 암묵적으로 되살리지 않는다.
새 ADR에서 authority, persisted/versioned format, credential boundary, migration/import, compatibility,
backup/restore, cutover와 rollback을 다시 결정하고 사용자 승인과 protected operational approval을
각각 받아 구현한다.

## Verification

- YAML source, verified-query와 budget strict validation 및 existing source query regression
- Retired managed package, admin routes, Control migration/fixture와 runtime composition 부재
- Retired environment setting의 value-safe fail-closed behavior
- `qm source list/show/validate`가 local YAML만 읽고 mutation하지 않는 동작
- Source comment/type/precision-scale/PII guidance와 secret non-disclosure boundary
- Ruff, mypy, full pytest와 applicable static PostgreSQL integration acceptance
