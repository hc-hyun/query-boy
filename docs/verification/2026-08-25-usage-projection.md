# Usage Projection Audit — 2026-08-25

Status: Complete

## Scope

`CTRL-08`은 승인된 latest resource attempt/last-success와 global gateway reporter 상태를 Control
Plane에서 계산하고, operator-only `GET /admin/sources/{source_id}/usage`로 공개한다. 기존 source
목록·상세·replica endpoint와 HTTP query/MCP surface는 바꾸지 않는다. Provider monetary cost는
근거가 없으므로 값을 추정하지 않고 `not_configured/PROVIDER_NOT_CONFIGURED`만 제공한다.

## Persistence And Writer Boundaries

- Additive migration 5는 latest-only `source_resource_observation_attempts`를 추가한다. `source_id`가
  primary key이고 generation, succeeded/failed outcome, 승인된 failure reason, DB-clock attempt time,
  nullable last-success time과 representative-presence marker를 저장한다. `(source_id, generation)`은
  existing profile revision을 참조한다.
- 기존 `source_resource_observations`는 current/previous last-success 값으로 남는다. 같은 generation의
  실패는 성공값을 보존하고 새 generation의 실패는 이전 generation 성공값을 초기화한다.
- 성공 보고는 attempt와 mandatory `table_bytes`, `index_bytes`, `total_storage_bytes` 및 optional
  `representative_records`를 같은 transaction/DB clock으로 기록한다. 실패 보고는
  `METADATA_UNAVAILABLE` 또는 `RESOURCE_READ_FAILED`만 받는다.
- Resource writer는 active/enabled/current generation으로 fence하고 성공 writer는 current active
  metadata revision까지 fence한다. 새 attempt table writer ACL은 SELECT/INSERT/UPDATE뿐이며
  DELETE/TRUNCATE는 없다.

## Projection Behavior

- Resource는 `not_configured|pending|available|stale|unavailable` 중 하나다. Disabled source,
  current-generation attempt 부재, failed attempt, freshness 만료를 서로 구분한다. Fresh last success가
  있으면 latest attempt가 실패여도 값은 available이고 nested attempt가 실패를 드러낸다.
- 성공 marker와 mandatory row, optional-presence marker 또는 freshness가 충돌하면 Control decode
  장애로 확대하지 않고 `unavailable/OBSERVATION_INCOMPLETE`, 빈 metrics와 null `fresh_until`을
  반환한다. Persisted value의 type/cardinality decode 실패는 fail-closed Control 503이다.
- Equality boundary `read_at == fresh_until`은 fresh다. Current/previous metric은 DB에 실제로 보고된
  값만 제공하며 빠진 값이나 시간 bucket을 0으로 만들지 않는다.
- Gateway 상태는 source traffic completeness가 아니라 reporter pipeline 전체 상태다. 같은 DB clock의
  live replica와 current-incarnation cursor를 비교해 all-live-current-fresh일 때만 available이다. 일부
  absent/expired는 `REPORTER_UNAVAILABLE`, live replica가 없고 accepted cursor가 있으면
  `REPORTER_EXPIRED`, cursor도 없으면 `NOT_REPORTED`다.
- `last_report_at`은 accepted cursor 전체의 최대 observed time이다. `fresh_until`은 모든 live replica가
  current cursor를 가질 때 그 최소값이며, 하나라도 없으면 null이다. Retired/stale replica row는 live
  계산에서만 제외하고 삭제하지 않는다.
- Rollup 조회는 한 repeatable-read snapshot에서
  `[UTC-hour(read_at)-31 days, UTC-hour(read_at)]` inclusive 범위와 fixed order를 적용한다. 31일은
  logical visibility/input window이고 나이만으로 age-only physical delete하지 않는다. Source별 최신
  1,000행 storage cap은 유지하고 API pagination은 추가하지 않는다.

## Delivery, Privacy And Failure Boundaries

- Middleware 인증 실패는 401이고 operator가 아니면 path/query validation보다 먼저 403이다. 모든
  query parameter와 잘못된 source path는 400, unknown source는 404, Control read/decode/cardinality
  실패는 secret-free 503이다. Availability/staleness는 정상 projection이므로 200이다.
- Exact response root는 `source_id`, `enabled`, `read_at`, `resource`, `gateway`, `monetary_cost`다.
  Gateway rollup에는 source ID를 반복하지 않으며 monetary cost에는 amount/currency/provider field를
  만들지 않는다.
- Runtime failure reporting과 service/HTTP 오류에는 credential, token, SQL literal, 질문, parameter,
  relation name과 raw internal database 오류를 남기지 않는다. Resource collection 실패는 best-effort로
  Control Plane에 보고하고 query readiness/data path를 바꾸지 않으며 cancellation은 전파한다.

## Evidence

| Boundary | Result |
|---|---|
| Migration 5, generation FK/check, least-privilege ACL and migration-first compatibility | PASS |
| Atomic success/latest failure, generation reset/preservation and writer fencing | PASS |
| Resource five-state priority, latest failure with fresh success and equality boundary | PASS |
| Incomplete marker as 200 state; malformed persisted decode/cardinality as redacted 503 | PASS |
| Global all-live reporter state, last report/freshness and inclusive 31-day fixed-order rollups | PASS |
| Operator-first HTTP validation, exact response, no-query and existing HTTP/MCP regression | PASS |
| Runtime reason mapping, best-effort isolation, cancellation and privacy redaction | PASS |
| Rolling migration, rollback preservation and Control restore drill | PASS |

## Commands And Results

| Command | Result |
|---|---|
| `git diff --check` | PASS |
| `bash -n scripts/control-plane-drill.sh` | PASS |
| `uv run ruff check .` | PASS |
| `uv run mypy src` | PASS — 29 source files |
| `uv run pytest` | PASS — 625 passed, 64 deselected |
| `uv run pytest --ignore=tests/test_documentation.py` | PASS — 611 passed, 64 deselected |
| `uv run pytest -m integration` | PASS — 52 passed, 637 deselected |
| `./scripts/control-plane-drill.sh` | PASS — custom archive, 13 tables, migration ledger, 15 FKs, 4 triggers, immutable history/receipts, observations and writer ACL |

## Rolling Compatibility And Rollback

Migration 5를 application보다 먼저 적용한다. 구버전 application은 새 table과 endpoint를 모르며 기존
query/source lifecycle을 계속 수행한다. 새 application은 migration 5가 있는 Control DB에서만 latest
attempt를 기록하고 usage projection을 제공한다.

Application rollback은 migration ledger, attempt table, observation/rollup/cursor table과 데이터를
그대로 보존한다. 구버전 Runtime은 migration 4 resource/gateway report는 계속하지만 latest-attempt row를
갱신하지 않는다. 이후 새 application을 다시 배포했을 때 resource는 freshness 만료 또는 legacy sample과
marker 불일치에 따른 `OBSERVATION_INCOMPLETE`를 보일 수 있고, 다음 atomic success로 회복한다. 이 상태는
source authority와 query readiness를 바꾸지 않는다. 물리적 table/data 삭제는 rollback 절차에 포함하지
않는다.

## Deliberate Limits

- Gateway rollup은 성공적으로 보고된 privacy-safe lower bound이며 source traffic 전체나 청구액이 아니다.
- Provider billing, amount/currency, DB-native statement statistic, caller/tenant chargeback은 구현하지 않았다.
- Source별 1,000행 cap 외에 age-only deletion SLA를 추가하지 않았다.
- State/reason priority, freshness, generation fence, schema/ACL, exact response, retention 또는 rollback
  의미를 바꾸면 해당 interface/format/policy 승인이 필요하다.
