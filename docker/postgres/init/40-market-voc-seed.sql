\connect market_voc

BEGIN;

INSERT INTO voc.users (user_id, display_name, team_name, email, is_active)
VALUES
  ('VOC-U0001', '김서준', '고객지원', 'voc-u0001@example.invalid', true),
  ('VOC-U0002', '이하린', '고객지원', 'voc-u0002@example.invalid', true),
  ('VOC-U0003', '박도현', '고객지원', 'voc-u0003@example.invalid', true),
  ('VOC-U0004', '최유진', '고객지원', 'voc-u0004@example.invalid', true),
  ('VOC-U0005', '정시우', '서비스센터', 'voc-u0005@example.invalid', true),
  ('VOC-U0006', '강예린', '서비스센터', 'voc-u0006@example.invalid', true),
  ('VOC-U0007', '조민석', '서비스센터', 'voc-u0007@example.invalid', true),
  ('VOC-U0008', '윤가은', '서비스센터', 'voc-u0008@example.invalid', true),
  ('VOC-U0009', '장지호', '시장품질', 'voc-u0009@example.invalid', true),
  ('VOC-U0010', '임서현', '시장품질', 'voc-u0010@example.invalid', true),
  ('VOC-U0011', '한도윤', '시장품질', 'voc-u0011@example.invalid', true),
  ('VOC-U0012', '오지민', '시장품질', 'voc-u0012@example.invalid', true),
  ('VOC-U0013', '서현준', '품질분석', 'voc-u0013@example.invalid', true),
  ('VOC-U0014', '신채린', '품질분석', 'voc-u0014@example.invalid', true),
  ('VOC-U0015', '권우석', '품질분석', 'voc-u0015@example.invalid', true),
  ('VOC-U0016', '황수아', '품질분석', 'voc-u0016@example.invalid', true),
  ('VOC-U0017', '안준영', '제품기획', 'voc-u0017@example.invalid', true),
  ('VOC-U0018', '송예나', '제품기획', 'voc-u0018@example.invalid', true),
  ('VOC-U0019', '류재민', '제품기획', 'voc-u0019@example.invalid', true),
  ('VOC-U0020', '홍다인', '제품기획', 'voc-u0020@example.invalid', true),
  ('VOC-U0021', '문승현', '데이터분석', 'voc-u0021@example.invalid', true),
  ('VOC-U0022', '배소윤', '데이터분석', 'voc-u0022@example.invalid', true),
  ('VOC-U0023', '백성호', 'VOC운영', 'voc-u0023@example.invalid', false),
  ('VOC-U0024', '허나경', 'VOC운영', 'voc-u0024@example.invalid', false)
ON CONFLICT (user_id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  team_name = EXCLUDED.team_name,
  email = EXCLUDED.email,
  is_active = EXCLUDED.is_active;

INSERT INTO voc.product_models (
  model_code,
  model_name,
  product_family,
  release_date,
  is_active
)
VALUES
  ('ARA-S1', '아라 S1', 'ARA', DATE '2022-01-15', true),
  ('ARA-S1-PRO', '아라 S1 Pro', 'ARA', DATE '2022-03-10', true),
  ('ARA-S2', '아라 S2', 'ARA', DATE '2022-05-20', true),
  ('ARA-S2-PRO', '아라 S2 Pro', 'ARA', DATE '2022-07-01', true),
  ('NURI-FOLD-1', '누리 폴드 1', 'NURI', DATE '2022-02-05', true),
  ('NURI-FOLD-2', '누리 폴드 2', 'NURI', DATE '2022-08-18', true),
  ('BORA-LITE-1', '보라 라이트 1', 'BORA', DATE '2022-04-12', true),
  ('BORA-LITE-2', '보라 라이트 2', 'BORA', DATE '2022-09-25', true)
ON CONFLICT (model_code) DO UPDATE SET
  model_name = EXCLUDED.model_name,
  product_family = EXCLUDED.product_family,
  release_date = EXCLUDED.release_date,
  is_active = EXCLUDED.is_active;

WITH model_quotas (
  model_code,
  device_start,
  device_count
) AS (
  VALUES
    ('ARA-S1', 1, 70),
    ('ARA-S1-PRO', 71, 65),
    ('ARA-S2', 136, 60),
    ('ARA-S2-PRO', 196, 55),
    ('NURI-FOLD-1', 251, 50),
    ('NURI-FOLD-2', 301, 40),
    ('BORA-LITE-1', 341, 35),
    ('BORA-LITE-2', 376, 25)
),
generated AS (
  SELECT
    g,
    quota.model_code,
    g - quota.device_start + 1 AS model_device_no
  FROM generate_series(1, 400) AS series(g)
  JOIN model_quotas AS quota
    ON g BETWEEN quota.device_start AND quota.device_start + quota.device_count - 1
)
INSERT INTO voc.devices (
  product_model_id,
  serial_number,
  manufacturing_lot,
  manufactured_at,
  sold_at,
  shipped_hw_version,
  shipped_sw_version
)
SELECT
  model.id,
  'VOC-SN-' || lpad(generated.g::text, 6, '0'),
  CASE
    WHEN generated.model_code = 'BORA-LITE-1' AND generated.model_device_no <= 18
      THEN 'GUMI-2303-B'
    WHEN generated.model_code LIKE 'NURI-FOLD-%'
      THEN 'SUWON-23' || lpad((1 + (generated.g % 6))::text, 2, '0') || '-A'
    ELSE 'GUMI-23' || lpad((1 + (generated.g % 8))::text, 2, '0') || '-A'
  END,
  DATE '2022-10-01' + ((generated.g * 5) % 240),
  DATE '2022-10-01' + ((generated.g * 5) % 240) + 30 + (generated.g % 60),
  (ARRAY['HW-1.0', 'HW-1.1', 'HW-1.2', 'HW-2.0'])[1 + ((generated.g - 1) % 4)],
  (ARRAY['SW-2.5.1', 'SW-3.0.0', 'SW-3.1.2', 'SW-3.2.0', 'SW-4.0.0'])[1 + ((generated.g - 1) % 5)]
FROM generated
JOIN voc.product_models AS model
  ON model.model_code = generated.model_code
ON CONFLICT (serial_number) DO UPDATE SET
  product_model_id = EXCLUDED.product_model_id,
  manufacturing_lot = EXCLUDED.manufacturing_lot,
  manufactured_at = EXCLUDED.manufactured_at,
  sold_at = EXCLUDED.sold_at,
  shipped_hw_version = EXCLUDED.shipped_hw_version,
  shipped_sw_version = EXCLUDED.shipped_sw_version;

WITH model_quotas (
  model_code,
  device_start,
  active_device_count,
  report_start,
  report_count
) AS (
  VALUES
    ('ARA-S1', 1, 63, 1, 180),
    ('ARA-S1-PRO', 71, 58, 181, 150),
    ('ARA-S2', 136, 54, 331, 135),
    ('ARA-S2-PRO', 196, 49, 466, 120),
    ('NURI-FOLD-1', 251, 45, 586, 240),
    ('NURI-FOLD-2', 301, 36, 826, 105),
    ('BORA-LITE-1', 341, 32, 931, 180),
    ('BORA-LITE-2', 376, 23, 1111, 90)
),
case_templates (
  defect_category,
  title,
  problem_detail,
  analysis_cause,
  response_action
) AS (
  VALUES
    ('BATTERY', '배터리 잔량 급감 및 충전 중단',
      '정상 사용 중 배터리 잔량이 빠르게 감소하고 간헐적으로 충전이 중단된다.',
      '배터리 cell 편차와 충전 제어 보정값이 특정 온도 구간에서 맞지 않는다.',
      '배터리 모듈을 교체하고 충전 제어 보정 firmware를 배포한다.'),
    ('DISPLAY', '화면 깜빡임 및 잔상 발생',
      '화면 밝기를 낮춘 상태에서 깜빡임과 이전 화면의 잔상이 관찰된다.',
      'display 구동 timing margin이 일부 panel 편차를 충분히 흡수하지 못한다.',
      'display module을 교체하고 구동 timing을 변경한 software를 안내한다.'),
    ('HINGE', '힌지 개폐 시 소음과 유격 발생',
      '제품을 반복 개폐하면 힌지에서 소음이 나고 좌우 유격이 증가한다.',
      '힌지 내부 washer 마모와 윤활 편차가 복합적으로 발생했다.',
      '개선 힌지로 교환하고 해당 제조 lot를 집중 모니터링한다.'),
    ('CONNECTIVITY', '무선 연결이 반복적으로 해제됨',
      'Wi-Fi 또는 Bluetooth 연결이 사용 중 끊기며 재연결이 필요하다.',
      '혼잡 채널에서 reconnect backoff 설정이 과도하게 길다.',
      '연결 정책을 수정한 firmware를 배포하고 네트워크 설정을 재구성한다.'),
    ('CAMERA', '카메라 실행 시 초점 고정 실패',
      '카메라 실행 후 자동 초점이 완료되지 않거나 초점이 반복 이동한다.',
      'actuator 초기 위치 보정값이 일부 module 공차를 벗어난다.',
      '카메라 module을 교체하고 calibration data를 다시 기록한다.'),
    ('SOFTWARE', '업데이트 후 설정 초기화와 앱 종료',
      'software 업데이트 이후 일부 설정이 초기화되고 특정 화면에서 앱이 종료된다.',
      '설정 migration과 cache schema 호환 처리가 누락됐다.',
      '수정 software를 배포하고 설정 복구 절차를 고객에게 안내한다.'),
    ('OVERHEATING', '충전과 사용 중 제품 과열',
      '고부하 사용과 충전이 겹치면 제품 온도가 높아지고 보호 종료가 발생한다.',
      '방열 부품 압착 편차와 전력 제어 조건이 동시에 영향을 준다.',
      '방열 부품을 점검하고 전력 제한 정책이 포함된 software를 배포한다.'),
    ('OTHER', '사용 중 기타 불편 사항 접수',
      '고객 사용 환경에서 반복되는 불편 사항이 접수되어 상세 확인이 필요하다.',
      '현재 정보만으로 원인을 특정하기 어려워 회수 분석이 필요하다.',
      '추가 로그와 회수품을 확보해 분석한 뒤 고객에게 결과를 안내한다.')
),
base_cases AS (
  SELECT
    g,
    quota.*,
    g - quota.report_start AS model_report_offset,
    TIMESTAMPTZ '2024-01-01 10:00:00+09'
      + ((g * 13) % 930) * INTERVAL '1 day'
      + ((g * 17) % 540) * INTERVAL '1 minute' AS received_at,
    CASE
      WHEN g % 20 < 17 THEN 'DEFECT'
      WHEN g % 20 = 17 THEN 'COMPLAINT'
      WHEN g % 20 = 18 THEN 'INQUIRY'
      ELSE 'SUGGESTION'
    END AS voc_type
  FROM generate_series(1, 1200) AS series(g)
  JOIN model_quotas AS quota
    ON g BETWEEN quota.report_start AND quota.report_start + quota.report_count - 1
),
cases_with_devices AS (
  SELECT
    base.*,
    device.id AS device_id,
    device.manufacturing_lot,
    device.shipped_hw_version,
    device.shipped_sw_version,
    model.model_name,
    model.product_family
  FROM base_cases AS base
  JOIN voc.devices AS device
    ON device.serial_number = 'VOC-SN-' || lpad(
      (
        base.device_start
        + ((base.model_report_offset * 13) % base.active_device_count)
      )::text,
      6,
      '0'
    )
  JOIN voc.product_models AS model
    ON model.id = device.product_model_id
),
categorized AS (
  SELECT
    source.*,
    CASE
      WHEN source.voc_type <> 'DEFECT' THEN 'OTHER'
      WHEN source.manufacturing_lot = 'GUMI-2303-B'
        THEN (ARRAY['BATTERY', 'OVERHEATING', 'BATTERY'])[1 + (source.g % 3)]
      WHEN source.product_family = 'NURI'
        THEN (ARRAY['HINGE', 'HINGE', 'DISPLAY', 'CONNECTIVITY', 'OVERHEATING'])[1 + (source.g % 5)]
      WHEN source.product_family = 'BORA'
        THEN (ARRAY['BATTERY', 'BATTERY', 'OVERHEATING', 'DISPLAY', 'SOFTWARE'])[1 + (source.g % 5)]
      ELSE (ARRAY['BATTERY', 'DISPLAY', 'CONNECTIVITY', 'CAMERA', 'SOFTWARE', 'OTHER'])[1 + (source.g % 6)]
    END AS defect_category
  FROM cases_with_devices AS source
),
generated AS (
  SELECT
    categorized.*,
    template.title,
    template.problem_detail,
    template.analysis_cause,
    template.response_action,
    CASE
      WHEN categorized.received_at >= TIMESTAMPTZ '2026-06-01 00:00:00+09' THEN
        CASE
          WHEN categorized.g % 10 < 4 THEN 'RECEIVED'
          WHEN categorized.g % 10 < 7 THEN 'TRIAGED'
          WHEN categorized.g % 10 < 9 THEN 'IN_PROGRESS'
          ELSE 'RESOLVED'
        END
      WHEN categorized.received_at >= TIMESTAMPTZ '2026-01-01 00:00:00+09' THEN
        CASE
          WHEN categorized.g % 10 = 0 THEN 'RECEIVED'
          WHEN categorized.g % 10 < 3 THEN 'TRIAGED'
          WHEN categorized.g % 10 < 6 THEN 'IN_PROGRESS'
          WHEN categorized.g % 10 < 9 THEN 'RESOLVED'
          ELSE 'CLOSED'
        END
      ELSE
        CASE
          WHEN categorized.g % 20 = 0 THEN 'IN_PROGRESS'
          WHEN categorized.g % 2 = 0 THEN 'RESOLVED'
          ELSE 'CLOSED'
        END
    END AS status
  FROM categorized
  JOIN case_templates AS template
    ON template.defect_category = categorized.defect_category
)
INSERT INTO voc.cases (
  voc_no,
  occurred_at,
  received_at,
  registered_by_id,
  assigned_to_id,
  device_id,
  voc_type,
  title,
  problem_detail,
  analysis_cause,
  response_action,
  defect_category,
  severity,
  status,
  intake_channel,
  market_region,
  country_code,
  observed_hw_version,
  observed_sw_version,
  resolved_at,
  resolution_code
)
SELECT
  'VOC-' || lpad(generated.g::text, 6, '0'),
  generated.received_at - (1 + (generated.g % 10)) * INTERVAL '1 day',
  generated.received_at,
  registrant.id,
  CASE
    WHEN generated.status = 'RECEIVED' AND generated.g % 2 = 0 THEN NULL
    ELSE assignee.id
  END,
  generated.device_id,
  generated.voc_type,
  generated.title || ' - ' || generated.model_name,
  generated.problem_detail
    || ' 고객 사용 기간은 '
    || (30 + (generated.g % 700))::text
    || '일이다.',
  CASE
    WHEN generated.status IN ('RECEIVED', 'TRIAGED') THEN NULL
    ELSE generated.analysis_cause
  END,
  CASE
    WHEN generated.status IN ('RESOLVED', 'CLOSED') THEN generated.response_action
    ELSE NULL
  END,
  generated.defect_category,
  CASE
    WHEN generated.defect_category = 'OVERHEATING' AND generated.g % 7 = 0 THEN 'CRITICAL'
    WHEN generated.defect_category = 'BATTERY' AND generated.g % 11 = 0 THEN 'CRITICAL'
    WHEN generated.defect_category IN ('HINGE', 'OVERHEATING', 'BATTERY') AND generated.g % 3 = 0 THEN 'HIGH'
    WHEN generated.g % 4 = 0 THEN 'LOW'
    ELSE 'MEDIUM'
  END,
  generated.status,
  (ARRAY['SERVICE_CENTER', 'CALL_CENTER', 'MOBILE_APP', 'PARTNER', 'MONITORING'])[1 + ((generated.g - 1) % 5)],
  (ARRAY['한국', '북미', '유럽', '일본', '동남아', '중동'])[1 + ((generated.g - 1) % 6)],
  (ARRAY['KR', 'US', 'DE', 'JP', 'SG', 'AE'])[1 + ((generated.g - 1) % 6)],
  CASE
    WHEN generated.g % 13 = 0 THEN 'HW-2.1'
    ELSE generated.shipped_hw_version
  END,
  (ARRAY['SW-2.5.1', 'SW-3.0.0', 'SW-3.1.2', 'SW-3.2.0', 'SW-4.0.0'])[1 + ((generated.g - 1) % 5)],
  CASE
    WHEN generated.status IN ('RESOLVED', 'CLOSED')
      THEN generated.received_at + (3 + (generated.g % 35)) * INTERVAL '1 day'
    ELSE NULL
  END,
  CASE
    WHEN generated.status NOT IN ('RESOLVED', 'CLOSED') THEN NULL
    WHEN generated.defect_category IN ('SOFTWARE', 'CONNECTIVITY') THEN 'FIRMWARE_UPDATE'
    WHEN generated.defect_category IN ('DISPLAY', 'CAMERA', 'HINGE') THEN 'PART_REPLACEMENT'
    WHEN generated.defect_category IN ('BATTERY', 'OVERHEATING') THEN 'PRODUCT_EXCHANGE'
    ELSE 'GUIDANCE_COMPLETED'
  END
FROM generated
JOIN voc.users AS registrant
  ON registrant.user_id = 'VOC-U'
    || lpad((1 + ((generated.g * 5) % 8))::text, 4, '0')
JOIN voc.users AS assignee
  ON assignee.user_id = 'VOC-U'
    || lpad((9 + ((generated.g * 7) % 8))::text, 4, '0')
ON CONFLICT (voc_no) DO UPDATE SET
  occurred_at = EXCLUDED.occurred_at,
  received_at = EXCLUDED.received_at,
  registered_by_id = EXCLUDED.registered_by_id,
  assigned_to_id = EXCLUDED.assigned_to_id,
  device_id = EXCLUDED.device_id,
  voc_type = EXCLUDED.voc_type,
  title = EXCLUDED.title,
  problem_detail = EXCLUDED.problem_detail,
  analysis_cause = EXCLUDED.analysis_cause,
  response_action = EXCLUDED.response_action,
  defect_category = EXCLUDED.defect_category,
  severity = EXCLUDED.severity,
  status = EXCLUDED.status,
  intake_channel = EXCLUDED.intake_channel,
  market_region = EXCLUDED.market_region,
  country_code = EXCLUDED.country_code,
  observed_hw_version = EXCLUDED.observed_hw_version,
  observed_sw_version = EXCLUDED.observed_sw_version,
  resolved_at = EXCLUDED.resolved_at,
  resolution_code = EXCLUDED.resolution_code;

WITH seeded_cases AS (
  SELECT
    case_row.id AS case_id,
    case_row.voc_no,
    case_row.received_at,
    right(case_row.voc_no, 6)::integer AS seed_no
  FROM voc.cases AS case_row
  WHERE case_row.voc_no BETWEEN 'VOC-000001' AND 'VOC-001200'
),
generated_comments AS (
  SELECT
    case_row.*,
    sequence_no
  FROM seeded_cases AS case_row
  CROSS JOIN LATERAL generate_series(
    1,
    1 + (case_row.seed_no % 4)
  ) AS generated(sequence_no)
)
INSERT INTO voc.case_comments (
  case_id,
  sequence_no,
  author_id,
  visibility,
  comment_type,
  body,
  created_at
)
SELECT
  generated.case_id,
  generated.sequence_no,
  author.id,
  CASE
    WHEN (generated.seed_no + generated.sequence_no) % 10 < 7 THEN 'INTERNAL'
    ELSE 'PUBLIC'
  END,
  CASE generated.sequence_no
    WHEN 1 THEN 'INTAKE'
    WHEN 2 THEN 'INVESTIGATION'
    WHEN 3 THEN 'CUSTOMER_RESPONSE'
    ELSE 'RESOLUTION'
  END,
  CASE generated.sequence_no
    WHEN 1 THEN '고객 증상과 발생 조건을 확인하고 필요한 로그 및 회수 절차를 안내했습니다.'
    WHEN 2 THEN '회수품과 로그를 분석해 원인 후보 및 영향 범위를 검토하고 있습니다.'
    WHEN 3 THEN '분석 진행 상황과 임시 조치 방법을 고객 및 서비스 채널에 전달했습니다.'
    ELSE '최종 대책을 적용했고 동일 조건의 추가 시장 모니터링을 진행합니다.'
  END,
  generated.received_at
    + generated.sequence_no * INTERVAL '2 days'
    + ((generated.seed_no * 19) % 600) * INTERVAL '1 minute'
FROM generated_comments AS generated
JOIN voc.users AS author
  ON author.user_id = 'VOC-U'
    || lpad((1 + ((generated.seed_no + generated.sequence_no * 5) % 22))::text, 4, '0')
ON CONFLICT (case_id, sequence_no) DO UPDATE SET
  author_id = EXCLUDED.author_id,
  visibility = EXCLUDED.visibility,
  comment_type = EXCLUDED.comment_type,
  body = EXCLUDED.body,
  created_at = EXCLUDED.created_at;

ANALYZE voc.users;
ANALYZE voc.product_models;
ANALYZE voc.devices;
ANALYZE voc.cases;
ANALYZE voc.case_comments;

COMMIT;
