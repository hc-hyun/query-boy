# Safe Query Verification — 2026-08-22

## Scope

PostgreSQL 18.6 local fixture에서 guarded query의 현재 안전 경계를 검증했다. 이 문서는
완료 선언이 아니라 roadmap 항목별 실행 증거와 남은 gap을 기록한다.

## Environment

- PostgreSQL: `postgres:18.6-bookworm`
- Source: `development_issues`, `market_voc`
- Reader: source별 `NOINHERIT`, `NOBYPASSRLS`, connection limit 3 login
- Data: development issue 600/comment 1,500, market VOC 1,200/comment 3,000
- Budget: interactive profile, source별 concurrency 2, statement 5s, transaction 8s,
  result 1,000 rows/1 MiB

## Commands And Result

```text
uv run ruff check .       PASS
uv run mypy src           PASS
./scripts/apply-db.sh     PASS
uv run pytest             PASS (unit suite)
uv run pytest -m integration
                          PASS (3 PostgreSQL integration tests)
```

## Evidence

| Boundary | Executed evidence | Result |
|---|---|---|
| Reader privilege | Bootstrap validation checks role attributes, base schema, TEMP, CREATE and curated-view SELECT | PASS |
| Revision and AST | Stale revision, write, base relation and unapproved function requests | Rejected before execution |
| Resolved object | `pg_proc`/`pg_operator` candidate namespace, volatility, security-definer and EXECUTE checks | PASS |
| Volatile bypass | Test-only AST allowlist includes `random`; database candidate policy still evaluates it | `QUERY_RESOLVED_FUNCTION_NOT_ALLOWED` |
| Plan admission | Three-way cross join with 216M estimated combinations | Rejected by cost/row threshold |
| Statement timeout | Admission threshold lifted and statement timeout set to 1ms | `QUERY_TIMEOUT`; next query succeeds |
| Result bound | 1,500 comment rows requested | 1,000 rows, `truncated=true`, byte limit preserved |
| Concurrency | One slow query holds a source with concurrency 1; second query queues | `QUERY_OVERLOADED` |
| Source isolation | Market query runs while development source slot is occupied | PASS |
| Cancel/rollback | Active four-way cross join task cancelled, then same source queried again | Cancelled and connection reusable |
| Disconnect | HTTP disconnect probe cancels the application task | PASS in unit test |

## Remaining Gaps

- Initial production budget은 반복 부하 시험과 운영 workload 자료가 없어 아직 provisional이다
  (`DEC-06`).
- Caller별 source authorization이 없어 `EXEC-02`는 미완료다.
- Client disconnect는 application cancellation까지 검증했지만 실제 socket disconnect와
  운영자 query-id cancel 경로를 함께 검증해야 `EXEC-06`, `EXEC-11`을 완료할 수 있다.
- RLS source는 아직 등록하지 않으며 trusted tenant context와 pool reset 검증이 필요하다.
