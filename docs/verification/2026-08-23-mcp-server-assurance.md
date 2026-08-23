# MCP Server Assurance — 2026-08-23

Status: Complete

## Scope And Verdict

이 감사는 [roadmap](../implementation-roadmap.md)의 `MCPX-01`~`MCPX-08`을 닫는다.
In-process MCP adapter만 확인하지 않고 Compose의 published loopback `/mcp`, 공식 MCP Python
client와 실제 PostgreSQL fixture에서 대량·병렬·코너케이스·사용성·비용 통제 경계를
검증했다. 기본 unit suite와 분리된 `mcp_server` marker가 같은 acceptance를 CI container
job에서 재현한다.

## Checklist Evidence

| ID | Primary evidence | Result |
|---|---|---|
| `MCPX-01` | `tests/test_mcp_server.py`, `tests/test_mcp_server_load.py`, pytest marker와 container CI job은 credential 없는 loopback URL만 허용하고 environment proxy를 우회하며 token을 출력하지 않는다. | PASS |
| `MCPX-02` | Quality configuration의 16개 case를 실제 MCP로 순회해 relation accuracy 1.0, answerability recall 1.0과 context byte 상한을 통과한다. | PASS |
| `MCPX-03` | Versioned verified registry의 9개 contract를 실제 context/query로 순회해 exact revision, relation, typed canonical result hash와 unique query ID를 통과한다. | PASS |
| `MCPX-04` | Raw HTTP가 Host 421, Origin 403, 무인증/중복 auth 401, 잘못되거나 중복된 media type 400, 1 MiB 초과 413과 bounded malformed JSON을 확인한다. Tool validation은 oversized SQL, extra field와 type coercion 입력을 반사하지 않는다. | PASS |
| `MCPX-05` | Source/revision/query rejection과 내부 오류는 safe `structuredContent.error`를 유지하면서 `isError=true`이며 strict argument schema가 추가 입력과 암묵적 integer 변환을 거부한다. 기존 object `outputSchema` discovery 계약도 유지한다. | PASS |
| `MCPX-06` | `mcp_tool_started/completed` DEBUG event와 MCP metric이 call ID, tool, protocol, caller/tenant, 허가 source, duration, outcome, public error/reason 및 query ID를 제공한다. Sentinel 회귀는 question/SQL/비인가 source 비기록을, 혼합 global/source metric 회귀는 operator snapshot 가용성을 확인한다. | PASS |
| `MCPX-07` | 한 client의 24개 동시 query와 8개 독립 session이 exact result와 unique ID를 반환한다. 두 development holder가 concurrency 2를 채우면 세 번째 요청은 overload되고 market query는 성공하며 두 holder timeout 뒤 development query가 즉시 복구된다. | PASS |
| `MCPX-08` | 실제 Uvicorn socket에서 modern client call 취소가 client session 종료 전에 hanging executor를 취소하고 같은 client의 후속 tool call이 성공한다. | PASS |

## Executed Evidence

```text
uv run pytest
  PASS (218 passed, 27 deselected)

./scripts/verify-container.sh
  PASS

uv run pytest -m mcp_server -s
  PASS (9 passed in 17.15s)
  quality cases 16; verified contracts 9
  same-client concurrent queries 24 (0.983s; max queue 456ms; max execution 535ms)
  independent sessions 8 (0.974s)
  active development holders 2
  holder outcomes QUERY_TIMEOUT / QUERY_TIMEOUT (5.103s / 5.122s)
  third development outcome QUERY_OVERLOADED (1.049s)
  concurrent market control 1,200 rows counted (0.098s)
  development recovery 600 rows counted (0.073s)

uv run pytest -m integration
  PASS (18 passed in 26.24s; includes modern MCP disconnect cancellation)

uv run ruff check .
uv run mypy src
  PASS
```

Wall-clock 값은 fixture에서 경계가 실제 발동했는지 설명하는 관측치이며 latency SLO gate가
아니다. CI assertion은 exact 결과, 공개 error code, concurrency/connection 불변식과 bounded
timeout에만 의존한다.

## Result Analysis

- 동일 client의 병렬 tool call과 독립 session 모두 query ID가 충돌하지 않았다.
- Source별 concurrency 2와 queue timeout이 development 요청만 제한했고 별도 market source는
  같은 시점에도 정상 응답했다. 이는 process/source별 격리이며 distributed global quota를
  뜻하지 않는다.
- Plan admission을 통과하지만 CPU가 비싼 fixture 식도 statement timeout에서 종료됐다.
  Timeout 이후 slot, connection과 transaction이 정상 query에 재사용됐다.
- MCP application failure가 protocol success처럼 보이던 `isError=false` 문제를 고쳐 agent가
  retry/중단 판단에 사용할 수 있는 표준 error signal과 safe structured detail을 함께 제공한다.
- Debug lifecycle은 병렬 `list_sources/get_context/query` 순서를 구별하고 성공 query의
  `query_id`로 application/DB audit와 연결한다. 입력 원문은 분석 편의를 위해서도 기록하지
  않는다.
- Global과 source별 MCP metric이 같은 이름을 가져도 operator snapshot이 안정적으로 정렬되며,
  존재하지 않는 source 입력은 log나 metric label cardinality로 유입되지 않는다.

## Managed Follow-up Checklist

아래 항목은 현재 stateless/local Compose 보장의 결함으로 숨기지 않고, 요구가 생길 때 별도
roadmap ID와 decision으로 승격한다.

- [ ] Legacy MCP 즉시 취소가 필요하면 stateful session, caller-bound session ownership,
  idle expiry와 multi-replica sticky routing을 함께 설계한다. 현재는 statement/transaction
  timeout이 legacy의 최종 상한이다.
- [ ] 1,000개 이상 session churn, FD/RSS 증가와 multi-replica saturation은 PR gate가 아닌
  scheduled soak 환경과 누수 기준을 먼저 정의한다.
- [ ] Transport→여러 tool call로 이어지는 사용자 workflow correlation이 필요하면 client가
  전달하는 검증된 bounded trace ID 계약을 정의한다. Caller/source/시간으로 추론하지 않는다.
- [ ] 통화 단위 비용은 gateway latency나 planner cost로 가장하지 않고 source database billing,
  DB-native CPU/I/O와 collector retention을 결합한 별도 chargeback 모델로 구현한다.

현재 production 계약 안에서는 실제 Docker MCP의 자동 검증, 입력 비노출, 비용 포화와 modern
disconnect 취소가 구현·CI·운영 문서와 일치한다.
