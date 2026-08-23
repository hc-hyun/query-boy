# Source Management Catalog Audit — 2026-08-23

Status: Complete

## Scope

`CTRL-04`의 immutable source provenance와 admin-only source inventory, effective detail 및
generation history 계약을 검증한다. 이 단계는 기존 `source_profile_revisions.manifest`와 active
pointer를 재사용하며 새 Control table, column, migration 또는 dependency를 추가하지 않는다.

## Implemented Contract

- Source manifest는 strict version 2만 허용하고 `provenance.owner`, `environment`,
  `database_migration_ref`를 모두 요구한다. 이전 version과 누락·추가·제어문자·unbounded 값은
  자동 보정 없이 거부한다.
- Provenance는 immutable generation에 포함된다. Owner와 DB migration reference 변경은 새
  generation을 만들지만 metadata revision을 바꾸지 않는다. Environment는 host, port, database,
  user와 TLS처럼 source identity에 고정한다.
- `GET /admin/sources`는 source ID 오름차순 keyset pagination과 owner, environment, enabled,
  budget-profile exact filter를 사용하며 비활성 source도 기본 inventory에 포함한다.
- `GET /admin/sources/{source_id}`는 current generation, resource tier의 명시적 hard limit,
  generation-published metadata revision과 현재 active metadata pointer를 구분해 반환한다.
- `GET /admin/sources/{source_id}/history`는 immutable generation을 내림차순 keyset page로
  반환한다. Rollback해 더 낮은 generation이 current여도 생성 순서를 다시 쓰지 않는다.
- 세 endpoint 모두 operator-only다. Query identity는 path/query validation과 Control-store 접근
  전에 403으로 거부한다. Unknown source는 operator에게만 bounded 404를 반환한다.
- Admin read SQL은 raw manifest, nonce/ciphertext, metadata snapshot과 verified-query table을
  선택하지 않는다. 허용한 JSON path와 lifecycle/metadata pointer만 읽고 bounded read model로
  decode한다. Response에는 plaintext credential, secret locator, semantic 자유형 text, question,
  SQL과 내부 DB 오류가 없다.

## Evidence

| Boundary | Evidence | Result |
|---|---|---|
| Manifest cutover | v0/v1/future version, provenance shape/length/control/extra-field corpus | PASS |
| Revision semantics | owner/environment/migration-reference 변경 전후 metadata hash 동일 | PASS |
| Lifecycle | provenance-only republish, credential preservation, environment rebind rejection, rollback restoration | PASS |
| Store projection | exact filters, two-source list cursor, generation cursor, disabled inventory와 strict decode | PASS |
| Contract parity | PostgreSQL identifier 63-character boundary를 manifest/filter/decoder가 동일하게 허용·거부 | PASS |
| Revision distinction | metadata refresh 뒤 published generation revision과 active pointer가 다르게 조회됨 | PASS |
| Snapshot consistency | current header와 generation page를 한 Control DB statement snapshot에서 조회 | PASS |
| Authorization/error | 두 query token의 세 GET 및 invalid path/query가 operator check에서 403; admin 400/404/503 bounded | PASS |
| Redaction | API/store projection에 credential, env locator, ciphertext, raw semantic/verified content 없음 | PASS |
| Managed acceptance | disposable Control DB의 L0→L1→L2 history, HTTP pagination, admin 200, query 403, unknown 404 | PASS |

```text
uv run pytest tests/test_registry.py tests/test_revision.py -q
  37 passed
uv run pytest tests/test_source_admin.py tests/test_http.py -q
  52 passed
uv run pytest tests/test_source_store.py -q
  23 passed, 1 deselected
COMPOSE_PROJECT_NAME=query-man uv run pytest -m integration \
  tests/test_source_store.py tests/test_control_startup.py -q
  2 passed, 23 deselected
uv run ruff check .
  PASS
uv run mypy src
  PASS (26 source files)
uv run pytest
  342 passed, 33 deselected
uv run pytest -m integration
  21 passed, 354 deselected
```

Integration은 production/development authority를 건드리지 않는 function-scoped disposable Control
DB에서 실행했고 종료 뒤 test database와 container migration 임시 directory가 남지 않았음을
확인한다. 중지했던 두 MCP app container도 같은 image/config로 재시작해 모두 healthy임을
확인했다. Ignore된 secret file은 isolated worktree로 복사하지 않고 원래 workspace의 환경을 test
process에만 주입했으며 값은 command output이나 audit에 기록하지 않았다.

## Test-Derived Correction And Deferred Work

첫 DB integration 실행은 test profile만 L0로 바꾸고 저장 manifest는 L2로 둔 fixture 불일치를
발견했다. 제품 projection이 저장된 L2를 정확히 반환했으므로 manifest 입력을 L0로 맞춰 publish
contract와 test expectation을 일치시켰고 재실행은 통과했다.

독립 리뷰는 세 가지 read-boundary 불일치를 추가로 찾았다. PostgreSQL bigint보다 큰 history
cursor가 DB cast에서 503이 될 수 있어 API/store 양쪽에서 bounded 400/ValueError로 막았다. History
header와 page를 별도 connection에서 읽던 경로는 concurrent pointer transition 때 서로 다른
current를 보일 수 있어 한 SQL statement로 합쳤다. 마지막으로 registry가 길이 제한 없이 허용한
identifier를 catalog decoder가 128자로 제한하던 계약은 PostgreSQL의 ASCII 63-character identifier
상한으로 통일하고 boundary regression을 추가했다.

첫 전체 integration 실행에서는 이미 실행 중인 MCP replica 두 개와 load test가 fixture reader의
의도된 connection budget을 공유해 representative load 한 건이 `QUERY_OVERLOADED`로 실패했다.
두 app container만 일시 중지하고 PostgreSQL은 유지한 격리 재실행에서 21개가 모두 통과했으며
trap으로 두 container를 즉시 healthy 상태로 복원했다. CI가 상시 dev replica와 같은 database에서
integration을 병행하게 되면 load fixture를 전용 disposable reader/database로 옮긴다. 이 관측을
이유로 production reader limit이나 query queue 상한을 늘리지 않는다.

이번 history는 generation creation과 current pointer만 뜻한다. Credential rotation,
deactivate/rollback의 actor, reason, outcome과 시간 chronology를 추정하지 않으며 `CTRL-05`가
append-only mutation receipt/audit로 추가한다. Replica별 실제 적용 상태는 `CTRL-06`, size/cost
observation은 `CTRL-07`/`CTRL-08` 범위다.

Exact filter는 현재 pointer 규모에서 JSON projection scan을 사용한다. 측정된 latency나 EXPLAIN
근거 없이 expression/composite index를 미리 추가하지 않는다. 운영 source 수가 늘어 page SLA를
넘는 증거가 생기면 같은 query shape를 기준으로 index를 별도 migration에서 검증한다.

`ruff C90` 진단에서 새 catalog/store/admin service 함수는 threshold 10을 넘지 않았지만 기존
`build_app`은 변경 전 49에서 admin GET route 추가 후 55가 됐다. `CTRL-05`에서 endpoint를 더
늘리기 전에 source-admin route registration만 작은 helper/module로 분리하고 operator-first
validation과 기존 lifespan/middleware 순서를 회귀 테스트로 보존한다. 이를 범용 controller
framework나 새 dependency 도입의 근거로 사용하지 않는다.

이전 source manifest generation을 자동 변환하지 않는 의도적 pre-release cutover다. Version 1
Control data가 있는 개발 환경은 새 runtime 배포 전에 격리 backup 후 Control DB를 재생성하고
reviewed version 2 manifest로 다시 publish한다. Immutable history를 SQL로 임의 backfill하거나
`unknown` provenance를 만들지 않는다.
