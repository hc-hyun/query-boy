# Runtime Replica Observation Audit — 2026-08-25

Status: Complete

## Scope

`CTRL-06`에서 managed runtime의 stable replica identity, latest-only desired/applied observation,
DB-clock freshness와 operator-only source별 조회를 구현했다. 기존 source list/detail/health/metrics,
public health/readiness, query/MCP response와 mutation receipt 의미는 바꾸지 않았다.

구현 계약은 [ADR 0016](../decisions/0016-centralized-source-management-plane.md)의 2026-08-25 사용자
승인 범위다. Replica retirement/delete/expiry, shutdown deregistration, planned-target inventory,
size/usage/cost observation과 새 readiness 정책은 추가하지 않았다.

## Implemented Contract

- Managed mode는 1~80자의 lowercase stable slug `QUERY_MAN_REPLICA_ID`를 필수로 요구한다.
  Bootstrap은 이 값이 있어도 읽거나 검증하지 않는다. 동시에 실행되는 slot은 ID를 공유하지 않고
  같은 slot 재시작은 ID를 재사용한다.
- Additive migration 3은 `runtime_replicas`와 `runtime_source_observations`에 ever-registered slot과
  replica/source별 latest observation만 저장한다. Registration은 incarnation을 증가시켜 이전
  process report를 fencing하며 delete/retirement/deregistration 경로는 없다.
- Runtime은 한 번만 registration하고 실제 cadence
  `max(QUERY_MAN_SOURCE_RELOAD_INTERVAL_MS, 5000)`ms로 best-effort report한다. Registration, report와
  fencing 실패 뒤 같은 process가 재등록하지 않는다.
- Desired는 current active source와 active metadata pointer에서 계산한다. Applied는 실제 replica가
  적용한 enabled/generation/state version, cache에 적용한 metadata revision과 source health다.
  Disabled desired에는 metadata/source-health drift를 적용하지 않는다.
- Freshness는 Runtime 시간이 아니라 한 DB read clock에서 `observed_at + 3 × cadence`까지다.
  Projection은 `pending|available|stale|unavailable`, fixed-order drift와
  `NOT_OBSERVED|HEARTBEAT_EXPIRED|CONTROL_SCAN_FAILED|RUNTIME_VALIDATION_REJECTED|RUNTIME_APPLY_FAILED|METADATA_PROBE_FAILED|null`
  이외의 reason을 공개하지 않는다.
- Operator-only `GET /admin/sources/{source_id}/replicas?limit&after_replica_id`는 C-collation replica ID
  keyset page를 반환한다. Known source의 stale/unavailable replica도 HTTP 200이며 raw 오류, manifest,
  connection, credential, question과 SQL은 저장하거나 반환하지 않는다.
- Observation failure는 data plane, readiness, existing source/component health, mutation receipt와
  shutdown 순서를 바꾸지 않는다. Candidate staging은 production observation을 갱신하지 않는다.

## Evidence

| Boundary | Evidence | Result |
|---|---|---|
| Configuration | Managed missing/invalid ID를 fail-closed하고 valid boundary를 허용하며 bootstrap invalid ID를 무시한다. | PASS |
| Persistence and fencing | First registration, same-slot incarnation increment, old-incarnation rejection, latest-only upsert와 ever-registered target retention을 검증한다. | PASS |
| Freshness and projection | DB clock, exact 3-cadence boundary, millisecond stale age, status priority, fixed drift order, disabled semantics와 bounded reason을 검증한다. | PASS |
| Least privilege | Writer가 두 observation table의 SELECT/INSERT/UPDATE만 갖고 DELETE/TRUNCATE와 authority/schema 권한은 얻지 않는다. | PASS |
| Runtime lifecycle | 시작 직후 한 번 등록/report하고 report cadence를 고정하며 registration/report 실패 뒤 재등록하지 않고 reload/cancellation을 유지한다. | PASS |
| Runtime applied state | Scan, validation, apply와 metadata probe 결과를 bounded reason으로 기록하고 성공 재수렴, disabled null projection과 staging suppression을 검증한다. | PASS |
| Metadata signal | 실제 cache publish/restore/invalidate만 revision을 갱신하고 failure/stale fallback 및 candidate staging이 정상 applied revision을 오염시키지 않는다. | PASS |
| Delivery | Operator-first auth, exact path/query parsing, pagination, 404/503 redaction과 unchanged existing surface를 검증한다. | PASS |
| Multi-replica acceptance | 두 managed application이 같은 L2 desired generation/state/metadata에 `available`, `drift=[]`로 수렴하고 deactivate 뒤 둘 다 정상 disabled projection으로 수렴한다. | PASS |
| Restore compatibility | Migration 3의 두 table을 archive/restore row-count와 9-table/8-FK schema 및 writer ACL drill에 포함한다. | PASS |

## Commands And Results

```text
uv run ruff check .
  PASS
uv run mypy src
  PASS (29 source files)
uv run pytest tests/test_runtime_config.py tests/test_operations.py \
  tests/test_metadata.py tests/test_source_admin.py tests/test_managed_mode.py \
  tests/test_http.py tests/test_runtime_startup_cleanup.py -m 'not integration'
  179 passed
uv run pytest -m integration tests/test_control_startup.py \
  tests/test_integration.py::test_onboards_third_source_without_runtime_restart \
  tests/test_integration.py::test_onboards_commerce_edges_across_authenticated_mcp_replicas
  3 passed
uv run pytest
  542 passed, 46 deselected
uv run pytest -m integration
  34 passed, 554 deselected
docker compose exec -T --env PGUSER=query_man_admin \
  --env PGDATABASE=query_man postgres \
  bash /docker-entrypoint-initdb.d/control-migrations/apply.sh
  PASS (current version 3)
./scripts/control-plane-drill.sh
  PASS (9 tables, 8 FKs, 4 triggers, replica observation ACL)
```

Provider persistence/migration focused suites additionally passed 121 non-integration tests and 16 Control
DB integration tests before the provider baseline commit. The repository-wide gates above were rerun after
provider, Runtime, Metadata, Delivery, operations documentation and restore-drill integration.

## Rolling Compatibility And Rollback

Migration 3 is additive. Apply it before the application rollout; old application processes ignore the two new
tables and continue serving. Configure one stable ID per managed deployment slot, then replace application
replicas sequentially. Old processes do not become targets until a new process first registers their slot.

Rollback the application without reverting, editing or dropping migration 3. An old application will stop
reporting, so every already-registered slot it leaves behind becomes stale after its freshness window and remains
in the ever-registered target set. Re-deploying the new application with the original ID increments the
incarnation and resumes observation. Renaming a slot to hide a stale row instead creates another permanent target
and is not a rollback procedure.

No observation write participates in source authority, query execution or mutation receipt transactions.
Therefore Control observation unavailability does not roll back a source mutation, remove a healthy registry or
change readiness. Operators diagnose the dedicated projection and bounded process log events separately.

## Deliberate Limits And Future Triggers

- Never-started planned replicas remain external deployment-inventory responsibility. Scale-down and permanent
  retirement remain visible as stale because this contract has no delete/retirement lifecycle.
- The tables retain latest state rather than a heartbeat history. Historical rollout evidence belongs in the
  deployment/change system, not an unbounded Control DB event stream.
- `CTRL-07`/`CTRL-08` own size, usage and cost observation contracts. The later `CTRL-09`
  [control recovery acceptance](2026-08-25-control-recovery-acceptance.md) covers cross-service
  backup/key recovery and two-replica recovery beyond the schema-level drill updated here; it does not
  turn this observation audit into production RPO/RTO evidence.
- Changing identity, target-set membership, freshness, status/drift/reason, persisted schema, endpoint or
  observation isolation requires a new explicit module-contract approval.
