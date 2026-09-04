# ADR 0002: Guarded Query External Behavior And Safety

Status: Accepted

## Context

AI-generated SQL, stale metadata, expensive plans, large results and disconnects must cross one gateway that
enforces policy independently of client behavior.

## Decision

`POST /query` accepts only the following application input.

```text
source_id, sql, metadata_revision, sql_policy_revision
```

Success returns no original SQL and contains `status`, `query_id`, both revisions, literal-free `fingerprint`,
columns/rows, row and byte counts, `truncated`, queue/elapsed time and bounded plan summary.

Execution order is fixed.

1. Authenticate and authorize source access.
2. Compare current metadata and SQL policy revisions; mismatch returns `409 METADATA_REVISION_MISMATCH`.
3. Parse one read-only SQL statement and enforce AST/relation/function/operator/cast allowlists.
4. Acquire the source concurrency slot and reader connection within bounded queue time.
5. Begin `REPEATABLE READ READ ONLY`, apply UTC and resource settings, then revalidate DB/session/reader state.
6. Resolve referenced objects and function/operator candidates against live PostgreSQL catalog.
7. Run `EXPLAIN (FORMAT JSON)` and enforce cost, rows and node limits.
8. Reject duplicate result column names and unsupported result OIDs.
9. Fetch bounded batches until row or compact UTF-8 JSON byte limit.
10. Commit success; cancel and rollback timeout, cancellation, disconnect, shutdown and every failure path.

Current successful result OIDs and canonical values are defined by
[ADR 0025](0025-static-non-rls-first-launch.md). `result_bytes` counts the compact JSON `rows` array, including
brackets and separators. A row that crosses the limit is omitted and sets `truncated=true`.

## Public errors

| HTTP | Code | Meaning |
|---:|---|---|
| 400 | `QUERY_REJECTED` | AST, object or plan policy rejection |
| 400 | `QUERY_INVALID` | Safely classified, client-correctable SQL meaning error |
| 404 | `SOURCE_NOT_FOUND` | Source is absent or not disclosed to caller |
| 408 | `QUERY_TIMEOUT` | Deadline or cancellation |
| 409 | `METADATA_REVISION_MISMATCH` | Context revision is stale |
| 429 | `QUERY_OVERLOADED` | Source admission queue is full |
| 503 | `QUERY_UNAVAILABLE` | Hidden database/infrastructure/result failure |

`QUERY_INVALID.details` contains only a bounded server-authored reason, `action=CORRECT_SQL` and retry hint.
Supported reason codes cover undefined column, invalid cast, division by zero, invalid limit/regular expression,
numeric range, function argument/usage and function signature mismatch. PostgreSQL message, identifier, SQL
snippet, literal and parser location are never reflected.

Privilege, connection/session, commit, unknown SQLSTATE, driver and serialization failures remain detail-free
`QUERY_UNAVAILABLE`. Retry hints never authorize repeating unchanged SQL or bypassing policy.

## Resource and lifecycle rules

- Plan admission complements rather than replaces transaction timeout, concurrency and result bounds.
- `max_concurrent_queries <= max_pool_size`; pool availability failure after admission is unavailable, not overload.
- Query ID is created before execution and joins safe audit with PostgreSQL `application_name`.
- Completion log may include source, pseudonymous caller, fingerprint, timing, rows/bytes and public outcome, but
  never SQL, literal, token, DSN or database detail.
- Active-query cleanup and pool return are serialized; a failed or cancelled transaction is never reused before
  rollback.
- Process-local limits do not promise a shared multi-replica quota.

Budget operation is documented in [Query 제한](../query-cost-control.md). Reader/object rules are
[ADR 0003](0003-reader-and-resolved-object-policy.md), AST policy is
[ADR 0001](0001-postgresql-ast-validation.md).
