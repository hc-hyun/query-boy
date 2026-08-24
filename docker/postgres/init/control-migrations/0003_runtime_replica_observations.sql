CREATE TABLE control.runtime_replicas (
  replica_id text PRIMARY KEY,
  incarnation bigint NOT NULL DEFAULT 1,
  heartbeat_interval_ms integer NOT NULL,
  report_reason_code text,
  observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT runtime_replicas_identity_unique
    UNIQUE (replica_id, incarnation),
  CONSTRAINT runtime_replicas_replica_id_valid
    CHECK (
      replica_id ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$'
      AND length(replica_id) <= 80
    ),
  CONSTRAINT runtime_replicas_incarnation_valid
    CHECK (incarnation > 0),
  CONSTRAINT runtime_replicas_heartbeat_interval_valid
    CHECK (heartbeat_interval_ms BETWEEN 5000 AND 300000),
  CONSTRAINT runtime_replicas_report_reason_valid
    CHECK (
      report_reason_code IS NULL
      OR report_reason_code = 'CONTROL_SCAN_FAILED'
    )
);

CREATE TABLE control.runtime_source_observations (
  replica_id text NOT NULL,
  incarnation bigint NOT NULL,
  source_id text NOT NULL,
  applied_generation bigint,
  applied_state_version bigint,
  applied_enabled boolean,
  applied_metadata_revision text,
  source_health text,
  reason_code text,
  PRIMARY KEY (replica_id, source_id),
  CONSTRAINT runtime_source_observations_replica_exists
    FOREIGN KEY (replica_id, incarnation)
    REFERENCES control.runtime_replicas (replica_id, incarnation)
    ON UPDATE CASCADE
    ON DELETE RESTRICT,
  CONSTRAINT runtime_source_observations_source_exists
    FOREIGN KEY (source_id)
    REFERENCES control.active_source_profiles (source_id)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT runtime_source_observations_generation_exists
    FOREIGN KEY (source_id, applied_generation)
    REFERENCES control.source_profile_revisions (source_id, generation)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT runtime_source_observations_metadata_exists
    FOREIGN KEY (source_id, applied_metadata_revision)
    REFERENCES control.metadata_snapshots (source_id, revision)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT runtime_source_observations_applied_state_valid
    CHECK (
      (
        applied_generation IS NULL
        AND applied_state_version IS NULL
        AND applied_enabled IS NULL
      )
      OR (
        applied_generation > 0
        AND applied_state_version > 0
        AND applied_enabled IS NOT NULL
      )
    ),
  CONSTRAINT runtime_source_observations_metadata_state_valid
    CHECK (
      (
        applied_generation IS NOT NULL
        AND applied_enabled IS DISTINCT FROM false
      )
      OR applied_metadata_revision IS NULL
    ),
  CONSTRAINT runtime_source_observations_source_health_valid
    CHECK (
      source_health IS NULL
      OR source_health IN ('initializing', 'healthy', 'stale', 'unavailable')
    ),
  CONSTRAINT runtime_source_observations_reason_valid
    CHECK (
      reason_code IS NULL
      OR reason_code IN (
        'RUNTIME_VALIDATION_REJECTED',
        'RUNTIME_APPLY_FAILED',
        'METADATA_PROBE_FAILED'
      )
    )
);

CREATE INDEX runtime_source_observations_source_replica_idx
  ON control.runtime_source_observations (source_id, replica_id);

CREATE INDEX runtime_replicas_replica_id_c_idx
  ON control.runtime_replicas (replica_id COLLATE "C");

COMMENT ON TABLE control.runtime_replicas IS
  'Latest DB-clock heartbeat for each stable managed runtime replica.';
COMMENT ON TABLE control.runtime_source_observations IS
  'Latest sanitized applied source and metadata state reported by each runtime replica.';
