# Query Cost And Resource Control

## Scope

Query Man이 직접 통제하는 대상은 query가 소비할 수 있는 database 자원, 동시 처리량과
응답 크기다. PostgreSQL `EXPLAIN total_cost`는 planner의 상대 추정치이며 실행 시간이나
클라우드 요금이 아니다. 통화 단위 비용은 database/host billing과 사용량을 source 단위로
결합해 외부 cost system에서 계산한다.

## Enforced Layers

`config/budget-profiles.yaml`의 budget schema `version: 2` profile을 모든 source에 같은 순서로
적용한다. 이는 source manifest schema `version: 2`와 별도 계약이다. `budget_profile`은
유일한 resource tier이며 관리자가 source마다 기존 profile 하나를 선택한다. 같은 source의
모든 query 사용자는 같은 profile 정의를 쓴다. 별도 `cost_tier`나 caller/user/organization
override는 없다. 현재 `interactive` 값은
[ADR 0005](decisions/0005-initial-query-budgets.md)에 고정한다.

| Layer | Enforced control | Failure or result signal |
|---|---|---|
| SQL boundary | 단일 read-only statement, relation/function/operator allowlist | `QUERY_REJECTED`와 bounded reason code |
| Plan admission | `total_cost`, 최대 plan rows, plan node 수 | `QUERY_PLAN_COST_EXCEEDED`, `QUERY_PLAN_ROWS_EXCEEDED`, `QUERY_PLAN_NODES_EXCEEDED` |
| Time | statement 5s, transaction 8s, lock 250ms, queue 1s | `QUERY_TIMEOUT` 또는 `QUERY_OVERLOADED` |
| Memory/temp/CPU shape | `work_mem=8MiB`, `temp_file_limit=64MiB`, parallel gather 0, JIT off | 적용값 재검증 실패 시 `QUERY_UNAVAILABLE` |
| Capacity | replica/source당 query concurrency 2와 pool 2, reader role connection hard cap | queue/pool reject metric |
| Result | 최대 1,000 rows와 compact JSON 1MiB | bounded rows와 `truncated=true` |
| Intervention | `query_id`를 active source와 대조한 admin cancel | PostgreSQL cancel, rollback, cancel metric |

Plan admission은 명백히 큰 추정치를 실행 전에 거르는 첫 방어선이다. 통계 오차, 함수
실행 비용, lock과 I/O 변동을 모두 예측하지 못하므로 time/resource/capacity/result 경계를
대체하지 않는다. 반대로 plan threshold를 통과했다는 사실은 query가 저렴하다는 보장이
아니다.

`work_mem`은 plan node와 동시 연산별로 소비되고 hash operation에는 PostgreSQL의
`hash_mem_multiplier`도 적용될 수 있어 process 전체 메모리 상한과 같지 않다. 그래서 query
concurrency, parallel worker와 reader connection limit를 함께 제한한다. `temp_file_limit`은
한 PostgreSQL process가 sort/hash 등에 만든 임시 파일 상한이며 명시적 temporary relation이나
source storage quota를 뜻하지 않는다. Reader는 별도로 database TEMP 권한도 갖지 않는다.

현재 경계는 replica 전체의 distributed source quota, caller/tenant별 quota·fairness,
user/organization별 tier, host cgroup CPU/memory quota와 일·월 통화 budget을 제공하지 않는다.
이는 초기 운영의 명시적 deferred scope이며 미리 assignment table이나 counter를 만들지 않는다.

두 replica의 독립 concurrency·connection 경계와 session resource 누수는
[multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md)에서 검증한다.
이 결과는 distributed global quota를 뜻하지 않는다. DB-native 비용 귀속의 구현 순서와
종료 조건은 [active development TODO](development-todo.md)의 `COST-*`에서 관리한다. Source
규모·증가량의 측정 방법과 gateway usage lower-bound 수집은 `CTRL-07`에서 구현됐고, 최종 운영 projection은
[source management plane](source-management-plane.md)의 한 management surface에서 제공한다.
`CTRL-08`은 비용 신호를 `not_configured|pending|available|stale|unavailable`로 구분하고 마지막
시도 시각과 bounded reason을 제공하며, missing/failed 값을 0으로 표시하지 않는다. 이 항목들은
public projection 구현 목표이며 DB-native/provider monetary collector는 여전히 범위 밖이다.

## What Is Measured

각 성공 응답과 `query_succeeded` audit event는 다음 값을 같은 `query_id`와 fingerprint에
연결한다.

- `queue_ms`, `elapsed_ms`
- `plan_summary.total_cost`, `max_rows`, `node_count`
- `row_count`, `result_bytes`, `truncated`

운영 rollup은 bounded한
`source_id + budget_profile + metadata_revision + time bucket`을 기본 key로 사용한다. Budget
정의는 metadata revision 재료이므로 별도 tier revision entity를 만들지 않는다.
`pg_stat_statements`의 query ID와 gateway fingerprint는 정확히 대응한다고 가정하지 않는다.
Caller/tenant는 security audit에는 남을 수 있지만 비용, quota 또는 metric label dimension으로
쓰지 않는다.

`query_failed` event는 같은 식별자와 공개 가능한 application `error_code`/`reason_code`를
기록한다. AST 검증 전에는 fingerprint가 없을 수 있고, executor에 진입한 실패는 같은
`query_id`의 `query_execution_failed` event가 fingerprint를 연결한다. Plan reject는 observed
cost/rows/nodes와 적용 threshold도 별도 event로 남긴다. SQL text, literal과 database error
detail은 기록하지 않는다. 수정 가능한 고정 SQLSTATE는 `QUERY_INVALID`와 `query_invalid`
metric으로 분리하고 알 수 없거나 권한·인프라 관련 오류는 계속 `QUERY_UNAVAILABLE`로 숨긴다.
`/admin/metrics`는 replica-local
counter와 `queue_ms`/`elapsed_ms`의 count·sum만 제공한다. 따라서 평균은 `sum / count`로
계산할 수 있지만 percentile, active pool gauge, stale age와 monetary cost는 이 endpoint에
존재하지 않는다. P95/P99가 필요하면 audit event를 histogram collector로 보내거나 별도
instrumentation을 추가해야 한다.

Audit는 process JSON log에 기록될 뿐 repository가 durable store를 제공하지 않는다. Query별
history를 운영 근거로 쓸 때는 top-level JSON field를 보존하는 collector, retention과 접근
통제를 먼저 구성한다. `elapsed_ms`는 DB connection을 얻은 뒤 transaction 실행 구간이고
`queue_ms`는 source semaphore 대기다. MCP는 별도의 HTTP lifecycle event/metric으로 request
arrival부터 response start와 final ASGI body 전달까지를 측정해 pool/connect, SDK dispatch와
serialization을 포함한다. 이 값도 client 수신, decode, tool scheduling과 model 응답 시간은
포함하지 않는다.

통화 단위 source cost projection은 같은 기간의 database/cluster billing을 gateway의 source별
성공 수, elapsed 합계와 database-native I/O/CPU 지표에 결합할 수 있다. 공유 cluster에서는
배분 추정일 뿐 query별 정확한 원가가 아니므로 방법과 오차를 함께 표시한다.
User/organization별 chargeback은 현재 제공하지 않는다.

## Live Investigation

1. 응답 또는 audit에서 `query_id`, source, fingerprint와 error/reject reason을 확보한다.
2. 실행 중 query만 monitoring identity로 확인한다. Application reader에 통계 전역 권한을
   추가하지 않는다.

   ```sql
   SELECT pid, datname, usename, application_name, state,
          wait_event_type, wait_event,
          pg_catalog.clock_timestamp() - query_start AS running_for
   FROM pg_catalog.pg_stat_activity
   WHERE datname = :'database_name'
     AND usename = :'reader_role'
     AND application_name = 'query-man:' || :'query_id'
     AND backend_type = 'client backend';
   ```

   `application_name`은 인증 식별자가 아니므로 database와 reader role을 함께 제한한다.
   기본 조회에서는 SQL literal 노출을 피하려고 `query` text를 선택하지 않는다.

3. 즉시 피해를 줄여야 하면 별도 admin credential로 gateway cancel 경계를 사용한다.

   ```text
   DELETE /queries/<query_id>
   ```

   Application reader와 query user에게 `pg_cancel_backend` 또는 Query Man cancel 권한을 주지
   않는다.
4. `/admin/metrics`에서 같은 source의 queue, pool exhaustion, timeout, reject와 truncation
   변화를 확인한다. `plan_summary`가 높은지와 실제 elapsed/I/O가 높은지는 별도로 판단한다.
5. 선택적으로 DBA가 `pg_stat_statements`를 운영한다. 이는 normalized statement의 장기
   calls/time/rows/shared block/temp block 집계를 제공하지만 Query Man `query_id` 또는 pglast
   fingerprint와 직접 연결되지 않는다. Gateway는 connection-local 고정 cursor 이름을 써
   request UUID별 entry 폭증은 막지만, `DECLARE`와 `FETCH` 통계가 원래 SELECT와 분리되거나
   여러 fingerprint 사이에서 합쳐질 수 있다. 따라서 reader/source aggregate 보조 신호로만
   사용한다. Extension과 monitoring role은 source owner가 별도로 관리하고 query text나
   통계를 Query Man API에 공개하지 않는다.

### Optional PostgreSQL Aggregate

Production source마다 DBA가 extension과 계측 overhead를 별도로 승인한다. Preload 설정 변경은
database restart가 필요할 수 있다.

```sql
SHOW shared_preload_libraries;
SHOW compute_query_id;
SHOW track_io_timing;
SHOW pg_stat_statements.track;
SHOW pg_stat_statements.track_planning;
SHOW pg_stat_statements.max;

SELECT extversion
FROM pg_catalog.pg_extension
WHERE extname = 'pg_stat_statements';

SELECT stats_reset, dealloc
FROM public.pg_stat_statements_info;
```

Application reader에는 `pg_read_all_stats`, `pg_monitor` 또는 `pg_signal_backend`를 부여하지
않는다. 별도 monitoring identity가 필요하면 source owner가 `CONNECT`, `pg_read_all_stats`와
extension view의 좁은 조회 권한만 review한다. 이 identity는 다른 session 통계를 볼 수 있는
민감한 운영 계정이다.

```sql
SELECT stats.queryid, stats.calls,
       stats.total_exec_time, stats.mean_exec_time, stats.max_exec_time,
       stats.rows, stats.shared_blks_read,
       stats.temp_blks_read, stats.temp_blks_written,
       stats.parallel_workers_to_launch, stats.parallel_workers_launched
FROM public.pg_stat_statements AS stats
JOIN pg_catalog.pg_roles AS role ON role.oid = stats.userid
JOIN pg_catalog.pg_database AS database_row ON database_row.oid = stats.dbid
WHERE role.rolname = '<reader_role>'
  AND database_row.datname = pg_catalog.current_database()
ORDER BY stats.total_exec_time DESC
LIMIT 20;
```

이 통계에는 metadata/session-policy/`EXPLAIN`/`DECLARE`/`FETCH`가 함께 섞이고 reset 또는
entry eviction이 발생할 수 있다. `stats_reset`/`dealloc`과 같이 수집하며 query text는
literal을 포함할 수 있으므로 dashboard 기본 필드로 저장하지 않는다. PostgreSQL query ID는
major version이나 object OID 변화에 걸친 stable application identifier가 아니다.

## Remediation Order

비싼 query가 발견되어도 limit부터 올리지 않는다.

1. 잘못된 grain, fanout join, 불필요한 wide column과 무제한 상세 결과를 먼저 고친다.
2. Source owner와 통계 freshness, 승인 view의 predicate/aggregation, 필요한 index 또는
   materialized view를 검토한다.
3. Production에서 무제한 `EXPLAIN ANALYZE`를 실행하지 않는다. 격리 replica나 대표 fixture로
   결과 정확성, plan과 부하를 재검증한다.
4. Verified query, `uv run query-man-verify`, integration과
   `uv run pytest -m 'load and not mcp_server' -s`를 통과시킨다. Compose MCP 경계를 바꾸면
   `uv run pytest -m 'mcp_server and not soak' -s`의 실제 server
   saturation도 통과시킨다. 이 검증은 source concurrency 2를 채운 상태의 세 번째 요청이
   `QUERY_OVERLOADED`, 5초 statement 상한을 넘긴 실행이 `QUERY_TIMEOUT`, 다른 source와
   후속 정상 query가 계속 성공하는지 확인한다.
5. 그래도 profile 변경이 필요하면 concurrency를 포함한 최악 자원량과 reader connection
   capacity를 review한다. Profile 변경은 metadata revision 재발행과 L2 verified contract
   재검증을 요구한다.

Source별 특별 숫자를 Python 분기나 manifest 임의 override로 넣지 않는다. 공통 workload가
현재 profile과 다를 때만 중앙 profile을 추가하고 절대 schema bounds, representative load와
운영 승인을 함께 남긴다.
