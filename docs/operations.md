# Operations Guide

Status: 현재 first-launch runbook + 구현됐지만 비활성인 managed 운영 reference

이 문서는 목적에 맞는 절만 읽습니다. 처음부터 끝까지 순서대로 실행하는 하나의 runbook이
아닙니다. 낯선 말은 [용어 사전](glossary.md)을 참고하세요.

| 상황 | 읽을 절 | 현재 launch에서 사용 |
|---|---|---|
| 실제 첫 오픈 준비·전환 | [Static Non-RLS First Launch](#static-non-rls-first-launch) | 예 |
| 현재 log, core health·metric, query alert 조사 | [Logging](#logging-policy), [Health](#health-and-metrics), [Alert](#alert-policy) | 예 |
| 초보자용 상태·log·diagnostic·managed 조회 | [Interactive Operator Shell](#interactive-operator-shell) | 상태/log/diag는 예; source mutation은 managed 활성화 후 |
| 로컬 Compose와 MCP 확인 | [Local Container Operations](#local-container-operations) | 예 |
| 안전한 process 종료 | [Graceful Shutdown](#graceful-shutdown) | 예 |
| Replica/usage 관측과 generation 전환 alert | [Health](#health-and-metrics), [Alert](#alert-policy) | 아니요 — managed 활성화 후에만 |
| Control DB migration·복구 gate | [Control DB Migration](#control-db-migration-and-environment-isolation) | 아니요 — managed 활성화 후에만 |
| Bootstrap에서 managed authority로 전환 | [Source Authority Cutover](#source-authority-startup-and-cutover) | 아니요 — 별도 승인 후에만 |

현재 해야 할 작업은 [Active TODO](development-todo.md)의 `LAUNCH-02` 하나입니다. Managed 절은
코드와 절차를 보존하기 위한 상세 reference이며, 설정만 켜서 current launch에 합치는 방법이 아닙니다.

## Static Non-RLS First Launch

[ADR 0025](decisions/0025-static-non-rls-first-launch.md)의 static launch는 일반 production이나
managed onboarding 절차가 아니다. 다음 exact profile만 대상으로 한다.

- `development-issues`, `market-voc` 두 bootstrap source
- PostgreSQL 18, server/client UTF-8, RLS source 0개
- final result OID `20, 21, 23, 25, 1082, 1184, 1700`
- SQL policy v3와 repository의 9개 verified query
- 단일 Query Man replica, private Docker network와 loopback listener

Repository 변경 승인은 실제 환경 실행 승인이 아니다. 실행 전 change record에는 target, operator
access, TLS/secret/backup, source·DDL·role/settings inventory, approved Git commit, upstream/application
image digest, route, stop/rollback condition과 책임자를 기록한다. 하나라도 확인할 수 없으면 시작하지
않는다. 이 protected action은 active TODO의 `LAUNCH-02`이며 별도 사용자 승인 전에는 실행하지
않는다.

### Artifact preparation

Working tree와 commit을 확인하고 approved 40-hex commit을 build argument로 명시한다. `unknown` label은
local 개발용일 뿐 launch artifact로 허용하지 않는다.

```bash
git status --short
git rev-parse HEAD
export QUERY_MAN_VCS_REF=<approved-40-hex-git-commit>
docker compose config --quiet
docker compose build --build-arg QUERY_MAN_VCS_REF="$QUERY_MAN_VCS_REF" query-man
docker image inspect query-man:local \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
```

Dockerfile과 Compose의 upstream reference는 readable tag와 immutable digest를 함께 가져야 한다.
Built application image ID/digest와 revision label을 change record에 복사하고 approved commit과 다르면
중단한다. `query-man-replica`의 `soak` profile은 시작하지 않는다.

### Traffic-off acceptance

Source manifest, budget/access policy, source DDL/view/function/operator/type/collation/extension, reader
role/grant와 database/role/server semantic setting을 승인 inventory와 비교한다. Business DML은 freeze
대상이 아니지만 schema/security/semantic drift는 설명 없이 수용하지 않는다. Runtime이 모든 privileged
DBA drift를 fingerprint하지 않으므로 이 비교는 운영자 책임이다.

```bash
docker compose up -d --wait postgres query-man
./scripts/verify-container.sh
uv run query-man-verify
```

Acceptance는 다음을 모두 요구한다.

- Compose의 `query-man` health가 exact `{"status":"ready"}` body를 확인한다.
- Runtime source inventory가 두 ID와 일치하고 RLS manifest가 0개다.
- Source connection preflight가 PostgreSQL 18, server/client UTF-8을 통과한다.
- Metadata revision 두 개와 verified column/row/hash가 repository baseline과 같다.
- 9/9 query가 SQL policy v3로 성공한다.
- Exact seven-OID positive, bool/JSON/bytea/float/array/record negative와 scalar-domain Catalog
  pre-publication rejection acceptance가 통과한다.
- HTTP와 MCP가 같은 unsupported result를 details 없는 `QUERY_UNAVAILABLE`로 반환한다.
- Application image revision/digest, PostgreSQL image digest와 deployed config가 change record와 같다.

`degraded`, RLS/unsupported advertised type, hash 차이, old SQL policy process, inventory drift 또는
rollback 미검증은 stop condition이다. Change record에는 실제 접속 비밀값, 질의 원문과 내부 DB 오류를
넣지 않는다.

### Cutover and rollback

Old route를 닫고 신규 유입, active query와 source connection을 drain한다. SQL policy v2/v3 process가
동시에 serving하지 않게 한 뒤 accepted single replica만 route한다. Route 뒤 exact ready, public error,
query usage와 PostgreSQL connection 수를 다시 확인한다.

Rollback은 route 차단→new replica drain→직전 image/config/SQL policy와 preserved source inventory
복구→그 release의 ready/verified baseline 확인→route 순서다. New Git history, RLS/Control row와 실행
기록은 삭제하지 않는다. 실제 cutover/rollback 결과만 새 immutable environment evidence로 남기며
repository의 과거 verification 문서를 소급 수정하지 않는다.

## Interactive Operator Shell

Repository root에서 다음 명령을 실행하면 `qm>` prompt가 열린다. 아무 명령도 모르면 빈 줄이나 `help`를
입력한다. Tab은 top-level/subcommand와 이미 조회한 source ID를 자동완성하며, 빈 줄은 이전 명령을
재실행하지 않는다.

```bash
uv run qm
```

상태·일반 로그의 시작점은 다음 네 명령이다. 같은 명령을 `uv run qm status`처럼 one-shot으로도
실행할 수 있다.

```text
qm> status
qm> status metrics
qm> logs
qm> logs --event query_failed --since 2h
```

`logs`는 local `docker compose logs` provider이고 기본 최근 30분/50줄, 최대 31일/1,000줄이다. `-f`,
`--level`, `--event`, `--qid`, `--subject`를 지원한다. Durable collector나 다른 host의 log를 검색하지
않으며 SQL/question을 새로 기록하지 않는다.

동의 기반 상세 capture는 짧은 `diag`를 쓴다. `list`는 content를 표시하지 않고 최대 7일/100건의 bounded
summary만 읽는다. `show`는 question 원문이 terminal에 나타날 수 있어 reason과 exact confirmation이
필수다. `purge`도 receipt 단위 reason과 복구 불가능 경고를 확인한다.

```text
qm> diag list
qm> diag show <capture-id> --reason incident-123
qm> diag purge <receipt-id> --reason privacy-123
```

Compose host에서는 CLI가 running `query-man` container 안의 같은 command로 private diagnostic volume을
읽는다. Key/plaintext는 subprocess argument에 넣지 않는다. Decrypted 출력은 terminal scrollback,
shell redirection, ticket 또는 일반 log collector에 복사하지 않는다. `--yes`는 승인된 automation에서만
쓰며 protected 환경의 실제 조회·삭제 승인이나 change record를 대신하지 않는다.

Managed source 조회는 `source`, `source show/usage/history/replicas/changes`를 사용한다. 변경 명령은
기존 admin HTTP API를 호출하므로 server-side validation, staging, expected generation/state와 mutation
receipt를 그대로 적용한다. Credential은 command line이 아닌 no-echo prompt로 입력하고, timeout이면
새 요청을 만들지 말고 화면에 나온 `source receipt <uuid>`로 terminal result를 확인한다.

```text
qm> source
qm> source show support-tickets
qm> source apply support-tickets manifest.yaml --reason change-123
qm> source disable support-tickets --reason incident-123
```

Base Compose의 `operator-local` token은 `.env`의 `QUERY_MAN_OPERATOR_TOKEN`에서 query caller와 별도로
주입된다. 이 caller는 `/admin/health`와 `/admin/metrics`를 볼 수 있지만 base Runtime은 bootstrap mode라
managed source route가 등록되지 않는다. Source mutation은 managed authority activation과 대상 환경
승인을 별도로 마친 뒤에만 가능하다. Exact CLI/rollback 경계는
[ADR 0028](decisions/0028-interactive-operator-shell.md)을 따른다.

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

Diagnostic capture는 이 일반 log의 예외가 아니라 별도 encrypted store다. 세 Runtime setting과 caller의
active consent receipt가 모두 있을 때만 authorized `get_context` question과 `query`의 literal-free SQL
shape를 저장하며 stdout, audit collector와 `/admin/metrics` payload에는 content를 넣지 않는다. Capture가
configured된 process의 일반 audit는 raw caller/tenant 대신 HMAC `subject_id`를 쓴다.

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

### Consent-gated diagnostic capture

Capture는 기본 disabled다. 다음 세 값을 함께 설정하며 source-generation encryption key와 다른 32-byte
key를 사용한다. Daily byte setting은 optional이고 기본 100 MiB, 허용 범위 1 MiB~10 GiB다.

```text
QUERY_MAN_DIAGNOSTIC_CAPTURE_DATABASE=/var/lib/query-man/diagnostics/capture.sqlite3
QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY=<URL-safe Base64 32 bytes>
QUERY_MAN_DIAGNOSTIC_CAPTURE_KEY_ID=<lowercase key slug>
QUERY_MAN_DIAGNOSTIC_CAPTURE_DAILY_BYTES=104857600
```

Compose image는 `/var/lib/query-man/diagnostics`를 mode `0700`인 named volume에 두고 SQLite file은
`0600`을 강제한다. 세 필드가 일부만 있거나 key/key ID/daily bound가 잘못되면 startup 전에 실패한다.
Access policy의 exact caller에는 실제 동의 receipt를 다음처럼 추가한다. 이 field가 없거나 timezone이
없는 expiry, version 불일치, 만료 equality 이후에는 capture하지 않는다.

```yaml
diagnostic_consent:
  version: 1
  receipt_id: consent-development-analyst-001
  expires_at: 2026-09-30T00:00:00Z
```

Encrypted payload에는 pseudonymous subject/key ID, consent receipt/version/expiry, authorized source와 다음
request field만 있다.

- `get_context`: bounded question 원문
- `query`: server query ID, input byte 수, parseable 여부와 exact single SELECT의 모든 constant를 `NULL`로
  바꾼 PostgreSQL rendering

Header, bearer, raw body, invalid/non-SELECT SQL 원문, result row와 database error는 저장하지 않는다.
Record별 logical TTL은 최대 7일이고 consent expiry가 더 이르면 그 시각을 쓰며, offline read도 expired
row를 반환하지 않는다. Worker는 write와 매시간
sweep에서 expired row를 물리 삭제한다. Queue는 process당 64개이고 shutdown drain은 최대 2초다. Capture
실패는 data response/readiness를 바꾸지 않으므로 collector는 다음 global counter를 경보해야 한다.

```text
diagnostic_capture_enqueued
diagnostic_capture_stored
diagnostic_capture_bytes_count/sum
diagnostic_capture_dropped
diagnostic_capture_queue_dropped
diagnostic_capture_budget_dropped
diagnostic_capture_submit_failed
diagnostic_capture_storage_failed
diagnostic_capture_shutdown_dropped
```

`diagnostic_capture_enqueued - stored - dropped`의 짧은 차이는 worker queue의 in-flight일 수 있다. 지속되는
차이, storage failure 한 번 또는 drop 증가는 capture pipeline incident로 조사하되 query limit/source
health를 바꾸지 않는다. Subject/caller/tenant/receipt는 metric label로 추가하지 않는다.

Decrypt와 receipt purge는 `query_man.runtime.diagnostic_capture`의
`decrypt_diagnostic_records`/`purge_diagnostic_consent` offline helper만 사용한다. 둘 다 database path,
해당 key/key ID를 요구하며 반환된 plaintext는 일반 terminal, ticket, evidence나 검색 index에 복사하지
않는다. Consent 철회는 traffic drain → access policy receipt 제거 → process 교체 → 이전 key ID까지 receipt
purge 순서로 수행한다. 실제 decrypt/purge, key rotation과 protected volume 변경은 target, 실행자, 출력
처리와 stop condition을 확인한 별도 operational approval가 필요하다. Capture DB는 immutable evidence가
아니며 purge/TTL 삭제를 막지 않는다. 전체 format과 rollback은
[ADR 0027](decisions/0027-consent-gated-diagnostic-capture.md)을 따른다.

## Control DB Migration And Environment Isolation

> Managed mode 전용입니다. ADR 0025 static first launch는 Control DB를 source authority로 사용하지
> 않습니다. 실제 적용에는 managed 운영 활성화와 대상 환경 실행 승인이 별도로 필요합니다.

<details>
<summary>Managed Control DB migration·recovery 상세 절차 펼치기</summary>


Control DB는 production, development와 integration test가 서로 다른 physical database/DSN을
사용한다. Production 관리자는 대상 identity를 확인한 뒤 target database/schema/control object
owner 권한과 `query_man_control_writer`를 create·alter하고 남은 membership을 회수할 cluster role
관리 권한을 모두 가진 migration identity의 표준 libpq `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
`PGPASSFILE` 또는 managed authentication을 설정하고 다음을 실행한다. Database owner만으로는
충분하지 않다. Password나 전체 DSN을 command argument 또는 change log에 넣지 않는다.

```bash
./scripts/apply-control-schema.sh
```

Runner는 `docker/postgres/init/control-migrations`의 연속된 `NNNN_name.sql`만 번호순으로
적용한다. `control.schema_migrations`에는 filename과 SHA-256 checksum을 기록하고, 각 pending
migration의 DDL과 ledger insert를 같은 transaction 및 database advisory lock 안에서 처리한다.
재실행은 이미 적용된 migration을 건너뛰며 repository와 DB의 filename/checksum이 다르거나 DB가
현재 checkout보다 앞서 있으면 pending numbered migration과 final security reconciliation 전에
fail-closed한다. Bootstrap schema/ledger 존재 확인은 이 검사보다 먼저 실행될 수 있다. 적용된
migration 파일이나 ledger를 수정해 맞추지 말고 새 forward migration을 추가한다. Schema
downgrade는 지원하지 않는다.

`0002_source_mutation_receipts.sql`은 additive라 pre-`CTRL-05` reader/writer process가 schema 적용
전후 계속 동작한다. 그러나 이전 application은 receipt를 만들지 않으므로 rollout 동안 admin
mutation traffic은 먼저 중단하고 schema를 적용한 뒤 `CTRL-05` application replica에만 보낸다.
Query/data plane replica는 순차 교체할 수 있다. 모든 admin replica가 새 application임을 확인하기
전에는 mutation traffic을 다시 열지 않는다.

`0003_runtime_replica_observations.sql`도 additive라 이전 application은 새 table을 사용하지 않은 채
계속 동작한다. Schema를 먼저 적용하고, 각 managed deployment slot에 동시에 중복되지 않는 stable
`QUERY_MAN_REPLICA_ID`를 설정한 뒤 새 application을 순차 교체한다. 이전 application은 target set에
자동 포함되지 않으며 새 application이 처음 등록한 slot부터 관측 대상이 된다. 같은 slot 재시작은
같은 ID를 재사용해 incarnation fencing을 갱신하고, 서로 동시에 실행되는 process가 ID를 공유하지
않게 한다.

Application rollback은 migration 3을 그대로 남긴 채 수행한다. 이전 application은 새 table을
무시하지만 이미 등록된 slot은 report가 끊겨 stale로 계속 남는다. 이 단계에는 delete, expiry,
retirement 또는 shutdown deregistration이 없으므로 ID를 바꿔 stale slot을 숨기거나 적용된 migration을
수정·삭제하지 않는다. 새 version을 다시 배포할 때 원래 stable ID를 재사용한다.

`0004_source_resource_and_gateway_usage.sql`도 세 table만 추가하는 additive migration이다. Schema를
먼저 적용하고 새 application을 순차 교체한다. 이전 application은 resource/gateway observation을
쓰지 않지만 기존 source/query path를 계속 사용한다. 새 Runtime은 optional manifest target을 기존
reader/catalog pool로 관측하고, 기존 Control source store max-two pool 안에서 resource/gateway write를
직렬화하므로 process당 metadata/source Control connection 최대 4는 바뀌지 않는다. Gateway는 트래픽이
없어도 60초 empty report로 cursor freshness를 갱신한다.

Migration 4 application rollback은 table, ledger와 이미 집계된 data를 남긴다. 31일은 logical
visibility/input window여서 age-only DELETE를 하지 않고, source당 최신 1,000행 cap을 넘긴 rollup만
writer가 DELETE할 수 있다. Resource/cursor에는 DELETE가 없고 rollup에도 TRUNCATE는 없다. 새 HTTP/MCP
또는 availability status는 migration 4만 적용한 application에는 없으므로 기존 public surface는
동일하다.

`0005_source_usage_projection.sql`은 source당 latest resource attempt/last-success row 하나만
추가하는 additive migration이다. Schema를 먼저 적용한 뒤 CTRL-08 application을 순차 교체한다.
이전 application은 새 table과 `/usage` endpoint를 모르며 기존 resource success write를 계속할 수
있지만 attempt row를 만들지 않으므로 새 projection은 current-generation report 전까지 pending일 수
있다. 새 attempt가 생긴 뒤 이전 application이 resource row만 다시 쓰는 mixed-version 구간에는 marker와
sample time이 달라져 usage resource가 일시적으로 `unavailable/OBSERVATION_INCOMPLETE`가 될 수 있다.
새 Runtime의 다음 atomic success가 이를 복구하며 query data plane/readiness에는 영향이 없다. 새
Runtime은 success에 generation을 전달하고 metadata/resource failure만 bounded reason으로 best-effort
기록한다.

Migration 5 application rollback은 table, ledger, attempt와 기존 resource/rollup data를 남긴다.
Writer는 attempt table의 SELECT/INSERT/UPDATE만 갖고 DELETE/TRUNCATE는 없다. Rollback 뒤 old
application은 attempt를 갱신하지 않으므로 값을 삭제하거나 0으로 바꾸지 말고 freshness가
stale로 수렴하거나 legacy sample 갱신과 marker가 달라지면 `OBSERVATION_INCOMPLETE`로 닫히도록 둔다.
31일 age-only delete 금지와 source당 rollup 1,000행 cap도 바뀌지 않는다.

Schema migration과 global `query_man_control_writer`/DB ACL은 의도적으로 분리돼 있다.
`reconcile-security.sql`은 매 실행마다 role을 harden하고 현재 DB의 최소 권한을 복구한다. 따라서
`pg_dump --no-privileges` restore에서도 pending migration이 없어도 ACL이 복구된다. Runtime
writer에는 migration ledger, schema CREATE 또는 DDL 권한을 부여하지 않는다. 전용 Control DB의
`PUBLIC` CONNECT/CREATE/TEMPORARY와 writer의 과거 database/schema/table/sequence/function grant
및 writer가 상속하던 parent-role membership을 먼저 회수한 뒤 allowlist를 다시 부여한다. Direct
object ACL grantee는 object owner와 writer만 허용한다. Migration identity가 membership 또는
delegated ACL을 회수할 권한이 없거나 writer가 object owner이면 exact postcondition에서
fail-closed하므로 writer와 migration/owner role을 분리한다. Runtime LOGIN처럼 writer membership을
받는 member role의 allowlist와 lifecycle은 database/IAM 운영 authority가 별도로 감사한다.

Advisory lock은 target database 안에서만 직렬화되지만 `query_man_control_writer`와 membership은
cluster-global이다. 같은 PostgreSQL cluster의 서로 다른 database를 대상으로 production migration,
restore drill과 disposable migration job을 동시에 실행하지 않고 운영/CI에서 cluster 단위로
직렬화한다. 여러 Control DB의 병렬 migration이 실제 요구가 되면 global role reconciliation을
분리하거나 고정 coordination database의 lock을 사용하도록 먼저 설계한다.

Stale membership/ACL을 복구했거나 security drift를 대응한 뒤에는 admin mutation traffic을 닫고
모든 control-writer session과 pool을 drain/recycle한 뒤 fresh connection으로 effective privilege를
검증한다. Membership 회수는 이미 이전 parent role로 `SET ROLE`한 session을 강제로 종료하거나 원래
role로 되돌리지 않는다.

Local/CI의 `scripts/apply-managed-acceptance-fixtures.sh`는 `compose.yaml`과
`compose.acceptance.yaml`을 함께 사용한 별도 `query-man-managed-acceptance` project의 development
Control DB에 같은 runner를 적용하지만 production migration 명령이 아니다. 기본 `apply-db.sh`는
current 두 static source만 준비하며 Control schema를 적용하지 않는다. Control-store와 hot-add integration test는
`query_man_control_test_<random>` DB를 test마다 생성하고, 모든 pool을 닫은 뒤 삭제하며 전후
development authority fingerprint가 동일한지 확인한다. CI가 비정상 종료돼 scratch DB가 남으면
해당 ephemeral Compose volume을 폐기한다. 운영 DB나 사용자가 지정한 임의 DB를 test cleanup
대상으로 삼지 않는다.

### Control Recovery Release Gate

빠른 same-cluster schema drill과 isolated Control recovery fixture acceptance를 구분한다.
둘 다 managed-acceptance PostgreSQL을 사용하며 script/test 안의 bare Compose subprocess가 같은 격리
project를 보도록 `COMPOSE_FILE`을 유지한다.

```bash
export COMPOSE_FILE=compose.yaml:compose.acceptance.yaml
docker compose up -d --wait postgres
./scripts/apply-managed-acceptance-fixtures.sh
./scripts/control-plane-drill.sh
uv run pytest -m integration -q tests/test_control_recovery.py
docker compose down -v --remove-orphans
unset COMPOSE_FILE
```

`test_control_recovery.py` command는 `recovery` profile의 격리 PostgreSQL 18.4 source에서 현재 18.6 fresh DB로
archive를 복원하고 13-table fingerprint, archive 밖 writer LOGIN/key, 모든 generation decrypt,
logical retention, receipt replay, source/verified file 없는 두 stable replica와 실제 guarded query를
검증한다. Existing recovery service를 덮어쓰지 않고 random-prefix database와 임시 artifact만
정리한다. Global writer reconciliation이 있으므로 production migration, 빠른 drill이나 다른
disposable migration test와 동시에 실행하지 않는다. Cleanup은 `query-man-managed-acceptance` volume만
삭제하며 base static volume은 건드리지 않는다.

이 repository gate는 production backup scheduler, archive age/access audit, TLS/IAM, secret-manager와
source business DB 복구를 대신하지 않는다. Release change record에는
[disaster recovery runbook](disaster-recovery.md)의 실제 RPO/RTO와 환경별 Restore 3~7단계 결과를
별도로 남긴다.

</details>

## Source Authority Startup And Cutover

> 구현된 managed capability의 전환 절차입니다. 현재 first launch에서는 실행하지 않습니다.

<details>
<summary>Bootstrap → managed 전환과 mutation reconciliation 펼치기</summary>


Runtime은 process 전체의 source authority를 한 mode로 고정한다.

| Mode | Valid source configuration | Authentication |
|---|---|---|
| `bootstrap` (default) | Control DSN/key 없음 | Loopback anonymous, query-only API token 또는 version 2 policy |
| `managed` | Control DSN/key와 stable replica ID 모두 있음 | Version 2 policy file 필수; query/admin identity 분리 |

Bootstrap에 Control 설정이 하나라도 있거나 managed에 Control DSN/key/replica ID 중 하나가 빠지면
configuration error로 시작하지 않는다. Bootstrap은 replica ID가 있어도 읽거나 검증하지 않는다.
Mode를 `auto`로 추론하거나 source별로 섞지 않는다. Managed startup은 source
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
3. 새 UUID/change reference와 expected state `0/0`으로 기존 admin API에 source를 L0/L1 staged
   publish한다. Reader credential은 external secret boundary에서 관리자가 전달하고 응답 receipt의
   resulting generation/state와 metadata revision을 기록한다.
4. 기존 reviewed verified-query record를 admin endpoint로 실행·저장한다. 현재 revision과
   invariant가 다르면 이관을 중단하고 재검토한다.
5. 같은 semantic/budget revision에서 L2 generation을 publish하고 `/meta`, guarded query와
   intended inactive state를 확인한다.
6. 필요한 모든 source와 L2 verified-query record가 Control DB에 있는지 확인한다. 각 serving slot에 고유하고
   재시작 뒤에도 유지되는 `QUERY_MAN_REPLICA_ID`를 배정한 뒤 `QUERY_MAN_SOURCE_MODE=managed`로
   순차 재시작한다. Source/verified file이 없어도 같은 inventory와 revision이 복원되고
   deactivate/rollback이 유지되는지 확인한다.
7. 각 source의 `GET /admin/sources/{source_id}/replicas`에서 예상한 slot이 모두 `available`이고
   `drift=[]`인지 확인한 뒤 traffic을 전환한다. Planned replica가 아직 시작되지 않았다면 이
   endpoint가 아니라 deployment inventory에서 누락을 확인한다.

이 절차는 startup import나 새 bulk endpoint가 아니다. Seed digest/import marker와 repository
write-back을 만들지 않는다.

### Canonical-Time Coordinated Cutover

[ADR 0019](decisions/0019-canonical-time-stability.md)의 R2는 rolling mixed fleet로 배포하지 않는다.

1. Source/admin/verified mutation을 동결하고 Control backup, R1 image/binary/key와 source별 active 및
   rollback generation/revision을 change record에 고정한다.
2. Public API가 verified payload를 열거하지 않으므로 보호된 migration/admin identity로
   `control.verified_query_contracts`의 current/rollback-preserved question, SQL, relations와 expected를
   제한된 offline export한다. Inventory 완전성을 증명하지 못하면 중단한다.
3. R1 business-calendar source DB migration을 적용하고 managed manifest의
   `database_migration_ref`가 실제 artifact를 가리키게 갱신한다. R1 runtime에서 source별
   L1→모든 verified query 재실행·재발행→L2와 rollback baseline을 확인한다.
4. Old fleet admission/route를 닫고 graceful drain을 완료한다. `pg_stat_activity`에서 old
   query/catalog application connection이 0임을 확인한 뒤에만 R2를 route 밖에서 시작한다.
5. R2에서 source별 L1→current와 rollback-preserved verified query 전체 재실행·재발행→L2를 수행하고,
   replica `available`, `drift=[]`, stale metadata/SQL policy 409와 `/ready`를 확인한 뒤 route한다.
6. Rollback은 mutation freeze를 유지한 채 R2를 drain하고 R1 image를 시작해 captured CAS/generation,
   revision, verified/L2와 ready를 복구한 뒤 route한다. R2 snapshot/generation/verified row는 삭제하지
   않는다.

Repository fixture 검증은 production inventory, backup, old fleet connection 0 또는 실제 route 전환을
증명하지 않는다. 환경별 change record가 별도로 필요하다.

### Admin Mutation And Timeout Reconciliation

모든 source mutation에는 canonical lowercase UUID `Idempotency-Key`, ticket/change reference인
`X-Query-Man-Reason`, 같은 admin detail snapshot에서 읽은 `X-Expected-Generation`과
`X-Expected-State-Version`을 보낸다. 새 source 최초 publish만 `0/0`이다. Metadata resume은
`X-Expected-Metadata-Revision`도 요구한다. Actor는 bearer에 연결된 operator caller ID로
자동 결정된다. 이유 header에 사람 이름, credential, SQL 또는 자유형 장애 내용을 쓰지 않는다.

성공하면 terminal receipt 전체를 change record에 연결하되 credential/body를 복사하지 않는다.
409 generation conflict는 다른 변경이 먼저 commit된 것이므로 detail과
`GET /admin/sources/{source_id}/mutations`를 다시 읽고 새 의도로 재검토한다. 같은 key/different
request의 `MUTATION_IDEMPOTENCY_CONFLICT`는 key를 재사용한 client 결함으로 취급한다.

HTTP timeout이나 연결 단절 뒤에는 다음 순서를 지킨다.

1. 원래 key로 `GET /admin/mutations/{idempotency_key}`를 조회한다.
2. Terminal receipt가 있으면 그 success/rejection과 `resulting_state`를 authoritative하게 사용한다.
3. 404면 아직 staging/in-flight일 수 있으므로 source detail의 generation/state를 대조하며 bounded
   polling한다. 404를 실패나 미실행 증거로 취급하지 않는다.
4. Wait 뒤에도 receipt가 없고 source가 요청 전 expected state임을 다시 확인한 경우에만 원래
   payload/header와 같은 key로 한 번 재전송한다. 새 key, 바뀐 reason/expected state 또는 수정한
   body를 섞거나 여러 replica에 fan-out하지 않는다. Receipt 생성 전 동시 요청은 staging/verified
   query를 중복 수행할 수 있지만 authority와 terminal receipt는 한 번만 commit된다.

Success receipt와 source/verified-query state 변경은 한 transaction이다. Post-commit local reload가
실패해도 success를 rollback하거나 rejection으로 바꾸지 않고 `source_reload`을 unavailable로
표시한다. 해당 replica는 poller가 같은 desired state를 적용할 때까지 degraded일 수 있으며
replica별 convergence는 전용 admin replica endpoint에서 직접 확인한다.

</details>

## Health And Metrics

Public endpoint는 inventory를 노출하지 않는다.

| Endpoint | Audience | External behavior | 현재 static launch |
|---|---|---|---|
| `GET /health` | Public/load balancer | Process liveness만 `ok` | 사용 |
| `GET /ready` | Public/load balancer | 아래 aggregate status만 반환; source ID 없음 | 사용 |
| `GET /admin/health` | Query Man admin | source별 `initializing`, `healthy`, `stale`, `unavailable` | 사용 |
| `GET /admin/metrics` | Query Man admin | source/component health와 bounded counter/total snapshot | 사용 |
| `GET /admin/sources/{source_id}/replicas` | Query Man admin | ever-registered replica별 desired/applied drift와 freshness | 비활성 — managed 전용 |
| `GET /admin/sources/{source_id}/usage` | Query Man admin | resource attempt/last-success와 31일 gateway lower-bound projection | 비활성 — managed 전용 |

현재 `LAUNCH-02`에서는 앞의 네 endpoint만 사용합니다. 아래 replica/usage 설명은 managed mode를
별도로 활성화한 환경을 위한 reference입니다.

<details>
<summary>Managed 전용 replica/usage endpoint 상세 펼치기</summary>

Replica endpoint는 `limit` 1~100과 exclusive `after_replica_id` cursor를 받는다. 알려진 source는
replica가 `pending`, `stale` 또는 `unavailable`이어도 200이며 다음을 확인한다.

- `available`과 빈 `drift`: freshness 안에서 desired enabled/generation/state/metadata가 일치한다.
- `pending`: 아직 observation이 없거나 적용 state가 충분하지 않다.
- `stale`: DB clock 기준 `observed_at + 3 × report cadence`가 지났다. Report cadence는
  `max(QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS, 5000)`ms다.
- `unavailable`: fresh report가 scan/apply/validation/metadata probe failure를 알렸다.
- Disabled desired는 metadata와 source health를 drift 판단에 쓰지 않는다. `source_health`와
  `applied.metadata_revision`이 null인 정상 disabled replica도 `available`일 수 있다.

`NOT_OBSERVED`, `HEARTBEAT_EXPIRED`, `CONTROL_SCAN_FAILED`, `RUNTIME_VALIDATION_REJECTED`,
`RUNTIME_APPLY_FAILED`, `METADATA_PROBE_FAILED` 외 reason이나 raw 오류는 공개하지 않는다.
Observation registration/report 실패는 이 endpoint의 freshness에만 나타나고 `/ready`, 기존
source health, query data plane과 mutation receipt를 바꾸지 않는다. Process log의
`replica_observation_registration_failed` 또는 `replica_observation_report_failed`를 조사하되 같은
process에서 수동 재등록하거나 ID를 바꾸지 않는다.

Usage endpoint는 query parameter 없이 한 DB snapshot의 최대 1,000 gateway row를 반환한다.
Resource의 latest attempt가 실패해도 last success가 72시간 freshness 안이면 `available`이며
`last_attempt`에서 실패를 확인한다. Last success가 없으면 `pending` 또는 bounded failure의
`unavailable`, 만료되면 `stale`다. Gateway는 source별 traffic 완전성이 아니라 reporter pipeline
health다. Heartbeat가 fresh한 모든 live replica의 current-incarnation cursor가 fresh해야
`available`이며 하나라도 absent/expired면 `REPORTER_UNAVAILABLE`이다. Live replica가 없으면 과거
accepted cursor 유무에 따라 `stale` 또는 `pending`이다. Startup 직후 첫 empty report 전의 짧은
`unavailable`은 정상이며 data plane/readiness를 바꾸지 않는다.

Gateway window는 Control DB clock 기준 현재 UTC hour부터 31일 전 hour까지 양 끝 포함이다. Empty
rollup, missing metric과 failed observation을 0으로 해석하지 않는다. Monetary cost는 provider가
연결되지 않아 `PROVIDER_NOT_CONFIGURED`만 표시하고 amount/currency를 제공하지 않는다. 이 endpoint의
credential/connection, observability relation/grain, replica/cursor identity, caller/tenant,
question/SQL/fingerprint/query ID 또는 raw 오류 노출은 incident로 취급한다.

</details>

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
- Diagnostic capture: global `diagnostic_capture_enqueued/stored/dropped`,
  `diagnostic_capture_bytes_count/sum`, queue/budget/submit/storage/shutdown drop counter
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
application을 시작한다. `/ready`가 `degraded`여도 HTTP 200 wire status는 유지하지만,
Compose healthcheck는 body가 정확히 `{"status":"ready"}`일 때만 healthy다. 따라서
`degraded` container는 launch acceptance를 통과하지 않는다.
Application port는 container 내부 `3000`, host loopback의 `${QUERY_MAN_PORT:-3000}`이며
PostgreSQL은 container network에서 `postgres:5432`다.

Compose는 `QUERY_MAN_SOURCE_MODE=bootstrap`이고 access policy는
`QUERY_MAN_CODEX_MCP_TOKEN`을 모든 active bootstrap source를 보는 query-only caller로 만들며,
별도 `QUERY_MAN_OPERATOR_TOKEN`을 health/metric/cancel용 explicit operator로 만든다. Token과 reader
password는 `.env`에서 주입하지만
image build context와 Git에는 포함하지 않는다. Application container에는 PostgreSQL
administrator password를 전달하지 않는다. 기본 Compose는 control-plane DSN/key를 주입하지 않고
source admin route도 등록하지 않는다. Managed acceptance가 필요하면 별도 Compose overlay를
명시적으로 적용한다. Overlay는 `query-man-managed-acceptance`라는 별도 project, PostgreSQL container와
volume을 사용한다. Integration fixture의 bare Compose subprocess도 이 project를 찾도록 test session에
`COMPOSE_FILE`을 유지한다.

```bash
export COMPOSE_FILE=compose.yaml:compose.acceptance.yaml
docker compose up -d --wait postgres
./scripts/apply-managed-acceptance-fixtures.sh
# 필요한 managed focused/integration test 실행
docker compose down -v --remove-orphans
unset COMPOSE_FILE
```

위 `down -v`는 managed-acceptance volume만 삭제한다. Base `query-man_postgres_data`나 production
설정을 이 project와 섞거나 삭제하지 않는다.

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

MCP client는 Query Man이 지원하는 current protocol version으로 실제 initialize와 tool inventory를
검증해야 합니다. Client별 feature flag와 설정 이름은 version에 따라 바뀔 수 있으므로 이 runbook에
특정 CLI version을 고정하지 않습니다. `.env`를 만들었다고 이미 실행 중인 client process에 값이
자동 전달되는 것은 아닙니다. MCP token만 필요한 process environment에 주입하고 database
credential을 client project로 복사하지 않습니다. Client가 실행하는 임의 shell command에는 token이
상속되지 않게 하며, client upgrade 뒤에는 `/mcp` startup과 tool inventory를 다시 확인합니다.

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

| Signal | 적용 범위 | Warning | Critical / action |
|---|---|---|---|
| Metadata refresh failure | 현재 static launch | source별 5분에 3회 또는 `stale` 전이 | `unavailable` 즉시 on-call |
| Replica convergence | managed 전용 — 현재 비활성 | expected slot의 non-empty drift 또는 `pending` 3 cadence | `stale`/`unavailable` 또는 missing expected slot이면 deployment/Control 연결 확인 |
| Validation reject | managed generation 전환 전용 — 현재 비활성 | 새 generation 1회 | 동일 source 3회 연속이면 publish 중지·마지막 정상 generation 확인 |
| Query reject | 현재 static launch | 10분 baseline의 3배 또는 20/min | 공격/잘못된 client 배포 확인; response/audit reason별 조사 |
| Queue pressure | 현재 static launch | 평균 queue가 timeout의 50% | 80% 또는 `query_pool_exhausted` 5회/5분이면 admission/budget 점검 |
| Timeout | 현재 static launch | source별 5분에 3회 | `Δquery_timeout / Δquery_execution_started`가 5분간 5% 초과 시 expensive fingerprint와 DB activity 확인 |
| Truncation | 현재 static launch | `Δquery_truncated / Δquery_execution_succeeded`가 10분간 10% | 25%면 질문/aggregation/limit policy 검토; limit 즉시 상향 금지 |
| Forced shutdown cancel | 현재 static launch | 1회 | grace, 장기 query와 배포 drain 순서 조사 |

외부 collector를 구성하면 현재 endpoint에서 execution/reject/timeout/truncation rate,
queue/elapsed 평균과 source/component status를 source별로, shutdown outcome을 replica별로
계산할 수 있다. Managed mode의 replica observation exact stale age는 현재 비활성인 전용 source
replica endpoint에만 있다.
Admin metrics는 active pool gauge, row/byte distribution과 percentile을 제공하지 않으므로 그
panel이 필요하면 먼저 계측을 추가한다. Public dashboard에는 source label을 노출하지 않는다.

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
Reader `TimeZone=UTC`는 transaction-local이므로 정상 commit뿐 아니라 timeout, disconnect와 강제
cancel rollback 뒤에도 pool에 남지 않아야 한다. Role/database default는 배포에서 바꾸지 않는다.
