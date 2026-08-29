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

CREATE OR REPLACE VIEW ai.meter_readings AS
SELECT
  reading.id AS reading_id,
  reading.reading_key,
  meter.id AS meter_id,
  meter.meter_code,
  site.site_code,
  site.region,
  site.site_type,
  meter.meter_type,
  meter.tariff,
  reading.interval_start,
  reading.interval_end,
  reading.local_date,
  reading.local_hour,
  reading.utc_offset_minutes,
  reading.consumed_kwh,
  reading.generated_kwh,
  reading.net_kwh,
  reading.quality_status,
  reading.meter_state
FROM energy.meter_readings AS reading
JOIN energy.meters AS meter ON meter.id = reading.meter_id
JOIN energy.sites AS site ON site.id = meter.site_id;

CREATE OR REPLACE VIEW ai.outage_overview AS
SELECT
  outage.id AS outage_id,
  outage.outage_no,
  site.id AS site_id,
  site.site_code,
  site.region,
  site.site_type,
  outage.started_at,
  (outage.started_at AT TIME ZONE 'Asia/Seoul')::date AS started_on,
  outage.ended_at,
  outage.outage_type,
  outage.outage_status,
  outage.affected_meter_count,
  outage.duration_minutes
FROM energy.outages AS outage
JOIN energy.sites AS site ON site.id = outage.site_id;

CREATE OR REPLACE VIEW ai.meter_overview AS
SELECT
  meter.id AS meter_id,
  meter.meter_code,
  site.site_code,
  site.region,
  site.site_type,
  meter.meter_type,
  meter.tariff,
  meter.installed_on,
  meter.retired_on,
  COALESCE(reading_stats.reading_count, 0)::integer AS reading_count,
  COALESCE(reading_stats.missing_reading_count, 0)::integer AS missing_reading_count,
  reading_stats.net_kwh_sum,
  reading_stats.first_interval_start,
  reading_stats.last_interval_start,
  COALESCE(outage_stats.outage_count, 0)::integer AS site_outage_count
FROM energy.meters AS meter
JOIN energy.sites AS site ON site.id = meter.site_id
LEFT JOIN (
  SELECT
    meter_id,
    count(*) AS reading_count,
    count(*) FILTER (WHERE quality_status = 'MISSING') AS missing_reading_count,
    sum(net_kwh) AS net_kwh_sum,
    min(interval_start) AS first_interval_start,
    max(interval_start) AS last_interval_start
  FROM energy.meter_readings
  GROUP BY meter_id
) AS reading_stats ON reading_stats.meter_id = meter.id
LEFT JOIN (
  SELECT site_id, count(*) AS outage_count
  FROM energy.outages GROUP BY site_id
) AS outage_stats ON outage_stats.site_id = site.id;

COMMENT ON VIEW ai.meter_readings IS 'Grain: one meter interval. Negative net_kwh means net export; missing readings are null and are not zero usage.';
COMMENT ON COLUMN ai.meter_readings.reading_key IS 'Stable synthetic interval-reading reference.';
COMMENT ON COLUMN ai.meter_readings.meter_id IS 'Join key to ai.meter_overview.meter_id; joining repeats one meter across many intervals.';
COMMENT ON COLUMN ai.meter_readings.meter_code IS 'Stable synthetic meter code with no customer identity or service address.';
COMMENT ON COLUMN ai.meter_readings.site_code IS 'Stable synthetic metered-site code with no service address.';
COMMENT ON COLUMN ai.meter_readings.region IS 'Synthetic region of the metered site.';
COMMENT ON COLUMN ai.meter_readings.site_type IS 'Synthetic operational category of the metered site.';
COMMENT ON COLUMN ai.meter_readings.tariff IS 'Synthetic tariff category of the meter, not a monetary rate.';
COMMENT ON COLUMN ai.meter_readings.interval_end IS 'Exclusive end timestamp of the UTC metering interval.';
COMMENT ON COLUMN ai.meter_readings.local_date IS 'Site-local calendar date label; use with local_hour and utc_offset_minutes around DST changes.';
COMMENT ON COLUMN ai.meter_readings.local_hour IS 'Wall-clock hour label; use with local_date and utc_offset_minutes because DST may repeat an hour.';
COMMENT ON VIEW ai.outage_overview IS 'Grain: one outage report. Overlapping site intervals can double-count duration.';
COMMENT ON COLUMN ai.outage_overview.outage_no IS 'Synthetic human-readable outage reference.';
COMMENT ON COLUMN ai.outage_overview.site_id IS 'Synthetic site key; one site can have overlapping outage reports.';
COMMENT ON COLUMN ai.outage_overview.site_code IS 'Stable synthetic metered-site code with no service address.';
COMMENT ON COLUMN ai.outage_overview.region IS 'Synthetic region of the affected site.';
COMMENT ON COLUMN ai.outage_overview.site_type IS 'Synthetic operational category of the affected site.';
COMMENT ON COLUMN ai.outage_overview.started_on IS 'Asia/Seoul calendar date derived from started_at.';
COMMENT ON VIEW ai.meter_overview IS 'Grain: one meter, preserving meters with zero readings. net_kwh_sum stays null when no non-missing readings exist.';
COMMENT ON COLUMN ai.meter_overview.meter_code IS 'Stable synthetic meter code with no customer identity or service address.';
COMMENT ON COLUMN ai.meter_overview.site_code IS 'Stable synthetic metered-site code with no service address.';
COMMENT ON COLUMN ai.meter_overview.retired_on IS 'Meter retirement date; null while the synthetic meter remains active.';
COMMENT ON COLUMN ai.meter_overview.first_interval_start IS 'Earliest reading interval start; null when the meter has no readings.';
COMMENT ON COLUMN ai.meter_overview.last_interval_start IS 'Latest reading interval start; null when the meter has no readings.';

GRANT USAGE ON SCHEMA energy TO energy_telemetry_view_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA energy TO energy_telemetry_view_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE query_man_admin IN SCHEMA energy
  GRANT SELECT ON TABLES TO energy_telemetry_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO energy_telemetry_view_owner;
ALTER VIEW ai.meter_readings OWNER TO energy_telemetry_view_owner;
ALTER VIEW ai.outage_overview OWNER TO energy_telemetry_view_owner;
ALTER VIEW ai.meter_overview OWNER TO energy_telemetry_view_owner;

REVOKE ALL ON SCHEMA energy FROM energy_telemetry_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA energy FROM energy_telemetry_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA energy FROM energy_telemetry_reader;
GRANT USAGE ON SCHEMA ai TO energy_telemetry_reader;
GRANT SELECT ON ai.meter_readings, ai.outage_overview, ai.meter_overview
  TO energy_telemetry_reader;

COMMIT;
