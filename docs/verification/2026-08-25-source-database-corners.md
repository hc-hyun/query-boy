# Source Database Corner Acceptance — 2026-08-25

Status: `DBEDGE-01`~`DBEDGE-05` complete; separate RLS security finding open

Last updated: 2026-08-26 (`DBEDGE-05` and RLS policy-drift sentinel)

## Scope

`DBEDGE-01`과 후속 `DBEDGE-02`~`DBEDGE-05`는 고정 bootstrap fixture를 더 늘리지 않고 test마다 서로
다른 UUID database를 만든다. 대부분은 Source Catalog → Metadata → Guarded Query의 실제 PostgreSQL
경계를 검증한다. SQL_ASCII text/bytea identity, domain/enum RowDescription OID, domain-type
`pg_depend` 같은 raw-only driver/catalog probe는 현재 public 계약으로 확대하지 않고, 필요한 경우
별도 public companion case가 실제 전파를 검증한다.
각 database는 전용 NOLOGIN view owner와 최소 권한 LOGIN reader를 사용하고, pool 종료 뒤 database와
두 role을 삭제한다. Production source/configuration, Control DB와 승인된 public contract는 변경하지
않는다. 같은 fixture에서 발견한 RLS base-policy 누출은 DBEDGE 완료 결과에 넣지 않고
[별도 open security finding](2026-08-26-rls-policy-drift.md)과 strict xfail로 분리한다.

Runnable acceptance는
[`test_source_database_corners.py`](../../tests/test_source_database_corners.py)다.

## Disposable Isolation

- Database: `query_man_corner_db_<uuid>`
- Reader: `query_man_corner_reader_<same uuid>`
- View owner: `query_man_corner_owner_<same uuid>`
- Reader는 `CONNECT`, curated schema `USAGE`, curated relation `SELECT`만 받는다. 일반 curated
  owner-rights view의 base relation 권한은 제거하며, RLS `security_invoker` probe만 PostgreSQL이
  요구하는 private schema/table read 권한을 restricted reader에게 부여한다.
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
| Open scalar/collection/result-type contract characterization | Month/infinity interval, 큰 JSON/JSONB numeric과 4,300/4,301자리 경계, duplicate-key JSON, SQL_ASCII text/bytea, time 24시·temporal year overflow, direct·hidden-view·domain-type collation, custom function/operator, record/unknown OID, unsupported/shifted collection, reader semantic/text-search/bytea GUC와 planner order | Silent hash collision, unsupported SQL type accidental success, same-revision SQL 의미/value/hash drift와 driver availability failure를 exact golden으로 재현했다. Direct bytea는 setting과 무관한 Base64 negative control이지만 허용된 `bytea::text`는 setting별 text/hash가 달랐다. Custom operator의 second-hop function binding과 order-sensitive float/JSONB aggregate도 같은 snapshot/revision에서 결과를 바꿨다. 의미 수정은 `ENC-01` 승인 전 중단했다. |
| Open RLS security sentinel | Valid restricted reader, trusted tenant, `row_security=on`, `security_invoker=true` view 뒤 hidden base policy `USING (true)` 또는 RLS disable | 같은 snapshot/revision에서 cross-tenant row가 성공했다. Accepted ADR 0014 위반이므로 누출 golden을 통과시키지 않고 strict xfail과 별도 `RLS-*` 우선 작업으로 분리했다. |

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

### Open security finding: hidden base RLS policy drift

Public view와 reader/session 검사는 모두 통과해도 hidden base relation의 policy를 `USING (true)`로
바꾸거나 RLS를 disable하면 authenticated tenant query가 다른 tenant 행을 반환했다. Snapshot과
metadata revision도 바뀌지 않았다. 이는 계약 선택 전 보존할 current behavior가 아니라 accepted
[ADR 0014](../decisions/0014-trusted-rls-tenant-context.md)의 격리 불변조건 위반이다. Exact SQL, hash,
독립 재현과 승인 경계는 [RLS policy drift finding](2026-08-26-rls-policy-drift.md)에 기록했다.
제품 수정은 `RLS-01` exact dependency/policy/lock/revision/error 계약 승인 전 중단한다.

### Approval-required follow-up: lossless encoding and source semantics

PostgreSQL 18/psycopg default loader를 실제 read-only query로 확인한 결과, 현재 encoder가 복구할 수
없는 silent loss와 SQL type/semantic identity 손실이 여러 경계에 있다.

| Value/default | Driver value/behavior | Finding |
|---|---|---|
| `interval '1 month 2 days 03:04:05.6'` | `timedelta(days=32, seconds=11045, microseconds=600000)` | Calendar month와 고정 30일이 구분되지 않는다. |
| `interval '0'`, `'infinity'`, `'-infinity'` | 셋 모두 `timedelta(0)` → public `0:00:00` | 두 infinity가 zero와 같은 value/hash로 조용히 합쳐진다. |
| `'{"amount":12345678901234567890.1234567890}'::jsonb` | `{"amount": 1.2345678901234567e+19}` | Fractional numeric precision/scale이 binary float에서 사라진다. |
| 4,300/4,301자리 JSON integer | 전자는 exact Python integer로 성공, 후자는 digit-limit `ValueError` | 같은 allowlisted JSON type 안에서 값 길이에 따라 성공/비공개 실패가 갈리고 rollback 뒤 pool은 복구된다. |
| Duplicate-key `json` 대 last-key-only `json` | 둘 다 `{"outer":{"amount":2}}` | PostgreSQL text cast는 서로 다르지만 Python object/public hash는 last key만 남는다. `jsonb`는 PostgreSQL이 last key로 normalize하는 control이다. |
| SQL_ASCII `text 'hello'` 대 같은 bytea | 둘 다 `bytes` → `base64:aGVsbG8=` | Driver/runtime type만 보는 encoder가 SQL text와 binary를 같은 public value/hash로 합친다. |
| `time '24:00:00'` 대 midnight | 전자는 psycopg `DataError`, 후자는 `00:00:00` | PostgreSQL의 valid/distinct end-of-day가 현재 allowlisted time 경로에서 실패한다. |
| 같은 `float8` value `1.2345678901234567`, `extra_float_digits=1|3` 대 `0|-1|-3` | text decode 전에 다른 자릿수의 Python float | Role/session default에 따라 public number와 hash가 달라질 수 있다. |
| 같은 ambiguous literal `'01/02/2024'::date`, ISO/DMY/MDY `DateStyle` | DB 오류, `2024-02-01`, `2024-01-02` | 같은 SQL의 성공 여부와 날짜 의미/hash가 달라진다. Non-ISO style의 timestamptz output은 추가로 psycopg decode에 실패한다. |
| 같은 interval, `IntervalStyle=postgres` 대 non-postgres | Loss-prone `timedelta` 또는 psycopg `NotImplementedError` | Role/session default에 따라 silent loss 또는 query availability failure가 달라진다. |
| `'{}'::int4multirange` 대 `'{}'::integer[]` | 둘 다 public `[]` | Empty multirange만 generic Sequence로 성공해 지원되는 SQL array와 같은 value/hash가 되고, nonempty multirange는 range element에서 비공개 실패한다. |
| 같은 backslash string literal, `standard_conforming_strings=on|off` | Backslash+`n` 또는 실제 newline | 같은 SQL text의 string value/hash가 달라진다. |
| `NULL = NULL`, `transform_null_equals=off|on` | `null` 또는 `true` | 같은 SQL predicate 의미/value/hash가 달라진다. |
| `'{NULL}'::text[]`, `array_nulls=on|off` | `[null]` 또는 `["NULL"]` | 같은 SQL array literal 의미/value/hash가 달라진다. |
| 같은 `CST`, `timezone_abbreviations=Default|Australia` | UTC `18:00` 또는 `02:30` | 같은 metadata/SQL-policy revision과 SQL이 서로 다른 instant/hash가 된다. |
| `text` column collation `C`→`pg_c_utf8` | `lower('Ä')`가 `Ä`→`ä` | Fresh catalog snapshot/revision도 같아 live semantic DDL을 탐지하지 못한다. |
| Boolean view의 hidden base column collation `C`→`pg_c_utf8` | 같은 view SQL/output에서 `false`→`true` | View를 같은 definition으로 재생성해도 snapshot/revision이 같아 visible output column만으로는 dependency drift를 탐지하지 못한다. |
| Custom domain collation `C`→`pg_c_utf8` | View definition은 같지만 direct `pg_type` edge의 `typcollation`이 바뀌 | Column/direct-`pg_collation` edge만 추적하면 constant domain cast의 active collation을 놓친다. |
| Same-OID custom function body `false`→`true` | View definition·snapshot·revision은 같지만 boolean result/hash가 바뀌 | Call-site/view text만으로 user function implementation drift를 자동 attest할 수 없다. |
| Same-name/signature custom operator를 다른 function에 rebind | View는 direct `pg_operator`, operator가 second-hop `pg_proc` dependency | View definition/definition hash/snapshot/revision은 같지만 boolean result/hash가 바뀐다. |
| `'[0:1]={10,20}'::integer[]` 대 `'{10,20}'::integer[]` | 둘 다 public `[10,20]` | PostgreSQL array lower bound가 사라져 다른 배열이 같은 value/hash가 된다. |
| `'{}'::int4range[]` 대 `'{}'::integer[]` | 둘 다 public `[]` | Empty range array는 accidental success하고 nonempty range array는 비공개 실패한다. |
| `ROW()` 대 `ROW(NULL::integer)` | 둘 다 public `[]` | Anonymous record의 field count와 NULL이 사라져 같은 hash가 된다. |
| `ROW(1::integer)` 대 `ROW('1'::text)` | 둘 다 public `["1"]` | Anonymous record field type이 사라져 같은 hash가 된다. |
| `money`, `point`, `xml` column 대 같은 text | psycopg가 모두 Python `str`로 반환 | Result OID를 보지 않는 encoder가 unsupported SQL type을 text처럼 성공시킨다. |
| `oid/name`과 그 array, named composite 대 allowed integer/text/array | 같은 Python int/str/list 또는 composite text | Known loader도 unsupported result OID를 숨겨 같은 public row/hash로 성공한다. |
| Direct `bytea`, `bytea_output=hex|escape` | 둘 다 `base64:AP8=` | Psycopg bytes loader와 encoder가 같은 value/hash로 정규화하는 좁은 negative control이다. |
| 허용된 `bytea::text`, `bytea_output=hex|escape` | `\\x00ff` 대 `\\000\\377` | Server-side cast가 role default를 public text/hash까지 노출하므로 기존 “pin 불필요” 결론은 direct loader에만 유효하다. |
| Hidden view의 implicit text-search config, `english|simple` | `rats`가 `rat` query에 `true|false` | Direct 함수는 SQL policy가 거부하지만 같은 함수를 감춘 curated view는 role default에 따라 same-revision result/hash가 바뀐다. |
| 같은 rows의 seq/index aggregate input order | `sum(float8)` `0.0|1.0`, duplicate-key `jsonb_object_agg` `{x:2}|{x:3}` | Loader 전 PostgreSQL aggregate 의미가 planner order에 따라 갈린다. Explicit ordering/unique-key 규칙 없이는 encoder만으로 고칠 수 없다. |

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
`sha256:3b05810025aca001615bd4e78fdbb40763f9d3ea1ba257043625796ba3783ced`였다. Duplicate-key와
last-key-only `json`도 둘 다
`sha256:638b941219f3f2bbbd3a92acaf57a2cc5f14e026d386e161fd8b3d24afa32b43`였지만 text cast hash는
`sha256:805656339a9ec4c31deae76681fb0b5d583754cec7bfc3006ea804411e08bdb4`와
`sha256:b81e68d6a989f1c789e2b943cfb1d060c578f9a67c505b3ae7c928f447c5c802`로 달랐다. SQL_ASCII
text와 같은 bytea는 둘 다 `base64:aGVsbG8=`/
`sha256:64f407d6e0fcd189c2c7d4bed463c38771b2f31823d40ff9cb96886fae19ce76`였고 같은 SQL_ASCII
database에서 client만 UTF8로 설정하면 text는 `hello`/
`sha256:a59c30483e34a8f6e687a53a5c025eee6dde4f8d60834b25d241d2aa4a0dec93`가 됐다.
같은 float8 value는
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
`timezone_abbreviations=Default|Australia`의 같은 `CST`는 각각 UTC `18:00`/
`02:30`과
`sha256:4e9285bbe4bbd477dfa08dfd6b9d0583b528f942c7ac6fccaeaf52e40abc8591`/
`sha256:95bbd395245ad95402487aea0c6d8038bdd9a46d3ce5cef298ddbaf9eaa342f7`로 갈렸다.
Column collation을 `C`에서 `pg_c_utf8`로 바꾼 fresh catalog도 같은 revision/snapshot이었지만
`lower('Ä')`의 hash는
`sha256:8fb5cd618a48f4b36dea2978fd188891679fdb0d92ae29924758eb4f4dc8f3c9`에서
`sha256:c4692859cde38b3e26c3bc09be96cc3ae2db09442fb7e8e826deace60da05a64`로 바뀌었다.
같은 SQL text로 재생성한 boolean view의 hidden base column도 snapshot/revision은 같았지만
`folded_matches`가 `false`/`true`, hash가
`sha256:24a658e9869ee578b8189b9e41242fe1521c1843bf2e4bae7ff64cca6c9c396f`/
`sha256:a6e1781ce2c45d140ae02f09454591e2ce6dcbd16eb2d3ca699f1f86a10b678a`로 갈렸다. 이 결과 때문에
ADR 0020 A의 fingerprint scope는 visible column에서 recursive view dependency까지 확장했다.
Custom domain을 `C`에서 `pg_c_utf8`로 같은 이름으로 재생성하고 같은 view SQL을 재생성한
case는 `pg_get_viewdef()` text가 같았지만 `_RETURN` rule의 direct `pg_type` dependency가 가리키는
`typcollation`이 `pg_catalog.C`에서 `pg_catalog.pg_c_utf8`로 바뀌었다. 같은 collation을 유지한 채
domain base/constraint만 바꾸면 binding이 동일할 수 있고 RowDescription은 base OID로 identity를 지운다.
이 evidence를 반영해 ADR 0020 A는 custom base/domain direct type dependency와 declared domain column을
OID erasure 전 거부한다. Pinned built-in type edge는 PostgreSQL 18 `pg_depend`에 없으므로
존재하지 않는 type-binding array를 만들지 않고 image/build/static-catalog identity로 통제한다.
Noncollatable `positive_integer` domain column을 포함한 whole-row view는 base table에
`refobjsubid=0` `pg_class` dependency만 남기고 direct `pg_type` edge는 남기지 않았다. 따라서
domain admission은 collation용 zero-subid expansion과 분리해 모든 non-dropped column을 검사해야 한다.
View call-site를 그대로 둔 `CREATE OR REPLACE FUNCTION` same-OID body 변경은 fresh
snapshot/revision을 바꾸지 않았지만 public `enabled` value를 `false`에서 `true`로,
hash를
`sha256:2c3bdb6d969f6176565315abeacf08d1aac846b2bb003fbc887a55519d10376c`에서
`sha256:630788e0d75c2d80b58158c7b0bb7ba7bb9af9ab8acfa21ae90433896ce1c42b`로 바꿨다.
이는 ADR 0020 A가 custom function implementation을 automatic fingerprint 밖의 protected
artifact freeze·cutover-stop residual로 정직하게 남겨야 하는 runnable 증거다.
Custom operator를 `operator_false`에서 `operator_true` function으로 drop/recreate rebind한 case도
`pg_get_viewdef`, snapshot/revision과 definition hash
`fa4f4892aa25aa2ac7cee9c54ab523ce`는 같았다. View `_RETURN` rule은 operator에만 direct dependency를
두고 operator가 function에 second-hop dependency를 두었다. Public false/true hash는 각각
`sha256:2c3bdb6d969f6176565315abeacf08d1aac846b2bb003fbc887a55519d10376c`/
`sha256:630788e0d75c2d80b58158c7b0bb7ba7bb9af9ab8acfa21ae90433896ce1c42b`였다.
`time '24:00:00'` direct result는 비공개 unavailable로 rollback했고 text cast는 PostgreSQL이 midnight와
구분해 `24:00:00`/`00:00:00`을 반환했다.
Interval zero와 positive/negative infinity는 모두 public `0:00:00`, hash
`sha256:265de8ffe863aa833be5993c281f86ae00468a34e51345ab53e537622c071b48`로 합쳐졌다.
BC/year-10000 date/timestamp는 details 없는 unavailable 뒤 같은 max-one pool에서 복구했다. 4,300자리
JSON integer는 exact value/hash
`sha256:f5990467cfa9498375afc2cab1363623590acfe5305370bf35dfc437c42704c8`로 성공하고 4,301자리는 비공개
실패 뒤 복구했다. Empty/nonempty varbit `""`/`"101"` hash는
`sha256:675a9688aa730d64927d9a124cec8825eb6f87abf0da494410bb26576f9fc5a1`였다.
Direct `bytea`의 `bytea_output=hex|escape`는 둘 다
`sha256:2aaa378b22694753a5e7cdfd62a8581ebbef77e9a46dedbe71534041aa288947`였다. 반면 허용된
`bytea::text`는 hex/escape에서 각각
`sha256:07714fda947fb9e09a2b6217b0fe0c4e53eb3d7032cce257e157acf1eb64b553`/
`sha256:be10c695747100145649abc3d972028963c4cb6dd3fbf2ca34bee276516e7c61`로 갈렸다. Raw setting probe는 오류 뒤
rollback과 마지막 rollback에서 transaction `IDLE`, 최초 reader default 복원을 함께 확인했다.
별도 public QueryService case는 reader role default를 서로 반대로 설정한 fresh Catalog/Query pool에서
같은 metadata/SQL-policy revision과 같은 SQL을 실행해 string, NULL comparison, array, timezone
abbreviation, bytea text cast와 hidden text search의 public value와 canonical helper-derived verified
hash가 실제로 모두 달라짐을 확인했다. `default_text_search_config=english|simple`의 true/false hash는
`sha256:f3b63060353a6de843bdab60cff00570124850083597cbb3ebc09406ddf3af16`/
`sha256:650abf959c971b3fd503ca4db961b5e37d917207abde3500214ad23d64833b56`였다. 이 case는 전체 public
응답의 revision, SQL policy, column/row/count/byte/plan shape도 함께 검증한다. 즉 raw driver 현상에
한정되지 않고 현재 public query 경계까지 전파된다.
같은 세 row와 SQL에서 planner role default만 seq/index scan으로 바꾸면 `sum(float8)`는 `0.0`/`1.0`,
hash는
`sha256:0e281397bb078de6414ccdef1ed9350c948a57b45765001261da4b51de253c88`/
`sha256:bc865c9c470c0a06cf4e957928f26fc1c3dc7d6ae1cfaebe271f53ace90b793a`로 갈렸다.
Duplicate key `jsonb_object_agg`도 `{x:2}`/`{x:3}`, hash
`sha256:dbf3df0bd59c886d21f44a6b339b2fdada8aff45599fc34394518469afef7d08`/
`sha256:50d6676ae9c55a3167bd4b59b6f3c31f3798157f8937cb8968219a7ca754f375`로 갈렸다. 이는
JSON loader가 관찰하기 전에 DB가 정규화한 order-sensitive SQL 결과라 별도 SQL/verified determinism
경계가 필요하다.
`ROW()`/`ROW(NULL::integer)`는 empty collection과 같은
`sha256:77f588e368495248abbd8eb87354efadbd31afa38d0ca675154506624470f06a`,
`ROW(1::integer)`/`ROW('1'::text)`는
`sha256:dadd5b0c8d9a51f5db4a5117d804c30dcbcc7f4cfa417a4df154de40d63de4f3`로 합쳐졌다.
Named composite와 같은 text, `oid/name` 및 그 nonempty array와 같은 integer/text/array도 각각 exact
public row/hash가 같았다. 이는 unregistered string loader뿐 아니라 registered int/list loader도 result
OID inspection 없이는 accidental success한다는 증거다.
PostgreSQL 18 RowDescription은 scalar integer domain을 `int4`, domain-over-`integer[]`를
`int4[]` OID로 보고했지만 array-of-domain, scalar enum과 enum array는 각각의 user-defined OID를
유지했다. 기본 psycopg loader에서 앞의 둘은 `1`/`[1, 2]`, 뒤의 user-defined collection/type은
`"{1}"`/`"ok"`/`"{ok}"` 문자열로 읽혔다. 이 차이는 base OID로 identity가 사라지는
domain도 Metadata/SQL admission에서 미리 거부하고 visible user-defined OID를 query-time에 거부하는
runnable upgrade sentinel이다.

Repository에 versioned된 verified SQL 11개를 read-only inventory한 결과 ambiguous date/timezone
abbreviation literal, interval, range/multirange 결과와 fractional/duplicate-key JSON은 없었다. 유일한 explicit
timestamptz literal은 ISO date와 offset을 사용하고, commerce JSONB fixture는 string/bool/null 및
string array만 포함한다. 이는 repository fixture의 예상 결과 보존 근거일 뿐 protected managed
current/rollback inventory를 대신하지 않으며 production cutover 전 외부 전량 확인이 필요하다.

Infinity date, Python 범위 밖 BC/far-future date/timestamp, 4,301자리 JSON integer, range, nonempty
multirange와 nonempty range array는 silent conversion이 아니라 현재 지원하지 않는 driver value다. 새
public encoding을 만들지 않고 bounded `QUERY_UNAVAILABLE`, rollback과 pool recovery acceptance만
추가했다. 반면 interval infinity는 zero로 조용히 합쳐졌다. Empty multirange/range-array 및 lower-bound
array의 accidental success, SQL_ASCII/collation/source-semantics drift, time 24시, record/unknown OID와
implicit text-search/aggregate determinism의 수정은 승인 전 중단했다.

### No additional product change required for the remaining closed edges

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
| `uv run ruff check tests/test_documentation.py tests/test_source_database_corners.py` (`DBEDGE-04`) | PASS |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` (`DBEDGE-04`) | PASS — SQL_ASCII, duplicate JSON, time 24시, timezone abbreviation, direct/hidden-view/domain-type collation과 known-loader OID/named-composite 포함 `24 passed`, 1 deselected |
| `uv run pytest tests/test_documentation.py -q` (`DBEDGE-04`) | PASS — `16 passed` |
| `uv run ruff check tests/test_documentation.py tests/test_source_database_corners.py` (`DBEDGE-04` function-body follow-up) | PASS |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` (`DBEDGE-04` function-body follow-up) | PASS — same-OID custom function body drift와 기존 disposable corner/cleanup 포함 `25 passed`, 1 deselected |
| `uv run pytest tests/test_documentation.py -q` (`DBEDGE-04` function-body follow-up) | PASS — `16 passed` |
| `uv run ruff check tests/test_source_database_corners.py` (`DBEDGE-05`) | PASS |
| `uv run pytest -m integration tests/test_source_database_corners.py -q` (`DBEDGE-05`) | PASS with explicit open security sentinels — operator/extreme scalar/semantic role-default/planner aggregate 포함 `28 passed`, 1 deselected, 2 xfailed |
| `uv run ruff check .` / `uv run mypy src` | PASS — 29 source files, mypy issue 0 |
| `uv run pytest` | PASS — `645 passed`, 92 deselected |
| `uv run pytest -m integration` | PASS — `80 passed`, 657 deselected |
| `uv run ruff check .` / `uv run mypy src` (`DBEDGE-05`) | PASS — 29 source files, mypy issue 0 |
| `uv run pytest` (`DBEDGE-05`) | PASS — `645 passed`, 97 deselected |
| `uv run pytest -m integration` (`DBEDGE-05` + open RLS sentinels) | PASS with explicit open security sentinels — `83 passed`, 657 deselected, 2 xfailed |
| Prefix residue query | PASS — database `0`, role `0` |

현재 `DBEDGE-05` root static/unit/integration 결과는 위 표에 기록한다. RLS xfail은 완료나 허용된
위험이 아니며 [open security finding](2026-08-26-rls-policy-drift.md)과 `RLS-*` TODO를 따른다. 실제 managed source의 전체
revision/hash 재발행과 `TIME-03` cutover 결과는 protected release acceptance에서 별도로 실행해
[canonical-time evidence](2026-08-25-canonical-time-stability.md)와 환경별 change record에 추가한다.
