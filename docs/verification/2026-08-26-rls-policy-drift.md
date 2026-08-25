# RLS Policy Drift Security Finding — 2026-08-26

Status: Open — contract decision and fail-closed implementation required

## Why This Is Not Accepted Current Behavior

[ADR 0014](../decisions/0014-trusted-rls-tenant-context.md)는 authenticated tenant를 transaction-local
`query_man.tenant_id`로 전달하고, `row_security=on`, restricted `NOBYPASSRLS` reader와
`security_invoker=true` view를 확인해 다른 tenant의 행이 보이지 않도록 결정했다. 실제 코드는 이
공개 view와 reader/session 표면은 확인하지만, view가 읽는 private base relation의 RLS enablement와
`pg_policy` 의미를 metadata revision 또는 query-time admission에서 attest하지 않는다.

따라서 아래 결과는 호환성을 위해 보존할 current contract가 아니라 accepted tenant-isolation
contract를 위반하는 보안 결함이다. 누출 결과를 통과 기대값으로 고정하지 않는다.

## Independent PostgreSQL 18.6 Reproduction

두 개의 UUID disposable database에서 독립적으로 다음 경계를 확인했다.

- Reader: `LOGIN`, `NOSUPERUSER`, `NOBYPASSRLS`, `NOINHERIT`, 필요한 schema `USAGE`와
  base/view `SELECT`만 보유
- Source: `tenant_isolation=rls`, `allowed_schemas=(analytics,)`, relation kind `view`
- Public view: `security_invoker=true`
- Base table: tenant column, RLS enabled, reader 대상 policy가
  `tenant_id = current_setting('query_man.tenant_id', true)`를 강제
- Query: 실제 `QueryService.query(..., tenant_id=<authenticated tenant>)` 경로
- Baseline: 요청 tenant의 행만 반환

다음 두 mutation을 각각 실행했다.

```sql
ALTER POLICY tenant_filter ON private.tenant_records USING (true);
```

```sql
ALTER TABLE private.tenant_records DISABLE ROW LEVEL SECURITY;
```

두 경우 모두 public view definition, `security_invoker`, reader privilege, `row_security=on` session
probe, `CatalogSnapshot`과 metadata revision은 그대로였다. 그러나 같은 authenticated tenant query가
다른 tenant 행까지 성공 응답으로 반환했다.

한-row/two-row 최소 corpus의 exact hash는 다음과 같다.

```text
tenant-only: sha256:e31870f73f15d65fbe67fa0b6e57f9b36d130e8dba5340c0fd2e3cad1b455270
cross-tenant: sha256:6432f5ac9d37fc70b8d0b5caa12a95340df62491a9e5b994cb36d16014b17f70
```

각 probe 종료 뒤 connection/database/role residue는 모두 `0`이었다.

## Runnable Sentinel

[`test_rls_source_requires_base_policy_drift_to_preserve_isolation`](../../tests/test_source_database_corners.py)은
policy 완화와 RLS disable을 각각 별도 disposable DB parameter로 실행한다. 두 parameter 모두 다른
tenant 행이 성공 결과가 되어서는 안 된다는 안전 기대값을 `strict=True` xfail로 남기며, 알려진
cross-tenant 결과를 확인한 전용 `_RlsIsolationViolationError`만 XFAIL로 인정한다. Setup/query/cleanup의
다른 예외는 숨기지 않는다. 현재 suite에서 두 case는 모두 `XFAIL`이어야 하며 `PASS`로 세지 않는다.
정확한 provider, fingerprint/admission 위치와 public error는 아직 승인되지 않았으므로 임의의
`METADATA_UNAVAILABLE` 또는 `QUERY_UNAVAILABLE` 계약을 테스트에 선결정하지 않았다.

## Disposable Lock And Dependency Follow-Up

후속 PostgreSQL 18.6 disposable characterization은 격리된 test database/role에만 DDL을 실행하고 제품
파일, production source와 persisted state를 바꾸지 않은 채
[proposed ADR 0024](../decisions/0024-rls-policy-drift-attestation.md)의 exact 선택지를 작성하기 위한
근거만 수집했다.

- Repeatable-read transaction에서 `set_config`/reader probe로 snapshot을 먼저 만든 뒤 concurrent
  `ALTER POLICY ... USING(true)`가 commit할 때까지 view lock을 기다리면, 같은 transaction의
  `pg_policy` 조회에는 옛 tenant predicate가 보이지만 실제 view query에는 새 permissive policy가
  적용되어 두 tenant 행이 반환됐다.
- `BEGIN` 직후 다른 `SELECT`보다 먼저 published view를 `ACCESS SHARE`로 lock하면, concurrent DDL이
  먼저 commit한 경우 새 policy가 catalog에 보이고 query가 먼저 lock한 경우 policy/RLS/view DDL은
  transaction 종료까지 진행하지 못했다.
- Root view 하나의 lock은 nested view와 private table까지 재귀적으로 잡았다. `ALTER POLICY`, RLS
  disable과 nested view replacement는 충돌했지만 `CREATE OR REPLACE FUNCTION`과 role membership
  `GRANT`는 성공했다.
- Outer와 nested view가 모두 security invoker인 경우 tenant row만 보였지만 nested owner-rights
  view와 security-definer custom function을 통하면 다른 tenant 행이 보였다.
- Reader 자체가 `NOINHERIT`여도 PostgreSQL 18의 membership edge가 `INHERIT TRUE`이면 group-target
  policy가 적용됐다. Table owner membership도 같은 우회가 생겼고 `FORCE ROW LEVEL SECURITY`가
  owner 우회를 막았다.
- Policy/role drop-recreate 뒤 OID는 바뀌어도 deparsed expression은 같았고, custom function body는
  같은 OID/expression/raw-node hash 아래 결과를 바꿀 수 있었다. Durable identity에 OID를 직접 넣거나
  expression hash만 비교해서는 충분하지 않다.
- 모든 probe 종료 뒤 이 follow-up이 만든 disposable database와 role residue는 각각 `0`, `0`이었다.

## Multi-Database Collation, Policy-Dependency And Text Follow-Up

추가 one-off disposable characterization은 PostgreSQL 18.6에서 다음 5개 UUID test database를 각각 만들었다.
제품 파일, source configuration과 persisted snapshot은 바꾸지 않았다.

- UTF8, libc `C`
- UTF8, libc `en_US.utf8`
- UTF8, ICU `und`
- UTF8, builtin `C.UTF-8`
- SQL_ASCII, libc `C`

모든 locale provider에서 explicit `pg_catalog."C"`는 provider `c`, encoding `-1`, deterministic
`true`, stored/actual version null인 같은 builtin identity였고 policy raw node의 outer
`inputcollid`도 같았다. Database-default comparison 대조군은 다른 default collation binding으로
구별됐다. PUBLIC/exact-reader target과 equality의 좌우를 바꾼 네 허용형은 모두 exact tenant 행만
반환했고 대소문자가 다른 tenant는 제외했다. `pg_get_expr` deparse는 qualification/cast spelling을
보존하지 않으므로 ADR 0024가 deparsed string equality가 아니라 raw binding과 live catalog를
authority로 두는 방향도 확인했다.

허용 policy의 PostgreSQL 18 stored dependency는 다음 exact shape였다. Numeric catalog OID는
실행별 값이므로 contract에 저장하거나 hardcode하지 않는다.

```text
pg_depend, unordered exact set:
  (pg_policy, policy_oid, 0, pg_class, table_oid, 0,                 'a')
  (pg_policy, policy_oid, 0, pg_class, table_oid, tenant_id_attnum, 'n')

pg_shdepend:
  PUBLIC policy      -> empty set
  exact-reader policy ->
    (current_database_oid, pg_policy, policy_oid, 0, pg_authid, reader_oid, 'r')
```

Pinned `=`, `current_setting`, `text`와 `C`는 `pg_depend` row가 없었다. 따라서 missing builtin row를
안전성으로 추론할 수 없고 raw node/live catalog 검사가 필요하다. 반대로 다른 dependency row나 role
shared dependency는 policy text가 같아도 거부해야 한다. 이 결과를 proposed ADR 0024의 exact
policy-dependency projection과 derived bound에 반영했다.

SQL_ASCII client에서는 catalog name, view definition, deparsed expression과 raw node가 psycopg
`bytes`로 반환됐다. ASCII-only 값은 strict UTF-8 round-trip과 UTF8 client에서의 `str` 값이 같은
bytes/hash를 만들었다. Hidden relation name에 invalid UTF-8 byte `ff`가 있으면 SQL_ASCII client는 raw
bytes를 반환하고 UTF8 client는 `CharacterNotInRepertoire`로 실패했다. 둘 다 fail-closed할 수 있지만
기존 제안의 단순 `value.encode("utf-8")`만으로는 bytes input과 non-UTF8 client decoding을 exact하게
다루지 못한다.

별도 exact counterexample은 SQL_ASCII catalog에 raw byte `e9`와 valid UTF-8 bytes `c3a9`인 두 relation/
comment/view definition을 만들었다. LATIN1 client가 raw `e9`를 읽은 값과 UTF8 client가 raw `c3a9`를
읽은 값은 둘 다 exact Python `str("é")`와 UTF-8 `c3a9`가 됐다. 더 중요하게 같은 Python identifier
`private."é"`가 LATIN1 connection에서는 raw-`e9` table을, UTF8 connection에서는 raw-`c3a9` table을
resolve했다. Fetch 뒤 raw bytes를 복원해 fingerprint만 다르게 해도 lock/identifier resolution이 먼저
다른 object를 선택할 수 있으므로 graph-local codec만으로는 충분하지 않다.

따라서 proposal은 standalone RLS v2 Catalog/Query pool도 connection startup에서
`client_encoding=UTF8`을 고정하고 PostgreSQL 18, `server_encoding=UTF8`, `client_encoding=UTF8`과
psycopg UTF-8 codec을 relation access 전 no-SQL connection info로 admission한다. Lock과 common reader
probe 뒤 live provider 직전에도 같은 info를 재확인하고 graph text는 exact `str`만 허용한다. Non-UTF8
RLS source는 UTF8 migration/re-onboarding과 fresh v2 발행 전 publish/route하지 않는다. 이 범위는
RLS identity를 위한 source/session restriction이며 non-RLS pool의 startup encoding/session/result 의미는
바꾸지 않는다. 다만 아래 exact-profile transition lifecycle은 모든 managed source에 공통 적용한다.
Interval/JSON/result OID와 broader source-semantics는 proposed ADR 0020에 그대로 남는다.

탐색, 5-database 재실행, exact policy dependency와 encoding-collision 재실행이 만든 prefix를 독립
조회한 최종 residue는 database `0`, role `0`, cleanup error `0`이었다.

## Read-Only Implementation-Readiness Audit

위 PostgreSQL probe 뒤에는 제품 동작, schema와 source configuration을 바꾸지 않고 현재 runtime의
transition/error 경계를 읽기 전용으로 추적했다. 이 검토는 proposed ADR 0024를 구현했다고 주장하는
acceptance가 아니라, exact 승인 뒤 구현자가 같은 race와 disclosure를 다시 만들지 않게 하는 근거다.

- 현재 `PostgresQueryExecutor`의 pool/semaphore와 `PostgresCatalog` pool은 `source_id`만 key로 사용하고
  두 `invalidate(source_id)`는 map에서 pool을 pop한 뒤 close할 뿐이다. `SourceReloader`에 주입되는 현재
  adapter 순서도 Catalog 다음 Query이고, enabled apply는 registry upsert 뒤 Metadata cache를 invalidate한다.
- 따라서 old query가 source를 읽은 직후 멈추고 reloader가 pool을 close/swap한 다음 재개되면 old
  profile로 pool을 다시 만들거나 같은 source ID의 new-profile pool을 소비할 수 있다. Catalog load와
  resource observation에도 같은 lease/publish race가 있다. QueryService second-read만 추가해도 그 확인
  직후 같은 race가 남으므로 exact-profile fence, active registration과 transition drain이 함께 필요하다.
- 이 repository의 locked `psycopg_pool 3.3.1` source에서 `AsyncConnectionPool.close()`는 waiting client와
  idle connection을 제거하지만 checked-out connection은 pool에 반환될 때까지 닫지 않는다고 명시한다.
  따라서 pool close만으로 registry swap 시점의 old Query/Catalog connection `0`을 증명할 수 없다.
- 현재 snapshot `_decode()`는 deterministic Pydantic failure를
  `StoredMetadataInvalidError(... ) from error`로 보존하고 `SourceReloader.sync()`/apply failure는
  `logger.exception`으로 chain을 render할 수 있다. In-memory canary probe는 outer cause가
  `ValidationError`이고 hidden relation canary가 formatted traceback에 포함되는 결과
  `canary_in_traceback=True`를 확인했다. 제안 계약은 deterministic codec cause/context/input repr을
  제거하되 PostgreSQL I/O/transport/driver failure와 cancellation은 해당 error로 바꾸지 않는다.
- Immutable history decode와 current runtime serving은 같은 판정이 아니다. Old-policy v2를 historical
  golden으로 읽을 수 있어도 public store/service/cold-start path가 current RLS snapshot으로 제공해서는
  안 되며, offline serving-compatibility도 live DB fingerprint equality를 주장할 수 없다. Fresh refresh와
  every RLS query transaction만 live equality를 증명한다.
- Current staging의 Metadata refresh error branch는 `ReaderSessionPolicyError` 또는 public
  `contract_violations` detail만 deterministic validation으로 구분한다. 새 graph provider의 private
  exception/message를 parse하지 않으면서
  deterministic graph failure는 400, `55P03`/deadline/transport/driver failure는 503, task cancellation은
  재전파하려면 Metadata-owned empty-args public marker가 필요하다.

이 audit 뒤 repository integration gate는 `83 passed, 657 deselected, 2 xfailed`였다. 두 XFAIL은 위
cross-tenant policy-drift sentinel이며 unexpected setup/query/cleanup failure는 없었다. Integration 종료 뒤
disposable database와 role residue를 다시 조회한 결과도 각각 `0`, `0`이었다.

따라서 단순 fingerprint 추가가 아니라 strict graph/policy admission, lock-first order와 custom/role
잔여 경계를 하나의 계약으로 결정해야 한다. ADR 0024는 제안 상태이고 정확한 사용자 승인 전에는
현재 snapshot, query order와 strict xfail을 바꾸지 않는다.

## Required Decision Before Implementation

`RLS-01`의 exact proposal은 [proposed ADR 0024](../decisions/0024-rls-policy-drift-attestation.md)의
`RLS-01-A`로 작성했다. 다음 범위는 아직 사용자에게 정확히 승인되지 않았다.

1. Published security-invoker view에서 private/nested dependency를 어디까지 재귀적으로 추적할지
2. Base relation의 `relrowsecurity`, `relforcerowsecurity`, owner와 reader-applicable policy의 command,
   permissive/restrictive composition, target role, `USING`/`WITH CHECK`를 어떤 canonical identity로
   admission할지
3. Custom function/operator와 nested owner-rights view를 거부할지 또는 transitive하게 attest할지
4. Metadata publish/revision과 같은 read-only transaction의 query-time drift check를 어떻게 연결하고
   concurrent DDL race를 어떤 lock/order로 닫을지. 두 Metadata phase는 각각
   `min(30_000, 8 * metadata_statement_timeout_ms)`를 checkout 전부터 return/reset까지 독립 소비하고
   1,000ms cleanup을 넘으면 discard하며, query live attestation은 existing query deadline을 소비한다.
5. Policy `pg_depend`/database-scoped `pg_shdepend`의 exact bound와 RLS source/pool의
   PostgreSQL-18/UTF8 identity를 어떻게 admission할지
6. Stable zero-root/root-count/graph와 codec/Pydantic failure를 hidden cause/context 없는 public
   marker/type으로 구분하고, pre-discovery/authoritative root-list add/drop/rename race 및
   `55P03`/deadline/transport/driver failure와 cancellation을 각각 marker 없는 503/재전파로 보존할지
   Candidate/active/query의 no-SQL connection invariant mismatch, completed common reader-session
   identity/policy mismatch와 fixed-setting SQLSTATE `22023`/`42501`은 각각 existing validation 400,
   `METADATA_UNAVAILABLE`, `QUERY_UNAVAILABLE`로 분류하고 resource observation에서는 exact
   `RESOURCE_READ_FAILED`로 소비한다.
   Deterministic marker/codec error의 direct `__cause__`와 `__context__`도 모두 `None`이어야 하며 rendered
   suppression만으로 충족한 것으로 보지 않는다.
   RLS `ReaderSessionPolicyError`는 위 no-SQL invariant, completed mismatch와 SQLSTATE `22023`/`42501`에서만
   direct cause/context None으로 만들되 각 consumer의 public 결과를 보존한다. Marker로 감싸지 않는 timeout/
   transport/other-driver는 candidate `SOURCE_CONTROL_UNAVAILABLE`, active Metadata
   `METADATA_UNAVAILABLE`, query timeout `QUERY_TIMEOUT`, query transport/other-driver
   `QUERY_UNAVAILABLE`, resource observation `RESOURCE_READ_FAILED`로 각각 소비하고 external cancellation은
   재전파한다. Non-RLS current reader-setting error mapping은 바꾸지 않는다.
   Marker-free transient raw exception은 provider/helper가 log하지 않고 consumer가 분류·cleanup한 뒤 버린다.
   Candidate/active Metadata/query는 except block 밖에서 direct cause/context 없는 existing safe outer error만
   raise하고, resource observation은 exception 없이 fixed reason만 report하며, external cancellation은
   log/wrapping 없이 재전파한다.
7. Private v1/v2 history decode, public offline current-policy serving-compatibility와 fresh refresh/query
   live attestation을 어떻게 분리해 cold start와 stale cache에서 old-policy snapshot을 막을지
8. 모든 managed source transition에서 exact `SourceProfile` fence와
   `invalidate(source_id, *, next_source)`를 사용하고 Query-first Query/Catalog active lease를 fixed
   three-phase(각 최대 1초) cleanup으로 drain한 뒤 Metadata cache, registry 순서로 적용할지. Resource observation과
   partial/disable failure도 fence를 우회하지 않으며 non-RLS active query도 transition
   `QUERY_UNAVAILABLE`로 끝날 수 있는 영향을 포함한다. Transition-cancelled observation은 exact
   `RESOURCE_READ_FAILED`, external observation cancellation은 failure-report 없는 재전파로 구분한다.
   Query terminal result handoff와 transition fence는 같은 lock에서 선형화해 fence-first old result를
   폐기한다. Query/observation의 external 대 transition cancellation은 같은 lock의 first-recorded reason이
   public result/report를 결정하고 later reason이 덮어쓰지 않는다.
   Ordinary active RLS observation의 connection-policy/operational read failure도 exact
   `RESOURCE_READ_FAILED`이며 raw error를 저장하지 않는다. Pre-BEGIN invariant mismatch는 rollback 없이
   close/discard하고 transaction setting/probe/read failure는 rollback/reset recovery 실패/broken connection만
   close/discard한다.
   Secret-bearing exact profile은 password canary가 fence/drain/tombstone retry의 error/log/metric/audit에
   남지 않아야 한다.
   Registry upsert/remove의 successful return만 transition commit point다. Applied-generation/status
   bookkeeping은 post-commit reconciliation이며 bookkeeping/probe failure 또는 external probe cancellation은
   committed projection을 되돌리지 않는다. Failure는 unavailable/failed status를 기록하지만 external
   cancellation은 fabricated status 없이 재전파한다. Same desired projection retry는 drain 없이 bookkeeping만 복구한다.
9. Drift의 cache/stale 금지, managed snapshot migration, current/rollback reissue와 rollback

이 범위는 Source Catalog connection admission, Metadata snapshot/revision, Guarded Query admission/error,
Control Plane apply, Runtime transition과 Assurance acceptance에 영향을 주는 module contract 변경이다.
사용자가 정확한 proposal을 승인하기 전 제품 코드, schema, snapshot codec 또는 production route를
변경하지 않는다.

이 repository contract 승인은 protected inventory/freeze, policy/data DDL, unroute/deactivate,
credential/pointer/reissue, fleet/route cutover와 rollback 실행 권한이 아니다. Standalone v2 환경 작업은
access와 change record를 갖춘 별도 `RLS-03` 승인을 요구하며 `ENC-02`/`TIME-03`을 기다리지 않는다.
Combined direct-v3 환경 작업만 coordinated `RLS-03`/`TIME-03` 승인과 access/change record를 요구한다.

Protected environment의 RLS source inventory, mutation freeze와 unverified route drain은 안전상 우선
조치지만 이 repository 세션에는 production DSN, source 목록, 권한, route/drain 또는 change record가
제공되지 않았다. 따라서 production이 안전하다고 주장하거나 외부 상태를 변경하지 않았다.
