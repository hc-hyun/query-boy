\connect support_tickets

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS support;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA support FROM PUBLIC;

CREATE TABLE IF NOT EXISTS support.tickets (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ticket_no text NOT NULL UNIQUE,
  opened_at timestamptz NOT NULL,
  closed_at timestamptz,
  queue_name text NOT NULL,
  priority text NOT NULL,
  status text NOT NULL,
  customer_region text NOT NULL,
  subject text NOT NULL,
  CONSTRAINT support_ticket_number_valid CHECK (ticket_no ~ '^SUP-[0-9]{5}$'),
  CONSTRAINT support_ticket_priority_valid CHECK (priority IN ('URGENT', 'HIGH', 'NORMAL', 'LOW')),
  CONSTRAINT support_ticket_status_valid CHECK (status IN ('OPEN', 'PENDING', 'RESOLVED')),
  CONSTRAINT support_ticket_closed_consistent CHECK ((status = 'RESOLVED') = (closed_at IS NOT NULL)),
  CONSTRAINT support_ticket_closed_after_open CHECK (closed_at IS NULL OR closed_at >= opened_at)
);

CREATE INDEX IF NOT EXISTS support_tickets_opened_idx
  ON support.tickets(opened_at);
CREATE INDEX IF NOT EXISTS support_tickets_queue_status_idx
  ON support.tickets(queue_name, status);

COMMENT ON TABLE support.tickets IS 'Customer support tickets from all service queues.';
COMMENT ON COLUMN support.tickets.queue_name IS 'Owning support queue.';
COMMENT ON COLUMN support.tickets.priority IS 'Operational response priority.';

CREATE OR REPLACE VIEW ai.ticket_overview
WITH (security_barrier = true)
AS
SELECT
  id AS ticket_id,
  ticket_no,
  opened_at,
  closed_at,
  queue_name,
  priority,
  status,
  customer_region,
  subject
FROM support.tickets;

COMMENT ON VIEW ai.ticket_overview IS 'One row per customer support ticket.';

GRANT USAGE ON SCHEMA support TO support_tickets_view_owner;
GRANT SELECT ON support.tickets TO support_tickets_view_owner;
ALTER VIEW ai.ticket_overview OWNER TO support_tickets_view_owner;
REVOKE ALL ON SCHEMA support FROM support_tickets_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA support FROM support_tickets_reader;
GRANT USAGE ON SCHEMA ai TO support_tickets_reader;
GRANT SELECT ON ai.ticket_overview TO support_tickets_reader;

COMMIT;
