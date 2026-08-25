CREATE TABLE control.source_resource_observations (
  source_id text NOT NULL,
  metric text NOT NULL,
  unit text NOT NULL,
  method text NOT NULL,
  definition_revision text NOT NULL,
  value bigint NOT NULL,
  metadata_revision text NOT NULL,
  sample_bucket_start timestamptz NOT NULL,
  observed_at timestamptz NOT NULL,
  fresh_until timestamptz NOT NULL,
  previous_value bigint,
  previous_metadata_revision text,
  previous_sample_bucket_start timestamptz,
  previous_observed_at timestamptz,
  previous_fresh_until timestamptz,
  PRIMARY KEY (source_id, metric),
  CONSTRAINT source_resource_observations_source_exists
    FOREIGN KEY (source_id)
    REFERENCES control.active_source_profiles (source_id)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT source_resource_observations_metadata_exists
    FOREIGN KEY (source_id, metadata_revision)
    REFERENCES control.metadata_snapshots (source_id, revision)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT source_resource_observations_previous_metadata_exists
    FOREIGN KEY (source_id, previous_metadata_revision)
    REFERENCES control.metadata_snapshots (source_id, revision)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT source_resource_observations_metric_valid
    CHECK (
      (metric = 'representative_records'
        AND unit = 'rows'
        AND method = 'postgres_catalog_estimate')
      OR
      (metric IN ('table_bytes', 'index_bytes', 'total_storage_bytes')
        AND unit = 'bytes'
        AND method = 'postgres_relation_size')
    ),
  CONSTRAINT source_resource_observations_definition_valid
    CHECK (definition_revision ~ '^sha256:[a-f0-9]{64}$'),
  CONSTRAINT source_resource_observations_value_valid
    CHECK (value >= 0),
  CONSTRAINT source_resource_observations_bucket_valid
    CHECK (
      sample_bucket_start =
        pg_catalog.date_trunc('day', sample_bucket_start AT TIME ZONE 'UTC')
          AT TIME ZONE 'UTC'
    ),
  CONSTRAINT source_resource_observations_freshness_valid
    CHECK (fresh_until = observed_at + interval '72 hours'),
  CONSTRAINT source_resource_observations_previous_valid
    CHECK (
      (
        previous_value IS NULL
        AND previous_metadata_revision IS NULL
        AND previous_sample_bucket_start IS NULL
        AND previous_observed_at IS NULL
        AND previous_fresh_until IS NULL
      )
      OR
      (
        previous_value >= 0
        AND previous_metadata_revision IS NOT NULL
        AND previous_sample_bucket_start =
          pg_catalog.date_trunc(
            'day', previous_sample_bucket_start AT TIME ZONE 'UTC'
          ) AT TIME ZONE 'UTC'
        AND previous_sample_bucket_start < sample_bucket_start
        AND previous_observed_at IS NOT NULL
        AND previous_fresh_until = previous_observed_at + interval '72 hours'
        AND previous_observed_at <= observed_at
      )
    )
);

CREATE TABLE control.gateway_usage_rollups (
  source_id text NOT NULL,
  budget_profile text NOT NULL,
  metadata_revision text NOT NULL,
  definition_revision text NOT NULL,
  bucket_start timestamptz NOT NULL,
  query_count bigint NOT NULL DEFAULT 0,
  success_count bigint NOT NULL DEFAULT 0,
  rejected_count bigint NOT NULL DEFAULT 0,
  timeout_count bigint NOT NULL DEFAULT 0,
  overloaded_count bigint NOT NULL DEFAULT 0,
  cancelled_count bigint NOT NULL DEFAULT 0,
  failed_count bigint NOT NULL DEFAULT 0,
  queue_ms_sum bigint NOT NULL DEFAULT 0,
  elapsed_ms_sum bigint NOT NULL DEFAULT 0,
  returned_rows_sum bigint NOT NULL DEFAULT 0,
  result_bytes_sum bigint NOT NULL DEFAULT 0,
  truncated_count bigint NOT NULL DEFAULT 0,
  observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (
    source_id,
    budget_profile,
    metadata_revision,
    definition_revision,
    bucket_start
  ),
  CONSTRAINT gateway_usage_rollups_source_exists
    FOREIGN KEY (source_id)
    REFERENCES control.active_source_profiles (source_id)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT gateway_usage_rollups_metadata_exists
    FOREIGN KEY (source_id, metadata_revision)
    REFERENCES control.metadata_snapshots (source_id, revision)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT gateway_usage_rollups_budget_profile_valid
    CHECK (
      budget_profile ~ '^[A-Za-z_][A-Za-z0-9_$]{0,62}$'
    ),
  CONSTRAINT gateway_usage_rollups_definition_valid
    CHECK (definition_revision ~ '^sha256:[a-f0-9]{64}$'),
  CONSTRAINT gateway_usage_rollups_bucket_valid
    CHECK (
      bucket_start =
        pg_catalog.date_trunc('hour', bucket_start AT TIME ZONE 'UTC')
          AT TIME ZONE 'UTC'
    ),
  CONSTRAINT gateway_usage_rollups_values_valid
    CHECK (
      query_count >= 0
      AND success_count >= 0
      AND rejected_count >= 0
      AND timeout_count >= 0
      AND overloaded_count >= 0
      AND cancelled_count >= 0
      AND failed_count >= 0
      AND queue_ms_sum >= 0
      AND elapsed_ms_sum >= 0
      AND returned_rows_sum >= 0
      AND result_bytes_sum >= 0
      AND truncated_count >= 0
      AND truncated_count <= success_count
      AND (
        success_count > 0
        OR (
          queue_ms_sum = 0
          AND elapsed_ms_sum = 0
          AND returned_rows_sum = 0
          AND result_bytes_sum = 0
          AND truncated_count = 0
        )
      )
      AND query_count = success_count + rejected_count + timeout_count
        + overloaded_count + cancelled_count + failed_count
    )
);

CREATE INDEX gateway_usage_rollups_source_bucket_idx
  ON control.gateway_usage_rollups (source_id, bucket_start DESC);

CREATE INDEX gateway_usage_rollups_bucket_idx
  ON control.gateway_usage_rollups (bucket_start);

CREATE TABLE control.gateway_usage_report_cursors (
  replica_id text PRIMARY KEY,
  incarnation bigint NOT NULL,
  last_sequence bigint NOT NULL,
  last_payload_hash text NOT NULL,
  observed_at timestamptz NOT NULL,
  fresh_until timestamptz NOT NULL,
  CONSTRAINT gateway_usage_report_cursors_replica_exists
    FOREIGN KEY (replica_id)
    REFERENCES control.runtime_replicas (replica_id)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT gateway_usage_report_cursors_replica_id_valid
    CHECK (
      replica_id ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'
      AND length(replica_id) <= 80
    ),
  CONSTRAINT gateway_usage_report_cursors_incarnation_valid
    CHECK (incarnation > 0),
  CONSTRAINT gateway_usage_report_cursors_sequence_valid
    CHECK (last_sequence > 0),
  CONSTRAINT gateway_usage_report_cursors_payload_hash_valid
    CHECK (last_payload_hash ~ '^sha256:[a-f0-9]{64}$'),
  CONSTRAINT gateway_usage_report_cursors_freshness_valid
    CHECK (fresh_until = observed_at + interval '180 seconds')
);

COMMENT ON TABLE control.source_resource_observations IS
  'Latest and previous comparable bounded source resource observations.';
COMMENT ON TABLE control.gateway_usage_rollups IS
  'Bounded lower-bound hourly gateway terminal usage grouped by trusted source profile revision.';
COMMENT ON TABLE control.gateway_usage_report_cursors IS
  'Fenced idempotency and DB-clock freshness cursor for each managed gateway usage reporter.';
