BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.subscription_overview (
  subscription_id,
  subscription_no,
  tenant_id,
  tenant_code,
  industry,
  region,
  acquired_on,
  plan_code,
  plan_name,
  billing_period,
  monthly_list_price,
  plan_included_units,
  started_on,
  ended_on,
  subscription_status,
  seats,
  mrr,
  canceled_at,
  cancellation_reason,
  invoice_count,
  unsettled_invoice_count,
  usage_row_count,
  first_usage_date,
  last_usage_date
) AS
SELECT
  subscription.id AS subscription_id,
  subscription.subscription_no,
  tenant.id AS tenant_id,
  tenant.tenant_code,
  tenant.industry,
  tenant.region,
  tenant.acquired_on,
  plan.plan_code,
  plan.name AS plan_name,
  plan.billing_period,
  plan.monthly_list_price,
  plan.included_units AS plan_included_units,
  subscription.started_on,
  subscription.ended_on,
  subscription.subscription_status,
  subscription.seats,
  subscription.mrr,
  subscription.canceled_at,
  subscription.cancellation_reason,
  COALESCE(invoice_stats.invoice_count, 0)::integer AS invoice_count,
  COALESCE(invoice_stats.unsettled_invoice_count, 0)::integer AS unsettled_invoice_count,
  COALESCE(usage_stats.usage_row_count, 0)::integer AS usage_row_count,
  usage_stats.first_usage_date,
  usage_stats.last_usage_date
FROM billing.subscriptions AS subscription
JOIN billing.tenants AS tenant ON tenant.id = subscription.tenant_id
JOIN billing.plans AS plan ON plan.id = subscription.plan_id
LEFT JOIN (
  SELECT
    subscription_id,
    count(*) AS invoice_count,
    count(*) FILTER (WHERE invoice_status IN ('OPEN', 'PARTIALLY_PAID', 'OVERDUE')) AS unsettled_invoice_count
  FROM billing.invoices WHERE subscription_id IS NOT NULL GROUP BY subscription_id
) AS invoice_stats ON invoice_stats.subscription_id = subscription.id
LEFT JOIN (
  SELECT
    subscription_id,
    count(*) AS usage_row_count,
    min(usage_date) AS first_usage_date,
    max(usage_date) AS last_usage_date
  FROM billing.usage_daily WHERE subscription_id IS NOT NULL GROUP BY subscription_id
) AS usage_stats ON usage_stats.subscription_id = subscription.id;

CREATE OR REPLACE VIEW ai.invoice_overview (
  invoice_id,
  invoice_no,
  tenant_id,
  tenant_code,
  industry,
  region,
  subscription_id,
  subscription_no,
  plan_code,
  plan_name,
  issued_at,
  issued_on,
  due_on,
  paid_at,
  invoice_status,
  currency,
  billed_amount,
  paid_amount,
  credit_amount,
  outstanding_amount
) AS
SELECT
  invoice.id AS invoice_id,
  invoice.invoice_no,
  tenant.id AS tenant_id,
  tenant.tenant_code,
  tenant.industry,
  tenant.region,
  subscription.id AS subscription_id,
  subscription.subscription_no,
  plan.plan_code,
  plan.name AS plan_name,
  invoice.issued_at,
  (invoice.issued_at AT TIME ZONE 'UTC')::date AS issued_on,
  invoice.due_on,
  invoice.paid_at,
  invoice.invoice_status,
  invoice.currency,
  invoice.billed_amount,
  invoice.paid_amount,
  invoice.credit_amount,
  (invoice.billed_amount - invoice.paid_amount - invoice.credit_amount)::numeric AS outstanding_amount
FROM billing.invoices AS invoice
JOIN billing.tenants AS tenant ON tenant.id = invoice.tenant_id
LEFT JOIN billing.subscriptions AS subscription ON subscription.id = invoice.subscription_id
LEFT JOIN billing.plans AS plan ON plan.id = subscription.plan_id;

CREATE OR REPLACE VIEW ai.usage_daily (
  usage_id,
  usage_key,
  tenant_id,
  tenant_code,
  industry,
  region,
  subscription_id,
  subscription_no,
  subscription_status,
  usage_date,
  product_area,
  active_users,
  usage_units,
  included_units,
  overage_units
) AS
SELECT
  usage.id AS usage_id,
  usage.usage_key,
  tenant.id AS tenant_id,
  tenant.tenant_code,
  tenant.industry,
  tenant.region,
  subscription.id AS subscription_id,
  subscription.subscription_no,
  subscription.subscription_status,
  usage.usage_date,
  usage.product_area,
  usage.active_users,
  usage.usage_units,
  usage.included_units,
  usage.overage_units
FROM billing.usage_daily AS usage
JOIN billing.tenants AS tenant ON tenant.id = usage.tenant_id
LEFT JOIN billing.subscriptions AS subscription ON subscription.id = usage.subscription_id;

COMMENT ON VIEW ai.subscription_overview IS E'query-man:source=saas-billing;view-contract=1\nGrain: one subscription lifecycle. MRR is a point-in-record contract value, not recognized revenue; churn requires a cohort and time window.';
COMMENT ON COLUMN ai.subscription_overview.subscription_no IS 'Stable synthetic human-readable subscription reference.';
COMMENT ON COLUMN ai.subscription_overview.tenant_id IS 'Synthetic tenant key; one tenant can have multiple subscription lifecycle records.';
COMMENT ON COLUMN ai.subscription_overview.acquired_on IS 'Synthetic tenant acquisition date, not the subscription start date.';
COMMENT ON COLUMN ai.subscription_overview.plan_name IS 'Synthetic plan display name; plan_code is the stable plan identifier.';
COMMENT ON COLUMN ai.subscription_overview.monthly_list_price IS 'Synthetic USD-equivalent monthly catalog price; it is not recognized revenue.';
COMMENT ON COLUMN ai.subscription_overview.plan_included_units IS 'Plan allowance in a product-specific usage unit; do not combine unlike product areas.';
COMMENT ON COLUMN ai.subscription_overview.ended_on IS 'Subscription lifecycle end date; null while the record remains open.';
COMMENT ON COLUMN ai.subscription_overview.canceled_at IS 'Cancellation timestamp; null when the subscription record was not canceled.';
COMMENT ON COLUMN ai.subscription_overview.invoice_count IS 'Preaggregated invoice count at subscription grain; zero means no linked invoice.';
COMMENT ON COLUMN ai.subscription_overview.first_usage_date IS 'Earliest linked usage date; null when the subscription has no linked usage.';
COMMENT ON COLUMN ai.subscription_overview.last_usage_date IS 'Latest linked usage date; null when the subscription has no linked usage.';
COMMENT ON VIEW ai.invoice_overview IS E'query-man:source=saas-billing;view-contract=1\nGrain: one invoice. Group monetary amounts by currency; invoice payment state is distinct from subscription state and product usage.';
COMMENT ON COLUMN ai.invoice_overview.tenant_id IS 'Synthetic tenant join key; joining to subscription or usage rows can fan one tenant out.';
COMMENT ON COLUMN ai.invoice_overview.tenant_code IS 'Stable synthetic enterprise-tenant code with no person identity or contact data.';
COMMENT ON COLUMN ai.invoice_overview.industry IS 'Synthetic industry category of the enterprise tenant.';
COMMENT ON COLUMN ai.invoice_overview.region IS 'Synthetic operating region of the enterprise tenant.';
COMMENT ON COLUMN ai.invoice_overview.subscription_id IS 'Optional subscription join key; null for tenant-level invoices not linked to a subscription.';
COMMENT ON COLUMN ai.invoice_overview.subscription_no IS 'Optional synthetic subscription reference; null for tenant-level invoices.';
COMMENT ON COLUMN ai.invoice_overview.plan_code IS 'Plan code of the linked subscription; null when no subscription is linked.';
COMMENT ON COLUMN ai.invoice_overview.plan_name IS 'Plan display name of the linked subscription; null when no subscription is linked.';
COMMENT ON COLUMN ai.invoice_overview.issued_on IS 'UTC calendar date derived from issued_at.';
COMMENT ON COLUMN ai.invoice_overview.paid_at IS 'Payment-completion timestamp; null for unpaid, partially paid, void, or credited states without completion.';
COMMENT ON VIEW ai.usage_daily IS E'query-man:source=saas-billing;view-contract=1\nGrain: one tenant/product-area/day usage record. Do not add usage_units across unlike product areas without a common-unit definition.';
COMMENT ON COLUMN ai.usage_daily.usage_key IS 'Stable synthetic daily-usage reference.';
COMMENT ON COLUMN ai.usage_daily.tenant_id IS 'Synthetic tenant join key; one tenant can have many product-area daily usage rows.';
COMMENT ON COLUMN ai.usage_daily.tenant_code IS 'Stable synthetic enterprise-tenant code with no person identity or contact data.';
COMMENT ON COLUMN ai.usage_daily.industry IS 'Synthetic industry category of the enterprise tenant.';
COMMENT ON COLUMN ai.usage_daily.region IS 'Synthetic operating region of the enterprise tenant.';
COMMENT ON COLUMN ai.usage_daily.subscription_id IS 'Optional subscription join key; null for tenant-level usage not linked to a subscription.';
COMMENT ON COLUMN ai.usage_daily.subscription_no IS 'Optional synthetic subscription reference; null for tenant-level usage.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;
REVOKE ALL ON SCHEMA billing FROM saas_billing_view_owner;
GRANT USAGE ON SCHEMA billing TO saas_billing_view_owner;
REVOKE ALL ON
  billing.plans,
  billing.tenants,
  billing.subscriptions,
  billing.invoices,
  billing.usage_daily
FROM saas_billing_view_owner;
GRANT SELECT ON
  billing.plans,
  billing.tenants,
  billing.subscriptions,
  billing.invoices,
  billing.usage_daily
TO saas_billing_view_owner;

REVOKE ALL ON SCHEMA ai FROM saas_billing_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO saas_billing_view_owner;
ALTER VIEW ai.subscription_overview OWNER TO saas_billing_view_owner;
ALTER VIEW ai.invoice_overview OWNER TO saas_billing_view_owner;
ALTER VIEW ai.usage_daily OWNER TO saas_billing_view_owner;
REVOKE CREATE ON SCHEMA ai FROM saas_billing_view_owner;

REVOKE ALL ON SCHEMA billing FROM saas_billing_reader;
REVOKE ALL ON
  billing.plans,
  billing.tenants,
  billing.subscriptions,
  billing.invoices,
  billing.usage_daily
FROM saas_billing_reader;
REVOKE ALL ON SCHEMA ai FROM saas_billing_reader;
GRANT USAGE ON SCHEMA ai TO saas_billing_reader;
REVOKE ALL ON
  ai.subscription_overview,
  ai.invoice_overview,
  ai.usage_daily
FROM PUBLIC;
REVOKE ALL ON
  ai.subscription_overview,
  ai.invoice_overview,
  ai.usage_daily
FROM saas_billing_reader;
GRANT SELECT ON
  ai.subscription_overview,
  ai.invoice_overview,
  ai.usage_daily
TO saas_billing_reader;

COMMIT;
