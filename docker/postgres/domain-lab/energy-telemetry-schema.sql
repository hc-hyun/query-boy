\connect energy_telemetry

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS energy;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA energy FROM PUBLIC;
REVOKE ALL ON SCHEMA ai FROM PUBLIC;

CREATE TABLE IF NOT EXISTS energy.sites (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  site_code text NOT NULL UNIQUE,
  name text NOT NULL,
  region text NOT NULL,
  site_type text NOT NULL,
  commissioned_on date NOT NULL,
  CONSTRAINT energy_sites_code_not_blank CHECK (btrim(site_code) <> ''),
  CONSTRAINT energy_sites_name_not_blank CHECK (btrim(name) <> ''),
  CONSTRAINT energy_sites_type_valid CHECK (site_type IN ('RESIDENTIAL', 'COMMERCIAL', 'INDUSTRIAL', 'SOLAR'))
);

CREATE TABLE IF NOT EXISTS energy.meters (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  meter_code text NOT NULL UNIQUE,
  site_id bigint NOT NULL REFERENCES energy.sites(id) ON DELETE RESTRICT,
  meter_type text NOT NULL,
  tariff text NOT NULL,
  installed_on date NOT NULL,
  retired_on date,
  CONSTRAINT energy_meters_code_not_blank CHECK (btrim(meter_code) <> ''),
  CONSTRAINT energy_meters_type_valid CHECK (meter_type IN ('CONSUMPTION', 'BIDIRECTIONAL', 'GENERATION')),
  CONSTRAINT energy_meters_tariff_valid CHECK (tariff IN ('STANDARD', 'TOU', 'INDUSTRIAL')),
  CONSTRAINT energy_meters_retired_after_install CHECK (retired_on IS NULL OR retired_on >= installed_on)
);

CREATE TABLE IF NOT EXISTS energy.meter_readings (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  reading_key text NOT NULL UNIQUE,
  meter_id bigint NOT NULL REFERENCES energy.meters(id) ON DELETE CASCADE,
  interval_start timestamptz NOT NULL,
  interval_end timestamptz NOT NULL,
  local_date date NOT NULL,
  local_hour smallint NOT NULL,
  utc_offset_minutes smallint NOT NULL,
  consumed_kwh numeric(14,4),
  generated_kwh numeric(14,4),
  net_kwh numeric(14,4),
  quality_status text NOT NULL,
  meter_state text NOT NULL,
  CONSTRAINT energy_readings_meter_interval_unique UNIQUE (meter_id, interval_start),
  CONSTRAINT energy_readings_key_not_blank CHECK (btrim(reading_key) <> ''),
  CONSTRAINT energy_readings_interval_valid CHECK (interval_end > interval_start),
  CONSTRAINT energy_readings_local_hour_valid CHECK (local_hour BETWEEN 0 AND 23),
  CONSTRAINT energy_readings_offset_valid CHECK (utc_offset_minutes BETWEEN -720 AND 840),
  CONSTRAINT energy_readings_quality_valid CHECK (quality_status IN ('ACTUAL', 'ESTIMATED', 'MISSING', 'CORRECTED')),
  CONSTRAINT energy_readings_state_valid CHECK (meter_state IN ('NORMAL', 'OUTAGE', 'RESET')),
  CONSTRAINT energy_readings_values_valid CHECK (
    (quality_status = 'MISSING' AND consumed_kwh IS NULL AND generated_kwh IS NULL AND net_kwh IS NULL)
    OR (
      quality_status <> 'MISSING'
      AND consumed_kwh IS NOT NULL AND consumed_kwh >= 0
      AND generated_kwh IS NOT NULL AND generated_kwh >= 0
      AND net_kwh = consumed_kwh - generated_kwh
    )
  )
);

CREATE TABLE IF NOT EXISTS energy.outages (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  outage_no text NOT NULL UNIQUE,
  site_id bigint NOT NULL REFERENCES energy.sites(id) ON DELETE RESTRICT,
  started_at timestamptz NOT NULL,
  ended_at timestamptz,
  outage_type text NOT NULL,
  outage_status text NOT NULL,
  affected_meter_count integer NOT NULL,
  duration_minutes numeric(14,2),
  CONSTRAINT energy_outages_no_not_blank CHECK (btrim(outage_no) <> ''),
  CONSTRAINT energy_outages_type_valid CHECK (outage_type IN ('GRID', 'PLANNED', 'WEATHER', 'EQUIPMENT')),
  CONSTRAINT energy_outages_status_valid CHECK (outage_status IN ('RESOLVED', 'ONGOING')),
  CONSTRAINT energy_outages_affected_nonnegative CHECK (affected_meter_count >= 0),
  CONSTRAINT energy_outages_end_after_start CHECK (ended_at IS NULL OR ended_at > started_at),
  CONSTRAINT energy_outages_resolution_consistent CHECK (
    (outage_status = 'ONGOING' AND ended_at IS NULL AND duration_minutes IS NULL)
    OR (outage_status = 'RESOLVED' AND ended_at IS NOT NULL AND duration_minutes IS NOT NULL AND duration_minutes >= 0)
  )
);

CREATE INDEX IF NOT EXISTS energy_meters_site_idx
  ON energy.meters(site_id);
CREATE INDEX IF NOT EXISTS energy_readings_meter_time_idx
  ON energy.meter_readings(meter_id, interval_start DESC);
CREATE INDEX IF NOT EXISTS energy_readings_quality_time_idx
  ON energy.meter_readings(quality_status, interval_start DESC);
CREATE INDEX IF NOT EXISTS energy_readings_local_clock_idx
  ON energy.meter_readings(local_date, local_hour, utc_offset_minutes);
CREATE INDEX IF NOT EXISTS energy_outages_site_time_idx
  ON energy.outages(site_id, started_at DESC);

COMMENT ON SCHEMA energy IS 'Private physical tables for the synthetic energy-telemetry domain lab.';
COMMENT ON TABLE energy.sites IS 'Grain: one synthetic metered site.';
COMMENT ON TABLE energy.meters IS 'Grain: one smart meter, including meters with no readings.';
COMMENT ON TABLE energy.meter_readings IS 'Grain: one UTC interval per meter. Repeated local DST labels differ by utc_offset_minutes; MISSING rows keep energy values null.';
COMMENT ON TABLE energy.outages IS 'Grain: one site outage report. Outage intervals may overlap and must not be summed without interval consolidation.';

COMMIT;
