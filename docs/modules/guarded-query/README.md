# Guarded Query Module

Status: Active

## 30초 요약

Guarded Query는 SQL을 PostgreSQL AST와 live object policy로 검증하고, 최소 권한 reader의 bounded
read-only transaction에서 실행합니다. Timeout·cancel·disconnect·shutdown은 rollback과 cleanup으로
끝나야 합니다.

## 책임과 interface

- SQL policy revision과 relation/function/operator/cast allowlist
- `QueryService.query(...)`: revisions 확인, validation, admission과 execution
- `PostgresQueryExecutor.drain(...)`, `close(...)`: active query lifecycle
- `PostgresQueryExecutor`: reader pool, transaction/session validation, plan과 result limit
- Exact result OID와 canonical JSON encoding

실행은 revision check → AST validation → source semaphore/pool → `REPEATABLE READ READ ONLY` transaction
→ UTC/resource settings → resolved-object check → bounded plan → batched result 순서입니다. 성공만 commit하고
그 밖의 경로는 cancel·rollback 뒤 connection을 반환합니다.

## 코드 지도

| 위치 | 책임 |
|---|---|
| `guarded_query/sql_validation.py` | Parser/walker, allowlist와 SQL policy revision |
| `guarded_query/query.py` | Service, PostgreSQL executor, admission/cancel/rollback |
| `guarded_query/result_encoding.py` | Exact OID와 canonical result encoding |

Metadata는 current snapshot/revisions를, Source Catalog는 reader와 budget을 제공합니다. Delivery는 public
query error를 render하지만 PostgreSQL detail을 받지 않습니다.

## 불변조건과 승인

- 한 개의 read-only `SELECT`/`WITH`와 allowlisted object만 실행합니다.
- Parser, AST 직렬화·순회 또는 fingerprint 계산의 재귀 한계 오류는 실행 전에 기존
  `400 QUERY_REJECTED`와 `SQL_PARSE_ERROR` reason으로 거부합니다.
- Query는 current metadata와 SQL policy revision 둘 다 일치해야 합니다.
- Timeout, concurrency, plan, memory/temp, row/byte와 OID limit을 우회하지 않습니다.
- SQL, literal, credential과 database error detail을 public response/log에 노출하지 않습니다.
- AST/allowlist, revision, result format, public error와 cancel/rollback outcome 변경은 별도 승인 대상입니다.

External behavior는 [ADR 0002](../../decisions/0002-guarded-query-contract.md), AST는
[ADR 0001](../../decisions/0001-postgresql-ast-validation.md), reader는
[ADR 0003](../../decisions/0003-reader-and-resolved-object-policy.md)를 따릅니다.

## 검증

```bash
uv run pytest tests/test_sql_validation.py tests/test_query.py tests/test_result_encoding.py
uv run pytest -m integration tests/test_database_integration.py
```

## 집중해서 읽을 범위

Parser/allowlist는 `sql_validation.py`와 security corpus, execution/limit/cancel은 `query.py`와 query,
integration/load tests, result type은 `result_encoding.py`, HTTP response test까지 읽습니다.
