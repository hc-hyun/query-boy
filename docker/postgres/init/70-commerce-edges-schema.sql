\connect commerce_edges

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS commerce;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA commerce FROM PUBLIC;

CREATE TABLE IF NOT EXISTS commerce.orders (
  order_id uuid PRIMARY KEY,
  placed_at timestamptz NOT NULL,
  customer_id uuid NOT NULL,
  channel text NOT NULL,
  currency_code character(3) NOT NULL,
  gross_amount numeric(12, 2) NOT NULL,
  discount_amount numeric(12, 2),
  promised_on date,
  attributes jsonb NOT NULL DEFAULT '{}'::jsonb,
  status text NOT NULL,
  CONSTRAINT commerce_orders_channel_valid
    CHECK (channel IN ('WEB', 'STORE', 'PARTNER')),
  CONSTRAINT commerce_orders_currency_valid
    CHECK (currency_code ~ '^[A-Z]{3}$'),
  CONSTRAINT commerce_orders_gross_nonnegative
    CHECK (gross_amount >= 0),
  CONSTRAINT commerce_orders_discount_valid
    CHECK (
      discount_amount IS NULL
      OR (discount_amount >= 0 AND discount_amount <= gross_amount)
    ),
  CONSTRAINT commerce_orders_status_valid
    CHECK (status IN ('DRAFT', 'PAID', 'REFUNDED')),
  CONSTRAINT commerce_orders_promised_after_placement
    CHECK (promised_on IS NULL OR promised_on >= placed_at::date)
);

CREATE TABLE IF NOT EXISTS commerce.order_lines (
  order_id uuid NOT NULL
    REFERENCES commerce.orders(order_id) ON DELETE RESTRICT,
  line_no smallint NOT NULL,
  sku text NOT NULL,
  quantity integer NOT NULL,
  unit_price numeric(12, 2) NOT NULL,
  returned_at timestamptz,
  note text,
  CONSTRAINT commerce_order_lines_pk PRIMARY KEY (order_id, line_no),
  CONSTRAINT commerce_order_lines_number_positive CHECK (line_no > 0),
  CONSTRAINT commerce_order_lines_sku_not_blank CHECK (btrim(sku) <> ''),
  CONSTRAINT commerce_order_lines_quantity_positive CHECK (quantity > 0),
  CONSTRAINT commerce_order_lines_price_nonnegative CHECK (unit_price >= 0),
  CONSTRAINT commerce_order_lines_note_not_blank
    CHECK (note IS NULL OR btrim(note) <> '')
);

CREATE INDEX IF NOT EXISTS commerce_orders_placed_idx
  ON commerce.orders(placed_at);
CREATE INDEX IF NOT EXISTS commerce_orders_channel_placed_idx
  ON commerce.orders(channel, placed_at);
CREATE INDEX IF NOT EXISTS commerce_order_lines_returned_idx
  ON commerce.order_lines(returned_at)
  WHERE returned_at IS NOT NULL;

COMMENT ON TABLE commerce.orders IS
  'Grain: one commerce order, including draft orders that may have no line yet.';
COMMENT ON TABLE commerce.order_lines IS
  'Grain: one line number within one commerce order.';

CREATE OR REPLACE VIEW ai."Order"
WITH (security_barrier = true)
AS
SELECT
  order_row.order_id AS "OrderID",
  order_row.placed_at AS "PlacedAt",
  order_row.customer_id AS "CustomerID",
  order_row.channel AS "Channel",
  order_row.currency_code AS "CurrencyCode",
  order_row.gross_amount AS "GrossAmount",
  order_row.discount_amount AS "DiscountAmount",
  (
    order_row.gross_amount
    - coalesce(order_row.discount_amount, 0::numeric)
  )::numeric(12, 2) AS "NetAmount",
  order_row.promised_on AS "PromisedOn",
  order_row.attributes AS "Attributes",
  order_row.status AS "Status"
FROM commerce.orders AS order_row;

CREATE OR REPLACE VIEW ai."OrderLine"
WITH (security_barrier = true)
AS
SELECT
  line.order_id AS "OrderID",
  line.line_no AS "LineNo",
  order_row.placed_at AS "PlacedAt",
  line.sku AS "SKU",
  line.quantity AS "Quantity",
  line.unit_price AS "UnitPrice",
  (line.quantity * line.unit_price)::numeric(12, 2) AS "LineAmount",
  line.returned_at AS "ReturnedAt",
  line.note AS "Note"
FROM commerce.order_lines AS line
JOIN commerce.orders AS order_row ON order_row.order_id = line.order_id;

COMMENT ON VIEW ai."Order" IS
  'Grain: one commerce order. Quoted identifiers are intentional fixture coverage.';
COMMENT ON COLUMN ai."Order"."OrderID" IS
  'Stable UUID order grain key; use the emitted sql_name including quotes.';
COMMENT ON COLUMN ai."Order"."DiscountAmount" IS
  'Nullable discount; null means no discount was supplied.';
COMMENT ON COLUMN ai."Order"."Attributes" IS
  'Structured JSON attributes that may contain JSON null and Unicode text.';
COMMENT ON VIEW ai."OrderLine" IS
  'Grain: one order line identified by the composite OrderID and LineNo key.';
COMMENT ON COLUMN ai."OrderLine"."OrderID" IS
  'UUID join key to ai.Order.OrderID.';
COMMENT ON COLUMN ai."OrderLine"."ReturnedAt" IS
  'Nullable return timestamp; non-null means the line was returned.';

GRANT USAGE ON SCHEMA commerce TO commerce_edges_view_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA commerce TO commerce_edges_view_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE query_man_admin IN SCHEMA commerce
  GRANT SELECT ON TABLES TO commerce_edges_view_owner;

GRANT USAGE, CREATE ON SCHEMA ai TO commerce_edges_view_owner;
ALTER VIEW ai."Order" OWNER TO commerce_edges_view_owner;
ALTER VIEW ai."OrderLine" OWNER TO commerce_edges_view_owner;
REVOKE CREATE ON SCHEMA ai FROM commerce_edges_view_owner;

REVOKE ALL ON SCHEMA commerce FROM commerce_edges_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA commerce FROM commerce_edges_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA commerce FROM commerce_edges_reader;
GRANT USAGE ON SCHEMA ai TO commerce_edges_reader;
GRANT SELECT ON ai."Order", ai."OrderLine" TO commerce_edges_reader;

COMMIT;
