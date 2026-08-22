# Safe Query Verification — 2026-08-22

## Scope

PostgreSQL 18.6 local fixture에서 guarded query의 현재 안전 경계를 검증했다. 이 문서는
당시 safe-query milestone의 실행 증거를 기록한다. 이후 milestone의 완료 상태는
[completion audit](2026-08-23-completion-audit.md)을 따른다.

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
                          PASS (5 integration tests, including load, live socket and PostgreSQL)
uv run pytest -m load -s PASS (40 concurrent cross-source queries plus metadata refresh)
uv run query-man-verify PASS (9/9 golden questions)
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
| Cancel/rollback | Active four-way cross join cancelled by task and operator query ID, then same source queried again | Cancelled and connection reusable |
| Disconnect | Uvicorn TCP socket closes while a query is active | ASGI disconnect cancels the application task |
| Source authorization | Caller allowlist filters `/sources` and denies `/meta`, `/query` before catalog/executor | PASS; denied and unknown source share 404 |
| Initial budget load | 40 concurrent queries across two sources with metadata refresh | 0 errors; observed queue max 641ms, elapsed max 729ms |
| Golden regression | 4 development + 5 market questions | Revision, AST relations, columns, row count and result hash all match |

## Historical Boundaries And Follow-Up

- 현재 budget은 local fixture의 초기 hard limit이며 production SLO로 일반화하지 않는다.
- 당시 미구현이던 RLS trusted tenant context와 pool reset은 `AUTH-05`~`AUTH-06`에서
  구현·통합 검증했다.
