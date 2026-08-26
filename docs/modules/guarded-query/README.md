# Guarded Query Module

Status: Logical boundary; physical package split pending

Current launch baseline: [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)
`LAUNCH-01-A`

## 목적

Guarded Query는 이미 선택된 source와 published metadata revision을 기준으로 SQL을 검증하고,
PostgreSQL에서 피해 상한을 강제하며 실행한다. 쉽게 말하면 “이 SQL을 실행해도 되는가”와
“실행한다면 어디까지 자원을 쓰게 할 것인가”를 책임지는 안전문이다.

이 module은 SQL을 생성하거나 caller의 접근 권한을 결정하지 않는다. SQL 문자열은 외부 입력으로
취급하며 prompt나 호출자 관례에 안전을 맡기지 않는다.

## 소유 책임

- PostgreSQL AST 기반 단일 read-only statement 검증
- Relation, function, operator와 type allowlist 및 SQL policy identity
- Published metadata revision과 SQL policy revision 일치 확인
- Source별 queue/concurrency admission과 supplied query ID의 active/cancel lifecycle
- Repeatable-read read-only transaction, timeout/resource setting과 reader-session 검증
- PostgreSQL이 resolve한 function/operator 재검증과 `EXPLAIN` plan admission
- Result column 이름/OID 검사, bounded cursor fetch, row/byte accounting과 canonical encoding
- Client/operator/shutdown cancellation, rollback, pool invalidation과 drain
- SQL literal, credential과 내부 database 오류를 숨기는 query-domain 오류 의미

## 소유하지 않는 책임

- Caller authentication, active-source visibility와 operator 권한
- Source manifest, budget/access policy와 runtime registry mutation
- Physical catalog 수집, metadata context ranking과 revision publish
- HTTP status, MCP `isError`와 transport serialization
- Verified query/quality case의 작성과 승인
- 사용자의 자연어 질문에서 SQL을 생성하는 기능

## 현재 코드 위치

- [`sql_validation.py`](../../../src/query_man/sql_validation.py): AST policy, allowlist,
  `ValidatedSql`과 `SQL_POLICY_REVISION`
- [`query.py`](../../../src/query_man/query.py): `QueryService`, 작은 application `QueryExecutor`,
  Runtime용 `RuntimeQueryExecutor`와 concrete `PostgresQueryExecutor`
- [`result_encoding.py`](../../../src/query_man/result_encoding.py): canonical scalar encoding과
  launch result OID policy material
- [`errors.py`](../../../src/query_man/errors.py): query-domain 오류; public transport rendering은
  Delivery 소유
- [`reader_policy.py`](../../../src/query_man/reader_policy.py): Source Catalog가 소유하고 이 module이
  소비하는 reader connection/session interface
- [`config/security-evaluation.yaml`](../../../config/security-evaluation.yaml): Assurance가 소유하는
  parser/execution security corpus
- Focused tests: [`test_sql_validation.py`](../../../tests/test_sql_validation.py),
  [`test_security_evaluation.py`](../../../tests/test_security_evaluation.py),
  [`test_query.py`](../../../tests/test_query.py),
  [`test_result_encoding.py`](../../../tests/test_result_encoding.py),
  [`test_source_database_corners.py`](../../../tests/test_source_database_corners.py)

현재 코드는 `src/query_man`의 평면 구조다. 위 파일 목록은 논리적 소유권이며 이미 별도 Python
package로 물리 분리됐다는 뜻이 아니다.

## 제공 인터페이스와 소유 경계

아래에서 `공식 Python interface`, application result/error, policy identity와 safety invariant를
구분한다. 여러 범주가 같은 파일에 있다는 이유로 전부를 하나의 module interface라고 부르지 않는다.

### 공식 Python interface

SQL 검증 provider:

```text
validate_sql(sql, allowed_relations, max_sql_bytes) -> ValidatedSql
SQL_POLICY_REVISION -> immutable current policy token
CANONICAL_TIME_POLICY_MATERIAL -> immutable revision/hash input
```

Application query service:

```text
QueryService.query(
  source_id, sql, metadata_revision, sql_policy_revision,
  *, query_id?, tenant_id?
) -> result dictionary
QueryService.cancel(query_id) -> found boolean
```

`QueryService`는 다음 작은 execution port를 소비한다.

```text
QueryExecutor:
  execute(source, sql, metadata_revision, validated, *, query_id?, tenant_id?)
  cancel(query_id) -> found boolean
  close() -> None
```

Runtime은 운영 lifecycle을 위해 이를 확장한 interface만 소비한다.

### RuntimeQueryExecutor lifecycle interface

```text
RuntimeQueryExecutor extends QueryExecutor:
  stop_accepting() -> None
  drain(grace_ms) -> None
  invalidate(source_id) -> None
```

`stop_accepting`은 새 query를 막고, `drain`은 grace 뒤 남은 query를 취소한다. `invalidate`는 해당
source의 pool/admission state를 버리고 `close`는 active task와 pool을 정리한다. Application-only
fake는 Runtime lifecycle method를 구현할 필요가 없다.

Guarded Query가 발생시키는 `QueryRejectedError`, `QueryInvalidError`, `QueryOverloadedError`,
`QueryTimeoutError`, `QueryUnavailableError`도 직접 consumer가 사용하는 domain-error interface다.
Delivery가 이 오류를 HTTP/MCP envelope로 표현하는 방식은 별도 external wire format이다.

### Application result와 error 의미

성공 result dictionary의 keys는 다음과 같다.

```text
status, query_id, metadata_revision, sql_policy_revision, fingerprint
columns, rows, row_count, result_bytes, truncated
queue_ms, elapsed_ms
plan_summary {total_cost, max_rows, node_count}
```

Delivery는 UUID query ID와 trusted tenant를 생성해 전달한다. Public caller는 query ID를 선택하지
않으며, operator authorization은 Delivery가 cancel 호출 전에 강제한다. Guarded Query의 cancel은
active query ID가 있었는지만 반환한다.

Query error category는 `SOURCE_NOT_FOUND`, `METADATA_REVISION_MISMATCH`, `QUERY_REJECTED`,
`QUERY_INVALID`, `QUERY_OVERLOADED`, `QUERY_TIMEOUT`, `QUERY_UNAVAILABLE`다. Allowlisted 사용자 SQL
오류만 server-authored reason/action으로 공개하고 database/driver detail은 숨긴다. Session,
connection, policy, result OID와 내부 실행 실패는 사용자 SQL 교정 오류로 위장하지 않고 details 없는
`QUERY_UNAVAILABLE`로 fail-closed한다.

### 현재 launch policy identity

SQL policy는 version 3이다. Digest material에는 기존 SQL/시간 policy와 함께 다음이 포함된다.

- PostgreSQL 18, server/client `UTF8`, driver codec `utf-8` reader compatibility
- PostgreSQL 18 base OID의 exact launch result set

이전 SQL policy token은 metadata revision mismatch로 분류해 executor 호출 전에 거부한다. 같은 serving
fleet에서 v2와 v3 process를 섞지 않는다.

Final result에서 허용하는 OID는 다음 일곱 개뿐이다.

| PostgreSQL type | OID |
|---|---:|
| `int8` | 20 |
| `int2` | 21 |
| `int4` | 23 |
| `text` | 25 |
| `date` | 1082 |
| `timestamptz` | 1184 |
| `numeric` | 1700 |

Boolean은 predicate, filter와 intermediate expression에는 계속 쓸 수 있지만 final result OID 16은
허용하지 않는다. Float, bytea, JSON/JSONB, UUID, interval/time, array, network, record/domain 등도
final result로 반환할 수 없다. 내부 canonical encoder가 기존 Python value를 처리할 수 있다는 사실은
launch 지원 OID를 넓히지 않는다.

Scalar domain은 PostgreSQL RowDescription에서 base OID로 평탄화되므로 executor OID gate만으로
구분하지 못한다. Current static launch는 bootstrap/Assurance Catalog가 exposed domain column을
snapshot publication 전에 거부하고 custom domain cast는 기존 SQL type allowlist가 거부하는 조합으로
이 경계를 닫는다. 이 static source-admission guard는 SQL policy material을 추가하지 않는다.

User cursor `execute` 뒤 column description을 캡처하고, duplicate column을 먼저 검사한 다음 모든
OID를 검사한다. Duplicate는 기존 `QUERY_REJECTED`가 우선한다. Empty/malformed description 또는
지원하지 않는 OID는 첫 `fetchmany` 전에 details 없는 `QUERY_UNAVAILABLE`로 끝나며 cursor close와
transaction rollback을 수행한다. Partial row, commit과 success usage는 남기지 않는다.

`result_bytes`는 compact UTF-8 JSON rows array의 대괄호와 comma를 포함한다. 다음 행이 row/byte
상한을 넘으면 그 행을 넣지 않고 `truncated=true`로 반환한다. Canonical time/result bytes,
metadata revision algorithm과 기존 verified result hash는 ADR 0025에서 변경하지 않았다.

### 실행 순서와 safety invariant

현재 안전 순서는 다음과 같다.

1. `SourceReader`에서 source 존재를 확인한다.
2. RLS source이면 tenant 유무와 관계없이 즉시 quarantine한다.
3. Published metadata/SQL policy revision을 확인한다.
4. SQL AST와 relation/function/operator/type을 검증한다.
5. Source별 concurrency slot을 획득한다.
6. Pool checkout 직후 no-SQL PostgreSQL 18/UTF-8 connection preflight를 수행한다.
7. Active query를 등록하고 read-only transaction, UTC/resource setting과 reader session을 검증한다.
8. Resolved function/operator와 `EXPLAIN` plan budget을 검증한다.
9. User cursor를 실행하고 duplicate column과 exact result OID를 확인한 뒤 bounded fetch한다.
10. 성공은 commit하고 실패·취소·disconnect는 cancel/rollback과 slot 반환으로 끝낸다.

RLS quarantine는 `QueryService`에서 metadata/revision/validation보다 먼저, direct executor에서 queue와
pool/DB보다 먼저 일어난다. 결과는 항상 details 없는 `503 QUERY_UNAVAILABLE` projection이다. 기존
RLS type, code와 history는 보존하지만 first launch serving에는 참여하지 않는다.

Reader connection preflight는 SQL을 실행하지 않으며 `BEGIN`과 application SQL보다 먼저 수행한다.
Deterministic mismatch는 connection을 close/discard하고 details 없는 `QUERY_UNAVAILABLE`로 끝낸다.
Transport/driver failure의 기존 transient 분류는 유지한다.

### Gateway usage reporting

Guarded Query는 Runtime의 bounded usage recorder에 server-resolved source ID, budget profile, active
metadata revision과 terminal outcome만 보낸다. 성공만 queue/elapsed/rows/bytes/truncation 합계에
기여한다. Recorder 실패는 query 결과를 바꾸지 않으며 SQL, literal, tenant, credential, query ID와
raw database error를 payload에 넣지 않는다.

## 소비 인터페이스와 전제

- [Source Catalog](../source-catalog/README.md)의 `SourceReader`, immutable `SourceProfile`/budget 및
  `READER_CLIENT_ENCODING`, `require_reader_connection_policy`, reader-session verifier
- [Metadata](../metadata/README.md)의 immutable published snapshot/revision과 allowed relation ceiling
- [Runtime](../runtime/README.md)의 bounded operations/usage reporting interface

Delivery는 active source authorization 뒤 generated query ID와 server-derived tenant를 전달하고 client
disconnect를 task cancellation로 전파한다. Runtime은 shutdown/reload 때 lifecycle capability를 정해진
순서로 호출한다. Assurance의 offline CLI만 concrete executor와 service를 직접 조립해 verified SQL도
동일한 `QueryService.query` 경로로 실행한다.

Guarded Query는 Control DB table, HTTP/MCP request model이나 다른 module의 private implementation을
직접 알지 않는다. Query 중 source profile이나 published snapshot을 in-place 변경하지 않는다.

## 불변조건

- SQL AST, relation/function/operator/type, revision과 resolved object를 fail-closed로 검증한다.
- Queue/concurrency/row/byte 상한은 executor가, read-only/timeout/work/temp/session policy는
  PostgreSQL transaction이 실제로 강제한다.
- Reader는 최소 권한이고 connection은 PostgreSQL 18 server/client UTF-8이어야 한다.
- RLS source는 metadata, queue와 database에 닿기 전에 quarantine한다.
- Final result는 exact seven OID이며 OID 검사는 첫 fetch 전에 끝난다.
- Query cancel, timeout, encoding 실패와 disconnect는 rollback과 slot 반환으로 끝난다.
- SQL, bind literal, credential과 원본 database 오류를 response 또는 일반 log에 노출하지 않는다.
- Source별 예외는 Python `source_id` 분기문이 아니라 profile, budget과 curated view로 표현한다.

## 모듈 내부 변경

다음은 공식 interface, policy identity, application/external 의미와 safety/lifecycle 결과를 보존할 때
Guarded Query 안에서 독립적으로 변경할 수 있다.

- 같은 AST acceptance를 만드는 validator 내부 정리
- 동일한 admission 결과를 만드는 plan helper 개선
- Row/byte/OID 판정 결과를 바꾸지 않는 cursor/encoding 성능 개선
- Public reason code를 유지하는 private exception mapping 정리
- Cancel/rollback/drain 결과를 유지하는 lock과 task bookkeeping 개선

## 사용자 승인이 필요한 경계 변경

승인 요청은 아래 실제 범주를 구분해 영향받는 provider/consumer와 함께 제시한다.

- Module interface: `validate_sql`, `ValidatedSql`, `QueryService`, `QueryExecutor`,
  `RuntimeQueryExecutor`, domain error의 shape/signature/호출 의미
- Policy/compatibility identity: 허용 SQL construct/relation/function/operator/type, SQL policy
  revision/material, reader compatibility, final result OID와 canonical encoding/hash
- Application/external format: query input/output field, result byte/truncation, error/reason과
  cancel-not-found 의미; HTTP/MCP rendering 변경은 Delivery 경계도 함께 검토
- Safety/lifecycle invariant: authorize/validate/admit/preflight/transaction/OID/fetch/cancel/rollback
  순서, timeout/concurrency/plan/row/byte/memory/temp limit과 fail-closed 의미
- Ownership/composition boundary: concrete executor 조립 위치, Source Catalog/Metadata/Runtime private
  implementation 접근 또는 Control DB dependency

`LAUNCH-01-A`보다 result type을 넓히거나 RLS serving을 다시 여는 작업은 parked research를 그대로
실행하는 일이 아니다. 현재 v3와 quarantine를 기준으로 compatibility, migration, rollback과 검증
범위를 새로 제안하고 정확히 승인받아야 한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_reader_policy.py \
  tests/test_sql_validation.py tests/test_security_evaluation.py \
  tests/test_query.py tests/test_result_encoding.py
```

Result/error projection을 바꾸면 HTTP/MCP tests를, concurrency/cancel 경계를 바꾸면 load/server tests를
추가한다. PostgreSQL transaction, reader, result OID 또는 pool 경계를 바꾸면 다음 integration gate도
실행한다.

```text
uv run pytest -m integration tests/test_source_database_corners.py
uv run pytest -m integration
```

완료 전 root `AGENTS.md`의 전체 gate를 실행한다.

## 집중해서 읽을 범위

Guarded Query 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 query/validator/encoding code와 focused tests
3. Source profile/reader interface, published metadata interface와 Runtime usage/lifecycle interface
4. Current authority인 [ADR 0025](../../decisions/0025-static-non-rls-first-launch.md)
5. 직접 관련된 기존 결정인 [ADR 0001](../../decisions/0001-postgresql-ast-validation.md),
   [ADR 0002](../../decisions/0002-guarded-query-contract.md),
   [ADR 0003](../../decisions/0003-reader-and-resolved-object-policy.md),
   [ADR 0005](../../decisions/0005-initial-query-budgets.md)
6. 변경되는 result/error/lifecycle의 직접 consumer code와 runnable test

Control DB persistence, metadata relevance algorithm과 parked RLS/encoding/trace proposal body는 현재
interface나 승인된 launch 경계를 변경하지 않는 한 읽을 필요가 없다.
