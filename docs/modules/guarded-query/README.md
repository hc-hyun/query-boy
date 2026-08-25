# Guarded Query Module

Status: Logical boundary; physical package split pending

## 목적

Guarded Query는 이미 선택된 source와 published metadata revision을 기준으로 SQL이 허용되는지
검사하고, PostgreSQL에서 피해 상한을 강제하며 실행한다. 쉽게 말하면 “이 SQL을 실행해도 되는가”와
“실행한다면 어디까지 쓰게 할 것인가”를 책임지는 안전문이다.

이 module은 SQL을 생성하거나 caller의 접근 권한을 결정하지 않는다. SQL 문자열은 외부 입력으로
취급하며 prompt나 호출자의 선의에 안전을 맡기지 않는다.

## 소유 책임

- PostgreSQL AST 기반 단일 read-only statement 검증
- Relation, function, operator와 type allowlist 및 `SQL_POLICY_REVISION`
- Published metadata revision과 SQL policy revision 일치 확인
- Source별 queue/concurrency admission과 supplied query ID의 active/cancel lifecycle
- Repeatable-read read-only transaction, local timeout/resource setting과 reader-session 검증
- PostgreSQL이 resolve한 function/operator 재검증과 `EXPLAIN` plan admission
- Bounded cursor fetch, row/byte accounting, canonical scalar encoding과 truncation
- Client/operator/shutdown cancellation, rollback, pool invalidation과 drain
- SQL literal, credential과 내부 database 오류를 숨기는 query-domain 오류 의미

## 소유하지 않는 책임

- Caller authentication, shared active-source visibility와 operator 권한
- Source manifest, budget profile과 runtime registry mutation
- Physical catalog 수집, metadata context ranking과 revision publish
- HTTP status, MCP `isError` 또는 transport serialization
- Verified query/quality case의 작성과 승인
- 사용자의 자연어 질문에서 SQL을 생성하는 기능

## 현재 코드 위치

- [`sql_validation.py`](../../../src/query_man/sql_validation.py): AST policy, allowlist,
  `ValidatedSql`과 `SQL_POLICY_REVISION`
- [`query.py`](../../../src/query_man/query.py): `QueryService`, 작은 application `QueryExecutor`,
  Runtime 전용 `RuntimeQueryExecutor`와 `PostgresQueryExecutor`
- [`result_encoding.py`](../../../src/query_man/result_encoding.py): result scalar의 JSON-safe
  canonical encoding
- [`errors.py`](../../../src/query_man/errors.py): query rejection/invalid/overload/timeout/unavailable
  의미; public rendering은 Delivery 계약
- [`reader_policy.py`](../../../src/query_man/reader_policy.py): Source Catalog가 소유하고 Metadata와
  이 module이 함께 소비하는 reader-session safety contract
- [`config/security-evaluation.yaml`](../../../config/security-evaluation.yaml): Assurance가
  소유하고 이 module의 parser/execution safety를 검증하는 versioned corpus
- Focused tests: [`test_sql_validation.py`](../../../tests/test_sql_validation.py),
  [`test_security_evaluation.py`](../../../tests/test_security_evaluation.py),
  [`test_query.py`](../../../tests/test_query.py),
  [`test_result_encoding.py`](../../../tests/test_result_encoding.py),
  [`test_source_database_corners.py`](../../../tests/test_source_database_corners.py)

`QueryService`는 `execute/cancel/close`만 제공하는 작은 `QueryExecutor`를 계속 소비한다. Runtime은
이를 확장한 `RuntimeQueryExecutor`를 요구하므로 application-only fake나 adapter가 운영 lifecycle
method까지 구현할 필요는 없다. Concrete `PostgresQueryExecutor`는 두 Protocol을 구조적으로
구현한다.
Assurance의 `query-man-verify`는 `assurance_cli.py`에서만 concrete executor와 `QueryService`를
조립하고 verified core에는 service를 주입한다. Verification SQL은 계속 `QueryService.query`를
통과하며 CLI가 tenant ID를 추가하지 않는다.

## 제공 계약

### SQL policy contract

```text
validate_sql(sql, allowed_relations, max_sql_bytes) -> ValidatedSql
SQL_POLICY_REVISION -> current immutable policy identity
```

- 하나의 `SELECT` 또는 허용된 read-only query만 통과한다.
- AST에서 확인한 relation, function, operator와 type이 모두 allowlist 안에 있어야 한다.
- 현재 function policy에는 `dense_rank`, `percentile_cont`, `regexp_replace`, `position`,
  `jsonb_build_object`, `to_jsonb`가 포함된다. 이름 승인은 visible `pg_catalog`
  overload candidate 전체가 namespace, volatility, security-definer와 reader execute 검증을
  통과할 때만 유효하며 table function이나 다른 schema의 동명 함수를 허용하지 않는다.
- Client가 보낸 metadata/SQL policy revision이 현재 published 값과 다르면 실행하지 않는다.
- Policy 재료가 바뀌면 revision도 함께 바뀌어 오래된 client 계획을 fail-closed한다.
- SQL policy version 2는 `result_encoding.py`의 immutable canonical-time policy material version 1을
  digest에 포함한다. Metadata는 같은 object/material을 자신의 revision에 포함한다.

### Query application contract

```text
query(source_id, sql, metadata_revision, sql_policy_revision, query_id?, tenant_id?)
cancel(query_id) -> found boolean
```

성공 결과의 exact keys는 다음과 같다.

```text
status, query_id, metadata_revision, sql_policy_revision, fingerprint
columns, rows, row_count, result_bytes, truncated
queue_ms, elapsed_ms
plan_summary {total_cost, max_rows, node_count}
```

Delivery Gateway가 UUID query ID와 trusted tenant를 생성해 전달하고 Guarded Query는 그 ID의
active/cancel lifecycle을 관리한다. Direct/internal caller가 ID를 생략하면 executor가 생성할 수
있지만 public request가 query ID를 선택하지 않는다.

Canonical scalar는 `null/bool/int/text/finite float`를 JSON scalar로, `numeric`을 decimal string,
`bytea`를 `base64:` standard Base64, date/time/interval/UUID/network를 stable string으로,
non-finite float를 `NaN|Infinity|-Infinity` string으로 표현한다. Sequence/mapping은 재귀적으로
encoding하고 mapping key는 string만 허용한다. 지원하지 않는 type은 fail-closed한다.
Aware Python datetime은 UTC `+00:00` ISO 문자열로 정규화하고 `Z`를 쓰지 않는다. Naive datetime,
date, time과 timetz는 기존 `isoformat()` 표현을 보존한다. Microseconds는 Python의 automatic
timespec을 그대로 따른다.

현재 default driver 경계에는 known gap이 있다. Month-bearing PostgreSQL interval은 Python
`timedelta`에서 calendar-month 의미를 잃고, JSON/JSONB fractional number는 float precision을 잃을
수 있으며 duplicate-key JSON은 앞 key를 잃는다. PostgreSQL time/timetz의 valid 24시는 decode되지
않고 SQL_ASCII text는 bytea와 같은 bytes/Base64로 보일 수 있다. 또한 common reader policy가
`extra_float_digits`, `DateStyle`, `IntervalStyle`, `standard_conforming_strings`,
`transform_null_equals`, `array_nulls`, `client_encoding`, `timezone_abbreviations`를 아직 고정하지 않아 role
default에 따라 finite float 결과/hash, date·string·NULL 비교·array literal SQL 의미가 달라지거나
일부 driver decode가 실패할 수 있다. Catalog/revision은 effective database/column collation도 담지
않아 live `C`→`pg_c_utf8` 변경 뒤 같은 revision의 `lower()` 결과가 바뀐다. Empty psycopg
Multirange와 empty range array는 element object가
없어 ordinary empty SQL array와 같은 `[]`/hash로 성공하지만 nonempty 값은 실패한다. Psycopg list는
array lower bound도 보존하지 않아 0-based와 1-based array가 같은 value/hash가 된다. Day-time
interval과 ordinary mapping/sequence evidence를 이 범위의 무손실·결정성 보장으로 확대하지 않는다.
Result cursor column OID도 encoder에 전달하지 않아 validator가 허용한 anonymous/named record가
field count/type을 잃고, `oid/name`·array, money/XML/geometric 같은 registered/unregistered OID가
Python int/str/list라는 이유로 approved integer/text/array처럼 통과한다. 현재 문서의 supported
PostgreSQL type과 실제 driver Python type을 같은 것으로 간주하지 않는다.
[Proposed ADR 0020](../../decisions/0020-lossless-interval-and-json-numeric-encoding.md)의
`ENC-01-A|B|C`가 승인되기 전 loader, reader setting, canonical value, revision과 hash를 변경하지
않는다. Infinity date, range와 nonempty multirange/range-array 같은 object-valued unsupported result는
현재 `QUERY_UNAVAILABLE`로 rollback하지만 record/composite와 Python int/string/list로 내려오는
unknown 또는 non-allowlisted result OID의 fail-closed gate는 아직 없다.

Exact A 제안이 승인되면 Guarded Query는 v2 published snapshot의 required
`source_semantics_fingerprint`를 받고 Metadata-owned helper로 user planning 전 live value를 비교한다.
User-result text cursor에만 loader를 SQL 실행 전 등록하고 RowDescription OID 전체를 fetch
전에 allowlist한다. Catalog/`EXPLAIN`/Control JSON loader와 HTTP/MCP field는 확장하지 않는다.
RowDescription에서 base OID로 identity가 지워지는 domain은 Metadata의 declared-column/direct-type
admission과 SQL type allowlist에서 미리 거부하고 base OID 자체를 domain 허용 증거로 쓰지 않는다.
직렬 구현에서는 Guarded Query가 immutable result-policy v2/SQL-policy v3 descriptor를 먼저
동결해 Metadata revision provider에 공개하고, Metadata baseline 확정 후 executor가 fingerprint를
소비한다. 이는 같은 module의 provider symbol과 consumer symbol을 서로 다른 agent가 동시
편집한다는 뜻이 아니다.
이는 승인 대기 제안이며 현재 executor 계약이 아니다.

`result_bytes`는 compact UTF-8 JSON rows array의 `[]`와 comma까지 포함한다. 다음 행이 byte 또는
row 상한을 넘으면 그 행을 넣지 않고 `truncated=true`로 반환한다. Duplicate result column은
dictionary row value 손실을 막기 위해 fetch 전에 거부한다.

### Gateway usage signal contract (`CTRL-07A`, implemented)

Guarded Query는 Runtime이 제공하는 bounded usage recorder에 server-resolved source ID,
`SourceProfile.budget.name`, active published metadata revision과 canonical terminal outcome만 보낸다.
Revision/policy, SQL allowlist, plan admission과 allowlisted user-SQL 오류는 `rejected`; queue/pool
포화는 `overloaded`; operator/disconnect/shutdown 취소는 `cancelled`; 나머지 timeout/unavailable은
각각 `timeout|failed`다. 성공만 queue/elapsed/returned rows/result bytes/truncation 합계에 기여한다.

한 query의 여러 audit event를 각각 세지 않고 attribution 이후 terminal outcome을 정확히 한 번
기록한다. Attribution 전에 끝난 authentication, unknown source와 active revision read failure는
record하지 않는다. Recorder failure는 query 결과나 오류를 바꾸지 않고 SQL, question, caller,
tenant, fingerprint, query ID와 raw database error를 payload에 넣지 않는다. 기존 public query
result/error와 audit event 의미는 유지한다.

Lower-priority [proposed ADR 0022](../../decisions/0022-w3c-workflow-trace-context.md)의 exact A는 아직
Guarded Query 계약이 아니다. 승인되고 Runtime/Delivery provider baseline이 먼저 확정된 뒤에만
Guarded Query는 current process-local trace만 execution failure, interruption과 plan rejection audit의
추가 correlation field로 소비한다. Delivery-private MCP call ID는 Guarded Query로 넘기지 않고 Delivery의
Gateway query lifecycle audit에서만 연결한다. Trace를 authorization, admission, cache, cancel key,
PostgreSQL `application_name`, result/hash 또는 usage signal에 넣지 않고 target query의 original trace를
cancel request trace로 덮어쓰지 않는다.

### Error contract

Public query error category는 `SOURCE_NOT_FOUND`, `METADATA_REVISION_MISMATCH`, `QUERY_REJECTED`,
`QUERY_INVALID`, `QUERY_OVERLOADED`, `QUERY_TIMEOUT`, `QUERY_UNAVAILABLE`다. `QUERY_REJECTED`는
server-owned reason code와 allowlisted construct만, `QUERY_INVALID`는 allowlisted SQLSTATE-derived
reason/action/retryable detail만 공개한다. 나머지 database/driver detail은 숨긴다.

`QUERY_INVALID` reason은 다음 고정 집합이다.

```text
QUERY_UNDEFINED_COLUMN, QUERY_INVALID_CAST, QUERY_DIVISION_BY_ZERO,
QUERY_INVALID_LIMIT, QUERY_INVALID_REGULAR_EXPRESSION,
QUERY_NUMERIC_VALUE_OUT_OF_RANGE, QUERY_INVALID_FUNCTION_ARGUMENT,
QUERY_INVALID_FUNCTION_USAGE, QUERY_FUNCTION_SIGNATURE_MISMATCH
```

Detail은 `{reason_code, action: "CORRECT_SQL", retryable: true}`고 message는 PostgreSQL 문구가
아닌 reason별 server-authored 교정 안내다. 이 분류는 사용자 SQL의 `EXPLAIN` execute/fetch와
실제 result cursor execute/fetch에서만 적용한다. Session setting, reader policy,
resolved-object 검증과 commit에서 같은 SQLSTATE가 발생해도 사용자 SQL 오류로
공개하지 않고 details 없는 `QUERY_UNAVAILABLE`로 fail-closed한다.

### Safe execution contract

실행 순서는 다음 안전 경계로 고정한다.

1. Current source와 tenant requirement 확인
2. Published metadata/SQL policy revision 확인
3. SQL AST와 relation/function/operator/type 검증
4. Source별 concurrency slot 획득
5. Repeatable-read read-only transaction 시작, 첫 settings statement의 transaction-local
   `TimeZone=UTC`, 나머지 limit 적용
6. Reader role/privilege/RLS/tenant/UTC session과 resolved function/operator 재검증; relation은
   published-name allowlist, reader privilege와 planning에서 제한
7. `EXPLAIN` plan budget admission
8. Cursor로 row/byte limit 안에서 fetch하고 canonical encoding
9. 성공 commit, 실패·취소·disconnect에는 database cancel과 rollback

앞 단계를 뒤 단계로 미루거나 검증 실패 뒤 SQL을 실행하지 않는다.

현재 6단계는 transaction-local `row_security=on`, trusted tenant, restricted reader와 published
security-invoker view를 확인하지만 hidden base relation의 RLS flag/policy 의미를 attest하지 않는다.
[RLS policy drift finding](../../verification/2026-08-26-rls-policy-drift.md)은 이 gap에서 다른 tenant
행이 성공함을 재현했으며 strict xfail sentinel로 남아 있다. 이는 안전 contract의 예외 승인이
아니다. Same-transaction lock/check order, provider fingerprint와 public error는 `RLS-01` exact 승인
전 임의로 구현하지 않는다.

### Executor lifecycle contract

```text
RuntimeQueryExecutor extends QueryExecutor:
  stop_accepting() -> None
  async drain(grace_ms: int) -> None
  async invalidate(source_id: str) -> None
  async close() -> None  # inherited
```

- `stop_accepting` 뒤 새 query를 받지 않는다.
- `drain(grace)`는 진행 중 query를 기다린 뒤 남은 query를 취소한다.
- Source generation 변경 시 해당 source pool과 admission state를 `invalidate`한다.
- `close`는 pool과 active task를 누출하지 않는다.

## 소비 계약

- [Source Catalog](../source-catalog/README.md)의 `SourceReader`로 얻는 source profile, budget, tenant
  policy와 reader safety. Profile/semantic graph는 recursively immutable한 read-only 입력이다.
- [Metadata](../metadata/README.md)의 exact recursively immutable published snapshot/revision과 allowed
  relation ceiling
- [Runtime](../runtime/README.md)의 operations reporting contract

Delivery caller는 shared active source contract를 확인한 뒤 generated query ID와 server-derived
tenant를 전달하고 disconnect를 task cancellation로 전파한다. Operator authorization은 Delivery가
cancel 호출 전에 강제하며 Guarded Query는 active query ID 존재 여부만 반환한다. Runtime caller는
shutdown/reload 때 lifecycle capability를 정해진 순서로 호출한다. 이는 Guarded Query가 두 module의
private implementation을 import한다는 뜻이 아니다.

Guarded Query는 Control DB table이나 HTTP/MCP request model을 직접 알지 않는다.
`QueryService`의 registry dependency는 `SourceReader`이며 source projection mutation capability를
소비하지 않는다. Source profile이나 published snapshot을 query 실행 중 in-place 변경하지 않는다.

## 불변조건

- 각 제한은 피해를 실제로 막을 수 있는 경계에서 강제한다. Queue/concurrency와 row/byte는
  application executor가, read-only/timeout/work/temp/session policy는 PostgreSQL transaction이
  강제하며 어느 한쪽을 다른 쪽의 관례로 대체하지 않는다.
- Relation disclosure와 authorization을 질문별 metadata context에만 맡기지 않는다.
- Reader는 최소 권한이고 transaction은 read-only이며 timeout/row/byte/concurrency 상한이 있다.
- Query cancel, timeout, encoding 실패와 client disconnect는 rollback과 slot 반환으로 끝난다.
- Unknown SQL construct, revision drift와 reader-policy drift는 fail-closed한다.
- SQL, bind literal, credential과 원본 database 오류를 response 또는 일반 log에 노출하지 않는다.
- Source별 예외는 Python 분기문이 아니라 profile, budget, curated view로 표현한다.

## 모듈 내부 변경

다음은 validation acceptance, 결과 shape와 lifecycle 의미를 보존할 때 독립적으로 변경할 수 있다.

- 같은 AST acceptance를 만드는 validator 내부 정리
- 동일한 admission 결과를 만드는 plan summary/helper 개선
- Row/byte 계산 결과를 바꾸지 않는 cursor와 encoding 성능 개선
- Public reason code를 유지하는 PostgreSQL exception mapping 정리
- Cancel/rollback/drain 결과를 유지하는 lock과 task bookkeeping 개선

## 사용자 승인이 필요한 계약 변경

- 허용 SQL statement, relation, function, operator 또는 type 범위 변경
- `SQL_POLICY_REVISION` 재료나 revision format 변경
- Query input/output field, canonical encoding, row/byte accounting 또는 truncation 의미 변경
- Authorization/validate/admit/transaction/limit/cancel/rollback 순서 변경
- Timeout, concurrency, plan, row, byte와 memory/temp limit의 의미 또는 완화
- Tenant context, reader session, resolved-object 검사나 fail-closed 정책 변경
- Query error/reason code, 정보 비노출 또는 cancel-not-found 의미 변경
- `QueryExecutor` 또는 `RuntimeQueryExecutor` method/shape와 pool invalidate/drain/shutdown 의미 변경

승인 요청에는 Source Catalog, Metadata, Delivery, Runtime과 Assurance의 직접 consumer 영향 및
rolling request/active query 처리 계획을 포함한다.

## 검증

최소 focused gate:

```text
uv run pytest tests/test_registry.py tests/test_sql_validation.py tests/test_security_evaluation.py \
  tests/test_query.py tests/test_result_encoding.py
```

Result/error contract를 건드리면 HTTP/MCP tests를, concurrency/cancel 경계를 건드리면 load/server
tests를 추가한다. PostgreSQL transaction, reader, RLS 또는 pool 경계를 바꾸면
`uv run pytest -m integration`도 실행한다. Disposable source corner gate는 다음과 같다.

```text
uv run pytest -m integration tests/test_source_database_corners.py
```

완료 전 root `AGENTS.md`의 전체 gate를 실행한다.

## 집중해서 읽을 범위

Guarded Query 작업은 기본적으로 다음만 읽는다.

1. 이 문서와 [module index](../README.md)
2. 변경 대상 query/validator/encoding code와 focused tests
3. Source profile의 소비 필드, published metadata와 reader safety 계약
4. [ADR 0001](../../decisions/0001-postgresql-ast-validation.md),
   [ADR 0002](../../decisions/0002-guarded-query-contract.md),
   [ADR 0003](../../decisions/0003-reader-and-resolved-object-policy.md),
   [ADR 0005](../../decisions/0005-initial-query-budgets.md)와
   [ADR 0014](../../decisions/0014-trusted-rls-tenant-context.md) 중 변경과 직접 관련된 결정
5. 변경되는 result/error/lifecycle의 직접 consumer contract

Control DB persistence와 metadata relevance algorithm은 계약을 변경하지 않는 한 읽을 필요가 없다.
