# ADR 0020: Lossless Scalar Encoding, Reader Formatting, And Result Types

Status: Proposed — user approval required before implementation

Date: 2026-08-25

Last expanded: 2026-08-26 (`DBEDGE-04` encoding, JSON, time, collation and OID characterization)

## Context

`DBEDGE-02`~`DBEDGE-04`의 PostgreSQL 18/psycopg raw-driver read-only probe와 public
QueryService case에서 silent data-loss, SQL semantic setting과 collection identity 경계를 재현했다.

| PostgreSQL value/default | 현재 Python value/동작 | 손실 또는 불안정성 |
|---|---|---|
| `interval '1 month 2 days 03:04:05.6'` | `timedelta(days=32, seconds=11045, microseconds=600000)` | Calendar month가 고정 30일로 평탄화되어 원래 interval과 `32 days`를 구분할 수 없다. |
| `'{"amount":12345678901234567890.1234567890}'::jsonb` | `{"amount": 1.2345678901234567e+19}` | JSONB가 보존한 decimal precision과 scale이 binary float 변환에서 사라진다. |
| 중첩 duplicate key가 있는 `json` 대 last-key만 있는 `json` | 둘 다 `{"outer": {"amount": 2}}` | PostgreSQL `json` text에는 두 key가 남아 있지만 default object loader와 public hash는 last key만 남겨 서로 다른 값을 합친다. `jsonb`의 동일 결과는 PostgreSQL 자체 normalization인 negative control이다. |
| `SQL_ASCII` database/client의 `text 'hello'` 대 같은 bytes의 `bytea` | 둘 다 Python `bytes`/`base64:aGVsbG8=` | Result OID를 보기 전 encoder가 text를 bytea로 오인해 같은 public value/hash가 된다. 같은 DB에서 client를 UTF8로 바꾸면 text만 `str`이 되어 결과가 달라진다. |
| PostgreSQL `time '24:00:00'` 대 `time '00:00:00'` | 전자는 psycopg `DataError`, 후자는 `time(0, 0)` | PostgreSQL이 구분하는 valid end-of-day 값을 현재 allowlisted `time` loader가 표현하지 못해 availability가 값에 따라 달라진다. `timetz`도 같은 24시 경계를 갖는다. |
| 같은 `float8` value `1.2345678901234567`, role별 `extra_float_digits=1|3` 대 `0|-1|-3` | text decode 전에 유효 자릿수가 달라진 Python float | 같은 value의 public number와 hash가 role/session default에 따라 달라질 수 있다. |
| 같은 ambiguous literal `'01/02/2024'::date`, role별 `DateStyle` | `ISO,YMD`는 DB 오류, DMY는 `2024-02-01`, MDY는 `2024-01-02` | 같은 SQL의 성공 여부와 날짜 의미/hash가 role/session default에 따라 달라진다. Non-ISO style의 `timestamptz` output은 추가로 psycopg decode에 실패한다. |
| 같은 interval, role별 `IntervalStyle` | `postgres`는 loss-prone `timedelta`, 다른 style은 default loader `NotImplementedError` | Silent interval loss 또는 비공개 availability failure가 role/database default에 묶인다. |
| `'{}'::int4multirange` 대 `'{}'::integer[]` | Empty psycopg Multirange가 generic `Sequence`로 `[]` encoding | Empty multirange가 지원되는 SQL array와 같은 public value/hash로 성공하지만 nonempty multirange는 range element에서 실패한다. |
| 같은 backslash string literal, role별 `standard_conforming_strings` | `"a\\nb"` 또는 실제 newline을 포함한 `"a\nb"` | 같은 SQL text의 문자열 의미와 hash가 달라진다. |
| `NULL = NULL`, role별 `transform_null_equals` | `NULL` 또는 `true` | 같은 SQL predicate의 three-valued logic 의미와 hash가 달라진다. |
| `'{NULL}'::text[]`, role별 `array_nulls` | `[null]` 또는 `["NULL"]` | 같은 array literal의 element 값과 hash가 달라진다. |
| 같은 `CST` timestamptz literal, `timezone_abbreviations=Default|Australia` | UTC `18:00` 또는 `02:30` | `TimeZone=UTC`를 고정해도 abbreviation input instant와 hash가 같은 metadata/SQL-policy revision 아래 달라진다. |
| 같은 `text` column, collation `C` 대 `pg_c_utf8` | `lower('Ä')`가 `Ä` 또는 `ä` | 현재 catalog/revision이 `attcollation`을 담지 않아 live DDL 뒤 snapshot/revision은 같은데 SQL 결과/hash가 달라진다. |
| Boolean만 공개하는 view의 hidden base `text` column collation `C` 대 `pg_c_utf8` | `lower(label)='ä'`가 `false` 또는 `true` | View SQL text와 public output column은 같아 visible-column fingerprint만으로도 탐지할 수 없다. Recursive rewrite dependency binding이 필요하다. |
| `'[0:1]={10,20}'::integer[]` 대 `'{10,20}'::integer[]` | 둘 다 Python/public `[10, 20]` | 배열 lower bound `0`과 `1`이 사라져 다른 PostgreSQL 배열이 같은 value/hash로 합쳐진다. |
| `'{}'::int4range[]` 대 `'{}'::integer[]` | 둘 다 Python/public `[]` | Empty range array는 element object가 없어 accidental success하고, 같은 type의 nonempty array는 실패한다. |
| `ROW()` 대 `ROW(NULL::integer)` | 둘 다 Python tuple/public `[]` | Record field count와 NULL이 사라져 같은 value/hash가 된다. |
| `ROW(1::integer)` 대 `ROW('1'::text)` | 둘 다 public `["1"]` | Anonymous record loader가 field type을 잃어 다른 typed field가 같은 value/hash로 합쳐진다. |
| `money`, `point`, `xml` 등 unregistered result OID | psycopg가 Python `str`로 반환 | Encoder가 SQL type을 보지 못하고 PostgreSQL `text`로 오인해 unsupported type이 accidental success한다. |
| `oid/name`과 그 array, named composite | Python `int`/`str`/`list` 또는 composite text | 같은 값을 가진 allowed integer/text/array와 public row/hash가 같아 known loader도 OID gate를 우회한다. |

현재 Guarded Query encoder는 top-level PostgreSQL `numeric`을 decimal string으로 무손실 전달하지만,
psycopg가 이미 평탄화한 `timedelta`와 JSONB 내부 float에서는 원래 값을 복원할 수 없다. 실제로
`interval '1 month'`와 `interval '30 days'`는 같은 `timedelta`/hash가 되지만 기준 날짜에 더한 결과가
다를 수 있고, 서로 다른 큰 fractional JSON 숫자도 같은 binary float/hash로 합쳐진다. 이는
[ADR 0002](0002-guarded-query-contract.md)의 stable scalar 계약과 roadmap `REF-12`의 무손실 의도를
일반 interval/JSONB numeric까지 충족했다고 주장하지 못하게 한다. `extra_float_digits` drift는 원래
float8의 binary value까지 바꾸지는 않지만 같은 값의 public representation과 verified evidence를 흔든다.
`DateStyle`은 output decode뿐 아니라 ambiguous date literal의 DB 입력 의미도 바꾸므로 단순 표시
설정이 아니다. `standard_conforming_strings`, `transform_null_equals`와 `array_nulls`도 parse 단계에서
같은 SQL text의 의미를 바꾼다. `timezone_abbreviations`와 column/database collation은 이 설정들을
고정해도 timestamp parsing과 text operator/function 의미를 바꿀 수 있다. `client_encoding=SQL_ASCII`는
SQL type identity까지 Python runtime value에서 지운다. 반면 `bytea_output=hex|escape`는 psycopg bytes loader와 현재 Base64
encoder가 같은 value/hash로 정규화하므로 새 pin이 필요하다는 근거는 발견되지 않았다.

반면 PostgreSQL infinity date와 range object는 현재 지원 대상이 아니다. 실제 query는 내부 값을
공개하지 않는 `QUERY_UNAVAILABLE`로 실패하고 rollback/pool 재사용 뒤 정상 query가 복구된다. 이
제안은 infinity/range에 새 public encoding을 추가하지 않는다.

## Current Contract

- Query connection은 psycopg default interval/time/JSON loader를 사용한다. PostgreSQL `json`의 duplicate
  object key는 Python mapping 변환에서 last key만 남고 `time|timetz`의 valid `24:00`은 decode에 실패한다.
- Common reader session은 `TimeZone=UTC`를 고정하지만 `DateStyle`, `IntervalStyle`,
  `extra_float_digits`, `standard_conforming_strings`, `transform_null_equals`, `array_nulls`,
  `client_encoding`과 `timezone_abbreviations`는 설정·검사하지 않는다. Source database의
  `server_encoding`도 UTF8인지 admission하지 않는다.
- Python aware datetime만 UTC로 정규화한다.
- `timedelta`는 `str(value)`, top-level `Decimal`은 decimal string, mapping/sequence는 재귀적으로
  encoding한다.
- psycopg Range/Multirange를 Sequence보다 먼저 구분하지 않아 empty multirange만 accidental success한다.
  Array loader는 lower bound를 버리고 Python list만 반환하며, empty range array는 element type을
  encoder가 관찰하지 못해 accidental success한다.
- Result cursor는 column type OID를 canonical encoder에 전달하지 않는다. 따라서 anonymous/named
  composite, money, XML, geometric, `oid/name`과 그 array 및 그 밖의 registered/unregistered OID가
  int/tuple/string/list 같은 지원 Python type으로 내려오면 현재 SQL type allowlist와 무관하게 성공할 수 있다.
- Public SQL capability는 `bit`/`varbit` cast를 이미 광고한다. Psycopg는 이를 `0|1` text로 반환하므로
  이 두 built-in type을 거부하면 advertised capability와 result contract가 충돌한다. Scalar domain은
  PostgreSQL RowDescription에서 allowed base OID로 내려오지만 enum과 array-of-domain은 user-defined
  OID로 남는 것도 PostgreSQL 18의 runnable disposable-DB probe에서 확인했다. Base OID만으로
  domain을 허용하면 declared type/constraint identity가 이미 사라지므로 안전한 증거가 아니다.
- Catalog snapshot/revision은 effective database/column collation identity, provider, determinism과
  stored/actual version을 담지 않는다. Explicit `COLLATE` SQL은 이미 거부하지만 curated column의 live
  collation DDL은 같은 revision 아래 function/operator 의미를 바꿀 수 있다.
- SQL policy version 2와 canonical-time material version 1이 현재 revision baseline이다.
- Public row shape, result hash와 immutable verified identity는 현재 encoding 결과에 묶여 있다.

## Options

### `ENC-01-A` — lossless canonical values (recommended)

1. Source Catalog는 Catalog/Query pool 연결 생성 시 `client_encoding=UTF8`을 고정한다. Transaction은
   PostgreSQL 18과 UTF8을 admission하고 ADR 0019대로 `BEGIN` 뒤 첫 settings statement를 계속
   `TimeZone=UTC`로 둔 뒤 나머지 deterministic setting을 transaction-local로 설정·검사한다.
2. Metadata는 bounded PostgreSQL catalog probe와 canonical fingerprint를 소유한다. Fingerprint는 database와
   모든 encoding-compatible collation definition뿐 아니라 published column binding, view/materialized-view의 hidden
   dependency column·visited definition·direct collation binding을 포함한다.
   Guarded Query는 published fingerprint를
   required Python contract로 받아 같은 query transaction에서 user planning 전에 재검사한다.
3. Current/rollback-preserved verified SQL과 managed view dependency를 inventory한다. Ambiguous date/time,
   timezone abbreviation, backslash string, `expression = NULL`, textual NULL array, non-1 lower bound,
   collation-dependent expression, custom procedure/operator dependency, IANA/POSIX named timezone과
   unsupported result OID가 발견되면 결과를 별도 승인하기 전 cutover를 중단한다.
4. 사용자 SQL의 text-format named result cursor에만 interval/time/JSON/array loader를 SQL 실행 전에
   등록한다. User SQL을 `DECLARE`한 뒤 duplicate column과 RowDescription OID 전체를 검사하고, 승인된
   OID라는 사실이 확인된 뒤에만 fetch/decode한다. `EXPLAIN`, Catalog, Control DB adapter는 바꾸지 않는다.
5. Finite interval은 PostgreSQL 18 `iso_8601` text, exact 24시 time/timetz는 distinct canonical string,
   JSON fraction/exponent는 Decimal string으로 전달한다. Duplicate JSON, temporal infinity,
   non-1 array lower bound와 non-allowlisted result OID는 details 없이 fail-closed한다.
6. Snapshot v1 JSONB document value/shape와 immutable row, metadata revision v1 canonical bytes를
   historical compatibility로 남기고 새 snapshot v2에는 version과 fingerprint를 필수로 저장한다.
   Result policy v2와 SQL policy v3을 새 revision에 넣으며 기존 row를 수정·삭제하지 않는다.
   Old/new serving fleet는 섞지 않는다.

장점은 확인된 silent loss를 제거하고 covered scalar가 role/database default와 무관한 exact hash를
갖는다는 것이다.
비용은 interval, end-of-day time 및 fractional JSONB가 포함된 public row/hash가 바뀔 수 있고 duplicate
JSON, unsupported OID, non-UTF8 source와 unexplained semantic/collation drift가 새로 거부된다는 것이다.
모든 source revision과 current/rollback-preserved verified contract를 다시 발행해야 한다.

#### Exact reader order and admission

Catalog/Query pool 모두 psycopg/libpq connection startup keyword `client_encoding="UTF8"`을 사용한다.
Transaction 안의 정확한 순서는 다음과 같다.

```text
connect(client_encoding=UTF8)
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY
1. TimeZone=UTC
2. DateStyle=ISO, YMD; IntervalStyle=iso_8601; extra_float_digits=1;
   standard_conforming_strings=on; transform_null_equals=off; array_nulls=on;
   timezone_abbreviations=Default
3. 기존 timeout/search_path/row-security/tenant/budget setting
4. common reader policy 검증
5. Metadata-owned source-semantics probe
6. Catalog 또는 resolved-object/EXPLAIN/user SQL
```

`client_encoding`만 connection lifetime이고 나머지는 transaction-local이다. Verifier는
`180000 <= server_version_num < 190000`, `server_encoding=UTF8`, `client_encoding=UTF8`과 위 setting
exact 값을 모두 요구한다. PostgreSQL 18 밖이거나 설정·encoding이 다르면 catalog/user value를 읽기 전에
거부한다. `bytea_output`은 current loader가 stable Base64로 정규화하므로 pin하지 않는다.

#### Exact `source_semantics_fingerprint` v1

Metadata가 공개하는 `load_source_semantics_fingerprint(connection, source) -> str` helper만 catalog와
query adapter가 함께 소비한다. 이미 reader policy를 통과한 read-only transaction을 입력으로 받고
setting/commit/rollback은 하지 않는다. Guarded Query가 별도 fingerprint SQL을 복제하지 않는다.

Canonical material은 모든 key를 생략 없이 포함하는 다음 exact shape다. `<string|null>`은 JSON string
또는 explicit `null`, row array는 아래 정렬 규칙을 따른다.

```text
{
  "version": 1,
  "postgresql": {
    "server_version_num": <integer>,
    "server_encoding": "UTF8",
    "unicode_version": <string>,
    "icu_unicode_version": <string|null>
  },
  "reader_settings": {
    "TimeZone": "UTC",
    "DateStyle": "ISO, YMD",
    "IntervalStyle": "iso_8601",
    "extra_float_digits": "1",
    "standard_conforming_strings": "on",
    "transform_null_equals": "off",
    "array_nulls": "on",
    "client_encoding": "UTF8",
    "timezone_abbreviations": "Default"
  },
  "database_collation": {
    "provider": "builtin|libc|icu",
    "collate": <string>,
    "ctype": <string>,
    "locale": <string|null>,
    "icu_rules": <string|null>,
    "stored_version": <string|null>,
    "actual_version": <string|null>
  },
  "timezone_abbreviations": {
    "setting": "Default",
    "rows": [
      {"abbrev": <string>, "utc_offset_seconds": <integer>, "is_dst": <boolean>}
    ]
  },
  "collations": [
    {
      "schema": <string>, "name": <string>, "encoding": "ANY|UTF8",
      "provider": "database_default|builtin|libc|icu", "deterministic": <boolean>,
      "collate": <string|null>, "ctype": <string|null>, "locale": <string|null>,
      "icu_rules": <string|null>, "stored_version": <string|null>,
      "actual_version": <string|null>
    }
  ],
  "published_column_collations": [
    {
      "relation_schema": <string>, "relation_name": <string>,
      "column_ordinal": <integer>, "collation_schema": <string>,
      "collation_name": <string>, "collation_encoding": "ANY|UTF8"
    }
  ],
  "view_dependency_column_collations": [
    {
      "view_schema": <string>, "view_name": <string>,
      "relation_schema": <string>, "relation_name": <string>,
      "column_ordinal": <integer>, "collation_schema": <string>,
      "collation_name": <string>, "collation_encoding": "ANY|UTF8"
    }
  ],
  "view_dependency_definitions": [
    {
      "view_schema": <string>, "view_name": <string>,
      "relation_kind": "view|materialized_view",
      "definition_sha256": "sha256:<64 lowercase hex>"
    }
  ],
  "view_rule_collations": [
    {
      "view_schema": <string>, "view_name": <string>,
      "collation_schema": <string>, "collation_name": <string>,
      "collation_encoding": "ANY|UTF8"
    }
  ]
}
```

- Database row는 `pg_database`의 provider/collate/ctype/locale/ICU rules/stored version과
  `pg_database_collation_actual_version()`을 사용한다.
- `collations`는 current database encoding과 호환되는 exact
  `pg_collation.collencoding IN (-1, pg_catalog.pg_char_to_encoding('UTF8'))` 전체를
  provider/determinism/locale/rules/stored version/`pg_collation_actual_version()`과 함께 사용한다.
  Canonical encoding은 `-1 -> "ANY"`, exact UTF8 id `-> "UTF8"`로 projection하고 나머지
  numeric value는 거부한다.
  단 `pg_catalog.default` provider `database_default` row는 PostgreSQL이
  `collversion=NULL`이어도 actual function에 database actual version을 반환하는 alias다. 이 row는
  exact `pg_catalog.default`, encoding `ANY`, provider `database_default` 한 개만 허용하고
  canonical `collate/ctype/locale/icu_rules/stored_version/actual_version`을 모두 explicit `null`로
  projection한다. Effective default의 authority는 별도 `database_collation` object다.
- `database_collation` stored/actual version과 `database_default`를 제외한 각 collation row에
  공통 3-state를 적용한다. 한쪽만 null이거나 non-null 둘이 다르면 active 여부와
  무관하게 거부한다. Non-null 둘이 같으면 `version_attested`, 둘 다 null이면
  `versionless`이며 단순 equality일 뿐 drift attestation이 아니다. `database_default` alias에는
  row-level state를 다시 적용하지 않고 `database_collation`을 쓴다.
- Active semantic collation set은 database default, published-column binding, owner-view hidden
  dependency-column binding과 direct view-rule binding이 resolve한 collation의 union이다. Active row는
  `version_attested`이거나 exact static-safe versionless만 허용한다. Static-safe는 provider `libc`,
  deterministic true, locale/rules null, `(collate,ctype)`가 exact `("C","C")` 또는
  `("POSIX","POSIX")`인 individual collation이다. Database default의 versionless 예외도 provider
  `libc`, locale/rules null, exact C/C 또는 POSIX/POSIX만이다. 나머지 active versionless는
  `_SourceSemanticsPolicyError`다. Unreferenced versionless definition은 inventory/hash에 남지만
  active binding으로 들어오는 즉시 거부한다.
- Published binding은 current catalog와 같은 eligible relation·column 중 `attcollation != 0`인 row다.
- Dependency root는 current catalog와 같은 allowed schema/kind, schema `USAGE`, relation `SELECT`
  predicate를 통과한 view/materialized view다. Recursive `view_walk`는 각 view의 exact `_RETURN`
  rule (`rulename='_RETURN'`, `ev_type='1'`, `is_instead`)에서 `deptype='n'`,
  `refclassid=pg_class`인 referenced view/materialized view를 `UNION`으로 탐색해 global visited
  set으로 cycle을 끊는다. 4,097개를 읽어 4,096 초과를 거부하고, visited view별
  `_RETURN` rule이 exact 하나인지 다시 검증한다.
- Visited rule의 `pg_depend`는 `classid=pg_rewrite`, `objid=rule.oid`, `objsubid=0`,
  `deptype='n'`만 semantic edge로 쓴다. `refclassid=pg_class`의 positive `refobjsubid`는 exact
  non-dropped attribute, zero는 해당 relation의 `attnum>0`, non-dropped, `attcollation!=0` column
  전체로 expand한다. Negative subid, missing relation/attribute와 unexpected shape는 거부한다.
  `refclassid!=pg_class`인 semantic edge는 `refobjsubid=0`만 허용하고 nonzero를 거부한다.
  Referenced relation이 view/materialized view면 위 walk이 그 `_RETURN`을 계속 탐색한다.
- Domain type admission은 collation projection과 분리한다. Transient
  `domain_type_admission_columns`은 모든 eligible published column과 각 `pg_class` edge의
  positive `refobjsubid`면 그 exact attribute, zero면 `attnum>0` 전체 non-dropped attribute를
  `attcollation` 관계없이 포함한다. Published identity는 relation schema/name/ordinal이고 기존
  published bound를 쓴다. Dependency identity는 owner view schema/name, relation schema/name, ordinal이며
  full-row deduplicate 후 dependency portion 10,000 rows를 초과하면 거부한다.
  각 `atttypid`를 `pg_type`에 exact join하고 declared `typtype='d'` domain이면 driver
  RowDescription이 base OID로 identity를 지우기 전 `_SourceSemanticsPolicyError`로 거부한다.
  Missing type join도 거부하며 이 domain gate는 다른 visible custom base/enum OID에 대한
  query-time allowlist gate를 대체하지 않는다. 이 transient set은 fingerprint material,
  snapshot, log와 public response에 넣지 않는다.
- `view_dependency_column_collations.view_*`는 root/path가 아니라 해당 direct edge를 소유한
  `_RETURN.ev_class`의 view identity다. 공유 nested view가 root 수만큼 폭증하지 않으며
  root→nested-column과 nested-view→base-column edge를 각각 hash한다. Root membership과 definition은
  persisted snapshot/revision의 relation·definition hash가 소유한다.
- Public eligibility 밖의 private nested view는 persisted snapshot에 definition hash가 없으므로 visited
  view/materialized view 전체의 `pg_get_viewdef(oid,false)` raw UTF8을 같은 transaction에서
  SHA-256한 `view_dependency_definitions`을 별도로 넣는다. Raw definition은 persist/log/public
  response에 넣지 않고 view당 65,536 bytes 및 전체 raw-text bound를 먼저 검사한다.
  Identity는 view schema/name이며 kind/hash payload가 다른 중복은 거부한다.
- PostgreSQL은 pinned system object를 참조하는 dependency row를 `pg_depend`에 생략할 수
  있으므로 shared helper는 fingerprint에 쓴 같은 bounded raw definition을 installed PostgreSQL-18
  pglast `parse_sql()`로 parse한다. Exact one statement와 `SelectStmt`를 요구하고 모든
  nesting depth를 walk해 `CollateClause`가 하나라도 있으면 거부한다. Parse failure,
  multiple/non-SELECT shape와 explicit clause는 `_SourceSemanticsPolicyError`이다. 문자열 검색으로
  대체하지 않는다. Runtime refresh는 no-stale `METADATA_UNAVAILABLE`, Control candidate는
  `SOURCE_VALIDATION_FAILED`, Query live probe는 `QUERY_UNAVAILABLE`로 caller별 mapping한다. User SQL의
  기존 explicit `COLLATE` 거부와 같은 축소된 capability이며 publish 후 clause가 생기는 live
  probe도 policy error로 user planning 전 닫는다.
- `refclassid=pg_collation`, `refobjsubid=0`은 owner view와 resolved collation을
  `view_rule_collations`에 넣고 missing/incompatible encoding은 거부한다.
- `refclassid=pg_type`은 `refobjsubid=0`만 허용하고 missing type, `typisdefined=false`와 nonzero
  subid를 거부한 뒤 해당 semantic edge 자체를 `_SourceSemanticsPolicyError`로 거부한다.
  PostgreSQL 18은 pinned built-in type dependency를 `pg_depend`에 저장하지 않으며, 관측되는
  direct type edge의 custom/domain implementation·constraint/transitive semantics을 typcollation만으로 attest할
  수 없다. Built-in type은 exact server major와 protected image/build/static-catalog identity, database
  default·column·direct collation 경계가 담당한다.
- `pg_proc`, `pg_operator`, `pg_opfamily`는 v1 canonical row를 만들지 않고 raw dependency bound에만
  포함한다. Call-site name/SQL text 변경은 visited definition hash가 담당한다. Reported PostgreSQL
  release는 exact `server_version_num`으로 구분하지만 same-number patched binary를 attest하지
  않으며 protected PostgreSQL image/build digest freeze가 담당한다. Same-name user
  procedure/operator body와 transitive dependency는 아래 residual limitation이다. `pg_class`,
  `pg_collation`, `pg_type`, `pg_proc`, `pg_operator`, `pg_opfamily` 외 `refclassid`는 unexpected
  shape로 admission을 거부한다.
- Material과 raw row는 persist/log/public response에 넣지 않고 fingerprint만 snapshot에 저장한다.

계수 순서는 exact하다. Abbreviation/collation/published candidate, root/relation/visited view,
raw `pg_depend` edge와 view definition 같은 원천 catalog resource는 deduplicate·expand 전에 각 hard
bound `N+1`까지 읽고 초과를 거부한다. 그 다음 join/shape를 검증하고, full-row identical
collapse와 logical-identity payload conflict 거부를 수행한 뒤, 각 canonical array를 해당 bound
`N+1`까지 계수해 초과를 거부하고 정렬한다. Dependency-column 10,000과 direct view-rule
collation 4,096은 canonical bound이고 dependency type-admission column 10,000은 transient validation-set
bound이며, 그 원천 direct edge에는
raw `pg_depend` 20,000 bound만 먼저 적용한다. 정렬은 timezone
`(abbrev UTF8 bytes, offset, is_dst)`, collation `(schema UTF8 bytes, name UTF8 bytes,
encoding)`, published column binding `(relation schema/name UTF8 bytes, ordinal)`, dependency column binding
`(view schema/name, relation schema/name, ordinal)`, view definition `(view schema/name)`, view-rule binding
`(view schema/name, collation schema/name, encoding)` 순이다. 여러 root/path가 같은 hidden column이나
direct collation에 도달하는 것은 정상이므로 full-row set union으로 identical row를 canonical
array bound 계산 전 deduplicate한다. Logical identity는 timezone=`abbrev`,
collation=`schema,name,encoding`, published binding=`relation schema,name,ordinal`, hidden-column
binding=`owner view schema,name,relation schema,name,ordinal`, definition=`view schema,name`, direct
collation binding=`view schema,name,collation schema,name,encoding`이다. 같은 identity에 payload가 다르면
ambiguous duplicate로 거부한다.
Unresolved catalog join과 unexpected provider/encoding/boolean/integer도 거부한다. Unicode
normalization은 하지 않는다. 모든 string sort component는 UTF8 bytes, integer는 numeric
ascending, boolean은 `false < true`로 비교한다.

```python
canonical = json.dumps(
    material,
    ensure_ascii=False,
    separators=(",", ":"),
    sort_keys=True,
    allow_nan=False,
).encode("utf-8")
fingerprint = "sha256:" + hashlib.sha256(canonical).hexdigest()
```

Hard bound는 abbreviation raw/canonical 512 rows/name 64 bytes/offset `-86400..86400` integral seconds,
encoding-compatible collation raw/canonical 4,096 rows, published binding raw/canonical
`min(source.budget.max_metadata_columns, 10,000)` rows, roots와 모든 distinct referenced `pg_class`
OID를 합친 relation set 4,096 rows 및 그 subset인 visited view 4,096 rows, visited rule의 raw
`pg_depend` edge 20,000 rows,
view definition 4,096 rows, dependency-column 10,000 rows, dependency type-admission column 10,000 rows,
direct view-rule collation 4,096 rows,
dynamic locale/collate/ctype/rule/version/view-definition cell 각각 65,536 bytes, raw text 합계
524,288 bytes, final canonical JSON 1,048,576 bytes다. Raw text는 같은 RR snapshot의 bounded
candidate에서 catalog-derived dynamic string leaf의 UTF8 `octet_length`를 occurrence별로 합한 값이며
null은 0, fixed JSON key·provider·encoding policy literal은 제외한다. Fetch 전 SQL aggregate로
검사하고 초과는
truncate하지 않고 거부한다. Query transaction마다 cache/TTL 없이 검사하며 기존 statement/transaction
deadline과 `elapsed_ms`에 포함하지만 result row/byte/plan에는 포함하지 않는다.

모든 collation definition만 hash하면 이미 존재하는 `C`에서 `pg_c_utf8`로 column binding만 바뀐 경우를
놓친다. Published output만 보면 hidden base column에 의존하는 boolean view도 놓치므로 dependency scope가
필수다. PostgreSQL catalog field의 의미는
[pg_database](https://www.postgresql.org/docs/18/catalog-pg-database.html),
[pg_collation](https://www.postgresql.org/docs/18/catalog-pg-collation.html),
[pg_attribute](https://www.postgresql.org/docs/18/catalog-pg-attribute.html)와
[pg_type](https://www.postgresql.org/docs/18/catalog-pg-type.html),
[pg_rewrite](https://www.postgresql.org/docs/18/catalog-pg-rewrite.html),
[pg_depend](https://www.postgresql.org/docs/18/catalog-pg-depend.html)를 따른다.

#### Exact result cursor, scalar and collection contract

Psycopg minimum `>=3.2`에서도 동작하도록 text-format named result cursor를 만든 직후 user SQL 실행 전에
cursor-local loader를 등록한다. Resolved-object/EXPLAIN/plan admission 뒤 `cursor.execute(user_sql)`,
duplicate column 검사, RowDescription OID gate 순서이며 OID 전체가 승인된 뒤에만 fetch한다. OID gate는
planning 전이 아니라 fetch/decode 전이다.

- Finite interval은 `IntervalStyle=iso_8601`에서 PostgreSQL 18 text를 그대로 string으로 낸다.
  `0 -> "PT0S"`, `1 month 2 days 03:04:05.6 -> "P1M2DT3H4M5.6S"`이며 mixed component sign도
  server text를 보존한다. `infinity|-infinity`는 거부한다. 모든 finite interval hash는 기존
  `timedelta.__str__()`에서 바뀔 수 있다.
- Ordinary time/timetz는 기존 psycopg value와 `time.isoformat()`을 유지한다. Raw 24시는 zero fraction만
  exact `"24:00:00"`, timetz offset은 `+09:00` 또는 seconds가 있으면 `+09:00:30` 같은 ISO colon
  형식으로 낸다. Midnight와 합치지 않는다.
- JSON loader는 `json.loads(raw, parse_float=Decimal, parse_int=bounded_int,
  parse_constant=reject, object_pairs_hook=reject_duplicate_names)` 의미다. 정수는 JSON integer,
  소수점 또는 exponent token은 `str(Decimal(token))` string이다. 모든 number token은 sign·점·exponent를
  포함한 ASCII 4,300 bytes 이하만 허용한다. `1.0 -> "1.0"`, `1e0 -> "1"`,
  `1E+2 -> "1E+2"`, `1.2300e2 -> "123.00"`, `-0.0 -> "-0.0"`이며 원 lexeme 보존은 아니다.
  Decoded key가 같은 duplicate는 모든 nesting depth에서 거부하고 Unicode normalization은 하지 않는다.
  JSONB가 저장 시 이미 last-key로 normalize한 값은 유지한다.
- Date/timestamp/timestamptz infinity와 Python 지원 범위 밖 temporal value는 현재처럼 거부한다.
  `bit`은 `[01]+`, `varbit`은 `[01]*` string이라 empty varbit는 `""`, 그 element array는 `[""]`,
  empty array는 `[]`로 구분한다.
- Approved array는 raw dimension prefix가 없거나 모든 lower bound가 1일 때 기존 nested list를 유지한다.
  0/negative/non-1 lower bound는 parser가 평탄화하기 전에 거부한다.
- Catalog, policy probe, `EXPLAIN (FORMAT JSON)`과 Control DB JSON adapter의 loader는 바꾸지 않는다.

Result OID scalar allowlist는 exact 24개다.

```text
bit, bool, bpchar, bytea, cidr, date, float4, float8,
inet, int2, int4, int8, interval, json, jsonb, numeric,
text, time, timestamp, timestamptz, timetz, uuid, varbit, varchar
```

Scalar `pg_type` row는 `pg_catalog` namespace, allowlisted `typname`, `typtype='b'`, `typisdefined=true`,
`typbasetype=0`, `typelem=0`, `typrelid=0`, `typndims=0`을 모두 만족해야 한다. Array는
`pg_catalog`, `typtype='b'`, defined/base type, `typcategory='A'`, `typrelid=0`, `typndims=0`,
`typarray=0`, `typsubscript=pg_catalog.array_subscript_handler`이고, `typelem` row가 scalar predicate를
만족하며 `element.typarray=array.oid`, `array.typname='_' || element.typname`이어야 한다.

Distinct RowDescription OID set과 catalog lookup row set은 exact 일치해야 한다. Declared domain relation/view
column은 Metadata admission에서, custom domain cast는 SQL type allowlist와 visited-rule direct `pg_type`
admission에서 driver OID erasure 전 거부한다. RowDescription이 approved base scalar/array OID를
보고했다는 사실만으로 domain을 허용하지 않는다. Domain OID 자체, array-of-domain/enum,
enum, record/composite, Range/Multirange, extension, `oid/name/reg*/xid*`,
money/XML/geometric/macaddr/pg_lsn/tid와 나머지 OID는 Python int/string/list/tuple, empty result/array여도
fetch 전에 거부한다. `pg_basetype()`로 user-defined OID를 자동 승인하지 않는다.

실패는 partial row/hash를 버리고 rollback한다. Unsupported/malformed OID lookup, duplicate JSON, number
상한, non-1 array, interval infinity와 loader/encoder 내부 실패는 details 없는 `QUERY_UNAVAILABLE`이며
gateway usage는 `failed`다. Internal gate 오류를 user-SQL `QUERY_INVALID`로 바꾸지 않는다. Duplicate result
column은 기존 `QUERY_REJECTED/QUERY_DUPLICATE_RESULT_COLUMN`을 유지한다.

#### Exact result policy v2 and SQL policy v3

Historical `CANONICAL_TIME_POLICY_MATERIAL` v1은 old revision 검증용으로 보존하고 active shared constant는
재귀 immutable `RESULT_ENCODING_POLICY_MATERIAL` v2로 분리한다. Exact JSON material은 다음과 같다.

```json
{
  "version": 2,
  "postgresql_major": 18,
  "result_cursor_format": "text",
  "reader_session": {
    "server_encoding": "UTF8",
    "client_encoding": "UTF8",
    "timezone": "UTC",
    "date_style": "ISO, YMD",
    "interval_style": "iso_8601",
    "extra_float_digits": "1",
    "standard_conforming_strings": "on",
    "transform_null_equals": "off",
    "array_nulls": "on",
    "timezone_abbreviations": "Default"
  },
  "source_semantics": {
    "fingerprint_version": 1,
    "named_timezone_rules": "managed_inventory_and_reissue_only"
  },
  "scalar": {
    "null_bool_int_text": "preserve_json_scalar",
    "finite_float": "json_number",
    "nonfinite_float": "string_NaN_Infinity_negative_Infinity",
    "numeric": "decimal_string_including_nonfinite",
    "bytea": "base64_standard_with_base64_prefix",
    "aware_datetime": "utc_isoformat_plus_00_00",
    "naive_datetime": "preserve_isoformat",
    "date": "preserve_isoformat",
    "ordinary_time": "preserve_isoformat",
    "end_of_day_time": "24_00_00_with_iso_offset",
    "finite_interval": "postgresql_18_iso_8601_text",
    "temporal_infinity": "reject",
    "uuid_network": "string",
    "bit": "nonempty_binary_string",
    "varbit": "possibly_empty_binary_string"
  },
  "json": {
    "integer": "json_integer",
    "fraction_or_exponent": "decimal_string",
    "number_token_max_ascii_bytes": 4300,
    "decimal_canonical": "python_decimal_str",
    "nonfinite": "reject",
    "duplicate_decoded_key": "reject_at_any_depth",
    "unicode_normalization": "none",
    "jsonb_duplicate": "database_last_key_normalization"
  },
  "result_oid": {
    "version": 1,
    "allowed_pg_catalog_scalar": [
      "bit", "bool", "bpchar", "bytea", "cidr", "date", "float4", "float8",
      "inet", "int2", "int4", "int8", "interval", "json", "jsonb", "numeric",
      "text", "time", "timestamp", "timestamptz", "timetz", "uuid", "varbit", "varchar"
    ],
    "allowed_array": "true_pg_catalog_array_of_allowed_scalar",
    "domain": "reject_declared_domain_before_oid_erasure",
    "nonallowlisted": "reject_before_fetch"
  },
  "array": {
    "lower_bounds": "all_one",
    "shape": "nested_json_array",
    "empty": "empty_json_array"
  }
}
```

Compact sorted UTF8 material은 1,849 bytes이고 golden은
`sha256:cf38dcf490fcd06886b7f0c8d308accc464d8ec9bb9fffcf9bc7c52b76ca37e7`다. SQL policy는
current function/operator/type/node set을 유지하고 version을 3, key를 exact
`"result_encoding_policy": RESULT_ENCODING_POLICY_MATERIAL`로 바꾼다. Expected golden은
`sha256:138b8c7fb1e017172acc6542236cb2f3890d5c0af98592766d566fe049639353`다.

#### Snapshot v1/v2, Metadata revision and Python Protocol

Public Python snapshot은 다음 additive contract다.

```python
@dataclass(frozen=True)
class CatalogSnapshot:
    relations: tuple[CatalogRelation, ...] = ()
    snapshot_contract_version: Literal[1, 2] = 1
    source_semantics_fingerprint: str | None = None
```

V1은 fingerprint `None`, v2는 exact `sha256:` lowercase 64 hex가 필수다. Fresh `PostgresCatalog.load()`는
항상 v2다. Persisted v1은 현재 `{"relations":[...]}` 그대로이고 v2는 다음 strict object다.

```json
{
  "snapshot_contract_version": 2,
  "source_semantics_fingerprint": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "relations": [
    {
      "schema_name": "analytics",
      "relation_name": "records",
      "kind": "view",
      "comment": null,
      "definition_hash": "0123456789abcdef0123456789abcdef",
      "security_invoker": true,
      "columns": [
        {
          "name": "record_id",
          "ordinal": 1,
          "data_type": "bigint",
          "nullable": false,
          "comment": null
        }
      ]
    }
  ]
}
```

Version/fingerprint 둘 다 없는 문서만 v1이다. Fingerprint만 있거나 v2 fingerprint 누락, unsupported
version/extra key는 invalid다. V1/v2 둘 다 현재 codec처럼 relation 1개 이상과 relation별 column
1개 이상을 요구한다. Decoder는 history/rollback을 위해 둘 다 읽지만 v1을 v2로 자동 변환하거나
기존 JSONB row를 update하지 않는다. New policy의 public Metadata/Query path는 active v1을 serve하지 않고
즉시 fresh v2 load/publish로 직행한다. Unpinned이면 v2를 activate/cache하고, pinned v1이면
v2 history row append는 허용하지만 active pointer/cache는 바꾸지 않고 details 없는
`METADATA_UNAVAILABLE`로 닫는다. 이 pinned-v1 path를 `_PinnedActiveRevision` stale fallback으로
재열지 않는다. Ordinary `MetadataService.rollback`/`MetadataStore.activate`는 v1 target을 거부하고
route-drained coordinated Control source rollback만 captured v1 pointer를 복구한다.

`create_metadata_revision()`은 snapshot version으로 dispatch한다. V1은 frozen
`CANONICAL_TIME_POLICY_MATERIAL_V1` bytes, legacy v1 material builder/canonicalizer를 별도로 사용하고
v2 field가 v1 path에 절대 유입되지 않는다. V2는 current source/budget/semantic/relation material과 canonicalizer를
유지하면서 top-level에 `snapshot_contract_version=2`, historical `canonical_time_policy` v1,
`result_encoding_policy` v2와 `source_semantics_fingerprint`를 포함한다.

Guarded Query의 required Python contract는 keyword-only fingerprint다. 기존 direct caller/adapter/fake가
함께 바뀌어야 하므로 rolling-compatible additive change가 아니라 coordinated breaking Python
contract change다.

```python
execute(
    source,
    sql,
    metadata_revision,
    validated,
    *,
    source_semantics_fingerprint: str,
    query_id: str | None = None,
    tenant_id: str | None = None,
)
```

QueryService가 v2 snapshot에서 전달한다. Live mismatch/probe DB 오류는 details 없는
`QUERY_UNAVAILABLE`; probe deadline은 기존 `QUERY_TIMEOUT`; queue/pool limit은 기존
`QUERY_OVERLOADED`; refresh 뒤 old metadata/SQL-policy token은 executor 전
`METADATA_REVISION_MISMATCH`다. Fingerprint/material은 HTTP/MCP field로 추가하지 않는다.

#### Exact failure and stale mapping

| Condition | Runtime/public result | Stale/side effect |
|---|---|---|
| Catalog transaction의 PostgreSQL major, server/client encoding, reader setting, fingerprint shape/bound/cardinality, unresolved dependency 또는 collation stored/actual version 불일치 | details 없는 `METADATA_UNAVAILABLE` | Deterministic policy violation이므로 stale fallback과 partial publish를 허용하지 않는다. |
| 같은 조건의 Control candidate validation | `SOURCE_VALIDATION_FAILED` | Candidate generation, snapshot, active pointer를 변경하지 않는다. |
| 기존 v2 metadata를 보유한 catalog refresh의 transient connection/DB failure | 기존 bounded-stale 규칙 안에서만 v2 metadata를 복구할 수 있다. | Query는 매 transaction live fingerprint probe를 별도로 통과해야 하며 새 publish는 없다. |
| New-policy path의 active v1 snapshot, invalid v2 codec/revision 또는 missing fingerprint | details 없는 `METADATA_UNAVAILABLE` | Ordinary v2 publish/serving path에서 v1을 자동 변환·serve·activate하지 않는다. Route를 닫고 v2 public path를 unavailable로 둔 explicit rollback만 captured v1 pointer를 복구·pin한 뒤 R1을 시작할 수 있다. |
| Query transaction의 live fingerprint mismatch, malformed/internal probe DB 오류 | details 없는 `QUERY_UNAVAILABLE` | Resolved-object/`EXPLAIN`/user SQL 전 rollback하고 active pointer를 변경하지 않는다. |
| Fingerprint probe가 기존 statement/transaction deadline 초과 | 기존 `QUERY_TIMEOUT` | Partial row/hash 없이 rollback한다. |
| Unsupported/malformed result OID, duplicate JSON, number bound, non-1 array, temporal infinity 또는 loader/encoder 오류 | details 없는 `QUERY_UNAVAILABLE` | Fetch한 partial row/hash를 버리고 rollback하며 usage outcome은 `failed`다. |
| Refresh/reissue 후 old metadata 또는 SQL-policy token | 기존 `METADATA_REVISION_MISMATCH` | Executor/DB 진입 전 거부한다. |

Deterministic source-semantics violation은 Metadata-owned private
`_SourceSemanticsPolicyError(ReaderSessionPolicyError)`로 raise한다. 다른 module은 subclass를
import하지 않고 public `ReaderSessionPolicyError` marker의 `isinstance`만 소비하며 psycopg,
timeout, connection error를 이 type으로 wrap하지 않는다. Metadata catch order는
`MetadataUnavailableError` → pinned-v2 handling →
`(ReaderSessionPolicyError, _CatalogValidationError, StoredMetadataInvalidError)` no-stale → generic
transient bounded-stale다. V2 codec/revision/fingerprint invariant을 깨뜨린 stored value를 cached v2로
재열지 않는다. Query helper policy error는 non-timeout generic path의 details 없는
`QUERY_UNAVAILABLE`, timeout/driver cancel은 기존 `QUERY_TIMEOUT`이다. Raw catalog material,
collation/timezone 이름, SQL, credential과 database error는 response·audit·ordinary log에 넣지 않는다.

#### Named timezone residual limitation

`pg_timezone_abbrevs`는 `TimeZone=UTC`와 `timezone_abbreviations=Default`에서 인식되는 abbreviation row만
고정한다. `Asia/Seoul` 같은 IANA named-zone의 전체 역사/미래 transition, OS/system tzdata 교체와 POSIX
zone rule은 자동 fingerprint하지 않는다. `pg_timezone_names`는 `CURRENT_TIMESTAMP`에 따라 offset이 바뀌어
stable version fingerprint가 아니다. 따라서 PostgreSQL/tzdata/system-zoneinfo 변경은 managed semantic
change로 freeze하고 current/rollback verified SQL의 named-zone inventory를 전량 재실행·재발행한다.
자동 fail-closed가 필요하면 별도 timezone-rules attestation 또는 symbolic-zone SQL 제한 계약을 다시
승인받는다. A가 named-zone drift까지 자동 검출한다고 주장하지 않는다.

#### Collation provider residual limitation

`pg_collation_actual_version()`과 database actual version은 provider가 보고한 version이지
cryptographic locale artifact attestation이 아니다. 같은 reported version 아래 distro backport/patch가
locale data를 바꾸는 경우와 active가 아닌 unreferenced versionless definition의 외부 drift는
fingerprint가 자동 검출한다고 주장하지 않는다. Protected cutover change record에
PostgreSQL image/build digest, libc/ICU/locale-data package identity를 고정하고 이 identity가 바뀌면
source/admin mutation freeze, route drain, full managed inventory/reissue를 다시 수행한다. Versionless
row는 active database/column/view semantic에 연결되지 않은 동안만 inventory에 존재할 수 있다.
Pin status 자체를 SQL catalog로 attest한다고 주장하지 않고 PostgreSQL-18 build/static
catalog identity를 같은 protected image/inventory에 고정한다.
Provider artifact의 cryptographic runtime attestation을 원하면 별도 계약을 다시 승인받는다.

#### Function and operator dependency residual limitation

Visited view definition hash는 function/operator call-site name과 SQL text를 고정한다. PostgreSQL exact
`server_version_num`은 reported release를 구분하지만 same-number patched binary를 attest하지
않으며 protected PostgreSQL image/build digest freeze가 이 gap을 담당한다. 그러나 same-name user procedure,
operator 또는 operator-family의 body/implementation과 그 transitive dependency graph은 v1 fingerprint가 자동
attest하지 않는다. 이 object는 protected managed artifact inventory에 freeze하고 발견·변경은
cutover stop이다. 같은 fingerprint/revision을 유지한 ordinary full reissue로 변경을 수용하지
않고 direct unpinned `pg_proc|pg_operator|pg_opfamily` admission rejection 또는 transitive semantic
fingerprint 확장 계약을 다시 승인받는다. A가 이 drift를 자동 fail-closed한다고 주장하지
않는다.

### `ENC-01-B` — loss-prone types fail closed

UTF8-only source/client, source-semantics fingerprint와 collation drift, `DateStyle=ISO, YMD`,
`extra_float_digits=1`, `standard_conforming_strings=on`, `transform_null_equals=off`,
`array_nulls=on`, `timezone_abbreviations=Default`는 A와 같이 고정·inventory하고
`IntervalStyle=postgres`를 설정·검사하되, user-result cursor scope의 custom text loader가 interval
전체, `time|timetz` 24시, fractional 또는 duplicate-key JSON/JSONB를 손실 전에 감지하면
`QUERY_UNAVAILABLE`로 거부한다. A와 같이
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

1. Source/admin/verified mutation을 freeze하고 protected inventory, Control backup, R1 artifact/key,
   active 및 rollback-preserved generation/revision/L2를 고정한다.
2. Current/rollback verified question·SQL·relations·expected 전체를 read-only export해 change record에 고정한다.
3. Old fleet admission/route를 닫고 active query, query/catalog/staging source connection과 poller를 0까지
   drain한 뒤 old process를 중지한다.
4. Route 밖 v2 fleet를 시작한다. V1 history는 decode할 수 있지만 active v1인 동안 public metadata/query와
   source readiness는 unavailable이어야 한다.
5. 각 current/rollback baseline에 immutable v2 counterpart generation/snapshot/revision을 append하고,
   해당 verified contract 전체를 새 QueryService로 실행·비교해 새 revision의 verified row와 L2를 남긴다.
   마지막에는 intended current v2 counterpart만 active로 둔다.
6. Server/client encoding, timezone-abbreviation fingerprint, database/column collation version과 ambiguous
   date/time, timezone abbreviation, ordinary backslash string, `expression = NULL`, textual NULL array
   literal, non-1 lower-bound array, hidden view dependency, IANA/POSIX named zone 및 unsupported/custom result
   OID를 전량 inventory한다. Interval/time/JSON changed value/hash와 domain rejection,
   bit/varbit 보존을
   exact 승인한다. 설명되지 않은 column/row/hash/rejection 또는 inventory 누락에는 중단한다.
7. Repository fixture 11개, managed inventory 전체, stale token 409, replica convergence/drift, L2와
   protected readiness를 확인한 뒤 route한다.

Rollback은 v2 route/mutation을 닫고 connection을 drain한 상태에서 Control Plane의 승인된 rollback로
captured v1 source generation과 metadata pointer를 복구·pin한다. V2 public path는 v1을 serve하지 않은 채
중지하고 R1 fleet의 replica/L2/readiness 확인 뒤 route한다. V2 snapshot/generation/verified row는 삭제하지
않는다. SQL_ASCII source는 자동 변환하지 않으며 UTF8 database migration/re-onboarding 없이는 cutover에서
제외한다.

Mixed old/new serving fleet는 같은 SQL의 row/hash가 달라 허용하지 않는다.

## Provider And Consumer Impact

- Provider: Guarded Query immutable result-policy v2/SQL-policy v3 descriptor를 먼저 동결한 뒤 Source
  Catalog common reader settings, Metadata source-semantics catalog probe·published snapshot codec/revision material,
  Guarded Query pool loader/result encoding·query-time fingerprint verifier 순으로 symbol baseline을 직렬화한다.
- Direct consumers: Delivery HTTP/MCP row, Assurance result hash/verified CLI, Control Plane immutable
  verified publish와 Runtime coordinated cutover.
- Public Python: `CatalogSnapshot`의 v2 version/fingerprint field는 source-compatible default를 두지만
  new-policy serving은 v2를 강제한다. `QueryExecutor.execute()`의 required keyword-only fingerprint는
  coordinated breaking change이므로 direct fake/adapter/consumer를 하나의 baseline에서 함께 갱신한다.
- Persistence: Control relational schema migration은 없고 persisted snapshot v2에는
  `snapshot_contract_version=2`와 `source_semantics_fingerprint`가 필수다. V1 JSONB document
  value/shape와 immutable row는 update/delete 없이 보존하고, v1 revision canonical input/golden만
  byte-for-byte로 고정한다. 새 snapshot/generation/verified row만 append한다.
- Security/privacy: SQL, question, credential 또는 token을 새로 저장하지 않는다.
- SQL behavior: non-UTF8 source, timezone abbreviation/collation drift와 `DateStyle=ISO,YMD`의 ambiguous
  date/time literal, backslash string literal, `expression = NULL`, NULL array literal은 기존 default와
  달리 실패하거나 다른 의미를 가질 수 있어 managed verified inventory stop condition이다.
  User SQL과 visited curated/nested view의 explicit `COLLATE`는 거부하고 active non-C/POSIX
  versionless collation도 거부한다.
- Result behavior: duplicate-key JSON, non-1 array와 unsupported/custom OID는 새로 거부될 수 있다.
  모든 finite interval과 fractional/exponent JSON의 public string/hash는 바뀔 수 있고 end-of-day time은
  새 canonical string으로 성공한다. Declared/custom domain은 OID identity erasure 전 새로 거부하고
  `bit|varbit`는 기존 string shape를 보존한다. Current/rollback verified SQL뿐 아니라 managed curated relation의 encoding, hidden collation
  dependency와 advertised result type inventory도 cutover stop condition이다.
- Compatibility: New fleet는 v1 history를 decode하지만 serve하지 않고 old fleet는 v2 strict document를
  decode하지 못하므로 mixed serving을 금지한다. Non-UTF8/non-PostgreSQL-18 source는 admission 실패다.
- Residual limitation: IANA/POSIX named timezone rule drift와 provider가 같은 version으로 보고하는
  distro/libc/ICU/locale-data drift는 자동 fingerprint하지 않으며 image/build/package identity
  change 때 managed inventory와 full verified reissue로 통제한다. Same-name user
  procedure/operator implementation·transitive dependency drift도 자동 감지 밖이며 protected artifact
  freeze·cutover stop 후 별도 fingerprint/admission 계약 승인이 필요하다.
- Data loss: A는 silent loss를 제거한다. B는 값을 거부한다. C는 runtime protection이 없다.

## Verification Required For `ENC-01-A`

- Positive/negative/mixed month, day, microsecond interval의 exact ISO-8601 golden과 arrays/nesting.
- Ordinary time/timetz와 exact `24:00:00` end-of-day scalar/array의 기존-compatible canonical golden.
- JSON/JSONB의 큰 integer, fractional precision/scale, exponent, nested array/object exact golden.
  Top-level/nested duplicate-key `json`은 text cast로 DB 보존을 증명한 뒤 비공개 fail-closed하고 unique-key
  JSON은 유지한다. JSONB last-key normalization은 DB canonical negative control로 둔다.
- UTF8 server/client positive case, role/database default SQL_ASCII drift와 SQL_ASCII server source의
  text/bytea type-confusion negative case 및 user/catalog SQL 전 rejection.
- `timezone_abbreviations=Default|Australia`와 abbreviation-file row drift가 서로 다른 instant를 만드는
  corpus, exact sorted-row fingerprint와 transaction reset. `C`/`pg_c_utf8`, database default, provider,
  deterministic, locale/rules, Unicode, stored/actual version, published binding과 same-definition boolean
  view의 hidden dependency corpus에서 fingerprint/revision이 바뀌고 live mismatch는 user planning 전에
  fail-closed한다. `pg_catalog.default` alias의 null stored/database actual version 차이는 정상
  source를 거부하지 않고 `database_collation` mismatch만 따로 거부하는 negative/positive
  corpus를 포함한다. Active exact C/POSIX null-version positive, active `C.utf8`/other
  versionless negative, unreferenced versionless inventory positive, explicit
  `COLLATE default|C|POSIX|custom` visited-view rejection, nested/constant-fold AST corpus와 private nested
  view definition-only drift도 포함한다. Column이 없는 custom base/domain cast와
  same-name domain drop/recreate는 collation이 같거나 다른 경우 모두 direct `pg_type` admission에서
  거부한다. `refobjsubid=0` whole-relation dependency 뒤의 noncollatable numeric domain이
  direct `pg_type` edge 없이도 transient all-column admission으로 거부되는 corpus를 포함한다.
  Direct type edge의 nonzero-subid/missing-join과 pinned built-in type edge가 생략되는 PostgreSQL 18
  behavior도 검증한다. Same-name custom procedure/operator body
  변경이 automatic fingerprint scope 밖이며 managed cutover stop임을 negative acceptance로 남긴다.
  Bounds/duplicate/unresolved dependency도 stale fallback 없이 닫는다.
- Empty/nonempty Range/Multirange 및 그 array는 모두 비공개 fail-closed하고 ordinary empty PostgreSQL
  array는 계속 `[]`인 QueryService golden, rollback과 pool recovery.
- 1-based one/multi-dimensional array는 기존 nested list를 유지하고 0/negative/non-1 lower bound는
  value/hash 평탄화 전에 비공개 fail-closed하는 golden.
- Allowed scalar/array OID corpus와 exact `bit|varbit` string positive corpus. Scalar domain,
  domain-over-approved-array, enum, array-of-domain/enum,
  anonymous/named record, money, XML, geometric, extension, explicit `oid/name/reg*/xid*/pg_lsn/tid`와
  그 array 및 그 밖의 non-allowlisted built-in/unknown OID 거부 corpus. Empty/nonempty
  composite/unsupported array가 Python int/`[]`/string으로 우회하지 못하며 duplicate column 검사,
  rollback과 pool recovery 의미를 보존한다.
  Public `cast_types`는 기존 bit/varbit를 유지한다.
- Empty varbit `""`, varbit array `[""]`와 empty array `[]`, interval infinity와 temporal year overflow,
  JSON number 4,300-byte boundary/overflow 및 decoded Unicode-equivalent duplicate key corpus.
- User-result cursor에만 custom interval/time/JSON loader가 적용되고 `EXPLAIN (FORMAT JSON)`, plan
  admission과 exact `plan_summary`는 기존 numeric decode 의미로 정상 동작하는 QueryService acceptance.
- Fetch batch 16행을 넘겨 17번째 이후에 duplicate JSON, number overflow, non-1 array 또는
  interval infinity를 둔 corpus로 이미 누적된 valid row/result bytes/hash도 전부 폐기하고 details 없는
  `QUERY_UNAVAILABLE`, usage `failed`, rollback 및 max-one pool의 다음 정상 query 복구를 같이
  검증한다.
- `extra_float_digits=1|3|0|-1|-3`, ISO/SQL/Postgres/German `DateStyle`, supported/unsupported
  `IntervalStyle`, `standard_conforming_strings=on|off`, `transform_null_equals=on|off`,
  `array_nulls=on|off`, `client_encoding=UTF8|SQL_ASCII`와 `timezone_abbreviations=Default|Australia` role
  default에서 exact 결과. PostgreSQL 18의 `extra_float_digits>=1`은 같은
  shortest-precise 출력을 내며 계약값 1이 더 정밀하다는 주장은 하지 않는다. `bytea_output=hex|escape`는
  같은 Base64/hash라는 negative control을 유지한다.
- Current/rollback-preserved verified SQL의 ambiguous date/timestamp/timezone abbreviation, ordinary
  backslash string, `expression = NULL`, NULL array literal, non-1 lower-bound array, collation dependency와
  result OID inventory 및 발견 시 stop.
- UTC/서울/뉴욕 role default, commit/rollback/timeout/cancel 뒤 reader-format과 timezone pool reset.
- Old metadata/SQL policy token의 executor-before rejection.
- V1/v2 strict codec, v1 JSONB value/shape·revision byte golden, v2 required fingerprint/invariant, active v1 serving rejection,
  result-policy 1,849-byte/hash 및 SQL-policy v3 hash, shared material identity와 recursive immutability.
- Lock된 psycopg 3.3.x 뿐 아니라 `pyproject.toml`의 oldest supported 3.2.x로 cursor-local
  loader, named cursor/OID-before-fetch acceptance를 별도 실행한다. 3.2에서 보존할 수 없으면
  지원 하한을 조용히 올리지 않고 dependency contract 변경을 다시 승인받는다.
- `QueryExecutor` keyword-only contract, fingerprint verifier 위치, catalog policy violation의
  `METADATA_UNAVAILABLE`, candidate `SOURCE_VALIDATION_FAILED`, live/OID/loader `QUERY_UNAVAILABLE`, probe timeout의
  `QUERY_TIMEOUT`과 raw detail 비노출.
- Repository fixture와 managed current/rollback inventory 전체 재실행, old/new Control row 공존.
- HTTP/MCP byte accounting, truncation, verified hash와 coordinated rollback.
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`, `uv run pytest -m integration`.

## Approval Boundary

이 ADR은 제안일 뿐 승인된 계약이 아니다. `ENC-01-A`, `ENC-01-B` 또는 미완료 defer인 `ENC-01-C`를
사용자가 정확히 선택하기 전에는 loader, reader-format setting, encoder, revision, verified hash 또는
source-semantics fingerprint/persisted snapshot과 production cutover를 변경하지 않는다. A/B만
implementation/production completion 선택지다. 권장 A
구현 승인 문구는 다음과 같다.
위 A의 exact reader/fingerprint/result/policy/codec/Protocol/error/cutover와 residual limitation을 하나의
implementation-ready 제안으로 묶었다. B는 policy version, migration/cutover, 보존·거부 범위를 다시 exact
restatement해야 하고 C는 open defer다. 일반적인 “진행/구현/승인”이나 ID만으로 계약 변경을 시작하지 않는다.

```text
ENC-01-A를 ADR 0020의 Exact reader order/admission, source_semantics_fingerprint v1 shape·정렬·bound·
hidden/private nested view dependency·visited definition·direct pg_type edge·declared/custom
domain pre-erasure rejection, result
cursor/scalar/collection/OID 규칙, 1,849-byte result policy v2
`sha256:cf38dcf490fcd06886b7f0c8d308accc464d8ec9bb9fffcf9bc7c52b76ca37e7`, SQL policy v3
`sha256:138b8c7fb1e017172acc6542236cb2f3890d5c0af98592766d566fe049639353`, snapshot/revision v1/v2와
QueryExecutor keyword-only fingerprint, exact public error mapping, PostgreSQL-18/UTF8-only admission,
current/rollback full verified reissue, mixed-fleet 금지와 immutable coordinated cutover/rollback 전 범위로
승인한다. Curated/nested view explicit COLLATE와 active non-C/POSIX versionless collation을
거부한다. IANA/POSIX named timezone rule drift와 provider가 같은 version으로 보고하는
distro/libc/ICU/locale-data drift는 자동 fingerprint하지 않고 PostgreSQL/tzdata/image/build/package
identity change마다 managed inventory와 full verified reissue로 통제한다. Same-name user
procedure/operator body·transitive dependency drift는 automatic fingerprint 밖이므로 protected artifact
freeze·cutover stop 후 별도 fingerprint/admission 계약 승인을 요구하는 residual limitation도 수용한다.
```
