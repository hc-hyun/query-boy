# MCP Multi-Replica Soak — 2026-08-23

Status: Complete

## Scope And Verdict

[active development TODO](../development-todo.md)의 `SOAK-01`~`SOAK-07`을 닫았다. 같은
application image를 쓰는 두 Compose replica와 실제 PostgreSQL fixture에서 modern-only MCP
경계, exact result, 동시 포화·복구와 1,000개 stateless session의 process resource 변화를
검증했다.

## Checklist Evidence

| ID | Evidence | Result |
|---|---|---|
| `SOAK-01` | Parent `/mcp` boundary와 raw HTTP 회귀가 정확히 `2026-07-28` 한 개만 허용하고 누락, `2025-11-25`, 미지원 값과 중복 header를 `-32022`로 거부한다. 공식 client fixture와 container verification도 같은 version을 고정한다. | PASS |
| `SOAK-02` | `compose.yaml`의 기본 runtime은 `query-man` 하나이며 `--profile soak`에서만 `query-man-replica`를 loopback port 3001에 추가한다. | PASS |
| `SOAK-03` | 두 replica가 같은 세 tool의 전체 input/output schema와 같은 revision을 반환하고 verified query 16개가 exact result와 서로 다른 query ID를 냈다. | PASS |
| `SOAK-04` | Replica마다 development query 두 개를 유지해 총 네 slot을 채웠을 때 추가 요청은 각 replica에서 `QUERY_OVERLOADED`, market control은 성공, 네 holder는 `QUERY_TIMEOUT`이었고 이후 두 replica가 정상 복구됐다. | PASS |
| `SOAK-05` | Warm-up 100개 뒤 900개를 실행해 총 1,000 session을 replica별 정확히 500개씩 처리했다. 동시성은 20이다. | PASS |
| `SOAK-06` | 두 application process 모두 PID 유지, restart 0, OOM false였다. Baseline 대비 FD 증가는 0 이하고 RSS 증가는 최대 256 KiB였다. Gate는 FD +8, RSS baseline +32 MiB 및 후반 +16 MiB다. | PASS |
| `SOAK-07` | 별도 `soak` marker와 주간·수동 `.github/workflows/mcp-soak.yml`을 추가하고 일반 PR container suite에서는 제외했다. | PASS |

## Executed Evidence

```text
uv run pytest -m soak -s -q
  PASS (3 passed in 66.18s)
  replicas 2; queries 16; unique query IDs 16; wall 1,063ms

cross-replica source saturation
  active holders 4; QUERY_TIMEOUT 4; QUERY_OVERLOADED 2
  market control 152ms; total 5,605ms; recovery exact on both replicas

1,000-session resource soak
  sessions 1,000; primary 500; replica 500; parallelism 20; wall 54,016ms
  observed latency p50 661ms; p95 928ms; max 1,162ms
  primary: pid 7; fd 11 -> 7; rss 102,340 KiB -> 102,340 KiB; restart 0; OOM false
  replica: pid 7; fd 11 -> 7; rss 102,232 KiB -> 102,488 KiB; restart 0; OOM false
```

Latency는 환경 설명을 위한 관측치이고 SLO gate가 아니다. Assertion은 exact count/result,
분배, 공개 error, process 생존과 resource growth 상한에만 의존한다.

## Capacity Boundary

Fixture reader role의 connection limit 7은 replica마다 query pool 2와 metadata pool 1, 그리고
publish staging 1을 합친 두 replica 구성에 정확히 맞는다.

```text
2 replicas × (query pool 2 + metadata pool 1) + staging 1 = 7
```

따라서 이 감사는 최대 두 replica만 보장한다. 세 번째 replica, 여러 replica를 합친 global
quota, load balancer fairness나 sticky routing은 검증하지 않았다. Replica 수를 늘리기 전에
reader connection budget과 routing 정책을 함께 다시 결정해야 한다.
