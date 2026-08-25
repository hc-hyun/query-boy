# ADR 0020: Lossless Scalar Encoding And Reader Formatting

Status: Proposed — user approval required before implementation

Date: 2026-08-25

## Context

`DBEDGE-02`의 PostgreSQL 18/psycopg raw-driver read-only probe와 empty-multirange public
QueryService case에서 silent data-loss와 reader-format stability 경계를 재현했다.

| PostgreSQL value/default | 현재 Python value/동작 | 손실 또는 불안정성 |
|---|---|---|
| `interval '1 month 2 days 03:04:05.6'` | `timedelta(days=32, seconds=11045, microseconds=600000)` | Calendar month가 고정 30일로 평탄화되어 원래 interval과 `32 days`를 구분할 수 없다. |
| `'{"amount":12345678901234567890.1234567890}'::jsonb` | `{"amount": 1.2345678901234567e+19}` | JSONB가 보존한 decimal precision과 scale이 binary float 변환에서 사라진다. |
| 같은 `float8` value `1.2345678901234567`, role별 `extra_float_digits=1|3` 대 `0|-1|-3` | text decode 전에 유효 자릿수가 달라진 Python float | 같은 value의 public number와 hash가 role/session default에 따라 달라질 수 있다. |
| 같은 ambiguous literal `'01/02/2024'::date`, role별 `DateStyle` | `ISO,YMD`는 DB 오류, DMY는 `2024-02-01`, MDY는 `2024-01-02` | 같은 SQL의 성공 여부와 날짜 의미/hash가 role/session default에 따라 달라진다. Non-ISO style의 `timestamptz` output은 추가로 psycopg decode에 실패한다. |
| 같은 interval, role별 `IntervalStyle` | `postgres`는 loss-prone `timedelta`, 다른 style은 default loader `NotImplementedError` | Silent interval loss 또는 비공개 availability failure가 role/database default에 묶인다. |
| `'{}'::int4multirange` 대 `'{}'::integer[]` | Empty psycopg Multirange가 generic `Sequence`로 `[]` encoding | Empty multirange가 지원되는 SQL array와 같은 public value/hash로 성공하지만 nonempty multirange는 range element에서 실패한다. |

현재 Guarded Query encoder는 top-level PostgreSQL `numeric`을 decimal string으로 무손실 전달하지만,
psycopg가 이미 평탄화한 `timedelta`와 JSONB 내부 float에서는 원래 값을 복원할 수 없다. 실제로
`interval '1 month'`와 `interval '30 days'`는 같은 `timedelta`/hash가 되지만 기준 날짜에 더한 결과가
다를 수 있고, 서로 다른 큰 fractional JSON 숫자도 같은 binary float/hash로 합쳐진다. 이는
[ADR 0002](0002-guarded-query-contract.md)의 stable scalar 계약과 roadmap `REF-12`의 무손실 의도를
일반 interval/JSONB numeric까지 충족했다고 주장하지 못하게 한다. `extra_float_digits` drift는 원래
float8의 binary value까지 바꾸지는 않지만 같은 값의 public representation과 verified evidence를 흔든다.
`DateStyle`은 output decode뿐 아니라 ambiguous date literal의 DB 입력 의미도 바꾸므로 단순 표시
설정이 아니다.

반면 PostgreSQL infinity date와 range object는 현재 지원 대상이 아니다. 실제 query는 내부 값을
공개하지 않는 `QUERY_UNAVAILABLE`로 실패하고 rollback/pool 재사용 뒤 정상 query가 복구된다. 이
제안은 infinity/range에 새 public encoding을 추가하지 않는다.

## Current Contract

- Query connection은 psycopg default interval/JSON loader를 사용한다.
- Common reader session은 `TimeZone=UTC`를 고정하지만 `DateStyle`, `IntervalStyle`과
  `extra_float_digits`는 설정·검사하지 않는다.
- Python aware datetime만 UTC로 정규화한다.
- `timedelta`는 `str(value)`, top-level `Decimal`은 decimal string, mapping/sequence는 재귀적으로
  encoding한다.
- psycopg Range/Multirange를 Sequence보다 먼저 구분하지 않아 empty multirange만 accidental success한다.
- SQL policy version 2와 canonical-time material version 1이 현재 revision baseline이다.
- Public row shape, result hash와 immutable verified identity는 현재 encoding 결과에 묶여 있다.

## Options

### `ENC-01-A` — lossless canonical values (recommended)

1. Catalog와 Query transaction의 common deterministic reader settings에 transaction-local
   `DateStyle=ISO, YMD`, `IntervalStyle=iso_8601`, `extra_float_digits=1` 설정·검사를 추가한다.
   Role/database default는 바꾸지 않는다.
2. Current/rollback-preserved verified SQL을 inventory해 날짜/timestamp 입력이 ISO `YYYY-MM-DD` 등
   unambiguous typed literal/expression인지 확인한다. Ambiguous literal이 하나라도 있으면 cutover를
   중단하고 해당 contract owner의 별도 결과 승인을 받는다.
3. 사용자 SQL을 fetch하는 bounded result cursor의 adapter context에만 interval text loader를 등록해
   PostgreSQL ISO-8601 interval text를 그대로 Python string으로 받는다. Calendar month, sign, day와
   subsecond를 보존한다.
4. 같은 user-result cursor scope에서만 JSON/JSONB loader의 fractional number를 `Decimal`로 읽는다.
   Recursive encoder는 JSON/JSONB 내부 `Decimal`도 top-level numeric과 같은 exact decimal string으로
   낸다. JSON integer, boolean, null, text, array와 object shape는 유지한다. Catalog, reader-policy
   probe, `EXPLAIN (FORMAT JSON)`과 Control DB JSON adapter는 기존 loader를 유지한다.
5. Encoder는 psycopg Range/Multirange를 generic Sequence보다 먼저 식별하고 empty/nonempty 모두
   `QUERY_UNAVAILABLE`로 거부한다. PostgreSQL array의 list encoding은 유지하며 새 range wire encoding은
   추가하지 않는다.
6. Finite `float4/float8`은 PostgreSQL의 pinned shortest-precise text와 Python float/JSON number shape를
   유지한다. Decimal string으로 새 변환하지 않으며 role default와 무관한 동일 decode를 검증한다.
7. Immutable result-encoding policy material version 2에 reader-format, interval, nested JSON numeric과
   Range/Multirange rejection
   규칙을 명시하고 SQL policy digest와 모든 metadata revision에 포함한다. SQL policy version은 3이 된다.
8. Public field/table shape와 Control schema는 바꾸지 않는다. 기존 snapshot/generation/verified row는
   수정·삭제하지 않고 새 revision row를 append한다.

장점은 확인된 silent loss를 제거하고 covered scalar가 role/database default와 무관한 exact hash를
갖는다는 것이다.
비용은 interval 및 fractional JSONB가 포함된 public row/hash가 바뀌고, 모든 source revision과
current/rollback-preserved verified contract를 다시 발행해야 한다는 것이다.

### `ENC-01-B` — loss-prone types fail closed

`DateStyle=ISO, YMD`와 `extra_float_digits=1`은 A와 같이 고정·inventory하고
`IntervalStyle=postgres`를 설정·검사하되, user-result cursor scope의 custom text loader가 interval
전체와 fractional JSON/JSONB number를 손실 전에 감지하면 `QUERY_UNAVAILABLE`로 거부한다. A와 같이
Range/Multirange도 empty/nonempty 모두 명시적으로 거부한다.
조용한 손실은 막지만 현재 지원하던 day-time interval과 JSONB query도 실패할 수 있다. Value를
손실 전에 감지하기 위한 custom loader는 여전히 필요하며 SQL policy/revision과 verified migration도
필요하다. 기능 손실이 커서 권장하지 않는다.

### `ENC-01-C` — current behavior를 명시적으로 수용

구현과 migration은 하지 않고 month-bearing interval, fractional JSONB numeric과 Range/Multirange를
production curated view/verified query에서 금지한다. 별도로 DB owner가 reader role/database의
`DateStyle=ISO, YMD`, `IntervalStyle=postgres`, `extra_float_digits=1`을 유지·attest한다. Runtime은
data type exclusion이나 setting을 검증하지 못하므로
Non-Negotiable data-loss 경계를 호출자 관례에 맡기게 된다. 명시적인 위험 수용 없이는 선택하지 않는다.

## Recommended Cutover For `ENC-01-A`

`TIME-03`은 아직 production에서 실행되지 않았다. A를 선택하면 TIME R2를 먼저 전환한 뒤 다시
global revision을 바꾸지 않고, 하나의 coordinated production cutover에서 최종 encoding baseline으로
current/rollback-preserved contract 전체를 재실행한다.

1. Protected managed inventory, backup, R1 artifact/ref와 rollback generation을 고정한다.
2. Old fleet와 source connection을 0까지 drain한다.
3. Route 밖 new fleet에서 source별 L1→전체 verified 재발행→L2와 replica convergence를 확인한다.
4. Ambiguous date/time literal이 없음을 확인하고 exact interval/JSONB changed hash를 승인한다.
   설명되지 않은 column/row/hash 변화에는 중단한다.
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
- SQL behavior: `DateStyle=ISO,YMD`에서 ambiguous date/time literal은 기존 role default와 달리 실패하거나
  다른 의미를 가질 수 있으므로 managed verified inventory stop condition이다.
- Data loss: A는 silent loss를 제거한다. B는 값을 거부한다. C는 runtime protection이 없다.

## Verification Required For `ENC-01-A`

- Positive/negative/mixed month, day, microsecond interval의 exact ISO-8601 golden과 arrays/nesting.
- JSON/JSONB의 큰 integer, fractional precision/scale, exponent, nested array/object exact golden.
- Empty/nonempty Range/Multirange는 모두 비공개 fail-closed하고 empty PostgreSQL array는 계속 `[]`인
  QueryService golden, rollback과 pool recovery.
- User-result cursor에만 custom interval/JSON loader가 적용되고 `EXPLAIN (FORMAT JSON)`, plan admission과
  exact `plan_summary`는 기존 numeric decode 의미로 정상 동작하는 QueryService acceptance.
- `extra_float_digits=1|3|0|-1|-3`, ISO/SQL/Postgres/German `DateStyle`과 supported/unsupported
  `IntervalStyle` role default에서 exact finite float/date/timestamptz/interval 결과. PostgreSQL 18의
  `extra_float_digits>=1`은 같은 shortest-precise 출력을 내며 계약값 1이 더 정밀하다는 주장은 하지 않는다.
- Current/rollback-preserved verified SQL의 ambiguous date/timestamp literal inventory와 발견 시 stop.
- UTC/서울/뉴욕 role default, commit/rollback/timeout/cancel 뒤 reader-format과 timezone pool reset.
- Old metadata/SQL policy token의 executor-before rejection.
- Repository fixture와 managed current/rollback inventory 전체 재실행, old/new Control row 공존.
- HTTP/MCP byte accounting, truncation, verified hash와 coordinated rollback.
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`, `uv run pytest -m integration`.

## Approval Boundary

이 ADR은 제안일 뿐 승인된 계약이 아니다. `ENC-01-A`, `ENC-01-B` 또는 위험 수용인 `ENC-01-C`를
사용자가 정확히 선택하기 전에는 loader, reader-format setting, encoder, revision, verified hash 또는
production cutover를 변경하지 않는다. 권장 구현 승인 문구는 다음과 같다.

```text
ENC-01-A를 ADR 0020의 DateStyle=ISO,YMD·IntervalStyle=iso_8601·extra_float_digits=1,
user-result cursor 전용 lossless interval/JSON numeric, Range/Multirange fail-closed,
result policy v2·SQL policy v3, provider/consumer 영향, verified SQL inventory,
single coordinated cutover와 immutable rollback 범위로 승인한다.
```
