# ADR 0034: Source Package, Reviewed Views And Direct Admission

Status: Accepted

Date: 2026-09-02

Decision ID: `SOURCE-VIEW-01`

Baseline: `4a147a731daf27a8ebb09ec95b326fa088d0f175`

## Context

기존 source 추가는 flat source manifest 외에 quality case와 verified query/result artifact를 함께
관리했습니다. Runtime은 verified artifact에서 현재 metadata revision의 존재 여부를 읽어 source
publication 조건으로 사용했지만, 해당 SQL과 결과를 Runtime에서 실행하지는 않았습니다. 자연어
quality case도 production에서 주기 실행되는 검사가 아니라 repository fixture acceptance였습니다.

이 구조는 source를 추가할 때 DB owner가 실제로 통제해야 하는 curated view와 Query Man의 설정을
분리하고, 설명·budget·catalog·result expectation을 하나의 품질 단계와 revision gate에 결합했습니다.
반면 Query Man은 이미 PostgreSQL catalog에서 view와 column을 동적으로 읽고, client가 받은
`metadata_revision` 및 `sql_policy_revision`과 실행 시점의 값을 비교합니다.

Source 추가의 최소 단위는 다음 두 책임이면 충분합니다.

- Query Man이 source 연결·허용 schema·budget·업무 의미를 이해하는 설정
- DB owner가 검토한 Query Man 전용 view 정의와 최소 권한

View 정의는 Git에 보존하되 application Runtime이 DDL을 실행해서는 안 됩니다. 또한 Git SQL과 live
definition의 암호학적 동일성 증명까지 도입하지 않고, 간단한 version marker와 protected DDL
inventory/freeze를 사용합니다.

## Decision

### 1. Source package가 Git authority다

각 source는 정확히 다음 두 파일로 구성합니다.

```text
config/sources/<source-id>/
  source.yaml
  views.sql
```

- Source manifest format은 version 4입니다.
- Source directory 이름은 `source.yaml.source_id`와 정확히 같아야 합니다.
- 각 directory에는 regular, non-symlink `source.yaml`과 `views.sql`만 존재해야 합니다.
- Flat manifest, `.yml`, unknown file, nested directory와 version 3 fallback은 허용하지 않습니다.
- `source.yaml`은 positive integer `view_contract_version`을 필수로 가집니다.
- `allowed_relation_kinds`는 정확히 `[view]`여야 합니다. Table이나 materialized view 공개는 이
  결정의 compatibility 범위가 아닙니다.
- `provenance.database_migration_ref`는 같은 source directory의 `views.sql`을 가리킵니다.

`SourceRegistry`는 두 파일의 구조적 존재와 YAML을 strict validation하지만 `views.sql` 내용을
해석하거나 실행하지 않습니다. Runtime source authority, Control DB, hot reload와 fallback은 계속
없습니다.

### 2. `views.sql`은 desired view artifact다

`views.sql`에는 다음만 둡니다.

- Query Man 전용 schema의 curated view 생성·교체
- Explicit output column과 schema-qualified base relation
- Relation·column comment와 view contract marker
- Dedicated NOLOGIN view owner
- Reader의 exact view `SELECT`
- View owner의 exact referenced base-relation `SELECT`
- `PUBLIC`과 reader의 불필요한 privilege revoke
- Atomic transaction과 bounded lock behavior

다음은 포함하지 않습니다.

- Business base table·index DDL 또는 seed/data DML
- Database나 reader/view-owner role 생성
- Password, token, DSN, certificate private key 또는 secret-store 명령
- Runtime migration hook, deployment, traffic route 또는 environment branch
- `SELECT *`, broad `SELECT ON ALL TABLES`와 future default grant

Application image에 SQL file이 포함될 수 있지만 Runtime은 이를 열거나 실행하지 않으며 administrator
credential도 받지 않습니다. DB/data owner가 exact output column과 no-PII 경계를 검토하고 DBA가
traffic 밖에서 적용합니다.

### 3. View contract version은 DB comment에 둔다

Reader에게 보이는 모든 allowed view는 comment 첫 줄에 다음 marker를 가져야 합니다.

```text
query-man:source=<source-id>;view-contract=<positive integer>
```

그 다음 줄부터는 사람이 읽는 non-empty relation description입니다. Metadata는 marker를 분리하고
description만 context와 revision의 comment material로 사용합니다.

Runtime은 다음을 fail-closed로 검사합니다.

- Marker 존재와 exact syntax
- Marker source와 active source ID 일치
- 모든 reader-visible view의 marker version과 `source.yaml.view_contract_version` 일치
- Process가 이미 승인한 같은 version에서 relation set/name/kind, view definition digest, output column
  name/order/type/nullability 또는 지원하는 view security option이 바뀌지 않음

동일 version의 구조 drift는 deterministic mismatch이며 stale snapshot으로 fallback하지 않습니다.
Relation/column description과 semantic metadata 개선은 view contract version을 올리지 않아도 되지만
`metadata_revision`은 정상적으로 바뀔 수 있습니다.

View set, definition, output shape, security behavior 또는 effective ownership/grant surface가 바뀌면
source-level version을 올리고 모든 published view marker를 같은 값으로 갱신합니다. YAML에 expected
definition hash나 exhaustive column inventory를 저장하지 않습니다.

Version marker는 live DB에 적용됐다고 선언한 view contract version이지 Git SQL과 live definition의
byte/semantic 동일성 증명이 아닙니다. Process 시작 전에 같은 version으로 바뀐 privileged DDL,
function/operator/collation/setting drift는 완전히 탐지하지 못합니다. ADR 0025의 protected inventory,
serving freeze와 설명되지 않은 drift의 route 중단을 계속 필수로 둡니다.

### 4. Semantic metadata는 직접 admission한다

별도 품질 단계 없이 candidate metadata를 publish하기 전에 다음을 직접 요구합니다.

- 발견된 모든 view와 semantic relation entry의 exact coverage
- 모든 view의 grain
- Semantic description 또는 marker 뒤의 DB relation description
- Event/comment/population role의 default time column
- Grain, default time, alias, value hint, measure, join과 business predicate가 참조하는 실제 column
- Join column type compatibility

위반하면 `METADATA_UNAVAILABLE`로 source를 거부하고 이전 stale snapshot을 제공하지 않습니다.
Exhaustive column list는 YAML에 두지 않으며 PostgreSQL catalog에서 동적으로 읽습니다. Base table의 새
column은 explicit view definition에 추가되지 않는 한 공개되지 않습니다.

기존 combined `metadata_revision`은 승인 artifact가 아니라 context와 query 사이의 request-freshness
token으로 유지합니다. `get_context`가 현재 metadata/SQL policy revision을 반환하고 `query`가 exact
두 revision을 요구하며 mismatch를 executor 전에 거부하는 동작은 변경하지 않습니다.

### 5. Source-specific acceptance artifact를 제거한다

다음을 current authority와 product interface에서 제거합니다.

- Quality/verified YAML과 domain-lab 복사본
- Quality/verified parser, result registry와 offline composition
- `query-man-evaluate`, `query-man-verify`
- Runtime의 verified revision load와 publication dependency
- Source manifest의 `minimum_quality_level`
- Metadata context의 `quality_level`
- `qm source validate`의 verified artifact output
- Repository CI와 protected launch procedure의 exact business-result gate

이는 exact representative result hash와 aggregate natural-language relation/answerability score를
의도적으로 제거합니다. 같은 기능을 다른 이름의 source artifact로 다시 만들지 않습니다. Query
lifecycle에 필요한 deterministic SQL은 test-local fixture로만 유지합니다.

다음 safety와 verification은 그대로 유지합니다.

- External input와 PostgreSQL AST validation
- Source/schema/relation/function/operator/type allowlist
- Minimum-privilege reader, read-only transaction와 PostgreSQL session policy
- Timeout, concurrency, plan, row와 byte limit
- Cancel, rollback, disconnect와 shutdown cleanup
- Result OID/canonical encoding negative corpus
- Metadata/SQL revision mismatch fail-closed
- Security evaluation, integration, container, bounded load와 soak

Domain-lab source-selection corpus는 client workflow 실험으로 남길 수 있지만 source publication,
onboarding artifact 또는 production gate가 아닙니다.

### 6. Ownership과 protected procedure

| Owner | 책임 |
|---|---|
| Query Man repository | `source.yaml`, desired `views.sql`, strict Runtime checks |
| DB/data owner | Exact output column, business meaning과 no-PII 확인 |
| DBA/operator | Traffic-off apply, inventory, privilege probe와 rollback |
| Runtime | Minimum-privilege reader catalog/query; DDL과 admin credential 금지 |

Repository change와 procedure 승인은 실제 protected DB action 권한이 아닙니다. 실제 실행에는 exact
target, access, executor, traffic freeze, stop condition, rollback artifact와 append-only change-record
owner를 확인한 별도 authorization이 필요합니다.

## Compatibility And Supersession

- 이 결정은 ADR 0030의 flat `config/sources/*.yaml` 및 verified-query authority를 source package
  authority로 대체합니다. Budget YAML authority와 retired managed capability는 유지합니다.
- ADR 0025의 두 non-RLS source, PostgreSQL 18/UTF-8, seven result OID, SQL policy, revision mismatch,
  reader/query safety와 privileged DDL freeze는 유지합니다. Nine-query repository/protected launch gate는
  이 결정이 대체합니다.
- ADR 0031의 DB-owner-confirmed no-PII curated-view boundary는 유지합니다. Quality/verified domain-lab
  verification 조항만 이 결정이 대체합니다.
- ADR 0033의 exact TLS mode와 transport behavior는 유지합니다. Manifest version 3 format만 version 4
  source package로 대체합니다.
- `get_context.quality_level`, `qm source list.minimum_quality_level`, 두 offline console command와
  `qm source validate`의 verified fields는 호환 기간 없이 제거합니다.
- `metadata_revision`과 `sql_policy_revision` wire fields 및 mismatch semantics는 유지합니다.

## Migration

Migration은 하나의 repository change set과 release로 수행합니다.

1. Active와 domain-lab flat manifests를 version 4 source directory로 이동합니다.
2. Existing fixture schema SQL의 curated view/comment/grant를 source-local `views.sql`로 옮깁니다.
3. Broad/default view-owner grant를 exact base relation grant로 축소합니다.
4. Fixture init/apply order를 base schema, views, seed, privilege validation 순서로 바꿉니다.
5. Source Registry, Metadata, Runtime와 operator CLI를 새 package/admission semantics로 전환합니다.
6. Quality/verified artifacts, code, entrypoints와 gates를 제거합니다.
7. Current ADR index, module/operations/onboarding 문서와 runnable tests를 함께 갱신합니다.

Version 3 fallback, dual directory parser, hidden environment switch와 Runtime DDL migration은 만들지
않습니다.

## Protected Apply And Rollback

Protected apply 전에는 traffic을 차단하고 current view definition, comment, owner, ACL과 dependency를
rollback artifact로 보존합니다. DB owner가 exact output에 PII가 없고 RLS 의존이 없음을 확인한 뒤,
DBA가 reader가 아닌 migration authority로 exact database에서 `ON_ERROR_STOP`과 transaction을 사용해
`views.sql`을 적용합니다.

적용 뒤 marker/source/version, relation·column/type, owner, ACL, reader exact view access와 base
schema/table/DML 거부를 확인합니다. PostgreSQL/encoding/TLS/reader/session/catalog probe와 application
readiness가 모두 통과한 뒤에만 route를 엽니다.

다음은 stop condition입니다.

- Target, owner, existing marker, dependency 또는 rollback artifact 불명확
- Missing base relation/column/type 또는 incompatible view replacement
- No-PII/RLS 확인 실패
- Marker mismatch, unexpected reader-visible view 또는 broad/default privilege
- Reader base privilege, Query Man schema CREATE 또는 DML privilege
- Lock/statement timeout이나 설명되지 않은 definition/revision drift
- Runtime DDL/admin credential 요구

Rollback은 traffic을 계속 차단하고 previous source/application artifact와 previous `views.sql`, comment,
owner와 ACL을 함께 복구하는 것입니다. `CREATE OR REPLACE VIEW`가 지원하지 않는 column removal/name/type
rollback은 사전에 별도 approved down SQL을 준비하지 않았다면 forward apply를 시작하지 않습니다.
Base table, business row, secret과 role을 자동 drop/delete하지 않습니다.

## Verification

- Version 4 exact-directory/file validation과 no-fallback negative tests
- Missing/malformed/source/version marker 및 unmarked reader-visible view rejection
- Direct semantic completeness와 all referenced-column validation
- Same-version structural drift no-stale rejection
- Reviewed version update 뒤 dynamic relation/column publication
- Comment/semantic improvement의 normal metadata revision rotation
- Stale metadata/SQL revision query rejection before execution
- Exact view owner/reader/base privilege fixture validation
- Security corpus, AST/allowlist, session/resource/OID/cancel/rollback tests
- Ruff, mypy, full pytest, PostgreSQL integration, container, load와 soak gates

Repository evidence는 exact commit과 CI provenance로 남깁니다. 실제 protected apply evidence는 승인된
environment record에만 append합니다.
