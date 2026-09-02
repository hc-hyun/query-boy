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

COMMIT;
