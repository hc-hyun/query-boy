# Operations Guide

## Logging Policy

`query_man`과 MCP application/audit logger는 one-line JSON을 기록한다. 공통 필드는 UTC
`timestamp`, `level`, `logger`, `event`이며 query audit의 bounded identifier와 numeric
outcome은 top-level JSON field로도 기록한다. Uvicorn lifecycle/access line은 Uvicorn의 text
format이므로 container stdout은 line-oriented mixed format이다. Exception은 class name만
`exception_type`으로 기록한다. Query Man formatter는 방어적으로 bearer 값,
password/credential/token/secret assignment와 quoted SQL literal을 `[REDACTED]`로 바꾼다.
SQL text와 request body는 application/audit log에 기록하지 않는다. Process는 stdout/stderr
이후의 durable 보관을 제공하지 않으므로 production collector가 retention, access control과
replica identity를 추가해야 한다.

Audit event는 다음 bounded field만 사용한다.

- MCP HTTP 완료(INFO): server-generated `mcp_http_request_id`, response-start/final-body duration,
  response byte, HTTP status와 outcome
- MCP tool 시작(DEBUG)/완료(INFO): 같은 `mcp_http_request_id`, server-generated `mcp_call_id`,
  고정 tool name, protocol, caller/tenant, 허가된 source, duration, outcome과 공개
  error/reason code
- Query 시작: `query_id`, caller ID, tenant ID, source ID
- Query 성공: 위 식별자, fingerprint, queue/elapsed ms, row/result byte, truncation과 plan cost
- Query 실패/중단: 위 식별자와 공개 가능한 application error code 또는 interrupted 상태
- Cancel 요청: `query_id`, caller ID와 tenant ID
- 인증 실패: HTTP method만 기록하고 bearer token과 path는 기록하지 않음
- 인가 실패: caller ID, tenant ID와 operation만 기록하고 requested source는 기록하지 않음

MCP 로그는 question, SQL, 전체 arguments, header, body, token 또는 비인가 source를 기록하지
않는다. HTTP request ID로 request lifecycle과 tool completion을, 성공한 MCP `query` 완료
event의 `query_id`로 같은 query audit를 연결할 수 있다. INFO의 HTTP/tool 완료 duration은 각각
final ASGI body 전달과 tool 내부 반환까지이며 client 수신·decode나 model 재개 시각은 아니다.
Tool 시작 순서까지 필요할 때만 `.env`의 `QUERY_MAN_LOG_LEVEL=debug`를 설정해 application
container를 재생성하고 `docker compose logs -f query-man`으로 본다. 장기 운영의 기본 INFO
수준을 debug로 올린 채 두지 않는다.

분 단위 지연을 조사할 때는 같은 request ID의 HTTP duration과 tool duration, 같은 query ID의
`queue_ms`/`elapsed_ms`를 순서대로 비교한다. HTTP와 tool이 모두 짧은데 다음 request arrival
전 공백이 길면 Query Man timeout, pool 또는 concurrency를 조정하지 않고 client/tool scheduler와
model-side trace를 확인한다.

Executor는 PostgreSQL transaction-local `application_name=query-man:<query_id>`를 설정하므로
동일 query ID로 application audit와 `pg_stat_activity`를 연결할 수 있다.
이는 실행 중 activity correlation이며 완료 뒤 장기 통계가 query ID를 보존한다는 뜻은 아니다.
비싼 query 조사와 `pg_stat_statements` 경계는
[query cost runbook](query-cost-control.md)을 따른다.

## Control DB Migration And Environment Isolation

Control DB는 production, development와 integration test가 서로 다른 physical database/DSN을
사용한다. Production 관리자는 대상 identity를 확인한 뒤 database owner용 표준 libpq
`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSFILE` 또는 managed authentication을 설정하고
다음을 실행한다. Password나 전체 DSN을 command argument 또는 change log에 넣지 않는다.

```bash
./scripts/apply-control-schema.sh
```

Runner는 `docker/postgres/init/control-migrations`의 연속된 `NNNN_name.sql`만 번호순으로
적용한다. `control.schema_migrations`에는 filename과 SHA-256 checksum을 기록하고, 각 pending
migration의 DDL과 ledger insert를 같은 transaction 및 database advisory lock 안에서 처리한다.
재실행은 이미 적용된 migration을 건너뛰며 repository와 DB의 filename/checksum이 다르거나 DB가
현재 checkout보다 앞서 있으면 DDL 전에 fail-closed한다. 적용된 migration 파일이나 ledger를
수정해 맞추지 말고 새 forward migration을 추가한다. Schema downgrade는 지원하지 않는다.

Schema migration과 global `query_man_control_writer`/DB ACL은 의도적으로 분리돼 있다.
`reconcile-security.sql`은 매 실행마다 role을 harden하고 현재 DB의 최소 권한을 복구한다. 따라서
`pg_dump --no-privileges` restore에서도 pending migration이 없어도 ACL이 복구된다. Runtime
writer에는 migration ledger, schema CREATE 또는 DDL 권한을 부여하지 않는다.

Local/CI의 `apply-db.sh`는 development fixture DB에 같은 runner를 적용하지만 production
migration 명령이 아니다. Control-store와 hot-add integration test는
`query_man_control_test_<random>` DB를 test마다 생성하고, 모든 pool을 닫은 뒤 삭제하며 전후
development authority fingerprint가 동일한지 확인한다. CI가 비정상 종료돼 scratch DB가 남으면
해당 ephemeral Compose volume을 폐기한다. 운영 DB나 사용자가 지정한 임의 DB를 test cleanup
대상으로 삼지 않는다.

## Source Authority Startup And Cutover

Runtime은 process 전체의 source authority를 한 mode로 고정한다.

| Mode | Valid source configuration | Authentication |
|---|---|---|
| `bootstrap` (default) | Control DSN/key 없음 | Loopback anonymous, query-only API token 또는 version 2 policy |
| `managed` | Control DSN/key 모두 있음 | Version 2 policy file 필수; query/admin identity 분리 |

Bootstrap에 Control 설정이 하나라도 있거나 managed에 둘 중 하나가 빠지면 configuration error로
시작하지 않는다. Mode를 `auto`로 추론하거나 source별로 섞지 않는다. Managed startup은 source
directory와 filesystem verified-query file을 열지 않지만 budget profile과 configured
authentication/access policy는 계속 deployment configuration에서 읽는다.

Access-policy version 2 caller는 `caller_id`, `tenant_id`, `token_env`와 `operator`만 선언한다.
모든 인증 identity는 모든 active source를 보며 source별 scope나 grant가 없다. Version 1,
`allowed_sources`와 `all_sources`가 남은 file은 자동으로 권한을 넓히지 않고 startup에서
거부한다. Managed mode에는 최소 한 개의 non-admin query identity와 한 개의 explicit operator
admin identity가 필요하며 `QUERY_MAN_API_TOKEN`과 anonymous local identity를 허용하지 않는다.
Bootstrap의 anonymous/API-token identity는 query-only다. `operator`는 query 권한에 admin API와
cancel을 추가하는 capability superset이고 별도 role hierarchy는 아니다.

Managed lifespan은 empty registry/verified map에서 Control DB를 scan한 뒤 enabled generation을
decrypt·validate하고 stored metadata/quality gate를 통과한 source만 적용한다. Disabled lifecycle은
registry에서 제거하며 lifecycle이 없는 file source는 absent다. Cold-start scan 실패에는 보존할
verified state가 없으므로 `/ready`가 `unavailable`이고 file로 fallback하지 않는다. 한 번 적용한
process의 이후 poll이 실패하면 마지막 verified registry를 유지하고 reload component를
`unavailable`로 표시한다. 사용 가능한 source가 있으면 aggregate readiness는 `degraded`다.

### Existing Bootstrap Source Cutover

1. Production과 분리된 migration/admin identity로 Control schema를 적용하고 runtime writer,
   encryption key recovery와 version 2 query/admin access policy를 확인한다.
2. Query traffic을 받지 않는 managed instance를 직접 시작한다. Empty Control DB에서는
   `/ready` 503이 정상이며 admin endpoint는 별도 운영 경로로 호출한다.
3. 기존 admin API로 source를 L0/L1 staged publish한다. Reader credential은 external secret
   boundary에서 관리자가 전달하고 응답 generation/metadata revision을 기록한다.
4. 기존 reviewed contract를 verified-query admin endpoint로 실행·저장한다. 현재 revision과
   invariant가 다르면 이관을 중단하고 재검토한다.
5. 같은 semantic/budget revision에서 L2 generation을 publish하고 `/meta`, guarded query와
   intended inactive state를 확인한다.
6. 필요한 모든 source와 L2 contract가 Control DB에 있는지 확인한 뒤 serving replica를
   `QUERY_MAN_SOURCE_MODE=managed`로 재시작한다. Source/verified file이 없어도 같은 inventory와
   revision이 복원되고 deactivate/rollback이 유지되는지 확인한 뒤 traffic을 전환한다.

이 절차는 startup import나 새 bulk endpoint가 아니다. Seed digest/import marker와 repository
write-back을 만들지 않으며, authoritative mutation receipt가 구현되기 전 timeout 응답을 blind
retry하지 않고 Control DB state부터 확인한다.

## Health And Metrics

Public endpoint는 inventory를 노출하지 않는다.

| Endpoint | Audience | Contract |
|---|---|---|
| `GET /health` | Public/load balancer | Process liveness만 `ok` |
| `GET /ready` | Public/load balancer | 아래 aggregate status만 반환; source ID 없음 |
| `GET /admin/health` | Query Man admin | source별 `initializing`, `healthy`, `stale`, `unavailable` |
| `GET /admin/metrics` | Query Man admin | source/component health와 bounded counter/total snapshot |

Startup은 authority mode에서 등록된 source별 published-metadata 제공 경로를 각 metadata
statement timeout 안에서 병렬 확인한다. Managed mode는 이 probe 전에 Control lifecycle scan을
수행한다. TTL 안의 stored active snapshot은 source DB catalog query 없이 복원될 수
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
  `query_rejected`, `query_invalid`, `query_revision_rejected`, `query_timeout`, `query_failed`
- Result/cancel: `query_truncated`, `query_cancel_requested`, `query_cancelled`,
  `query_interrupted`, `query_shutdown_cancelled`
- MCP HTTP: `mcp_http_request_started/completed/failed/cancelled`,
  `mcp_http_request_duration_ms_count/sum`, `mcp_http_response_started_ms_count/sum`,
  `mcp_http_response_bytes_count/sum`
- MCP tool: `mcp_tool_started`, source별 `mcp_tool_completed`, `mcp_tool_failed`,
  `mcp_tool_cancelled`, `mcp_tool_duration_ms_count/sum`
- Startup/reload: `startup_metadata_probe_failed`, `source_reload_scan_failed`,
  `source_reload_apply_failed`, `source_reload_metadata_probe_failed`
- Shutdown: `shutdown_started`, `shutdown_drained`, `shutdown_forced_cancel`

Replica별 in-process metric이므로 collector는 admin endpoint를 scrape한 뒤 source/replica
label로 합산한다. Counter 차이로 rate를, `*_sum / *_count`로 구간 평균을 계산할 수 있다.
현재 snapshot만으로 percentile을 복원할 수 없으므로 P95/P99는 audit event histogram 또는
별도 metric instrumentation이 필요하다. Process restart 때 in-memory 값은 초기화된다.

## Local Container Operations

`docker compose up -d --wait postgres`, `./scripts/apply-db.sh`,
`docker compose up -d --build --wait query-man` 순서로 database 변경을 먼저 적용한 뒤
application을 시작한다. `/ready`가 `ready` 또는 `degraded`를 반환하면 container는
healthy지만 release smoke에서는 body가 `{"status":"ready"}`인지 별도로 확인한다.
Application port는 container 내부 `3000`, host loopback의 `${QUERY_MAN_PORT:-3000}`이며
PostgreSQL은 container network에서 `postgres:5432`다.

Compose는 `QUERY_MAN_SOURCE_MODE=bootstrap`이고 access policy는
`QUERY_MAN_CODEX_MCP_TOKEN`을 모든 active bootstrap source를 보는 query-only caller로 만들고
operator 권한을 주지 않는다. Token과 reader password는 `.env`에서 주입하지만
image build context와 Git에는 포함하지 않는다. Application container에는 PostgreSQL
administrator password를 전달하지 않는다. 기본 Compose는 control-plane DSN/key를 주입하지
않으므로 source admin endpoint가 비활성인 local runtime이다. Managed source test나 production
설정을 이 Compose default와 섞지 않는다.

Container는 non-root, read-only filesystem과 `/tmp` tmpfs로 실행한다. 기본 Docker
`stop_grace_period` 30초는 application drain 기본값 10초보다 길다.
`QUERY_MAN_SHUTDOWN_GRACE_MS`를 30초에 가깝거나 그 이상으로 바꾸면 orchestrator grace도
process 종료 overhead를 포함해 더 크게 조정한다. Runtime log는
`docker compose logs -f query-man`으로 읽고 종료는 `docker compose down`을 사용한다.
`./scripts/verify-container.sh`는 exact readiness, 무인증 401, non-root/read-only image 경계와
공식 MCP client의 tool discovery 및 실제 guarded query를 한 번에 검증한다.
`uv run pytest -m 'mcp_server and not soak' -s`는 같은 published loopback endpoint에서 전체
metadata 품질 case, verified query, raw protocol/보안 경계, 입력 비노출, 병렬 session과 비용
포화·복구를 검증한다. 이 suite는 실행 중인 Compose application을 변경하지 않으며 token을
출력하지 않는다.

MCP transport는 bind 주소와 관계없이 DNS rebinding 보호를 활성화한다. 기본 Compose는
loopback Host/Origin만 허용한다. Reverse proxy나 외부 hostname으로 배포할 때는
`QUERY_MAN_MCP_ALLOWED_HOSTS`와 `QUERY_MAN_MCP_ALLOWED_ORIGINS`를 실제 공개 Host와 HTTPS
Origin의 comma-separated allowlist로 명시하며 wildcard 전체 허용을 사용하지 않는다.
MCP POST는 정확한 `application/json` media type 하나와 `mcp-protocol-version: 2026-07-28`
하나만 허용한다. Media type parameter는 허용하지만 prefix 변형, 누락·이전·미지원 protocol
version과 중복 Content-Type/Authorization/protocol header는 거부한다. 이전 initialize
handshake를 위한 compatibility path는 운영하지 않는다.

Codex CLI 0.149.0 client project는 `.codex/config.toml`의
`features.mcp_2026_07_28 = true`로 이 protocol을 명시적으로 활성화한다. Client project에서
`codex features list`를 실행해 값이 `true`인지 확인하고, token은 값 자체를 출력하지 않은 채
현재 shell 환경에 존재하는지만 확인한다. `.env` 파일만 생성해서는 Codex process에 자동
전달되지 않는다. Client `.env`에는 MCP token만 두며 database credential을 복사하지 않는다.
`shell_environment_policy.filters.QUERY_MAN_CODEX_MCP_TOKEN = "exclude"`를 설정해 Codex가
실행하는 shell command에는 token이 상속되지 않게 한다. Codex upgrade 뒤에는 실제 startup과
`/mcp` tool inventory를 다시 확인하며, flag가 기본값이 되거나 제거되면 project override도
삭제한다.

지원 protocol의 JSON response 경로에서는 Query Man이 ASGI disconnect를 직접 감시해 실행 중
query를 취소·rollback한다. Database statement/transaction timeout은 최종 실행 상한이다.

두 replica 내구성 검증은 기본 Compose topology를 바꾸지 않는 `soak` profile로 실행한다.

```bash
docker compose --profile soak build query-man
docker compose --profile soak up -d --no-build --wait query-man query-man-replica
uv run pytest -m soak -s
```

이 suite는 1,000개 stateless session의 정확한 500/500 분배, 양쪽 source 포화·복구, PID,
restart/OOM, FD와 RSS growth를 검사한다. 주간·수동 workflow에서 실행하며 일반 PR gate에는
포함하지 않는다. 현재 reader connection limit은
`2 replicas × (query pool 2 + metadata pool 1) + staging 1 = 7`에 맞으므로 세 번째 replica를
추가하기 전에 connection budget을 재검토한다. 상세 threshold와 최근 증거는
[multi-replica soak audit](verification/2026-08-23-mcp-multi-replica-soak.md)을 따른다.

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
PostgreSQL fixture 및 Query Man application image의 수정 가능한 Critical vulnerability scan을
수행한다. Dependabot은
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
