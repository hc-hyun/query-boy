# ADR 0021: Database-Native Cost Attribution

Status: Proposed read-only prework — priority gate and user approval required before implementation

Date: 2026-08-26

## Priority Boundary

이 문서는 `COST-01`의 선택지를 미리 검토한 초안이다. 열린 `TIME-03`이 완료되거나 사용자가 이를
명시적으로 defer하기 전에는 `COST-01`을 공식 시작하지 않고 code, source role/function, Control
schema/config와 public projection을 바꾸지 않는다. 아래 선택지를 승인하는 것은 `ENC-01`이나
`TIME-03`의 완료를 뜻하지 않으며 contract 선택만 먼저 승인해도 implementation start gate는 열리지
않는다. 열린 ENC 작업보다 먼저 구현하려면 별도의 exact global reprioritization이 필요하다.

## Context

현재 `CTRL-07A`/`CTRL-08`은 source별 resource estimate와 gateway가 성공적으로 보고한 hourly
lower-bound만 저장한다. `/admin/sources/{source_id}/usage`의 `monetary_cost`는 provider 근거가 없어
`not_configured`이고 DB-native statement counter는 아직 수집하지 않는다. `budget_profile`만 resource
tier이며 caller/user/organization chargeback dimension은 없다.

Local PostgreSQL 18.6 fixture에는 `pg_stat_statements` 1.12가 preload되어 있고
`compute_query_id=auto`, `track=top`, `track_planning=off`, `track_utility=on`, `save=on`, `max=5000`이다.
네 source reader는 database `CONNECT`만 가지며 extension view/function, `pg_monitor`,
`pg_read_all_stats`, reset capability는 갖지 않는다.

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
   아래 6번을 따른다. Exact database `CONNECT`, non-PUBLIC `query_man_monitor` schema `USAGE`와 아래 두 fully-qualified
   function `EXECUTE`만 받고 reader/view owner,
   `pg_monitor`, `pg_read_all_stats`, `pg_signal_backend` membership, extension view 직접 `SELECT`와
   reset 실행 권한은 받지 않는다.
2. Source owner가 관리하는 별도 NOLOGIN function owner만 `pg_read_all_stats`와 필요한 exact
   `pg_control_system()` execute를 가진다. Locked `search_path`와 fully-qualified object를 쓰는 argument
   없는 두 `SECURITY DEFINER` function으로 header와 rowset을 분리한다. Exact schema는
   `query_man_monitor`이고 PUBLIC schema/function privilege는 모두 revoke한다. Collector는 search path에
   의존하지 않고 아래 이름 그대로 `SELECT * FROM` 호출한다. 두 function signature는 argument가 없고
   아래 순서/type의 exact `RETURNS TABLE`이며 overload를 만들지 않는다.

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

   PostgreSQL output parameter에 `NOT NULL` constraint를 선언할 수 없으므로 contract가 모든 cell의
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
   `0..9223372036854775807`, `wal_bytes`는 `0..10^38-1`이다. Decode, delta 또는 atomic rollup add가
   범위를 벗어나면 partial write 없이 scan 전체를 `COUNTER_OVERFLOW`로 discard한다.
   Raw instance field는 collector connection 안에서만 사용하고 저장/log/public projection하지 않는다.

   Preflight와 매 scan은 active source generation의 application reader username을 parameter `$1`로만 넣어
   같은 monitor connection에서 다음 exact binding probe를 실행한다. Username 원문과 database name은
   source config와 in-memory 비교에만 쓰고 sample/log/public projection에 저장하지 않는다.

   ```sql
   SELECT (
            SELECT database.oid
            FROM pg_catalog.pg_database AS database
            WHERE database.datname = pg_catalog.current_database()
          ) AS dbid,
          pg_catalog.to_regrole($1::text)::oid AS userid;
   ```

   Exactly one row와 두 non-null OID를 요구한다. Info-before와 info-after의 `dbid/userid`는 이 binding
   row와 같고, 모든 statement row의 `dbid/userid`는 두 info와 같아야 한다. Info가 bound source와 다르면
   `TARGET_MISMATCH`, statement row 하나라도 다르면 `OBSERVATION_INCOMPLETE`로 전체 scan을 fail closed하고
   baseline/delta로 쓰지 않는다. 이 probe는 expected username을 target argument로 source-owner function에
   넘기지 않으며 broad statistics privilege도 요구하지 않는다.
3. Collector는 info field의 exact canonical UTF-8 encoding을 SHA-256해 64-lower-hex
   `target_instance_id`를 만든다.

   ```text
   pg18\n{system_identifier}\n{postmaster_started_at YYYY-MM-DDTHH:MM:SS.ffffff+00:00}\n{server_addr canonical inet}\n{server_port}\n{dbid}\n{userid}
   ```

   V1은 non-load-balanced TCP endpoint와 non-null `server_addr`만 지원한다. DB/role recreation,
   failover 또는 postmaster restart는 target ID를 바꿔 반드시 re-baseline한다. `save=on`으로 counter가
   restart 뒤 보존돼도 이전 target과 subtract하지 않는다.
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
     "binding": {"database_oid": "current_database_catalog_oid", "info_must_match_bound_source": true, "reader_oid": "to_regrole(active_source_reader_username_parameter)", "statement_rows_must_match_info": true},
     "bounds": {"counter_max": 9223372036854775807, "freshness_seconds": 900, "lease_seconds": 120, "rollup_rows_per_source": 1000, "statement_rows": 5000, "wal_bytes_max": "99999999999999999999999999999999999999", "window_days": 31},
     "cadence_seconds": 300,
     "delta": {"complete": "subtract_previous", "counter_overflow": "discard_invalidate_wait_complete", "counter_regression": "discard_current_complete_rebaseline", "empty_rowset": "valid_complete_sample", "entry_missing": "discard_current_complete_rebaseline", "incomplete_scan": "discard_invalidate_wait_complete", "mid_scan_stats_reset_or_dealloc_change": "discard_invalidate_wait_complete", "mid_scan_target_or_settings_change": "fail_invalidate_wait_complete", "new_entry": "full_cumulative_delta", "new_target_or_allowed_settings": "baseline_new_observation_identity", "prior_dealloc_increase": "discard_current_complete_rebaseline", "prior_global_reset": "discard_current_complete_rebaseline", "row_limit": "discard_invalidate_wait_complete", "selective_reset": "discard_current_complete_rebaseline", "stale_control_fence": "no_write"},
     "failure_precedence": [
       ["bound_database_or_reader_or_mid_scan_target", "failed_TARGET_MISMATCH"],
       ["extension_preload_version_or_projection_missing", "failed_EXTENSION_UNAVAILABLE"],
       ["unsupported_or_mid_scan_unstable_setting", "failed_SETTINGS_MISMATCH"],
       ["statement_rows_over_5000", "discarded_ROW_LIMIT_EXCEEDED"],
       ["mid_scan_reset_or_dealloc_shape_type_null_info_cardinality_decode_nonfinite_nonintegral_statement_binding_or_incomplete", "discarded_OBSERVATION_INCOMPLETE"],
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
     "projection": {"info_function": "query_man_monitor.monitor_info_v1()", "overloads": false, "schema": "query_man_monitor", "statement_function": "query_man_monitor.monitor_statements_v1()"},
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
     "target_instance": {"digest": "sha256_lower_hex", "template": "pg18\n{system_identifier}\n{postmaster_started_at_utc_microseconds}\n{server_addr_canonical_inet}\n{server_port}\n{dbid}\n{userid}"},
     "timeouts": {"connect_seconds": 5, "idle_in_transaction_ms": 5000, "lock_ms": 250, "statement_ms": 20000, "transaction_ms": 75000},
     "transaction": "read_only_read_committed",
     "transforms": {"execution_time_us": "round(total_exec_time::numeric*1000)", "wal_bytes": "numeric38_decimal_string"},
     "version": 1
   }
   ```

   V1 golden은 아래 canonical material을 다시 계산한 값이며 documentation test가 이를 검증한다.
   `sha256:b4bf6e4400041f51d57cf828ed580100c07b59cdb543268b363989ef64484b77`

   Field addition/removal/order/type, projection name/signature, deterministic session setting, rounding, support
   settings, target digest algorithm, reset/delta rule, cadence/freshness, attribution or cap change creates a new
   definition revision. Source-specific target ID, credential, generation, profile와 metadata revision 값 자체는
   이 global definition hash에 넣지 않는다.
5. Internal base identity는
   `target_instance_id + dbid + userid + queryid + toplevel`; sample identity에는 row별 `stats_since`,
   global `stats_reset`, 위 JSON의 fixed-order supported-settings tuple과 collector definition revision도
   포함한다. Query ID를 gateway fingerprint, application `query_id`, caller 또는 tenant와 join하거나
   외부에 공개하지 않는다.
6. 한 collector가 5분마다 `binding/info-before → bounded statement rows → binding/info-after`를 읽는다.
   Empty rowset도 complete sample이다. Exactly one-row binding/info, identical before/after target/settings/
   `stats_reset`/`dealloc`, exact row shape/type/null/bounds와 모든 row의 binding 일치를 모두 만족해야 stable
   complete sample이다.

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
   2. Extension/preload/version 또는 required schema/function 부재:
      `failed/EXTENSION_UNAVAILABLE`.
   3. Unsupported setting 또는 info-before/after의 allowed setting 불안정:
      `failed/SETTINGS_MISMATCH`.
   4. Statement row가 5,000개를 초과함: `discarded/ROW_LIMIT_EXCEEDED`.
   5. Mid-scan reset/dealloc, column/order/SQL OID/null/info cardinality/decode, non-finite·non-integral numeric,
      statement-row binding 또는 incomplete cursor 위반: `discarded/OBSERVATION_INCOMPLETE`.
      따라서 finite `-0.5`는 여기서 끝나며 negative counter 규칙과 겹치지 않는다.
   6. Decode된 counter가 finite integral이지만 negative이거나 허용 상한 초과:
      `discarded/COUNTER_OVERFLOW`.
   7. 위 condition이 아닌 connection, permission, transport, timeout 또는 projection SQL execution 오류:
      `failed/MONITOR_UNAVAILABLE`.

   Control의 source generation/state, monitoring revision, profile/metadata/definition 또는 lease fence가 scan
   중 바뀐 경우는 위 두 source-result 규칙과 다르게 stale collector의 sample/baseline/attempt/rollup 전체를
   no-write한다. 완전한 baseline 뒤 새 base identity가 나타나면 그 cumulative counter 전체가 해당
   interval의 observed delta다. Last accepted complete sample freshness는 15분이다.
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
   뒤로 이동할 수 있다. V1은 source마다 exact stats target 하나만 허용하고 replica를 합산하지 않는다.
   Collector replica ID는 lease provenance일 뿐 usage dimension이 아니다.
9. Lease는 source별 Control DB row의 monotonic epoch, owner replica/incarnation과 DB-clock
   `lease_until=now+120 seconds`다. Collector는 짧은 Control transaction에서 acquire한 뒤 connection과
   lock을 놓고 source scan을 수행한다. 새 짧은 transaction이 current owner/epoch, unexpired lease와
   unchanged source/config/revision을 확인할 때만 결과를 commit한다. Source I/O 중 Control connection이나
   advisory lock을 잡지 않고 lease를 갱신하지 않는다. Expiry/fencing/config change는 stale owner가
   last-attempt까지 쓰지 못하게 sample/baseline/attempt/rollup 전체를 commit하지 않는다. Public state는
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
    실제 실행하지 않는다. Source I/O 동안 Control connection/lock은 0개다.
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
    receipt/replay 의미를 따르고 auth/dependency failure는 terminal success로 기록하지 않는다.

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
      EXTENSION_UNAVAILABLE | SETTINGS_MISMATCH | RESET_DETECTED |
      SERVER_DEALLOCATION_DETECTED | ENTRY_DISAPPEARED |
      COUNTER_REGRESSION | ROW_LIMIT_EXCEEDED | COUNTER_OVERFLOW |
      OBSERVATION_INCOMPLETE | OBSERVATION_EXPIRED | null

    rollup:
      budget_profile, metadata_revision, definition_revision,
      bucket_start, observed_at,
      calls, execution_time_us, rows,
      shared/local blocks hit|read|dirtied|written,
      temp blocks read|written, wal_records, wal_fpi, wal_bytes
    ```

    `attempted_at`, `last_success_at`, `fresh_until`, `window_*`, `bucket_start`, `observed_at`은 outer
    `read_at`과 같은 Control DB clock/UTC ISO representation이다. `window_end`는 `read_at`의 UTC hour,
    `window_start=window_end-31 days`이고 양 끝을 포함한다. `baseline`은
    `BASELINE_REQUIRED`, `accepted`는 null, `discarded`는 reset/deallocation/entry/regression/row-limit/
    overflow/incomplete 중 exact detected reason, `failed`는 monitor/bound-target/extension/settings
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

    Rollup은 `bucket_start DESC, observed_at DESC, budget_profile C ASC, metadata_revision ASC,
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

이는 비교용 direction-only 대안이며 implementation-ready 계약이 아니다. Existing active source generation의
reader credential을 collector가 직접 재사용할지, 별도 monitoring revision/admin endpoint를 만들지,
disable/rollback/lease/status/public projection을 A와 같게 유지할지 아직 고정하지 않았다. 따라서
`COST-01-B`라는 ID 선택이나 포괄적 승인은 구현 권한이 아니며, 이 lifecycle·wire·persistence·rollback을
exact restatement한 새 승인 경계를 먼저 제시하고 사용자가 별도로 승인해야 한다.

### `COST-01-C` — external collector or explicit deferral

Query Man은 DB-native row/config/schema/public field를 만들지 않는다. Current `/usage`의 exact
`resource|gateway|monetary_cost` shape와 monetary `not_configured` placeholder를 그대로 유지하며
`database_native` section 자체를 추가하지 않는다. 외부 aggregate input은 실제 요구가 생기면 별도
signed/bounded contract로 설계하고 지금 generic webhook/plugin을 만들지 않는다. 가장 안전한
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
3. Additive Control migration을 먼저 적용하고 새 application을 monitoring config 없는
   `not_configured` 상태로 배포한다. Query data plane은 바뀌지 않는다.
4. Source 하나를 configure한다. Candidate credential preflight가 성공한 뒤에만 monitoring authority를
   commit하고 baseline/fresh sample/fence를 검증한 뒤 확대한다. `/usage` exact shape가 바뀌므로 admin
   traffic은 새 fleet convergence 동안 한 version으로 route한다.
5. Code rollback은 collection을 멈추고 state를 stale하게 둘 뿐 Control table/ledger/data와 source
   function/role을 drop/reset하지 않는다. Security rollback은 collector drain 뒤 monitoring LOGIN을
   `NOLOGIN`으로 만들 수 있다. Existing source manifest/read credential과 query path는 보존한다.

## Verification

- PostgreSQL 18.6 fresh/upgrade fixture의 positive projection과 direct view/query text/reset/broad-role
  negative privilege probe, exact schema/fully-qualified no-argument `RETURNS TABLE` SQL OID/order,
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
- Two Runtime replica의 120-second epoch lease/fence, source I/O 중 Control connection 0, crash/takeover
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
- Migration-first, old-code rollback, recovery fingerprint/archive restore와 no data/schema deletion
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`, `uv run pytest -m integration`

## Approval Boundary

이 제안은 승인된 계약이 아니다. 아래 문구는 contract 선택만 고정하며 ENC/TIME start gate를 자동으로
열지 않는다. 구현까지 먼저 시작하려면 별도로 “열린 ENC-01~02와 TIME-03보다 COST-01~05를 먼저
수행한다”는 exact reprioritization과 production blocker가 남는 영향을 승인해야 한다.
현재 A만 아래 문구로 implementation-ready 범위를 제시하며 C는 exact defer다. B는 direction-only라
ID 선택만으로 승인할 수 없고 lifecycle/wire/persistence/rollback을 다시 명시한 별도 exact 승인이 필요하다.

```text
COST-01-A 계약을 전용 monitoring LOGIN과
exact query_man_monitor schema의 fully-qualified info/statement sanitized projection,
exact RETURNS TABLE SQL type/non-null·execution-time/wal numeric integrality/range와
bound source database/application-reader/statement-row OID 검증,
TimeZone=UTC·DateStyle=ISO,YMD monitor session, target-instance identity와 row별 stats_since,
canonical definition revision, accepted Control-DB clock의 whole-delta UTC-hour 귀속,
단일-target·120초 epoch lease-fenced reader-role pgss aggregate, reset/server-deallocation/overflow fail-closed,
별도 immutable monitoring revision/pointer·exact operator GET/configure/rotate/disable/rollback·
disabled-preserving credential rotate·authority commit 전 source preflight·source/monitor CAS/receipt·AES-GCM AAD,
target/supported-settings observation identity별 baseline/freshness, complete-current rebaseline 대 invalid-sample
baseline invalidation, new identity baseline과 initial pending/failure/discard 및 ordered failure precedence,
31일 logical retention, deterministic 1,000-row cap, exact operator-only database_native status/reason/rollup projection,
Query Man query별 CPU/청구 근거가 아니라는 한계, migration-first 및 schema/data-preserving rollback
범위로 승인한다. 이는 contract 선택이며 열린 ENC/TIME보다 구현을 먼저 시작하는 승인은 아니다.
```
