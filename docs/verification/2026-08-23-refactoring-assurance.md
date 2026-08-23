# Refactoring Assurance Audit — 2026-08-23

Status: Complete

## Scope And Verdict

이 감사는 production baseline 이후의 논리 오류, 상태 경쟁, reader 권한 drift, 종료 순서,
비용 통제와 운영 문구를 다시 검토한 [roadmap](../implementation-roadmap.md)의
`REF-01`~`REF-15`를 닫는다. 이전
[completion audit](2026-08-23-completion-audit.md)과 다른 날짜별 verification 문서는 각 실행
시점의 역사적 증거다. 이 문서의 회귀 수치와 보장 범위는 `REF-01`~`REF-15`
종료 시점을 나타내며, 이후 변경을 포괄하는 단일 현재 audit가 아니다. 후속 scope는
[architecture Completion Tracking](../architecture.md#completion-tracking)에 나열한 각 scoped audit을 따른다.

감사는 checkbox를 근거로 삼지 않았다. 코드 경로와 PostgreSQL transaction/role 계약을
대조하고, 발견한 모순을 재현 테스트로 고정한 뒤 unit, 실제 PostgreSQL integration, load,
quality, verified result, DR와 security 검사를 독립적으로 다시 실행했다. 현재 정의된 115개
roadmap 항목은 각 checklist의 구현·검증·문서 완료 조건을 충족한다.

## Refactoring Checklist Evidence

| ID | Corrected contract and primary evidence | Result |
|---|---|---|
| `REF-01` | `catalog.py`가 constraint ordinality로 composite PK/FK pair와 index column 순서를 수집하고 `revision.py`/`metadata_store.py`가 그 순서를 보존한다. Revision 회귀는 순서 반전을 다른 revision으로 판정하고 store 회귀는 physical structure round trip을 검증한다. | PASS |
| `REF-02` | `app.py`/`mcp_server.py`의 caller provider 예외를 `list_sources`, `get_context`, `query` 모두 bounded `INTERNAL_ERROR`로 변환한다. 실제 in-process MCP client 회귀가 내부 예외 문구 비노출을 확인한다. | PASS |
| `REF-03` | `runtime_config.py`가 Python logging backend가 지원하는 level만 허용하며 `trace` 같은 잘못된 값은 startup 전에 거부한다. | PASS |
| `REF-04` | `metadata.py`의 source epoch와 `metadata_store.py`의 generation/state-version CAS가 이전 profile의 지연 refresh 및 publish를 차단한다. Process-local race와 실제 control DB superseded-generation 회귀를 모두 통과했다. | PASS |
| `REF-05` | 공통 `reader_policy.py`를 catalog/query transaction 안에서 실제 catalog/query 작업 전에 적용해 database, session user, restricted role, read-only/repeatable-read, search path, RLS, tenant context와 resource setting을 확인한다. 권한 회수는 cached metadata가 있어도 stale fallback 없이 fail-closed한다. | PASS |
| `REF-06` | `query.py`가 semaphore queue, pool wait와 DB 실행을 포함한 모든 admitted task를 추적한다. Drain은 신규 admission을 닫고 grace 뒤 queued/active task를 cancel하며 connection을 rollback한다. Semaphore wait, pool lease wait와 active DB 작업을 분리한 회귀를 통과했다. | PASS |
| `REF-07` | `source_admin.py`/`source_store.py`가 source pointer 전이마다 증가하는 state version으로 rotate, deactivate와 rollback을 직렬화하고, resume은 같은 source provenance의 generation/state version을 CAS로 확인한 뒤 metadata pin을 해제한다. Disabled rotation, stale/equal apply와 다른 connection identity 재사용을 거부한다. | PASS |
| `REF-08` | Budget schema v2가 `work_mem`, `temp_file_limit`, parallel worker와 JIT을 bounded profile로 검증하고 catalog/query transaction-local setting과 effective-value check를 강제한다. 실제 PostgreSQL 회귀와 replica-aware connection 산정을 확인했다. | PASS |
| `REF-09` | `operations.py`, startup probe와 control-plane reloader가 active inventory와 component health를 reconcile한다. Startup, scan/apply failure, staging 격리와 dynamic deactivate가 public readiness/operator health에 일관되게 반영된다. | PASS |
| `REF-10` | Stored active metadata의 activation provenance로 TTL/stale age를 복원한다. Restart, rollback과 resume 회귀에서 process cache 시각으로 freshness가 다시 시작되지 않음을 확인했다. | PASS |
| `REF-11` | Metadata identity에 source execution budget과 revision-scoped policy가 포함된다. Verified contract 등록 시 guarded query를 실행하고, 이후 L2 publish/rollback/reload는 현재 budget/policy가 포함된 revision identity와 verified membership/quality를 재검증한다. | PASS |
| `REF-12` | `result_encoding.py`가 PostgreSQL `numeric`을 정확한 decimal string, binary를 Base64 string, 날짜/시간을 ISO string으로 만든다. 공통 canonical scalar encoding과 동일한 compact-JSON 규칙을 byte accounting과 HTTP/MCP serialization에 적용하며 non-finite/unsupported 값은 fail-closed한다. | PASS |
| `REF-13` | 표준 `uv run query-man` entrypoint의 signal handler가 readiness/admission을 먼저 닫고 Uvicorn graceful timeout을 application grace의 초 단위 올림값으로 사용한다. Live Uvicorn의 `handle_exit(SIGTERM)` lifecycle과 lifespan drain 회귀를 통과했다. | PASS |
| `REF-14` | Query audit에 query ID, fingerprint, queue/elapsed/result/plan 신호와 rejection threshold를 연결했다. 비용 runbook, readiness/metrics/alert 문구, production control migration과 custom-archive DR drill을 실제 수집·검증 범위에 맞췄다. | PASS |
| `REF-15` | 아래의 locked environment, static, unit, integration, load, evaluation, verified, DR, security와 documentation 회귀를 모두 통과하고 이 감사를 남겼다. | PASS |

## Material Defects Found And Closed

이번 검토는 표현 정리에 그치지 않고 다음 실행 오류를 재현하고 수정했다.

- Psycopg의 기본 implicit transaction 뒤에 application `BEGIN`을 실행해 선언한
  `REPEATABLE READ READ ONLY`가 적용되지 않을 수 있었다. Reader pool을 autocommit mode로
  열고 application의 explicit transaction만 경계를 만들게 했으며, 실제 session에서
  isolation/read-only 값을 검증한다. 이 autocommit 설정은 transaction 제거가 아니라 implicit
  `BEGIN` 방지용이며, explicit `BEGIN`은 계속 connection의 첫 SQL이어야 한다.
- Named cursor를 한 row마다 fetch해 정상적인 40-query 부하에서도 queue timeout을 유발했다.
  16-row 고정 batch와 row-limit sentinel로 왕복을 줄이면서도 row/UTF-8 byte truncation은
  정확히 유지했다. Session setting/policy 조회도 bounded round trip으로 합쳤다.
- Metadata structure의 정렬이 composite key/FK pair의 의미 있는 column 순서를 잃을 수 있었고,
  이전 generation refresh가 새 source metadata를 덮을 수 있었다. Ordered revision material,
  process epoch와 control-plane CAS로 두 경계를 고정했다.
- Catalog cache가 reader privilege drift를 일시 장애처럼 취급해 stale metadata를 제공할 수
  있었다. Catalog와 query가 같은 reader/session policy를 검사하고 security drift는 stale
  fallback 대상에서 제외한다.
- Graceful drain이 실행 중 DB query만 중심으로 추적해 semaphore/pool 대기 task를 남길 수
  있었고 signal 처리와 application grace의 시작점도 달랐다. Admission 시점부터 task를
  추적하고 signal entry에서 동일한 grace를 시작하도록 맞췄다.
- Restart가 stored metadata의 stale window를 새로 시작하고, readiness inventory가 poll/deactivate
  상태와 어긋날 수 있었다. Persisted activation provenance와 active inventory reconciliation으로
  수정했다.
- 비용·dashboard·restore 문서가 제공하지 않는 percentile, query별 장기 통계 또는 full service
  recovery를 암시하던 부분을 실제 metric과 drill 범위로 제한했다. Production schema 적용은
  fixture seed script와 분리했다.

## Final Executed Evidence

모든 명령은 repository root의 locked `uv` 환경과 PostgreSQL 18.6 fixture에서 실행했다.

```text
uv sync --locked
  PASS (57 packages resolved, 53 audited)

uv run ruff check .
  PASS

uv run mypy src
  PASS (26 source files)

uv run pytest
  PASS (205 passed, 17 deselected integration tests)

uv run pytest -m 'integration and not load'
  PASS (16 passed, 206 deselected)

uv run pytest -m load -s
  PASS (40 queries, 2 sources, 1 passed, 221 deselected)
  service_wall_ms: p50 558, p95 813, max 838
  execution_ms:    p50 66, p95 85, max 89
  queue_ms:        p95 680, max 741
  max plan total_cost: 215.55

uv run query-man-evaluate
  PASS (16 cases; relation accuracy 1.0, answerability recall 1.0,
        average context 7,892 bytes, maximum context 13,509 bytes)

uv run query-man-verify
  PASS (9 result contracts)
  development revision sha256:83c8918b9a9d0eda9a394a93063a679dd3c513efc192ba5ead65afd078122464
  market revision      sha256:d62177e5798a405308e698e550d5350f7c295e543ecc652e10a7b6784d855f2e

./scripts/control-plane-drill.sh
  PASS (custom archive, 5 tables, 4 FKs, 3 triggers, writer ACL)

locked runtime dependency pip-audit
  PASS (no known vulnerabilities)

Gitleaks v8.30.1
  PASS (full repository history, no leaks)

Trivy v0.72.0 filesystem
  PASS (0 HIGH/CRITICAL dependency, secret or misconfiguration findings)

Trivy v0.72.0 postgres:18.6-bookworm
  PASS (0 unsuppressed CRITICAL findings; the path-scoped, expiring gosu exception
        in .trivyignore.yaml remains subject to its documented 2026-09-30 review)

Markdown relative-link audit
  PASS
```

The load test first reproduced overload and PostgreSQL's "transaction already in progress" warning.
The explicit-transaction and bounded-fetch fixes above were applied before the recorded passing run;
the failed run is diagnostic history, not release evidence. `service_wall_ms` measures the complete
`QueryService.query` call, including metadata/revision validation, semaphore queue, pool wait and DB
execution. Response `execution_ms` starts after a DB connection is acquired, while `queue_ms` is only
the enforced source-semaphore admission wait. None is the complete HTTP latency.

## Recovery Evidence Boundary

The automated drill proves a same-cluster/current-PostgreSQL custom
`pg_dump`/`pg_restore --no-owner --no-privileges` round trip, two error-free applications of the current
control schema, row-count parity for five tables, four foreign keys, three non-internal user-trigger
registrations and writer-group ACLs. It does not validate trigger names/definitions or execute an
immutable mutation rejection. It also does not prove cross-host/version or old-schema migration,
archive content hashes, source business DB/global role recovery, dedicated LOGIN creation/authentication,
ciphertext decryption, active generation semantics, runtime/source query success, or actual RPO/RTO.
Those remain the explicit manual restore checks in
[disaster recovery](../disaster-recovery.md).

## Deliberate Boundaries

These are explicit scope boundaries, not hidden completion claims. Expanding one requires a new
roadmap item and executable acceptance evidence.

- `/admin/metrics` is replica-local and exposes counters plus queue/elapsed count and sum. It has no
  histogram/percentile, stale-age, active-pool gauge, durable history or monetary-cost metric. The
  repository also does not deploy the external collector, dashboard or alert executor described by
  the operator policy. Source-scoped in-memory metrics are also removed when that source is deactivated,
  so a collector must scrape them before inventory pruning if they are needed as history.
- Optional `pg_stat_statements` is a reader/source aggregate aid. It is not directly joined to Query
  Man query UUIDs or pglast fingerprints, and cursor/utility statistics can be split or combined.
- Credential ciphertext uses AES-256-GCM with one direct master key. Ciphertext has no key version and
  online master-key rotation is not implemented.
- Query admission is process-local. The fixture reader hard cap is 7 connections for two replicas
  (`2 × (query pool 2 + metadata pool 1) + staging 1`); replica/pool changes require recalculation.
- `work_mem` is an operation-level limit and `temp_file_limit` is a backend-level limit, not a whole
  process/host memory or disk quota. Planner cost is a relative estimate, not time or currency.
- The recorded load run is a deterministic local two-source safety-budget check, not a production
  latency SLO, multi-replica saturation result or monetary-cost measurement. Its percentile values are
  diagnostics; the executable gates are the documented timeout, queue and plan bounds.
- Control metadata and source stores are separate pools of maximum 2 connections each, so one replica
  can use 4 control connections. A dedicated LOGIN's capacity must be calculated for the deployment.
- Limited caller source grants are startup configuration and require restart to change. Only an
  explicitly trusted `all_sources` caller sees future control-plane sources without restart.
- Readiness is HTTP 200 when at least one active source is healthy/stale: it is `ready` only when every
  active source and component is healthy, otherwise `degraded`. No usable source, initialization and
  shutdown are 503. A valid stored snapshot may restore without a current source DB connection, so
  readiness is not live query-connection liveness.
- The guaranteed early signal ordering belongs to the standard `uv run query-man` entrypoint. A custom
  process manager must reproduce that signal and grace contract.

Within these boundaries, the production-ready architecture and all 115 roadmap checklist items have
the implementation and scoped evidence recorded above.
