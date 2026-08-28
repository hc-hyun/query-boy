\connect parcel_logistics

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS logistics;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA logistics FROM PUBLIC;
REVOKE ALL ON SCHEMA ai FROM PUBLIC;

CREATE TABLE IF NOT EXISTS logistics.hubs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  hub_code text NOT NULL UNIQUE,
  name text NOT NULL,
  region text NOT NULL,
  opened_on date NOT NULL,
  CONSTRAINT logistics_hubs_code_not_blank CHECK (btrim(hub_code) <> ''),
  CONSTRAINT logistics_hubs_name_not_blank CHECK (btrim(name) <> ''),
  CONSTRAINT logistics_hubs_region_not_blank CHECK (btrim(region) <> '')
);

CREATE TABLE IF NOT EXISTS logistics.shipments (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tracking_no text NOT NULL UNIQUE,
  origin_hub_id bigint NOT NULL REFERENCES logistics.hubs(id) ON DELETE RESTRICT,
  destination_hub_id bigint NOT NULL REFERENCES logistics.hubs(id) ON DELETE RESTRICT,
  service_level text NOT NULL,
  customer_segment text NOT NULL,
  shipped_at timestamptz NOT NULL,
  promised_at timestamptz NOT NULL,
  delivered_at timestamptz,
  shipment_status text NOT NULL,
  delivery_outcome text NOT NULL,
  exception_type text,
  weight_kg numeric(10,3) NOT NULL,
  package_value numeric(14,2) NOT NULL,
  CONSTRAINT logistics_shipments_tracking_not_blank CHECK (btrim(tracking_no) <> ''),
  CONSTRAINT logistics_shipments_distinct_hubs CHECK (origin_hub_id <> destination_hub_id),
  CONSTRAINT logistics_shipments_service_valid CHECK (service_level IN ('STANDARD', 'EXPRESS', 'SAME_DAY')),
  CONSTRAINT logistics_shipments_segment_valid CHECK (customer_segment IN ('CONSUMER', 'SMB', 'ENTERPRISE')),
  CONSTRAINT logistics_shipments_promise_after_ship CHECK (promised_at > shipped_at),
  CONSTRAINT logistics_shipments_delivery_after_ship CHECK (delivered_at IS NULL OR delivered_at >= shipped_at),
  CONSTRAINT logistics_shipments_status_valid CHECK (shipment_status IN ('CREATED', 'IN_TRANSIT', 'DELIVERED', 'EXCEPTION', 'LOST')),
  CONSTRAINT logistics_shipments_outcome_valid CHECK (delivery_outcome IN ('PENDING', 'ON_TIME', 'LATE', 'NOT_APPLICABLE')),
  CONSTRAINT logistics_shipments_exception_valid CHECK (exception_type IS NULL OR exception_type IN ('WEATHER', 'ADDRESS', 'DAMAGED', 'LOST', 'CUSTOMS')),
  CONSTRAINT logistics_shipments_delivery_consistent CHECK ((shipment_status = 'DELIVERED') = (delivered_at IS NOT NULL)),
  CONSTRAINT logistics_shipments_outcome_consistent CHECK (
    (delivery_outcome = 'ON_TIME' AND delivered_at IS NOT NULL AND delivered_at <= promised_at)
    OR (delivery_outcome = 'LATE' AND delivered_at IS NOT NULL AND delivered_at > promised_at)
    OR (delivery_outcome = 'PENDING' AND delivered_at IS NULL AND shipment_status NOT IN ('LOST'))
    OR (delivery_outcome = 'NOT_APPLICABLE' AND delivered_at IS NULL AND shipment_status = 'LOST')
  ),
  CONSTRAINT logistics_shipments_lost_consistent CHECK ((shipment_status = 'LOST') = (exception_type = 'LOST')),
  CONSTRAINT logistics_shipments_weight_positive CHECK (weight_kg > 0),
  CONSTRAINT logistics_shipments_value_nonnegative CHECK (package_value >= 0)
);

CREATE TABLE IF NOT EXISTS logistics.tracking_events (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  event_key text NOT NULL UNIQUE,
  shipment_id bigint NOT NULL REFERENCES logistics.shipments(id) ON DELETE CASCADE,
  event_sequence integer NOT NULL,
  hub_id bigint REFERENCES logistics.hubs(id) ON DELETE RESTRICT,
  event_code text NOT NULL,
  event_status text NOT NULL,
  scanned_at timestamptz NOT NULL,
  recorded_at timestamptz NOT NULL,
  CONSTRAINT logistics_tracking_events_shipment_sequence_unique UNIQUE (shipment_id, event_sequence),
  CONSTRAINT logistics_tracking_events_key_not_blank CHECK (btrim(event_key) <> ''),
  CONSTRAINT logistics_tracking_events_sequence_positive CHECK (event_sequence > 0),
  CONSTRAINT logistics_tracking_events_code_valid CHECK (event_code IN ('PICKED_UP', 'ARRIVED_HUB', 'DEPARTED_HUB', 'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION')),
  CONSTRAINT logistics_tracking_events_status_valid CHECK (event_status IN ('RECORDED', 'DUPLICATE', 'LATE_INGEST'))
);

CREATE INDEX IF NOT EXISTS logistics_shipments_origin_date_idx
  ON logistics.shipments(origin_hub_id, shipped_at DESC);
CREATE INDEX IF NOT EXISTS logistics_shipments_destination_date_idx
  ON logistics.shipments(destination_hub_id, shipped_at DESC);
CREATE INDEX IF NOT EXISTS logistics_shipments_status_promise_idx
  ON logistics.shipments(shipment_status, promised_at DESC);
CREATE INDEX IF NOT EXISTS logistics_tracking_events_shipment_scan_idx
  ON logistics.tracking_events(shipment_id, scanned_at, id);
CREATE INDEX IF NOT EXISTS logistics_tracking_events_hub_scan_idx
  ON logistics.tracking_events(hub_id, scanned_at DESC);

COMMENT ON SCHEMA logistics IS 'Private physical tables for the synthetic parcel-logistics domain lab.';
COMMENT ON TABLE logistics.hubs IS 'Grain: one parcel hub, including hubs without traffic.';
COMMENT ON TABLE logistics.shipments IS 'Grain: one shipment. delivery_outcome is the fixture-stable promise comparison.';
COMMENT ON TABLE logistics.tracking_events IS 'Grain: one ingested tracking event. Sequence, scan time, and record time may order differently; tied scan times are intentional.';

CREATE OR REPLACE VIEW ai.shipment_overview AS
SELECT
  shipment.id AS shipment_id,
  shipment.tracking_no,
  origin.hub_code AS origin_hub_code,
  origin.region AS origin_region,
  destination.hub_code AS destination_hub_code,
  destination.region AS destination_region,
  shipment.service_level,
  shipment.customer_segment,
  shipment.shipped_at,
  (shipment.shipped_at AT TIME ZONE 'Asia/Seoul')::date AS shipped_on,
  shipment.promised_at,
  shipment.delivered_at,
  shipment.shipment_status,
  shipment.delivery_outcome,
  shipment.exception_type,
  shipment.weight_kg,
  shipment.package_value,
  CASE
    WHEN shipment.delivered_at IS NULL THEN NULL
    ELSE round(extract(epoch FROM (shipment.delivered_at - shipment.promised_at))::numeric / 60, 2)
  END AS delivery_delay_minutes,
  COALESCE(event_stats.event_count, 0)::integer AS tracking_event_count,
  event_stats.first_scanned_at,
  event_stats.last_scanned_at,
  event_stats.last_recorded_at
FROM logistics.shipments AS shipment
JOIN logistics.hubs AS origin ON origin.id = shipment.origin_hub_id
JOIN logistics.hubs AS destination ON destination.id = shipment.destination_hub_id
LEFT JOIN (
  SELECT
    shipment_id,
    count(*) AS event_count,
    min(scanned_at) AS first_scanned_at,
    max(scanned_at) AS last_scanned_at,
    max(recorded_at) AS last_recorded_at
  FROM logistics.tracking_events
  GROUP BY shipment_id
) AS event_stats ON event_stats.shipment_id = shipment.id;

CREATE OR REPLACE VIEW ai.tracking_events AS
SELECT
  event.id AS tracking_event_id,
  event.event_key,
  shipment.id AS shipment_id,
  shipment.tracking_no,
  shipment.service_level,
  shipment.shipment_status,
  event.event_sequence,
  hub.hub_code,
  hub.region AS hub_region,
  event.event_code,
  event.event_status,
  event.scanned_at,
  event.recorded_at,
  round(extract(epoch FROM (event.recorded_at - event.scanned_at))::numeric / 60, 2) AS ingest_lag_minutes
FROM logistics.tracking_events AS event
JOIN logistics.shipments AS shipment ON shipment.id = event.shipment_id
LEFT JOIN logistics.hubs AS hub ON hub.id = event.hub_id;

CREATE OR REPLACE VIEW ai.hub_overview AS
SELECT
  hub.id AS hub_id,
  hub.hub_code,
  hub.name AS hub_name,
  hub.region,
  hub.opened_on,
  COALESCE(origin_stats.origin_shipment_count, 0)::integer AS origin_shipment_count,
  COALESCE(destination_stats.destination_shipment_count, 0)::integer AS destination_shipment_count,
  COALESCE(event_stats.tracking_event_count, 0)::integer AS tracking_event_count,
  event_stats.last_scanned_at
FROM logistics.hubs AS hub
LEFT JOIN (
  SELECT origin_hub_id, count(*) AS origin_shipment_count
  FROM logistics.shipments GROUP BY origin_hub_id
) AS origin_stats ON origin_stats.origin_hub_id = hub.id
LEFT JOIN (
  SELECT destination_hub_id, count(*) AS destination_shipment_count
  FROM logistics.shipments GROUP BY destination_hub_id
) AS destination_stats ON destination_stats.destination_hub_id = hub.id
LEFT JOIN (
  SELECT hub_id, count(*) AS tracking_event_count, max(scanned_at) AS last_scanned_at
  FROM logistics.tracking_events WHERE hub_id IS NOT NULL GROUP BY hub_id
) AS event_stats ON event_stats.hub_id = hub.id;

COMMENT ON VIEW ai.shipment_overview IS 'Grain: one shipment. tracking_event_count is preaggregated and missing scans remain zero.';
COMMENT ON COLUMN ai.shipment_overview.delivery_outcome IS 'Stable fixture classification: delivered on/before promise, delivered late, pending, or lost/not applicable.';
COMMENT ON VIEW ai.tracking_events IS 'Grain: one tracking event. Joining to shipment overview by shipment_id fans one shipment out to many scans.';
COMMENT ON COLUMN ai.tracking_events.ingest_lag_minutes IS 'Recorded time minus scan time; use recorded_at for ingest chronology and scanned_at for physical-event chronology.';
COMMENT ON VIEW ai.hub_overview IS 'Grain: one hub, preserving hubs with zero shipment or scan activity.';

GRANT USAGE ON SCHEMA logistics TO parcel_logistics_view_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA logistics TO parcel_logistics_view_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE query_man_admin IN SCHEMA logistics
  GRANT SELECT ON TABLES TO parcel_logistics_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO parcel_logistics_view_owner;
ALTER VIEW ai.shipment_overview OWNER TO parcel_logistics_view_owner;
ALTER VIEW ai.tracking_events OWNER TO parcel_logistics_view_owner;
ALTER VIEW ai.hub_overview OWNER TO parcel_logistics_view_owner;

REVOKE ALL ON SCHEMA logistics FROM parcel_logistics_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA logistics FROM parcel_logistics_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA logistics FROM parcel_logistics_reader;
GRANT USAGE ON SCHEMA ai TO parcel_logistics_reader;
GRANT SELECT ON ai.shipment_overview, ai.tracking_events, ai.hub_overview
  TO parcel_logistics_reader;

COMMIT;
