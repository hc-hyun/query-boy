\connect saas_billing

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA billing FROM PUBLIC;
REVOKE ALL ON SCHEMA ai FROM PUBLIC;

CREATE TABLE IF NOT EXISTS billing.plans (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  plan_code text NOT NULL UNIQUE,
  name text NOT NULL,
  billing_period text NOT NULL,
  monthly_list_price numeric(14,2) NOT NULL,
  included_units numeric(16,2) NOT NULL,
  CONSTRAINT billing_plans_code_not_blank CHECK (btrim(plan_code) <> ''),
  CONSTRAINT billing_plans_name_not_blank CHECK (btrim(name) <> ''),
  CONSTRAINT billing_plans_period_valid CHECK (billing_period IN ('MONTHLY', 'ANNUAL')),
  CONSTRAINT billing_plans_values_nonnegative CHECK (monthly_list_price >= 0 AND included_units >= 0)
);

CREATE TABLE IF NOT EXISTS billing.tenants (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_code text NOT NULL UNIQUE,
  industry text NOT NULL,
  region text NOT NULL,
  acquired_on date NOT NULL,
  CONSTRAINT billing_tenants_code_not_blank CHECK (btrim(tenant_code) <> ''),
  CONSTRAINT billing_tenants_industry_not_blank CHECK (btrim(industry) <> ''),
  CONSTRAINT billing_tenants_region_not_blank CHECK (btrim(region) <> '')
);

CREATE TABLE IF NOT EXISTS billing.subscriptions (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subscription_no text NOT NULL UNIQUE,
  tenant_id bigint NOT NULL REFERENCES billing.tenants(id) ON DELETE RESTRICT,
  plan_id bigint NOT NULL REFERENCES billing.plans(id) ON DELETE RESTRICT,
  started_on date NOT NULL,
  ended_on date,
  subscription_status text NOT NULL,
  seats integer NOT NULL,
  mrr numeric(14,2) NOT NULL,
  canceled_at timestamptz,
  cancellation_reason text,
  CONSTRAINT billing_subscriptions_no_not_blank CHECK (btrim(subscription_no) <> ''),
  CONSTRAINT billing_subscriptions_status_valid CHECK (subscription_status IN ('TRIAL', 'ACTIVE', 'PAUSED', 'CANCELED', 'EXPIRED')),
  CONSTRAINT billing_subscriptions_seats_positive CHECK (seats > 0),
  CONSTRAINT billing_subscriptions_mrr_nonnegative CHECK (mrr >= 0),
  CONSTRAINT billing_subscriptions_end_valid CHECK (ended_on IS NULL OR ended_on >= started_on),
  CONSTRAINT billing_subscriptions_cancel_consistent CHECK (
    (subscription_status = 'CANCELED') = (canceled_at IS NOT NULL AND cancellation_reason IS NOT NULL)
  ),
  CONSTRAINT billing_subscriptions_terminal_end CHECK (
    (subscription_status IN ('CANCELED', 'EXPIRED')) = (ended_on IS NOT NULL)
  )
);

CREATE TABLE IF NOT EXISTS billing.invoices (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  invoice_no text NOT NULL UNIQUE,
  tenant_id bigint NOT NULL REFERENCES billing.tenants(id) ON DELETE RESTRICT,
  subscription_id bigint REFERENCES billing.subscriptions(id) ON DELETE SET NULL,
  issued_at timestamptz NOT NULL,
  due_on date NOT NULL,
  paid_at timestamptz,
  invoice_status text NOT NULL,
  currency text NOT NULL,
  billed_amount numeric(14,2) NOT NULL,
  paid_amount numeric(14,2) NOT NULL,
  credit_amount numeric(14,2) NOT NULL,
  CONSTRAINT billing_invoices_no_not_blank CHECK (btrim(invoice_no) <> ''),
  CONSTRAINT billing_invoices_status_valid CHECK (invoice_status IN ('DRAFT', 'OPEN', 'PARTIALLY_PAID', 'PAID', 'OVERDUE', 'VOID', 'CREDITED')),
  CONSTRAINT billing_invoices_currency_valid CHECK (currency IN ('KRW', 'USD', 'EUR')),
  CONSTRAINT billing_invoices_amounts_valid CHECK (
    billed_amount >= 0 AND paid_amount >= 0 AND credit_amount >= 0
    AND paid_amount + credit_amount <= billed_amount
  ),
  CONSTRAINT billing_invoices_paid_time_valid CHECK (paid_at IS NULL OR paid_at >= issued_at),
  CONSTRAINT billing_invoices_payment_consistent CHECK (
    (invoice_status = 'PAID' AND paid_at IS NOT NULL AND paid_amount + credit_amount = billed_amount)
    OR (invoice_status = 'PARTIALLY_PAID' AND paid_at IS NOT NULL AND paid_amount > 0 AND paid_amount + credit_amount < billed_amount)
    OR (invoice_status NOT IN ('PAID', 'PARTIALLY_PAID') AND paid_at IS NULL)
  )
);

CREATE TABLE IF NOT EXISTS billing.usage_daily (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  usage_key text NOT NULL UNIQUE,
  tenant_id bigint NOT NULL REFERENCES billing.tenants(id) ON DELETE RESTRICT,
  subscription_id bigint REFERENCES billing.subscriptions(id) ON DELETE SET NULL,
  usage_date date NOT NULL,
  product_area text NOT NULL,
  active_users integer NOT NULL,
  usage_units numeric(16,2) NOT NULL,
  included_units numeric(16,2) NOT NULL,
  overage_units numeric(16,2) NOT NULL,
  CONSTRAINT billing_usage_key_not_blank CHECK (btrim(usage_key) <> ''),
  CONSTRAINT billing_usage_product_not_blank CHECK (btrim(product_area) <> ''),
  CONSTRAINT billing_usage_values_nonnegative CHECK (
    active_users >= 0 AND usage_units >= 0 AND included_units >= 0 AND overage_units >= 0
  ),
  CONSTRAINT billing_usage_overage_consistent CHECK (
    overage_units = greatest(usage_units - included_units, 0::numeric)
  )
);

CREATE INDEX IF NOT EXISTS billing_subscriptions_tenant_status_idx
  ON billing.subscriptions(tenant_id, subscription_status, started_on DESC);
CREATE INDEX IF NOT EXISTS billing_subscriptions_plan_idx
  ON billing.subscriptions(plan_id);
CREATE INDEX IF NOT EXISTS billing_invoices_tenant_date_idx
  ON billing.invoices(tenant_id, issued_at DESC);
CREATE INDEX IF NOT EXISTS billing_invoices_subscription_idx
  ON billing.invoices(subscription_id);
CREATE INDEX IF NOT EXISTS billing_invoices_status_due_idx
  ON billing.invoices(invoice_status, due_on);
CREATE INDEX IF NOT EXISTS billing_usage_tenant_date_idx
  ON billing.usage_daily(tenant_id, usage_date DESC);
CREATE INDEX IF NOT EXISTS billing_usage_subscription_date_idx
  ON billing.usage_daily(subscription_id, usage_date DESC);

COMMENT ON SCHEMA billing IS 'Private physical tables for the synthetic SaaS subscription-and-billing domain lab.';
COMMENT ON TABLE billing.plans IS 'Grain: one SaaS plan. monthly_list_price and subscription MRR are synthetic USD-equivalent catalog values.';
COMMENT ON TABLE billing.tenants IS 'Grain: one synthetic enterprise tenant.';
COMMENT ON TABLE billing.subscriptions IS 'Grain: one subscription lifecycle; a tenant can have multiple sequential or overlapping records.';
COMMENT ON TABLE billing.invoices IS 'Grain: one invoice in exactly one currency. Credit and partial-payment states are explicit.';
COMMENT ON TABLE billing.usage_daily IS 'Grain: one tenant, optional subscription, product area, and usage date. Product areas may use different units.';

CREATE OR REPLACE VIEW ai.subscription_overview AS
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

CREATE OR REPLACE VIEW ai.invoice_overview AS
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

CREATE OR REPLACE VIEW ai.usage_daily AS
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

COMMENT ON VIEW ai.subscription_overview IS 'Grain: one subscription lifecycle. MRR is a point-in-record contract value, not recognized revenue; churn requires a cohort and time window.';
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
COMMENT ON VIEW ai.invoice_overview IS 'Grain: one invoice. Group monetary amounts by currency; invoice payment state is distinct from subscription state and product usage.';
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
COMMENT ON VIEW ai.usage_daily IS 'Grain: one tenant/product-area/day usage record. Do not add usage_units across unlike product areas without a common-unit definition.';
COMMENT ON COLUMN ai.usage_daily.usage_key IS 'Stable synthetic daily-usage reference.';
COMMENT ON COLUMN ai.usage_daily.tenant_id IS 'Synthetic tenant join key; one tenant can have many product-area daily usage rows.';
COMMENT ON COLUMN ai.usage_daily.tenant_code IS 'Stable synthetic enterprise-tenant code with no person identity or contact data.';
COMMENT ON COLUMN ai.usage_daily.industry IS 'Synthetic industry category of the enterprise tenant.';
COMMENT ON COLUMN ai.usage_daily.region IS 'Synthetic operating region of the enterprise tenant.';
COMMENT ON COLUMN ai.usage_daily.subscription_id IS 'Optional subscription join key; null for tenant-level usage not linked to a subscription.';
COMMENT ON COLUMN ai.usage_daily.subscription_no IS 'Optional synthetic subscription reference; null for tenant-level usage.';

GRANT USAGE ON SCHEMA billing TO saas_billing_view_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA billing TO saas_billing_view_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE query_man_admin IN SCHEMA billing
  GRANT SELECT ON TABLES TO saas_billing_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO saas_billing_view_owner;
ALTER VIEW ai.subscription_overview OWNER TO saas_billing_view_owner;
ALTER VIEW ai.invoice_overview OWNER TO saas_billing_view_owner;
ALTER VIEW ai.usage_daily OWNER TO saas_billing_view_owner;

REVOKE ALL ON SCHEMA billing FROM saas_billing_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA billing FROM saas_billing_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA billing FROM saas_billing_reader;
GRANT USAGE ON SCHEMA ai TO saas_billing_reader;
GRANT SELECT ON ai.subscription_overview, ai.invoice_overview, ai.usage_daily
  TO saas_billing_reader;

COMMIT;
