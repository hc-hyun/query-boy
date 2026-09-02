\connect market_voc

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS voc;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA voc FROM PUBLIC;

COMMENT ON SCHEMA voc IS
  'Source tables for market voice-of-customer cases and returned product observations.';
COMMENT ON SCHEMA ai IS
  'Curated, read-only query surfaces for AI clients.';

CREATE TABLE IF NOT EXISTS voc.users (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id text NOT NULL UNIQUE,
  display_name text NOT NULL,
  team_name text NOT NULL,
  email text NOT NULL UNIQUE,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT voc_users_user_id_format
    CHECK (user_id ~ '^VOC-U[0-9]{4}$'),
  CONSTRAINT voc_users_name_not_blank
    CHECK (btrim(display_name) <> ''),
  CONSTRAINT voc_users_team_not_blank
    CHECK (btrim(team_name) <> '')
);

CREATE TABLE IF NOT EXISTS voc.product_models (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  model_code text NOT NULL UNIQUE,
  model_name text NOT NULL,
  product_family text NOT NULL,
  release_date date NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  CONSTRAINT voc_product_models_code_not_blank
    CHECK (btrim(model_code) <> ''),
  CONSTRAINT voc_product_models_name_not_blank
    CHECK (btrim(model_name) <> '')
);

CREATE TABLE IF NOT EXISTS voc.devices (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_model_id bigint NOT NULL
    REFERENCES voc.product_models(id) ON DELETE RESTRICT,
  serial_number text NOT NULL UNIQUE,
  manufacturing_lot text NOT NULL,
  manufactured_at date NOT NULL,
  sold_at date NOT NULL,
  shipped_hw_version text NOT NULL,
  shipped_sw_version text NOT NULL,
  CONSTRAINT voc_devices_serial_not_blank
    CHECK (btrim(serial_number) <> ''),
  CONSTRAINT voc_devices_lot_not_blank
    CHECK (btrim(manufacturing_lot) <> ''),
  CONSTRAINT voc_devices_sale_after_manufacture
    CHECK (sold_at >= manufactured_at),
  CONSTRAINT voc_devices_hw_not_blank
    CHECK (btrim(shipped_hw_version) <> ''),
  CONSTRAINT voc_devices_sw_not_blank
    CHECK (btrim(shipped_sw_version) <> '')
);

CREATE TABLE IF NOT EXISTS voc.cases (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  voc_no text NOT NULL UNIQUE,
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL,
  registered_by_id bigint NOT NULL
    REFERENCES voc.users(id) ON DELETE RESTRICT,
  assigned_to_id bigint
    REFERENCES voc.users(id) ON DELETE RESTRICT,
  device_id bigint NOT NULL
    REFERENCES voc.devices(id) ON DELETE RESTRICT,
  voc_type text NOT NULL,
  title text NOT NULL,
  problem_detail text NOT NULL,
  analysis_cause text,
  response_action text,
  defect_category text NOT NULL,
  severity text NOT NULL,
  status text NOT NULL,
  intake_channel text NOT NULL,
  market_region text NOT NULL,
  country_code text NOT NULL,
  observed_hw_version text NOT NULL,
  observed_sw_version text NOT NULL,
  resolved_at timestamptz,
  resolution_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT voc_cases_number_format
    CHECK (voc_no ~ '^VOC-[0-9]{6}$'),
  CONSTRAINT voc_cases_received_after_occurrence
    CHECK (received_at >= occurred_at),
  CONSTRAINT voc_cases_type_valid
    CHECK (voc_type IN ('DEFECT', 'COMPLAINT', 'INQUIRY', 'SUGGESTION')),
  CONSTRAINT voc_cases_title_not_blank
    CHECK (btrim(title) <> ''),
  CONSTRAINT voc_cases_detail_not_blank
    CHECK (btrim(problem_detail) <> ''),
  CONSTRAINT voc_cases_category_valid
    CHECK (defect_category IN (
      'BATTERY', 'DISPLAY', 'HINGE', 'CONNECTIVITY',
      'CAMERA', 'SOFTWARE', 'OVERHEATING', 'OTHER'
    )),
  CONSTRAINT voc_cases_severity_valid
    CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
  CONSTRAINT voc_cases_status_valid
    CHECK (status IN ('RECEIVED', 'TRIAGED', 'IN_PROGRESS', 'RESOLVED', 'CLOSED')),
  CONSTRAINT voc_cases_channel_valid
    CHECK (intake_channel IN (
      'SERVICE_CENTER', 'CALL_CENTER', 'MOBILE_APP', 'PARTNER', 'MONITORING'
    )),
  CONSTRAINT voc_cases_country_code_format
    CHECK (country_code ~ '^[A-Z]{2}$'),
  CONSTRAINT voc_cases_resolution_consistent
    CHECK (
      (status IN ('RESOLVED', 'CLOSED')) =
      (resolved_at IS NOT NULL AND resolution_code IS NOT NULL)
    ),
  CONSTRAINT voc_cases_resolution_after_received
    CHECK (resolved_at IS NULL OR resolved_at >= received_at),
  CONSTRAINT voc_cases_cause_not_blank
    CHECK (analysis_cause IS NULL OR btrim(analysis_cause) <> ''),
  CONSTRAINT voc_cases_action_not_blank
    CHECK (response_action IS NULL OR btrim(response_action) <> '')
);

CREATE TABLE IF NOT EXISTS voc.case_comments (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id bigint NOT NULL
    REFERENCES voc.cases(id) ON DELETE CASCADE,
  sequence_no smallint NOT NULL,
  author_id bigint NOT NULL
    REFERENCES voc.users(id) ON DELETE RESTRICT,
  visibility text NOT NULL,
  comment_type text NOT NULL,
  body text NOT NULL,
  created_at timestamptz NOT NULL,
  CONSTRAINT voc_case_comments_sequence_positive
    CHECK (sequence_no > 0),
  CONSTRAINT voc_case_comments_visibility_valid
    CHECK (visibility IN ('INTERNAL', 'PUBLIC')),
  CONSTRAINT voc_case_comments_type_valid
    CHECK (comment_type IN ('INTAKE', 'INVESTIGATION', 'CUSTOMER_RESPONSE', 'RESOLUTION', 'GENERAL')),
  CONSTRAINT voc_case_comments_body_not_blank
    CHECK (btrim(body) <> ''),
  CONSTRAINT voc_case_comments_case_sequence_unique
    UNIQUE (case_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS voc_devices_model_idx
  ON voc.devices(product_model_id);
CREATE INDEX IF NOT EXISTS voc_devices_lot_idx
  ON voc.devices(manufacturing_lot);
CREATE INDEX IF NOT EXISTS voc_cases_received_idx
  ON voc.cases(received_at DESC);
CREATE INDEX IF NOT EXISTS voc_cases_status_date_idx
  ON voc.cases(status, received_at DESC);
CREATE INDEX IF NOT EXISTS voc_cases_category_severity_idx
  ON voc.cases(defect_category, severity);
CREATE INDEX IF NOT EXISTS voc_cases_region_date_idx
  ON voc.cases(market_region, received_at DESC);
CREATE INDEX IF NOT EXISTS voc_cases_registered_by_idx
  ON voc.cases(registered_by_id);
CREATE INDEX IF NOT EXISTS voc_cases_assigned_open_idx
  ON voc.cases(assigned_to_id, status)
  WHERE status NOT IN ('RESOLVED', 'CLOSED');
CREATE INDEX IF NOT EXISTS voc_cases_device_idx
  ON voc.cases(device_id);
CREATE INDEX IF NOT EXISTS voc_case_comments_case_date_idx
  ON voc.case_comments(case_id, created_at, id);

COMMENT ON TABLE voc.users IS
  'Grain: one internal service, quality, or product user handling market VOC.';
COMMENT ON COLUMN voc.users.user_id IS
  'Stable business user identifier displayed in VOC and comment results.';
COMMENT ON TABLE voc.product_models IS
  'Grain: one commercial product model.';
COMMENT ON COLUMN voc.product_models.model_name IS
  'Human-readable product model name used in market reports.';
COMMENT ON TABLE voc.devices IS
  'Grain: one sold physical device identified by serial number.';
COMMENT ON COLUMN voc.devices.manufacturing_lot IS
  'Manufacturing lot used for cohort and recurring-defect analysis.';
COMMENT ON COLUMN voc.devices.shipped_hw_version IS
  'Hardware version at shipment; a VOC case keeps its own observed snapshot.';
COMMENT ON COLUMN voc.devices.shipped_sw_version IS
  'Software version at shipment; a VOC case keeps its own observed snapshot.';
COMMENT ON TABLE voc.cases IS
  'Grain: one market VOC intake case for one device. Most seeded rows are defect reports.';
COMMENT ON COLUMN voc.cases.occurred_at IS
  'Customer-reported occurrence timestamp, stored with timezone.';
COMMENT ON COLUMN voc.cases.received_at IS
  'Timestamp when the company received the VOC; use for intake-period reporting.';
COMMENT ON COLUMN voc.cases.registered_by_id IS
  'Internal user who registered the VOC.';
COMMENT ON COLUMN voc.cases.assigned_to_id IS
  'Internal user currently responsible for the VOC; null means unassigned.';
COMMENT ON COLUMN voc.cases.problem_detail IS
  'Customer symptom and reproduction context captured at intake.';
COMMENT ON COLUMN voc.cases.analysis_cause IS
  'Confirmed or leading technical cause; null while analysis is incomplete.';
COMMENT ON COLUMN voc.cases.response_action IS
  'Customer response or corrective action; null until an action is fixed.';
COMMENT ON COLUMN voc.cases.observed_hw_version IS
  'Hardware version observed for this VOC case.';
COMMENT ON COLUMN voc.cases.observed_sw_version IS
  'Software version observed for this VOC case.';
COMMENT ON TABLE voc.case_comments IS
  'Grain: one chronological internal or public comment attached to one VOC case.';

COMMIT;

\if :{?query_man_skip_views}
\else
\ir /query-man-source-views/market-voc.sql
\endif
