BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.order_overview (
  order_id,
  order_no,
  customer_code,
  customer_segment,
  home_region,
  ordered_at,
  ordered_on,
  channel,
  fulfillment_region,
  payment_status,
  order_status,
  currency,
  gross_amount,
  discount_amount,
  refunded_amount,
  net_collected_amount,
  delivered_at,
  canceled_at,
  line_count,
  item_quantity,
  returned_quantity
) AS
SELECT
  order_row.id AS order_id,
  order_row.order_no,
  customer.customer_code,
  customer.segment AS customer_segment,
  customer.home_region,
  order_row.ordered_at,
  (order_row.ordered_at AT TIME ZONE 'Asia/Seoul')::date AS ordered_on,
  order_row.channel,
  order_row.fulfillment_region,
  order_row.payment_status,
  order_row.order_status,
  order_row.currency,
  order_row.gross_amount,
  order_row.discount_amount,
  order_row.refunded_amount,
  CASE
    WHEN order_row.payment_status IN ('PAID', 'PARTIALLY_REFUNDED', 'REFUNDED')
      THEN (order_row.gross_amount - order_row.discount_amount - order_row.refunded_amount)::numeric
    ELSE 0::numeric
  END AS net_collected_amount,
  order_row.delivered_at,
  order_row.canceled_at,
  COALESCE(line_stats.line_count, 0)::integer AS line_count,
  COALESCE(line_stats.item_quantity, 0)::bigint AS item_quantity,
  COALESCE(line_stats.returned_quantity, 0)::bigint AS returned_quantity
FROM retail.orders AS order_row
JOIN retail.retail_customers AS customer ON customer.id = order_row.customer_id
LEFT JOIN (
  SELECT
    order_id,
    count(*) AS line_count,
    sum(quantity) AS item_quantity,
    sum(returned_quantity) AS returned_quantity
  FROM retail.order_lines
  GROUP BY order_id
) AS line_stats ON line_stats.order_id = order_row.id;

CREATE OR REPLACE VIEW ai.order_lines (
  order_line_id,
  order_id,
  order_no,
  ordered_at,
  customer_code,
  channel,
  fulfillment_region,
  order_status,
  currency,
  line_no,
  product_code,
  product_name,
  category,
  brand,
  quantity,
  unit_price,
  discount_amount,
  line_gross_amount,
  line_net_before_refund,
  returned_quantity,
  return_status,
  return_requested_at
) AS
SELECT
  line.id AS order_line_id,
  order_row.id AS order_id,
  order_row.order_no,
  order_row.ordered_at,
  customer.customer_code,
  order_row.channel,
  order_row.fulfillment_region,
  order_row.order_status,
  order_row.currency,
  line.line_no,
  product.product_code,
  product.name AS product_name,
  product.category,
  product.brand,
  line.quantity,
  line.unit_price,
  line.discount_amount,
  (line.unit_price * line.quantity)::numeric AS line_gross_amount,
  (line.unit_price * line.quantity - line.discount_amount)::numeric AS line_net_before_refund,
  line.returned_quantity,
  line.return_status,
  line.return_requested_at
FROM retail.order_lines AS line
JOIN retail.orders AS order_row ON order_row.id = line.order_id
JOIN retail.retail_customers AS customer ON customer.id = order_row.customer_id
JOIN retail.products AS product ON product.id = line.product_id;

CREATE OR REPLACE VIEW ai.customer_overview (
  customer_id,
  customer_code,
  customer_segment,
  home_region,
  joined_on,
  order_count,
  canceled_order_count,
  refunded_order_count,
  first_order_at,
  last_order_at
) AS
SELECT
  customer.id AS customer_id,
  customer.customer_code,
  customer.segment AS customer_segment,
  customer.home_region,
  customer.joined_on,
  COALESCE(order_stats.order_count, 0)::integer AS order_count,
  COALESCE(order_stats.canceled_order_count, 0)::integer AS canceled_order_count,
  COALESCE(order_stats.refunded_order_count, 0)::integer AS refunded_order_count,
  order_stats.first_order_at,
  order_stats.last_order_at
FROM retail.retail_customers AS customer
LEFT JOIN (
  SELECT
    customer_id,
    count(*) AS order_count,
    count(*) FILTER (WHERE order_status = 'CANCELED') AS canceled_order_count,
    count(*) FILTER (WHERE refunded_amount > 0) AS refunded_order_count,
    min(ordered_at) AS first_order_at,
    max(ordered_at) AS last_order_at
  FROM retail.orders
  GROUP BY customer_id
) AS order_stats ON order_stats.customer_id = customer.id;

COMMENT ON VIEW ai.order_overview IS E'query-man:source=retail-commerce;view-contract=1\nGrain: one retail order. Monetary amounts must be grouped by currency; no FX conversion exists.';
COMMENT ON COLUMN ai.order_overview.order_no IS 'Synthetic human-readable order reference; it is not a customer identifier.';
COMMENT ON COLUMN ai.order_overview.customer_segment IS 'Synthetic customer segment copied from the customer record at fixture generation time.';
COMMENT ON COLUMN ai.order_overview.home_region IS 'Synthetic customer home-region label; fulfillment_region is the order delivery region.';
COMMENT ON COLUMN ai.order_overview.ordered_on IS 'Asia/Seoul calendar date derived from ordered_at.';
COMMENT ON COLUMN ai.order_overview.discount_amount IS 'Order-level discount amount in the row currency; aggregate only within one currency.';
COMMENT ON COLUMN ai.order_overview.net_collected_amount IS 'Gross minus discount minus refunded amount only for PAID, PARTIALLY_REFUNDED, and REFUNDED payments; all unsettled statuses are zero. This is not accounting revenue.';
COMMENT ON COLUMN ai.order_overview.delivered_at IS 'Delivery-completion timestamp; null until the order is delivered or when delivery does not apply.';
COMMENT ON COLUMN ai.order_overview.canceled_at IS 'Cancellation timestamp; null when the order was not canceled.';
COMMENT ON COLUMN ai.order_overview.line_count IS 'Preaggregated line count that does not multiply order rows.';
COMMENT ON COLUMN ai.order_overview.item_quantity IS 'Preaggregated sum of ordered item quantity at order grain; zero means the order has no exposed lines.';
COMMENT ON COLUMN ai.order_overview.returned_quantity IS 'Preaggregated returned item quantity at order grain; zero means no item was returned.';
COMMENT ON VIEW ai.order_lines IS E'query-man:source=retail-commerce;view-contract=1\nGrain: one order line. Joining to ai.order_overview by order_id fans one order out to many lines.';
COMMENT ON COLUMN ai.order_lines.order_id IS 'Join key to ai.order_overview.order_id; joining expands one order to many order lines.';
COMMENT ON COLUMN ai.order_lines.order_no IS 'Synthetic human-readable order reference copied from the parent order.';
COMMENT ON COLUMN ai.order_lines.customer_code IS 'Stable synthetic customer code with no real-person identity or contact data.';
COMMENT ON COLUMN ai.order_lines.fulfillment_region IS 'Delivery region of the parent order, not the synthetic customer home region.';
COMMENT ON COLUMN ai.order_lines.order_status IS 'Current fixture status of the parent order repeated on each order line.';
COMMENT ON COLUMN ai.order_lines.line_no IS 'Line sequence within one order; it is unique only together with order_id.';
COMMENT ON COLUMN ai.order_lines.product_name IS 'Synthetic product display name; product_code is the stable product identifier.';
COMMENT ON COLUMN ai.order_lines.brand IS 'Synthetic product brand label.';
COMMENT ON COLUMN ai.order_lines.unit_price IS 'Per-item price in the row currency before line discount and refund.';
COMMENT ON COLUMN ai.order_lines.discount_amount IS 'Line-level discount amount in the row currency.';
COMMENT ON COLUMN ai.order_lines.line_gross_amount IS 'unit_price multiplied by quantity in the row currency, before line discount and refund.';
COMMENT ON COLUMN ai.order_lines.return_requested_at IS 'Return-request timestamp; null when no return was requested.';
COMMENT ON VIEW ai.customer_overview IS E'query-man:source=retail-commerce;view-contract=1\nGrain: one synthetic retail customer, including zero-order customers. Monetary totals are intentionally absent because currencies can differ.';
COMMENT ON COLUMN ai.customer_overview.customer_code IS 'Stable synthetic customer code with no real-person identity or contact data.';
COMMENT ON COLUMN ai.customer_overview.first_order_at IS 'Earliest order timestamp for the customer; null when the customer has no orders.';
COMMENT ON COLUMN ai.customer_overview.last_order_at IS 'Latest order timestamp for the customer; null when the customer has no orders.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;
REVOKE ALL ON SCHEMA retail FROM retail_commerce_view_owner;
GRANT USAGE ON SCHEMA retail TO retail_commerce_view_owner;
REVOKE ALL ON
  retail.retail_customers,
  retail.products,
  retail.orders,
  retail.order_lines
FROM retail_commerce_view_owner;
GRANT SELECT ON
  retail.retail_customers,
  retail.products,
  retail.orders,
  retail.order_lines
TO retail_commerce_view_owner;

REVOKE ALL ON SCHEMA ai FROM retail_commerce_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO retail_commerce_view_owner;
ALTER VIEW ai.order_overview OWNER TO retail_commerce_view_owner;
ALTER VIEW ai.order_lines OWNER TO retail_commerce_view_owner;
ALTER VIEW ai.customer_overview OWNER TO retail_commerce_view_owner;
REVOKE CREATE ON SCHEMA ai FROM retail_commerce_view_owner;

REVOKE ALL ON SCHEMA retail FROM retail_commerce_reader;
REVOKE ALL ON
  retail.retail_customers,
  retail.products,
  retail.orders,
  retail.order_lines
FROM retail_commerce_reader;
REVOKE ALL ON SCHEMA ai FROM retail_commerce_reader;
GRANT USAGE ON SCHEMA ai TO retail_commerce_reader;
REVOKE ALL ON
  ai.order_overview,
  ai.order_lines,
  ai.customer_overview
FROM PUBLIC;
REVOKE ALL ON
  ai.order_overview,
  ai.order_lines,
  ai.customer_overview
FROM retail_commerce_reader;
GRANT SELECT ON
  ai.order_overview,
  ai.order_lines,
  ai.customer_overview
TO retail_commerce_reader;

COMMIT;
