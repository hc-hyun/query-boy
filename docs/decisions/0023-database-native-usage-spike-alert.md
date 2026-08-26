# ADR 0023: Database-Native Usage Spike Alert

Status: Parked research — outside ADR 0025 first-launch scope

Date: 2026-08-26

이 문서는 조사 기록으로 보존한다. [ADR 0025](0025-static-non-rls-first-launch.md)의 구현과 별개이고,
필수 COST base evidence와 별도 변경 승인이 생기기 전에는 active module baseline이 아니다.

## Priority And Base Boundary

이 문서는 `COST-04`의 별도 addendum다. [proposed ADR 0021](0021-database-native-cost-attribution.md)의
`COST-01-A`가 정확히 승인·구현되고 explicit-zero rollup, `accepted_samples`와 observation identity
evidence가 생긴 뒤에만 승인·구현할 수 있다. 이 문서를 승인해도 `ENC`, `TIME-03`, `COST-01-A` 또는
작업 우선순위가 자동 승인·완료되지 않는다. 현재 code/schema/config/wire를 바꾸지 않는다.

이 제안은 하나의 module interface가 아니다. Evaluator/store capability는 module interface, four-table
design은 persisted format, threshold/retention/status는 policy, operator route는 external API,
evaluation/cooldown은 lifecycle rule이다. 승인안은 이 범주별 영향을 함께 명시한다.

## Context

Base COST 제안은 target reader role의 `pg_stat_statements` lower-bound hourly aggregate를 만든다. 이것은
Query Man query별 CPU나 청구액이 아니고 reset, eviction과 sampling gap을 합성하지 않는다. 급증 alert는
이 불완전성을 숨기지 않으면서 multi-replica 중복, missing-to-zero, threshold flapping, event 보존과
operator 전달을 정확히 정해야 한다.

## Options

### `COST-04-A` — durable polling-only execution-time spike alert (recommended)

1. Primary/provider owner는 Control Plane이다. Runtime은 모든 fenced committed monitoring attempt
   (`baseline|accepted|discarded|failed`)의 usage transaction이 끝난 뒤 별도 evaluator transaction을
   호출하는 순서만, Delivery는 operator route validation/serialization만 소유한다. Source/monitoring
   mutation은 같은 source advisory transaction에서 alert transition을 함께 기록하고 policy mutation은
   policy pointer/state/event를 원자적으로 바꾼다. Evaluator 실패는 이미 committed usage나 source mutation을
   rollback하지 않고 다음 attempt가 같은 closed bucket을 재시도한다. Assurance는 migration,
   multi-replica dedup, lifecycle과 redaction을 조립한다. Source Catalog의 current `budget_profile`과
   Metadata revision은 base identity로만 소비한다. Guarded Query는 consumer가 아니며 alert가 admission,
   limit, source enablement나 query result를 바꾸지 않는다.
2. V1 signal은 하나뿐이다.

   ```text
   base_epoch = budget_profile + metadata_revision + definition_revision
       + observation_identity + observation_started_bucket
   policy_epoch = alert_policy_revision + alert_policy_state_version
       + policy_activated_bucket
   key = source_id + base_epoch + policy_epoch + epoch_started_bucket
   epoch_started_bucket = max(observation_started_bucket, policy_activated_bucket)
   signal = execution_time_us
   unit = microseconds attributed to a sample-count-qualified closed UTC bucket
   ```

   Caller, tenant, queryid, fingerprint, SQL과 target ID를 key/event에 넣지 않는다. Exact base observation
   identity가 바뀌거나 과거와 같은 digest가 다시 나타나도 COST-01의 새 `observation_started_at`을 UTC
   hour로 내린 값이 새 base epoch를 만든다. Metadata/profile/definition change도 새 observation start와
   seven-hour warm-up을 요구하며 metadata revision 간 값을 합산하지 않는다. Policy configure/rollback은
   revision이 재사용돼도 새 policy state version/activation bucket의 epoch다. Policy-state `activated_at`은
   policy mutation에서만 바뀌고 evaluator가 `next_event_id`/`updated_at`을 바꿔도 보존한다.
   Evaluation gap은 `epoch_started_bucket=H`로 더 늦은 cutoff를 만들고 backfill하지 않는다. Base
   rollup에는 observation digest가 없으므로 exact profile/metadata/definition이 일치하고
   `bucket_start > epoch_started_bucket`인 row만 선택해 transition hour 전체를 보수적으로 제외한다.
   같은 hour의 반복 transition도 그 hour를 계속 제외한다. Internal observation digest/start는
   state/event에 보존하지만 wire/log에는 내보내지 않는다.
3. Operator configure body는 exact 다음 세 field다.

   ```json
   {
     "absolute_execution_time_us": "60000000",
     "fire_multiplier_bps": 30000,
     "recovery_multiplier_bps": 15000
   }
   ```

   `absolute_execution_time_us`는 leading-zero 없는 decimal string `1..9223372036854775807`, fire는
   `10001..1000000`, recovery는 `0..fire-1` integer다. JSON number/float/exponent, duplicate/extra field를
   거부한다. Policy revision은 configure 당시 `budget_profile`과 COST definition revision에 binding되고
   둘 중 하나가 바뀌면 `binding_mismatch`로 평가를 멈춰 새 configure를 요구한다. Source generation은
   audit provenance지만 profile/definition이 같으면 policy 자체는 유지된다.
4. Evaluator는 Control DB `read_at` 한 값을 사용한다.

   ```text
   H = date_trunc('hour', read_at, 'UTC') - interval '1 hour'
   required buckets = H-6h ... H
   sample-count-qualified = exact epoch/profile/metadata/definition rollup row,
       bucket_start > epoch_started_bucket, and accepted_samples >= 10
   baseline = floor((sorted(previous six values)[2]
                   + sorted(previous six values)[3]) / 2)
   firing = v >= absolute
         AND numeric(v) * 10000 >= numeric(baseline) * fire_multiplier_bps
   recovery-hour = v < absolute
                OR numeric(v) * 10000
                   <= numeric(firing_baseline) * recovery_multiplier_bps
   ```

   All seven buckets must belong to the current observation/policy epoch and be sample-count-qualified. Missing,
   wrong identity or `accepted_samples<10` is never zero and produces waiting state. Baseline zero still needs the
   absolute threshold. Integer/numeric arithmetic only is allowed. Equality fires; recovery multiplier equality
   recovers. Two consecutive recovery-hours resolve. Missing/unavailable resets recovery streak to 0 without
   resolving. Resolve creates `cooldown_until=occurred_at+900 seconds`; equality at cooldown permits firing.
   The first evaluation therefore needs seven qualified closed hours after the transition hour.
   `accepted_samples>=10`은 accepted-attribution delta 개수 조건일 뿐 whole-hour/continuous coverage,
   first/last sample 위치, 최대 gap 또는 reset 없는 관측을 보장하지 않는다. Same-identity
   reset/deallocation/discard는 새 alert epoch를 만들지 않고 subsequent count heuristic만 다시 채운다.
   Continuous/reset-bounded coverage가 필요하면 COST-01에 별도 quality epoch/evidence를 승인해야 하며 이
   A의 4-table persisted design으로 가장하지 않는다.
5. A source advisory lock, current policy-state row lock and persisted `last_evaluated_bucket` serialize evaluation.
   Configure 또는 rollback이 새 policy activation을 만들면 current state를
   `pending/waiting/BASELINE_REQUIRED`, recovery 0, last-evaluated/evaluated/observed/baseline 전부 null로
   seed한다. 이 initial null은 evaluation이나 gap이 아니다. Committed `ready|waiting|unavailable` 모두 H와 `evaluated_at=read_at`을
   기록하고, 같은 H는 no-op, `H < last_evaluated_bucket`은 corrupted state/503이다. Alert transaction 자체가
   rollback된 경우만 같은 H를 재시도한다. `H > last_evaluated_bucket + 1 hour`이면 historical alert를
   backfill하지 않고 open firing을 정확히 한 번 `superseded/EVALUATION_GAP`으로 닫은 뒤
   `epoch_started_bucket=H`, pending/recovery 0으로 새 warm-up을 시작한다.

   Transition precedence는 policy disable/replace/binding change → observation epoch change → evaluation gap →
   source/monitor/base unavailable → missing/baseline wait → cooldown → ready fire/recovery다. Policy/observation
   transition도 open firing을 한 번 supersede하고 해당 transition UTC hour를 새 cutoff로 둔다. Base
   stale/unavailable, source/monitoring disabled와 target mismatch는 firing을 닫지 않고 evaluation unavailable로
   표시하며 recovery streak만 0으로 만든다. Waiting missing도 streak를 0으로 만든다.

   Cooldown은 event 발생 wall clock 기준이다. `read_at < cooldown_until`이면 그 H를
   `waiting/COOLDOWN_ACTIVE`로 finalize해 만료 뒤 같은 H를 다시 평가하지 않고, equality부터 다음 H가 fire할
   수 있다. Resolved event의 public baseline은 rolling current median이 아니라 frozen firing baseline이다.
   Superseded event의 public observed/baseline은 항상 null이며 policy/observation transition은 transition
   UTC hour, `EVALUATION_GAP`은 H를 event bucket으로 쓴다. One evaluation emits at most one event and one
   firing에는 terminal resolved/superseded event가 최대 하나다. Alert state never changes query
   readiness/health.
6. Lifecycle literals are exact.

   ```text
   alert_status = pending | normal | firing
   evaluation_status = ready | waiting | unavailable

   waiting reason = BASELINE_REQUIRED | MISSING_BUCKET | COOLDOWN_ACTIVE
   unavailable reason = POLICY_DISABLED | POLICY_BINDING_MISMATCH | SOURCE_DISABLED |
     MONITORING_DISABLED | TARGET_MISMATCH | OBSERVATION_EXPIRED |
     DATABASE_NATIVE_UNAVAILABLE | EVALUATION_OVERFLOW

   event_type/reason:
     firing/THRESHOLD_EXCEEDED
     resolved/RECOVERY_CONFIRMED
     superseded/POLICY_REPLACED | POLICY_DISABLED |
       POLICY_BINDING_CHANGED | OBSERVATION_IDENTITY_CHANGED | EVALUATION_GAP
   ```

   A firing event references itself as `firing_event_id`; resolved/superseded references the exact same
   policy-activation/base epoch의 open firing event. Repeated firing evaluations create no event. There is no
   acknowledgement/read receipt or GET side effect.
7. Base migration 6 뒤의 additive Control migration **7**은 다음 literal schema다. 지금은 승인 전이라 file을
   만들거나 실행하지 않는다.

   ```sql
   CREATE TABLE control.source_db_alert_policy_revisions (
     source_id text NOT NULL,
     alert_policy_revision bigint NOT NULL,
     configured_generation bigint NOT NULL,
     budget_profile text NOT NULL,
     definition_revision text NOT NULL,
     absolute_execution_time_us bigint NOT NULL,
     fire_multiplier_bps integer NOT NULL,
     recovery_multiplier_bps integer NOT NULL,
     created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
     PRIMARY KEY (source_id, alert_policy_revision),
     CONSTRAINT db_alert_policy_source_exists
       FOREIGN KEY (source_id, configured_generation)
       REFERENCES control.source_profile_revisions (source_id, generation)
       ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_policy_number_valid CHECK (
       alert_policy_revision > 0 AND configured_generation > 0
       AND absolute_execution_time_us > 0
       AND fire_multiplier_bps BETWEEN 10001 AND 1000000
       AND recovery_multiplier_bps >= 0
       AND recovery_multiplier_bps < fire_multiplier_bps
     ),
     CONSTRAINT db_alert_policy_binding_valid CHECK (
       budget_profile ~ '^[A-Za-z_][A-Za-z0-9_$]{0,62}$'
       AND definition_revision ~ '^sha256:[a-f0-9]{64}$'
     )
   );

   CREATE TRIGGER source_db_alert_policy_revisions_are_immutable
   BEFORE UPDATE OR DELETE ON control.source_db_alert_policy_revisions
   FOR EACH ROW EXECUTE FUNCTION control.reject_source_profile_revision_mutation();

   CREATE TABLE control.source_db_alert_policy_state (
     source_id text PRIMARY KEY,
     active_alert_policy_revision bigint NOT NULL,
     enabled boolean NOT NULL,
     alert_policy_state_version bigint NOT NULL,
     next_event_id bigint NOT NULL,
     activated_at timestamptz NOT NULL,
     updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
     CONSTRAINT db_alert_policy_state_source_exists
       FOREIGN KEY (source_id)
       REFERENCES control.active_source_profiles (source_id)
       ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_policy_state_revision_exists
       FOREIGN KEY (source_id, active_alert_policy_revision)
       REFERENCES control.source_db_alert_policy_revisions (
         source_id, alert_policy_revision
       ) ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_policy_state_number_valid CHECK (
       active_alert_policy_revision > 0
       AND alert_policy_state_version > 0 AND next_event_id > 0
       AND updated_at >= activated_at
     )
   );

   CREATE TABLE control.source_db_alert_events (
     source_id text NOT NULL,
     event_id bigint NOT NULL,
     alert_policy_revision bigint NOT NULL,
     alert_policy_state_version bigint NOT NULL,
     metadata_revision text NOT NULL,
     observation_identity text NOT NULL,
     observation_started_bucket timestamptz NOT NULL,
     epoch_started_bucket timestamptz NOT NULL,
     event_type text NOT NULL,
     reason_code text NOT NULL,
     bucket_start timestamptz NOT NULL,
     occurred_at timestamptz NOT NULL,
     observed_execution_time_us bigint,
     baseline_execution_time_us bigint,
     firing_baseline_execution_time_us bigint NOT NULL,
     firing_event_id bigint NOT NULL,
     firing_event_type text NOT NULL,
     PRIMARY KEY (source_id, event_id),
     CONSTRAINT db_alert_event_firing_target_unique
       UNIQUE (
         source_id, event_id, event_type,
         alert_policy_revision, alert_policy_state_version,
         metadata_revision, observation_identity,
         observation_started_bucket, epoch_started_bucket,
         firing_baseline_execution_time_us
       ),
     CONSTRAINT db_alert_event_policy_exists
       FOREIGN KEY (source_id, alert_policy_revision)
       REFERENCES control.source_db_alert_policy_revisions (
         source_id, alert_policy_revision
       ) ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_event_metadata_exists
       FOREIGN KEY (source_id, metadata_revision)
       REFERENCES control.metadata_snapshots (source_id, revision)
       ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_event_firing_exists
       FOREIGN KEY (
         source_id, firing_event_id, firing_event_type,
         alert_policy_revision, alert_policy_state_version,
         metadata_revision, observation_identity,
         observation_started_bucket, epoch_started_bucket,
         firing_baseline_execution_time_us
       ) REFERENCES control.source_db_alert_events (
         source_id, event_id, event_type,
         alert_policy_revision, alert_policy_state_version,
         metadata_revision, observation_identity,
         observation_started_bucket, epoch_started_bucket,
         firing_baseline_execution_time_us
       )
       DEFERRABLE INITIALLY DEFERRED,
     CONSTRAINT db_alert_event_number_valid CHECK (
       (
         event_id > 0 AND alert_policy_revision > 0
         AND alert_policy_state_version > 0
         AND metadata_revision ~ '^sha256:[a-f0-9]{64}$'
         AND observation_identity ~ '^sha256:[a-f0-9]{64}$'
         AND observation_started_bucket =
           pg_catalog.date_trunc(
             'hour', observation_started_bucket AT TIME ZONE 'UTC'
           ) AT TIME ZONE 'UTC'
         AND epoch_started_bucket =
           pg_catalog.date_trunc(
             'hour', epoch_started_bucket AT TIME ZONE 'UTC'
           ) AT TIME ZONE 'UTC'
         AND epoch_started_bucket >= observation_started_bucket
         AND bucket_start =
           pg_catalog.date_trunc('hour', bucket_start AT TIME ZONE 'UTC')
             AT TIME ZONE 'UTC'
         AND occurred_at >= bucket_start
         AND firing_event_type = 'firing'
         AND firing_baseline_execution_time_us >= 0
         AND (observed_execution_time_us IS NULL
           OR observed_execution_time_us >= 0)
         AND (baseline_execution_time_us IS NULL
           OR baseline_execution_time_us >= 0)
       ) IS TRUE
     ),
     CONSTRAINT db_alert_event_lifecycle_valid CHECK (
       (
         (event_type = 'firing' AND reason_code = 'THRESHOLD_EXCEEDED'
           AND firing_event_id = event_id
           AND observed_execution_time_us IS NOT NULL
           AND baseline_execution_time_us IS NOT NULL
           AND baseline_execution_time_us =
             firing_baseline_execution_time_us)
         OR (event_type = 'resolved' AND reason_code = 'RECOVERY_CONFIRMED'
           AND firing_event_id <> event_id
           AND observed_execution_time_us IS NOT NULL
           AND baseline_execution_time_us IS NOT NULL
           AND baseline_execution_time_us =
             firing_baseline_execution_time_us)
         OR (event_type = 'superseded'
           AND reason_code IN (
             'POLICY_REPLACED', 'POLICY_DISABLED', 'POLICY_BINDING_CHANGED',
             'OBSERVATION_IDENTITY_CHANGED', 'EVALUATION_GAP'
           ) AND firing_event_id <> event_id
           AND observed_execution_time_us IS NULL
           AND baseline_execution_time_us IS NULL)
       ) IS TRUE
     )
   );

   CREATE TRIGGER source_db_alert_events_are_immutable
   BEFORE UPDATE OR DELETE ON control.source_db_alert_events
   FOR EACH ROW EXECUTE FUNCTION control.reject_source_profile_revision_mutation();

   CREATE INDEX source_db_alert_events_read_idx
     ON control.source_db_alert_events (
       source_id, occurred_at DESC, event_id DESC
     );

   CREATE UNIQUE INDEX source_db_alert_events_one_terminal_idx
     ON control.source_db_alert_events (source_id, firing_event_id)
     WHERE event_type IN ('resolved', 'superseded');

   CREATE TABLE control.source_db_alert_states (
     source_id text PRIMARY KEY,
     alert_policy_revision bigint NOT NULL,
     alert_policy_state_version bigint NOT NULL,
     metadata_revision text NOT NULL,
     observation_identity text NOT NULL,
     observation_started_bucket timestamptz NOT NULL,
     epoch_started_bucket timestamptz NOT NULL,
     alert_status text NOT NULL,
     evaluation_status text NOT NULL,
     reason_code text,
     last_evaluated_bucket timestamptz,
     evaluated_at timestamptz,
     observed_execution_time_us bigint,
     baseline_execution_time_us bigint,
     firing_event_id bigint,
     firing_event_type text,
     firing_since timestamptz,
     firing_baseline_execution_time_us bigint,
     recovery_streak smallint NOT NULL,
     cooldown_until timestamptz,
     updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
     CONSTRAINT db_alert_state_policy_exists
       FOREIGN KEY (source_id, alert_policy_revision)
       REFERENCES control.source_db_alert_policy_revisions (
         source_id, alert_policy_revision
       ) ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_state_metadata_exists
       FOREIGN KEY (source_id, metadata_revision)
       REFERENCES control.metadata_snapshots (source_id, revision)
       ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_state_firing_event_exists
       FOREIGN KEY (
         source_id, firing_event_id, firing_event_type,
         alert_policy_revision, alert_policy_state_version,
         metadata_revision, observation_identity,
         observation_started_bucket, epoch_started_bucket,
         firing_baseline_execution_time_us
       ) REFERENCES control.source_db_alert_events (
         source_id, event_id, event_type,
         alert_policy_revision, alert_policy_state_version,
         metadata_revision, observation_identity,
         observation_started_bucket, epoch_started_bucket,
         firing_baseline_execution_time_us
       )
       ON UPDATE RESTRICT ON DELETE RESTRICT,
     CONSTRAINT db_alert_state_identity_valid CHECK (
       (
         alert_policy_revision > 0 AND alert_policy_state_version > 0
         AND metadata_revision ~ '^sha256:[a-f0-9]{64}$'
         AND observation_identity ~ '^sha256:[a-f0-9]{64}$'
         AND observation_started_bucket =
           pg_catalog.date_trunc(
             'hour', observation_started_bucket AT TIME ZONE 'UTC'
           ) AT TIME ZONE 'UTC'
         AND epoch_started_bucket =
           pg_catalog.date_trunc(
             'hour', epoch_started_bucket AT TIME ZONE 'UTC'
           ) AT TIME ZONE 'UTC'
         AND epoch_started_bucket >= observation_started_bucket
         AND recovery_streak BETWEEN 0 AND 1
       ) IS TRUE
     ),
     CONSTRAINT db_alert_state_evaluation_valid CHECK (
       (
         (evaluation_status = 'ready' AND reason_code IS NULL
           AND observed_execution_time_us IS NOT NULL
           AND baseline_execution_time_us IS NOT NULL)
         OR (evaluation_status = 'waiting'
           AND reason_code = 'BASELINE_REQUIRED'
           AND observed_execution_time_us IS NULL
           AND baseline_execution_time_us IS NULL)
         OR (evaluation_status = 'waiting'
           AND reason_code = 'MISSING_BUCKET'
           AND last_evaluated_bucket IS NOT NULL
           AND evaluated_at IS NOT NULL
           AND observed_execution_time_us IS NULL
           AND baseline_execution_time_us IS NULL)
         OR (evaluation_status = 'waiting'
           AND reason_code = 'COOLDOWN_ACTIVE'
           AND alert_status = 'normal'
           AND cooldown_until IS NOT NULL
           AND evaluated_at IS NOT NULL
           AND evaluated_at < cooldown_until
           AND observed_execution_time_us IS NOT NULL
           AND baseline_execution_time_us IS NOT NULL)
         OR (evaluation_status = 'unavailable' AND reason_code IN (
           'POLICY_DISABLED', 'POLICY_BINDING_MISMATCH', 'SOURCE_DISABLED',
           'MONITORING_DISABLED', 'TARGET_MISMATCH',
           'OBSERVATION_EXPIRED', 'DATABASE_NATIVE_UNAVAILABLE',
           'EVALUATION_OVERFLOW'
         ) AND observed_execution_time_us IS NULL
           AND baseline_execution_time_us IS NULL)
       ) IS TRUE
     ),
     CONSTRAINT db_alert_state_last_evaluation_valid CHECK (
       (
         (last_evaluated_bucket IS NULL AND evaluated_at IS NULL
           AND observed_execution_time_us IS NULL
           AND baseline_execution_time_us IS NULL
           AND alert_status = 'pending'
           AND evaluation_status = 'waiting'
           AND reason_code = 'BASELINE_REQUIRED')
         OR (last_evaluated_bucket IS NOT NULL AND evaluated_at IS NOT NULL
           AND ((observed_execution_time_us IS NULL
               AND baseline_execution_time_us IS NULL)
             OR (observed_execution_time_us IS NOT NULL
               AND observed_execution_time_us >= 0
               AND baseline_execution_time_us IS NOT NULL
               AND baseline_execution_time_us >= 0))
           AND last_evaluated_bucket =
             pg_catalog.date_trunc(
               'hour', last_evaluated_bucket AT TIME ZONE 'UTC'
             ) AT TIME ZONE 'UTC'
           AND evaluated_at >= last_evaluated_bucket + interval '1 hour')
       ) IS TRUE
     ),
     CONSTRAINT db_alert_state_firing_valid CHECK (
       (
         (alert_status = 'firing' AND firing_event_id IS NOT NULL
           AND firing_event_type = 'firing'
           AND firing_since IS NOT NULL
           AND firing_baseline_execution_time_us IS NOT NULL
           AND firing_baseline_execution_time_us >= 0
           AND recovery_streak BETWEEN 0 AND 1
           AND cooldown_until IS NULL)
         OR (((alert_status = 'pending' AND evaluation_status <> 'ready')
             OR alert_status = 'normal')
           AND firing_event_id IS NULL AND firing_event_type IS NULL
           AND firing_since IS NULL
           AND firing_baseline_execution_time_us IS NULL
           AND recovery_streak = 0)
       ) IS TRUE
     ),
     CONSTRAINT db_alert_state_cooldown_valid CHECK (
       (
         (cooldown_until IS NULL OR alert_status = 'normal')
         AND (
           reason_code IS DISTINCT FROM 'COOLDOWN_ACTIVE'
           OR (evaluation_status = 'waiting'
             AND alert_status = 'normal'
             AND cooldown_until IS NOT NULL
             AND evaluated_at IS NOT NULL
             AND evaluated_at < cooldown_until)
         )
       ) IS TRUE
     )
   );
   ```

   Migration 7은 existing receipt operation check에
   `configure_database_native_alert_policy|disable_database_native_alert_policy|
   rollback_database_native_alert_policy`만 additive로 더한다. 먼저 이 세 operation/result를 read-only
   decode하는 compatibility release를 전 fleet에 배포하고 migration 7, writer/route release 순서로
   진행하며 rollback target도 compatibility release다. Alert mutation은 source generation/state를 바꾸지
   않고 receipt의 existing resulting field/result에 current source 값을 보존하며 request HMAC은
   method/path/canonical body, common expected generation/state와 expected alert-policy version을 모두 포함한다.
   Writer grants는 policy revisions/events
   `SELECT,INSERT`, policy state/alert states `SELECT,INSERT,UPDATE`뿐이며 DELETE/TRUNCATE/DDL/schema CREATE나
   새 sequence/function grant는 없다. Existing receipt sequence `USAGE`는 유지한다. Migration 뒤 recovery
   scope는 base 18+alert 4=22 tables다. Events와 policy revisions은 immutable이고 age-based physical
   DELETE는 없다. Source advisory lock과 policy-state row lock 아래 active policy pointer/state version,
   alert state, event insert와 `next_event_id` increment를 한 transaction에서 CAS한다. Evaluator가
   `next_event_id`/`updated_at`을 바꿔도 policy `activated_at`은 바꾸지 않는다. Event threshold는 immutable
   policy revision을 join해 복원하며 internal observation digest와 firing baseline은 response allowlist 밖이다.
8. Operator surface는 다음 exact route뿐이다. Authentication/authorization은 path/query/body validation보다
   먼저이고 non-operator는 invalid input도 403이다.

   ```text
   GET /admin/sources/{source_id}/database-native-alerts
     ?limit=50&before_event_id=<positive-int>
   PUT /admin/sources/{source_id}/database-native-alert-policy
   DELETE /admin/sources/{source_id}/database-native-alert-policy
   POST /admin/sources/{source_id}/database-native-alert-policy/rollback/{alert_policy_revision}
   ```

   GET limit default 50/range 1..100, `before_event_id` exclusive descending keyset이며 duplicate/unknown query를
   거부한다. Event order는 `event_id DESC`다. Current state는 age와 무관하게 반환하고 events는 Control DB clock의 inclusive
   `[read_at-90 days, read_at]`만 최대 limit까지 반환한다. `next_before_event_id`는 추가 row가 있을 때 마지막
   returned ID, 아니면 null이다. Exact response는 다음이다.

   ```text
   source_id, read_at
   policy: status=not_configured|enabled|disabled|binding_mismatch,
     active_alert_policy_revision, alert_policy_state_version,
     budget_profile, definition_revision,
     absolute_execution_time_us, fire_multiplier_bps, recovery_multiplier_bps,
     baseline_hours=6, minimum_accepted_samples=10,
     recovery_hours=2, cooldown_seconds=900, event_retention_days=90
   current: null | {
     alert_status, evaluation_status, reason_code,
     evaluated_bucket_start, evaluated_at,
     observed_execution_time_us, baseline_execution_time_us,
     firing_event_id, firing_since, recovery_streak, cooldown_until
   }
   event_window_start, event_window_end, events, next_before_event_id

   event: event_id, policy_revision, event_type, reason_code,
     bucket_start, occurred_at,
     observed_execution_time_us, baseline_execution_time_us,
     absolute_execution_time_us, fire_multiplier_bps,
     recovery_multiplier_bps, firing_event_id
   ```

   위에 이름이 나온 top-level/policy/current/event key는 object가 존재하면 항상 JSON에 존재하고 nullable
   field도 생략하지 않고 JSON `null`로 보낸다. `current=null` 자체만 object 부재를 뜻한다. Current object의
   exact nullability는 다음과 같다. `reason_code`는 `ready`일 때만 null이다. `evaluated_bucket_start`와
   `evaluated_at`은 함께 null이거나 함께 non-null이고, null은 configure 또는 rollback으로 새 policy
   activation을 만든 직후 아직 한 번도 평가하지 않은 `pending/waiting/BASELINE_REQUIRED` state에서만
   허용한다. `observed_execution_time_us`와
   `baseline_execution_time_us`는 `ready` 또는 `waiting/COOLDOWN_ACTIVE`일 때 둘 다 non-null이고 다른
   waiting/unavailable에서는 둘 다 null이다. `firing_event_id`와 `firing_since`는 `alert_status=firing`일
   때만 둘 다 non-null이다. `cooldown_until`은 pending/firing에서 null이고 normal에서는 null 또는 non-null이며,
   `COOLDOWN_ACTIVE`이면 반드시 non-null이고 `evaluated_at < cooldown_until`이다. `recovery_streak`은 항상
   JSON integer 0 또는 1이고 non-firing에서는 0이다.

   Event object도 모든 listed key를 항상 보낸다. Firing은 `firing_event_id=event_id`, resolved/superseded는
   원 firing event ID를 가진다. Firing/resolved event의 observed/baseline은 둘 다 non-null이고 resolved
   baseline은 frozen firing baseline이다. Superseded event의 두 값은 둘 다 null이다. Policy threshold와
   multiplier, event ID/time/type/reason은 event object에서 non-null이다. `next_before_event_id`만 다음 page가
   없으면 null이고 있으면 마지막 returned positive event ID다.

   모든 execution-time 값은 null 또는 decimal string이다. `not_configured`는 active revision과 모든 policy
   value가 null, alert version 0, current null, events empty다. `disabled`는 retained pointer와 policy value를
   반환하지만 current는 null이고 historical event는 계속 보인다. `enabled|binding_mismatch`는 pointer/value와
   current가 모두 non-null이며 mismatch current는 `unavailable/POLICY_BINDING_MISMATCH`다. Open firing이
   90일보다 오래되면 `current.firing_event_id`가 현재 events page/window에 없어도 정상이다. 90일은 inclusive
   logical visibility일 뿐 physical delete가 없어 capacity/archive가 계속 증가한다. Internal policy activation,
   metadata/observation identity/start와 firing baseline은 반환하지 않는다. Malformed persisted
   state/cardinality는 정상 status로 축약하지 않고 `SOURCE_CONTROL_UNAVAILABLE` 503이다.
9. Mutation은 current common headers와 `X-Expected-Alert-Policy-State-Version`을 요구한다. Initial configure는
   alert version 0이고 success result는
   `{status,source_id,generation,state_version,alert_policy_revision,alert_policy_state_version}`다. Status는
   `alert_policy_configured|alert_policy_disabled|alert_policy_rolled_back`이다. Same key/same canonical
   request만 replay하고 same payload/new key configure는 새 immutable revision이다. PUT만 exact JSON
   Content-Type/body를 요구하고 existing 1 MiB cap을 적용한다. DELETE/rollback은 zero-length body, query 없음,
   rollback path `1..CONTROL_SEQUENCE_MAX`이며 duplicate/extra query/header를 거부한다. Configure는
   current COST definition/profile과 stable current base observation identity가 있어야 하며 monitoring
   not-configured/disabled 또는 baseline 전이면 `SOURCE_ALERT_POLICY_CONFLICT`다. Disable은 pointer를
   보존하고 rollback은 current profile/definition에 맞는 historical revision만 enabled로 선택한다.

   | Current state | Operation | Result |
   |---|---|---|
   | not_configured/version 0 | configure | revision 1, enabled, state version 1, pending epoch |
   | enabled, disabled or binding_mismatch | configure | next revision, enabled, state version +1, old firing superseded `POLICY_REPLACED` |
   | enabled or binding_mismatch | disable | pointer retained, disabled, state version +1, old firing superseded `POLICY_DISABLED` |
   | not_configured or disabled | disable | conflict |
   | enabled | rollback to current revision | conflict |
   | enabled or binding_mismatch | rollback to different matching revision | selected revision, enabled, state version +1, `POLICY_REPLACED` |
   | disabled | rollback to any matching revision including pointer | selected revision, enabled, state version +1 |

   Every success increments alert policy state version exactly once, stores policy `activated_at` from one Control DB
   clock and starts a new warm-up cutoff. Source generation/state는 바꾸지 않고 success result에 current 값을
   그대로 반환한다. Rollback target이 없으면 404, 존재하지만 current profile/definition과 맞지 않거나
   current base identity/CAS가 바뀌면 conflict다.
   `ALERT_POLICY_REVISION_NOT_FOUND` 404 message는
   `"The requested alert policy revision was not found."`, `SOURCE_ALERT_POLICY_CONFLICT` 409 message는
   `"The source alert policy state changed; retry with current state."`다. Body validation은 existing
   `SOURCE_VALIDATION_FAILED` 400, generation/state/idempotency error는 existing code를 쓴다. 400/409만
   terminal rejection receipt이며 auth, 404와 503은 receipt를 만들지 않는다.
10. Delivery backend는 operator polling 하나뿐이다. Webhook/email/push/retry queue/destination credential,
    query-facing endpoint/MCP tool, `/admin/metrics` source label과 notification worker를 만들지 않는다.
    SQL, parameter, queryid, target/monitor credential, caller/tenant와 raw DB error를 state/event/log/response에
    넣지 않는다. Alert는 lower-bound execution-time signal이며 billing, chargeback, CPU 또는 Query Man-only
    usage라고 표시하지 않는다.

### `COST-04-B` — external notification

Webhook/email/push destination, secret, SSRF policy, signing, retry/backoff/dead-letter와 delivery receipt retention이
모두 미정인 direction-only다. ID 선택이나 포괄적 승인으로 구현하지 않는다.

### `COST-04-C` — explicit deferral

Base COST rollup/status만 제공하고 threshold, alert state/event와 route를 만들지 않는다. 가장 작은 defer지만
`COST-04`는 완료되지 않는다.

## Rollout And Rollback

1. ENC/TIME gate 뒤 exact `COST-01-A`를 승인·구현하고 base migration 6과 source projection을 검증한다.
2. 최소 7시간의 explicit-zero/accepted-sample/identity evidence로 count-based threshold feasibility를
   확인하되 이를 continuous-hour coverage라고 부르지 않는다.
3. 이 A를 별도로 승인한 뒤 세 alert receipt를 read-only decode하는 compatibility release를 먼저 전
   fleet에 배포하고 migration 7, writer/route release 순서로 적용한다. 모든 source는 alert
   `not_configured`로 시작한다.
4. Source 하나에 configure해 seven-hour warm-up, firing/recovery/cooldown과 two-replica dedup을 검증한 뒤
   확대한다.
5. Code rollback은 compatibility release까지만 허용하며 evaluator/routes를 제거하고 base collection과 migration ledger/policy/state/event data를
   보존한다. 재배포 뒤 gap을 backfill하지 않고 새 baseline을 요구한다. Table/data를 drop하거나 age-delete하지
   않는다.

## Verification

- Six-hour even median floor, baseline zero, absolute/multiplier equality와 numeric overflow boundary
- Seven bucket `accepted_samples>=10`, missing/explicit-zero/sample-count qualification과 whole-hour coverage를
  주장하지 않는 한계
- Metadata/profile/definition/observation/policy activation version, same-revision reactivation, A→B→A와
  transition-hour exclusion
- Two-hour recovery, streak reset, 900-second equality와 cooldown refire
- Policy replace/disable/binding/identity/gap superseded event와 firing preservation on unavailable
- Two replica same-H exactly-once, ready/waiting/unavailable advance, gap/no-backfill, rollback-only retry와
  evaluator failure가 usage commit을 rollback하지 않음
- Migration 7 fresh/upgrade, self-firing insert, cross-policy/type/epoch terminal rejection, one-terminal unique,
  NULL/state-matrix corruption rejection, exact PK/FK/check/index/grant, immutable trigger와 22-table recovery
- Compatibility-reader-first rollout, configure/disable/rollback transition/CAS/idempotency/receipt,
  404/503 nonterminal과 exact public message
- Inclusive 90-day boundary, exclusive event-ID pagination, no physical deletion과 malformed-state 503
- Operator-first auth, exact shape, decimal-string encoding과 SQL/credential/identity redaction
- No query/MCP/metrics/admission/source-state effect and schema/data-preserving rollback
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`, `uv run pytest -m integration`

## Approval Boundary

이 문서는 정확한 제안일 뿐 현재 승인 baseline이나 구현이 아니다. 승인하려면 해당되는 모든 변경
범주와 영향을 정확히 지정해야 한다. Base evidence 뒤 A를 선택할 경우 아래 전체를
정확히 승인해야 한다. B는 delivery API를 다시 제시해야 하고 C는 completion defer다.

```text
COST-04-A를 source/profile/metadata/definition/base observation identity+start와 alert policy
revision+state-version+activation에 묶인 execution_time_us closed-bucket lower-bound spike signal로 승인한다.
Transition hour를 제외하고 매 bucket accepted_samples>=10인 7개 bucket과 이전 6-bucket median-floor
baseline을 쓰되 이 count 조건은 whole-hour/continuous/reset-free coverage가 아니며 same-identity
reset/discard가 epoch를 끊지 않는다는 한계를 승인한다. Absolute decimal threshold와 integer bps
fire/recovery, two-hour recovery, wall-clock 900-second cooldown, missing-to-zero 금지, committed bucket advance,
gap no-backfill과 firing/resolved/superseded one-terminal exact lifecycle을 승인한다. Control migration 7의
literal 4-table policy-activation/observation snapshot, composite firing FK, one-terminal index, NULL/state-matrix
check, immutability/least-privilege schema, 22-table recovery와 compatibility-reader-first rollout,
90-day logical/no-physical-delete event와 unbounded capacity 한계, exact operator
GET/configure/disable/rollback transition/CAS/receipt/error/decimal-string wire를 승인한다. Delivery는
operator polling only이며 webhook/email/push/retry/ack, query/MCP/metric label, automatic admission/disable,
billing/chargeback과 sensitive identifiers는 제외한다. 이는 COST-01-A나 ENC/TIME 우선순위를 승인하지 않고
base evidence 전 구현을 허용하지 않으며 rollback은 schema/data를 보존한다.
```
