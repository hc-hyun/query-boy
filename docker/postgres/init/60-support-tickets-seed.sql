\connect support_tickets

BEGIN;

INSERT INTO support.tickets (
  ticket_no,
  opened_at,
  closed_at,
  queue_name,
  priority,
  status,
  customer_region,
  subject
)
SELECT
  'SUP-' || lpad(number::text, 5, '0'),
  timestamptz '2026-01-01 09:00:00+09' + number * interval '12 hours',
  CASE WHEN number % 4 IN (0, 1)
    THEN timestamptz '2026-01-01 09:00:00+09' + number * interval '12 hours' + interval '36 hours'
    ELSE NULL END,
  (ARRAY['ACCOUNT', 'DEVICE', 'DELIVERY'])[1 + number % 3],
  (ARRAY['URGENT', 'HIGH', 'NORMAL', 'LOW'])[1 + number % 4],
  CASE WHEN number % 4 IN (0, 1) THEN 'RESOLVED' WHEN number % 4 = 2 THEN 'PENDING' ELSE 'OPEN' END,
  (ARRAY['KR', 'JP', 'US', 'DE'])[1 + number % 4],
  'Deterministic support fixture ticket ' || number
FROM generate_series(1, 120) AS generated(number)
ON CONFLICT (ticket_no) DO NOTHING;

ANALYZE support.tickets;

COMMIT;
