CREATE SCHEMA IF NOT EXISTS control;

CREATE TABLE IF NOT EXISTS control.metadata_snapshots (
  source_id text NOT NULL,
  revision text NOT NULL,
  snapshot jsonb NOT NULL,
  published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (source_id, revision),
  CONSTRAINT metadata_snapshots_source_id_valid
    CHECK (source_id ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$' AND length(source_id) <= 80),
  CONSTRAINT metadata_snapshots_revision_valid
    CHECK (revision ~ '^sha256:[a-f0-9]{64}$'),
  CONSTRAINT metadata_snapshots_document_valid
    CHECK (jsonb_typeof(snapshot) = 'object')
);

CREATE TABLE IF NOT EXISTS control.active_metadata_revisions (
  source_id text PRIMARY KEY,
  revision text NOT NULL,
  pinned boolean NOT NULL DEFAULT false,
  activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT active_metadata_revision_exists
    FOREIGN KEY (source_id, revision)
    REFERENCES control.metadata_snapshots (source_id, revision)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS control.source_profile_revisions (
  source_id text NOT NULL,
  generation bigint NOT NULL CHECK (generation > 0),
  manifest jsonb NOT NULL,
  secret_nonce bytea NOT NULL,
  secret_ciphertext bytea NOT NULL,
  metadata_revision text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (source_id, generation),
  CONSTRAINT source_profile_revisions_source_id_valid
    CHECK (source_id ~ '^[a-z0-9]+(?:-[a-z0-9]+)*$' AND length(source_id) <= 80),
  CONSTRAINT source_profile_revisions_manifest_valid
    CHECK (jsonb_typeof(manifest) = 'object'),
  CONSTRAINT source_profile_revisions_secret_nonce_valid
    CHECK (octet_length(secret_nonce) = 12),
  CONSTRAINT source_profile_revisions_secret_ciphertext_valid
    CHECK (octet_length(secret_ciphertext) >= 17),
  CONSTRAINT source_profile_revisions_metadata_revision_exists
    FOREIGN KEY (source_id, metadata_revision)
    REFERENCES control.metadata_snapshots (source_id, revision)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS control.active_source_profiles (
  source_id text PRIMARY KEY,
  generation bigint NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  state_version bigint NOT NULL DEFAULT 1,
  activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT active_source_profiles_state_version_valid
    CHECK (state_version > 0),
  CONSTRAINT active_source_profile_revision_exists
    FOREIGN KEY (source_id, generation)
    REFERENCES control.source_profile_revisions (source_id, generation)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS control.verified_query_contracts (
  source_id text NOT NULL,
  query_id text NOT NULL,
  metadata_revision text NOT NULL,
  question text NOT NULL,
  relations jsonb NOT NULL,
  sql text NOT NULL,
  expected jsonb NOT NULL,
  published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  PRIMARY KEY (source_id, query_id, metadata_revision),
  CONSTRAINT verified_query_contract_source_exists
    FOREIGN KEY (source_id, metadata_revision)
    REFERENCES control.metadata_snapshots (source_id, revision)
    ON UPDATE RESTRICT
    ON DELETE RESTRICT,
  CONSTRAINT verified_query_contract_id_valid
    CHECK (query_id ~ '^[a-z][a-z0-9-]{0,99}$'),
  CONSTRAINT verified_query_contract_question_valid
    CHECK (length(question) BETWEEN 1 AND 2000),
  CONSTRAINT verified_query_contract_relations_valid
    CHECK (jsonb_typeof(relations) = 'array' AND jsonb_array_length(relations) > 0),
  CONSTRAINT verified_query_contract_sql_valid
    CHECK (length(sql) BETWEEN 1 AND 100000),
  CONSTRAINT verified_query_contract_expected_valid
    CHECK (jsonb_typeof(expected) = 'object')
);

-- These ALTER statements adopt the exact unversioned baseline without deleting its history.
ALTER TABLE control.active_metadata_revisions
  ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;

ALTER TABLE control.active_source_profiles
  ADD COLUMN IF NOT EXISTS state_version bigint NOT NULL DEFAULT 1;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_constraint
    WHERE conname = 'active_source_profiles_state_version_valid'
      AND conrelid = 'control.active_source_profiles'::regclass
  ) THEN
    ALTER TABLE control.active_source_profiles
      ADD CONSTRAINT active_source_profiles_state_version_valid
      CHECK (state_version > 0);
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION control.reject_metadata_snapshot_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'metadata snapshots are immutable';
END;
$$;

DROP TRIGGER IF EXISTS metadata_snapshots_are_immutable
  ON control.metadata_snapshots;
CREATE TRIGGER metadata_snapshots_are_immutable
BEFORE UPDATE OR DELETE ON control.metadata_snapshots
FOR EACH ROW EXECUTE FUNCTION control.reject_metadata_snapshot_mutation();

CREATE OR REPLACE FUNCTION control.reject_source_profile_revision_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
  RAISE EXCEPTION 'source profile revisions are immutable';
END;
$$;

DROP TRIGGER IF EXISTS source_profile_revisions_are_immutable
  ON control.source_profile_revisions;
CREATE TRIGGER source_profile_revisions_are_immutable
BEFORE UPDATE OR DELETE ON control.source_profile_revisions
FOR EACH ROW EXECUTE FUNCTION control.reject_source_profile_revision_mutation();

DROP TRIGGER IF EXISTS verified_query_contracts_are_immutable
  ON control.verified_query_contracts;
CREATE TRIGGER verified_query_contracts_are_immutable
BEFORE UPDATE OR DELETE ON control.verified_query_contracts
FOR EACH ROW EXECUTE FUNCTION control.reject_source_profile_revision_mutation();

COMMENT ON TABLE control.metadata_snapshots IS
  'Immutable reader-visible catalog snapshots keyed by source and metadata revision.';
COMMENT ON TABLE control.active_metadata_revisions IS
  'Atomic active metadata revision pointer for each source.';
COMMENT ON TABLE control.source_profile_revisions IS
  'Immutable validated source manifests and AES-GCM-encrypted reader credentials.';
COMMENT ON TABLE control.active_source_profiles IS
  'Atomic active generation and enabled state for each control-plane source.';
COMMENT ON TABLE control.verified_query_contracts IS
  'Immutable guarded-query result contracts bound to a metadata revision.';
