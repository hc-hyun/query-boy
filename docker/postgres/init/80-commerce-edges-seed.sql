\connect commerce_edges

BEGIN;

INSERT INTO commerce.orders (
  order_id,
  placed_at,
  customer_id,
  channel,
  currency_code,
  gross_amount,
  discount_amount,
  promised_on,
  attributes,
  status
)
VALUES
  (
    '00000000-0000-0000-0000-000000000001',
    timestamptz '2026-08-01 01:00:00+00',
    '10000000-0000-0000-0000-000000000001',
    'WEB',
    'KRW',
    100.00,
    NULL,
    date '2026-08-05',
    '{"campaign":"summer","gift":false}'::jsonb,
    'PAID'
  ),
  (
    '00000000-0000-0000-0000-000000000002',
    timestamptz '2026-08-02 02:00:00+00',
    '10000000-0000-0000-0000-000000000001',
    'WEB',
    'KRW',
    80.00,
    5.50,
    NULL,
    '{"campaign":null,"gift":true}'::jsonb,
    'REFUNDED'
  ),
  (
    '00000000-0000-0000-0000-000000000003',
    timestamptz '2026-08-03 03:00:00+00',
    '10000000-0000-0000-0000-000000000002',
    'STORE',
    'KRW',
    50.25,
    0.00,
    date '2026-08-03',
    '{"store":"서울"}'::jsonb,
    'DRAFT'
  ),
  (
    '00000000-0000-0000-0000-000000000004',
    timestamptz '2026-08-04 04:00:00+00',
    '10000000-0000-0000-0000-000000000003',
    'PARTNER',
    'KRW',
    120.00,
    20.00,
    date '2026-08-10',
    '{"partner":"alpha","tags":["bulk","priority"]}'::jsonb,
    'PAID'
  )
ON CONFLICT (order_id) DO NOTHING;

INSERT INTO commerce.order_lines (
  order_id,
  line_no,
  sku,
  quantity,
  unit_price,
  returned_at,
  note
)
VALUES
  (
    '00000000-0000-0000-0000-000000000001',
    1,
    'SKU-ALPHA',
    2,
    30.00,
    NULL,
    NULL
  ),
  (
    '00000000-0000-0000-0000-000000000001',
    2,
    'SKU-BETA',
    1,
    40.00,
    timestamptz '2026-08-10 10:00:00+00',
    '포장 손상'
  ),
  (
    '00000000-0000-0000-0000-000000000002',
    1,
    'SKU-GAMMA',
    1,
    80.00,
    timestamptz '2026-08-05 12:00:00+00',
    NULL
  ),
  (
    '00000000-0000-0000-0000-000000000004',
    1,
    'SKU-DELTA',
    1,
    50.00,
    NULL,
    NULL
  ),
  (
    '00000000-0000-0000-0000-000000000004',
    2,
    'SKU-EPSILON',
    1,
    25.00,
    NULL,
    '취급 주의'
  ),
  (
    '00000000-0000-0000-0000-000000000004',
    3,
    'SKU-ZETA',
    3,
    15.00,
    NULL,
    NULL
  )
ON CONFLICT (order_id, line_no) DO NOTHING;

ANALYZE commerce.orders;
ANALYZE commerce.order_lines;

COMMIT;
