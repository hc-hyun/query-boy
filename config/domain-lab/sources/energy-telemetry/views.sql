BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.meter_readings (
  reading_id,
  reading_key,
  meter_id,
  meter_code,
  site_code,
  region,
  site_type,
  meter_type,
  tariff,
  interval_start,
  interval_end,
  local_date,
  local_hour,
  utc_offset_minutes,
  consumed_kwh,
  generated_kwh,
  net_kwh,
  quality_status,
  meter_state
) AS
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

CREATE OR REPLACE VIEW ai.outage_overview (
  outage_id,
  outage_no,
  site_id,
  site_code,
  region,
  site_type,
  started_at,
  started_on,
  ended_at,
  outage_type,
  outage_status,
  affected_meter_count,
  duration_minutes
) AS
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

CREATE OR REPLACE VIEW ai.meter_overview (
  meter_id,
  meter_code,
  site_code,
  region,
  site_type,
  meter_type,
  tariff,
  installed_on,
  retired_on,
  reading_count,
  missing_reading_count,
  net_kwh_sum,
  first_interval_start,
  last_interval_start,
  site_outage_count
) AS
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

COMMENT ON VIEW ai.meter_readings IS E'query-man:source=energy-telemetry;view-contract=1\nGrain: one meter interval. Negative net_kwh means net export; missing readings are null and are not zero usage.';
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
COMMENT ON VIEW ai.outage_overview IS E'query-man:source=energy-telemetry;view-contract=1\nGrain: one outage report. Overlapping site intervals can double-count duration.';
COMMENT ON COLUMN ai.outage_overview.outage_no IS 'Synthetic human-readable outage reference.';
COMMENT ON COLUMN ai.outage_overview.site_id IS 'Synthetic site key; one site can have overlapping outage reports.';
COMMENT ON COLUMN ai.outage_overview.site_code IS 'Stable synthetic metered-site code with no service address.';
COMMENT ON COLUMN ai.outage_overview.region IS 'Synthetic region of the affected site.';
COMMENT ON COLUMN ai.outage_overview.site_type IS 'Synthetic operational category of the affected site.';
COMMENT ON COLUMN ai.outage_overview.started_on IS 'Asia/Seoul calendar date derived from started_at.';
COMMENT ON VIEW ai.meter_overview IS E'query-man:source=energy-telemetry;view-contract=1\nGrain: one meter, preserving meters with zero readings. net_kwh_sum stays null when no non-missing readings exist.';
COMMENT ON COLUMN ai.meter_overview.meter_code IS 'Stable synthetic meter code with no customer identity or service address.';
COMMENT ON COLUMN ai.meter_overview.site_code IS 'Stable synthetic metered-site code with no service address.';
COMMENT ON COLUMN ai.meter_overview.retired_on IS 'Meter retirement date; null while the synthetic meter remains active.';
COMMENT ON COLUMN ai.meter_overview.first_interval_start IS 'Earliest reading interval start; null when the meter has no readings.';
COMMENT ON COLUMN ai.meter_overview.last_interval_start IS 'Latest reading interval start; null when the meter has no readings.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;
REVOKE ALL ON SCHEMA energy FROM energy_telemetry_view_owner;
GRANT USAGE ON SCHEMA energy TO energy_telemetry_view_owner;
REVOKE ALL ON
  energy.sites,
  energy.meters,
  energy.meter_readings,
  energy.outages
FROM energy_telemetry_view_owner;
GRANT SELECT ON
  energy.sites,
  energy.meters,
  energy.meter_readings,
  energy.outages
TO energy_telemetry_view_owner;

REVOKE ALL ON SCHEMA ai FROM energy_telemetry_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO energy_telemetry_view_owner;
ALTER VIEW ai.meter_readings OWNER TO energy_telemetry_view_owner;
ALTER VIEW ai.outage_overview OWNER TO energy_telemetry_view_owner;
ALTER VIEW ai.meter_overview OWNER TO energy_telemetry_view_owner;
REVOKE CREATE ON SCHEMA ai FROM energy_telemetry_view_owner;

REVOKE ALL ON SCHEMA energy FROM energy_telemetry_reader;
REVOKE ALL ON
  energy.sites,
  energy.meters,
  energy.meter_readings,
  energy.outages
FROM energy_telemetry_reader;
REVOKE ALL ON SCHEMA ai FROM energy_telemetry_reader;
GRANT USAGE ON SCHEMA ai TO energy_telemetry_reader;
REVOKE ALL ON
  ai.meter_readings,
  ai.outage_overview,
  ai.meter_overview
FROM PUBLIC;
REVOKE ALL ON
  ai.meter_readings,
  ai.outage_overview,
  ai.meter_overview
FROM energy_telemetry_reader;
GRANT SELECT ON
  ai.meter_readings,
  ai.outage_overview,
  ai.meter_overview
TO energy_telemetry_reader;

COMMIT;
