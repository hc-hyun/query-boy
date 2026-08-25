# ADR 0024: Recursive RLS Policy Attestation And Lock-First Query Admission

Status: Proposed — exact user approval required before implementation

Date: 2026-08-26

Decision ID: `RLS-01-A`

## Context

[ADR 0014](0014-trusted-rls-tenant-context.md)는 authenticated tenant를 transaction-local
`query_man.tenant_id`로 전달하고 restricted `NOBYPASSRLS` reader, `row_security=on`과 published
`security_invoker=true` view를 확인한다. 현재 snapshot과 metadata revision은 그 view 아래의 private
table, nested view와 `pg_policy` 의미를 담지 않는다.

[RLS drift finding](../verification/2026-08-26-rls-policy-drift.md)은 별도 PostgreSQL 18.6 disposable
database에서 다음 두 변경 뒤에도 snapshot/revision과 public view 검사가 그대로 통과하고 다른 tenant
행이 성공 응답으로 반환되는 것을 재현했다.

```sql
ALTER POLICY tenant_filter ON private.tenant_records USING (true);
ALTER TABLE private.tenant_records DISABLE ROW LEVEL SECURITY;
```

추가 read-only probe는 transaction 순서에도 실제 race가 있음을 보였다.

```text
BEGIN REPEATABLE READ
SELECT set_config(...)                 # old MVCC snapshot이 여기서 고정됨
LOCK published_view                    # concurrent ALTER POLICY commit 뒤 획득
read pg_policy                         # old expression
execute published_view                 # new permissive policy가 적용되어 누출
```

PostgreSQL은 view의 `ACCESS SHARE` lock을 현재 rewrite dependency까지 재귀적으로 잡고 policy, RLS flag,
table/view DDL의 `ACCESS EXCLUSIVE` lock과 충돌시킨다. 그러나 이미 만들어진 repeatable-read snapshot을
새로 만들지는 않는다. 따라서 fingerprint만 추가하거나 첫 catalog `SELECT` 뒤에 lock을 두는 것은
충분하지 않다. Lock은 `BEGIN` 뒤 첫 relation action이어야 한다. PostgreSQL의 관계 lock은
`CREATE OR REPLACE FUNCTION`, role membership과 `ALTER ROLE ... BYPASSRLS`를 막지 않으므로 허용 구조와
잔여 trust boundary도 함께 제한해야 한다.

관련 PostgreSQL 의미는 [LOCK](https://www.postgresql.org/docs/18/sql-lock.html),
[explicit locking](https://www.postgresql.org/docs/18/explicit-locking.html),
[row security](https://www.postgresql.org/docs/18/ddl-rowsecurity.html),
[`pg_policy`](https://www.postgresql.org/docs/18/catalog-pg-policy.html),
[`CREATE POLICY`](https://www.postgresql.org/docs/18/sql-createpolicy.html)와
[`pg_auth_members`](https://www.postgresql.org/docs/18/catalog-pg-auth-members.html),
[`pg_depend`](https://www.postgresql.org/docs/18/catalog-pg-depend.html),
[`pg_shdepend`](https://www.postgresql.org/docs/18/catalog-pg-shdepend.html),
[`pg_rewrite`](https://www.postgresql.org/docs/18/catalog-pg-rewrite.html) 및
[system catalog initial OID rules](https://www.postgresql.org/docs/18/system-catalog-initial-data.html),
[character-set behavior](https://www.postgresql.org/docs/18/multibyte.html)와
[psycopg text adaptation](https://www.psycopg.org/psycopg3/docs/basic/adapt.html#strings-adaptation)을
따른다.

이 결과는 승인된 tenant-isolation contract의 예외가 아니라 보안 결함이다. 이 ADR을 추가하거나
일반적으로 “승인한다”고 말하는 것만으로 아래 계약이 승인되거나 현재 구현이 바뀌지는 않는다.

## Current Contract

- `CatalogSnapshot`은 published relation과 column, view definition hash와 root
  `security_invoker`만 담는다.
- Persisted snapshot은 version field가 없는 exact `{"relations":[...]}` v1 document다.
- RLS source도 v1 snapshot/revision을 publish, cache, restore하고 bounded stale fallback을 사용할 수 있다.
- Metadata catalog transaction과 query transaction은 `BEGIN` 뒤 첫 settings statement로
  `TimeZone=UTC`를 설정하고 reader probe를 실행한다. Hidden dependency lock/attestation은 없다.
- `QueryExecutor.execute()`에는 RLS attestation input이 없다.
- `SourceReloader`의 apply와 concurrent query 사이에서 old `SourceProfile`과 new metadata snapshot이
  섞일 수 있다.
- Public HTTP/MCP error set에는 이미 `METADATA_UNAVAILABLE`, `QUERY_UNAVAILABLE`,
  `METADATA_REVISION_MISMATCH`와 HTTP 400 `QUERY_REJECTED`가 있다. Tenant 누락은 top-level code가
  아니라 existing bounded detail `reason_code=TENANT_CONTEXT_REQUIRED`다.

## Options

### `RLS-01-A` — strict structural attestation and lock-first comparison (recommended)

RLS attestation material v1은 안전성을 자동 추론할 수 있는 좁은 구조만 허용하고, 그 구조의 relation별 canonical
fingerprint를 snapshot/revision에 넣는다. Metadata publish와 매 query가 같은 Metadata-owned provider를
사용한다. Query는 첫 relation action으로 실제 참조 root view를 lock하고 같은 transaction에서 live
fingerprint를 비교한 뒤에만 planning과 user SQL을 실행한다.

안전 구조 검사를 통과한 graph만 observed fingerprint를 published expected value로 승격할 수 있다.
임의의 observed hash, operator가 복사한 hash 또는 기존 snapshot은 안전성 증거가 아니다. 이 선택은
별도 source-manifest hash field나 새 public admin/wire field를 만들지 않는다.

비용은 기존 RLS source가 PostgreSQL 18/UTF8과 아래 좁은 policy/view/table 규칙을 만족해야 하고,
snapshot v2 재발행과 route-off fleet cutover가 필요하다는 것이다. Non-UTF8 RLS database는 UTF8
migration/re-onboarding 없이는 route할 수 없다. Group-target 또는 복합 SELECT policy,
partitioned/foreign/materialized dependency와 custom function/operator는 새 계약 없이는 사용할 수 없다.

### `RLS-01-B` — flexible transitive attestation

Group role, non-FORCE table, partition, custom function/operator와 임의 policy expression을 허용하려면
effective role-membership graph, function body, operator implementation과 source-side migration lock을
함께 attest해야 한다. Relation lock 중에도 `GRANT`와 `CREATE OR REPLACE FUNCTION`이 성공하므로 shared
advisory-lock 또는 privileged broker protocol도 필요하다.

이는 방향 비교용이며 implementation-ready 계약이 아니다. B를 선택하려면 persisted shape, lock
protocol, privileged writer, failure/rollback과 transitive bound를 다시 exact proposal로 승인받는다.

### `RLS-01-C` — defer with RLS production blocked

Strict xfail과 open finding을 유지하고 RLS source의 production publish/route를 계속 보류한다. 이는
누출 위험 수용이나 완료 선택지가 아니다.

## Exact `RLS-01-A` Contract

### 1. Recursive graph admission

Published root view마다 Metadata가 rewrite dependency를 재귀적으로 수집한다. 다음 조건을 모두
만족해야 한다.

1. Root와 reachable nested relation 중 view는 PostgreSQL ordinary view이고 모두
   `security_invoker=true`다.
2. Terminal relation은 inheritance/partition 관계가 없는 ordinary table만 허용한다.
   Partitioned table/partition, materialized view, foreign table, sequence와 다른 relkind는 거부한다.
3. Root마다 terminal table이 하나 이상 있어야 한다.
4. 모든 terminal table은 `relrowsecurity=true`, `relforcerowsecurity=true`이고 실제 reader session의
   `pg_catalog.row_security_active(table_oid)=true`여야 한다. `current_user`, `session_user`와 configured
   source reader name은 byte-for-byte 같아야 한다. Reader는 terminal table owner와 이름이 다르고
   `pg_catalog.pg_has_role(current_user, owner_oid, 'USAGE')=false`여야 한다.
5. 각 deparsed root/nested view definition은 recursive graph의 relation만 internal allowlist로 주고
   public `validate_sql` contract와 current immutable Guarded Query `SQL_POLICY_REVISION`의 AST
   node/function/operator/type policy로 다시 검증한다. Metadata는 Query의 private
   `_FUNCTION_POLICY_QUERY`, `_OPERATOR_POLICY_QUERY` 또는 executor helper를 import/복제하지 않는다.
   Stored `_RETURN` rule binding과 그 `pg_depend` rows가 identity authority이며 deparsed text를 현재
   name resolution의 증거로 사용하지 않는다. Metadata-owned RLS admission은 used name의 모든 visible
   function/operator candidate와 stored custom dependency를 확인해 PostgreSQL-18 initdb object만
   허용하고 normal-operation OID range인 `>=16384`, extension/user object를 namespace와 무관하게
   거부한다. Allowed function/operator는 exact `pg_catalog`, non-volatile, non-security-definer와 reader
   execute 조건을 모두 만족해야 한다. `set_config`, file/program/dynamic SQL과 policy allowlist 밖의
   built-in도 거부한다.
6. View rewrite graph의 custom function, operator, type, collation 또는 policy가 다른 relation을 읽는
   subquery dependency는 거부한다. Owner-rights nested view와 security-definer function을 hash만 해서
   허용하지 않는다.
7. Terminal table OID가 `pg_inherits.inhrelid` 또는 `inhparent`인 row는 모두 없어야 한다. 이름이 같아도
   unresolved/duplicate dependency, recursion bound 초과와 catalog row cardinality 불일치는
   deterministic policy violation이다.

각 ordinary view는 `(ev_class=view_oid, rulename='_RETURN')`인 `pg_rewrite` row가 정확히 하나여야 하고
`ev_type='1'`, `ev_enabled='O'`, `is_instead=true`, empty `ev_qual`이어야 한다. 해당 rule의 dependency는
filter 전 모든 `pg_depend` row를 `bound+1`로 읽고 다음 exact projection만 허용한다.

- Dependent side는 항상 `(classid=pg_rewrite, objid=return_rule_oid, objsubid=0)`여야 한다.
- `(refclassid=pg_class, refobjid=current_view_oid, refobjsubid=0, deptype='i')` internal ownership row는
  정확히 하나여야 하며 graph/canonical material에서는 제외한다.
- 나머지는 모두 `deptype='n'`이고 `refclassid=pg_class`여야 한다. `refobjsubid=0`은 whole relation,
  positive value는 target relation의 live non-dropped exact column ordinal이어야 한다. Negative/missing
  ordinal과 disallowed target relkind는 거부한다.
- `refclassid`가 `pg_proc|pg_operator|pg_type|pg_collation`이면 namespace와 무관하게 non-pinned/custom
  bound dependency로 거부한다. 다른 catalog class/dependency type도 거부한다. PostgreSQL이 omitted한
  pinned builtin은 deparsed AST allowlist와 used name의 모든 visible candidate가 `oid < 16384`, exact
  `pg_catalog` 및 위 safety predicate라는 검사를 함께 통과해야 한다.
- Raw seven-field dependency tuple의 exact duplicate만 violation이다. 같은 target의 whole-relation과
  서로 다른 column row, 또는 서로 다른 parent view가 같은 target에 도달하는 것은 정상이다. Recursion은
  target relation OID마다 한 번만 수행하되 모든 distinct column-level edge를 canonical material에 남긴다.

`pg_get_viewdef` reparse는 AST grammar 증거이고 stored `_RETURN` action/dependency가 bound identity
authority다. `rewrite_action_sha256`가 binding identity digest이며 `definition_sha256`는 validation text의
digest다. Deparser representation drift는 안전하다고 무시하지 않고 availability를 fail-closed한다.

View의 built-in PostgreSQL behavior와 actual reader role은 PostgreSQL 18 image/build 및 protected
source-admin boundary를 신뢰한다. PostgreSQL major는 18만 허용한다. Same-version patched binary와
superuser catalog mutation은 아래 residual limitation에 남긴다.

이 RLS-specific bound-object admission과 dependency projection은 Metadata가 소유하고
`RlsPolicyAttestation.version=1` 의미로 동결한다. `view_sql_policy_revision`은 Guarded Query가 공개한
AST policy version만 pin한다. 둘 중 하나의 의미가 바뀌면 새 attestation/snapshot revision과 contract
승인이 필요하다. 따라서 Metadata가 Guarded Query private SQL과 우연히 같아야 한다거나 Query private
helper가 module contract가 된다는 의미가 아니다.

### 2. Exact SELECT policy grammar

각 terminal table에서 Query Man SELECT에 영향을 줄 수 있는 policy는 다음 하나뿐이어야 한다.

```text
command       = SELECT (`polcmd = 'r'`)
permissive    = true
target roles  = [PUBLIC] or [the exact session reader role]
USING         = (<tenant_id column> COLLATE pg_catalog."C") =
                pg_catalog.current_setting('query_man.tenant_id', true)
WITH CHECK    = null
```

Equality의 양쪽 순서만 바꿀 수 있다. Metadata는 `search_path=pg_catalog`에서 deparsed expression을
기존 pinned PostgreSQL-18 parser로 읽고 catalog/raw node binding을 함께 확인한다. Tenant operand는
해당 table에서 exact logical name이 `tenant_id`인 단일 ordinary, non-generated, non-identity
`NOT NULL pg_catalog.text` column이어야 하고 operator는 built-in
`pg_catalog.=(text,text)`, function은 built-in
`pg_catalog.current_setting(text,boolean)`이어야 한다. PostgreSQL이 string literal에 붙인 exact
`pg_catalog.text` cast와 tenant operand의 exact `COLLATE pg_catalog."C"` 외의 cast/collation,
boolean wrapper, `OR`, 추가 predicate, subquery, custom object와 volatile expression은 허용하지 않는다.

임의의 다른 text column을 tenant key로 자동 선택하지 않는다. DB owner는 각 protected row의
`tenant_id`가 실제 authoritative tenant identity라는 source-data contract를 책임진다. 이 의미를
source별로 바꾸려면 manifest의 table별 tenant-column declaration과 migration을 별도 승인해야 한다.

Comparison collation은 system-owned deterministic bytewise `pg_catalog."C"` exact identity로
고정한다. Column/database default, user/extension collation과 case/accent-insensitive collation을 tenant
equality에 사용하지 않는다. 이는 deparsed 문자열이 `COLLATE pg_catalog."C"`와 같은지를 비교하는
규칙이 아니다. Policy raw binding/custom dependency와 live `pg_collation`을 함께 확인해 exact PG18
builtin `C`, provider `c`, encoding `-1`, deterministic `true`, stored/actual version `null`을 요구한다.

다른 `SELECT` policy, `FOR ALL` policy와 restrictive policy는 reader에게 현재 적용되는지와 무관하게
거부한다. Role membership 변경으로 나중에 permissive `OR`가 열리는 것을 막기 위해 유일한 target은
PUBLIC 또는 exact current reader 한 role만 허용하고 group/multiple role은 거부한다.
`polroles=ARRAY[0::oid]`만 PUBLIC이고 named target은 live `current_user=session_user=configured reader`
role OID와 exact equality를 확인한 뒤 persisted material에만 canonical role name을 쓴다.
`INSERT|UPDATE|DELETE` 전용 policy는 Query Man read-only SELECT에 영향을
주지 않으므로 이 attestation의 policy set에서 제외하며, command가 `ALL` 또는 `SELECT`로 바뀌면
relation DDL lock과 다음 live comparison에서 거부된다. PostgreSQL 18 catalog에서 알 수 없는
`polcmd` 값은 non-SELECT policy로 추정하지 않고 거부한다.

Stored dependency도 exact raw shape로 검증한다. Catalog numeric OID를 hardcode하거나 persisted field로
쓰지 않고 live `regclass` identity와 current database OID를 쓴다. Bounded raw `pg_policy` scan과
dependency 이외의 table당 unique SELECT grammar/cardinality 검사를 먼저 통과한 policy set을 `P`,
`N=len(P)`로 둔다. `P`는 provider-call union graph의 distinct policy OID set이며 여러 root가 같은
table/policy에 도달해도 한 번만 센다. Root bound는 각 root에서 reachable한 distinct subset에,
provider-call bound는 union `P`에 각각 적용한다. `pg_depend`는 한 provider call에서 한 번만 조회한다. Object scope는
`classid=pg_policy AND objid IN P`까지만 먼저 제한하고 `objsubid`, referenced side와 `deptype`을
filter하지 않은 seven fields에 `LIMIT 2*N+1`을 적용한다. 다른 policy row는 이 object set 밖이므로
무시한다. Python에서 policy별로 group한 뒤 순서와 무관하게 다음 두 tuple만 정확히 하나씩 허용한다.

```text
(pg_policy, policy_oid, 0, pg_class, terminal_table_oid, 0,                 'a')
(pg_policy, policy_oid, 0, pg_class, terminal_table_oid, tenant_id_attnum, 'n')
```

첫 row는 policy의 terminal table auto dependency이고 두 번째는 exact positive `tenant_id` ordinal의
normal dependency다. Duplicate, missing, 다른 class/object/subobject/dependency type과 세 번째 row는
거부한다. Pinned built-in `=`, `current_setting`, `text`와 `C` collation은 이 catalog에 dependency row가
없으므로, row 부재를 object safety로 추론하지 않고 위 raw-node와 live built-in 검사를 계속 요구한다.

`pg_shdepend`도 한 provider call에서 한 번만 조회한다. Cluster-wide이고 policy OID가 database마다
겹칠 수 있으므로 object scope를 `dbid=current_database_oid AND classid=pg_policy AND objid IN P`까지만
먼저 제한한다. 다른 database나 다른 policy의 row는 이 object set 밖이므로 무시한다. `objsubid`,
referenced side와 `deptype`을 filter하지 않은 seven fields에 `LIMIT N+1`을 적용하고 policy별로 group한다.
PUBLIC `polroles=[0]` policy는 exact empty set이어야 한다. Named reader policy는 다음 policy-role shared
dependency 하나만 허용한다.

```text
(current_database_oid, pg_policy, policy_oid, 0, pg_authid, exact_reader_oid, 'r')
```

그 object scope 안의 unexpected `objsubid`, owner, group, 다른 role/dependency row와 duplicate는
거부한다. 이 두 dependency set은
admission evidence이고 shape가 위 canonical policy fields에서 결정되므로 persisted material에 별도
row를 복제하지 않는다. Shape가 달라지면 같은 policy text/hash라도 candidate와 live query를
fail-closed한다.

### 3. Canonical identity and fixed bounds

Relation OID, policy OID와 role OID는 durable identity field로 사용하지 않는다. Role은 canonical
name으로, relation/column은 exact case-sensitive logical name과 ordinal로 기록한다. Raw PostgreSQL
node는 public/persisted field가 아니라 same-name rebinding 탐지용 SHA-256만 사용한다.

RLS attestation text identity v1은 PostgreSQL 18과 exact UTF8 server/client pair에서만 정의한다.
Metadata Catalog와 Guarded Query의 RLS source pool은 libpq/psycopg connection-startup keyword
`client_encoding="UTF8"`을 사용한다. 이 startup/session invariant는 common reader safety provider인
Source Catalog가 다음 public contract로 소유하고 Metadata와 Guarded Query가 소비한다.

```python
RLS_READER_CLIENT_ENCODING: Final = "UTF8"

def require_rls_reader_connection_policy(
    connection: AsyncConnection[Any], source: SourceProfile
) -> None: ...
```

함수는 RLS source 전용이며 SQL을 실행하거나 setting/lifecycle을 바꾸지 않는다. Source가 RLS가 아니거나
libpq ParameterStatus/connection info의 `180000 <= server_version < 190000`,
`server_encoding=UTF8`, `client_encoding=UTF8`과 psycopg active codec `utf-8` 중 하나라도 다르면 existing
`ReaderSessionPolicyError`를 낸다. Connection을 연 직후 pre-discovery가 relation을 읽기 전과
authoritative/query checkout이 `BEGIN`에 들어가기 전에 caller가 실행한다. Root lock과 existing
settings/common reader probe 뒤 `attest_rls_roots`가 live graph를 읽기 직전에도 같은 connection에서
다시 실행한다. Metadata는 이 connection policy를 복제하지 않고 strict graph text/fingerprint만
소유한다. Pre-`BEGIN` mismatch는 rollback 없이 connection을 close/discard하고 pool에 반환하지 않는다.
Transaction 안의 recheck mismatch는 bounded rollback 뒤 connection을 close/discard한다.
Connection-startup parameter는 transaction 안의 settings statement가 아니므로 `BEGIN` 뒤 lock-first와
`TimeZone=UTC` first-settings 순서를 바꾸지 않는다.

이 admission 뒤 graph의 모든 textual leaf는 exact Python `str`만 허용하고 strict UTF-8로 encode한다.
Psycopg `bytes`, `bytearray`, `memoryview`, 숫자/boolean, 다른 object와 contract가 허용하지 않은 null은
거부한다. `replace`, `ignore`, `surrogateescape`, locale/default codec, `str(bytes)`, Base64 fallback,
Unicode normalization, trim과 newline 변환은 사용하지 않는다. Exact UTF-8 bytes는 byte bound,
C-order sort/equality, inner digest와 final canonical JSON input에 함께 사용한다. 이 규칙은
root/nested/table schema·relation, reader/owner/policy/target-role/column, function/operator/type/collation
name, reloptions, deparsed definition/expression, raw node와 dependency projection의 모든 textual
enum/name에 적용한다. Numeric OID, ordinal과 boolean을 string으로 coerce하지 않는다.

SQL_ASCII raw `e9`를 LATIN1 client가 `str("é")`로 decode한 identity와 raw UTF-8 `c3a9`를 UTF8 client가
같은 string으로 decode한 identity는 Python string만 보면 충돌하고, 같은 Python identifier도 client
encoding에 따라 서로 다른 relation을 resolve한다. 따라서 graph-local decode/hash만으로 SQL_ASCII를
허용하지 않는다. Non-UTF8 RLS source는 database를 UTF8로 migration/re-onboard하고 새 v2를 발행하기
전에는 publish/route할 수 없다. Non-RLS source의 현재 pool/encoding/result 동작은 이 결정에서 바꾸지
않는다. RLS scalar loader/canonical response shape도 새로 정의하지 않지만 startup client UTF8 때문에
prior non-UTF8 RLS session과 비교한 public value/hash가 달라지거나 query가 새로 거부될 수 있고 이는
아래 full verified reissue/stop condition으로 검증한다. RLS에서도 interval/JSON/result OID 등 public
result losslessness와 broader source-semantics fingerprint는 ADR 0020의 별도 `ENC-01` 결정에 남긴다.
Encoding pair는 attestation/snapshot persisted field가 아니라 every candidate/live transaction의
mandatory admission이다.

각 root의 internal canonical material은 다음 exact key 집합을 사용한다.

```text
{
  "version": 1,
  "reader_role": <canonical role name>,
  "view_sql_policy_revision": <current SQL_POLICY_REVISION string>,
  "root": {"schema": <string>, "relation": <string>},
  "views": [
    {
      "schema": <string>, "relation": <string>, "owner": <role name>,
      "kind": "view", "security_invoker": true,
      "reloptions": [<sorted strings>],
      "definition_sha256": "sha256:<64 lowercase hex>",
      "rewrite_action_sha256": "sha256:<64 lowercase hex>"
    }
  ],
  "tables": [
    {
      "schema": <string>, "relation": <string>, "owner": <role name>,
      "kind": "table", "reader_is_owner": false,
      "reader_has_owner_usage": false,
      "row_security": true, "force_row_security": true,
      "row_security_active": true, "has_inheritance": false
    }
  ],
  "policies": [
    {
      "table_schema": <string>, "table_relation": <string>,
      "policy_name": <string>, "command": "SELECT", "permissive": true,
      "roles": [<"PUBLIC" or exact reader_role string>],
      "tenant_column": {
        "name": "tenant_id", "ordinal": <integer>, "not_null": true,
        "generated": false, "identity": false,
        "type_schema": "pg_catalog", "type_name": "text",
        "comparison_collation_schema": "pg_catalog",
        "comparison_collation_name": "C",
        "comparison_collation_provider": "c",
        "comparison_collation_encoding": -1,
        "comparison_collation_deterministic": true,
        "comparison_collation_stored_version": null,
        "comparison_collation_actual_version": null
      },
      "using_shape": "tenant_column_equals_query_man_tenant_v1",
      "using_expression_sha256": "sha256:<64 lowercase hex>",
      "using_node_sha256": "sha256:<64 lowercase hex>",
      "with_check": null
    }
  ],
  "dependencies": [
    {
      "from_schema": <string>, "from_relation": <string>,
      "to_schema": <string>, "to_relation": <string>,
      "to_kind": "view|table", "to_column_ordinal": <integer|null>,
      "dependency_type": "normal"
    }
  ]
}
```

`roles` array length는 항상 1이고 그 한 값은 literal `"PUBLIC"` 또는 top-level `reader_role`과 byte-for-byte
같은 string 중 하나다. Pipe를 포함한 합성 string이나 두 값을 함께 저장하지 않는다.

정렬은 `views|tables`를 `(schema, relation)`, policy를
`(table_schema, table_relation, policy_name)`, `reloptions|roles`를 C byte order로 한다. Dependency는
`(from_schema, from_relation, to_schema, to_relation, to_kind, null_rank, ordinal, dependency_type)`로
정렬하며 `null_rank=0, ordinal=0`은 whole-relation null, positive column ordinal은
`null_rank=1, ordinal=<value>`다. String element는 모두 UTF-8 C byte order다. 모든 key와 explicit null을
포함한다. Compact
`ensure_ascii=false`, `sort_keys=true`, `separators=(",", ":")`, UTF-8 JSON의 SHA-256을
`sha256:<64 lowercase hex>`로 만든다.

Inner digest input도 implementation choice가 아니다. `search_path=pg_catalog`인 같은 transaction에서
다음 non-null PostgreSQL `str`을 받아 위 text identity의 exact strict UTF-8 bytes에 어떤 trim,
newline/Unicode normalization 또는 prefix도 하지 않고 SHA-256을 적용한 뒤 output에만 `sha256:`
prefix를 붙인다.

| Field | Exact input text |
|---|---|
| `definition_sha256` | `pg_catalog.pg_get_viewdef(view_oid, false)` |
| `rewrite_action_sha256` | 해당 view의 유일한 `_RETURN` `pg_rewrite.ev_action::text` |
| `using_expression_sha256` | `pg_catalog.pg_get_expr(polqual, polrelid, false)` |
| `using_node_sha256` | `pg_policy.polqual::text` |

Missing/duplicate `_RETURN`, null `polqual`, null/invalid UTF-8 result는 거부한다. `WITH CHECK`는 exact null만
허용하므로 digest field를 만들지 않는다.

고정 implementation bound는 root별 dependency depth 16, distinct reachable relation 256, filter 전 모든
`pg_policy` raw row(DML-only 포함) 512, filter 전 `_RETURN` dependency raw row(internal/custom 포함) 2,048,
individual deparsed definition/expression/raw node 65,536 UTF-8 bytes, aggregate raw/deparsed input
1,048,576 UTF-8 bytes와 final canonical material 1,048,576 UTF-8 bytes다. 한 provider call/source transaction은
root 256, union distinct graph relation 4,096, raw policy row 8,192, raw dependency row 32,768,
aggregate raw/deparsed input과 aggregate canonical material 각각 16,777,216 UTF-8 bytes를 넘지 않는다.
Existing source budget의 published root/column 상한도 같이 적용하며 더 작은 값이 effective bound다.
모든 count/byte limit은 projection/filter/dedupe 전 `bound+1`로 검사한다. 초과를 truncate하거나 새
source별 knob를 만들지 않고 fail-closed한다.

위 distinct `P`와 relation bound에서 파생되는 policy dependency root별 최대 raw row는 `pg_depend` 512,
`pg_shdepend` 256(sentinel 513/257), provider-call 전체는 8,192/4,096(sentinel 8,193/4,097)이다. 별도
configurable bound나 policy별 N개의 query를 추가하지 않는다. Count는 scoped object set에서 semantic
field filtering/dedupe 전 raw row 수다.

RLS canonical material, hidden schema/table/column, policy/role/expression, raw node와 RLS attestation
fingerprint는 public context, HTTP/MCP, audit, metric 또는 ordinary log에 저장하지 않는다. 이는 기존
public `ValidatedSql.fingerprint` 계약을 없애거나 숨긴다는 뜻이 아니다. Persisted RLS value는
attestation version, root identity, `view_sql_policy_revision`과 최종 attestation fingerprint뿐이다.

### 4. Python and persisted snapshot v2 contract

Metadata가 다음 immutable Python value를 제공한다.

```python
@dataclass(frozen=True)
class RlsPolicyAttestation:
    version: Literal[1]
    root_schema: str
    root_relation: str
    view_sql_policy_revision: str
    fingerprint: str

@dataclass(frozen=True)
class RlsQueryAttestation:
    version: Literal[1]
    roots: tuple[RlsPolicyAttestation, ...]

@dataclass(frozen=True)
class CatalogRelation:
    # existing fields
    rls_policy_attestation: RlsPolicyAttestation | None = None

@dataclass(frozen=True)
class CatalogSnapshot:
    relations: tuple[CatalogRelation, ...] = ()
    snapshot_contract_version: Literal[1, 2] = 1
```

`RlsQueryAttestation.roots`는 unique `(root_schema, root_relation)` UTF-8 C-order tuple이고 각 descriptor의
version, SQL policy revision과 hash shape가 valid해야 한다. Empty tuple은 relationless RLS query에만
허용한다. Duplicate/out-of-order/mismatched descriptor는 executor 전 invariant violation이다.

V1 persisted document와 v1 metadata revision canonical bytes는 byte-for-byte historical baseline으로
남긴다. V2는 RLS source 전용이며 strict document는 다음 shape다.

```json
{
  "snapshot_contract_version": 2,
  "relations": [
    {
      "schema_name": "analytics",
      "relation_name": "tenant_records",
      "kind": "view",
      "columns": [
        {
          "name": "record_id",
          "ordinal": 1,
          "data_type": "bigint",
          "nullable": false,
          "comment": null
        }
      ],
      "comment": null,
      "definition_hash": "0123456789abcdef0123456789abcdef",
      "security_invoker": true,
      "rls_policy_attestation": {
        "version": 1,
        "root_schema": "analytics",
        "root_relation": "tenant_records",
        "view_sql_policy_revision": "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "fingerprint": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      }
    }
  ]
}
```

위 `columns`는 실제 strict codec처럼 1개 이상의 exact existing column object를 가져야 한다. Existing
optional relation field는 v1과 같이 nonempty일 때만 나타나며 empty primary-key/foreign-key/index key를
새로 쓰지 않는다.

V2의 모든 relation은 exact root identity와 일치하는 attestation이 필수고 non-RLS source에는 v2를
허용하지 않는다. V1 document에는 version/attestation field가 없어야 한다. Missing/extra/unsupported
version, invalid hash, relation-attestation identity mismatch와 mixed attested/unattested relation은
invalid다. Codec/history validation은 persisted `view_sql_policy_revision`이 well-formed이고 fingerprint
material에 들어간 version-specific byte invariant만 확인한다. Historical v2를 current
`SQL_POLICY_REVISION`과 같다고 다시 쓰거나 그 불일치만으로 decode 실패시키지 않는다. Fresh builder와
serving validation은 attestation의 `view_sql_policy_revision`, live fingerprint material과 current
`SQL_POLICY_REVISION`이 모두 일치해야 하고 policy revision이 바뀌면 live graph에서 새 v2
snapshot/revision을 재발행한다. Decoder는 v1/v2 history를 읽되 다음 serving policy를 분리한다.

- Non-RLS source는 v1만 serve한다.
- RLS source는 v2만 serve한다. Active/pinned/cached v1은 history-only이고 stale fallback 없이
  `METADATA_UNAVAILABLE`다.
- V1 row를 update/delete하거나 v2로 자동 변환하지 않는다. Fresh live graph를 검증해 새 immutable v2
  row와 revision을 append한다.

`create_metadata_revision()`은 snapshot version으로 dispatch한다. V1 builder/canonicalizer와 golden은
동결한다. V2는 source/budget/semantic/relation material에 `snapshot_contract_version=2`와 relation별
attestation version/root/`view_sql_policy_revision`/fingerprint를 모두 포함한다. Stored field만 current
value로 바꾸거나 old fingerprint를 재사용해 fresh/current revision을 만들면 builder/serving invariant가
거부한다. Historical decoder는 원래 revision field와 bytes를 보존한다. Fingerprint가
같아도 v1과 v2 revision은 같지 않다.

[Proposed ADR 0020](0020-lossless-interval-and-json-numeric-encoding.md)의 encoding/source-semantics
snapshot은 이 v2를 덮어쓰지 않고 v3로 배정한다. V3 RLS relation은 attestation field shape와 RLS
semantics를 누적하되 stored v2 bytes/hash를 복사하지 않고 current policy/live graph에서 fresh value를
만들며 source-semantics fingerprint를 추가한다. Snapshot version namespace와 RLS material v1,
source-semantics material v1, result policy v2, SQL policy v3을 서로 혼동하지 않는다.

### 5. Catalog and query lock-first order

Metadata는 Catalog와 Guarded Query가 복제하지 않고 함께 소비할 다음 async capability를 제공한다.

```python
attest_rls_roots(
    connection: AsyncConnection[Any],
    source: SourceProfile,
    roots: tuple[tuple[str, str], ...],
) -> tuple[RlsPolicyAttestation, ...]
```

Caller가 이미 소유한 exact source connection/transaction만 사용한다. Helper는 새 connection/pool이나
background task를 만들고 `BEGIN`, `LOCK`, settings, reader probe, `COMMIT` 또는 `ROLLBACK`을 수행하지
않는다. Roots는 unique `(schema, relation)` C-order tuple이어야 한다. Helper는 그 transaction에서 live
graph를 validate/hash해 같은 순서의 immutable descriptors를 반환하고 published expected value와의
비교, transaction lifecycle과 public error mapping은 caller가 맡는다. 별도 metadata connection으로
검사한 결과를 query connection의 증거로 사용하지 않는다.

RLS Metadata discovery는 root 이름을 얻기 위한 bounded pre-discovery transaction과 authoritative
transaction을 분리한다. 두 조회는 exact same publishable-root predicate, 즉 allowed schema의 ordinary
view, schema `USAGE`, table-level `SELECT`, 그리고 `attnum>0`, non-dropped, column `SELECT`인 visible
column이 하나 이상인지를 사용한다. 이 predicate와 ordering은 한 Metadata-owned SQL/capability를
재사용하고 복제하지 않는다. Root count는 source budget과 fixed attestation bound를 모두 적용한
`bound+1`로 읽어 초과 시 lock SQL을 만들기 전에 거부한다. Zero root도 `LOCK TABLE` empty statement를
만들지 않고 existing `No selectable relations` validation failure로 거부한다.

두 Metadata phase는 각각 별도 pool checkout/transaction이고 다음 derived outer deadline을 사용한다.
새 manifest/budget field는 추가하지 않는다.

```text
metadata_phase_timeout_ms = min(30_000, 8 * source.budget.metadata_statement_timeout_ms)
```

각 deadline은 pool connection checkout 직전에 시작해 transaction `COMMIT`과 connection
return/reset이 끝난 뒤 종료한다. Pre-discovery와 authoritative phase가 각각 자기 deadline 전부를 가지며
남은 시간을 서로 이월하지 않는다. Authoritative phase에서는 이 client-side deadline만 lock 전부터
활성이고 PostgreSQL `statement_timeout`은 lock 뒤 existing setting step에서 원래
`metadata_statement_timeout_ms`로 설정한다. 따라서 lock 전에 timeout-setting `SELECT`를 넣지 않는다.
Deadline expiry는 in-flight operation을 cancel하고 connection을 rollback/reset한다. Cancel/rollback이
1,000ms 안에 끝나지 않으면 connection을 close/discard해 pool에 반환하지 않는다. External task
cancellation은 같은 cleanup 뒤 그대로 전파하고 Metadata failure로 삼키지 않는다. Active RLS refresh의
phase timeout은 아래 no-stale `METADATA_UNAVAILABLE`이고 partial root/snapshot을 publish하지 않는다.

```text
RLS pool connection startup: client_encoding=UTF8

pre-discovery checkout
  -> no-SQL PostgreSQL-18/server=UTF8/client=UTF8/driver-codec admission
  -> begin read-only transaction
  -> allowed schema의 candidate root view exact names 수집
  -> commit

authoritative checkout
  -> no-SQL PostgreSQL-18/server=UTF8/client=UTF8/driver-codec admission
  -> BEGIN REPEATABLE READ READ ONLY
  -> LOCK TABLE <all sorted candidate roots> IN ACCESS SHARE MODE NOWAIT
  -> TimeZone=UTC                         # 여전히 첫 settings statement
  -> existing transaction-local budget/search_path/row_security/tenant settings
  -> common reader-session probe
  -> 같은 connection info를 SQL 없이 재확인
  -> root view list 재조회; pre-discovery와 exact equality 확인
  -> recursive admission + fingerprint + existing catalog collection
  -> commit
```

Missing/added/renamed root, lock conflict와 any validation failure는 partial snapshot 없이 rollback한다.
Identifier는 structured schema/name에서 `psycopg.sql.Identifier`로만 조립한다. Metadata가 hidden graph
SQL과 canonical provider를 소유하고 Source Catalog 또는 Guarded Query에 복제하지 않는다.

QueryService는 source, metadata와 SQL validation 뒤 실제 `ValidatedSql.relations`에 대응하는 root
attestation만 schema/name C order로 묶는다. 모든 referenced relation이 v2 relation에 존재해야 한다.
Executor의 required coordinated Python contract는 다음과 같다.

```python
execute(
    source,
    sql,
    metadata_revision,
    validated,
    *,
    rls_attestation: RlsQueryAttestation | None,
    query_id: str | None = None,
    tenant_id: str | None = None,
)
```

Non-RLS v1은 `None`, RLS v2는 relation을 참조하지 않는 `SELECT 1`도 version 1의 empty-roots object를
전달한다. 따라서 bundle 존재가 tenant-required의 authoritative 신호이며 순간적으로 old
`SourceProfile.tenant_isolation`이 섞여도 tenant 없는 실행을 허용하지 않는다.

RLS query transaction의 exact 순서는 다음과 같다.

```text
RLS pool connection startup: client_encoding=UTF8
checkout: no-SQL PostgreSQL-18/server=UTF8/client=UTF8/driver-codec admission
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY
1. LOCK TABLE <sorted referenced root views> IN ACCESS SHARE MODE NOWAIT
2. TimeZone=UTC                         # 첫 settings statement
3. existing transaction-local budget/search_path/row_security/tenant settings
4. common reader/session policy probe, including exact trusted tenant
5. 같은 connection info를 SQL 없이 재확인
6. Metadata-owned live per-root RLS graph admission and fingerprint comparison
7. existing resolved-object validation
8. EXPLAIN budget admission
9. user cursor execute/fetch/encode
10. success COMMIT; every failure/cancel/disconnect ROLLBACK
```

Empty-roots RLS query는 relation lock/fingerprint loop만 비고 2~10단계와 tenant requirement는 그대로다.
Lock conflict는 기다리거나 retry하지 않는다. Fingerprint mismatch, malformed graph와 probe failure 뒤
resolved-object/`EXPLAIN`/user SQL을 실행하지 않는다.

QueryService는 metadata read 뒤 `SourceReader.get(source_id)`를 다시 읽고
`control_generation`, `control_state_version`, enabled existence와 `tenant_isolation`이 처음 읽은 profile과
같은지 확인한다. 불일치면 executor에 들어가지 않고 fail-closed한다. Executor는 bundle과 source mode가
서로 모순되거나 RLS bundle인데 tenant가 없으면 DB user SQL 전에 다시 거부한다.

Source generation, state 또는 `tenant_isolation` 변경 apply는 Metadata Catalog와 Guarded Query pool을
모두 invalidate한 뒤 새 profile을 노출한다. RLS profile은 startup UTF8 option 없이 만들어진 old pool을
재사용하지 않는다. Invalidation/apply 실패는 old/new pool을 섞지 않고 source를 unavailable로 둔다.

기존 tenant-error precedence는 보존한다. 첫 stable `SourceProfile`이 RLS인데 trusted tenant가 없으면
metadata read 전에 HTTP 400 `QUERY_REJECTED`와 bounded
`reason_code=TENANT_CONTEXT_REQUIRED`다. 따라서 active RLS v1도 tenant가 없으면 이 결과가 먼저이고,
tenant가 있을 때 v1 `METADATA_UNAVAILABLE`을 확인한다. 첫 profile이 non-RLS였는데 later v2 bundle이
보이는 reload race는 tenant 유무와 무관하게 source second-read mismatch/metadata-mode validation을
먼저 `METADATA_UNAVAILABLE`로 닫는다. QueryService의 stable source-mode/snapshot contradiction도
tenant 유무보다 먼저 `METADATA_UNAVAILABLE`이다. 그 invariant을 통과한 stable RLS v2 bundle에서
tenant가 없는 방어 경로만 existing `QUERY_REJECTED` reason을 쓴다. Executor는 먼저 source-mode/bundle
consistency를 확인해 contradiction이면 tenant 유무와 무관하게 details 없는 `QUERY_UNAVAILABLE`,
일치하는 RLS bundle인 경우에만 tenant 누락을 existing `QUERY_REJECTED` reason으로 거부한다.

### 6. Error, cache and disclosure contract

새 public error code와 HTTP/MCP field를 만들지 않는다.

| Condition | Public result | Side effect/stale rule |
|---|---|---|
| Control candidate의 PostgreSQL-version/UTF8/driver-type invariant mismatch | existing `SOURCE_VALIDATION_FAILED` | Candidate generation/snapshot/pointer를 쓰지 않는다. Pre-BEGIN이면 즉시, live recheck이면 rollback 뒤 connection을 close/discard한다. |
| Control candidate의 structural/policy/snapshot-codec violation | existing `SOURCE_VALIDATION_FAILED` | Candidate generation/snapshot/pointer를 쓰지 않고 transaction을 rollback/reset한다. Reset이 실패할 때만 connection을 discard한다. |
| Active RLS metadata의 PostgreSQL-version/UTF8/driver-type invariant mismatch | details 없는 existing `METADATA_UNAVAILABLE` | Older v2 stale fallback을 금지한다. Pre-BEGIN이면 즉시, live recheck이면 rollback 뒤 connection을 close/discard한다. |
| Trusted tenant가 있는 active RLS v1, invalid v2, deterministic graph violation 또는 refresh drift | details 없는 existing `METADATA_UNAVAILABLE` | Cached/persisted v1 또는 older v2 stale fallback을 금지하고 transaction을 rollback/reset한다. Reset이 실패할 때만 connection을 discard한다. |
| Active RLS metadata pre-discovery/lock/live attestation의 `55P03`, timeout, connection/transport/driver failure | details 없는 existing `METADATA_UNAVAILABLE` | Transient 여부와 무관하게 그 refresh에서 older v2 stale fallback을 금지하고 partial publish하지 않는다. Cancellation은 삼키지 않는다. |
| Source generation/state/mode second-read mismatch | details 없는 existing `METADATA_UNAVAILABLE` | Executor와 source SQL에 들어가지 않는다. |
| Stable source mode 또는 consistency를 통과한 v2 bundle이 RLS인데 trusted tenant 없음 | existing HTTP 400 `QUERY_REJECTED`, detail `reason_code=TENANT_CONTEXT_REQUIRED` | Existing source-mode check는 metadata 전, bundle defense는 lock/plan/user SQL 전에 거부한다. |
| Executor까지 도달한 source-mode/bundle contradiction, tenant 동시 누락 포함 | details 없는 existing `QUERY_UNAVAILABLE` | Contradiction을 tenant check보다 먼저, lock/plan/user SQL 전에 거부한다. |
| Query checkout/live PostgreSQL-version/UTF8 mismatch | details 없는 existing `QUERY_UNAVAILABLE` (503) | Checkout mismatch는 `BEGIN` 없이, live mismatch는 rollback 뒤 connection을 close/discard한다. Resolved-object/plan/user SQL은 실행하지 않는다. |
| Query lock `NOWAIT` conflict, live fingerprint mismatch, graph/probe non-timeout internal/driver failure | details 없는 existing `QUERY_UNAVAILABLE` (503) | Resolved-object/plan/user SQL 전 rollback/reset한다. Driver state가 복구되지 않으면 connection을 discard한다. |
| Query transaction의 live attestation이 existing transaction/statement deadline을 초과하거나 DB statement가 timeout/cancel됨 | existing `QUERY_TIMEOUT`과 current operator/shutdown cancellation mapping | Partial attestation/result 없이 rollback하고 pool reset/recovery를 보존한다. |
| Client의 old metadata/SQL-policy token | existing `METADATA_REVISION_MISMATCH` | Executor 전에 거부하고 context refetch가 필요하다. |

RLS graph timeout은 위 existing query/catalog deadline과 cancellation 분류를 따른다. SQL, hidden relation,
policy/role/expression, OID/raw node와 RLS attestation fingerprint는 response, safe detail, metric label
또는 새 provider/helper log에 넣지 않는다. 기존 gateway의 trusted-tenant deny audit와 public SQL
fingerprint audit/redaction 계약은 제거하거나 확장하지 않고 그대로 보존한다. 내부 bounded
reason/metric은 secret-free cardinality만 가질 수 있다.

Fresh metadata cache hit는 live RLS 증거가 아니다. 매 RLS user query가 own transaction에서 lock-first
comparison을 수행한다. Deterministic attestation failure는 cache age와 무관하게 stale fallback을
재열지 않는다. RLS refresh가 실제 live attestation을 시작한 뒤 생긴 lock conflict, timeout 또는
transport/driver failure도 older cached/persisted v2로 대체하지 않는다. 아직 refresh를 시도하지 않은
fresh-cache read 자체는 current cache contract대로 허용하지만 query-time verifier를 생략하지 않는다.

### 7. Provider, consumer and ownership impact

- **Metadata provider:** recursive discovery/admission/canonical helper, immutable types, relation
  attestation, RLS-specific stored-binding rule, v1/v2 codec/revision과 no-stale classification을
  소유한다. Source Catalog의 RLS connection policy와 Guarded Query의 public
  `validate_sql`/`SQL_POLICY_REVISION`만 소비한다.
- **Guarded Query consumer:** QueryExecutor required keyword, source-generation second read, tenant
  authority, RLS pool startup client UTF8, lock-first comparison 호출과 query error/rollback 순서를
  소유한다. Encoder policy/shape는 그대로지만 prior non-UTF8 client와 비교한 public value/hash는 바뀌거나
  새 admission에서 거부될 수 있다.
- **Source Catalog provider:** `RLS_READER_CLIENT_ENCODING`과 no-SQL
  `require_rls_reader_connection_policy`를 소유한다. Metadata/Guarded Query pool이 startup client UTF8과
  checkout/live invariant에 이를 소비하고 common reader settings와 current `SourceProfile`/manifest v2는
  보존한다. `TimeZone=UTC`는 lock 뒤 첫 settings statement다. 새 manifest fingerprint/timeout field를
  만들지 않고 existing metadata statement budget에서 fixed phase deadline을 derive한다. Non-RLS pool은
  이 결정에서 바꾸지 않는다.
- **Control Plane consumer:** existing JSONB append/pointer transaction으로 v2를 저장하고 isolated
  candidate failure, current/rollback reissue와 verified publish를 조율한다. Control SQL schema migration은
  없다.
- **Delivery consumer:** 기존 tenant derivation과 public error mapping/redaction을 그대로 사용하며
  request/response/tool field를 추가하지 않는다.
- **Runtime consumer:** old process/connection drain, route-off new fleet, replica convergence와 safe
  binary rollback을 조립한다. Fingerprint 업무 규칙을 구현하지 않는다.
- **Assurance verifier:** strict xfail을 fail-closed passing regression으로 바꾸고 provider/consumer,
  race, cache, migration과 cleanup을 검증한다. Offline CLI는 계속 tenant를 공급하지 않는다.

Shared `reader_policy.py`, `catalog.py`, `query.py`, `models.py`, `metadata_store.py`, integration fixture와
contract 문서는 coordinating workstream이 single-writer로 직렬화한다. Source Catalog reader-connection
provider baseline을 먼저 동결하고 Metadata graph/snapshot/provider를 그다음 확정한다. 두 baseline 뒤에
Guarded Query와 Control/Runtime/Assurance consumer를 갱신한다. 고정된 서로 다른 consumer 구현만
병렬화한다.

### 8. Migration, cutover and rollback

RLS snapshot v2는 old strict decoder와 serving-compatible하지 않다. 구 process가 decode 실패 전에
cached RLS v1을 계속 사용할 수 있으므로 rolling mixed fleet를 허용하지 않는다.

현재 repository RLS fixture도 그대로는 A를 만족하지 않는다. `tenant_ai.private_records`는 FORCE와
reader-direct SELECT policy를 이미 가지므로 target role은 보존할 수 있지만, tenant comparison에 exact
`COLLATE pg_catalog."C"`가 없고 별도 `query_man_admin FOR ALL USING(true)` policy가 있다. 후자는 role
membership 변화로 SELECT permissive `OR`가 될 수 있어 제거해야 한다. Local bootstrap admin은
superuser라 seed/write가 이 policy에 의존하지 않지만 managed DB에서는 이를 가정하지 않는다.
Protected inventory에서 admin write가 `FOR ALL` policy에 의존하면 권한을 자동 재작성하지 않고
DB owner가 별도 non-SELECT policy/trusted maintenance path와 rollback을 승인할 때까지 중단한다.
Strict corner sentinel도 현재 FORCE가 없고 default `FOR ALL`이므로 승인 뒤 test fixture를 exact FORCE +
SELECT grammar로 바꿔야 한다. 이는 단순 test 정리가 아니라 source DB policy migration 영향이다.

RLS connection/identifier identity도 PostgreSQL 18/UTF8로 inventory한다. Non-UTF8 database는 PostgreSQL
in-place setting 변경으로 전환하지 않고 해당 source를 unroute/deactivate한 채 둔다. DB owner가 data,
identifier, collation과 credential/connection identity를 보존하는 source별 UTF8 database migration 또는
re-onboarding plan과 rollback을 별도 제시·승인받은 뒤에만 이 cutover에 다시 넣는다. ADR 0024 승인은
unknown protected data migration 실행 승인이 아니다. Server는 UTF8이지만 prior client encoding이
UTF8이 아니었던 source도 startup pin으로 text adaptation, public row/value와 verified hash가 달라질 수
있다. Encoder policy/response shape를 바꾸지는 않지만 current/rollback verified contract를 모두 새
connection으로 재실행해 결과를 비교하고 설명·승인되지 않은 value/hash/rejection이면 route 전에
중단한다.

Fresh bootstrap SQL만 고치는 것은 existing source migration이 아니다. Repository fixture는 versioned
forward/rollback source-policy SQL artifact와 checksum을 추가하고 fresh init은 forward 결과와 같게
유지한다. Managed source마다 DB owner가 제공한 forward/rollback migration reference와 checksum,
pre/post policy inventory를 protected change record에 남긴다. Query Man application이 source DDL을
자동 실행하지 않는다. Rollback SQL을 적용할 때도 해당 RLS source는 먼저 unroute/drain하고 v1로
다시 serve하지 않는다.

Verified reissue의 protected external tenant mapping v1은 Control DB/repository에 ingest하지 않는 다음
exact record다. Entry key `(source_id, query_id, old_metadata_revision)`는 unique하고 captured verified row와
exact match해야 한다. Value는 `tenant_id`, existing access-policy의 `operator` role을 가진
`operator_subject`, original identity/access authority를 가리키는 `authority_ref`다.
`operator_subject`는 existing access-policy와 `CallerContext`의 exact `caller_id`이고 authenticated
caller ID와 byte-for-byte 같아야 한다. `authority_ref`는 그 caller/tenant/role binding을 승인한
identity/access authority artifact의 immutable version, digest와 protected reference를 모두 가진다.
Reissue 후 같은 entry에 `new_metadata_revision`과 immutable `verified_receipt_ref`를 append한다.
Duplicate key, missing/multiple tenant, caller ID/tenant mismatch와 missing/changed authority는 중단한다.
Record는 mapped current와 rollback counterpart가 모두 retirement된 뒤까지 protected change evidence로
보존한다.

각 reissue는 새 override/tool이 아니라 existing verified admin endpoint를 사용하고 authenticated
operator caller의 `tenant_id`가 mapping과 byte-for-byte 같아야 한다. 필요한 mapped tenant의 operator
principal을 existing access-policy schema로 제공할 수 없으면 중단하고 별도 reissue identity/tool 계약을
승인받는다. Offline Assurance CLI는 tenant가 없으므로 RLS verified reissue 대체 수단이 아니다.

다음 1~8은 ENC v3보다 먼저 RLS v2를 독립 배포하는 standalone cutover다.

1. Protected source/admin/verified mutation을 freeze한다.
2. 모든 RLS source의 current 및 지정 rollback generation/revision/L2, credential/artifact와 verified
   contract, PostgreSQL version/server/client encoding, exact tenant column/policy와 admin write dependency를
   read-only inventory한다. Non-UTF8 database는 별도 DB-owner migration/re-onboarding 승인 없이 이
   cutover에서 제외하고 unroute/deactivate한다. 각 verified
   contract의 trusted tenant는 protected external
   change record에 mapping하고 누락 시 중단한다. Tenant를 Control DB schema나 public wire에 새로
   저장하지 않는다.
3. RLS admission/route를 닫고 active query, query/catalog/staging connection과 poller를 0까지 drain한
   뒤 모든 old process를 중지한다.
4. DB owner가 dedicated maintenance connection 하나로 captured checksum의 forward source-policy
   migration을 transactionally 적용하고 post-policy inventory/checksum을 남긴 뒤 connection을 닫는다.
   이 동안 new poller/catalog/staging/query connection을 시작하지 않는다. Apply/postcondition 실패는
   migration transaction을 rollback하고 source를 unroute 상태로 둔다.
5. New release를 route 밖에서 시작한다. Active RLS v1은 available/readiness 근거가 될 수 없다.
6. 지정 rollback baseline부터 current baseline까지 fresh graph를 검증해 immutable v2
   generation/snapshot/revision을 append한다. 각 verified contract는 protected trusted tenant로 전량
   재실행하고 새 revision-bound row와 L1/L2를 만든다. 기존 result가 같아도 자동 승계하지 않고,
   startup client UTF8 때문에 달라진 value/hash/rejection은 별도 설명·승인 없이는 중단한다.
7. 모든 replica가 expected generation/revision, `available`, `drift=[]`, L2에 수렴하고 stale token 409,
   strict RLS regression과 residue 0을 확인한다.
8. Intended current v2만 active로 두고 route한다. V1/v2 row와 verified history는 삭제하지 않는다.

Functional rollback은 위 과정에서 미리 재발행·검증한 v2 rollback generation에만 수행한다. V1 RLS
snapshot과 non-UTF8 source/rollback counterpart를 activate/serve하지 않는다. Old binary로 rollback해야 하면 먼저 모든 RLS source를
deactivate/unroute하고 connection을 drain한 뒤 non-RLS source만 old release에서 제공한다. RLS availability를
포기하는 것이 tenant isolation을 깨는 binary rollback보다 안전하다.

현재 TODO의 권장 production path처럼 ADR 0020 encoding v3와 같은 protected change record에서
수행하면 v1→v3 direct reissue를 사용한다. Route하지 않을 production v2 current/rollback row를
중간 산출물로 만들지 않는다. V2 codec/revision golden과 disposable standalone v2 acceptance는 RLS-02
repository baseline에 남기되 protected current/rollback verified contract는 fresh v3 counterpart로만
전량 재발행한다. Functional rollback도 미리 재발행·검증한 v3 rollback generation으로 한다. 별도로
실제 배포·검증해 captured한 v2 release/generation이 이미 있는 경우에만 v3→v2 rollback할 수 있다.
V3→v1 binary/pointer rollback은 위 RLS deactivate/unroute 규칙을 따른다.

### 9. Residual limitation

PUBLIC-or-exact-reader-only SELECT policy, FORCE RLS와 custom dependency rejection은 ordinary role membership과 custom
function body race가 SELECT isolation을 넓히지 못하게 한다. Relation lock은 policy/table/view DDL을
transaction 끝까지 막는다.

그러나 source superuser 또는 role admin은 relation lock과 무관하게 actual reader를 `SUPERUSER`/
`BYPASSRLS`로 바꾸거나 PostgreSQL builtin/catalog/binary를 변조할 수 있다. Common reader probe와
`row_security_active()`는 검사 시점의 drift를 잡지만 privileged mutation과 user SQL 사이의 adversarial
race를 직렬화하지 않는다. `RLS-01-A`는 source DB/role admin과 admitted `pg_catalog`
implementation/PostgreSQL image를 trusted,
mutation-free serving boundary로 두고, 그 identity/privilege/image 변경에는 RLS route drain, full
inventory와 reissue를 요구한다. Adversarial privileged writer까지 막으려면 별도 source-side advisory
lock/broker contract와 사용자 승인이 필요하다.

Attestation은 policy가 exact `tenant_id` column을 trusted context와 비교함을 증명하지만 business row에
잘못된 tenant ID를 기록한 data-quality 오류까지 판별하지 않는다. DB owner의 constraint/write path와
source-data migration이 그 값의 권위와 정확성을 책임진다. Query Man에 row provenance나 cross-system
tenant registry comparison을 추가하려면 별도 data contract가 필요하다.

`NOWAIT`는 DDL과 query가 겹칠 때 availability를 낮출 수 있다. 이는 unbounded wait/deadlock 또는
잠재 누출보다 선택한 fail-closed 결과다.

## Verification Required For `RLS-01-A`

- Existing `ALTER POLICY ... USING(true)`와 `DISABLE ROW LEVEL SECURITY` strict xfail 두 parameter를
  details 없는 fail-closed passing regression으로 전환한다.
- Root/nested invoker positive, nested owner-rights, materialized/foreign/partition/inheritance negative.
- Recursive view SQL-policy positive corpus와 `set_config`, volatile/security-definer,
  file/program/dynamic-SQL, custom 또는 non-allowlisted built-in function/operator/type negative corpus.
- `_RETURN` exact shape, internal/normal/custom/unknown `pg_depend`, whole/column edge, exact duplicate와
  PostgreSQL-18 initdb-OID visible candidate admission corpus. Lock 앞 tracing/catalog `SELECT`가 없어야
  한다는 first-relation-action event assertion.
- Policy별 exact table-auto/tenant-column `pg_depend`, PUBLIC empty 또는 exact-reader 단일
  `pg_shdepend`, duplicate/custom/unknown row와 per-root/provider-call derived bound corpus. Same policy의
  unexpected `objsubid`는 count/reject하고 unrelated policy row는 무시하며, 두 database의 colliding
  policy OID/shared row에서 current-DB row만 scope하는 corpus를 포함한다. 여러 root가 같은 policy에
  도달해도 distinct object 한 번으로 세고 bound/bound+1을 검증한다.
- ENABLE/FORCE, owner, reader superuser/BYPASS, `row_security_active`, zero/one/multiple SELECT policy,
  PUBLIC/exact-reader/group/multiple role, permissive/restrictive/ALL과 commuted exact C-collated equality corpus.
- Tenant column nullable/non-text/cast/collation, subquery, boolean wrapper, custom function/operator/type/
  collation과 security-definer leak corpus.
- Same-name policy/role/table/view/function/operator drop/recreate, OID change와 raw-node binding corpus.
- DDL이 먼저 lock을 가진 경우 query가 `NOWAIT`로 plan 전 실패하고, query가 먼저 lock을 가진 경우
  DDL이 commit까지 진행하지 못하는 deterministic event-order test.
- Snapshot-before-lock에서는 old catalog/new execution이 갈라지는 PostgreSQL negative
  characterization과 lock-first에서 latest catalog가 보이는 positive control.
- Pre-discovery/root-list race, graph depth/relation/policy/dependency/text/material byte bound와 duplicate/
  unresolved row no-stale rejection.
- Cold/warm/stale cache, pinned RLS v1, corrupt/mixed v2, pinned RLS v2와 same live fingerprint의 serving,
  pinned RLS v2와 different live fingerprint의 no-stale `METADATA_UNAVAILABLE`, source
  generation/state/mode reload race와 relationless RLS query tenant requirement.
- Tenant parallel execution, pool reset, timeout/cancel/disconnect, partial-row 폐기와 raw detail 비노출.
- RLS pool create/reset/reconnect마다 startup client UTF8가 적용되고 source mode/generation 전환이 old
  non-RLS/RLS pool을 invalidate하며, pre-BEGIN mismatch는 transaction 없이 discard하고 post-lock
  mismatch는 rollback 뒤 discard하는 event corpus.
- PostgreSQL 18 UTF8 libc/ICU/builtin locale별 admission, exact-C tenant result와 같은 DB 반복 호출
  fingerprint stability, 별도 fixed canonical fixture golden을 검증한다. Cross-DB fingerprint equality는
  모든 canonical leaf/raw-node bytes가 실제 같음을 먼저 증명한 통제 fixture에만 요구한다. Strict `str`,
  multibyte byte bound/Unicode non-normalization도 검증한다. SQL_ASCII/non-UTF8 server와 non-UTF8 startup client는
  relation/catalog SQL 전, fetched unexpected bytes/surrogate는 publish/plan/user SQL 전 candidate·refresh·
  query no-stale로 거부한다. Raw `e9`/LATIN1와 UTF8 `c3a9`의 same-Python-name/different-relation
  characterization도 유지한다.
- V1/v2 strict codec and revision golden, historical old-policy v2 decode와 current-policy serving rejection,
  old decoder incompatibility, current/rollback full verified reissue, replica convergence,
  functional/binary rollback과 immutable history preservation.
- Fresh init과 existing repository DB의 forward/rollback policy migration artifact/checksum, source-owner
  managed migration reference stop condition. Disposable managed RLS source에서 tenant별 existing-schema
  operator identity, current/rollback verified reissue, two-replica convergence와 standalone v2 functional/
  binary rollback acceptance. Direct-v3 acceptance와 v3 rollback은 ADR 0020 `ENC-01` exact 승인 및
  `ENC-02` cumulative provider baseline 뒤 `RLS-03`/`TIME-03`에서만 실행하며 RLS-02에 skip/xfail
  placeholder로 선결정하지 않는다.
- Repository gates `uv run ruff check .`, `uv run mypy src`, `uv run pytest`,
  `uv run pytest -m integration`.

## Approval Boundary

이 ADR은 exact 제안일 뿐 승인된 계약이나 구현이 아니다. 현재의 일반적인 “승인합니다”는 아래
policy grammar, persisted/Python contract, error, cutover와 residual boundary 전체를 특정하지 않으므로
`RLS-01-A` 구현 승인이 아니다. A를 구현하려면 다음 범위를 그대로 승인하거나 차이를 정확히 지정해야
한다. B는 direction-only고 C는 production-blocking defer다.

```text
RLS-01-A를 ADR 0024의 ordinary invoker-view/ordinary-table-only recursive graph,
ENABLE+FORCE RLS와 current_user=session_user=configured-reader 및 effective reader non-owner,
정확히 하나의 PUBLIC 또는 exact-reader permissive SELECT
tenant-equality policy와 plain non-generated text `tenant_id`, exact `pg_catalog.C` comparison,
policy별 exact table-auto/tenant-column `pg_depend`와 PUBLIC-empty 또는 named-reader-one
`pg_shdepend` raw shape·derived bound,
current SQL_POLICY_REVISION으로 검증한 nested view와 custom/volatile/security-definer/non-allowlisted
function/operator/type/collation·subquery·partition/foreign/materialized/
inheritance 거부, exact `_RETURN`/`pg_depend`와 PostgreSQL-18 initdb builtin admission,
RLS Catalog/Query connection-startup `client_encoding=UTF8`, no-SQL PostgreSQL-18/server/client UTF8
admission과 strict str-only graph text/bytes fail-closed, rls_attestation_v1 canonical
shape·정렬·per-root/source 고정 bound, relation별 snapshot/revision v2,
attestation에 고정한 view SQL policy revision, required RlsQueryAttestation/QueryExecutor 계약,
BEGIN 직후 ACCESS SHARE NOWAIT lock-first 순서,
existing metadata statement budget에서 derive한 pre-discovery/authoritative outer deadline과 bounded
cleanup, source generation/state 재확인, HTTP 400 QUERY_REJECTED/TENANT_CONTEXT_REQUIRED reason을 포함한 exact
no-stale/public error와 기존 gateway tenant/SQL-fingerprint audit 보존·RLS fingerprint 비공개,
manifest와 Control SQL schema 무변경, existing operator caller를 쓰는 protected external
verified-tenant mapping v1을 승인한다. Standalone RLS 배포는 current/rollback v2를 전량 재발행하고,
ENC와 통합한 권장 배포는 production v2 row 없이 current/rollback v3로 direct 재발행하며 verified v3
functional rollback만 허용한다. Mixed fleet 금지와 v1 history-only·old-binary RLS deactivate rollback도
승인한다. 이 RLS 승인만으로 v3 provider/codec/test 구현을 시작하지 않고 ADR 0020 `ENC-01` exact
승인과 `ENC-02` baseline을 별도로 기다린다.
Repository/managed source의 comparison을 exact C-collated policy로 migration하고 SELECT에 영향을 주는
admin/group/ALL policy를 제거하되, admin write dependency가 발견되면 별도 승인 전 중단하는 영향도
승인한다. Fresh init 외에 repository forward/rollback SQL artifact와 managed DB-owner migration
reference/checksum이 필요하고 source DDL은 application이 자동 실행하지 않는 범위도 승인한다.
Non-UTF8 RLS database는 자동 변환하지 않고 즉시 unroute/deactivate하며, source별 DB-owner data
migration/re-onboarding과 rollback을 별도 승인하고 fresh v2를 발행하기 전에는 publish/route하지 않는
영향도 승인한다. 이 승인은 그 protected data migration 실행 자체의 승인이 아니다.
각 protected row의 exact `tenant_id` 값은 DB owner가 보장하는 authoritative source-data contract다.
Source superuser/role-admin/PostgreSQL image는 mutation-free trusted serving boundary이고 이를
adversarial하게 직렬화하려면 별도 broker 계약이 필요하다는 residual limitation도 수용한다.
이 UTF8 admission은 RLS v2 pool/source identity에만 적용하고 non-RLS pool과 result encoder policy/
response shape는 바꾸지 않는다. 다만 prior non-UTF8 client의 RLS public value/hash/rejection은 달라질
수 있어 current/rollback verified 전량 재실행과 설명되지 않은 차이의 별도 승인이 필요하다.
Interval/JSON/result OID와 cumulative source-semantics를 포함한 broader encoding 계약은 별도 ADR
0020/ENC-01 범위라는 경계도 수용한다.
```
