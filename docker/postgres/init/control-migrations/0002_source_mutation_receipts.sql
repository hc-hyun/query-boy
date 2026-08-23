CREATE TABLE control.source_mutation_receipts (
  event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idempotency_key uuid NOT NULL UNIQUE,
  request_hash text NOT NULL,
  operation text NOT NULL,
  source_id text NOT NULL,
  actor text NOT NULL,
  reason text NOT NULL,
  expected_generation bigint NOT NULL,
  expected_state_version bigint NOT NULL,
  resulting_generation bigint,
  resulting_state_version bigint,
  outcome text NOT NULL,
  http_status smallint NOT NULL,
  error_code text,
  result jsonb NOT NULL,
  recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT source_mutation_receipts_request_hash_valid
    CHECK (request_hash ~ '^hmac-sha256:[a-f0-9]{64}$'),
  CONSTRAINT source_mutation_receipts_operation_valid
    CHECK (operation IN (
      'publish_source',
      'rotate_credential',
      'publish_verified_query',
      'rollback_source',
      'resume_metadata_publish',
      'deactivate_source'
    )),
  CONSTRAINT source_mutation_receipts_source_id_valid
    CHECK (source_id ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$' AND length(source_id) <= 80),
  CONSTRAINT source_mutation_receipts_actor_valid
    CHECK (actor ~ '^[A-Za-z][A-Za-z0-9_-]{0,79}$'),
  CONSTRAINT source_mutation_receipts_reason_valid
    CHECK (reason ~ '^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$'),
  CONSTRAINT source_mutation_receipts_expected_state_valid
    CHECK (expected_generation >= 0 AND expected_state_version >= 0),
  CONSTRAINT source_mutation_receipts_resulting_state_valid
    CHECK (
      (resulting_generation IS NULL AND resulting_state_version IS NULL)
      OR (resulting_generation >= 0 AND resulting_state_version >= 0)
    ),
  CONSTRAINT source_mutation_receipts_error_code_valid
    CHECK (
      error_code IS NULL
      OR error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'
    ),
  CONSTRAINT source_mutation_receipts_result_valid
    CHECK (
      jsonb_typeof(result) = 'object'
      AND octet_length(result::text) <= 8192
    ),
  CONSTRAINT source_mutation_receipts_outcome_valid
    CHECK (
      (
        outcome = 'succeeded'
        AND resulting_generation IS NOT NULL
        AND resulting_state_version IS NOT NULL
        AND http_status = 200
        AND error_code IS NULL
      )
      OR (
        outcome = 'rejected'
        AND resulting_generation IS NULL
        AND resulting_state_version IS NULL
        AND http_status IN (400, 409)
        AND error_code IS NOT NULL
      )
    )
);

CREATE INDEX source_mutation_receipts_source_id_event_id_idx
  ON control.source_mutation_receipts (source_id, event_id DESC);

CREATE TRIGGER source_mutation_receipts_are_immutable
BEFORE UPDATE OR DELETE ON control.source_mutation_receipts
FOR EACH ROW EXECUTE FUNCTION control.reject_source_profile_revision_mutation();

COMMENT ON TABLE control.source_mutation_receipts IS
  'Immutable idempotency receipts and append-only source lifecycle audit events.';
