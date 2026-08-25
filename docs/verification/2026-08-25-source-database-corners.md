# Source Database Corner Acceptance — 2026-08-25

Status: Complete

## Scope

`DBEDGE-01`은 고정 bootstrap fixture를 더 늘리지 않고 test마다 서로 다른 UUID database를 만들어
Source Catalog → Metadata → Guarded Query의 실제 PostgreSQL 경계를 검증한다. 각 database는 전용
NOLOGIN view owner와 최소 권한 LOGIN reader를 사용하고, pool 종료 뒤 database와 두 role을 삭제한다.
Production source/configuration, Control DB와 public contract는 변경하지 않는다.

Runnable acceptance는
[`test_source_database_corners.py`](../../tests/test_source_database_corners.py)다.

## Disposable Isolation

- Database: `query_man_corner_db_<uuid>`
- Reader: `query_man_corner_reader_<same uuid>`
- View owner: `query_man_corner_owner_<same uuid>`
- Reader는 `CONNECT`, curated schema `USAGE`, curated relation `SELECT`만 받고 base relation 권한은
  제거한다.
- Reader session은 기존 read-only timeout/memory/temp/parallel/JIT/search-path contract를 사용한다.
- Temporal acceptance는 role default를 `UTC`, `Asia/Seoul`, `America/New_York`로 각각 만들고,
  production과 같은 reader transaction이 database/role default를 바꾸지 않은 채 transaction-local
  UTC를 설정·검사하는지 검증한다.
- 실패 경로에서도 query/catalog pool을 닫고 active connection을 확인한 뒤 database와 role을
  정리한다. 각 cleanup target은 앞 단계 실패와 무관하게 독립적으로 시도하고 body/cleanup 오류를
  함께 보존한다. 별도 unit case가 일부 cleanup action 실패 뒤 후속 action과 오류 집계를, integration
  case가 fixture body에서 의도적으로 예외를 발생시킨 뒤 같은 database와 두 role이 모두
  사라졌는지 검증한다.

Integration 종료 뒤 다음 residue query 결과는 database `0`, role `0`이었다.

```text
SELECT
  (SELECT count(*) FROM pg_catalog.pg_database
   WHERE datname LIKE 'query_man_corner_db_%'),
  (SELECT count(*) FROM pg_catalog.pg_roles
   WHERE rolname LIKE 'query_man_corner_reader_%'
      OR rolname LIKE 'query_man_corner_owner_%');

0|0
```

## Acceptance Matrix

| Database | 실제 경계 | Result |
|---|---|---|
| Wide/untrusted metadata | 63-column curated view, 62개가 같은 comment phrase에 매칭, command-like column comment, hidden secret base column | Context는 target 8개로 제한되고 secret column은 catalog/context에 없었다. Guarded Query relation allowlist와 PostgreSQL ACL이 base-table 조회를 각각 거부했다. Comment는 data로만 남았다. |
| Temporal/rich scalar | DST 전환 전후 `timestamptz`/`timetz`, interval, IPv4/IPv6 `inet`/`cidr`, array, `NaN`/`Infinity`/`-Infinity`, NULL과 exclusive upper bound | UTC role에서 canonical string/array/non-finite/null encoding, half-open range, ordered rows와 exact UTF-8 result-byte 계산이 일치했다. |
| Structure/empty result | Partitioned parent와 두 child, composite primary key/index, empty materialized view와 unique index | Allowlisted parent와 materialized view만 catalog에 나타나고 partition child는 숨겨졌다. Numeric/NULL row와 empty result `[]`의 columns, row count, bytes와 plan invariants가 일치했다. |

## Findings And Changes

### Fixed: wide question matches exceeded the context target

기존 `_select_context_columns`는 필수 column과 질문에 매칭된 column을 먼저 전부 합쳐 일반 match만으로
profile target을 초과했다. Unit reproduction에서는 target 6에 15개가 반환됐다. 이는
[ADR 0009](../decisions/0009-question-scoped-column-disclosure.md)의 “필수 correctness column만 target
초과 허용” 결정과 달랐다.

수정은 기존 계약 안에서 다음 순서로 선택한다.

1. 필수 correctness column 전체
2. 남은 target까지 질문 match를 ordinal/name 순으로 선택
3. 남은 target을 ordinal/name 순으로 채움

Target 6은 필수 3개와 match 3개, target 2는 필수 3개만 반환하는 unit regression을 추가했다.
Public response field, metadata revision, persisted/wire shape와 budget 의미는 바꾸지 않았다.

### Resolved follow-up: canonical `timestamptz`

같은 PostgreSQL instant도 reader session timezone에 따라 Python datetime offset과 canonical result hash가
달라진다. Read-only reproduction은 다음과 같았다.

| Session TimeZone | Canonical value | Verified result hash |
|---|---|---|
| `UTC` | `2024-03-10T07:00:00+00:00` | `sha256:6d3a744b1171f1b1265a4c6138c01d3cc82f3a2b049a15dab6beddbfb590f6ad` |
| `Asia/Seoul` | `2024-03-10T16:00:00+09:00` | `sha256:35b7f6f1bed58e7e04bd50f50d8f491c6aa85883f6bf2623cc8ea6f42f55844c` |

이 finding의 repository contract는 사용자가 [ADR 0019](../decisions/0019-canonical-time-stability.md)의
정확한 정책과 영향을 승인한 뒤 `TIME-01`~`TIME-02`에서 해결했다. Catalog/Query는
transaction-local UTC를 먼저 설정·검사하고 aware datetime을 UTC `+00:00`으로 정규화한다. 현재
disposable acceptance는
role default `UTC`, `Asia/Seoul`, `America/New_York`, spring/fall DST, naive/date/time/timetz 비변경과
exact verified hash
`sha256:20c9ca4c43400d44c101727ec987b0ae379e086146db1f092da13ac737676549`,
success/rollback/timeout pool reset을 검증한다. 상세 revision/verified migration 결과는
[canonical-time evidence](2026-08-25-canonical-time-stability.md)에 있다. 실제 managed production
재발행과 rollback change record는 열린 `TIME-03`이다.

### No product change required

- Command-like DB comment는 context에 description data로 나타나지만 SQL instruction이나 allowlist로
  해석되지 않았다. Onboarding/Text-to-SQL Skill도 comment를 untrusted data로 취급한다.
- Partition child hiding, materialized-view index discovery, empty result, array/network/non-finite scalar와
  result-byte accounting은 현재 계약대로 동작했다.
- 테스트를 위해 production manifest, source-specific Python branch, dependency 또는 영구 fixture DB를
  추가하지 않았다.

## Verification

| Command | Result |
|---|---|
| `uv run ruff check src/query_man/metadata.py tests/test_metadata.py tests/test_source_database_corners.py` | PASS |
| `uv run pytest tests/test_metadata.py -q` | PASS — metadata regression 포함 |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` | PASS — 3 data corner + 1 failure-cleanup, `4 passed` |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` (TIME follow-up) | PASS — UTC/서울/뉴욕 temporal 3 + other corners/cleanup, `6 passed` |
| Prefix residue query | PASS — database `0`, role `0` |

Root static/unit/integration gate와 전체 revision/hash 재발행 결과는 같은 release acceptance에서
별도로 실행하고 [canonical-time evidence](2026-08-25-canonical-time-stability.md)에 기록한다.
