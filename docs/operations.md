# Operations Guide

## Logging Policy

`query-man` process log는 one-line JSON이다. 공통 필드는 UTC `timestamp`, `level`, `logger`,
`event`이며 exception은 class name만 `exception_type`으로 기록한다. Traceback, DB error detail,
SQL text와 request body는 운영 log에 기록하지 않는다. Formatter는 방어적으로 bearer 값,
password/credential/token/secret assignment와 quoted SQL literal을 `[REDACTED]`로 바꾼다.

Audit event는 다음 식별자만 사용한다.

- 성공 query/cancel: `query_id`, caller ID, tenant ID, source ID
- 인증 실패: HTTP method만 기록하고 bearer token과 path는 기록하지 않음
- 인가 실패: caller ID, tenant ID와 operation만 기록하고 requested source는 기록하지 않음

Executor는 PostgreSQL transaction-local `application_name=query-man:<query_id>`를 설정하므로
동일 query ID로 application audit와 `pg_stat_activity`를 연결할 수 있다.

## Health And Metrics

Public endpoint는 inventory를 노출하지 않는다.

| Endpoint | Audience | Contract |
|---|---|---|
| `GET /health` | Public/load balancer | Process liveness만 `ok` |
| `GET /ready` | Public/load balancer | `ready`, `degraded`, `shutting_down`; source ID 없음 |
| `GET /admin/health` | Operator | source별 `healthy`, `stale`, `unavailable` |
| `GET /admin/metrics` | Operator | source label을 포함한 bounded counter/total snapshot |

관리 metric 이름은 다음과 같다.

- Metadata: `metadata_refresh_started`, `metadata_refresh_succeeded`,
  `metadata_refresh_failed`, `metadata_validation_rejected`, `metadata_stale_served`
- Queue/execution: `query_queue_ms_count/sum`, `query_execution_started/succeeded`,
  `query_elapsed_ms_count/sum`, `query_pool_exhausted`, `query_rejected`, `query_timeout`
- Result/cancel: `query_truncated`, `query_cancel_requested`, `query_cancelled`
- Shutdown: `shutdown_started`, `shutdown_drained`, `shutdown_forced_cancel`

Replica별 in-process metric이므로 collector는 operator endpoint를 scrape한 뒤 source/replica
label로 합산한다. 장기 rate와 percentile은 collector에서 계산한다.

## Alert Policy

| Signal | Warning | Critical / action |
|---|---|---|
| Metadata refresh failure | source별 5분에 3회 | stale 상한의 80% 도달 또는 unavailable 즉시 on-call |
| Validation reject | 새 generation 1회 | 동일 source 3회 연속이면 publish 중지·마지막 정상 generation 확인 |
| Query reject | 10분 baseline의 3배 또는 20/min | 공격/잘못된 client 배포 확인; reason code별 분리 |
| Queue pressure | 평균 queue가 timeout의 50% | 80% 또는 `query_pool_exhausted` 5회/5분이면 admission/budget 점검 |
| Timeout | source별 5분에 3회 | 5분 failure rate 5% 초과 시 expensive fingerprint와 DB activity 확인 |
| Truncation | 10분 결과의 10% | 25%면 질문/aggregation/limit 계약 검토; limit 즉시 상향 금지 |
| Forced shutdown cancel | 1회 | grace, 장기 query와 배포 drain 순서 조사 |

Dashboard는 request rate, reject reason, queue/elapsed, row/byte truncation, metadata generation,
stale age, pool pressure와 shutdown outcome을 source별로 제공한다. Public dashboard에는 source
label을 노출하지 않는다.

## Security Update Policy

CI는 locked Python dependency audit, Git history secret scan, repository filesystem/config scan과
PostgreSQL fixture image의 수정 가능한 Critical vulnerability scan을 수행한다. Dependabot은
Python, GitHub Actions와 Docker dependency를 매주 확인한다.

보안 finding은 dependency/image 업데이트로 먼저 해소한다. 도달 불가능한 upstream image
component만 `.trivyignore.yaml`에 정확한 path, 근거와 만료일을 지정할 수 있다. 만료일 없는
예외와 CVE 전체 범위 예외는 허용하지 않는다. 현재 `gosu` 예외는 entrypoint의 로컬 UID/GID
전환만 수행해 TLS session-resumption 코드가 실행되지 않는다는 근거로 제한했으며,
2026-09-30 전에 공식 PostgreSQL image rebuild 여부를 재검토한다.

## Graceful Shutdown

Shutdown 시작 시 readiness는 `shutting_down`이 되고 신규 HTTP/MCP 요청은
`503 SERVICE_SHUTTING_DOWN`으로 거부된다. Executor도 신규 query를 닫은 뒤
`QUERY_MAN_SHUTDOWN_GRACE_MS` 동안 active query 완료를 기다린다. 기한 뒤 남은 connection은
cancel하고 transaction rollback을 거친 후 pool을 닫는다. Orchestrator termination grace는
이 값보다 길게 설정한다.
