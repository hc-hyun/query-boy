\connect retail_commerce

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS retail;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA retail FROM PUBLIC;
REVOKE ALL ON SCHEMA ai FROM PUBLIC;

CREATE TABLE IF NOT EXISTS retail.retail_customers (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_code text NOT NULL UNIQUE,
  segment text NOT NULL,
  home_region text NOT NULL,
  joined_on date NOT NULL,
  CONSTRAINT retail_customers_code_not_blank CHECK (btrim(customer_code) <> ''),
  CONSTRAINT retail_customers_segment_valid CHECK (segment IN ('CONSUMER', 'SMB', 'VIP', 'GUEST')),
  CONSTRAINT retail_customers_region_not_blank CHECK (btrim(home_region) <> '')
);

CREATE TABLE IF NOT EXISTS retail.products (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_code text NOT NULL UNIQUE,
  name text NOT NULL,
  category text NOT NULL,
  brand text NOT NULL,
  list_price numeric(14,2) NOT NULL,
  launched_on date NOT NULL,
  CONSTRAINT retail_products_code_not_blank CHECK (btrim(product_code) <> ''),
  CONSTRAINT retail_products_name_not_blank CHECK (btrim(name) <> ''),
  CONSTRAINT retail_products_price_nonnegative CHECK (list_price >= 0)
);

CREATE TABLE IF NOT EXISTS retail.orders (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_no text NOT NULL UNIQUE,
  customer_id bigint NOT NULL REFERENCES retail.retail_customers(id) ON DELETE RESTRICT,
  ordered_at timestamptz NOT NULL,
  channel text NOT NULL,
  fulfillment_region text NOT NULL,
  payment_status text NOT NULL,
  order_status text NOT NULL,
  currency text NOT NULL,
  gross_amount numeric(14,2) NOT NULL,
  discount_amount numeric(14,2) NOT NULL,
  refunded_amount numeric(14,2) NOT NULL,
  delivered_at timestamptz,
  canceled_at timestamptz,
  CONSTRAINT retail_orders_no_not_blank CHECK (btrim(order_no) <> ''),
  CONSTRAINT retail_orders_channel_valid CHECK (channel IN ('ONLINE', 'STORE', 'MARKETPLACE')),
  CONSTRAINT retail_orders_payment_valid CHECK (payment_status IN ('PENDING', 'AUTHORIZED', 'PAID', 'PARTIALLY_REFUNDED', 'REFUNDED', 'FAILED')),
  CONSTRAINT retail_orders_status_valid CHECK (order_status IN ('PLACED', 'PROCESSING', 'SHIPPED', 'DELIVERED', 'CANCELED')),
  CONSTRAINT retail_orders_currency_valid CHECK (currency IN ('KRW', 'USD', 'EUR', 'JPY')),
  CONSTRAINT retail_orders_amounts_valid CHECK (
    gross_amount >= 0 AND discount_amount >= 0 AND refunded_amount >= 0
    AND discount_amount <= gross_amount
    AND refunded_amount <= gross_amount - discount_amount
  ),
  CONSTRAINT retail_orders_delivery_after_order CHECK (delivered_at IS NULL OR delivered_at >= ordered_at),
  CONSTRAINT retail_orders_cancel_after_order CHECK (canceled_at IS NULL OR canceled_at >= ordered_at),
  CONSTRAINT retail_orders_terminal_time_consistent CHECK (
    (order_status = 'DELIVERED') = (delivered_at IS NOT NULL)
    AND (order_status = 'CANCELED') = (canceled_at IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS retail.order_lines (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  order_id bigint NOT NULL REFERENCES retail.orders(id) ON DELETE CASCADE,
  line_no smallint NOT NULL,
  product_id bigint NOT NULL REFERENCES retail.products(id) ON DELETE RESTRICT,
  quantity integer NOT NULL,
  unit_price numeric(14,2) NOT NULL,
  discount_amount numeric(14,2) NOT NULL,
  returned_quantity integer NOT NULL,
  return_status text NOT NULL,
  return_requested_at timestamptz,
  CONSTRAINT retail_order_lines_order_line_unique UNIQUE (order_id, line_no),
  CONSTRAINT retail_order_lines_line_positive CHECK (line_no > 0),
  CONSTRAINT retail_order_lines_quantity_positive CHECK (quantity > 0),
  CONSTRAINT retail_order_lines_unit_price_nonnegative CHECK (unit_price >= 0),
  CONSTRAINT retail_order_lines_discount_valid CHECK (
    discount_amount >= 0 AND discount_amount <= unit_price * quantity
  ),
  CONSTRAINT retail_order_lines_return_quantity_valid CHECK (
    returned_quantity >= 0 AND returned_quantity <= quantity
  ),
  CONSTRAINT retail_order_lines_return_status_valid CHECK (
    return_status IN ('NONE', 'REQUESTED', 'PARTIAL', 'FULL', 'REJECTED')
  ),
  CONSTRAINT retail_order_lines_return_time_consistent CHECK (
    (return_status = 'NONE') = (return_requested_at IS NULL)
  )
);

CREATE INDEX IF NOT EXISTS retail_orders_customer_date_idx
  ON retail.orders(customer_id, ordered_at DESC);
CREATE INDEX IF NOT EXISTS retail_orders_status_date_idx
  ON retail.orders(order_status, ordered_at DESC);
CREATE INDEX IF NOT EXISTS retail_orders_region_date_idx
  ON retail.orders(fulfillment_region, ordered_at DESC);
CREATE INDEX IF NOT EXISTS retail_order_lines_product_idx
  ON retail.order_lines(product_id);
CREATE INDEX IF NOT EXISTS retail_order_lines_return_idx
  ON retail.order_lines(return_status, return_requested_at DESC);

COMMENT ON SCHEMA retail IS 'Private physical tables for the synthetic retail-commerce domain lab.';
COMMENT ON SCHEMA ai IS 'Curated read-only views for the retail-commerce Query Boy source.';
COMMENT ON TABLE retail.retail_customers IS 'Grain: one synthetic retail customer, including customers with no orders.';
COMMENT ON TABLE retail.products IS 'Grain: one sellable product.';
COMMENT ON TABLE retail.orders IS 'Grain: one order in exactly one currency; no foreign-exchange conversion is provided.';
COMMENT ON TABLE retail.order_lines IS 'Grain: one product line within one order.';

COMMIT;
