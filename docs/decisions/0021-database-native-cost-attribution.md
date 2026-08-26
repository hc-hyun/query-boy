# ADR 0021: Database-Native Cost Attribution

Status: Parked research — outside ADR 0025 first-launch scope

Date: 2026-08-26

이 문서는 조사 기록으로 보존한다. [ADR 0025](0025-static-non-rls-first-launch.md)의 구현·acceptance가
끝나도 COST 기능이 자동 승인되지는 않으며 active first-launch TODO나 module baseline이 아니다.

## Priority Boundary

이 문서는 `COST-01`의 선택지를 미리 검토한 초안이다. 열린 `TIME-03`이 완료되거나 사용자가 이를
명시적으로 defer하기 전에는 `COST-01`을 공식 시작하지 않고 code, source role/function, Control
schema/config와 public projection을 바꾸지 않는다. 아래 선택지를 승인하는 것은 `ENC-01`이나
`TIME-03`의 완료를 뜻하지 않으며 decision 선택만 먼저 승인해도 implementation start gate는 열리지
않는다. 열린 ENC 작업보다 먼저 구현하려면 별도의 exact global reprioritization이 필요하다.

이 제안은 하나의 module interface가 아니다. Collector/writer capability는 module interface,
monitoring/rollup tables는 persisted format, admin projection은 external API, identity/retention/status는
policy, credential/lease/cleanup은 security/lifecycle invariant다. Approval Boundary는 이 범주별 영향을
함께 명시한다.

## Context

현재 `CTRL-07A`/`CTRL-08`은 source별 resource estimate와 gateway가 성공적으로 보고한 hourly
lower-bound만 저장한다. `/admin/sources/{source_id}/usage`의 `monetary_cost`는 provider 근거가 없어
`not_configured`이고 DB-native statement counter는 아직 수집하지 않는다. `budget_profile`만 resource
tier이며 caller/user/organization chargeback dimension은 없다.

Local PostgreSQL 18.6 fixture에는 `pg_stat_statements` 1.12가 preload되어 있고
`compute_query_id=auto`, `track=top`, `track_planning=off`, `track_utility=on`, `save=on`, `max=5000`이다.
네 source reader는 `pg_monitor`/`pg_read_all_stats` membership과 reset capability는 없지만 stock
PUBLIC ACL을 통해 cluster의 connectable database들에 `CONNECT`, target database에 `TEMPORARY`,
`public.pg_stat_statements`, `public.pg_stat_statements_info`를 직접 사용하고
`pg_catalog.pg_control_system()`을 실행할 수 있다. Extension은 다른 user의 sensitive query text/queryid를
가리지만 이 ambient access도 아래 A의 최소 권한 matrix를 만족하지 않는다. 따라서 COST configure 전에
source DBA가 relevant PUBLIC/object ACL을 blast-radius 검토와 함께 harden해야 한다.

[PostgreSQL 18 pg_stat_statements documentation](https://www.postgresql.org/docs/18/pgstatstatements.html)은
row identity가 `dbid + userid + queryid + toplevel`이고, server 전체에서 bounded entry를 추적하며,
`stats_reset`/`dealloc`이 delta 해석에 필요하다고 명시한다. Query ID는 major version, machine과 catalog
OID에 걸쳐 안정적이라고 가정할 수 없고 logical replica 합산 key도 아니다. 같은 문서는 다른 user의
query text/queryid 열람에 높은 privilege가 필요함을 밝힌다.
[Predefined roles documentation](https://www.postgresql.org/docs/18/predefined-roles.html)은
`pg_monitor`가 `pg_read_all_settings`, `pg_read_all_stats`, `pg_stat_scan_tables`를 포함하고 이 role들이
privileged information을 넓게 공개할 수 있음을 경고한다. 따라서 application reader나 network LOGIN에
이 role을 직접 부여하지 않는다.

## Options

### `COST-01-A` — dedicated sanitized monitor (recommended)

1. Source owner는 source마다 전용 monitoring LOGIN을 만든다. 이 role은
   `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS`, default read-only,
   `search_path=pg_catalog`와 `CONNECTION LIMIT 2`를 사용한다. Collector connection timeout은 5초다.
   각 scan은 `READ ONLY READ COMMITTED` transaction에서 transaction-local `lock_timeout=250ms`,
   `statement_timeout=20s`, `transaction_timeout=75s`, `idle_in_transaction_session_timeout=5s`를
   설정하고 `TimeZone=UTC`, `DateStyle=ISO, YMD`까지 설정·검사한 뒤 실행한다.
   Timeout/connect/permission/SQL execution failure는 `failed/MONITOR_UNAVAILABLE`이고 partial
   baseline/delta를 쓰지 않는다. Projection/decode/value failure의 더 구체적인 exhaustive mapping은
   아래 6번을 따른다. Source-specific explicit grant는 bound target database 하나의 `CONNECT`, non-PUBLIC
   `query_man_monitor` schema `USAGE`와 아래 두 fully-qualified function `EXECUTE`뿐이고 reader/view owner,
   `pg_monitor`, `pg_read_all_stats`, `pg_signal_backend` membership, extension view 직접 `SELECT`와
   reset 실행 권한은 받지 않으며 모든 direct grant의 grant option은 false다. Monitor가 다른 role의
   member이거나 다른 role이 monitor의 member인
   steady-state `pg_auth_members` edge도 0개다. PostgreSQL builtin의 일반 PUBLIC 권한 전체를 없앤다는 뜻은 아니며,
   아래에서 열거한 pg_stat_statements 관련 object에는 ACL 경로가 무엇이든 effective direct access가
   없어야 한다. V1은 한 PostgreSQL database의 DB-native monitoring binding을
   Query Man source 하나에만 허용한다. 같은 database의 binding을 **다른 source가** 같은 reader 또는
   다른 reader로 configure하거나 disabled binding을 재사용하면 `SOURCE_MONITORING_CONFLICT`다. 같은
   source가 같은 binding에 새 monitoring revision을 만드는 것은 허용한다. 서로 다른 source lock에서
   동시에 configure하면 named unique constraint가 최종 직렬화하고 losing authority transaction 전체를
   rollback한 뒤 별도 짧은 transaction에서 409 rejection receipt만 기록한다. Physical clone처럼
   `system_identifier+dbid`가 같은 별도 endpoint도 V1에서는 같은 database로 보수적으로 취급한다. 같은
   cluster의 다른 database는 `dbid`가 다르므로 허용한다. 이 제한을 없애려면 monitor-login→reader-OID
   mapping table과 그 lifecycle을 새 persisted/lifecycle change로 승인해야 하며 V1에서 미리 만들지 않는다.
2. Source owner가 관리하는 별도 NOLOGIN function owner만 `pg_read_all_stats`, `public` schema `USAGE`,
   `public.pg_stat_statements`의 아래 21개 direct column-level `SELECT`,
   `public.pg_stat_statements_info`의 `dealloc,stats_reset` column-level `SELECT`, exact non-reset raw
   `public.pg_stat_statements(boolean)`/`public.pg_stat_statements_info()`와 필요한
   `pg_catalog.pg_control_system()` `EXECUTE`를 가진다. Relation-level `SELECT`, `query` column direct
   `SELECT`와 reset `EXECUTE`는 갖지 않는다. PostgreSQL view는 underlying raw function의 execute 권한을
   caller에게 요구하므로 이 NOLOGIN owner는 raw function을 직접 호출하면 broader row/query output을 볼 수
   있다. 따라서 안전 경계는 column ACL만이 아니라 **NOLOGIN, owner가 member인 outgoing edge는
   `pg_read_all_stats` 하나, owner를 target role로 삼는 inbound membership edge는 0개, exact wrapper
   `prosrc`**의 결합이다. Locked `search_path`와
   fully-qualified object를 쓰는 argument
   없는 두 `SECURITY DEFINER` function으로 header와 rowset을 분리한다. Exact schema는
   `query_man_monitor`이고 PUBLIC schema/function privilege는 모두 revoke한다. Collector는 search path에
   의존하지 않고 아래 이름 그대로 `SELECT * FROM` 호출한다. 두 function signature는 argument가 없고
   아래 순서/type의 exact `RETURNS TABLE`이며 overload를 만들지 않는다. Function owner는
   `NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOREPLICATION NOBYPASSRLS`이고 inherited
   membership은 `pg_read_all_stats` 하나뿐이고 그 edge의 `inherit_option=true`,
   `set_option=false`, `admin_option=false`다. 두 function은 exact `SECURITY DEFINER STABLE
   PARALLEL RESTRICTED SET search_path = pg_catalog, pg_temp`이고 모든 object를 schema-qualify한다.
   Function body는 이 database에 승인된 application reader role 하나를 source-owner가 안전한 literal로
   고정해 그 OID만 filter하며 임의 target argument나 lookup table을 받지 않는다. Source object는 한
   transaction에서 만들고 PUBLIC `EXECUTE`를 revoke한 뒤 current monitor에만 exact two function
   `EXECUTE`를 grant하고 commit한다. Monitor와 function owner는 모든
   `pg_stat_statements_reset` overload의 effective `EXECUTE`를 갖지 않는다.

   이 최소 권한은 한 가지 ACL text 모양이 아니라 **effective capability matrix**로 판정한다. Source DBA는
   Query Man을 설치하기 전에 PUBLIC/default-role/role-membership/owner 경로를 모두 합친 결과가 다음과
   같도록 관련 ACL을 harden해야 한다. PostgreSQL LOGIN은 cluster-wide이고 PUBLIC grant에는 role별 deny가
   없으므로, database set의 targeted shared revoke는 target database의 PUBLIC `TEMPORARY`와 **target 외
   모든 `pg_database.datallowconn=true` database의 PUBLIC `CONNECT`**다. Source DBA는 다른 legitimate role에
   필요한 CONNECT를 grant option 없이 다시 grant하고 이후 새 database 생성 때도 이 invariant를 먼저 적용한다. Target
   database의 PUBLIC `CONNECT`는 남기거나 revoke 뒤 monitor에 직접 grant할 수 있지만 monitor의 effective
   database privilege는 target에서 `CONNECT=true`, `CREATE|TEMP=false`, 다른 모든 connectable database에서
   `CONNECT=false`로 같아야 한다. 새 database는 반드시 `CREATE DATABASE ... ALLOW_CONNECTIONS false`로
   만든 뒤 PUBLIC CONNECT revoke와 legitimate-role grant-option-false 재grant를 완료하고 마지막에만
   `ALTER DATABASE ... ALLOW_CONNECTIONS true`로 연다. Database 생성 권한자는 이 순서를 우회하지 않으며
   detect-after-exposure를 정상 lifecycle로 사용하지 않는다. Object set은 두 extension view의 PUBLIC `SELECT`, raw statement/info
   function과 모든 reset overload 및 `pg_control_system()`의 PUBLIC `EXECUTE`다. 그 뒤
   function owner에게만 21+2 view column, raw non-reset statement/info function과 `pg_control_system()`을
   모두 grant option 없이 명시적으로 grant하고 monitor에는 wrapper 두 개만 grant option 없이 grant한다.
   Monitor는 두 view를 직접 읽거나 raw
   statement/info/reset/control function을 직접 실행할 수 없다. Function owner의 broader raw read
   capability는 NOLOGIN/no-inbound-membership으로 격리하며 reset은 owner에게도 금지한다. `public` schema
   `USAGE` 자체는 PUBLIC에 남겨도 이 relevant object 결과가 같으면 허용한다. 반대로 stock/default ACL이
   relevant object를 노출하면 configure 전에 source DBA가 그 database의 다른 사용자에 미칠 blast radius를
   검토해 명시적으로 harden해야 한다. Query Man application은 shared PUBLIC ACL을 자동 revoke하지 않고,
   안전한 결과를 만들 수 없으면 fail closed한다. `pg_catalog` builtin의 unrelated ambient PUBLIC
   `EXECUTE`는 이 relevant set에 포함하지 않는다.

   ```text
   query_man_monitor.monitor_info_v1() RETURNS TABLE (
     system_identifier text,
     postmaster_started_at timestamptz,
     server_addr inet,
     server_port integer,
     dbid oid,
     userid oid,
     server_version_num integer,
     extension_version text,
     compute_query_id text,
     track text,
     track_planning text,
     track_utility text,
     save text,
     max integer,
     stats_reset timestamptz,
     dealloc bigint
   ) -> exactly one row

   query_man_monitor.monitor_statements_v1() RETURNS TABLE (
     dbid oid,
     userid oid,
     queryid bigint,
     toplevel boolean,
     stats_since timestamptz,
     calls bigint,
     execution_time_us numeric,
     rows bigint,
     shared_blks_hit bigint,
     shared_blks_read bigint,
     shared_blks_dirtied bigint,
     shared_blks_written bigint,
     local_blks_hit bigint,
     local_blks_read bigint,
     local_blks_dirtied bigint,
     local_blks_written bigint,
     temp_blks_read bigint,
     temp_blks_written bigint,
     wal_records bigint,
     wal_fpi bigint,
     wal_bytes numeric
   ) -> zero to 5,000 rows
   ```

   두 SQL function의 canonical `prosrc` template는 아래 두 block이다. 각 block은 첫 `WITH` byte부터
   마지막 semicolon 뒤 LF까지 UTF-8 bytes가 material이다. `{{reader_sql_literal}}` 한 token만 active
   source reader username에 PostgreSQL `quote_literal(username)`을 적용한 결과로 치환한다. Reader
   username grammar는 ASCII `[A-Za-z_][A-Za-z0-9_$]{0,62}`라 다른 치환이나 encoding은 없다.
   Preflight와 매 scan은 active reader로 치환한 exact `prosrc` bytes를 비교하며 SQL whitespace나
   의미상 동치 body도 허용하지 않는다.

   ```sql
   WITH target AS (
     SELECT database_row.oid AS dbid, role_row.oid AS userid
     FROM pg_catalog.pg_database AS database_row
     CROSS JOIN pg_catalog.pg_roles AS role_row
     WHERE database_row.datname = pg_catalog.current_database()
       AND role_row.rolname = {{reader_sql_literal}}::pg_catalog.name
   ),
   extension_row AS (
     SELECT extension.extversion
     FROM pg_catalog.pg_extension AS extension
     WHERE extension.extname = 'pg_stat_statements'
       AND extension.extnamespace = 'public'::pg_catalog.regnamespace
   )
   SELECT
     control.system_identifier::pg_catalog.text,
     pg_catalog.pg_postmaster_start_time(),
     pg_catalog.inet_server_addr(),
     pg_catalog.inet_server_port(),
     target.dbid,
     target.userid,
     pg_catalog.current_setting('server_version_num')::pg_catalog.int4,
     extension_row.extversion,
     pg_catalog.current_setting('compute_query_id'),
     pg_catalog.current_setting('pg_stat_statements.track'),
     pg_catalog.current_setting('pg_stat_statements.track_planning'),
     pg_catalog.current_setting('pg_stat_statements.track_utility'),
     pg_catalog.current_setting('pg_stat_statements.save'),
     pg_catalog.current_setting('pg_stat_statements.max')::pg_catalog.int4,
     info.stats_reset,
     info.dealloc
   FROM pg_catalog.pg_control_system() AS control
   CROSS JOIN target
   CROSS JOIN extension_row
   CROSS JOIN public.pg_stat_statements_info AS info;
   ```

   ```sql
   WITH target AS (
     SELECT database_row.oid AS dbid, role_row.oid AS userid
     FROM pg_catalog.pg_database AS database_row
     CROSS JOIN pg_catalog.pg_roles AS role_row
     WHERE database_row.datname = pg_catalog.current_database()
       AND role_row.rolname = {{reader_sql_literal}}::pg_catalog.name
   )
   SELECT
     stats.dbid,
     stats.userid,
     stats.queryid,
     stats.toplevel,
     stats.stats_since,
     stats.calls,
     pg_catalog.round(stats.total_exec_time::pg_catalog.numeric * 1000),
     stats.rows,
     stats.shared_blks_hit,
     stats.shared_blks_read,
     stats.shared_blks_dirtied,
     stats.shared_blks_written,
     stats.local_blks_hit,
     stats.local_blks_read,
     stats.local_blks_dirtied,
     stats.local_blks_written,
     stats.temp_blks_read,
     stats.temp_blks_written,
     stats.wal_records,
     stats.wal_fpi,
     stats.wal_bytes
   FROM public.pg_stat_statements AS stats
   JOIN target
     ON target.dbid = stats.dbid
    AND target.userid = stats.userid;
   ```

   Template revision은 위 placeholder bytes의 SHA-256이며 info는
   `sha256:093d8c24cf77b9a93e2e790fa01f2d190965be3a3864beb3dbdbbf5b7a19fa20`, statement는
   `sha256:728bbb12bec272b7a6bc31320fcd11ce55513281a06e076458ce1928097707f4`로 아래 canonical
   definition에 고정한다. Body
   bytes 외에도 `LANGUAGE SQL`, `prokind='f'`, `proretset=true`, input argument 없음, exact TABLE output
   name/mode/OID/order, `proleakproof=false`, `proisstrict=false`, exact function/schema owner, overload 부재,
   `SECURITY DEFINER`, `STABLE`, `PARALLEL RESTRICTED`, exact
   `proconfig=["search_path=pg_catalog, pg_temp"]`와 extension object/namespace/version을 attest한다.
   따라서 same-OID body 변경도 OID/signature만 믿지 않고 fail-closed한다.

   PostgreSQL output parameter에 `NOT NULL` constraint를 선언할 수 없으므로 projection rules가 모든 cell의
   non-null을 요구하고 collector가 cursor-description SQL OID, column order와 null을
   검사한다. `system_identifier`는 unsigned decimal ASCII text, timestamp는 UTC-aware value다.
   `execution_time_us`는 SQL `numeric`의 rounded integer로 받아 collector가 `0..2^63-1`인지 확인한 뒤
   application integer로 바꾼다. SQL `bigint` cast 전에 overflow시키지 않아 `COUNTER_OVERFLOW`를 구분한다.
   `wal_bytes`도 SQL `numeric`으로 받고 collector가 finite/nonnegative/integral 및 `0..10^38-1`을 검사한
   뒤 exponent 없는 decimal string으로 canonicalize한다. PostgreSQL function result가 typmod를 보존한다고
   가정하지 않는다. Non-integral/non-finite는 `OBSERVATION_INCOMPLETE`, 범위 밖은 `COUNTER_OVERFLOW`이며
   baseline으로 쓰지 않는다. Function은 exact
   target reader `dbid/userid`만 선택한다. Representative query text, parameter,
   database/role name과 임의 target argument는 반환하지 않는다. Empty statement rowset은 정상이다.
   `execution_time_us`는 nonnegative `round(total_exec_time::numeric * 1000)`이다. Raw/delta/rollup의
   `calls|execution_time_us|rows|block counters|wal_records|wal_fpi`는 각각
   `0..9223372036854775807`, `wal_bytes`는 `0..10^38-1`이다. `system_identifier`는
   `0..18446744073709551615`의 leading-zero 없는 decimal ASCII(`0` 제외), `dbid/userid`는
   `0..4294967295`, `server_port`는 `1..65535`다. `server_addr`는 psycopg가 반환한 IPv4/IPv6를
   Python `ipaddress.ip_address` compressed lowercase text로 canonicalize한다. Decode, delta 또는 atomic rollup add가
   범위를 벗어나면 partial write 없이 scan 전체를 `COUNTER_OVERFLOW`로 discard한다.
   Raw system identifier, server address/port와 database/reader OID는 collector connection 안에서만
   사용하고 저장/log/public projection하지 않는다. 이 값으로 만든 bounded digest만 저장한다.

   Preflight와 매 scan은 active source generation의 application reader username을 parameter `$1`로만 넣어
   같은 monitor connection에서 다음 exact binding probe를 실행한다. Username 원문과 database name은
   source config와 in-memory 비교에만 쓰고 sample/log/public projection에 저장하지 않는다.

   ```sql
   SELECT (
            SELECT database_row.oid
            FROM pg_catalog.pg_database AS database_row
            WHERE database_row.datname = pg_catalog.current_database()
          ) AS dbid,
          (
            SELECT role_row.oid
            FROM pg_catalog.pg_roles AS role_row
            WHERE role_row.rolname = $1::pg_catalog.name
          ) AS userid;
   ```

   Exactly one row와 두 non-null OID를 요구한다. Info-before와 info-after의 `dbid/userid`는 이 binding
   row와 같고, 모든 statement row의 `dbid/userid`는 두 info와 같아야 한다. Duplicate
   `(queryid,toplevel)` row도 `OBSERVATION_INCOMPLETE`다. Info가 bound source와 다르면
   `TARGET_MISMATCH`, statement row 하나라도 다르면 `OBSERVATION_INCOMPLETE`로 전체 scan을 fail closed하고
   baseline/delta로 쓰지 않는다. 이 probe는 expected username을 target argument로 source-owner function에
   넘기지 않으며 broad statistics privilege도 요구하지 않는다.
3. Collector는 info field의 exact canonical UTF-8 encoding을 SHA-256해 `sha256:<64-lower-hex>`
   `database_binding_id`와 `target_instance_id`를 만든다.

   ```text
   database_binding_id material:
   pg18-db\n{system_identifier}\n{dbid}

   target_instance_id material:
   pg18\n{system_identifier}\n{postmaster_started_at YYYY-MM-DDTHH:MM:SS.ffffff+00:00}\n{server_addr canonical inet}\n{server_port}\n{dbid}\n{userid}
   ```

   V1은 non-load-balanced TCP endpoint와 non-null `server_addr`만 지원한다. DB/role recreation,
   failover 또는 postmaster restart는 target ID를 바꿔 반드시 re-baseline한다. `save=on`으로 counter가
   restart 뒤 보존돼도 이전 target과 subtract하지 않는다. Stable database binding은 postmaster start,
   address와 reader를 제외하고 system identifier+database OID만 포함하며 disabled state에도 보존한다.
4. V1 지원 baseline은 PostgreSQL major 18, `pg_stat_statements` 1.12, preload,
   `compute_query_id=auto|on`, `track=top`, `track_planning=off`, `track_utility=on`, `save=on`,
   `max=5000`이다. Major, projection 또는 allowed support-policy 변경은 새 definition revision을 요구한다.
   Source가 이미 허용된 값 안에서 `compute_query_id=auto↔on`처럼 실제 setting을 바꾸거나 restart/failover로
   target instance가 바뀌면 global definition은 유지하되 새 observation identity와 baseline을 요구한다.
   `track_io_timing` 값과 무관하게 timing column은 V1 projection에 넣지 않는다.
   `definition_revision`은 아래 valid JSON value를 parse한 뒤 `ensure_ascii=true`, key 정렬,
   separators `,`/`:`, whitespace 0으로 UTF-8 encode해 SHA-256한 `sha256:<64 lower hex>`다. Array order는
   material이고 object source order는 material이 아니다.

   ```json
   {
     "attribution": {"bucket_start": "date_trunc('hour',accepted_at,'UTC')", "clock": "control_db_clock_timestamp", "delta": "whole", "prorate": false},
     "binding": {"database_binding": "one_source_per_system_identifier_and_database_oid", "database_oid": "current_database_catalog_oid", "info_must_match_bound_source": true, "reader_oid": "exact_pg_roles_rolname(active_source_reader_username_parameter)", "statement_rows_must_match_info": true},
     "bounds": {"accepted_samples_per_hour": 12, "collector_concurrency_per_replica": 4, "counter_max": 9223372036854775807, "freshness_seconds": 900, "lease_seconds": 120, "rollup_rows_per_source": 1000, "statement_rows": 5000, "wal_bytes_max": "99999999999999999999999999999999999999", "window_days": 31},
     "cadence_seconds": 300,
     "delta": {"complete": "subtract_previous", "counter_overflow": "discard_invalidate_wait_complete", "counter_regression": "discard_current_complete_rebaseline", "duplicate_identity": "discard_invalidate_wait_complete", "empty_rowset": "valid_complete_sample", "entry_missing": "discard_current_complete_rebaseline", "explicit_zero": "persist_rollup_row", "incomplete_scan": "discard_invalidate_wait_complete", "mid_scan_stats_reset_or_dealloc_change": "discard_invalidate_wait_complete", "mid_scan_target_or_settings_change": "fail_invalidate_wait_complete", "new_entry": "full_cumulative_delta", "new_target_or_allowed_settings": "baseline_new_observation_identity", "prior_dealloc_increase": "discard_current_complete_rebaseline", "prior_global_reset": "discard_current_complete_rebaseline", "row_limit": "discard_invalidate_wait_complete", "selective_reset": "discard_current_complete_rebaseline", "stale_control_fence": "no_write"},
     "failure_precedence": [
       ["bound_database_or_reader_or_mid_scan_target", "failed_TARGET_MISMATCH"],
       ["monitor_or_function_owner_privilege_or_projection_security_drift", "failed_MONITOR_PRIVILEGE_MISMATCH"],
       ["extension_preload_version_or_projection_missing", "failed_EXTENSION_UNAVAILABLE"],
       ["unsupported_or_mid_scan_unstable_setting", "failed_SETTINGS_MISMATCH"],
       ["statement_rows_over_5000", "discarded_ROW_LIMIT_EXCEEDED"],
       ["mid_scan_reset_or_dealloc_duplicate_identity_shape_type_null_info_cardinality_decode_nonfinite_nonintegral_statement_binding_or_incomplete", "discarded_OBSERVATION_INCOMPLETE"],
       ["finite_integral_counter_negative_or_over_max", "discarded_COUNTER_OVERFLOW"],
       ["permission_transport_timeout_or_other_sql_execution", "failed_MONITOR_UNAVAILABLE"]
     ],
     "info": {
       "fields": [
         ["system_identifier", "text", "unsigned_decimal_ascii", false], ["postmaster_started_at", "timestamptz", "utc_timestamp", false],
         ["server_addr", "inet", "canonical_inet", false], ["server_port", "integer", "int32", false],
         ["dbid", "oid", "oid_integer", false], ["userid", "oid", "oid_integer", false],
         ["server_version_num", "integer", "int32", false], ["extension_version", "text", "ascii_text", false],
         ["compute_query_id", "text", "ascii_text", false], ["track", "text", "ascii_text", false],
         ["track_planning", "text", "ascii_text", false], ["track_utility", "text", "ascii_text", false],
         ["save", "text", "ascii_text", false], ["max", "integer", "int32", false],
         ["stats_reset", "timestamptz", "utc_timestamp", false], ["dealloc", "bigint", "int64", false]
       ],
       "protocol": "monitor_info_v1"
     },
     "observation_identity": {"settings_fields": ["server_version_num", "extension_version", "compute_query_id", "track", "track_planning", "track_utility", "save", "max"], "target_field": "target_instance_id"},
     "projection": {"acl_profile": "pgss_v1_21_2_owner_raw_nonreset_readstats_inbound0_monitor_wrappers_nomembership_targetdb_only_notemp_public_hardened", "body_template_encoding": "utf8_exact_lf_terminal_newline", "extension_namespace": "public", "function_language": "sql", "function_security": "security_definer_stable_parallel_restricted_locked_search_path", "info_body_template_revision": "sha256:093d8c24cf77b9a93e2e790fa01f2d190965be3a3864beb3dbdbbf5b7a19fa20", "info_function": "query_man_monitor.monitor_info_v1()", "overloads": false, "reader_literal_placeholder": "{{reader_sql_literal}}", "schema": "query_man_monitor", "statement_body_template_revision": "sha256:728bbb12bec272b7a6bc31320fcd11ce55513281a06e076458ce1928097707f4", "statement_function": "query_man_monitor.monitor_statements_v1()"},
     "session": {"date_style": "ISO, YMD", "time_zone": "UTC"},
     "statement": {
       "fields": [
         ["dbid", "oid", "oid_integer", false], ["userid", "oid", "oid_integer", false],
         ["queryid", "bigint", "int64", false], ["toplevel", "boolean", "boolean", false],
         ["stats_since", "timestamptz", "utc_timestamp", false], ["calls", "bigint", "int64", false],
         ["execution_time_us", "numeric", "checked_int64_integer", false], ["rows", "bigint", "int64", false],
         ["shared_blks_hit", "bigint", "int64", false], ["shared_blks_read", "bigint", "int64", false],
         ["shared_blks_dirtied", "bigint", "int64", false], ["shared_blks_written", "bigint", "int64", false],
         ["local_blks_hit", "bigint", "int64", false], ["local_blks_read", "bigint", "int64", false],
         ["local_blks_dirtied", "bigint", "int64", false], ["local_blks_written", "bigint", "int64", false],
         ["temp_blks_read", "bigint", "int64", false], ["temp_blks_written", "bigint", "int64", false],
         ["wal_records", "bigint", "int64", false], ["wal_fpi", "bigint", "int64", false],
         ["wal_bytes", "numeric", "integral_decimal_string_max_38_digits", false]
       ],
       "protocol": "monitor_statements_v1"
     },
     "support": {"compute_query_id": ["auto", "on"], "extension": "pg_stat_statements", "extension_version": "1.12", "max": 5000, "postgres_major": 18, "save": "on", "track": "top", "track_planning": "off", "track_utility": "on"},
     "target_instance": {"database_binding_digest": "sha256_prefixed_lower_hex", "database_binding_template": "pg18-db\n{system_identifier}\n{dbid}", "digest": "sha256_prefixed_lower_hex", "template": "pg18\n{system_identifier}\n{postmaster_started_at_utc_microseconds}\n{server_addr_canonical_inet}\n{server_port}\n{dbid}\n{userid}"},
     "timeouts": {"connect_seconds": 5, "idle_in_transaction_ms": 5000, "lock_ms": 250, "statement_ms": 20000, "transaction_ms": 75000},
     "transaction": "read_only_read_committed",
     "transforms": {"execution_time_us": "round(total_exec_time::numeric*1000)", "wal_bytes": "numeric38_decimal_string"},
     "version": 1
   }
   ```

   V1 golden은 아래 canonical material을 다시 계산한 값이며 documentation test가 이를 검증한다.
   `sha256:f621a1815ad806d23d23f00fe0f0faa030c4083fbf49d184a4b8dd3d7e379160`

   Field addition/removal/order/type, projection name/signature/body template/effective ACL profile, deterministic session setting,
   rounding, support settings, target digest algorithm, reset/delta rule, cadence/freshness, attribution or cap
   change creates a new
   definition revision. Source-specific target ID, credential, generation, profile와 metadata revision 값 자체는
   이 global definition hash에 넣지 않는다.
5. Internal base identity는
   `target_instance_id + dbid + userid + queryid + toplevel`; sample identity에는 row별 `stats_since`,
   global `stats_reset`, 위 JSON의 fixed-order supported-settings tuple과 collector definition revision도
   포함한다. Query ID를 gateway fingerprint, application `query_id`, caller 또는 tenant와 join하거나
   외부에 공개하지 않는다. Persisted current observation identity는 다음 newline-delimited ASCII material의
   `sha256:<64-lower-hex>`다. 모든 variable field는 앞에서 제한한 identifier/integer/enum/hash라 newline을
   포함할 수 없다.

   ```text
   dbnative-observation-v1
   {monitoring_revision}
   {database_binding_id}
   {target_instance_id}
   {server_version_num}
   {extension_version}
   {compute_query_id}
   {track}
   {track_planning}
   {track_utility}
   {save}
   {max}
   {source_generation}
   {source_state_version}
   {budget_profile}
   {metadata_revision}
   {definition_revision}
   ```
   `observation_started_at`은 current config 아래 이 observation identity를 처음 stable하게 읽은 fenced
   Control DB attempt time이다. Config/target/settings/metadata/profile transition 뒤 digest text가 과거 값과
   우연히 같아져도 새 start time을 쓴다. Same-identity reset/deallocation/incomplete scan은 이 start time을
   바꾸지 않으며 별도 continuous-coverage epoch를 뜻하지 않는다.
6. 한 collector가 5분마다 `binding/info-before → bounded statement rows → binding/info-after`를 읽는다.
   Empty rowset도 complete sample이다. Exactly one-row binding/info, identical before/after target/settings/
   `stats_reset`/`dealloc`, exact row shape/type/null/bounds와 모든 row의 binding 일치를 모두 만족해야 stable
   complete sample이다. Cursor는 5,001번째 row까지 fetch해 cap 초과를 검출한다. Input row order는
   의미가 없으며 duplicate 검사를 통과한 rowset을 signed numeric `queryid ASC, toplevel false-before-true`로
   정규화한 뒤 baseline/delta를 계산한다.

   Stable complete sample의 `target_instance_id` 또는 allowed supported-settings tuple이 이전 것과 다르면
   새 observation identity의 첫 `baseline/BASELINE_REQUIRED`로 commit하고 old identity와 subtract하지 않는다.
   이는 정상 restart/failover와 허용된 `compute_query_id=auto↔on` 전환이며 discarded/failed가 아니다.
   같은 observation identity에서 global `stats_reset` 변경, `dealloc` 증가, row `stats_since` 변경,
   previous identity 누락 또는 counter regression을 발견하면 delta는 `discarded`로 기록하되 current complete
   rowset을 같은 fenced transaction에서 새 baseline으로 교체한다. `dealloc`은 server-global signal이므로
   증가는 target row eviction의 증명이 아니라 unrelated database/user churn일 수도 있는 conservative
   `SERVER_DEALLOCATION_DETECTED` discard다.

   반대로 binding/info가 scan 중 바뀌거나 row cap 초과, type/null/decode/row-binding 불일치,
   counter overflow 또는 incomplete cursor이면 현재 rowset은 baseline 자격이 없다. Fenced commit은 bounded
   `failed|discarded` attempt를 기록하고 current operational baseline을 invalidate/remove하며, partial row나
   0 delta를 쓰지 않는다. 다음 stable complete scan만 `baseline/BASELINE_REQUIRED`로 새 baseline을 만든다.
   FK-safe write order는 old statement baseline delete → usage parent의 identity/config/attempt update →
   complete current baseline rows insert다. Invalid scan은 child delete 뒤 parent baseline fields를 null로
   만들고, accepted/rebaseline은 complete current rowset으로 baseline을 전부 교체한다.
   Mid-scan target/allowed-settings transition, bound-source mismatch 또는 unsupported setting은
   `failed/TARGET_MISMATCH|SETTINGS_MISMATCH`, mid-scan
   reset/dealloc·shape/null/decode/row-binding/incomplete는 `discarded/OBSERVATION_INCOMPLETE`, row cap과
   overflow는 각각 `discarded/ROW_LIMIT_EXCEEDED|COUNTER_OVERFLOW`다.

   Runtime acquisition/validation failure는 canonical JSON의 `failure_precedence` array 순서로 판정한다.
   한 scan에 여러 조건이 있어도 첫 match 하나만 public reason이 되므로 exhaustive하고 deterministic하다.
   Stable complete sample의 reset/deallocation/entry/regression delta 판정은 이 validation을 모두 통과한 뒤에만
   수행한다. Preflight는 내부 분류의 원문 detail을 공개하지 않고 모두 앞서 정의한
   `SOURCE_VALIDATION_FAILED` surface로 축약한다.

   1. Info header의 database/application-reader OID 불일치 또는 mid-scan target instance 변경:
      `failed/TARGET_MISMATCH`.
   2. Monitoring/function-owner role attribute·membership, schema/function ACL 또는 exact function security
      property가 drift함: `failed/MONITOR_PRIVILEGE_MISMATCH`.
   3. Extension/preload/version 또는 required schema/function 부재:
      `failed/EXTENSION_UNAVAILABLE`.
   4. Unsupported setting 또는 info-before/after의 allowed setting 불안정:
      `failed/SETTINGS_MISMATCH`.
   5. Statement row가 5,000개를 초과함: `discarded/ROW_LIMIT_EXCEEDED`.
   6. Mid-scan reset/dealloc, duplicate identity, column/order/SQL OID/null/info cardinality/decode, non-finite·non-integral numeric,
      statement-row binding 또는 incomplete cursor 위반: `discarded/OBSERVATION_INCOMPLETE`.
      따라서 finite `-0.5`는 여기서 끝나며 negative counter 규칙과 겹치지 않는다.
   7. Decode된 counter가 finite integral이지만 negative이거나 허용 상한 초과:
      `discarded/COUNTER_OVERFLOW`.
   8. 위 condition이 아닌 connection, permission, transport, timeout 또는 projection SQL execution 오류:
      `failed/MONITOR_UNAVAILABLE`.

   Control의 source generation/state, monitoring revision, profile/metadata/definition 또는 lease fence가 scan
   중 바뀐 경우는 위 두 source-result 규칙과 다르게 stale collector의 sample/baseline/attempt/rollup 전체를
   no-write한다. 완전한 baseline 뒤 새 base identity가 나타나면 그 cumulative counter 전체가 해당
   interval의 observed delta다. Last accepted complete sample freshness는 15분이다.

   Collector는 managed mode에서만 existing Runtime background lifecycle 안에 존재한다. 각 replica는 Control
   DB clock의 `next_attempt_at`을 5초마다 확인하고 due source를 `(next_attempt_at, source_id COLLATE "C")`
   순서로 claim하며 process당 source scan은 최대 4개다. Configure 직후는 immediately due이고 committed
   `baseline|accepted|discarded|failed` attempt 모두 `next_attempt_at=attempted_at+300 seconds`로 옮긴다.
   Initial configure authority transaction은 하나의 Control DB clock `t`로 lease epoch 0, nullable lease
   owner/incarnation/acquired/until 전부 null, `next_attempt_at=t`와 current
   source/monitor/profile/metadata/definition tuple을 채우고, identity/baseline/attempt/success만 전부 null인
   usage row를 seed한다. Monitoring
   configure/rotate/rollback은 epoch를 1 증가시켜 active lease를 clear하고 old statement baseline을 먼저
   지운 뒤 current config tuple을 쓰며 identity/attempt/success를 clear하고 즉시 due로 만든다. Disable도
   epoch/state version을 올리고 lease/baseline/observation을 clear하지만 revision/binding pointer는 보존한다.
   Active source state/profile/metadata drift는 claim transaction이 current tuple과 monitoring binding을
   다시 읽어 matching generation이면 같은 reset을 수행하고, generation binding mismatch면 source I/O 없이
   `TARGET_MISMATCH` status를 유지한다.
   Catch-up scan과 exponential backoff는 없다. Source당 global lease 하나와 replica당 source task 하나만
   허용해 collector 1 connection, concurrent admin preflight 최대 1 connection으로 monitor LOGIN의 limit 2를
   지킨다. Shutdown은 새 claim을 멈추고 active cursor cancel→transaction rollback→connection close를 기존
   runtime grace 안에서 수행한다. 취소되거나 fenced된 owner는 attempt도 쓰지 않고 lease expiry에 맡긴다.
   Collector 장애는 query readiness와 data plane result를 바꾸지 않는다.
7. Delta는 target reader role의 `pg_stat_statements` aggregate이지 Query Man business query별 측정이나
   CPU counter가 아니다. Metadata/session-policy/EXPLAIN/DECLARE/FETCH와 같은 auxiliary statement 및
   같은 reader credential을 다른 client가 쓴 workload도 포함한다. Failed/reset/evicted interval은
   누락되고 external role reuse는 Query Man usage보다 크게 보이게 할 수 있으므로 gateway query count와
   일치하지 않는다. Public `lower_bound=true`는 retained pgss counter에 누락 구간을 합성하지 않았다는
   뜻일 뿐 Query Man-only lower bound나 청구 근거가 아니다. Min/max/mean, plan time, query text,
   caller/tenant, SQL/parameter, application query ID와 fingerprint는 저장하지 않는다.
8. Baseline과 sample 사이 active source generation, state version, monitoring revision,
   `budget_profile`, metadata revision과 collector definition revision이 모두 같을 때만 다음 UTC-hour
   key에 귀속한다.

   ```text
   source_id + budget_profile + metadata_revision + definition_revision + bucket_start
   ```

   Fenced commit transaction이 Control DB `clock_timestamp()`을 한 번 읽어 `accepted_at`과
   `observed_at`으로 사용하고, PostgreSQL 18의 timestamptz 반환식
   `bucket_start=date_trunc('hour', accepted_at, 'UTC')`로 정한다.
   5분 interval이 hour boundary를 넘어도 delta 전체를 이 accepted hour에 넣고 시간 비례 분할하지 않는다.
   따라서 bucket은 실행 시각별 정확한 분배가 아니라 accepted sample attribution이고 최대 한 cadence만큼
   뒤로 이동할 수 있다. Accepted delta의 모든 counter가 0이어도 `accepted_samples=1`인 explicit-zero
   rollup row를 저장하고 같은 hour의 accepted commit마다 최대 12까지 더한다. Missing hour는 row 부재이며
   observed zero와 같지 않다. V1은 source마다 exact stats target 하나만 허용하고 replica를 합산하지 않는다.
   Collector replica ID는 lease provenance일 뿐 usage dimension이 아니다. Application은 bigint/numeric
   counter와 accepted-sample 합계를 DML 전에 bound-check한다. PostgreSQL overflow로 transaction이 abort된
   뒤 같은 transaction에서 discard attempt를 쓰려 하지 않는다.
9. Lease는 source별 Control DB row의 monotonic epoch, owner replica/incarnation과 DB-clock
   `lease_until=now+120 seconds`다. Collector는 짧은 Control transaction에서 acquire한 뒤 connection과
   lock을 놓고 source scan을 수행한다. Claim은 current `runtime_replicas` row와 incarnation을 확인하고
   epoch를 증가시킨 뒤 owner/incarnation/acquired/until만 채우며 `next_attempt_at`은 옮기지 않는다. 새 짧은
   transaction이 DB clock `< lease_until`, current owner/epoch/incarnation, runtime replica의 current
   incarnation과 unchanged source/config/revision을 확인할 때만 결과를 commit한다. 정상 commit은 lease
   owner/incarnation/acquired/until을 다시 null로 clear하되 epoch은 보존한다. Source I/O 중 Control connection이나
   advisory lock을 잡지 않고 lease를 갱신하지 않는다. Expiry/fencing/config change는 stale owner가
   last-attempt까지 쓰지 못하게 sample/baseline/attempt/rollup 전체를 commit하지 않는다. Crash/cancel도
   write 없이 active lease를 남겨 expiry takeover하게 한다. Public state는
   이전 fenced commit 또는 새 current identity의 pending 계산만 사용하고 다음 replica가 새 epoch를 acquire한다.
10. Operator-visible/input window는 기존과 같은 inclusive 31일이고 age만으로 row를 DELETE하지 않는다.
    Latest statement baseline은 source당 5,000행이다. Rollup write는 deterministic oldest ordering으로
    source당 최신 1,000행만 남기되, read에서 persisted cardinality가 1,000을 넘거나 malformed하면
    truncate하지 않고 503 fail-closed한다. Baseline/cursor는 operational state이지 billing history가 아니다.
11. Monitoring authority는 source generation과 분리한 immutable revision + active pointer를 사용한다.
    `configure|rotate_credential|disable|rollback`은 source advisory lock 아래 active source
    generation/state version과 monitoring state version을 CAS하고 기존 admin idempotency key, keyed request
    hash, actor/reason과 terminal receipt/history를 사용한다. Revision은 exact source generation과 target
    definition에 binding된다. Source publish/rollback은 monitoring pointer를 자동 변경하지 않으며 binding이
    current source generation과 다르면 collection은 `TARGET_MISMATCH`로 멈춘다. Operator가 matching
    historical monitoring revision을 별도 rollback해야 한다.

    Exact operator-only application surface는 다음과 같다. `GET`은 credential, username, raw target와
    target ID를 반환하지 않는다.

    ```text
    GET    /admin/sources/{source_id}/database-native-monitoring
      -> {source_id, status=not_configured|enabled|disabled,
          active_monitoring_revision: null|positive-int,
          monitoring_state_version: nonnegative-int,
          current_source_generation: positive-int,
          bound_source_generation: null|positive-int,
          definition_revision: null|sha256-revision}

    PUT    /admin/sources/{source_id}/database-native-monitoring
      body={username, credential}                         -> monitor_configured
    POST   /admin/sources/{source_id}/database-native-monitoring/credential
      body={credential}                                   -> monitor_credential_rotated
    DELETE /admin/sources/{source_id}/database-native-monitoring
      empty body                                          -> monitor_disabled
    POST   /admin/sources/{source_id}/database-native-monitoring/rollback/{monitoring_revision}
      empty body                                          -> monitor_rolled_back
    ```

    Mutations require the current `Idempotency-Key`, `X-Query-Man-Reason`,
    `X-Expected-Generation`, `X-Expected-State-Version` headers plus one exact
    `X-Expected-Monitoring-State-Version`; metadata-revision header와 query parameter는 금지한다. Initial
    configure는 monitoring version 0, 이후 mutation은 current positive version을 요구한다. Monitoring
    revision은 source-local monotonic positive integer이며 `(source_id, monitoring_revision)`으로 식별하고
    재사용하지 않는다. Path revision과 state version은 `0..CONTROL_SEQUENCE_MAX`; rollback path는
    positive다. JSON은 extra/duplicate/non-finite를
    거부하고 existing 1 MiB body cap을 사용한다. `username`은 1~63자의 ASCII PostgreSQL identifier
    (`[a-z_][a-z0-9_]{0,62}`), credential은 1~2,048자의 nonempty secret string이다. Plaintext credential은
    same-key/different-secret을 구분하는 in-memory canonical envelope의 keyed-HMAC input에만 포함하고,
    저장되는 request hash는 HMAC뿐이다. Plaintext를 response/receipt/history/log에 넣지 않는다.

    Configure는 active source endpoint/database/TLS를 재사용하고 username/credential만 받아 새 immutable
    revision을 만들며 enabled로 전환한다. Rotate는 target/username/definition/source binding을 유지하고
    credential만 바꾼 새 revision을 만들되 current enabled/disabled 상태를 유지한다. Disable은
    active pointer를 삭제하지 않고 `enabled=false`와 state version만 append해 rollback 가능성을 보존한다.
    Rollback은 current source generation과 exact definition에 binding된 historical revision만 선택하고
    enabled로 전환한다. 모든 새 active revision은 이전 baseline/success와 subtract하지 않고 baseline부터
    다시 시작한다.

    Configure/rotate는 authority commit 전에 다음 bounded preflight를 수행한다. 먼저 짧은 Control read로
    source generation/state와 monitoring state version snapshot을 얻고 connection을 닫는다. Candidate
    credential로 source에 연결해 V1 transaction/session setting, 두 fully-qualified projection의
    permission/shape/type/null/bounds, exact binding probe와 info/statement `dbid/userid` 일치를 확인하고
    membership/direct-view/reset privilege가 없음을 privilege introspection으로 검사한다. Reset function을
    실제 실행하지 않는다. 이 security introspection은 configure/rotate preflight와 매 collector scan에서
    projection 호출 전에 반복한다. Exact pass 조건은 다음과 같다.

    - monitor role은 앞의 LOGIN attribute/`CONNECTION LIMIT 2`/role settings가 정확하고 monitor가 member인
      `pg_auth_members.member=monitor_oid`와 monitor를 target으로 하는 `roleid=monitor_oid` row가 모두 0개라
      `pg_monitor|pg_read_all_stats|pg_signal_backend`의 direct/indirect member가 아니며 database
      target database `CONNECT` 외 `CREATE|TEMP`와 monitor schema `CREATE`가 없다. 같은 target connection의
      `pg_database`에서 `datallowconn=true AND oid<>current_database_oid`인 row 전체를 name을 log하지 않는
      one-row aggregate privilege probe로 검사했을 때 monitor의 `has_database_privilege(...,'CONNECT')` true
      count는 0이어야 한다. Target CONNECT가 direct ACL item이면 grant option은 false이고 monitor는 target
      database owner가 아니다. `public` schema `USAGE`의 raw ACL shape와
      무관하게 extension view direct `SELECT`, underlying statement/info function, 모든
      `pg_stat_statements_reset` overload와 `pg_control_system()`의 effective `EXECUTE`가 없다.
    - `query_man_monitor` schema owner는 expected NOLOGIN function owner이고 PUBLIC은 schema
      `USAGE|CREATE` 및 두 function `EXECUTE`가 없다. Monitor는 schema `USAGE`와 exact two no-argument
      functions `EXECUTE`만 grant option 없이 명시적으로 가진다. Owner와 current monitor 외 unexpected grantee가 없고
      rotate/configure로 monitor username이 바뀌면 이전 monitor의 schema/function ACL을 먼저 제거한다.
    - Function owner role attribute는 앞의 exact set이다. Outgoing membership은
      `(roleid=pg_read_all_stats_oid, member=owner_oid, inherit_option=true, set_option=false,
      admin_option=false)` 한 행이고, owner를 target role로 삼는 inbound `roleid=owner_oid` 행은 0개다. Source-owner가
      부여하는 relevant direct ACL은 `public` schema `USAGE`, pgss statement 21개/info 2개 column-level
      `SELECT`, raw non-reset statement/info function과 `pg_catalog.pg_control_system()` `EXECUTE`다.
      Public object의 이 direct grant는 모두 grant option false다.
      Relation-level/`query` column direct `SELECT`, 다른 extension column과 reset `EXECUTE`는 없다. Raw
      statement function의 broader output capability는 이 trusted NOLOGIN owner에만 의도적으로 남는다.
      두 wrapper function은 exact extension namespace/object를
      참조하고 owner, `prolang=sql`, `prokind=f`, `proretset=true`, `prosecdef=true`,
      `proleakproof=false`, `proisstrict=false`, volatility `STABLE`, parallel `RESTRICTED`, exact
      `proconfig`, argument/result name/mode/OID/order와 overload 부재가 일치한다. Active reader literal을
      치환한 canonical `prosrc` bytes도 두 template와 같아야 하며 info/binding/statement OID 비교를
      추가로 수행한다. Function body/SQL text를 log/public projection에 내보내지 않는다.

    Query Man은 PostgreSQL/extension namespace의 ambient PUBLIC privilege를 임의로 재정의하지 않는다.
    Source DBA가 relevant ACL closure를 위 exact effective capability matrix에 맞추지 않았거나 그 결과가
    하나라도 drift하면 preflight는 `SOURCE_VALIDATION_FAILED`, running scan은
    `failed/MONITOR_PRIVILEGE_MISMATCH`다. Source I/O 동안 Control connection/lock은 0개다.
    그 뒤 source advisory lock의 새 짧은 transaction에서 처음 snapshot과 모든 CAS를 다시 검사해 revision을
    commit한다. 중간 state change는 409 conflict이고 credential revision을 만들지 않는다. Preflight 실패는
    raw database detail 없이 existing `SOURCE_VALIDATION_FAILED` 400 immutable rejection receipt이며, Control
    dependency failure는 503이고 terminal receipt가 아니다. 성공 preflight와 authority commit 사이 source
    외부 상태가 다시 바뀔 수 있으므로 첫 collector scan도 같은 검사를 반복하고 실패하면 bounded
    canonical `failure_precedence`의 first matching outcome/reason으로 fail closed한다.

    Exact state transition은 다음과 같다. Same idempotency key/same canonical request만 기존 terminal
    receipt를 replay한다. 아래에서 허용되지 않은 transition을 새 key로 요청하면
    `SOURCE_MONITORING_CONFLICT`이고 state/revision을 만들지 않는다.

    | Current state | Operation | Result |
    |---|---|---|
    | not_configured/version 0 | configure | revision 1, enabled, state version 1 |
    | enabled | configure | next revision, enabled, state version +1; same payload/new key도 새 intended revision |
    | disabled | configure | next revision, enabled, state version +1 |
    | enabled | rotate | next revision with same target/username/binding, enabled, state version +1 |
    | disabled | rotate | next revision with same target/username/binding, disabled, state version +1 |
    | not_configured | rotate | conflict |
    | enabled | disable | pointer retained, disabled, state version +1; no new revision |
    | not_configured or disabled | disable | conflict |
    | enabled | rollback to different valid revision | selected revision, enabled, state version +1 |
    | enabled | rollback to current revision | conflict |
    | disabled | rollback to any valid revision including current pointer | selected revision, enabled, state version +1 |

    Rollback 대상이 없으면 `MONITORING_REVISION_NOT_FOUND`; 존재하지만 current source generation이나 exact
    definition에 binding되지 않으면 `SOURCE_MONITORING_CONFLICT`다. Successful mutation마다 monitoring
    state version은 정확히 한 번 증가하며 disable/rollback은 monitoring revision sequence를 소비하지 않는다.

    Success는 HTTP 200의 existing terminal mutation receipt이고 `result`는 exact
    `{status, source_id, generation, state_version, monitoring_revision,
    monitoring_state_version}`다. Status는 위 네 operation result literal 중 하나다. Existing
    `INVALID_REQUEST|SOURCE_VALIDATION_FAILED` 400, `SOURCE_NOT_FOUND|MONITORING_REVISION_NOT_FOUND` 404,
    `SOURCE_GENERATION_CONFLICT|SOURCE_MONITORING_CONFLICT|MUTATION_IDEMPOTENCY_CONFLICT` 409와
    `SOURCE_CONTROL_UNAVAILABLE` 503만 쓴다. Deterministic validation/conflict는 기존 immutable rejection
    receipt/replay 의미를 따른다. Exact new public errors는
    `MONITORING_REVISION_NOT_FOUND` 404/`"The requested monitoring revision was not found."`와
    `SOURCE_MONITORING_CONFLICT` 409/`"The source monitoring state changed; retry with current state."`다.
    Unknown source와 missing monitoring revision 404, auth와 dependency 503은 terminal receipt를 만들지
    않는다. 기존 constraint와 같이 400/409 rejection만 immutable receipt이고 request HMAC에는 expected
    monitoring state version까지 포함한다.

    Monitoring credential은 existing 32-byte source encryption key와 AES-256-GCM/12-byte random nonce를
    재사용하되 AAD를 exact ASCII
    `query-man/source/{source_id}/monitoring-revision/{monitoring_revision}`로 분리한다. Plaintext와 key는
    response/log/receipt에 들어가지 않는다. 과거 migration을 바꾸지 않는 additive Control migration은
    다음 책임을 분리한다.

    ```text
    source_db_monitoring_revisions     immutable source-generation target/method + encrypted credential
    source_db_monitoring_state         active revision + enabled + monitoring state version
    source_db_usage_state              lease, current-identity latest baseline/attempt/success, reset/dealloc
    source_db_statement_baselines      latest-only internal counters
    source_db_usage_rollups            bounded UTC-hour aggregate
    ```

    이 persisted design을 적용할 경우 다음 파일은 additive migration **6**의 literal schema가 된다. 지금은 승인 전
    proposal이므로 migration file을 만들거나 실행하지 않는다. Constraint 이름도 migration baseline이며
    기존 migration 1~5를 수정하지 않는다.

    ```sql
    CREATE TABLE control.source_db_monitoring_revisions (
      source_id text NOT NULL,
      monitoring_revision bigint NOT NULL,
      source_generation bigint NOT NULL,
      method text NOT NULL,
      definition_revision text NOT NULL,
      monitor_username text NOT NULL,
      database_binding_id text NOT NULL,
      secret_nonce bytea NOT NULL,
      secret_ciphertext bytea NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      PRIMARY KEY (source_id, monitoring_revision),
      CONSTRAINT db_monitoring_revision_binding_unique
        UNIQUE (source_id, monitoring_revision, database_binding_id),
      CONSTRAINT db_monitoring_revision_fence_unique
        UNIQUE (
          source_id, monitoring_revision, database_binding_id,
          source_generation, definition_revision
        ),
      CONSTRAINT db_monitoring_revision_source_exists
        FOREIGN KEY (source_id, source_generation)
        REFERENCES control.source_profile_revisions (source_id, generation)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_monitoring_revision_number_valid
        CHECK (monitoring_revision > 0 AND source_generation > 0),
      CONSTRAINT db_monitoring_revision_method_valid
        CHECK (method = 'pg_stat_statements_v1'),
      CONSTRAINT db_monitoring_revision_definition_valid
        CHECK (definition_revision ~ '^sha256:[a-f0-9]{64}$'),
      CONSTRAINT db_monitoring_revision_username_valid
        CHECK (monitor_username ~ '^[a-z_][a-z0-9_]{0,62}$'),
      CONSTRAINT db_monitoring_revision_binding_valid
        CHECK (database_binding_id ~ '^sha256:[a-f0-9]{64}$'),
      CONSTRAINT db_monitoring_revision_secret_valid
        CHECK (octet_length(secret_nonce) = 12
          AND octet_length(secret_ciphertext) BETWEEN 17 AND 8208)
    );

    CREATE TRIGGER source_db_monitoring_revisions_are_immutable
    BEFORE UPDATE OR DELETE ON control.source_db_monitoring_revisions
    FOR EACH ROW EXECUTE FUNCTION control.reject_source_profile_revision_mutation();

    CREATE TABLE control.source_db_monitoring_state (
      source_id text PRIMARY KEY,
      active_monitoring_revision bigint NOT NULL,
      enabled boolean NOT NULL,
      monitoring_state_version bigint NOT NULL,
      database_binding_id text NOT NULL,
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT db_monitoring_state_source_exists
        FOREIGN KEY (source_id)
        REFERENCES control.active_source_profiles (source_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_monitoring_state_revision_exists
        FOREIGN KEY (
          source_id, active_monitoring_revision, database_binding_id
        ) REFERENCES control.source_db_monitoring_revisions (
          source_id, monitoring_revision, database_binding_id
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_monitoring_state_version_valid
        CHECK (active_monitoring_revision > 0 AND monitoring_state_version > 0),
      CONSTRAINT db_monitoring_state_database_unique UNIQUE (database_binding_id)
    );

    CREATE TABLE control.source_db_usage_state (
      source_id text PRIMARY KEY,
      lease_epoch bigint NOT NULL DEFAULT 0,
      lease_owner_replica_id text,
      lease_owner_incarnation bigint,
      lease_acquired_at timestamptz,
      lease_until timestamptz,
      next_attempt_at timestamptz NOT NULL,
      monitoring_revision bigint NOT NULL,
      monitoring_state_version bigint NOT NULL,
      source_generation bigint NOT NULL,
      source_state_version bigint NOT NULL,
      budget_profile text NOT NULL,
      metadata_revision text NOT NULL,
      definition_revision text NOT NULL,
      database_binding_id text NOT NULL,
      observation_identity text,
      observation_started_at timestamptz,
      target_instance_id text,
      server_version_num integer,
      extension_version text,
      compute_query_id text,
      track text,
      track_planning text,
      track_utility text,
      save text,
      max integer,
      baseline_at timestamptz,
      stats_reset timestamptz,
      dealloc bigint,
      last_attempt_at timestamptz,
      last_attempt_outcome text,
      last_attempt_reason_code text,
      last_success_at timestamptz,
      fresh_until timestamptz,
      updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT db_usage_state_observation_unique
        UNIQUE (source_id, observation_identity),
      CONSTRAINT db_usage_state_source_exists
        FOREIGN KEY (source_id)
        REFERENCES control.active_source_profiles (source_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_usage_state_metadata_exists
        FOREIGN KEY (source_id, metadata_revision)
        REFERENCES control.metadata_snapshots (source_id, revision)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_usage_state_monitoring_exists
        FOREIGN KEY (
          source_id, monitoring_revision, database_binding_id,
          source_generation, definition_revision
        )
        REFERENCES control.source_db_monitoring_revisions (
          source_id, monitoring_revision, database_binding_id,
          source_generation, definition_revision
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_usage_state_lease_replica_exists
        FOREIGN KEY (lease_owner_replica_id)
        REFERENCES control.runtime_replicas (replica_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_usage_state_lease_valid CHECK (
        lease_epoch >= 0
        AND (
          (
            lease_owner_replica_id IS NULL
            AND lease_owner_incarnation IS NULL
            AND lease_acquired_at IS NULL
            AND lease_until IS NULL
          ) OR (
            lease_epoch > 0
            AND lease_owner_replica_id IS NOT NULL
            AND lease_owner_replica_id ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'
            AND length(lease_owner_replica_id) <= 80
            AND lease_owner_incarnation IS NOT NULL
            AND lease_owner_incarnation > 0
            AND lease_acquired_at IS NOT NULL
            AND lease_until IS NOT NULL
            AND lease_until = lease_acquired_at + interval '120 seconds'
          )
        )
      ),
      CONSTRAINT db_usage_state_fence_valid CHECK (
        monitoring_revision > 0 AND monitoring_state_version > 0
        AND source_generation > 0 AND source_state_version > 0
        AND budget_profile ~ '^[A-Za-z_][A-Za-z0-9_$]{0,62}$'
        AND metadata_revision ~ '^sha256:[a-f0-9]{64}$'
        AND definition_revision ~ '^sha256:[a-f0-9]{64}$'
        AND database_binding_id ~ '^sha256:[a-f0-9]{64}$'
      ),
      CONSTRAINT db_usage_state_identity_valid CHECK (
        (
          (
            observation_identity IS NULL
            AND observation_started_at IS NULL
            AND target_instance_id IS NULL
            AND server_version_num IS NULL AND extension_version IS NULL
            AND compute_query_id IS NULL AND track IS NULL
            AND track_planning IS NULL AND track_utility IS NULL
            AND save IS NULL AND max IS NULL
          ) OR (
            observation_identity IS NOT NULL
            AND observation_identity ~ '^sha256:[a-f0-9]{64}$'
            AND observation_started_at IS NOT NULL
            AND target_instance_id IS NOT NULL
            AND target_instance_id ~ '^sha256:[a-f0-9]{64}$'
            AND server_version_num IS NOT NULL
            AND server_version_num BETWEEN 180000 AND 189999
            AND extension_version IS NOT NULL
            AND extension_version = '1.12'
            AND compute_query_id IS NOT NULL
            AND compute_query_id IN ('auto', 'on')
            AND track IS NOT NULL AND track = 'top'
            AND track_planning IS NOT NULL AND track_planning = 'off'
            AND track_utility IS NOT NULL AND track_utility = 'on'
            AND save IS NOT NULL AND save = 'on'
            AND max IS NOT NULL AND max = 5000
          )
        ) IS TRUE
      ),
      CONSTRAINT db_usage_state_baseline_valid CHECK (
        (
          (
            baseline_at IS NULL AND stats_reset IS NULL AND dealloc IS NULL
          ) OR (
            observation_identity IS NOT NULL
            AND observation_started_at IS NOT NULL
            AND baseline_at IS NOT NULL
            AND stats_reset IS NOT NULL
            AND dealloc IS NOT NULL AND dealloc >= 0
            AND last_attempt_at IS NOT NULL
          )
        ) IS TRUE
      ),
      CONSTRAINT db_usage_state_attempt_valid CHECK (
        (
          (
            last_attempt_at IS NULL AND last_attempt_outcome IS NULL
            AND last_attempt_reason_code IS NULL
          ) OR (
            last_attempt_at IS NOT NULL
            AND last_attempt_outcome IS NOT NULL
            AND (
              (last_attempt_outcome = 'accepted'
                AND last_attempt_reason_code IS NULL
                AND baseline_at IS NOT NULL
                AND baseline_at = last_attempt_at
                AND last_success_at IS NOT NULL
                AND last_success_at = last_attempt_at)
              OR (last_attempt_outcome = 'baseline'
                AND last_attempt_reason_code = 'BASELINE_REQUIRED'
                AND baseline_at IS NOT NULL
                AND baseline_at = last_attempt_at)
              OR (last_attempt_outcome = 'discarded'
                AND last_attempt_reason_code IS NOT NULL
                AND last_attempt_reason_code IN (
                  'RESET_DETECTED', 'SERVER_DEALLOCATION_DETECTED',
                  'ENTRY_DISAPPEARED', 'COUNTER_REGRESSION',
                  'ROW_LIMIT_EXCEEDED', 'COUNTER_OVERFLOW',
                  'OBSERVATION_INCOMPLETE'
                ) AND (
                  (last_attempt_reason_code IN (
                    'RESET_DETECTED', 'SERVER_DEALLOCATION_DETECTED',
                    'ENTRY_DISAPPEARED', 'COUNTER_REGRESSION'
                  ) AND baseline_at IS NOT NULL
                    AND baseline_at = last_attempt_at)
                  OR (last_attempt_reason_code IN (
                    'ROW_LIMIT_EXCEEDED', 'COUNTER_OVERFLOW',
                    'OBSERVATION_INCOMPLETE'
                  ) AND baseline_at IS NULL)
                ))
              OR (last_attempt_outcome = 'failed'
                AND last_attempt_reason_code IS NOT NULL
                AND last_attempt_reason_code IN (
                  'MONITOR_UNAVAILABLE', 'TARGET_MISMATCH',
                  'MONITOR_PRIVILEGE_MISMATCH', 'EXTENSION_UNAVAILABLE',
                  'SETTINGS_MISMATCH'
                ) AND baseline_at IS NULL)
            )
          )
        ) IS TRUE
      ),
      CONSTRAINT db_usage_state_success_valid CHECK (
        (
          (last_success_at IS NULL AND fresh_until IS NULL)
          OR (
            observation_identity IS NOT NULL
            AND observation_started_at IS NOT NULL
            AND last_attempt_at IS NOT NULL
            AND last_success_at IS NOT NULL
            AND fresh_until IS NOT NULL
            AND fresh_until = last_success_at + interval '15 minutes'
          )
        ) IS TRUE
      ),
      CONSTRAINT db_usage_state_time_order_valid CHECK (
        (
          (last_attempt_at IS NULL
            OR next_attempt_at = last_attempt_at + interval '300 seconds')
          AND (observation_started_at IS NULL
            OR (last_attempt_at IS NOT NULL
              AND observation_started_at <= last_attempt_at))
          AND (baseline_at IS NULL
            OR (last_attempt_at IS NOT NULL
              AND observation_started_at IS NOT NULL
              AND observation_started_at <= baseline_at
              AND baseline_at <= last_attempt_at))
          AND (last_success_at IS NULL
            OR (last_attempt_at IS NOT NULL
              AND last_success_at <= last_attempt_at))
        ) IS TRUE
      )
    );

    CREATE INDEX source_db_usage_state_due_idx
      ON control.source_db_usage_state (
        next_attempt_at ASC, source_id COLLATE "C" ASC
      );

    CREATE TABLE control.source_db_statement_baselines (
      source_id text NOT NULL,
      observation_identity text NOT NULL,
      queryid bigint NOT NULL,
      toplevel boolean NOT NULL,
      stats_since timestamptz NOT NULL,
      calls bigint NOT NULL,
      execution_time_us bigint NOT NULL,
      rows bigint NOT NULL,
      shared_blks_hit bigint NOT NULL,
      shared_blks_read bigint NOT NULL,
      shared_blks_dirtied bigint NOT NULL,
      shared_blks_written bigint NOT NULL,
      local_blks_hit bigint NOT NULL,
      local_blks_read bigint NOT NULL,
      local_blks_dirtied bigint NOT NULL,
      local_blks_written bigint NOT NULL,
      temp_blks_read bigint NOT NULL,
      temp_blks_written bigint NOT NULL,
      wal_records bigint NOT NULL,
      wal_fpi bigint NOT NULL,
      wal_bytes numeric(38, 0) NOT NULL,
      PRIMARY KEY (source_id, queryid, toplevel),
      CONSTRAINT db_statement_baseline_state_exists
        FOREIGN KEY (source_id, observation_identity)
        REFERENCES control.source_db_usage_state (
          source_id, observation_identity
        ) ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_statement_baseline_counters_valid CHECK (
        calls >= 0 AND execution_time_us >= 0 AND rows >= 0
        AND shared_blks_hit >= 0 AND shared_blks_read >= 0
        AND shared_blks_dirtied >= 0 AND shared_blks_written >= 0
        AND local_blks_hit >= 0 AND local_blks_read >= 0
        AND local_blks_dirtied >= 0 AND local_blks_written >= 0
        AND temp_blks_read >= 0 AND temp_blks_written >= 0
        AND wal_records >= 0 AND wal_fpi >= 0 AND wal_bytes >= 0
      )
    );

    CREATE TABLE control.source_db_usage_rollups (
      source_id text NOT NULL,
      budget_profile text NOT NULL,
      metadata_revision text NOT NULL,
      definition_revision text NOT NULL,
      bucket_start timestamptz NOT NULL,
      accepted_samples smallint NOT NULL,
      calls bigint NOT NULL,
      execution_time_us bigint NOT NULL,
      rows bigint NOT NULL,
      shared_blks_hit bigint NOT NULL,
      shared_blks_read bigint NOT NULL,
      shared_blks_dirtied bigint NOT NULL,
      shared_blks_written bigint NOT NULL,
      local_blks_hit bigint NOT NULL,
      local_blks_read bigint NOT NULL,
      local_blks_dirtied bigint NOT NULL,
      local_blks_written bigint NOT NULL,
      temp_blks_read bigint NOT NULL,
      temp_blks_written bigint NOT NULL,
      wal_records bigint NOT NULL,
      wal_fpi bigint NOT NULL,
      wal_bytes numeric(38, 0) NOT NULL,
      observed_at timestamptz NOT NULL,
      PRIMARY KEY (
        source_id, budget_profile, metadata_revision,
        definition_revision, bucket_start
      ),
      CONSTRAINT db_usage_rollup_source_exists
        FOREIGN KEY (source_id)
        REFERENCES control.active_source_profiles (source_id)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_usage_rollup_metadata_exists
        FOREIGN KEY (source_id, metadata_revision)
        REFERENCES control.metadata_snapshots (source_id, revision)
        ON UPDATE RESTRICT ON DELETE RESTRICT,
      CONSTRAINT db_usage_rollup_key_valid CHECK (
        budget_profile ~ '^[A-Za-z_][A-Za-z0-9_$]{0,62}$'
        AND definition_revision ~ '^sha256:[a-f0-9]{64}$'
        AND bucket_start =
          pg_catalog.date_trunc('hour', bucket_start AT TIME ZONE 'UTC')
            AT TIME ZONE 'UTC'
        AND observed_at >= bucket_start
        AND observed_at < bucket_start + interval '1 hour'
        AND accepted_samples BETWEEN 1 AND 12
      ),
      CONSTRAINT db_usage_rollup_counters_valid CHECK (
        calls >= 0 AND execution_time_us >= 0 AND rows >= 0
        AND shared_blks_hit >= 0 AND shared_blks_read >= 0
        AND shared_blks_dirtied >= 0 AND shared_blks_written >= 0
        AND local_blks_hit >= 0 AND local_blks_read >= 0
        AND local_blks_dirtied >= 0 AND local_blks_written >= 0
        AND temp_blks_read >= 0 AND temp_blks_written >= 0
        AND wal_records >= 0 AND wal_fpi >= 0 AND wal_bytes >= 0
      )
    );

    CREATE INDEX source_db_usage_rollups_read_idx
      ON control.source_db_usage_rollups (
        source_id, bucket_start DESC, observed_at DESC,
        budget_profile COLLATE "C" ASC,
        metadata_revision ASC, definition_revision ASC
      );
    ```

    Migration 6은 `source_mutation_receipts_operation_valid`을 같은 이름으로 재생성해 기존 6개 literal에
    `configure_database_native_monitoring|rotate_database_native_monitoring_credential|
    disable_database_native_monitoring|rollback_database_native_monitoring` 네 literal만 더한다. Receipt의
    400/409 invariant와 기존 row는 바꾸지 않는다. Monitoring mutation은 source generation/state를
    변경하지 않으므로 success receipt의 existing `resulting_generation/resulting_state_version`에는 CAS한
    current source 값을 그대로 기록하고 monitoring revision/version은 exact result JSON과 expected
    monitoring version을 포함한 request HMAC에만 둔다. 따라서 generic history column만으로 expected
    monitoring version을 사람이 직접 조회할 수 없는 점은 의도한 V1 한계다. Existing receipt identity
    sequence `USAGE` grant는 유지한다. Security reconcile의 exact writer grant는 revisions
    `SELECT,INSERT`, monitoring state와 usage state `SELECT,INSERT,UPDATE`, baseline과 rollup
    `SELECT,INSERT,UPDATE,DELETE`뿐이다. 다섯 table 모두 `TRUNCATE|REFERENCES|TRIGGER|MAINTAIN`과 ownership,
    schema `CREATE`, function/sequence grant는 없다. Migration 뒤 recovery fingerprint scope는 기존 13개에
    이 5개를 더한 18개 table, foreign key는 25개, user trigger는 5개이며 code rollback도 migration
    ledger/table/data를 보존한다.

    Existing code의 receipt decoder는 operation/result field를 strict하게 검사하므로 rollout은 네 새
    operation/result shape를 **read-only decode만** 하는 compatibility release를 전 fleet에 먼저 배포한 뒤
    migration 6, writer/route release 순서다. Rollback target도 이 compatibility release이며 그보다 오래된
    release로 되돌리지 않는다. `db_monitoring_state_database_unique` constraint의 exact `23505`만
    cross-source binding `SOURCE_MONITORING_CONFLICT`로 변환하고 다른 unique violation을 축약하지 않는다.

    Strict source manifest 밖에 두어 old code가 기존 source generation/read credential을 그대로 load한다.
    Monitoring transition audit/history는 existing immutable terminal mutation receipt/history를 재사용하고
    독립 `source_db_monitoring_history` table은 만들지 않는다.
    Writer DELETE는 obsolete baseline과 deterministic rollup cap cleanup에만 허용하고 revision/state/
    history에는 허용하지 않는다. DDL, truncate와 pgss reset capability는 없다.
12. Existing operator-only usage response에 `gateway`나 `monetary_cost`를 재해석하지 않고 sibling
    `database_native` section을 추가한다. Exact shape는 다음과 같다.

    ```text
    database_native:
      status, reason_code,
      coverage="target_reader_role_pg_stat_statements",
      lower_bound=true, includes_auxiliary_statements=true,
      last_attempt: null | {
        attempted_at: utc-timestamp, outcome, reason_code
      },
      last_success_at: null | utc-timestamp,
      fresh_until: null | utc-timestamp,
      window_start: utc-hour-timestamp,
      window_end: utc-hour-timestamp,
      rollups: [] | [rollup]

    status = not_configured | pending | available | stale | unavailable
    outcome = baseline | accepted | discarded | failed
    reason_code = NOT_CONFIGURED | MONITORING_DISABLED | BASELINE_REQUIRED |
      SOURCE_DISABLED | MONITOR_UNAVAILABLE | TARGET_MISMATCH |
      MONITOR_PRIVILEGE_MISMATCH | EXTENSION_UNAVAILABLE | SETTINGS_MISMATCH | RESET_DETECTED |
      SERVER_DEALLOCATION_DETECTED | ENTRY_DISAPPEARED |
      COUNTER_REGRESSION | ROW_LIMIT_EXCEEDED | COUNTER_OVERFLOW |
      OBSERVATION_INCOMPLETE | OBSERVATION_EXPIRED | null

    rollup:
      budget_profile, metadata_revision, definition_revision,
      bucket_start, observed_at, accepted_samples,
      calls, execution_time_us, rows,
      shared_blks_hit, shared_blks_read, shared_blks_dirtied, shared_blks_written,
      local_blks_hit, local_blks_read, local_blks_dirtied, local_blks_written,
      temp_blks_read, temp_blks_written, wal_records, wal_fpi, wal_bytes
    ```

    `attempted_at`, `last_success_at`, `fresh_until`, `window_*`, `bucket_start`, `observed_at`은 outer
    `read_at`과 같은 Control DB clock/UTC ISO representation이다. `window_end`는 `read_at`의 UTC hour,
    `window_start=window_end-31 days`이고 양 끝을 포함한다. `baseline`은
    `BASELINE_REQUIRED`, `accepted`는 null, `discarded`는 reset/deallocation/entry/regression/row-limit/
    overflow/incomplete 중 exact detected reason, `failed`는 monitor/monitor-privilege/bound-target/extension/settings
    reason만 갖는다. 다른 outcome/reason 조합은 persisted decode failure다.

    Availability에 사용할 current observation identity는 exact
    `(active monitoring_revision, target_instance_id, fixed-order supported-settings tuple,
    source generation, source state version,
    budget_profile, metadata_revision, definition_revision)`다. Credential rotation, configure/rollback,
    source/profile/metadata/definition change, restart/failover로 identity가 바뀌면 old success는 즉시 current
    status/freshness에서 제외하고 새 baseline을 요구한다. Target을 아직 읽지 못한 current config attempt는
    nullable target로 기록하지만 old target success를 재사용하지 않는다.

    Status precedence는 source disabled `unavailable/SOURCE_DISABLED` → monitoring state 없음
    `not_configured/NOT_CONFIGURED` → explicitly disabled `not_configured/MONITORING_DISABLED` → current
    binding mismatch `unavailable/TARGET_MISMATCH` → current observation state 순서다. Current identity의
    accepted success가 있고 `read_at <= fresh_until`이면 이후 same-identity failure/discard/re-baseline이
    있어도 `available/null`이 우선한다. 그 외 current observation precedence는 정확히 다음과 같다.
    Latest complete `baseline_at`이 없던 success보다 새롭거나 accepted success가 전혀 없을 때 baseline이
    존재하면 `pending/BASELINE_REQUIRED`; accepted success가 expired했고 그 뒤 새 complete baseline이 없으면
    `stale/OBSERVATION_EXPIRED`; baseline/success/committed attempt가 모두 없는 initial configured state면
    `pending/BASELINE_REQUIRED`; baseline/success 없이 latest committed attempt가 `failed` 또는 `discarded`면
    `unavailable/<그 attempt의 bounded reason>`이다. 따라서 첫 scan의 row-limit/overflow/incomplete도 0이나
    pending으로 숨기지 않는다. Baseline 뒤 accepted sample이 생기면 그 success가 새
    epoch를 충족한다. Same-identity reset/deallocation은 fresh success를 즉시 지우지 않지만 freshness 만료
    뒤에는 새 baseline 이후 accepted sample을 요구한다. Identity가 바뀐 old success는 항상 freshness에
    참여하지 않는다. Historical in-window rollup은
    disabled/not-configured/stale/identity change에도 삭제·zero 합성 없이 반환한다.

    `accepted_samples`는 `1..12` integer이며 explicit-zero와 missing hour를 구분한다. Rollup은
    `bucket_start DESC, observed_at DESC, budget_profile C ASC, metadata_revision ASC,
    definition_revision ASC`의 최대 1,000행이다. `wal_bytes`만 `0..10^38-1` decimal string이고 다른 public
    counter는 `0..9223372036854775807` integer다. Queryid, target, collector replica와 credential은 반환하지 않는다. Missing
    hour/value는 absent이고 0이 아니다. Persisted decode/cardinality 위반은 status 200으로 가장하지 않고
    existing `SOURCE_CONTROL_UNAVAILABLE` 503이다. Monetary placeholder는 그대로다.
    Writer cap cleanup도 같은 fully ordered key에서 첫 1,000행을 keep하고 나머지를 delete한다. Source
    advisory lock 안의 한 transaction에서 수행하므로 동률이나 replica마다 다른 victim을 고르지 않는다.

장점은 application reader와 network-facing monitor에 broad statistics privilege를 주지 않고 reset,
server deallocation/entry disappearance과 replica 중복을 fail-closed하는 것이다. 비용은 source DBA object/credential, additive Control
schema와 collector lifecycle이 필요하다는 점이다.

### `COST-01-B` — existing reader executes the sanitized projection

A와 같은 sanitized no-argument function만 reader에게 실행하게 해 별도 LOGIN/credential을 줄인다.
그러나 reader compromise가 자기 operational statement identity/counter를 볼 수 있고 catalog/query/
monitor capacity가 결합되며 sampling statement가 같은 user aggregate를 오염시킬 수 있다. Broad
predefined role과 direct view access는 여전히 금지한다. 변경량은 작지만 권장하지 않는다.

이는 비교용 direction-only 대안이며 implementation-ready change set이 아니다. Existing active source generation의
reader credential을 collector가 직접 재사용할지, 별도 monitoring revision/admin endpoint를 만들지,
disable/rollback/lease/status/public projection을 A와 같게 유지할지 아직 고정하지 않았다. 따라서
`COST-01-B`라는 ID 선택이나 포괄적 승인은 구현 권한이 아니며, 이 lifecycle·wire·persistence·rollback을
exact restatement한 새 승인 경계를 먼저 제시하고 사용자가 별도로 승인해야 한다.

### `COST-01-C` — external collector or explicit deferral

Query Man은 DB-native row/config/schema/public field를 만들지 않는다. Current `/usage`의 exact
`resource|gateway|monetary_cost` shape와 monetary `not_configured` placeholder를 그대로 유지하며
`database_native` section 자체를 추가하지 않는다. 외부 aggregate input은 실제 요구가 생기면 별도
signed/bounded external interface로 설계하고 지금 generic webhook/plugin을 만들지 않는다. 가장 안전한
defer지만 M15는 완료되지 않는다.

## Explicit Rejections

- Application reader 또는 monitoring LOGIN에 `pg_monitor`/`pg_read_all_stats` 직접 부여
- Query text, SQL, parameter 또는 representative statement 저장/반환
- PostgreSQL query ID와 gateway fingerprint/application query ID의 exact mapping
- Runtime replica마다 lease 없이 같은 server-global counter 수집
- Physical/logical/load-balanced target의 임의 합산
- `pg_stat_statements_reset`, missing/reset/deallocation interval을 0으로 기록
- Caller/tenant/user/organization dimension, chargeback 또는 통화 단위 비용 추정

## Rollout And Rollback

1. ENC final baseline을 확정하고 `TIME-03`을 완료하거나 정확히 defer한다. 그보다 먼저 진행하려면 열린
   ENC/TIME보다 COST를 앞당기는 exact global reprioritization을 별도로 승인받으며, 이는 ENC의 production
   blocker를 해결하지 않는다.
2. Source DBA가 extension/settings, NOLOGIN owner, sanitized function, monitoring LOGIN과 exact negative
   privilege probe를 준비한다. Schema/function 이름은 exact `query_man_monitor.monitor_info_v1()`와
   `query_man_monitor.monitor_statements_v1()`다.
3. 네 monitoring receipt operation/result를 read-only decode하는 compatibility release를 먼저 전 fleet에
   배포한다. 그 뒤 additive Control migration 6을 적용하고 writer/route release를 monitoring config 없는
   `not_configured` 상태로 배포한다. Query data plane은 바뀌지 않는다.
4. Source 하나를 configure한다. Candidate credential preflight가 성공한 뒤에만 monitoring authority를
   commit하고 baseline/fresh sample/fence를 검증한 뒤 확대한다. `/usage` exact shape가 바뀌므로 admin
   traffic은 새 fleet convergence 동안 한 version으로 route한다.
5. Code rollback은 compatibility release까지만 허용한다. Collection을 멈추고 state를 stale하게 둘 뿐
   Control table/ledger/data와 source
   function/role을 drop/reset하지 않는다. Security rollback은 collector drain 뒤 monitoring LOGIN을
   `NOLOGIN`으로 만들 수 있다. Existing source manifest/read credential과 query path는 보존한다.

## `COST-04` Boundary — separate ADR 0023 approval required

이 ADR의 A는 sanitized collection, delta/rollup, base rollup의 inclusive 31일 logical visibility/input
window와 operator-only status/projection을 정확히 제안하지만 TODO `COST-04`의 usage spike/alert는
별도 [proposed ADR 0023](0023-database-native-usage-spike-alert.md)에 분리한다. 그 문서의 A는 closed-hour
execution-time count-qualified signal, baseline/hysteresis/lifecycle, Control migration 7, 90-day event visibility와
operator polling-only wire를 exact하게 제안하고 B/C도 구분한다.

따라서 `COST-01-A`를 정확히 승인해도 ADR 0023을 자동 승인하거나 `COST-04`를 구현·완료하지 않는다.
Base rollup의 explicit-zero/accepted-sample/identity evidence가 생긴 뒤 별도 exact 승인을 받는다. 그 전에는
기존 `/usage` projection을 threshold나 alert로 해석하거나 새 metric label/notification을 만들지 않는다.

## Verification

- PostgreSQL 18.6 fresh/upgrade fixture의 positive projection과 monitor target-only CONNECT/database TEMP/direct
  view/raw function/reset/broad-role negative privilege probe, exact owner column ACL과 raw function의
  NOLOGIN/exact membership-direction containment,
  canonical reader-literal `prosrc`와 same-OID body drift,
  exact schema/fully-qualified no-argument `RETURNS TABLE` SQL OID/order,
  all-cell non-null, `execution_time_us` numeric-to-int64 및 `wal_bytes` numeric integrality/38-digit range와
  overload rejection
- Bound source database/application-reader OID probe, info-before/after와 모든 statement row의 exact binding,
  다른 reader/function body/mixed rowset의 fail-closed corpus
- Target/extension/setting/row-limit/shape-or-decode/counter-bound/generic transport의 ordered simultaneous-failure
  precedence, finite `-0.5` 대 integral negative corpus와 public outcome/reason golden
- Monitor role/database default가 다른 fixture의 transaction-local `TimeZone=UTC`/`DateStyle=ISO,YMD`
  설정·검사, stable timestamptz decode와 canonical definition digest golden
- Global/selective reset의 `stats_reset|stats_since`, eviction/dealloc, counter regression, empty/row-cap,
  incomplete scan, restart/save와 target/failover re-baseline
- Epoch 0/null-owner initial seed, nullable corruption rejection, due-index ordering, two Runtime replica의
  120-second epoch lease/fence/normal-clear, source I/O 중 Control connection 0, crash/takeover
- Monitoring configure/rotate/disable/rollback CAS/receipt, generation mismatch, candidate preflight 뒤 CAS
  재검사, preflight/commit 사이 source drift, disabled-preserving credential rotate, AES-GCM AAD와 old-code load
- Monitoring GET/PUT/POST/DELETE exact HTTP shape, initial/version CAS, disable-preserved pointer,
  historical rollback/re-enable, terminal replay/conflict/error corpus와 credential redaction
- Definition/generation/profile/metadata revision transition, auxiliary/external-role-use scope와
  canonical definition digest golden, current-identity freshness exclusion, accepted-at whole-delta UTC-hour
  attribution, hourly/31-day/write-cleanup/read-fail-closed cap과 counter overflow boundaries
- Fresh success, newer baseline, expired success, no-attempt initial pending와 first-attempt failure unavailable의
  exact status precedence, first discarded row-limit/overflow/incomplete unavailable, stable-complete current
  rebaseline 대 invalid sample baseline-clear/wait-next-complete, new target/allowed-setting baseline outcome,
  stale Control fence no-write 분리
- SQL/question/parameter/credential/queryid/caller/tenant redaction과 public shape/reason corpus
- Compatibility-reader-first/migration/writer rollout, new receipt를 읽는 rollback target, 18-table/25-FK/
  5-trigger recovery fingerprint/archive restore와 no data/schema deletion
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`, `uv run pytest -m integration`

## Approval Boundary

이 문서는 정확한 제안일 뿐 현재 승인 baseline이나 구현이 아니다. 승인하려면 해당되는 모든 변경
범주와 영향을 정확히 지정해야 한다. 아래 문구는 decision 선택만 고정하며 ENC/TIME start gate를 자동으로
열지 않는다. 구현까지 먼저 시작하려면 별도로 “열린 ENC-01~02와 TIME-03보다 COST-01~05를 먼저
수행한다”는 exact reprioritization과 production blocker가 남는 영향을 승인해야 한다.
현재 A만 base collector/rollup/projection의 implementation-ready 범위를 제시하며 C는 exact defer다.
B는 direction-only라
ID 선택만으로 승인할 수 없고 lifecycle/wire/persistence/rollback을 다시 명시한 별도 exact 승인이 필요하다.
아래 A 문구는 proposed ADR 0023의 `COST-04` threshold/alert addendum를 승인하지 않는다.

```text
COST-01-A를 전용 monitoring LOGIN과
exact query_man_monitor schema의 fully-qualified info/statement sanitized projection,
database당 monitored source 하나·physical-clone 포함 stable database binding uniqueness,
source-owner가 case-exact reader literal을 고정한 canonical SQL prosrc/body-template digest,
SECURITY DEFINER/STABLE/PARALLEL RESTRICTED/SQL metadata·exact 21+2 column ACL·owner-only raw non-reset
function capability와 NOLOGIN·owner outgoing readstats/inbound 0·monitor membership 0 containment·versioned
effective ACL profile,
preflight+매-scan body/negative privilege probe, source DBA가 blast-radius를 검토한 relevant
target database PUBLIC TEMPORARY·other-database PUBLIC CONNECT 및 PUBLIC/object ACL hardening·legitimate-role
grant-option-false regrant·target direct CONNECT grant-option-false·새 database의
ALLOW_CONNECTIONS false→ACL hardening→true 선행 순서·Query Man의 shared ACL 자동 revoke 금지·effective capability mismatch의
configure/scan fail-closed,
exact RETURNS TABLE SQL type/non-null·execution-time/wal numeric integrality/range와
bound source database/application-reader/statement-row OID 검증,
TimeZone=UTC·DateStyle=ISO,YMD monitor session, target-instance identity와 row별 stats_since,
canonical definition revision, accepted Control-DB clock의 whole-delta UTC-hour 귀속,
5초 due poll·replica당 4 scan·300초 cadence·단일-target·120초 epoch lease-fenced reader-role pgss aggregate,
5,001-row/duplicate identity 검출, reset/server-deallocation/privilege/overflow fail-closed와 explicit-zero row,
별도 immutable monitoring revision/pointer·exact operator GET/configure/rotate/disable/rollback·
disabled-preserving credential rotate·authority commit 전 source preflight·source/monitor CAS/receipt·AES-GCM AAD,
target/supported-settings observation identity별 baseline/freshness, complete-current rebaseline 대 invalid-sample
baseline invalidation, new identity baseline과 initial pending/failure/discard 및 ordered failure precedence,
additive Control migration 6의 literal 5-table composite PK/FK/null-safe CHECK/due index/immutability/
least-privilege ACL, epoch-0 null-owner initial seed·normal lease clear·raw OID 비저장과 monitoring receipt
operation 4개·404/503 non-terminal 의미·18-table/25-FK/5-trigger recovery scope,
31일 logical retention, accepted_samples와 deterministic 1,000-row cap, exact operator-only database_native
status/reason/rollup projection과 bounded public monitoring errors,
Query Man query별 CPU/청구 근거가 아니라는 한계, compatibility-reader-first/migration/writer rollout과
compatibility release까지만 가능한 schema/data-preserving rollback
범위로 승인한다. 이는 change-set 선택이며 열린 ENC/TIME보다 구현을 먼저 시작하는 승인은 아니다.
```
