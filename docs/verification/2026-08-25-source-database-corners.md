# Source Database Corner Acceptance — 2026-08-25

Status: Complete

Last updated: 2026-08-26 (`DBEDGE-03`)

## Scope

`DBEDGE-01`과 후속 `DBEDGE-02`~`DBEDGE-03`은 고정 bootstrap fixture를 더 늘리지 않고 test마다 서로
다른 UUID database를 만들어 Source Catalog → Metadata → Guarded Query의 실제 PostgreSQL 경계를
검증한다.
각 database는 전용 NOLOGIN view owner와 최소 권한 LOGIN reader를 사용하고, pool 종료 뒤 database와
두 role을 삭제한다. Production source/configuration, Control DB와 승인된 public contract는 변경하지
않는다.

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
- Outer disposable fixture는 실패 경로에서도 active connection을 확인한 뒤 database와 role을
  정리한다. 이 fixture의 cleanup target은 앞 단계 실패와 무관하게 독립적으로 시도하고 body/cleanup
  오류를 함께 보존한다. `DBEDGE-03`에서 추가한 multi-resource setup/query cleanup도 같은 방식을
  사용한다. 별도 unit case가 fixture cleanup action 일부 실패 뒤 후속 action과 오류 집계를,
  integration case가 fixture body에서 의도적으로 예외를 발생시킨 뒤 같은 database와 두 role이 모두
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
| Live view drift | Fresh-cache TTL 뒤 `CREATE OR REPLACE VIEW`로 definition만 변경 | Old metadata token은 query 실행 전에 거부되고 새 definition hash/revision에서만 changed result가 실행됐다. |
| Catalog hard limits | Cold cache에서 relation 3개/상한 2, column 3개/상한 2, structure 3개/상한 2와 warm cache에서 relation 1개/상한 1 뒤 두 번째 relation grant | 실제 catalog가 partial snapshot을 publish하지 않았고, warm cache도 이전 snapshot을 stale 성공으로 가장하지 않은 채 `METADATA_UNAVAILABLE`로 fail-closed했다. 초과 object를 제거한 뒤 같은 max-one pool은 정상 snapshot을 다시 publish했다. |
| Unsupported driver values | PostgreSQL infinity date, `int4range`, nonempty `int4multirange`와 nonempty `int4range[]` | 내부 driver/object detail 없이 `QUERY_UNAVAILABLE`로 rollback했고 같은 max-one pool의 다음 supported query가 정상 복구됐다. |
| Multibyte byte boundary | 한글과 emoji를 포함한 두 row, 첫 row compact UTF-8 exact boundary | 첫 complete row만 반환하고 두 번째 row를 부분 직렬화하지 않은 채 `truncated=true`, exact byte count를 유지했다. |
| Open scalar/collection/result-type contract characterization | Month interval/30 days, 서로 다른 큰 JSONB fractional numeric, anonymous record와 string-valued unknown OID, empty multirange/range-array/integer-array, 0/1 lower-bound array, `extra_float_digits=1|3|0|-1|-3`, ISO/DMY/MDY DateStyle, non-postgres IntervalStyle, `standard_conforming_strings`, `transform_null_equals`, `array_nulls`와 `bytea_output` | Silent hash collision, unsupported SQL type의 accidental success, SQL 의미/value/hash drift와 driver availability failure를 exact golden으로 재현했다. `bytea_output=hex|escape`는 같은 Base64/hash라는 negative control이었다. 의미 수정은 `ENC-01` 승인 전 중단했다. |

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

### Fixed: warm cache가 catalog hard-limit drift를 숨겼다

Cold cache의 relation/column limit은 이미 `METADATA_UNAVAILABLE`로 닫혔지만, 정상 snapshot을 한 번
cache한 뒤 새 relation이 상한을 넘으면 catalog의 generic `RuntimeError`를 일시 장애로 분류해 이전
snapshot을 stale로 제공했다. 새로 grant된 relation을 무시한 채 정상처럼 보이는 결과였다.

Catalog의 명시적 column/structure/relation/per-relation-column 상한과 불가능한 structure shape만
Metadata module 내부 validation 오류로 분류하고 stale fallback 없이 닫았다. 일반 provider
`RuntimeError`/`ValueError`와 DB outage는 기존 bounded-stale 대상이다. 따라서 public exception/wire,
budget 의미와 cache TTL/stale window는 바꾸지 않고 기존 “validation은 fail-closed, transient
catalog failure만 bounded-stale” 계약에 구현을 맞췄다.
상한 판정 뒤 rollback도 실패하는 fake에서 원래 validation 오류를 primary로 보존해 warm cache가
다시 열리지 않고 `METADATA_UNAVAILABLE`인 것도 단위 회귀로 고정했다.

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

### Approval-required follow-up: lossless interval, JSONB numeric, and reader formatting

PostgreSQL 18/psycopg default loader를 실제 read-only query로 확인한 결과, 현재 encoder가 복구할 수
없는 silent loss 두 건이 있다.

| Value/default | Driver value/behavior | Finding |
|---|---|---|
| `interval '1 month 2 days 03:04:05.6'` | `timedelta(days=32, seconds=11045, microseconds=600000)` | Calendar month와 고정 30일이 구분되지 않는다. |
| `'{"amount":12345678901234567890.1234567890}'::jsonb` | `{"amount": 1.2345678901234567e+19}` | Fractional numeric precision/scale이 binary float에서 사라진다. |
| 같은 `float8` value `1.2345678901234567`, `extra_float_digits=1|3` 대 `0|-1|-3` | text decode 전에 다른 자릿수의 Python float | Role/session default에 따라 public number와 hash가 달라질 수 있다. |
| 같은 ambiguous literal `'01/02/2024'::date`, ISO/DMY/MDY `DateStyle` | DB 오류, `2024-02-01`, `2024-01-02` | 같은 SQL의 성공 여부와 날짜 의미/hash가 달라진다. Non-ISO style의 timestamptz output은 추가로 psycopg decode에 실패한다. |
| 같은 interval, `IntervalStyle=postgres` 대 non-postgres | Loss-prone `timedelta` 또는 psycopg `NotImplementedError` | Role/session default에 따라 silent loss 또는 query availability failure가 달라진다. |
| `'{}'::int4multirange` 대 `'{}'::integer[]` | 둘 다 public `[]` | Empty multirange만 generic Sequence로 성공해 지원되는 SQL array와 같은 value/hash가 되고, nonempty multirange는 range element에서 비공개 실패한다. |
| 같은 backslash string literal, `standard_conforming_strings=on|off` | Backslash+`n` 또는 실제 newline | 같은 SQL text의 string value/hash가 달라진다. |
| `NULL = NULL`, `transform_null_equals=off|on` | `null` 또는 `true` | 같은 SQL predicate 의미/value/hash가 달라진다. |
| `'{NULL}'::text[]`, `array_nulls=on|off` | `[null]` 또는 `["NULL"]` | 같은 SQL array literal 의미/value/hash가 달라진다. |
| `'[0:1]={10,20}'::integer[]` 대 `'{10,20}'::integer[]` | 둘 다 public `[10,20]` | PostgreSQL array lower bound가 사라져 다른 배열이 같은 value/hash가 된다. |
| `'{}'::int4range[]` 대 `'{}'::integer[]` | 둘 다 public `[]` | Empty range array는 accidental success하고 nonempty range array는 비공개 실패한다. |
| `ROW()` 대 `ROW(NULL::integer)` | 둘 다 public `[]` | Anonymous record의 field count와 NULL이 사라져 같은 hash가 된다. |
| `ROW(1::integer)` 대 `ROW('1'::text)` | 둘 다 public `["1"]` | Anonymous record field type이 사라져 같은 hash가 된다. |
| `money`, `point`, `xml` column 대 같은 text | psycopg가 모두 Python `str`로 반환 | Result OID를 보지 않는 encoder가 unsupported SQL type을 text처럼 성공시킨다. |
| 같은 `bytea`, `bytea_output=hex|escape` | 둘 다 `base64:AP8=` | Psycopg bytes loader와 encoder가 같은 value/hash로 정규화해 pin 필요성은 확인되지 않았다. |

실제로 `interval '1 month'`와 `interval '30 days'`는 같은 Python value/hash로 합쳐지지만 기준 날짜에
더한 결과는 다를 수 있고, 서로 다른 큰 fractional JSON 숫자도 같은 float/hash로 합쳐진다. 이는
day-time interval과 ordinary JSON shape가 동작한다는 기존 evidence를 무효화하지 않지만, 일반
interval 및 nested JSON numeric까지 무손실이라고 확대할 수 없게 한다. Reader setting, loader와
canonical row를 고치면 public value, byte count, result hash, SQL policy/metadata revision과 full
verified migration이 바뀐다. 따라서 의미 구현을 멈추고
[proposed ADR 0020](../decisions/0020-lossless-interval-and-json-numeric-encoding.md)의 `ENC-01-A|B|C`
선택과 영향 범위를 사용자 승인 대상으로 올렸다.

Runnable characterization의 exact collision hash는 interval 두 값 모두
`sha256:a1d1217174eb9b0ebce121652ec50bec72411619310ca4f1fee427d55f412014`, JSONB 두 값 모두
`sha256:3b05810025aca001615bd4e78fdbb40763f9d3ea1ba257043625796ba3783ced`였다. 같은 float8 value는
`extra_float_digits=1|3`에서 `1.2345678901234567`이지만 0, -1, -3에서 각각
`1.23456789012346`, `1.2345678901235`, `1.23456789012`로 decode됐다. 같은 ambiguous date literal은
`ISO,YMD`에서 실패하고 DMY/MDY에서 각각 `2024-02-01`/`2024-01-02`와 서로 다른 exact hash를 냈다.
Empty multirange, empty range array와 empty integer array는 모두
`sha256:77f588e368495248abbd8eb87354efadbd31afa38d0ca675154506624470f06a`가 됐다. Lower bound 0/1의
두 integer array도 모두
`sha256:0a4513b560854f795950856ddcddcc1a5f8fac4b0341fce951944bbc8ba066dd`가 됐다.
`standard_conforming_strings=on|off`는 각각
`sha256:f485c1c90af20c905bf5097cde301042a8fb8fa1c69cd0d1b087bed7bfbb7e95`/
`sha256:e96b206dd05fac4069d74fcd73661a9c762e52ca2ce7d1e197589b5a5d1ffe9e`,
`transform_null_equals=off|on`은
`sha256:465ac580f981f85b5e0107198603949c8746915297554f1718aacc0e3fc73bee`/
`sha256:f3b63060353a6de843bdab60cff00570124850083597cbb3ebc09406ddf3af16`,
`array_nulls=on|off`는
`sha256:2ceeafc6cdd6acffce2907fafba6a2490f69e992d58c4516cc7ec548e0383242`/
`sha256:58c554cec2ac89ee75e8ff731df9f8b83ab3511cb79db36e8abda29935e640b0`로 갈렸다.
`bytea_output=hex|escape`는 둘 다
`sha256:2aaa378b22694753a5e7cdfd62a8581ebbef77e9a46dedbe71534041aa288947`였다. Raw setting probe는 오류 뒤
rollback과 마지막 rollback에서 transaction `IDLE`, 최초 reader default 복원을 함께 확인했다.
별도 public QueryService case는 reader role default를 서로 반대로 설정한 fresh Catalog/Query pool에서
같은 metadata/SQL-policy revision과 같은 SQL을 실행해 string, NULL comparison과 array의 public
value와 canonical helper-derived verified hash가 실제로 모두 달라짐을 확인했다. 이 case는 전체 public
응답의 revision, SQL policy, column/row/count/byte/plan shape도 함께 검증한다. 즉 raw driver 현상에
한정되지 않고 현재 public query 경계까지 전파된다.
`ROW()`/`ROW(NULL::integer)`는 empty collection과 같은
`sha256:77f588e368495248abbd8eb87354efadbd31afa38d0ca675154506624470f06a`,
`ROW(1::integer)`/`ROW('1'::text)`는
`sha256:dadd5b0c8d9a51f5db4a5117d804c30dcbcc7f4cfa417a4df154de40d63de4f3`로 합쳐졌다.
PostgreSQL 18 RowDescription은 scalar integer domain을 `int4`, domain-over-`integer[]`를
`int4[]` OID로 보고했지만 array-of-domain, scalar enum과 enum array는 각각의 user-defined OID를
유지했다. 기본 psycopg loader에서 앞의 둘은 `1`/`[1, 2]`, 뒤의 user-defined collection/type은
`"{1}"`/`"ok"`/`"{ok}"` 문자열로 읽혔다. 이 차이는 승인안의 allowlisted base-domain 보존과
visible user-defined type fail-closed 경계를 위한 runnable upgrade sentinel이다.

Repository에 versioned된 verified SQL 11개를 read-only inventory한 결과 ambiguous date/time
literal, interval, range/multirange 결과와 fractional JSON numeric은 없었다. 유일한 explicit
timestamptz literal은 ISO date와 offset을 사용하고, commerce JSONB fixture는 string/bool/null 및
string array만 포함한다. 이는 repository fixture의 예상 결과 보존 근거일 뿐 protected managed
current/rollback inventory를 대신하지 않으며 production cutover 전 외부 전량 확인이 필요하다.

Infinity date, range, nonempty multirange와 nonempty range array는 silent conversion이 아니라 현재
지원하지 않는 driver value다. 새 public encoding을 만들지 않고 bounded `QUERY_UNAVAILABLE`, rollback과
pool recovery acceptance만 추가했다. Empty multirange/range-array 및 lower-bound array의 accidental
success와 record/unknown OID의 type gate 수정은 ADR 0020 승인 범위에 남겼다.

### No additional product change required for the other edges

- Command-like DB comment는 context에 description data로 나타나지만 SQL instruction이나 allowlist로
  해석되지 않았다. Onboarding/Text-to-SQL Skill도 comment를 untrusted data로 취급한다.
- Partition child hiding, materialized-view index discovery, empty result, array/network/non-finite scalar와
  result-byte accounting은 현재 계약대로 동작했다.
- 테스트를 위해 production manifest, source-specific Python branch, dependency 또는 영구 fixture DB를
  추가하지 않았다.
- Exact golden은 PostgreSQL 18의 현재 wire/driver 경계를 기록하므로 disposable fixture는 연결한 server
  major가 18이 아니면 DB/role을 만들기 전에 fail-fast한다. PostgreSQL major upgrade는 이 assertion과
  characterization을 명시적으로 재검토하는 계기다.

## Verification

| Command | Result |
|---|---|
| `uv run ruff check src/query_man/metadata.py tests/test_metadata.py tests/test_source_database_corners.py` | PASS |
| `uv run pytest tests/test_metadata.py -q` | PASS — metadata regression 포함 |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` | PASS — 3 data corner + 1 failure-cleanup, `4 passed` |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` (TIME follow-up) | PASS — UTC/서울/뉴욕 temporal 3 + other corners/cleanup, `6 passed` |
| `uv run ruff check src/query_man/catalog.py src/query_man/metadata.py tests/test_catalog.py tests/test_metadata.py tests/test_source_database_corners.py` (`DBEDGE-02`) | PASS |
| `uv run pytest tests/test_catalog.py tests/test_metadata.py tests/test_source_database_corners.py -m 'not integration' -q` (`DBEDGE-02`) | PASS — rollback 실패와 transient/validation cache 분류 포함 `47 passed`, 16 deselected |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` (`DBEDGE-02`) | PASS — live drift, cold/warm relation·column·structure limits, scalar characterization, unsupported recovery와 multibyte 포함 `14 passed`, 1 deselected |
| `uv run ruff check tests/test_documentation.py tests/test_source_database_corners.py` (`DBEDGE-03`) | PASS |
| `uv run pytest tests/test_catalog.py tests/test_metadata.py tests/test_source_database_corners.py -m 'not integration' -q` (`DBEDGE-03`) | PASS — `47 passed`, 20 deselected |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` (`DBEDGE-03`) | PASS — public QueryService semantic GUC, array identity, domain/enum OID, record/unknown OID characterization 포함 `18 passed`, 1 deselected |
| `uv run pytest tests/test_documentation.py -q` (`DBEDGE-03`) | PASS — `16 passed` |
| `uv run ruff check .` / `uv run mypy src` | PASS — 29 source files, mypy issue 0 |
| `uv run pytest` | PASS — `645 passed`, 85 deselected |
| `uv run pytest -m integration` | PASS — `73 passed`, 657 deselected |
| Prefix residue query | PASS — database `0`, role `0` |

Root static/unit/integration gate와 전체 revision/hash 재발행 결과는 같은 release acceptance에서
별도로 실행하고 [canonical-time evidence](2026-08-25-canonical-time-stability.md)에 기록한다.
