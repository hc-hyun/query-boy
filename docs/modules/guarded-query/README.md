# Guarded Query Module

Status: Physical package boundary active

## 목적

### 30초 요약

Guarded Query는 외부 SQL을 PostgreSQL AST로 검사하고 current metadata revision과 source authorization을
확인한 뒤, 제한된 read-only transaction에서 plan을 admit해 실행한다. Timeout, overload, client
disconnect와 shutdown에도 cancel·rollback·connection cleanup을 보장한다.

## 소유 책임

- SQL AST, source/schema/relation/function/operator/cast allowlist
- Metadata/SQL policy revision 일치 확인
- Queue/pool admission, plan cost/row/node와 session resource limit
- Read-only execution, streaming result row/byte bound와 exact seven OID encoding
- Active query cancel, drain, rollback과 connection cleanup
- Literal-free diagnostic SQL rendering와 stable public query reason

## 소유하지 않는 책임

- Source YAML parsing, metadata catalog collection과 caller 인증·인가
- HTTP/MCP request/response envelope
- Runtime configuration/composition, logging backend와 diagnostic storage
- Query usage를 Control DB에 기록하거나 source generation을 reload하는 기능

## 현재 코드 위치

| 위치 | 책임 |
|---|---|
| [`guarded_query/sql_validation.py`](../../../src/query_man/guarded_query/sql_validation.py) | AST parser/walker, allowlist와 SQL policy revision |
| [`guarded_query/query.py`](../../../src/query_man/guarded_query/query.py) | Application service, PostgreSQL executor, admission/cancel/drain/close |
| [`guarded_query/result_encoding.py`](../../../src/query_man/guarded_query/result_encoding.py) | Exact OID validation과 canonical value encoding |
| [`guarded_query/diagnostics.py`](../../../src/query_man/guarded_query/diagnostics.py) | Literal-free diagnostic rendering |
| [`test_sql_validation.py`](../../../tests/test_sql_validation.py), [`test_query.py`](../../../tests/test_query.py), [`test_result_encoding.py`](../../../tests/test_result_encoding.py) | Focused tests |

## 제공 인터페이스와 소유 경계

`QueryService.query(...)`, `QueryExecutor`와 Delivery lifecycle용 `DeliveryQueryExecutor`가 public Python
interface다. Concrete `PostgresQueryExecutor` 조립은 Runtime과 offline Assurance CLI만 수행한다.

`validate_sql`은 single read-only statement, approved relation/function/operator/type, bounded bytes와
current SQL policy를 강제한다. `SQL_POLICY_REVISION`과 canonical material은 Metadata revision이 소비하는
policy identity다.

`diagnostics.redact_sql_literals`는 Runtime diagnostic adapter가 소비하는 literal-free rendering
entrypoint다. Parse할 수 없거나 multi-statement인 SQL은 capture하지 않는 기존 fail-closed 의미를
유지하며 diagnostic storage나 consent 판단은 소유하지 않는다.

정확한 PostgreSQL grammar/fingerprint 경계는 [ADR 0001](../../decisions/0001-postgresql-ast-validation.md),
query result/error·admission·cancel 계약은 [ADR 0002](../../decisions/0002-guarded-query-contract.md),
reader/resolved-object 검사는 [ADR 0003](../../decisions/0003-reader-and-resolved-object-policy.md)에 있습니다.

### 현재 launch policy identity

SQL policy v3, current `SQL_POLICY_REVISION`과 exact seven result OID가 current launch identity다. 이
heading은 source extension 문서와 과거 review link의 stable target이기도 하다.

### Canonical time identity

Reader transaction은 `REPEATABLE READ READ ONLY` 직후 transaction-local `TimeZone=UTC`를 설정하고,
catalog·resolved-object 검사·`EXPLAIN`·user SQL보다 먼저 exact UTC를 검증합니다. Commit, rollback,
cancel과 pool reuse 뒤 local setting이 남지 않아야 하며 database/role default를 바꾸지 않습니다.

Canonical-time policy material version 1은 aware `datetime`만 UTC로 변환해 `+00:00` ISO로 표현하고
`Z`로 축약하지 않습니다. Naive `datetime`, `date`, `time`, `timetz`의 기존 ISO 표현은 보존하며 mapping과
sequence에도 재귀적으로 같은 규칙을 적용합니다. 이 material은 SQL-policy digest와 모든 metadata
revision에 들어가며 stale token은 executor 진입 전에 fail-closed합니다.

Transport UTC와 한국 업무 달력은 별개입니다. `ai.issue_overview.discovered_on`은
`(discovered_at AT TIME ZONE 'Asia/Seoul')::date`, `ai.voc_overview.received_on`은
`(received_at AT TIME ZONE 'Asia/Seoul')::date`이고 월별 VOC SQL은 SELECT/GROUP BY 모두
`date_trunc('month', received_at, 'Asia/Seoul')`을 사용합니다. 이 의미가 바뀌면 두 source의 metadata
revision과 9개 verified query를 함께 재검토합니다. 과거 managed coordinated-cutover 절차는 retired됐고
현재 rollout/rollback은 ADR 0025와 Operations를 따릅니다.

Application result는 stable `columns`, `rows`, `row_count`, `metadata_revision`, truncation/limit 정보로
구성한다. `QueryRejectedError`, `QueryInvalidError`, `QueryOverloadedError`, `QueryTimeoutError`,
`QueryUnavailableError`의 발생 의미는 Guarded Query가 소유하고 Delivery가 external status/code/message로
render한다. Parser/driver/database 내부 error와 SQL literal은 공개하지 않는다.

실행 순서는 authorize된 source와 current revision 확인, queue/pool admission, connection/session policy,
read-only transaction, AST/plan admission, declared result OID 확인, bounded fetch, commit 또는
cancel/rollback/cleanup이다. Client disconnect와 shutdown은 active backend query를 취소하고 rollback한다.

Process-local semaphore 입장 대기 초과는 caller 부하로 분류해 `QUERY_OVERLOADED`(429)를 유지한다.
Semaphore를 통과한 뒤 pool이 connection을 공급하지 못하거나 PostgreSQL connection/session policy가
깨지면 details 없는 `QUERY_UNAVAILABLE`(503)로 분류하고 해당 source의 query health를
`unavailable`로 내린다. 같은 query 경로의 성공한 `COMMIT`만 query health를 복구한다. SQL invalid/rejection,
statement timeout, cancel과 unsupported result 같은 caller-triggerable/fail-closed 실패는 source health를
내리지 않으며, 실패한 SQL을 자동으로 재실행하지 않는다.

## 소비 인터페이스와 전제

| Provider | 소비 항목 | 전제 |
|---|---|---|
| Source Catalog | `SourceReader`, source budget/allowlist, reader policy | Caller가 지정한 DSN이나 limit을 사용하지 않음 |
| Metadata | Published revision/catalog relations | Revision mismatch에서 재해석하지 않고 거부 |
| Runtime | Operations sink와 pool composition | Operations 실패가 query cleanup을 깨뜨리지 않음 |

Delivery는 application/lifecycle interface만 소비하며 PostgreSQL executor private method를 호출하지 않는다.

## 불변조건

- 외부 SQL은 실행 전에 AST와 allowlist를 통과한다.
- Source/schema/relation/function/operator, result OID와 budgets는 gateway와 PostgreSQL에서 강제한다.
- Transaction은 read-only이며 timezone, timeout, memory/temp, parallel/JIT policy를 transaction-local로 적용한다.
- Source Catalog가 resolve한 exact `disable`/`require`/`verify-full` mode를 query pool의 libpq
  `sslmode`로 전달하고 `gssencmode=disable`을 적용하며 ambient default로 재해석하지 않는다. SQL 전에
  실제 TLS state가 reviewed mode와 일치하는지도 확인한다.
- Current metadata/SQL policy revision이 다르면 DB 접근 전에 fail-closed한다.
- Row/byte/OID limit은 결과를 client에 보내기 전에 검사한다.
- Timeout, cancel, disconnect, error와 shutdown은 rollback하고 pool connection을 반환한다.
- Queue 과부하와 DB dependency 장애를 각각 429와 details 없는 503으로 구분하고, 성공한 commit 전에는
  query health를 복구하지 않는다.
- Credential, Authorization header, SQL literal과 내부 DB error를 응답/log에 기록하지 않는다.

## 모듈 내부 변경

Public semantics와 실행/cleanup 순서를 보존하는 private AST helper, plan traversal, batch size와 lock 구현은
module 내부 변경이다. Security parser와 cancellation path에는 runnable regression test를 남긴다.

## 사용자 승인이 필요한 경계 변경

- Query lifecycle outcome과 domain error code/retry semantics
- SQL policy version/revision, AST/function/operator/cast/relation allowlist
- Plan, queue, timeout, row/byte/OID와 PostgreSQL session limits
- Metadata revision match, transaction/cancel/rollback/drain/close invariant
- Result canonical encoding과 HTTP/MCP-visible query fields

## 검증

```bash
uv run pytest tests/test_sql_validation.py tests/test_query.py tests/test_result_encoding.py
```

Connection, cancel, disconnect 또는 PostgreSQL type 경계를 바꾸면 integration/database-corner tests를
추가한다.

## 집중해서 읽을 범위

| 변경 | 먼저 읽을 범위 |
|---|---|
| SQL syntax/policy | `sql_validation.py`, security corpus, `test_sql_validation.py` |
| Execution/admission/cancel | `query.py`, Delivery/Runtime direct consumer, `test_query.py`, integration disconnect test |
| Result type/encoding | `result_encoding.py`, verified hash consumer, `test_result_encoding.py` |
| Revision | Metadata revision producer/consumer와 `test_revision.py` |
| Diagnostic SQL | `diagnostics.py`, capture adapter/direct tests |
