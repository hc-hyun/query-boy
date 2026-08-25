# Resource And Gateway Observation Audit — 2026-08-25

Status: Complete

## Scope

`CTRL-07`은 승인된 `CTRL-07A` 계약에 따라 optional source resource definition, bounded PostgreSQL
catalog measurement, daily current/previous resource observation과 privacy-safe hourly gateway usage
lower-bound rollup을 구현했다. 새 HTTP/MCP endpoint, admin availability response, monetary cost,
DB-native statistics collector와 caller/tenant allocation은 추가하지 않았다.

31일 의미는 같은 날 사용자가 추가 승인했다. 이는 DB clock 기준 logical visibility/input window이며
나이만으로 physical row를 삭제하지 않는다. 향후 `CTRL-08` read는 cutoff 밖 row를 반환하지 않고,
현재 writer는 오래된/future bucket을 거부한다. 저장 공간의 별도 상한으로 source당 최신 1,000
rollup row를 유지하며 이 cap을 넘긴 row만 물리적으로 정리한다.

## Implemented Contract

- Strict manifest v2는 optional `observability`에 representative grain/physical relation과 이를 포함하는
  1~16개 distinct storage relation을 받는다. 같은 DB의 non-system ordinary table/materialized view만
  허용하고 query relation allowlist, public source projection과 metadata revision에는 추가하지 않는다.
- `RuntimeCatalogProvider.observe_resources()`는 기존 reader와 max-one catalog pool, read-only
  transaction/timeout을 재사용한다. `pg_class.reltuples`, `pg_table_size`, `pg_indexes_size`,
  `pg_total_relation_size`만 사용하며 `COUNT(*)`, caller SQL과 `EXPLAIN ANALYZE`를 실행하지 않는다.
- Resource는 `representative_records`, `table_bytes`, `index_bytes`, `total_storage_bytes` 네 metric만
  source-level로 저장한다. Metric/method/grain 또는 sorted relation list와 DB migration reference의
  canonical SHA-256을 definition revision으로 사용한다.
- Migration 4의 `source_resource_observations`는 metric별 UTC daily current와 comparable previous만
  저장한다. Same bucket은 current만 교체하고 method/definition 변경은 previous를 지우며 DB-clock
  72시간 freshness를 사용한다. Source별 transaction advisory lock이 동일 source의 동시 저장을
  직렬화하고 lock 획득 뒤 Control DB clock을 기록한다. Runtime은 apply 뒤와 이후 24시간마다
  best-effort로 시도한다.
- Guarded Query는 trusted source/profile/published revision을 얻은 뒤 fixed terminal outcome 하나만
  Runtime recorder에 기록한다. Success만 queue/elapsed/row/byte/truncated 합계에 기여하며 caller,
  tenant, question, SQL, query/fingerprint/PG query ID와 raw 오류는 rollup payload에 없다.
- Runtime은 최대 1,000 pending group, report당 100 delta와 60초 cadence를 사용한다. 트래픽이 없어도
  empty payload를 보내 cursor freshness를 갱신하고, 실패 시 sequence/payload를 그대로 재시도한 뒤
  성공할 때만 pending을 ack한다.
- `gateway_usage_report_cursors`는 stable replica ID/incarnation, monotonic sequence와 payload hash를
  fence/deduplicate한다. Replica ID별 transaction advisory lock이 cursor가 아직 없는 최초 동시 보고도
  직렬화하며 DB-clock 180초 freshness를 저장한다. `gateway_usage_rollups`는
  source/profile/metadata/definition/hour key로 모든 replica의 성공적으로 보고된 값만 더하고 서로 다른
  replica의 commit 순서가 바뀌어도 `observed_at`을 과거로 되돌리지 않는다.
- Metadata/source Control pool은 각각 max two, process당 최대 4라는 기존 계약을 유지한다.
  Resource/gateway Control write를 process-local lock으로 직렬화하고 gateway가 source lock을 기다리는
  동안 replica row를 잠그지 않아 reload/heartbeat pool과 row-lock 경계를 격리한다.

## Persistence And Privilege

Additive migration 4는 다음 세 table만 추가한다.

```text
source_resource_observations
gateway_usage_rollups
gateway_usage_report_cursors
```

Common writer는 resource/cursor에 SELECT/INSERT/UPDATE만, rollup에 source당 1,000행 cap을 위한
DELETE를 포함한 SELECT/INSERT/UPDATE/DELETE만 갖는다. 어느 table에도 TRUNCATE가 없고 authority,
receipt와 replica observation DELETE 권한도 없다. Application rollback은 migration ledger, table과
data를 남긴다.

## Evidence

| Boundary | Result |
|---|---|
| Manifest strict parsing, immutability, metadata revision/public projection exclusion | PASS |
| Exact catalog relation/kind query와 네 fixed metric, 기존 reader/max-one pool | PASS |
| Daily concurrent sample/same-bucket/current-previous/definition-reset/DB-clock freshness transaction | PASS |
| Six terminal outcomes, success-only sums, bounded groups와 privacy exclusion | PASS |
| Empty report, concurrent first report, exact retry/ack, sequence/hash/incarnation fencing과 atomic rollback | PASS |
| Logical 31-day input window, no age-only DELETE와 source당 latest 1,000 cap | PASS |
| Two-replica additive rollup/cap/replay와 gateway lock wait 중 heartbeat isolation | PASS |
| Migration 4 fresh/N-1 apply, checksum/rollback, least privilege와 restore drill | PASS |
| Existing HTTP/MCP/health/readiness/admin metrics unchanged | PASS |

## Commands And Results

| Command | Result |
|---|---|
| `git diff --check` | PASS |
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 29 source files |
| `uv run pytest` | PASS — 613 passed, 59 deselected |
| `uv run pytest -m integration` | PASS — 47 passed, 625 deselected |
| `bash -n scripts/control-plane-drill.sh` | PASS |
| `./scripts/control-plane-drill.sh` | PASS — custom archive, 12 tables, migration ledger, 14 FKs, 4 triggers, immutable history/receipts, observations and writer ACL |

## Rolling Compatibility And Rollback

Apply migration 4 before replacing application replicas. Old applications ignore the new tables and keep the
existing data plane and source lifecycle. New applications may report only after their stable replica registration
succeeds. Replace replicas sequentially; mixed versions produce a lower-bound rollup rather than a complete bill.

Rollback the application without editing, reverting or dropping migration 4. Old applications stop refreshing
resource/gateway observations, so cursor/resource freshness expires but query readiness and source authority do
not change. Re-deploying the new application with the same stable slot obtains a new incarnation and starts its
gateway sequence at one.

## Deliberate Limits

- `CTRL-08` owns public `not_configured|pending|available|stale|unavailable`, last-attempt/reason and exact
  admin response. It must apply the DB-clock 31-day logical cutoff when reading rollups.
- Rows older than 31 days may remain physically when a source has fewer than 1,000 rollup keys. They are not
  eligible for future projection and no age-only cleanup SLA exists.
- Resource estimates inherit PostgreSQL `ANALYZE` accuracy and are not exact counts. Provider billing,
  monetary allocation, PostgreSQL execution/block/temp/WAL aggregates and caller/tenant chargeback remain out
  of scope.
- Changing metric/method/definition, report/fencing, logical retention, 1,000-row cap, connection budget,
  writer privilege or public projection requires explicit contract approval.
