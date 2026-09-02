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

COMMIT;
