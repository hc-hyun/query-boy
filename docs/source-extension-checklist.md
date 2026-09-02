# Source Onboarding And Extension Checklist

Status: Source package authority defined by ADR 0034; first-launch inventory frozen by ADR 0025

Source 하나의 Git authority는 정확히 다음 두 파일이다.

```text
config/sources/<source-id>/
  source.yaml
  views.sql
```

`source.yaml`은 Query Man이 어디를 어떤 제한과 업무 의미로 읽을지 정하고, `views.sql`은 DB owner가
검토한 Query Man 전용 공개면을 정의한다. Source별 세 번째 acceptance file은 없고 Runtime은 SQL을
실행하지 않는다. 자세한 결정은
[ADR 0034](decisions/0034-source-view-package-and-direct-admission.md), no-PII 경계는
[ADR 0031](decisions/0031-no-pii-curated-view-boundary.md), TLS는
[ADR 0033](decisions/0033-explicit-source-tls-modes.md)을 따른다.

## 30초 경로 선택

| 하려는 일 | 사용할 경로 |
|---|---|
| 새 source 추가·기존 공개 view 변경 | 이 checklist로 package, DB owner/DBA handoff와 gate를 함께 검토한다. |
| 준비 항목만 plan으로 정리 | [`query-man-source-onboarding` Skill](../skills/query-man-source-onboarding/SKILL.md)을 사용한다. |
| 현재 package 구조/YAML 확인 | `uv run qm source list`, `show`, `validate`를 사용한다. DB나 파일을 바꾸지 않는다. |
| 실제 source 데이터 질문 | [`query-man-text-to-sql` Skill](../skills/query-man-text-to-sql/SKILL.md)의 context→query 흐름을 사용한다. |

새 source 또는 공개 view 변경은 persisted format, policy/lifecycle, ownership과 protected procedure에
영향을 줄 수 있다. Exact source, DB, 이유, output/grant 변화, migration/rollback과 검증 계획을 승인받기
전에는 current inventory를 바꾸지 않는다. Repository 승인은 protected DB 실행 권한이 아니다.

## 두 파일에 무엇을 두나요?

### `source.yaml`

Manifest version은 4다. 최소한 다음을 review한다.

- Directory와 정확히 같은 `source_id`
- 양의 정수 `view_contract_version`
- Host/port/database/reader와 password 값이 아닌 secret environment key
- Exact `sslmode`: `disable`, `require`, `verify-full` 중 하나
- Query Man 전용 schema와 exact `allowed_relation_kinds: [view]`
- 기존 `budget_profile`
- 모든 공개 view의 semantic entry, grain, 설명과 필요한 default time
- Alias, value hint, measure, join, business predicate가 참조하는 column
- 같은 폴더의 `views.sql`을 가리키는 provenance

Directory에는 regular non-symlink `source.yaml`과 `views.sql`만 둔다. Flat YAML, `.yml`, nested directory,
unknown file와 이전 format fallback은 거부한다. Password, token, full DSN과 private key는 Git에 넣지
않는다.

### `views.sql`

이 파일은 desired DB view artifact다. 다음만 둔다.

- Query Man 전용 schema의 curated view 생성·교체
- Explicit output column list와 schema-qualified base relation
- View/column comment와 contract marker
- Dedicated NOLOGIN view owner와 exact object별 privilege
- Reader의 exact public view `SELECT`
- View owner가 실제 참조하는 exact base relation의 `SELECT`
- `PUBLIC`/reader의 불필요한 privilege revoke
- Atomic transaction과 bounded lock timeout

다음은 넣지 않는다.

- Base table/index DDL, seed 또는 row DML
- Database, reader/view-owner role 생성, password나 secret-store 명령
- Runtime migration hook, database 선택/deploy/traffic/environment 분기
- `SELECT *`, `SELECT ON ALL TABLES`, broad future/default grant

`views.sql`은 application Runtime이 읽거나 실행하지 않는다. Local fixture는 명시적 fixture wiring으로,
protected environment는 별도 승인된 DBA 절차로 적용한다.

## View marker와 version

Reader에게 보이는 모든 view comment의 첫 줄은 다음 exact marker다.

```text
query-man:source=<source-id>;view-contract=<positive integer>
사람이 읽는 비어 있지 않은 relation 설명
```

Marker source/version은 `source.yaml`과 일치해야 한다. Metadata는 marker를 제거한 설명만 context에
공개한다.

다음이 바뀌면 source-level `view_contract_version`을 올리고 모든 공개 view marker를 같은 값으로
갱신한다.

- 공개 view set/name/kind 또는 definition
- Output column 이름·순서·type·nullability
- View security behavior
- Effective owner/grant surface

Relation/column 설명, semantic overlay와 budget만 바뀌면 contract version을 올리지 않아도 된다.
그 변화가 SQL 생성 context에 영향을 주면 `metadata_revision`은 정상적으로 바뀔 수 있다.

Version은 live DB가 선언한 contract 번호다. Git SQL과 live definition의 byte 동일성을 증명하는 hash가
아니다. Process가 이미 본 같은 version의 구조 변화는 fail-closed하지만 restart 전 privileged drift를
모두 잡지는 못한다. Protected DDL inventory, serving freeze와 설명되지 않은 drift의 route 중단은 계속
필수다.

## 누가 언제 왜 사용하나요?

| 사람/구성요소 | 언제 | 왜 |
|---|---|---|
| Query Man repository owner | PR에서 `source.yaml`과 desired `views.sql` 작성·review | Application 설정과 원하는 DB 공개면을 한 source 폴더에서 추적 |
| DB/data owner | PR과 apply 전 exact view output 검토 | Grain·업무 의미·no-PII와 필요한 base relation을 확인 |
| DBA/operator | Protected target에 traffic을 끈 상태로 apply/rollback | 강한 DDL 권한, lock, dependency, owner/ACL과 change record를 통제 |
| Runtime | Startup/refresh 때 최소 권한 reader로 catalog와 marker를 읽음 | View/semantic/version 불일치를 query 전에 fail-closed |
| Client | `get_context` 뒤 exact 두 revision으로 `query` | 낡은 metadata나 SQL policy로 만든 query 실행 방지 |

## Runtime이 직접 검사하는 것

Metadata publish 전 다음을 요구한다.

- Reader가 볼 수 있는 relation이 allowed schema의 view뿐임
- 모든 공개 view marker source/version 일치
- 발견된 모든 view와 semantic relation entry가 정확히 대응
- 모든 view의 grain과 description
- Event/comment/population role의 default time column
- Grain/default time/alias/value hint/measure/predicate column 존재
- Approved join relation/column과 type 일치
- 같은 process의 same-version structural drift 없음

실패는 deterministic `METADATA_UNAVAILABLE`이며 이전 stale snapshot으로 우회하지 않는다. Column 목록은
PostgreSQL catalog에서 동적으로 읽으므로 YAML에 exhaustive column list나 expected definition hash를
복제하지 않는다.

`metadata_revision`과 `sql_policy_revision`은 계속 request-freshness token이다. Client가 context에서 받은
두 값을 query에 정확히 전달하지 않으면 executor 전에 거부한다.

## 항상 필요한 안전 검토

| 영역 | 필수 결과 |
|---|---|
| Database/reader | PostgreSQL 18, server/client/driver UTF-8, TCP와 reviewed TLS, 최소 권한 LOGIN과 유한 connection limit |
| Public views | DB owner가 exact output에 PII가 없음을 확인, explicit columns, marker/comment, dedicated owner와 exact grants |
| Source package | Version 4, exact two files, view-only allowlist, 기존 budget, complete semantic overlay, secret 값 없음 |
| Metadata | Bounded read-only catalog, marker/direct admission, dynamic columns, revision/drift fail-closed |
| Guarded Query | AST와 모든 allowlist, read-only transaction, timeout/concurrency/plan/row/byte/OID limit, cancel/rollback |
| Delivery/Runtime | Source authorization, HTTP/MCP parity, single replica, startup/readiness와 shutdown cleanup |
| Repository gates | Security corpus, integration, container, bounded load와 soak |

현재 final result OID는 `20, 21, 23, 25, 1082, 1184, 1700`만 허용한다. 다른 type, function,
operator, collation, extension, reader setting이나 RLS가 필요하면 source 추가를 멈추고 별도 policy와
migration 승인을 받는다. Database `TEMP` privilege 보유 자체는 reader admission 실패가 아니지만
사용자 SQL의 `SELECT INTO`, DDL과 `pg_temp` relation은 계속 거부한다.

## Protected apply

Apply 전 DB owner/DBA가 다음을 준비한다.

- Exact target/database, executor/access와 traffic freeze
- Current view definition, comment, owner, ACL과 dependency rollback artifact
- New exact output의 no-PII/업무 의미 확인
- Lock/statement timeout, stop condition과 append-only change-record owner
- 호환되지 않는 column remove/name/type change의 별도 reviewed down SQL

DBA는 reader가 아닌 migration authority로 exact database에서 `ON_ERROR_STOP`과 transaction을 사용해
적용한다. Apply 후 marker/source/version, relation/column/type, owner/ACL, reader exact view access와
base schema/table/DML 거부를 확인한다. PostgreSQL/encoding/TLS/session/catalog probe와 application
readiness가 모두 통과한 뒤에만 route를 연다.

다음이면 즉시 중단한다.

- Target, owner, dependency, current marker 또는 rollback artifact 불명확
- Missing/incompatible base relation·column·type
- No-PII 확인 실패 또는 RLS 의존
- Marker mismatch, unexpected reader-visible view, broad/default privilege
- Reader base privilege, Query Man schema CREATE 또는 DML privilege
- Timeout, 설명되지 않은 definition/revision drift 또는 Runtime DDL/admin credential 요구

Rollback 중에도 traffic을 막고 previous application/source package, view definition/comment/owner/ACL을
함께 복구한다. Base table, business row, secret과 role을 자동 drop/delete하지 않는다.

## Repository 검증

최소 검증은 다음과 같다.

```bash
uv run qm source validate
uv run pytest tests/test_registry.py tests/test_catalog.py tests/test_metadata.py tests/test_revision.py
uv run ruff check .
uv run mypy src
uv run pytest
```

DB/fixture 변경은 PostgreSQL integration, static privilege validation, container, load와 soak도 실행한다.
Repository PASS는 exact commit/CI evidence일 뿐 protected apply 완료 증거가 아니다.

## 보통 필요하지 않은 것

- Source별 Python branch, endpoint 또는 MCP tool
- Source별 세 번째 acceptance/expected-result file
- YAML의 exhaustive column list나 Git/live definition hash
- 새 budget profile이나 caller별 grant
- Control DB, hot reload, runtime writer/migration hook
- Plugin, framework, factory, wrapper 또는 별도 service

이 항목이 실제로 필요하면 단순 source 추가가 아니다. 영향 범주와 owner, migration/rollback을 따로
제시하고 승인받는다.
