# ADR 0020: Lossless Scalar Encoding, Reader Formatting, And Result Types

Status: Proposed — user approval required before implementation

Date: 2026-08-25

Last expanded: 2026-08-26 (`DBEDGE-03` semantic-setting, array and type-OID characterization)

## Context

`DBEDGE-02`~`DBEDGE-03`의 PostgreSQL 18/psycopg raw-driver read-only probe와 public
QueryService case에서 silent data-loss, SQL semantic setting과 collection identity 경계를 재현했다.

| PostgreSQL value/default | 현재 Python value/동작 | 손실 또는 불안정성 |
|---|---|---|
| `interval '1 month 2 days 03:04:05.6'` | `timedelta(days=32, seconds=11045, microseconds=600000)` | Calendar month가 고정 30일로 평탄화되어 원래 interval과 `32 days`를 구분할 수 없다. |
| `'{"amount":12345678901234567890.1234567890}'::jsonb` | `{"amount": 1.2345678901234567e+19}` | JSONB가 보존한 decimal precision과 scale이 binary float 변환에서 사라진다. |
| 같은 `float8` value `1.2345678901234567`, role별 `extra_float_digits=1|3` 대 `0|-1|-3` | text decode 전에 유효 자릿수가 달라진 Python float | 같은 value의 public number와 hash가 role/session default에 따라 달라질 수 있다. |
| 같은 ambiguous literal `'01/02/2024'::date`, role별 `DateStyle` | `ISO,YMD`는 DB 오류, DMY는 `2024-02-01`, MDY는 `2024-01-02` | 같은 SQL의 성공 여부와 날짜 의미/hash가 role/session default에 따라 달라진다. Non-ISO style의 `timestamptz` output은 추가로 psycopg decode에 실패한다. |
| 같은 interval, role별 `IntervalStyle` | `postgres`는 loss-prone `timedelta`, 다른 style은 default loader `NotImplementedError` | Silent interval loss 또는 비공개 availability failure가 role/database default에 묶인다. |
| `'{}'::int4multirange` 대 `'{}'::integer[]` | Empty psycopg Multirange가 generic `Sequence`로 `[]` encoding | Empty multirange가 지원되는 SQL array와 같은 public value/hash로 성공하지만 nonempty multirange는 range element에서 실패한다. |
| 같은 backslash string literal, role별 `standard_conforming_strings` | `"a\\nb"` 또는 실제 newline을 포함한 `"a\nb"` | 같은 SQL text의 문자열 의미와 hash가 달라진다. |
| `NULL = NULL`, role별 `transform_null_equals` | `NULL` 또는 `true` | 같은 SQL predicate의 three-valued logic 의미와 hash가 달라진다. |
| `'{NULL}'::text[]`, role별 `array_nulls` | `[null]` 또는 `["NULL"]` | 같은 array literal의 element 값과 hash가 달라진다. |
| `'[0:1]={10,20}'::integer[]` 대 `'{10,20}'::integer[]` | 둘 다 Python/public `[10, 20]` | 배열 lower bound `0`과 `1`이 사라져 다른 PostgreSQL 배열이 같은 value/hash로 합쳐진다. |
| `'{}'::int4range[]` 대 `'{}'::integer[]` | 둘 다 Python/public `[]` | Empty range array는 element object가 없어 accidental success하고, 같은 type의 nonempty array는 실패한다. |
| `ROW()` 대 `ROW(NULL::integer)` | 둘 다 Python tuple/public `[]` | Record field count와 NULL이 사라져 같은 value/hash가 된다. |
| `ROW(1::integer)` 대 `ROW('1'::text)` | 둘 다 public `["1"]` | Anonymous record loader가 field type을 잃어 다른 typed field가 같은 value/hash로 합쳐진다. |
| `money`, `point`, `xml` 등 unregistered result OID | psycopg가 Python `str`로 반환 | Encoder가 SQL type을 보지 못하고 PostgreSQL `text`로 오인해 unsupported type이 accidental success한다. |

현재 Guarded Query encoder는 top-level PostgreSQL `numeric`을 decimal string으로 무손실 전달하지만,
psycopg가 이미 평탄화한 `timedelta`와 JSONB 내부 float에서는 원래 값을 복원할 수 없다. 실제로
`interval '1 month'`와 `interval '30 days'`는 같은 `timedelta`/hash가 되지만 기준 날짜에 더한 결과가
다를 수 있고, 서로 다른 큰 fractional JSON 숫자도 같은 binary float/hash로 합쳐진다. 이는
[ADR 0002](0002-guarded-query-contract.md)의 stable scalar 계약과 roadmap `REF-12`의 무손실 의도를
일반 interval/JSONB numeric까지 충족했다고 주장하지 못하게 한다. `extra_float_digits` drift는 원래
float8의 binary value까지 바꾸지는 않지만 같은 값의 public representation과 verified evidence를 흔든다.
`DateStyle`은 output decode뿐 아니라 ambiguous date literal의 DB 입력 의미도 바꾸므로 단순 표시
설정이 아니다. `standard_conforming_strings`, `transform_null_equals`와 `array_nulls`도 parse 단계에서
같은 SQL text의 의미를 바꾼다. 반면 `bytea_output=hex|escape`는 psycopg bytes loader와 현재 Base64
encoder가 같은 value/hash로 정규화하므로 새 pin이 필요하다는 근거는 발견되지 않았다.

반면 PostgreSQL infinity date와 range object는 현재 지원 대상이 아니다. 실제 query는 내부 값을
공개하지 않는 `QUERY_UNAVAILABLE`로 실패하고 rollback/pool 재사용 뒤 정상 query가 복구된다. 이
제안은 infinity/range에 새 public encoding을 추가하지 않는다.

## Current Contract

- Query connection은 psycopg default interval/JSON loader를 사용한다.
- Common reader session은 `TimeZone=UTC`를 고정하지만 `DateStyle`, `IntervalStyle`,
  `extra_float_digits`, `standard_conforming_strings`, `transform_null_equals`와 `array_nulls`는
  설정·검사하지 않는다.
- Python aware datetime만 UTC로 정규화한다.
- `timedelta`는 `str(value)`, top-level `Decimal`은 decimal string, mapping/sequence는 재귀적으로
  encoding한다.
- psycopg Range/Multirange를 Sequence보다 먼저 구분하지 않아 empty multirange만 accidental success한다.
  Array loader는 lower bound를 버리고 Python list만 반환하며, empty range array는 element type을
  encoder가 관찰하지 못해 accidental success한다.
- Result cursor는 column type OID를 canonical encoder에 전달하지 않는다. 따라서 anonymous/named
  composite, money, XML, geometric, bit string과 그 밖의 unregistered OID가 tuple/string/list 같은
  지원 Python type으로 내려오면 현재 SQL type allowlist와 무관하게 성공할 수 있다.
- Public SQL capability는 `bit`/`varbit` cast를 이미 광고한다. Psycopg는 이를 `0|1` text로 반환하므로
  이 두 built-in type을 거부하면 advertised capability와 result contract가 충돌한다. Scalar domain은
  PostgreSQL RowDescription에서 allowed base OID로 내려오지만 enum과 array-of-domain은 user-defined
  OID로 남는 것도 PostgreSQL 18의 runnable disposable-DB probe에서 확인했다.
- SQL policy version 2와 canonical-time material version 1이 현재 revision baseline이다.
- Public row shape, result hash와 immutable verified identity는 현재 encoding 결과에 묶여 있다.

## Options

### `ENC-01-A` — lossless canonical values (recommended)

1. Catalog와 Query transaction의 common deterministic reader settings에 user/catalog SQL보다 먼저
   transaction-local `DateStyle=ISO, YMD`, `IntervalStyle=iso_8601`, `extra_float_digits=1`,
   `standard_conforming_strings=on`, `transform_null_equals=off`, `array_nulls=on` 설정·검사를
   추가한다. Role/database default는 바꾸지 않는다. `bytea_output`은 pin하지 않는다.
2. Current/rollback-preserved verified SQL을 inventory해 날짜/timestamp 입력이 ISO `YYYY-MM-DD` 등
   unambiguous typed literal/expression인지 확인하고, ordinary backslash string literal,
   `expression = NULL`, NULL array literal과 non-1 lower-bound array가 없는지 확인한다. 의미가 달라질
   수 있는 사용이 하나라도 있으면 cutover를 중단하고 해당 contract owner의 별도 결과 승인을 받는다.
3. 사용자 SQL을 fetch하는 bounded result cursor의 adapter context에만 interval text loader를 등록해
   PostgreSQL ISO-8601 interval text를 그대로 Python string으로 받는다. Calendar month, sign, day와
   subsecond를 보존한다.
4. 같은 user-result cursor scope에서만 JSON/JSONB loader의 fractional number를 `Decimal`로 읽는다.
   Recursive encoder는 JSON/JSONB 내부 `Decimal`도 top-level numeric과 같은 exact decimal string으로
   낸다. JSON integer, boolean, null, text, array와 object shape는 유지한다. Catalog, reader-policy
   probe, `EXPLAIN (FORMAT JSON)`과 Control DB JSON adapter는 기존 loader를 유지한다.
5. User-result cursor description의 SQL type OID를 fetch 전에 검사한다. 같은 source의 `pg_type`에서
   namespace/type-kind/base/element를 해석하고 숫자 OID나 Python runtime type만 신뢰하지 않는다.
   Versioned allowlist는 exact built-in `bool`, `int2|int4|int8`, `text|varchar|bpchar`,
   `float4|float8`, `numeric`, `bytea`, `json|jsonb`, `date|time|timetz|timestamp|timestamptz|interval`,
   `uuid`, `inet|cidr`, `bit|varbit`와 이 중 allowed built-in element OID의 array다. `bit|varbit`는
   exact `0|1` string으로 유지하고 existing public `cast_types`에서 제거하지 않는다.
6. PostgreSQL protocol이 allowed base OID로 보고하는 scalar domain 또는 domain-over-approved-array는
   base canonical value로 명시적으로 지원하며 domain identity는 public row/hash에 넣지 않는다. 반면
   RowDescription 또는 array element에서 user-defined OID로 보이는 enum, array-of-domain/enum,
   anonymous/named composite, Range/Multirange, extension/hstore, money, XML, geometric, macaddr,
   `pg_lsn`, `tid`, unknown type은 Python value가 `str`, tuple 또는 list여도 empty/nonempty 모두
   `QUERY_UNAVAILABLE`로 거부한다. 새 enum/exotic wire encoding은 추가하지 않는다.
7. 일반 PostgreSQL array는 모든 dimension lower bound가 1일 때만 기존 list encoding을 유지한다.
   User-result cursor 전용 custom array loader가 원본 dimension header를 확인해 non-1 lower bound를
   평탄화 전에 거부하며 새 array wire shape는 추가하지 않는다.
8. Finite `float4/float8`은 PostgreSQL의 pinned shortest-precise text와 Python float/JSON number shape를
   유지한다. Decimal string으로 새 변환하지 않으며 role default와 무관한 동일 decode를 검증한다.
9. Immutable result-encoding policy material version 2에 reader-format, interval, nested JSON numeric,
   result OID allowlist와 collection rejection
   규칙을 명시하고 SQL policy digest와 모든 metadata revision에 포함한다. SQL policy version은 3이 된다.
10. Public field/table shape와 Control schema는 바꾸지 않는다. 기존 snapshot/generation/verified row는
   수정·삭제하지 않고 새 revision row를 append한다.

장점은 확인된 silent loss를 제거하고 covered scalar가 role/database default와 무관한 exact hash를
갖는다는 것이다.
비용은 interval 및 fractional JSONB가 포함된 public row/hash가 바뀌고, 모든 source revision과
current/rollback-preserved verified contract를 다시 발행해야 한다는 것이다.

### `ENC-01-B` — loss-prone types fail closed

`DateStyle=ISO, YMD`, `extra_float_digits=1`, `standard_conforming_strings=on`,
`transform_null_equals=off`와 `array_nulls=on`은 A와 같이 고정·inventory하고
`IntervalStyle=postgres`를 설정·검사하되, user-result cursor scope의 custom text loader가 interval
전체와 fractional JSON/JSONB number를 손실 전에 감지하면 `QUERY_UNAVAILABLE`로 거부한다. A와 같이
Range/Multirange와 그 array를 empty/nonempty 모두 거부하고 non-1 lower-bound array도 평탄화 전에
거부한다. A와 같은 cursor-description OID allowlist로 record/composite와 unknown/exotic OID도
Python value 변환 전에 거부한다.
조용한 손실은 막지만 현재 지원하던 day-time interval과 JSONB query도 실패할 수 있다. Value를
손실 전에 감지하기 위한 custom loader는 여전히 필요하며 SQL policy/revision과 verified migration도
필요하다. 기능 손실이 커서 권장하지 않는다.

### `ENC-01-C` — defer without production acceptance

구현과 migration을 미루고 현재 known-loss behavior를 production-safe 또는 M14.5 완료로 인정하지
않는다. Runtime이 type/setting exclusion을 강제하지 못하므로 DB-owner 관례만으로 affected query를
허용하지 않는다. C를 선택하면 `ENC-02`, `TIME-03`과 새 production acceptance/cutover를 모두 block한
채 `ENC-01`을 open defer로 유지한다. 이는 data-loss 위험 수용이나 완료 선택지가 아니다.

## Recommended Cutover For `ENC-01-A`

Repository의 TIME R2 구현은 완료됐지만 `TIME-03` production 전환은 아직 실행되지 않았다. A를
선택하면 `TIME-03`을 따로 먼저 실행하지 않는다. `ENC-02`에서 final encoding baseline을 구현·검증한
뒤 R1 protected environment에서 TIME R2와 final ENC baseline을 하나의 coordinated `TIME-03`
cutover로 적용하고 current/rollback-preserved contract를 한 번만 재실행·재발행한다.

1. Protected managed inventory, backup, R1 artifact/ref와 rollback generation을 고정한다.
2. Old fleet와 source connection을 0까지 drain한다.
3. Route 밖 new fleet에서 source별 L1→전체 verified 재발행→L2와 replica convergence를 확인한다.
4. Ambiguous date/time, ordinary backslash string, `expression = NULL`, textual NULL array literal,
   non-1 lower-bound array와 unsupported/custom result OID를 전량 inventory한다. Interval/JSONB changed
   value/hash, Range/Multirange/array, record/composite/enum/extension rejection과 domain/bit/varbit 보존을
   exact 승인한다. 설명되지 않은 column/row/hash/rejection 변화에는 중단한다.
5. Rollback은 new fleet를 drain하고 보존된 R1/pre-cutover generation/revision/L2를 복구한다. 새 row는
   삭제하지 않는다.

Mixed old/new serving fleet는 같은 SQL의 row/hash가 달라 허용하지 않는다.

## Provider And Consumer Impact

- Provider: Source Catalog common reader settings, Guarded Query pool loader/result encoding/SQL policy,
  Metadata revision material.
- Direct consumers: Delivery HTTP/MCP row, Assurance result hash/verified CLI, Control Plane immutable
  verified publish와 Runtime coordinated cutover.
- Persistence: Control schema migration 없음; 새 snapshot/generation/verified rows만 append.
- Security/privacy: SQL, question, credential 또는 token을 새로 저장하지 않는다.
- SQL behavior: `DateStyle=ISO,YMD`의 ambiguous date/time literal, backslash string literal,
  `expression = NULL`, NULL array literal은 기존 role default와 달리 실패하거나 다른 의미를 가질 수
  있으므로 managed verified inventory stop condition이다.
- Result behavior: non-1 array와 unsupported/custom OID는 새로 거부될 수 있다. Scalar allowed-base
  domain과 `bit|varbit`는 기존 base/string shape를 보존한다. Current/rollback verified SQL뿐 아니라
  managed curated relation의 advertised result type inventory도 cutover stop condition이다.
- Data loss: A는 silent loss를 제거한다. B는 값을 거부한다. C는 runtime protection이 없다.

## Verification Required For `ENC-01-A`

- Positive/negative/mixed month, day, microsecond interval의 exact ISO-8601 golden과 arrays/nesting.
- JSON/JSONB의 큰 integer, fractional precision/scale, exponent, nested array/object exact golden.
- Empty/nonempty Range/Multirange 및 그 array는 모두 비공개 fail-closed하고 ordinary empty PostgreSQL
  array는 계속 `[]`인 QueryService golden, rollback과 pool recovery.
- 1-based one/multi-dimensional array는 기존 nested list를 유지하고 0/negative/non-1 lower bound는
  value/hash 평탄화 전에 비공개 fail-closed하는 golden.
- Allowed scalar/array OID corpus, exact `bit|varbit` string, scalar allowed-base domain과
  domain-over-approved-array의 positive base-value 보존 corpus. Enum, array-of-domain/enum,
  anonymous/named record, money, XML, geometric, extension, explicit `pg_lsn`/`tid`와 그 밖의
  non-allowlisted built-in/unknown OID 거부 corpus. Empty composite/unsupported array가 Python
  `[]`/string으로 우회하지 못하며 duplicate column 검사, rollback과 pool recovery 의미를 보존한다.
  Public `cast_types`는 기존 bit/varbit를 유지한다.
- User-result cursor에만 custom interval/JSON loader가 적용되고 `EXPLAIN (FORMAT JSON)`, plan admission과
  exact `plan_summary`는 기존 numeric decode 의미로 정상 동작하는 QueryService acceptance.
- `extra_float_digits=1|3|0|-1|-3`, ISO/SQL/Postgres/German `DateStyle`, supported/unsupported
  `IntervalStyle`, `standard_conforming_strings=on|off`, `transform_null_equals=on|off`와
  `array_nulls=on|off` role default에서 exact 결과. PostgreSQL 18의 `extra_float_digits>=1`은 같은
  shortest-precise 출력을 내며 계약값 1이 더 정밀하다는 주장은 하지 않는다. `bytea_output=hex|escape`는
  같은 Base64/hash라는 negative control을 유지한다.
- Current/rollback-preserved verified SQL의 ambiguous date/timestamp, ordinary backslash string,
  `expression = NULL`, NULL array literal, non-1 lower-bound array와 result OID inventory 및 발견 시 stop.
- UTC/서울/뉴욕 role default, commit/rollback/timeout/cancel 뒤 reader-format과 timezone pool reset.
- Old metadata/SQL policy token의 executor-before rejection.
- Repository fixture와 managed current/rollback inventory 전체 재실행, old/new Control row 공존.
- HTTP/MCP byte accounting, truncation, verified hash와 coordinated rollback.
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`, `uv run pytest -m integration`.

## Approval Boundary

이 ADR은 제안일 뿐 승인된 계약이 아니다. `ENC-01-A`, `ENC-01-B` 또는 미완료 defer인 `ENC-01-C`를
사용자가 정확히 선택하기 전에는 loader, reader-format setting, encoder, revision, verified hash 또는
production cutover를 변경하지 않는다. A/B만 implementation/production completion 선택지다. 권장 A
구현 승인 문구는 다음과 같다.
현재 exact implementation-ready 승인은 A만 아래에 제시한다. B 또는 C를 선택하면 해당 policy version,
migration/cutover, 보존·거부 범위를 다시 exact restatement해 승인받아야 하며 ID 선택만으로 구현하지 않는다.

```text
ENC-01-A를 ADR 0020의 DateStyle=ISO,YMD·IntervalStyle=iso_8601·extra_float_digits=1,
standard_conforming_strings=on·transform_null_equals=off·array_nulls=on,
user-result cursor 전용 lossless interval/JSON numeric,
built-in bool, int2/int4/int8, text/varchar/bpchar, float4/float8, numeric, bytea, json/jsonb,
date/time/timetz/timestamp/timestamptz/interval, uuid, inet/cidr, bit/varbit와
그 allowed built-in element array만 허용하는 versioned result OID allowlist,
scalar allowed-base domain과 domain-over-approved-array의 base canonical value 및
bit/varbit current shape/cast capability 유지, visible enum·array-of-domain/enum·record/composite·
Range/Multirange·extension, money/XML/geometric/macaddr/pg_lsn/tid/unknown 및 그 밖의
non-allowlisted built-in result fail-closed,
1-based array list 유지·non-1 lower-bound fail-closed,
result policy v2·SQL policy v3, provider/consumer 영향, verified SQL inventory,
single coordinated cutover와 immutable rollback 범위로 승인한다.
```
