# Query 제한과 자원 사용

Query Man의 budget은 비용 금액이 아니라 query 한 건과 source pool의 hard limit입니다. Current authority는
`config/budget-profiles.yaml`이며 client가 override할 수 없습니다.

## 강제 계층

| 단계 | 주요 제한 | 실패 결과 |
|---|---|---|
| 요청 | SQL byte, strict JSON, 인증·source authorization | `INVALID_REQUEST`, 인증 오류 |
| AST | 하나의 read-only statement, relation/function/operator/cast allowlist | `QUERY_REJECTED` |
| Admission | Source별 semaphore, queue timeout, pool 크기 | `QUERY_OVERLOADED` 또는 `QUERY_UNAVAILABLE` |
| Transaction | `REPEATABLE READ READ ONLY`, statement/transaction/lock timeout, UTC | timeout 또는 비공개 unavailable |
| Plan | `EXPLAIN` total cost, 최대 rows와 node 수 | `QUERY_REJECTED` |
| Result | Exact OID, row 수, compact UTF-8 JSON byte | bounded result 또는 fail-closed |
| Lifecycle | Timeout, disconnect, shutdown | cancel·rollback·cleanup |

현재 `interactive` profile의 값은 YAML에서 직접 확인합니다. 대표 기본값은 query statement 5초,
transaction 8초, queue 1초, pool/concurrency 2, result 1,000행/1 MiB입니다. 문서의 숫자를 별도
authority로 복제하지 않습니다.

`max_concurrent_queries`는 `max_pool_size`보다 클 수 없습니다. Pool만 늘려도 admission 한도는 늘지
않습니다. Planner cost는 PostgreSQL의 상대 추정치이며 시간이나 돈이 아니므로 timeout과 결과 제한을
대체하지 않습니다.

## 관측값

성공 응답과 safe log는 다음과 같은 bounded 값을 제공합니다.

- `query_id`, source와 pseudonymous caller
- `queue_ms`, `elapsed_ms`
- plan total cost, 최대 rows와 node 수
- `row_count`, `result_bytes`, `truncated`
- public outcome/error code

`/admin/metrics`는 process-local counter snapshot입니다. Durable audit, billing이나 여러 replica의 합계가
아닙니다. SQL, literal, raw row, token, DSN과 database message는 기록하지 않습니다.

## 조사 순서

1. `/ready`와 `/admin/health`에서 metadata/query pool과 source 상태를 확인합니다.
2. Public error를 구분합니다.
   - `QUERY_OVERLOADED`: admission queue가 가득 참
   - `QUERY_TIMEOUT`: deadline 또는 cancel
   - `QUERY_REJECTED`: AST/plan/resource policy 거부
   - `QUERY_UNAVAILABLE`: 비공개 database/infrastructure failure
3. `queue_ms`가 크면 caller 동시성, semaphore와 pool saturation을 봅니다.
4. `elapsed_ms`와 plan 수치가 크면 불필요한 scan, wide projection과 정렬·집계를 줄입니다.
5. `truncated`이면 더 큰 한도를 요청하기 전에 질문과 SQL 범위를 좁힙니다.
6. PostgreSQL 조사 권한이 별도로 승인된 경우에만 DBA 도구로 lock, active session과 aggregate query
   통계를 확인합니다. Raw SQL이나 bind 값을 일반 운영 log로 복사하지 않습니다.

Limit을 늘리기 전에 query shape, view 설계와 caller load를 먼저 고칩니다. 변경이 필요하면 한 번에 한
제약만 조정하고 worst-case memory(`work_mem × plan node × concurrency`)와 temp disk, timeout, rollback을
함께 검증합니다. Source별 임시 override나 요청 parameter는 추가하지 않습니다.

## 안전한 변경

Budget 변경은 policy 의미 변경입니다. 다음을 같은 change-set에서 review합니다.

- `config/budget-profiles.yaml`과 이를 선택하는 source package
- Registry cross-field validation
- Query admission/transaction/plan/result tests
- Bounded load test와 container memory·stop grace
- Rollback할 직전 YAML revision

Protected 환경에서는 approved commit을 고정하고 traffic 밖에서 적용합니다. Timeout, memory, temp file,
row/byte 또는 cancel·rollback 방어가 약해지거나 관측되지 않으면 중단하고 직전 profile로 rollback합니다.
