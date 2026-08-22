# Production Release Acceptance — 2026-08-23

## Outcome

Architecture Success Criteria와 roadmap `REL-01`~`REL-08`을 모두 검증했다. Bootstrap source
두 개와 control-plane으로 실행 중 등록한 세 번째 source가 동일한 registry, metadata,
authorization, SQL validation, executor와 MCP code path를 사용한다.

## Acceptance Matrix

| ID | Executed evidence | Result |
|---|---|---|
| REL-01 | `development_issues`, `market_voc`, `support_tickets` 독립 DB | 동일 runtime path PASS |
| REL-02 | 실행 중 support source L0→L1→verified→L2 publish 후 MCP list/context/query | 재시작·재배포 없이 PASS |
| REL-03 | Retrieval evaluation 16건과 verified SQL/result invariant 9건 | 16/16, 9/9 PASS |
| REL-04 | [`security-evaluation.yaml`](../../config/security-evaluation.yaml)의 version 1 corpus | 22/22 reject PASS |
| REL-05 | 40-query load, source별 queue/concurrency, cross-source progress, cancel/rollback, plan/row/byte/timeout limit | PASS |
| REL-06 | Overlay drift, source outage stale fallback/expiry, immutable active revision rollback/pin/resume | PASS |
| REL-07 | Operator health/metrics, alert threshold, redacted audit, backup/restore와 security update runbook | 검토 PASS |
| REL-08 | 아래 Architecture Success Criteria mapping | 전부 PASS |

## Load And Hard-Limit Evidence

격리된 local fixture에서 `uv run pytest -m load -s`로 source당 20개, 총 40개 query를
실행했다.

```json
{"elapsed_ms_max":578,"elapsed_ms_p50":400,"elapsed_ms_p95":554,
 "plan_total_cost_max":215.55,"queries":40,"queue_ms_max":509,
 "queue_ms_p95":481,"sources":["development-issues","market-voc"]}
```

별도 통합 시나리오는 한 source의 slot이 찬 동안 다른 source query가 완료되는지,
queue timeout, client/operator cancel, rollback 후 connection 재사용, 1,000-row truncation,
1 MiB byte 상한, plan rejection과 statement timeout을 검증한다. 부하 측정은 다른 DB stress
test와 동시에 실행하지 않는다. Fixture reader의 `CONNECTION LIMIT 3`은 query pool 2개와
metadata connection 1개를 의도적으로 강제하기 때문이다.

## Failure Recovery Evidence

| Failure | Expected recovery | Evidence |
|---|---|---|
| Semantic/schema drift | 새 revision 거부, stale로 우회하지 않음 | `test_fails_closed_on_drift_even_with_cache` |
| 일시 source outage | 마지막 정상 revision을 제한 시간 동안 stale로 제공 | `test_returns_stale_revision_after_refresh_failure` |
| Stale 상한 초과 | `METADATA_UNAVAILABLE` fail-closed | `test_source_outage_fails_closed_after_stale_limit_expires` |
| 잘못된 source update | 현재 generation/profile/pool 유지 | source admin regression |
| Control revision rollback | 이전 immutable revision pin, 명시적 resume 후 신규 revision 활성화 | metadata store regression |
| Control-plane loss | 격리 DB restore 후 5개 table count 일치 | `control-plane-drill.sh` PASS |

## Operations Review

- Dashboard contract와 alert threshold: [`operations.md`](../operations.md)
- Migration, backup, restore, RPO/RTO: [`disaster-recovery.md`](../disaster-recovery.md)
- No-deploy operator 절차와 rollback: [`source-onboarding.md`](../source-onboarding.md)
- Structured log redaction과 audit tests: bearer/credential/SQL literal/DB detail 비노출 PASS
- Security automation: dependency, Git history secret, filesystem/config, PostgreSQL image scan PASS

## Architecture Success Criteria Mapping

| Criterion | Enforcement/evidence |
|---|---|
| Source 추가에 code 변경 없음 | Versioned manifest와 encrypted credential을 admin API로 publish |
| `source_id` runtime 분기 없음 | 세 번째 source acceptance가 공통 registry/gateway/executor/MCP 사용 |
| 보안·비용 정책을 gateway가 강제 | AST/OID allowlist, read-only transaction, plan/timeout/concurrency/row/byte limit |
| 필요한 DB만 semantic/curated view 보강 | L0/L1/L2 publish gate와 support L2 promotion |
| 실제 질문/SQL 회귀 검증 | Quality evaluation과 immutable verified query/result contract |

## Final Regression

```text
uv run ruff check .                 PASS
uv run mypy src                     PASS (24 source files)
uv run pytest                       PASS (141 unit tests)
uv run pytest -m integration        PASS (13 PostgreSQL/MCP/load tests)
uv run query-man-evaluate           PASS (16/16, max context 13,509 bytes)
uv run query-man-verify             PASS (9/9 verified SQL contracts)
./scripts/control-plane-drill.sh     PASS (5/5 control tables)
```
