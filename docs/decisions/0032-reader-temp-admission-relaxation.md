# ADR 0032: Reader TEMP Admission Relaxation

Status: Accepted

Date: 2026-08-30

Decision ID: `QB-READER-TEMP-RELAX-20260830`

Baseline: `1ff390ab67df215181810a84ac8b2ca8570eceee`

## Context

PostgreSQL database의 `TEMP` privilege는 보통 `PUBLIC`, 즉 모든 role에 기본 부여된다. 기존 reader
session policy는 이 privilege가 하나라도 있으면 source를 거부했다. 그 결과 application이 실행하지
않는 temporary-table 기능을 막기 위해 source DB 전체의 기본 privilege를 바꾸도록 요구했고, 권한을
바꿀 수 없는 reviewed source도 등록하지 못했다.

Query Man의 실제 요청 경로는 단일 `SelectStmt`만 허용한다. `SELECT INTO`, DDL, 명시적 `pg_temp`
relation과 allowlist 밖 relation은 SQL AST validation에서 거부한다. Metadata와 query는 gateway가
소유한 connection에서 `REPEATABLE READ READ ONLY` transaction, `search_path=pg_catalog`, allowed-schema
`CREATE` 금지와 resource budget을 계속 검사한다.

## Decision

- Reader가 database `TEMP` privilege를 보유하는지는 source admission 조건이 아니다.
  `require_reader_session_policy`는 `has_database_privilege(..., 'TEMP')`를 검사하지 않는다.
- 이 결정은 temporary table을 사용자 query 기능으로 추가하지 않는다. 사용자 SQL의 `SELECT INTO`,
  DDL과 `pg_temp` relation 접근은 계속 거부하며 multi-statement, session affinity와 요청 간 temporary
  workspace도 지원하지 않는다.
- `search_path=pg_catalog`, schema-qualified allowed relation, 승인된 function/operator와 resolved-object
  검사, allowed schema의 `CREATE` 금지, 최소 권한 role, read-only transaction, timeout, concurrency,
  row/byte limit는 유지한다.
- `temp_file_limit`는 sort/hash 같은 executor 작업의 temporary file 상한으로 계속 적용한다. 명시적
  temporary relation의 quota나 database `TEMP` privilege 검사가 아니다.
- Repository의 작은 local/acceptance fixture는 reader에 database `TEMP`를 부여해 이 허용 경계를
  실제 PostgreSQL에서 회귀 검증한다. 사용자 SQL의 DDL·temporary relation 차단과 read-only
  transaction은 별도로 유지한다.

PostgreSQL은 session-local temporary schema가 존재하면 명시적 `search_path`에 없어도 relation과 data
type lookup에서 먼저 검색할 수 있다. 현재 안전성은 `search_path` 하나가 아니라 gateway session에
temporary object를 만드는 실행 경로가 없다는 전제까지 포함한다. 향후 내부 SQL, multi-statement,
temporary workspace 또는 다른 session-mutating 기능을 추가하면 unqualified `date`/`text` cast와 pool
cleanup을 포함한 이 결정을 먼저 재검토한다.

## Compatibility And Supersession

이 결정은 [ADR 0003](0003-reader-and-resolved-object-policy.md)의 “Reader는 database `TEMP`를 갖지
않는다”는 admission 요건만 supersede한다. Base schema `USAGE`, allowed schema `CREATE`, role flag와
나머지 reader policy는 그대로다.

또한 [ADR 0001](0001-postgresql-ast-validation.md)에서 unqualified `date`와 `text` type의 안전 근거로
database `TEMP` 권한 부재를 요구한 부분만 supersede한다. 현재 근거는 같은 gateway session에서
temporary type을 만들 수 없는 단일-SELECT/DDL 차단, `pg_catalog` search path와 기존 resolved-object
검사의 결합이다. Unqualified cast allowlist 자체와 SQL AST 정책은 바뀌지 않는다.

Python module interface, HTTP/MCP wire format, Source manifest/YAML schema, metadata revision, SQL policy
revision, credential format과 database DDL은 바뀌지 않는다. 기존 source와 새 source가 같은 process에
있어도 별도 migration이나 rolling compatibility 절차는 필요하지 않다.

## Consequences

Source DB의 database-wide `PUBLIC TEMP` 기본값을 Query Man 때문에 바꿀 필요가 없어지고 source 추가가
단순해진다. 반면 reader credential을 Query Man 밖에서 직접 사용하면 그 별도 session에서 temporary
relation을 만들고 storage를 소비할 수 있다. Credential 격리, network 접근 제한과 필요시 DB owner의
선택적 `TEMP` revoke가 그 위험을 소유하며, Query Man admission은 이를 증명하지 않는다.

이번 변경 뒤 `TEMP`를 가진 source가 등록된 상태에서 rollback하면 해당 reader의 privilege를 회수할
때까지 이전 application version은 metadata/query session admission을 실패한다.

## Change And Rollback

Reader session probe에서 database `TEMP` 확인 한 항목만 제거하고 source/onboarding 문서와 회귀 시험을
현재 경계로 맞춘다. Local fixture의 revoke/validation, source YAML, database, credential, protected
environment와 배포 절차는 변경하지 않는다.

Rollback은 이 Git change set을 reviewed revert하는 것이다. 이미 등록된 source가 database `TEMP`를
가지고 있다면 revert 전에 DB owner가 권한 회수 가능 여부를 확인하거나 admission 실패를 받아들여야
한다.

## Verification

- Session-policy SQL이 database `TEMP` privilege를 조회하지 않으면서 나머지 schema/resource 검사를
  유지하는지 unit test로 확인한다.
- Disposable PostgreSQL source에서 reader에게 `TEMP`를 부여한 뒤 session policy가 통과하는지 확인한다.
- `SELECT INTO TEMP`와 `pg_temp` relation이 계속 SQL validation과 security evaluation에서 거부되는지
  확인한다.
- Source Catalog, Metadata, Guarded Query, onboarding/documentation과 전체 test gate를 통과한다.
