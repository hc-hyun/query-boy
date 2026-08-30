# Operations Guide

Status: current Git-reviewed YAML first-launch runbook

이 문서는 목적에 맞는 절만 읽습니다. 낯선 말은 [용어 사전](glossary.md)을 참고하세요.

| 상황 | 읽을 절 |
|---|---|
| 실제 첫 오픈 준비·전환 | [Static Non-RLS First Launch](#static-non-rls-first-launch) |
| AuthBridge bearer 인증 준비 | [Resource Server JWT 계약](resource-server-jwt-auth.md) |
| 상태·log·diagnostic·source YAML 조회 | [Interactive Operator Shell](#interactive-operator-shell) |
| Core health·metric·alert 조사 | [Health](#health-and-metrics), [Alert](#alert-policy) |
| 로컬 Compose와 MCP 확인 | [Local Container Operations](#local-container-operations) |
| 안전한 process 종료 | [Graceful Shutdown](#graceful-shutdown) |

현재 source·verified query·budget authority는 Git-reviewed YAML 하나뿐입니다. Runtime admin
mutation, Control DB, hot reload과 source convergence 운영 절차는 제공하지 않습니다.
정확한 결정 기준은 [ADR 0030](decisions/0030-git-reviewed-yaml-source-authority.md)입니다.
실제 protected environment 전환은 [Active TODO](development-todo.md)의 `LAUNCH-02`입니다.

현재 application은 reviewed Git revision, pinned artifact와 외부 secret 설정으로 복구합니다. Source
업무 데이터의 backup·restore는 각 source DB owner의 정책에 따릅니다. 남아 있는 과거 Control DB,
backup, credential 또는 key는 이 runbook으로 폐기하지 않습니다. Exact inventory, retention, target,
access scope, rollback과 change-record 책임을 정한 별도 protected-operation 승인이 필요합니다.

## Static Non-RLS First Launch

[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 first launch는 다음 exact profile만 대상으로 합니다.

- `development-issues`, `market-voc` 두 source
- Git-reviewed `config/sources/*.yaml`, `config/verified-queries.yaml`, `config/budget-profiles.yaml`
- PostgreSQL 18, server/client UTF-8, RLS source 0개
- final result OID `20, 21, 23, 25, 1082, 1184, 1700`
- SQL policy v3와 repository의 9개 verified query
- 단일 Query Man replica, private Docker network와 loopback listener

Repository 변경 승인은 실제 환경 실행 승인이 아닙니다. 실행 전 change record에 target,
operator access, authentication authority, TLS/secret/backup, source·DDL·role/settings inventory,
approved Git commit, upstream/application image digest, route, stop/rollback 조건과 책임자를
기록합니다. AuthBridge를 선택하면 exact issuer, Query Man 전용 audience/scope mapper, CA
trust와 client token 취득·refresh owner도 기록합니다.

### Artifact preparation

```bash
git status --short
git rev-parse HEAD
export QUERY_MAN_VCS_REF=<approved-40-hex-git-commit>
docker compose config --quiet
docker compose build --build-arg QUERY_MAN_VCS_REF="$QUERY_MAN_VCS_REF" query-man
docker image inspect query-man:local \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
```

Application image revision/digest와 approved commit이 다르면 중단합니다. `soak` profile은 first-launch
artifact acceptance에서 시작하지 않습니다.

### Traffic-off acceptance

Source YAML, budget/access policy, source DDL/view/function/operator/type/collation/extension, reader
role/grant와 database/role/server setting을 승인 inventory와 비교합니다. YAML은 secret 값이
아니라 환경 변수 이름만 포함해야 합니다.

```bash
uv run qm source validate
docker compose up -d --wait postgres query-man
./scripts/verify-container.sh
uv run query-man-verify
```

Exact readiness, 두 source, RLS 0개, PostgreSQL 18/UTF-8, metadata revision, 9/9 verified query,
seven-OID positive/negative와 HTTP/MCP parity를 모두 확인합니다. AuthBridge를 선택하면 JWT
access token 서명·issuer·audience·exp/nbf·scope/role를 Discovery의 `jwks_uri`로 로컬
검증하고 ID token, refresh token, 다른 audience와 만료 token을 거부합니다. JWKS는 cache하되
알 수 없는 `kid`가 오면 한 번 갱신합니다. Authorization header와 token을 log에 남기지
않습니다.

`degraded`, inventory/RLS/result type/revision/hash drift, mixed SQL policy, rollback 미검증은
stop condition입니다.

### Cutover and rollback

Old route를 닫고 신규 유입, active query와 source connection을 drain한 뒤 accepted single
replica만 route합니다. Rollback은 route 차단 → new replica drain → 직전 image/config/SQL
policy와 source inventory 복구 → readiness/verified baseline 확인 → route 순서입니다. 실제
결과는 승인된 environment change record에 append-only/immutable하게 남깁니다. Repository에는
날짜별 PASS 문서를 만들지 않고 exact commit과 CI provenance만 연결합니다.

## Interactive Operator Shell

```bash
uv run qm
```

`status`, `logs`, `diag`는 runtime 상태·log·동의 기반 diagnostic capture를 다룹니다. `source`는
현재 checkout의 local YAML을 읽는 read-only 명령입니다.

```text
qm> status
qm> status metrics
qm> logs --event query_failed --since 2h
qm> source list
qm> source show market-voc
qm> source validate
```

`source list/show/validate`는 server, DB 또는 repository를 변경하지 않습니다. 현재 파일의 strict
schema, source ID 충돌, verified query 참조와 budget profile 참조를 검사하는 로컬 도구입니다.
Source 변경은 YAML pull request와 배포로만 반영합니다.

`logs`의 기본 window/limit는 30분/50건이고 최대 31일/1,000건입니다. `diag list`는 기본 1시간/20건,
최대 7일/100건입니다. 모든 출력은 bounded하며 secret, token, raw request body와 내부 DB 오류를
표시하지 않습니다.

`diag show`는 question 원문이 terminal에 나타날 수 있고 `diag purge`는 복구할 수 없으므로
reason과 exact confirmation이 필요합니다. Protected 환경의 실제 조회·삭제는 별도 operational
approval과 change record를 요구합니다.

## Logging Policy

Application/audit log는 one-line JSON을 기록하며 SQL text, question, request body, bearer token,
Authorization header, credential과 내부 DB error를 기록하지 않습니다. Query의 bounded
identifier, duration, row/byte, truncation과 공개 error code만 남깁니다. Formatter는 token/secret
assignment과 quoted SQL literal을 방어적으로 redact합니다.

PostgreSQL transaction-local `application_name=query-man:<query_id>`로 실행 중 activity와 audit를
연결할 수 있습니다. 비용 조사는 [query cost runbook](query-cost-control.md)을 따릅니다.

### Consent-gated diagnostic capture

Capture는 기본 disabled입니다. Database path, 별도 32-byte key, key ID를 함께 설정하고 caller에
만료 가능한 `diagnostic_consent` receipt가 있을 때만 최대 7일 encrypted store에 저장합니다.

```text
QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE=/var/lib/query-man/diagnostics/capture.sqlite3
QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY=<URL-safe Base64 32 bytes>
QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID=<lowercase key slug>
QUERY_MAN_DIAGNOSTIC_CAPTURE_DAILY_BYTES=104857600
```

Question 원문 또는 literal을 `NULL`로 바꾼 single-SELECT shape만 저장합니다. Header, token, raw
body, invalid SQL, result row와 DB error는 저장하지 않습니다. TTL/purge를 막지 않으며 decrypt·purge·key
rotation은 대상, 실행자, 출력 처리와 stop condition을 확인한 별도 승인이 필요합니다.

## Retired Source Authority Procedures

이전 Control DB migration, runtime source mutation, source authority cutover와 recovery 절차는 현재
구현에 적용할 수 없습니다. 과거 ADR·evidence의 사실은 그 commit의 Git 이력에 보존됩니다.

### Canonical-Time Coordinated Cutover

해당 managed cutover는 retired됐으며 현재 runbook으로 실행하지 않습니다. 과거 절차는
[Git 기록 안내](verification/README.md)의 archive commit에서만 확인합니다.

## Health And Metrics

| Endpoint | Audience | External behavior |
|---|---|---|
| `GET /health` | Public/load balancer | Process liveness `ok` |
| `GET /ready` | Public/load balancer | Aggregate status; source ID를 노출하지 않음 |
| `GET /admin/health` | Query Man operator | Source별 bounded health |
| `GET /admin/metrics` | Query Man operator | Source/component health와 bounded counter/total snapshot |

Startup은 YAML에 등록된 source별 metadata 경로를 병렬 probe합니다. Source health는 마지막
metadata refresh/restore 결과이며 매 health 요청마다 DB를 ping한 결과가 아닙니다.

| Status | Meaning | `/ready` HTTP |
|---|---|---:|
| `initializing` | Source inventory가 아직 probe되지 않음 | 503 |
| `ready` | 모든 active source가 healthy | 200 |
| `degraded` | 일부 source에 문제가 있지만 healthy/stale source가 있음 | 200 |
| `unavailable` | Healthy/stale source가 하나도 없음 | 503 |
| `shutting_down` | 신규 작업을 받지 않음 | 503 |

`degraded`는 HTTP 200이지만 launch acceptance의 exact readiness는 아닙니다. Counter는 process
restart 때 초기화되며 public dashboard에 source label을 노출하지 않습니다.

## Local Container Operations

```bash
docker compose up -d --wait postgres
./scripts/apply-db.sh
docker compose up -d --build --wait query-man
./scripts/verify-container.sh
```

Application port는 container `3000`, host loopback `${QUERY_MAN_PORT:-3000}`입니다. Token과 reader
password는 `.env`에서 주입하고 image build context·Git·application log에 넣지 않습니다.
Application container에 PostgreSQL administrator password를 전달하지 않습니다.

MCP의 Host/Origin, content type, protocol version과 duplicate security header를 fail-closed로 검증합니다.
Reverse proxy 배포는 exact HTTPS Host/Origin allowlist를 설정하고 wildcard를 사용하지 않습니다.
Client disconnect는 실행 중 query를 cancel·rollback하며 DB timeout은 최종 상한입니다.

## Alert Policy

| Signal | Warning | Critical / action |
|---|---|---|
| Metadata refresh failure | source별 5분에 3회 또는 `stale` | `unavailable`이면 즉시 조사 |
| Query reject | 10분 baseline의 3배 또는 20/min | 공격·오류 client 배포 확인 |
| Queue pressure | 평균 queue가 timeout의 50% | 80% 또는 pool exhaustion 5회/5분 |
| Timeout | source별 5분에 3회 | 5분 실행의 5% 초과 시 fingerprint/DB activity 확인 |
| Truncation | 10분 성공의 10% | 25%면 질문·집계·limit 검토; limit 즉시 상향 금지 |
| Forced shutdown cancel | 1회 | grace, 장기 query와 drain 순서 조사 |

## Security Update Policy

CI의 dependency audit, secret scan, filesystem/config scan과 image vulnerability scan을 유지합니다. 보안
finding은 dependency/image update로 먼저 해소하고, 예외은 exact path·근거·만료일을 가진
최소 범위로만 허용합니다.

## Graceful Shutdown

SIGTERM/SIGINT에서 readiness와 executor admission을 먼저 닫고 listener를 종료한 뒤 이미 수락한
task를 grace 안에 drain합니다. 기한을 넘으면 PostgreSQL cancel·rollback을 실행합니다.
Orchestrator termination grace는 `QUERY_MAN_SHUTDOWN_GRACE_MS`와 process 종료 overhead보다 길게
설정합니다. Transaction-local `TimeZone=UTC`는 commit, timeout, disconnect, forced cancel
후 pool에 남지 않아야 합니다.
