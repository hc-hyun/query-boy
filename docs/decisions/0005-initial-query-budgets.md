# ADR 0005: Initial Query Budgets

Status: Accepted

Date: 2026-08-22

## Context

Planner cost는 시간이나 금액이 아니며 환경과 통계에 따라 달라진다. 그래도 초기
gateway는 무제한 실행보다 검증된 보수적 hard limit이 필요하다. Source가 manifest에
임의 숫자를 직접 넣게 하면 전체 안전 상한을 우회할 수 있다.

## Decision

Budget schema version 2의 `interactive` profile은 다음 경계를 강제한다.

| Limit | Value | Role |
|---|---:|---|
| Query concurrency | source당 2 | 동시에 DB 자원을 쓰는 query 상한 |
| Query pool | source당 2 | concurrency와 동일한 connection 상한 |
| Metadata pool | source당 1 | single-flight refresh 전용 |
| Reader connection limit | fixture source당 7 | replica 2 × (query 2 + metadata 1) + staging 1 |
| Queue timeout | 1,000ms | 대기 요청의 빠른 overload 반환 |
| Statement timeout | 5,000ms | 한 SQL statement 실행 상한 |
| Transaction timeout | 8,000ms | setup, plan, fetch, commit 전체 상한 |
| Lock timeout | 250ms | 분석 query의 lock 대기 상한 |
| Work memory | operation당 8MiB | 정렬/hash 등의 transaction-local memory 기준 |
| Temporary file | backend당 64MiB | spill 피해의 transaction-local hard cap |
| Parallel/JIT | gather worker 0 / JIT off | query 하나의 CPU 증폭과 compile overhead 차단 |
| Result | 1,000 rows / 1MiB | 응답 메모리·전송 상한 |
| SQL text | 100,000 UTF-8 bytes | parser 입력 상한 |
| Plan | cost 100,000 / rows 1,000,000 / nodes 100 | 명백히 비싼 plan의 보조 admission |

Source manifest는 숫자를 직접 override하지 않고 중앙의 versioned budget profile 이름만
참조한다. 신규 profile은 registry schema의 절대 상한, representative load test와
review를 통과해야 한다. Query와 metadata transaction은 `work_mem`, `temp_file_limit`,
`max_parallel_workers_per_gather`, `jit`를 local setting으로 적용한 뒤 effective 값을
재검증한다. Reader가 필요한 parameter `SET` 권한을 잃거나 값이 drift하면 fail-closed한다.

`budget_profile`은 Query Man의 유일한 resource tier다. 관리자가 source별로 기존 profile 하나를
선택하며 그 source를 사용하는 모든 query principal에게 같은 profile 정의가 적용된다.
Caller/user/organization별 override나 별도 `cost_tier`를 만들지 않는다. 이 profile은 실행 시간,
동시성, 결과 크기 같은 피해 상한이지 가격표나 chargeback 등급이 아니다. 운영 rollup은 적용된
profile 이름과 budget 정의를 포함한 metadata revision을 남길 수 있지만 provider billing 없이
이를 통화 비용으로 환산하지 않는다.

Reader connection capacity는 `replica 수 × (query pool + metadata pool) + 동시 staging`으로
계산한다. 현재 acceptance는 두 replica와 한 staging connection을 동시에 사용하므로 7이다.
Replica를 늘리려면 role hard cap과 database `max_connections` 여유를 먼저 다시 산정한다.

2026-08-22 PostgreSQL 18.6 fixture에서 metadata refresh와 함께 source당 20개, 총 40개
grouped query를 동시에 제출했다.

```text
observed service-call wall p50 483ms, p95 712ms, max 729ms
observed source queue      p95 600ms, max 641ms
max plan total_cost 215.55
timeout 0, overload 0, error 0
```

이 당시 harness의 elapsed 표기는 metadata/revision 확인, source queue, pool wait와 DB 실행을
포함한 `QueryService.query` 전체 wall-clock이었다. API 응답의 DB connection 획득 이후
`elapsed_ms`와는 다른 측정이며, 이후 harness는 두 값을 별도 field로 출력한다.

별도 saturation test는 concurrency 1 profile에서 두 번째 동일-source query를 queue
timeout으로 거부하면서 다른 source query가 성공함을 확인한다. 216M 조합 cross join은
plan admission으로 거부되고, admission을 test에서 해제한 query는 statement timeout으로
중단된다.

## Consequences

- 이 측정은 초기 안전값의 근거이지 production latency SLO가 아니다. Hardware,
  dataset 또는 workload가 달라지면 profile별로 같은 test를 다시 실행한다.
- Replica가 여러 개면 process-local concurrency를 replica 수만큼 곱하게 된다. Shared
  source quota가 필요할 때 distributed limiter 없이는 replica 수를 늘리지 않는다.
- Plan threshold는 timeout, connection, row/byte hard limit을 대체하지 않는다.
- Budget 전체는 metadata revision 재료다. 더 엄격하거나 느슨한 실행 정책으로 바꾸면
  기존 L2 verified contract를 새 revision에서 다시 실행해 승인해야 한다.
- 통화 단위 비용과 운영 조사 절차는
  [query cost runbook](../query-cost-control.md)을 따른다.
