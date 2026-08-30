# 현재 결정과 방향

Status: Current — 현행 authority와 핵심 설계 방향의 압축본

현재 tree에는 모든 구현 단계를 ADR로 남기지 않습니다. 현행 authority와 owner README의 요약만으로
안전하게 대체할 수 없는 세부 계약만 원문으로 유지하고, 나머지 방향은 이 문서와 owner 문서에
둡니다.

## 현행 authority와 policy

| 결정 | 정하는 범위 |
|---|---|
| [ADR 0025](0025-static-non-rls-first-launch.md) | 두 source, 단일 replica, PostgreSQL 18/UTF-8, RLS 차단, 일곱 result OID, SQL policy v3와 protected launch gate |
| [ADR 0030](0030-git-reviewed-yaml-source-authority.md) | `config/sources/*.yaml`, `config/verified-queries.yaml`, `config/budget-profiles.yaml`의 Git-reviewed 단일 authority와 retired managed capability |
| [ADR 0031](0031-no-pii-curated-view-boundary.md) | DB owner가 개인정보를 제거했다고 확인한 reviewed curated view만 제공하고 Query Man은 PII를 탐지·분류·마스킹하지 않는 공개 경계 |
| [ADR 0032](0032-reader-temp-admission-relaxation.md) | Database `TEMP` 보유는 reader admission 조건이 아니며 사용자 SQL의 temporary relation·DDL 차단은 유지하는 경계 |

ADR 0025와 ADR 0030이 겹치면 ADR 0030의 source-authority supersession을 적용하고 ADR 0025의 좁은
serving·safety·launch gate는 유지합니다. 실제 active 작업은
[Active TODO](../development-todo.md)만 기준으로 삼습니다.

## 현행 세부 계약

아래 문서는 방향을 정하는 roadmap이 아니라 현재 구현이 지켜야 하는 정확한 보안·wire·lifecycle
계약입니다. 변경할 때만 관련 문서를 읽습니다.

| 계약 | 읽는 경우 |
|---|---|
| [ADR 0001](0001-postgresql-ast-validation.md) | PostgreSQL AST grammar, function/operator/cast와 fingerprint 경계를 바꿀 때. Reader `TEMP` 부재 전제는 ADR 0032가 대체 |
| [ADR 0002](0002-guarded-query-contract.md) | Query success/error, admission, result byte, cancel·rollback 의미를 바꿀 때 |
| [ADR 0003](0003-reader-and-resolved-object-policy.md) | Reader/view-owner 권한, session policy와 DB-resolved object 검사를 바꿀 때. Database `TEMP` admission 요건은 ADR 0032가 대체 |
| [ADR 0006](0006-mcp-transport-and-workflow.md) | MCP protocol/version/tool schema, validation error와 retry 경계를 바꿀 때 |
| [ADR 0027](0027-consent-gated-diagnostic-capture.md) | Consent, encrypted persisted format, privacy·TTL·fail-open lifecycle을 바꿀 때 |
| [ADR 0031](0031-no-pii-curated-view-boundary.md) | 개인정보 공개 책임과 no-PII curated-view admission 경계를 바꿀 때 |
| [ADR 0032](0032-reader-temp-admission-relaxation.md) | Reader database `TEMP` admission과 temporary-object 안전 근거를 바꿀 때 |

## 핵심 방향

### 구조와 소유권

- Query Man은 하나의 repository·wheel·process인 modular monolith입니다.
- Source Catalog, Metadata, Guarded Query, Delivery, Runtime, Assurance 여섯 physical package를
  사용하고 marker-only `__init__.py`에서 interface를 재수출하지 않습니다.
- 정확한 owner, allowed dependency, module interface와 승인 분류는
  [module index](../modules/README.md)와 각 module README가 canonical source입니다.
- Production composition은 Runtime, offline acceptance composition은 Assurance CLI만 소유합니다.

### Source와 metadata

- Source definition, verified query와 budget은 Git-reviewed YAML만 authority로 사용합니다. Runtime
  mutation, Control DB, hot reload 또는 fallback authority는 없습니다.
- PostgreSQL catalog의 type·precision·scale은 사실로 수집하고 comment는 비신뢰 설명 데이터로
  취급합니다. Query Man은 개인정보를 탐지·분류·마스킹하거나 column 단위로 인가하지 않습니다.
  DB owner가 개인정보를 제거했다고 확인한 reviewed curated view만 등록하며 불명확하면 중단합니다.
- Metadata는 immutable revision으로 발행하고 질문별 context를 bounded selection합니다. Client가
  낡은 metadata/SQL-policy revision을 보내면 실행 전에 fail-closed합니다.
- Source DDL, view/function/operator/type/collation/extension과 semantic DB setting은 serving 중
  동결합니다. 현재 revision이 privileged DBA drift를 모두 증명하지 못하므로 설명되지 않은 변화는
  route 중단 조건입니다.
- 허용 OID로 cast·derive한 값도 column/hidden-view collation, same-OID function body, operator의
  transitive function, `standard_conforming_strings`·`transform_null_equals`·`array_nulls`·
  `timezone_abbreviations`·`bytea_output`·`default_text_search_config` 또는 planner-order-sensitive
  float/JSONB aggregate 때문에 같은 revision에서 달라질 수 있습니다. 현재 대응은 승인 inventory와
  freeze이며 이를 full attestation으로 과장하지 않습니다. Passing characterization은
  [`test_source_database_corners.py`](../../tests/test_source_database_corners.py)의 `test_enc_01_*`와
  `test_enc_01_characterizes_planner_order_sensitive_aggregates`에 남아 있습니다.

### Query와 외부 제공

- PostgreSQL AST validation, relation/function/operator allowlist와 단일 read-only statement를
  application과 DB transaction 양쪽에서 강제합니다.
- 최소 권한 reader, timeout, concurrency, plan·row·byte limit, cancel·rollback과 client-disconnect
  cleanup을 유지합니다. SQL literal, credential, token과 내부 DB 오류는 공개하거나 일반 log에
  남기지 않습니다.
- Database `TEMP` privilege 보유는 reader admission 조건이 아니지만 `SELECT INTO`, DDL,
  `pg_temp` relation, multi-statement와 요청 간 temporary workspace는 계속 허용하지 않습니다.
- HTTP와 MCP는 같은 application service와 source/authorization/query 경계를 사용합니다.
- 인증 principal은 현재 active source를 같은 source-wide budget으로 조회합니다. Source별 사용자
  grant나 caller별 tier를 암묵적으로 만들지 않습니다.
- AuthBridge 연동은 [Resource Server JWT 계약](../resource-server-jwt-auth.md)의 opt-in capability이며
  access token만 로컬 검증합니다. 기본 Compose와 protected cutover 권한을 자동 변경하지 않습니다.

### 검증과 운영

- Verified query는 SQL allowlist가 아니라 metadata·relation·ordered result의 회귀검사입니다. 정확한
  format과 실행법은 [Assurance module](../modules/assurance/README.md#verified-query-회귀검사)이 소유합니다.
- Local Compose, encrypted consent-gated diagnostic capture와 `qm` operator shell의 현재 절차는
  [Operations](../operations.md)가 소유합니다.
- Repository acceptance는 exact commit의 runnable test/CI 결과입니다. Protected environment 실행은
  별도의 target·access·inventory·stop/rollback·change-record 승인이 필요합니다.

## 보류된 방향

아래 항목은 요구를 잊지 않기 위한 요약일 뿐 일정이나 구현 승인이 아닙니다. 정확한 ID와 다시
시작하는 조건은 [Active TODO의 보류 표](../development-todo.md#현재-일정에-없는-일)에 있습니다.

| 주제 | 현재 방향 |
|---|---|
| RLS serving | 현재 모든 RLS source를 DB 접근 전에 차단하므로 cross-tenant probe는 serving에서 도달하지 않습니다. 재활성화하려면 hidden base-policy/dependency의 recursive attestation·migration·cutover 승인이 먼저이며 quarantine 회귀는 [`test_source_database_corners.py`](../../tests/test_source_database_corners.py)의 `test_rls_source_requires_base_policy_drift_to_preserve_isolation`이 검증합니다. |
| Result type 확대 | OID `20, 21, 23, 25, 1082, 1184, 1700` 밖은 거부합니다. 실제 질문과 lossless encoding·새 policy revision 승인이 먼저입니다. |
| DB-backed source authority | 과거 managed 구현을 복원하거나 YAML fallback으로 연결하지 않습니다. Authority·schema·credential·migration·backup/rollback을 새로 결정해야 합니다. |
| DB-native 비용·경보 | 현재 query resource limit만 강제합니다. Monitoring 권한·retention·aggregate 의미와 alert threshold를 별도로 승인해야 합니다. |
| Workflow trace | 현재 request/MCP/query ID를 사용합니다. 이것으로 부족한 실제 correlation 요구와 header trust boundary가 먼저입니다. |

## 새 ADR이 필요한 때

Module interface, external wire, persisted/versioned format, policy/compatibility identity,
safety/lifecycle invariant, ownership/composition boundary 또는 protected operational procedure의 의미를
바꿀 때는 [승인 절차](../development-guidelines.md#승인-규칙)를 먼저 따릅니다. 선택 이유와
compatibility·migration·rollback을 장기간 독립적으로 유지해야 할 때만 새 ADR을 만들고, 단순 구현
완료나 조사 일지는 current owner 문서와 runnable test에 반영합니다.

## Git archive

정리한 ADR 0004~0005, 0007~0024, 0026, 0028~0029, 완료 roadmap, future-work 상세와 날짜별 검증
기록은 commit
`1ff390ab67df215181810a84ac8b2ca8570eceee`에 보존돼 있습니다.

```bash
git show 1ff390ab67df215181810a84ac8b2ca8570eceee:docs/decisions/0020-lossless-interval-and-json-numeric-encoding.md
git ls-tree -r --name-only 1ff390ab67df215181810a84ac8b2ca8570eceee docs/decisions docs/verification
```

현재 문장을 과거 원문에 소급 적용하지 않습니다. 필요한 역사적 맥락은 위 commit에서 읽습니다.
Git history를 rewrite하지 않습니다.
