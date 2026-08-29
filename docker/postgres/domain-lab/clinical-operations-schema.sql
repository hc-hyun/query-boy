\connect clinical_operations

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS clinical;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA clinical FROM PUBLIC;
REVOKE ALL ON SCHEMA ai FROM PUBLIC;

CREATE TABLE IF NOT EXISTS clinical.synthetic_patients (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  patient_code text NOT NULL UNIQUE,
  age_band text NOT NULL,
  sex_at_birth text NOT NULL,
  home_region text NOT NULL,
  enrolled_on date NOT NULL,
  CONSTRAINT clinical_patients_code_not_blank CHECK (btrim(patient_code) <> ''),
  CONSTRAINT clinical_patients_age_band_valid CHECK (age_band IN ('0-17', '18-34', '35-49', '50-64', '65+')),
  CONSTRAINT clinical_patients_sex_valid CHECK (sex_at_birth IN ('FEMALE', 'MALE', 'OTHER', 'UNKNOWN')),
  CONSTRAINT clinical_patients_region_not_blank CHECK (btrim(home_region) <> '')
);

CREATE TABLE IF NOT EXISTS clinical.providers (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider_code text NOT NULL UNIQUE,
  specialty text NOT NULL,
  facility_region text NOT NULL,
  CONSTRAINT clinical_providers_code_not_blank CHECK (btrim(provider_code) <> ''),
  CONSTRAINT clinical_providers_specialty_not_blank CHECK (btrim(specialty) <> ''),
  CONSTRAINT clinical_providers_region_not_blank CHECK (btrim(facility_region) <> '')
);

CREATE TABLE IF NOT EXISTS clinical.appointments (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  appointment_no text NOT NULL UNIQUE,
  patient_id bigint NOT NULL REFERENCES clinical.synthetic_patients(id) ON DELETE RESTRICT,
  provider_id bigint NOT NULL REFERENCES clinical.providers(id) ON DELETE RESTRICT,
  booked_at timestamptz NOT NULL,
  scheduled_at timestamptz NOT NULL,
  encounter_type text NOT NULL,
  appointment_status text NOT NULL,
  cancellation_reason text,
  checked_in_at timestamptz,
  completed_at timestamptz,
  wait_minutes integer,
  CONSTRAINT clinical_appointments_no_not_blank CHECK (btrim(appointment_no) <> ''),
  CONSTRAINT clinical_appointments_schedule_after_book CHECK (scheduled_at >= booked_at),
  CONSTRAINT clinical_appointments_encounter_valid CHECK (encounter_type IN ('IN_PERSON', 'VIDEO', 'PHONE')),
  CONSTRAINT clinical_appointments_status_valid CHECK (appointment_status IN ('SCHEDULED', 'CHECKED_IN', 'COMPLETED', 'CANCELED', 'NO_SHOW')),
  CONSTRAINT clinical_appointments_cancel_consistent CHECK ((appointment_status = 'CANCELED') = (cancellation_reason IS NOT NULL)),
  CONSTRAINT clinical_appointments_checkin_consistent CHECK (
    checked_in_at IS NULL OR appointment_status IN ('CHECKED_IN', 'COMPLETED')
  ),
  CONSTRAINT clinical_appointments_completion_consistent CHECK ((appointment_status = 'COMPLETED') = (completed_at IS NOT NULL)),
  CONSTRAINT clinical_appointments_checkin_after_schedule CHECK (checked_in_at IS NULL OR checked_in_at >= scheduled_at),
  CONSTRAINT clinical_appointments_complete_after_checkin CHECK (completed_at IS NULL OR (checked_in_at IS NOT NULL AND completed_at >= checked_in_at)),
  CONSTRAINT clinical_appointments_wait_valid CHECK (wait_minutes IS NULL OR wait_minutes >= 0)
);

CREATE TABLE IF NOT EXISTS clinical.lab_results (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  result_no text NOT NULL UNIQUE,
  patient_id bigint NOT NULL REFERENCES clinical.synthetic_patients(id) ON DELETE RESTRICT,
  appointment_id bigint REFERENCES clinical.appointments(id) ON DELETE SET NULL,
  test_code text NOT NULL,
  collected_at timestamptz NOT NULL,
  resulted_at timestamptz,
  result_status text NOT NULL,
  result_numeric numeric(16,4),
  unit text NOT NULL,
  reference_low numeric(16,4) NOT NULL,
  reference_high numeric(16,4) NOT NULL,
  interpretation text NOT NULL,
  correction_version smallint NOT NULL,
  critical_status text NOT NULL,
  CONSTRAINT clinical_labs_no_not_blank CHECK (btrim(result_no) <> ''),
  CONSTRAINT clinical_labs_test_valid CHECK (test_code IN ('CBC_HGB', 'GLUCOSE', 'LDL', 'ALT', 'CREATININE')),
  CONSTRAINT clinical_labs_status_valid CHECK (result_status IN ('PENDING', 'FINAL', 'CORRECTED')),
  CONSTRAINT clinical_labs_reference_valid CHECK (reference_high >= reference_low),
  CONSTRAINT clinical_labs_interpretation_valid CHECK (interpretation IN ('LOW', 'NORMAL', 'HIGH', 'INDETERMINATE')),
  CONSTRAINT clinical_labs_correction_nonnegative CHECK (correction_version >= 0),
  CONSTRAINT clinical_labs_critical_valid CHECK (critical_status IN ('YES', 'NO', 'UNKNOWN')),
  CONSTRAINT clinical_labs_result_time_valid CHECK (resulted_at IS NULL OR resulted_at >= collected_at),
  CONSTRAINT clinical_labs_status_consistent CHECK (
    (
      result_status = 'PENDING'
      AND resulted_at IS NULL
      AND result_numeric IS NULL
      AND interpretation = 'INDETERMINATE'
      AND correction_version = 0
      AND critical_status = 'UNKNOWN'
    )
    OR (
      result_status = 'FINAL'
      AND resulted_at IS NOT NULL
      AND result_numeric IS NOT NULL
      AND interpretation <> 'INDETERMINATE'
      AND correction_version = 0
      AND critical_status IN ('YES', 'NO')
    )
    OR (
      result_status = 'CORRECTED'
      AND resulted_at IS NOT NULL
      AND result_numeric IS NOT NULL
      AND interpretation <> 'INDETERMINATE'
      AND correction_version > 0
      AND critical_status IN ('YES', 'NO')
    )
  )
);

CREATE INDEX IF NOT EXISTS clinical_appointments_patient_time_idx
  ON clinical.appointments(patient_id, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS clinical_appointments_provider_time_idx
  ON clinical.appointments(provider_id, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS clinical_appointments_status_time_idx
  ON clinical.appointments(appointment_status, scheduled_at DESC);
CREATE INDEX IF NOT EXISTS clinical_labs_patient_time_idx
  ON clinical.lab_results(patient_id, collected_at DESC);
CREATE INDEX IF NOT EXISTS clinical_labs_appointment_idx
  ON clinical.lab_results(appointment_id);
CREATE INDEX IF NOT EXISTS clinical_labs_status_test_idx
  ON clinical.lab_results(result_status, test_code, collected_at DESC);

COMMENT ON SCHEMA clinical IS 'Private physical tables for a fully synthetic clinical-operations domain lab.';
COMMENT ON TABLE clinical.synthetic_patients IS 'Grain: one synthetic patient code. No name, address, phone, email, identifier, diagnosis, or real patient data exists.';
COMMENT ON TABLE clinical.providers IS 'Grain: one synthetic provider code; no real clinician identity exists.';
COMMENT ON TABLE clinical.appointments IS 'Grain: one synthetic appointment used only for operational scheduling analysis.';
COMMENT ON TABLE clinical.lab_results IS 'Grain: one synthetic numeric lab result. Pending results deliberately have null result values.';

CREATE OR REPLACE VIEW ai.appointment_overview AS
SELECT
  appointment.id AS appointment_id,
  appointment.appointment_no,
  patient.patient_code,
  patient.age_band,
  patient.sex_at_birth,
  patient.home_region,
  provider.provider_code,
  provider.specialty,
  provider.facility_region,
  appointment.booked_at,
  appointment.scheduled_at,
  (appointment.scheduled_at AT TIME ZONE 'Asia/Seoul')::date AS scheduled_on,
  appointment.encounter_type,
  appointment.appointment_status,
  appointment.cancellation_reason,
  appointment.checked_in_at,
  appointment.completed_at,
  appointment.wait_minutes
FROM clinical.appointments AS appointment
JOIN clinical.synthetic_patients AS patient ON patient.id = appointment.patient_id
JOIN clinical.providers AS provider ON provider.id = appointment.provider_id;

CREATE OR REPLACE VIEW ai.lab_results AS
SELECT
  result.id AS lab_result_id,
  result.result_no,
  patient.id AS patient_id,
  patient.patient_code,
  patient.age_band,
  patient.home_region,
  appointment.id AS appointment_id,
  appointment.appointment_no,
  result.test_code,
  result.collected_at,
  result.resulted_at,
  result.result_status,
  result.result_numeric,
  result.unit,
  result.reference_low,
  result.reference_high,
  result.interpretation,
  result.correction_version,
  result.critical_status
FROM clinical.lab_results AS result
JOIN clinical.synthetic_patients AS patient ON patient.id = result.patient_id
LEFT JOIN clinical.appointments AS appointment ON appointment.id = result.appointment_id;

CREATE OR REPLACE VIEW ai.patient_overview AS
SELECT
  patient.id AS patient_id,
  patient.patient_code,
  patient.age_band,
  patient.sex_at_birth,
  patient.home_region,
  patient.enrolled_on,
  COALESCE(appointment_stats.appointment_count, 0)::integer AS appointment_count,
  COALESCE(appointment_stats.canceled_count, 0)::integer AS canceled_appointment_count,
  COALESCE(appointment_stats.no_show_count, 0)::integer AS no_show_count,
  appointment_stats.last_scheduled_at,
  COALESCE(lab_stats.lab_result_count, 0)::integer AS lab_result_count,
  COALESCE(lab_stats.pending_lab_count, 0)::integer AS pending_lab_count,
  COALESCE(lab_stats.critical_lab_count, 0)::integer AS critical_lab_count,
  lab_stats.last_collected_at
FROM clinical.synthetic_patients AS patient
LEFT JOIN (
  SELECT
    patient_id,
    count(*) AS appointment_count,
    count(*) FILTER (WHERE appointment_status = 'CANCELED') AS canceled_count,
    count(*) FILTER (WHERE appointment_status = 'NO_SHOW') AS no_show_count,
    max(scheduled_at) AS last_scheduled_at
  FROM clinical.appointments GROUP BY patient_id
) AS appointment_stats ON appointment_stats.patient_id = patient.id
LEFT JOIN (
  SELECT
    patient_id,
    count(*) AS lab_result_count,
    count(*) FILTER (WHERE result_status = 'PENDING') AS pending_lab_count,
    count(*) FILTER (WHERE critical_status = 'YES') AS critical_lab_count,
    max(collected_at) AS last_collected_at
  FROM clinical.lab_results GROUP BY patient_id
) AS lab_stats ON lab_stats.patient_id = patient.id;

COMMENT ON VIEW ai.appointment_overview IS 'Grain: one fully synthetic appointment. NO_SHOW excludes CANCELED by status definition.';
COMMENT ON COLUMN ai.appointment_overview.appointment_no IS 'Synthetic appointment reference with no real patient or encounter identity.';
COMMENT ON COLUMN ai.appointment_overview.sex_at_birth IS 'Fully synthetic demographic category; it is sensitive in real clinical data but identifies no person in this fixture.';
COMMENT ON COLUMN ai.appointment_overview.home_region IS 'Fully synthetic broad home-region category; no address is exposed.';
COMMENT ON COLUMN ai.appointment_overview.booked_at IS 'Timestamp when the synthetic appointment was booked.';
COMMENT ON COLUMN ai.appointment_overview.scheduled_on IS 'Asia/Seoul calendar date derived from scheduled_at.';
COMMENT ON COLUMN ai.appointment_overview.checked_in_at IS 'Check-in timestamp; null when the appointment has not checked in or check-in does not apply.';
COMMENT ON COLUMN ai.appointment_overview.completed_at IS 'Completion timestamp; null unless the appointment was completed.';
COMMENT ON VIEW ai.lab_results IS 'Grain: one fully synthetic lab-result record. Pending numeric values are null, not zero; CORRECTED is the latest fixture record state.';
COMMENT ON COLUMN ai.lab_results.result_no IS 'Synthetic lab-result reference with no real specimen or patient identity.';
COMMENT ON COLUMN ai.lab_results.patient_id IS 'Synthetic patient join key; joining to ai.patient_overview can repeat one patient across many lab results.';
COMMENT ON COLUMN ai.lab_results.patient_code IS 'Stable synthetic patient code with no real-person identity or contact data.';
COMMENT ON COLUMN ai.lab_results.age_band IS 'Fully synthetic age band; it is not an exact age or birth date.';
COMMENT ON COLUMN ai.lab_results.home_region IS 'Fully synthetic broad home-region category; no address is exposed.';
COMMENT ON COLUMN ai.lab_results.appointment_id IS 'Optional join key to ai.appointment_overview; null for lab results without an appointment.';
COMMENT ON COLUMN ai.lab_results.appointment_no IS 'Optional synthetic appointment reference; null for lab results without an appointment.';
COMMENT ON COLUMN ai.lab_results.resulted_at IS 'Result publication timestamp; null while result_status is PENDING.';
COMMENT ON COLUMN ai.lab_results.unit IS 'Measurement unit determined by test_code; do not compare or sum values across different tests or units.';
COMMENT ON VIEW ai.patient_overview IS 'Grain: one fully synthetic patient code, preserving patients with no appointments or labs. It contains no PII or diagnoses.';
COMMENT ON COLUMN ai.patient_overview.canceled_appointment_count IS 'Preaggregated count of CANCELED appointments for the synthetic patient.';
COMMENT ON COLUMN ai.patient_overview.last_scheduled_at IS 'Latest scheduled appointment timestamp; null when the patient has no appointments.';
COMMENT ON COLUMN ai.patient_overview.lab_result_count IS 'Preaggregated count of lab-result records for the synthetic patient.';
COMMENT ON COLUMN ai.patient_overview.last_collected_at IS 'Latest specimen collection timestamp; null when the patient has no lab results.';

GRANT USAGE ON SCHEMA clinical TO clinical_operations_view_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA clinical TO clinical_operations_view_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE query_man_admin IN SCHEMA clinical
  GRANT SELECT ON TABLES TO clinical_operations_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO clinical_operations_view_owner;
ALTER VIEW ai.appointment_overview OWNER TO clinical_operations_view_owner;
ALTER VIEW ai.lab_results OWNER TO clinical_operations_view_owner;
ALTER VIEW ai.patient_overview OWNER TO clinical_operations_view_owner;

REVOKE ALL ON SCHEMA clinical FROM clinical_operations_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA clinical FROM clinical_operations_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA clinical FROM clinical_operations_reader;
GRANT USAGE ON SCHEMA ai TO clinical_operations_reader;
GRANT SELECT ON ai.appointment_overview, ai.lab_results, ai.patient_overview
  TO clinical_operations_reader;

COMMIT;
