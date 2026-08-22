# ADR 0005: Initial Query Budgets

Status: Accepted

Date: 2026-08-22

## Context

Planner cost는 시간이나 금액이 아니며 환경과 통계에 따라 달라진다. 그래도 초기
gateway는 무제한 실행보다 검증된 보수적 hard limit이 필요하다. Source가 manifest에
임의 숫자를 직접 넣게 하면 전체 안전 상한을 우회할 수 있다.

## Decision

초기 `interactive` profile은 다음 경계를 강제한다.

| Limit | Value | Role |
|---|---:|---|
| Query concurrency | source당 2 | 동시에 DB 자원을 쓰는 query 상한 |
| Query pool | source당 2 | concurrency와 동일한 connection 상한 |
| Metadata pool | source당 1 | single-flight refresh 전용 |
| Reader connection limit | source당 3 | query 2 + metadata 1의 DB hard cap |
| Queue timeout | 1,000ms | 대기 요청의 빠른 overload 반환 |
| Statement timeout | 5,000ms | 한 SQL statement 실행 상한 |
| Transaction timeout | 8,000ms | setup, plan, fetch, commit 전체 상한 |
| Lock timeout | 250ms | 분석 query의 lock 대기 상한 |
| Result | 1,000 rows / 1MiB | 응답 메모리·전송 상한 |
| SQL text | 100,000 UTF-8 bytes | parser 입력 상한 |
| Plan | cost 100,000 / rows 1,000,000 / nodes 100 | 명백히 비싼 plan의 보조 admission |

Source manifest는 숫자를 직접 override하지 않고 중앙의 versioned budget profile 이름만
참조한다. 신규 profile은 registry schema의 절대 상한, representative load test와
review를 통과해야 한다.

2026-08-22 PostgreSQL 18.6 fixture에서 metadata refresh와 함께 source당 20개, 총 40개
grouped query를 동시에 제출했다.

```text
elapsed p50 <= 483ms, p95 <= 712ms, max 729ms
queue   p95 <= 600ms, max 641ms
max plan total_cost 215.55
timeout 0, overload 0, error 0
```

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
