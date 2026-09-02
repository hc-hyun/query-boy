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

COMMIT;
