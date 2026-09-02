BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.shipment_overview (
  shipment_id,
  tracking_no,
  origin_hub_code,
  origin_region,
  destination_hub_code,
  destination_region,
  service_level,
  customer_segment,
  shipped_at,
  shipped_on,
  promised_at,
  delivered_at,
  shipment_status,
  delivery_outcome,
  exception_type,
  weight_kg,
  package_value,
  delivery_delay_minutes,
  tracking_event_count,
  first_scanned_at,
  last_scanned_at,
  last_recorded_at
) AS
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

CREATE OR REPLACE VIEW ai.tracking_events (
  tracking_event_id,
  event_key,
  shipment_id,
  tracking_no,
  service_level,
  shipment_status,
  event_sequence,
  hub_code,
  hub_region,
  event_code,
  event_status,
  scanned_at,
  recorded_at,
  ingest_lag_minutes
) AS
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

CREATE OR REPLACE VIEW ai.hub_overview (
  hub_id,
  hub_code,
  hub_name,
  region,
  opened_on,
  origin_shipment_count,
  destination_shipment_count,
  tracking_event_count,
  last_scanned_at
) AS
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

COMMENT ON VIEW ai.shipment_overview IS E'query-man:source=parcel-logistics;view-contract=1\nGrain: one shipment. tracking_event_count is preaggregated and missing scans remain zero.';
COMMENT ON COLUMN ai.shipment_overview.origin_hub_code IS 'Stable synthetic code of the shipment origin hub.';
COMMENT ON COLUMN ai.shipment_overview.origin_region IS 'Region of the shipment origin hub.';
COMMENT ON COLUMN ai.shipment_overview.destination_hub_code IS 'Stable synthetic code of the shipment destination hub.';
COMMENT ON COLUMN ai.shipment_overview.destination_region IS 'Region of the shipment destination hub.';
COMMENT ON COLUMN ai.shipment_overview.customer_segment IS 'Synthetic shipment customer segment; no customer identity or contact data is exposed.';
COMMENT ON COLUMN ai.shipment_overview.shipped_on IS 'Asia/Seoul calendar date derived from shipped_at.';
COMMENT ON COLUMN ai.shipment_overview.delivery_outcome IS 'Stable fixture classification: delivered on/before promise, delivered late, pending, or lost/not applicable.';
COMMENT ON COLUMN ai.shipment_overview.weight_kg IS 'Shipment package weight measured in kilograms.';
COMMENT ON COLUMN ai.shipment_overview.package_value IS 'Synthetic package-value amount without currency or FX metadata; do not report it as revenue or combine it with monetary sources.';
COMMENT ON COLUMN ai.shipment_overview.first_scanned_at IS 'Earliest physical scan timestamp; null when the shipment has no tracking events.';
COMMENT ON COLUMN ai.shipment_overview.last_scanned_at IS 'Latest physical scan timestamp; null when the shipment has no tracking events.';
COMMENT ON COLUMN ai.shipment_overview.last_recorded_at IS 'Latest ingest timestamp among tracking events; null when the shipment has no tracking events.';
COMMENT ON VIEW ai.tracking_events IS E'query-man:source=parcel-logistics;view-contract=1\nGrain: one tracking event. Joining to shipment overview by shipment_id fans one shipment out to many scans.';
COMMENT ON COLUMN ai.tracking_events.event_key IS 'Stable synthetic tracking-event reference.';
COMMENT ON COLUMN ai.tracking_events.shipment_id IS 'Join key to ai.shipment_overview.shipment_id; joining fans one shipment out to many tracking events.';
COMMENT ON COLUMN ai.tracking_events.tracking_no IS 'Synthetic human-readable tracking reference copied from the parent shipment.';
COMMENT ON COLUMN ai.tracking_events.service_level IS 'Service level copied from the parent shipment.';
COMMENT ON COLUMN ai.tracking_events.shipment_status IS 'Current fixture status of the parent shipment repeated on each tracking event.';
COMMENT ON COLUMN ai.tracking_events.hub_code IS 'Synthetic hub code for the scan; null when an event has no hub.';
COMMENT ON COLUMN ai.tracking_events.hub_region IS 'Region of the scan hub; null when an event has no hub.';
COMMENT ON COLUMN ai.tracking_events.ingest_lag_minutes IS 'Recorded time minus scan time; use recorded_at for ingest chronology and scanned_at for physical-event chronology.';
COMMENT ON VIEW ai.hub_overview IS E'query-man:source=parcel-logistics;view-contract=1\nGrain: one hub, preserving hubs with zero shipment or scan activity.';
COMMENT ON COLUMN ai.hub_overview.hub_code IS 'Stable synthetic hub identifier.';
COMMENT ON COLUMN ai.hub_overview.hub_name IS 'Synthetic hub display name.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;
REVOKE ALL ON SCHEMA logistics FROM parcel_logistics_view_owner;
GRANT USAGE ON SCHEMA logistics TO parcel_logistics_view_owner;
REVOKE ALL ON
  logistics.hubs,
  logistics.shipments,
  logistics.tracking_events
FROM parcel_logistics_view_owner;
GRANT SELECT ON
  logistics.hubs,
  logistics.shipments,
  logistics.tracking_events
TO parcel_logistics_view_owner;

REVOKE ALL ON SCHEMA ai FROM parcel_logistics_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO parcel_logistics_view_owner;
ALTER VIEW ai.shipment_overview OWNER TO parcel_logistics_view_owner;
ALTER VIEW ai.tracking_events OWNER TO parcel_logistics_view_owner;
ALTER VIEW ai.hub_overview OWNER TO parcel_logistics_view_owner;
REVOKE CREATE ON SCHEMA ai FROM parcel_logistics_view_owner;

REVOKE ALL ON SCHEMA logistics FROM parcel_logistics_reader;
REVOKE ALL ON
  logistics.hubs,
  logistics.shipments,
  logistics.tracking_events
FROM parcel_logistics_reader;
REVOKE ALL ON SCHEMA ai FROM parcel_logistics_reader;
GRANT USAGE ON SCHEMA ai TO parcel_logistics_reader;
REVOKE ALL ON
  ai.shipment_overview,
  ai.tracking_events,
  ai.hub_overview
FROM PUBLIC;
REVOKE ALL ON
  ai.shipment_overview,
  ai.tracking_events,
  ai.hub_overview
FROM parcel_logistics_reader;
GRANT SELECT ON
  ai.shipment_overview,
  ai.tracking_events,
  ai.hub_overview
TO parcel_logistics_reader;

COMMIT;
