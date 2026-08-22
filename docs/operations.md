# Operations Guide

## Logging Policy

`query-man` process log는 one-line JSON이다. 공통 필드는 UTC `timestamp`, `level`, `logger`,
`event`이며 query audit의 bounded identifier와 numeric outcome은 top-level JSON field로도
기록한다. Exception은 class name만 `exception_type`으로 기록한다. Traceback, DB error detail,
SQL text와 request body는 운영 log에 기록하지 않는다. Formatter는 방어적으로 bearer 값,
password/credential/token/secret assignment와 quoted SQL literal을 `[REDACTED]`로 바꾼다.
Process는 stdout/stderr 이후의 durable 보관을 제공하지 않으므로 production collector가
retention, access control과 replica identity를 추가해야 한다.

Audit event는 다음 bounded field만 사용한다.

- Query 시작: `query_id`, caller ID, tenant ID, source ID
- Query 성공: 위 식별자, fingerprint, queue/elapsed ms, row/result byte, truncation과 plan cost
- Query 실패/중단: 위 식별자와 공개 가능한 application error code 또는 interrupted 상태
- Cancel 요청: `query_id`, caller ID와 tenant ID
- 인증 실패: HTTP method만 기록하고 bearer token과 path는 기록하지 않음
- 인가 실패: caller ID, tenant ID와 operation만 기록하고 requested source는 기록하지 않음

Executor는 PostgreSQL transaction-local `application_name=query-man:<query_id>`를 설정하므로
동일 query ID로 application audit와 `pg_stat_activity`를 연결할 수 있다.
이는 실행 중 activity correlation이며 완료 뒤 장기 통계가 query ID를 보존한다는 뜻은 아니다.
비싼 query 조사와 `pg_stat_statements` 경계는
[query cost runbook](query-cost-control.md)을 따른다.

## Health And Metrics

Public endpoint는 inventory를 노출하지 않는다.

| Endpoint | Audience | Contract |
|---|---|---|
| `GET /health` | Public/load balancer | Process liveness만 `ok` |
| `GET /ready` | Public/load balancer | 아래 aggregate status만 반환; source ID 없음 |
| `GET /admin/health` | Operator | source별 `initializing`, `healthy`, `stale`, `unavailable` |
| `GET /admin/metrics` | Operator | source/component health와 bounded counter/total snapshot |

Startup은 등록 source별 published-metadata 제공 경로를 각 metadata statement timeout 안에서
병렬 확인한다. TTL 안의 stored active snapshot은 source DB catalog query 없이 복원될 수
있으므로 readiness는 query connection liveness 보장이 아니다. Source health는 마지막 metadata
refresh/restore 결과이며 매 health 요청마다 database에 ping한 결과가 아니다.
Control-plane scan도 별도 component health로 추적한다. Aggregate 의미와 HTTP status는 다음과
같다.

| Status | Meaning | `/ready` HTTP |
|---|---|---:|
| `initializing` | Active inventory가 아직 probe되지 않음 | 503 |
| `ready` | 모든 active source와 reload component가 healthy | 200 |
| `degraded` | 하나 이상의 source는 healthy/stale이지만 일부 source 또는 reload component에 문제 | 200 |
| `unavailable` | healthy/stale source가 하나도 없음 | 503 |
| `shutting_down` | Process가 신규 작업을 받지 않음 | 503 |

여러 source 중 하나의 장애로 전체 gateway replica를 load balancer에서 제거하지 않기 위해
`degraded`는 200이다. Caller가 고른 source가 unavailable이면 해당 `/meta`/query 흐름은
별도로 실패한다. Dynamic deactivate는 처리한 replica의 health inventory에서 즉시 제거되고
다른 replica에는 다음 reload poll 뒤 반영된다. Isolated staging 실패는 현재 production source
health를 변경하지 않는다.

관리 metric 이름은 다음과 같다.

- Metadata: `metadata_refresh_started`, `metadata_refresh_succeeded`,
  `metadata_refresh_failed`, `metadata_validation_rejected`, `metadata_stale_served`
- Queue/execution: `query_queue_ms_count/sum`, `query_execution_started/succeeded`,
  `query_elapsed_ms_count/sum`, `query_queue_rejected`, `query_pool_exhausted`,
  `query_rejected`, `query_revision_rejected`, `query_timeout`, `query_failed`
- Result/cancel: `query_truncated`, `query_cancel_requested`, `query_cancelled`,
  `query_interrupted`, `query_shutdown_cancelled`
- Startup/reload: `startup_metadata_probe_failed`, `source_reload_scan_failed`,
  `source_reload_apply_failed`, `source_reload_metadata_probe_failed`
- Shutdown: `shutdown_started`, `shutdown_drained`, `shutdown_forced_cancel`

Replica별 in-process metric이므로 collector는 operator endpoint를 scrape한 뒤 source/replica
label로 합산한다. Counter 차이로 rate를, `*_sum / *_count`로 구간 평균을 계산할 수 있다.
현재 snapshot만으로 percentile을 복원할 수 없으므로 P95/P99는 audit event histogram 또는
별도 metric instrumentation이 필요하다. Process restart 때 in-memory 값은 초기화된다.

## Alert Policy

| Signal | Warning | Critical / action |
|---|---|---|
| Metadata refresh failure | source별 5분에 3회 또는 `stale` 전이 | `unavailable` 즉시 on-call |
| Validation reject | 새 generation 1회 | 동일 source 3회 연속이면 publish 중지·마지막 정상 generation 확인 |
| Query reject | 10분 baseline의 3배 또는 20/min | 공격/잘못된 client 배포 확인; response/audit reason별 조사 |
| Queue pressure | 평균 queue가 timeout의 50% | 80% 또는 `query_pool_exhausted` 5회/5분이면 admission/budget 점검 |
| Timeout | source별 5분에 3회 | `Δquery_timeout / Δquery_execution_started`가 5분간 5% 초과 시 expensive fingerprint와 DB activity 확인 |
| Truncation | `Δquery_truncated / Δquery_execution_succeeded`가 10분간 10% | 25%면 질문/aggregation/limit 계약 검토; limit 즉시 상향 금지 |
| Forced shutdown cancel | 1회 | grace, 장기 query와 배포 drain 순서 조사 |

외부 collector를 구성하면 현재 endpoint에서 execution/reject/timeout/truncation rate,
queue/elapsed 평균과 source/component status를 source별로, shutdown outcome을 replica별로
계산할 수 있다. Exact
stale age, active pool gauge, row/byte distribution과 percentile은 현재 제공하지 않으므로
그 panel이 필요하면 먼저 계측을 추가한다. Public dashboard에는 source label을 노출하지
않는다.

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

표준 `uv run query-man` entrypoint의 SIGTERM/SIGINT handler 진입 시 readiness와 executor
admission을 즉시 닫은 뒤 Uvicorn이
listener를 종료한다. 이미 연결된 요청이 application middleware에 도달하면
`503 SERVICE_SHUTTING_DOWN`이고, 새 connection은 listener 상태에 따라 거절될 수 있다.
Uvicorn graceful timeout은 `QUERY_MAN_SHUTDOWN_GRACE_MS`를 초 단위로 올림한 값이며 그 안에서
이미 수락한 HTTP/MCP task 완료를 기다린다. 기한을 넘으면 Uvicorn이 task를 취소하고 executor가
PostgreSQL cancel과 rollback을 수행한다. Lifespan의 drain/close는 남은 queue·pool wait·active
query를 정리하는 마지막 안전 경계다. Orchestrator termination grace는 이 값과 process 종료
overhead보다 길게 설정한다.
