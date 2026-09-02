BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.appointment_overview (
  appointment_id,
  appointment_no,
  patient_code,
  age_band,
  sex_at_birth,
  home_region,
  provider_code,
  specialty,
  facility_region,
  booked_at,
  scheduled_at,
  scheduled_on,
  encounter_type,
  appointment_status,
  cancellation_reason,
  checked_in_at,
  completed_at,
  wait_minutes
) AS
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

CREATE OR REPLACE VIEW ai.lab_results (
  lab_result_id,
  result_no,
  patient_id,
  patient_code,
  age_band,
  home_region,
  appointment_id,
  appointment_no,
  test_code,
  collected_at,
  resulted_at,
  result_status,
  result_numeric,
  unit,
  reference_low,
  reference_high,
  interpretation,
  correction_version,
  critical_status
) AS
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

CREATE OR REPLACE VIEW ai.patient_overview (
  patient_id,
  patient_code,
  age_band,
  sex_at_birth,
  home_region,
  enrolled_on,
  appointment_count,
  canceled_appointment_count,
  no_show_count,
  last_scheduled_at,
  lab_result_count,
  pending_lab_count,
  critical_lab_count,
  last_collected_at
) AS
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

COMMENT ON VIEW ai.appointment_overview IS E'query-man:source=clinical-operations;view-contract=1\nGrain: one fully synthetic appointment. NO_SHOW excludes CANCELED by status definition.';
COMMENT ON COLUMN ai.appointment_overview.appointment_no IS 'Synthetic appointment reference with no real patient or encounter identity.';
COMMENT ON COLUMN ai.appointment_overview.sex_at_birth IS 'Fully synthetic demographic category; it is sensitive in real clinical data but identifies no person in this fixture.';
COMMENT ON COLUMN ai.appointment_overview.home_region IS 'Fully synthetic broad home-region category; no address is exposed.';
COMMENT ON COLUMN ai.appointment_overview.booked_at IS 'Timestamp when the synthetic appointment was booked.';
COMMENT ON COLUMN ai.appointment_overview.scheduled_on IS 'Asia/Seoul calendar date derived from scheduled_at.';
COMMENT ON COLUMN ai.appointment_overview.checked_in_at IS 'Check-in timestamp; null when the appointment has not checked in or check-in does not apply.';
COMMENT ON COLUMN ai.appointment_overview.completed_at IS 'Completion timestamp; null unless the appointment was completed.';
COMMENT ON VIEW ai.lab_results IS E'query-man:source=clinical-operations;view-contract=1\nGrain: one fully synthetic lab-result record. Pending numeric values are null, not zero; CORRECTED is the latest fixture record state.';
COMMENT ON COLUMN ai.lab_results.result_no IS 'Synthetic lab-result reference with no real specimen or patient identity.';
COMMENT ON COLUMN ai.lab_results.patient_id IS 'Synthetic patient join key; joining to ai.patient_overview can repeat one patient across many lab results.';
COMMENT ON COLUMN ai.lab_results.patient_code IS 'Stable synthetic patient code with no real-person identity or contact data.';
COMMENT ON COLUMN ai.lab_results.age_band IS 'Fully synthetic age band; it is not an exact age or birth date.';
COMMENT ON COLUMN ai.lab_results.home_region IS 'Fully synthetic broad home-region category; no address is exposed.';
COMMENT ON COLUMN ai.lab_results.appointment_id IS 'Optional join key to ai.appointment_overview; null for lab results without an appointment.';
COMMENT ON COLUMN ai.lab_results.appointment_no IS 'Optional synthetic appointment reference; null for lab results without an appointment.';
COMMENT ON COLUMN ai.lab_results.resulted_at IS 'Result publication timestamp; null while result_status is PENDING.';
COMMENT ON COLUMN ai.lab_results.unit IS 'Measurement unit determined by test_code; do not compare or sum values across different tests or units.';
COMMENT ON VIEW ai.patient_overview IS E'query-man:source=clinical-operations;view-contract=1\nGrain: one fully synthetic patient code, preserving patients with no appointments or labs. It contains no PII or diagnoses.';
COMMENT ON COLUMN ai.patient_overview.canceled_appointment_count IS 'Preaggregated count of CANCELED appointments for the synthetic patient.';
COMMENT ON COLUMN ai.patient_overview.last_scheduled_at IS 'Latest scheduled appointment timestamp; null when the patient has no appointments.';
COMMENT ON COLUMN ai.patient_overview.lab_result_count IS 'Preaggregated count of lab-result records for the synthetic patient.';
COMMENT ON COLUMN ai.patient_overview.last_collected_at IS 'Latest specimen collection timestamp; null when the patient has no lab results.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;
REVOKE ALL ON SCHEMA clinical FROM clinical_operations_view_owner;
GRANT USAGE ON SCHEMA clinical TO clinical_operations_view_owner;
REVOKE ALL ON
  clinical.synthetic_patients,
  clinical.providers,
  clinical.appointments,
  clinical.lab_results
FROM clinical_operations_view_owner;
GRANT SELECT ON
  clinical.synthetic_patients,
  clinical.providers,
  clinical.appointments,
  clinical.lab_results
TO clinical_operations_view_owner;

REVOKE ALL ON SCHEMA ai FROM clinical_operations_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO clinical_operations_view_owner;
ALTER VIEW ai.appointment_overview OWNER TO clinical_operations_view_owner;
ALTER VIEW ai.lab_results OWNER TO clinical_operations_view_owner;
ALTER VIEW ai.patient_overview OWNER TO clinical_operations_view_owner;
REVOKE CREATE ON SCHEMA ai FROM clinical_operations_view_owner;

REVOKE ALL ON SCHEMA clinical FROM clinical_operations_reader;
REVOKE ALL ON
  clinical.synthetic_patients,
  clinical.providers,
  clinical.appointments,
  clinical.lab_results
FROM clinical_operations_reader;
REVOKE ALL ON SCHEMA ai FROM clinical_operations_reader;
GRANT USAGE ON SCHEMA ai TO clinical_operations_reader;
REVOKE ALL ON
  ai.appointment_overview,
  ai.lab_results,
  ai.patient_overview
FROM PUBLIC;
REVOKE ALL ON
  ai.appointment_overview,
  ai.lab_results,
  ai.patient_overview
FROM clinical_operations_reader;
GRANT SELECT ON
  ai.appointment_overview,
  ai.lab_results,
  ai.patient_overview
TO clinical_operations_reader;

COMMIT;
