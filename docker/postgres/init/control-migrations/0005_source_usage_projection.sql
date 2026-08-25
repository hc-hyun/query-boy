CREATE TABLE control.source_resource_observation_attempts (
  source_id text PRIMARY KEY,
  generation bigint NOT NULL,
  last_attempt_at timestamptz NOT NULL,
  last_attempt_outcome text NOT NULL,
  last_attempt_reason_code text,
  last_success_at timestamptz,
  last_success_has_representative boolean,
  CONSTRAINT source_resource_observation_attempts_generation_exists
    FOREIGN KEY (source_id, generation)
    REFERENCES control.source_profile_revisions (source_id, generation)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT source_resource_observation_attempts_outcome_valid
    CHECK (
      (
        last_attempt_outcome = 'succeeded'
        AND last_attempt_reason_code IS NULL
        AND last_success_at = last_attempt_at
        AND last_success_has_representative IS NOT NULL
      )
      OR
      (
        last_attempt_outcome = 'failed'
        AND last_attempt_reason_code IS NOT NULL
        AND last_attempt_reason_code IN (
          'METADATA_UNAVAILABLE',
          'RESOURCE_READ_FAILED'
        )
      )
    ),
  CONSTRAINT source_resource_observation_attempts_success_valid
    CHECK (
      (
        last_success_at IS NULL
        AND last_success_has_representative IS NULL
      )
      OR
      (
        last_success_at IS NOT NULL
        AND last_success_has_representative IS NOT NULL
        AND last_success_at <= last_attempt_at
      )
    )
);

COMMENT ON TABLE control.source_resource_observation_attempts IS
  'Latest bounded resource collection attempt and current-generation last success.';
