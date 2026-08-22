\connect development_issues

BEGIN;

INSERT INTO development.users (user_id, display_name, team_name, email, is_active)
VALUES
  ('DEV-U0001', '김민준', '플랫폼개발', 'dev-u0001@example.invalid', true),
  ('DEV-U0002', '이서연', '플랫폼개발', 'dev-u0002@example.invalid', true),
  ('DEV-U0003', '박지훈', 'HW개발', 'dev-u0003@example.invalid', true),
  ('DEV-U0004', '최하은', 'HW개발', 'dev-u0004@example.invalid', true),
  ('DEV-U0005', '정도윤', 'SW개발', 'dev-u0005@example.invalid', true),
  ('DEV-U0006', '강지우', 'SW개발', 'dev-u0006@example.invalid', true),
  ('DEV-U0007', '조현우', '펌웨어개발', 'dev-u0007@example.invalid', true),
  ('DEV-U0008', '윤서아', '펌웨어개발', 'dev-u0008@example.invalid', true),
  ('DEV-U0009', '장우진', '기구개발', 'dev-u0009@example.invalid', true),
  ('DEV-U0010', '임수빈', '기구개발', 'dev-u0010@example.invalid', true),
  ('DEV-U0011', '한예준', '품질검증', 'dev-u0011@example.invalid', true),
  ('DEV-U0012', '오채원', '품질검증', 'dev-u0012@example.invalid', true),
  ('DEV-U0013', '서준호', '품질검증', 'dev-u0013@example.invalid', true),
  ('DEV-U0014', '신유나', '품질검증', 'dev-u0014@example.invalid', true),
  ('DEV-U0015', '권민재', '제품기획', 'dev-u0015@example.invalid', true),
  ('DEV-U0016', '황다은', '제품기획', 'dev-u0016@example.invalid', true),
  ('DEV-U0017', '안태윤', '인증시험', 'dev-u0017@example.invalid', true),
  ('DEV-U0018', '송나연', '인증시험', 'dev-u0018@example.invalid', false)
ON CONFLICT (user_id) DO UPDATE SET
  display_name = EXCLUDED.display_name,
  team_name = EXCLUDED.team_name,
  email = EXCLUDED.email,
  is_active = EXCLUDED.is_active;

INSERT INTO development.product_models (
  model_code,
  model_name,
  product_family,
  release_date,
  is_active
)
VALUES
  ('ARA-S1', '아라 S1', 'ARA', DATE '2023-03-15', true),
  ('ARA-S1-PRO', '아라 S1 Pro', 'ARA', DATE '2023-07-01', true),
  ('ARA-S2', '아라 S2', 'ARA', DATE '2023-09-20', true),
  ('NURI-FOLD-1', '누리 폴드 1', 'NURI', DATE '2023-09-10', true),
  ('NURI-FOLD-2', '누리 폴드 2', 'NURI', DATE '2023-11-05', true),
  ('BORA-LITE-1', '보라 라이트 1', 'BORA', DATE '2023-10-12', true)
ON CONFLICT (model_code) DO UPDATE SET
  model_name = EXCLUDED.model_name,
  product_family = EXCLUDED.product_family,
  release_date = EXCLUDED.release_date,
  is_active = EXCLUDED.is_active;

WITH generated AS (
  SELECT
    g,
    (ARRAY[
      'ARA-S1', 'ARA-S1-PRO', 'ARA-S2',
      'NURI-FOLD-1', 'NURI-FOLD-2', 'BORA-LITE-1'
    ])[1 + ((g - 1) % 6)] AS model_code
  FROM generate_series(1, 160) AS series(g)
)
INSERT INTO development.test_units (
  product_model_id,
  serial_number,
  initial_hw_version,
  initial_sw_version,
  manufactured_at
)
SELECT
  model.id,
  'DEV-SN-' || lpad(generated.g::text, 6, '0'),
  (ARRAY['HW-1.0', 'HW-1.1', 'HW-1.2', 'HW-2.0'])[1 + ((generated.g - 1) % 4)],
  (ARRAY['SW-2.4.0', 'SW-2.5.1', 'SW-3.0.0', 'SW-3.1.2', 'SW-4.0.0'])[1 + ((generated.g - 1) % 5)],
  DATE '2024-01-01' + ((generated.g * 7) % 300)
FROM generated
JOIN development.product_models AS model
  ON model.model_code = generated.model_code
ON CONFLICT (serial_number) DO UPDATE SET
  product_model_id = EXCLUDED.product_model_id,
  initial_hw_version = EXCLUDED.initial_hw_version,
  initial_sw_version = EXCLUDED.initial_sw_version,
  manufactured_at = EXCLUDED.manufactured_at;

WITH issue_templates (
  template_no,
  issue_type,
  title,
  problem_detail,
  cause,
  countermeasure
) AS (
  VALUES
    (1, 'HARDWARE', '전원 인가 후 간헐적 부팅 실패',
      '저온 부팅 시험에서 전원 LED만 켜지고 초기화가 완료되지 않는다.',
      '전원 시퀀스 구간의 커패시터 용량 편차로 reset 해제가 지연된다.',
      '커패시터 사양을 상향하고 reset timing margin 검증 항목을 추가한다.'),
    (2, 'SOFTWARE', '장시간 동작 후 메모리 사용량 증가',
      '연속 운전 중 메모리 점유율이 증가하며 48시간 이후 응답이 느려진다.',
      '상태 수집 task에서 해제되지 않는 event buffer가 누적된다.',
      'buffer lifecycle을 수정하고 장기 endurance 자동 시험을 추가한다.'),
    (3, 'FIRMWARE', '펌웨어 업데이트 이후 설정값 초기화',
      'OTA 업데이트 완료 후 일부 사용자 설정이 기본값으로 돌아간다.',
      '설정 schema version migration에서 신규 필드 기본값 처리가 누락됐다.',
      'migration 단계별 호환 로직과 rollback 회귀 시험을 추가한다.'),
    (4, 'MECHANICAL', '힌지 반복 동작 시 유격 증가',
      '개폐 수명 시험 후 기준보다 큰 좌우 유격과 마찰음이 확인된다.',
      '힌지 washer 표면 처리 편차로 마모가 빠르게 진행된다.',
      'washer 소재와 공차를 변경하고 수명 시험 표본 수를 확대한다.'),
    (5, 'INTERFACE', 'USB 연결이 간헐적으로 해제됨',
      '고속 데이터 전송 중 장치 연결이 끊기고 재연결이 필요하다.',
      '특정 cable 조건에서 signal integrity margin이 부족하다.',
      'routing과 termination 값을 조정하고 cable matrix 시험을 추가한다.'),
    (6, 'HARDWARE', '고부하 조건에서 표면 온도 상승',
      '최대 부하와 충전을 동시에 수행하면 표면 온도가 목표를 초과한다.',
      '방열 pad 압착 편차로 열저항이 설계값보다 높다.',
      'pad 두께와 조립 검사 기준을 변경하고 thermal logging을 강화한다.'),
    (7, 'SOFTWARE', '절전 모드 복귀 시간이 길어짐',
      '반복 suspend/resume 시험에서 일부 cycle의 복귀 시간이 10초를 넘는다.',
      '백그라운드 동기화 lock과 resume event가 경합한다.',
      'lock 범위를 축소하고 resume path에 timeout 및 telemetry를 추가한다.'),
    (8, 'FIRMWARE', '센서 측정값에 순간적인 이상치 발생',
      '진동 환경에서 센서 값이 한 cycle 동안 허용 범위를 크게 벗어난다.',
      'ADC sampling 시점과 motor switching noise가 겹친다.',
      'sampling phase를 변경하고 median filter 및 진단 counter를 적용한다.'),
    (9, 'MECHANICAL', '조립 후 버튼 클릭감 편차',
      '동일 lot 내에서도 버튼 작동력과 복귀감 편차가 크게 나타난다.',
      'bracket 체결 torque 편차가 dome preload에 영향을 준다.',
      '체결 공정 torque 관리와 전수 작동력 검사를 도입한다.'),
    (10, 'INTERFACE', '무선 연결 재시도 횟수 증가',
      '혼잡 채널 환경에서 연결 완료까지 여러 차례 재시도가 발생한다.',
      '채널 탐색 우선순위와 backoff 값이 혼잡 환경에 적합하지 않다.',
      '탐색 정책을 조정하고 혼잡도별 연결 성능 기준을 정의한다.')
),
generated AS (
  SELECT
    g,
    template.*,
    TIMESTAMPTZ '2025-01-01 09:00:00+09'
      + ((g * 17) % 520) * INTERVAL '1 day'
      + ((g * 29) % 540) * INTERVAL '1 minute' AS discovered_at,
    CASE (g % 10)
      WHEN 0 THEN 'OPEN'
      WHEN 1 THEN 'ANALYZING'
      WHEN 2 THEN 'ACTION_PLANNED'
      WHEN 3 THEN 'VERIFYING'
      ELSE 'RESOLVED'
    END AS status
  FROM generate_series(1, 600) AS series(g)
  JOIN issue_templates AS template
    ON template.template_no = 1 + ((g - 1) % 10)
)
INSERT INTO development.issues (
  issue_no,
  discovered_at,
  reporter_id,
  assignee_id,
  test_unit_id,
  title,
  problem_detail,
  cause,
  countermeasure,
  issue_type,
  severity,
  status,
  observed_hw_version,
  observed_sw_version,
  resolved_at
)
SELECT
  'DEV-' || lpad(generated.g::text, 6, '0'),
  generated.discovered_at,
  reporter.id,
  CASE
    WHEN generated.status = 'OPEN' AND generated.g % 2 = 0 THEN NULL
    ELSE assignee.id
  END,
  unit.id,
  generated.title || ' - ' || model.model_name,
  generated.problem_detail
    || ' 시험 반복 횟수는 '
    || (20 + (generated.g % 180))::text
    || '회이다.',
  CASE
    WHEN generated.status IN ('OPEN', 'ANALYZING') THEN NULL
    ELSE generated.cause
  END,
  CASE
    WHEN generated.status IN ('OPEN', 'ANALYZING') THEN NULL
    ELSE generated.countermeasure
  END,
  generated.issue_type,
  CASE
    WHEN generated.g % 37 = 0 THEN 'CRITICAL'
    WHEN generated.g % 5 = 0 THEN 'HIGH'
    WHEN generated.g % 3 = 0 THEN 'LOW'
    ELSE 'MEDIUM'
  END,
  generated.status,
  CASE
    WHEN generated.g % 11 = 0 THEN 'HW-2.1'
    ELSE unit.initial_hw_version
  END,
  (ARRAY['SW-2.5.1', 'SW-3.0.0', 'SW-3.1.2', 'SW-3.2.0', 'SW-4.0.0'])[1 + ((generated.g - 1) % 5)],
  CASE
    WHEN generated.status = 'RESOLVED'
      THEN generated.discovered_at + (5 + (generated.g % 40)) * INTERVAL '1 day'
    ELSE NULL
  END
FROM generated
JOIN development.users AS reporter
  ON reporter.user_id = 'DEV-U' || lpad((1 + ((generated.g * 7) % 18))::text, 4, '0')
JOIN development.users AS assignee
  ON assignee.user_id = 'DEV-U' || lpad((1 + ((generated.g * 5) % 18))::text, 4, '0')
JOIN development.test_units AS unit
  ON unit.serial_number = 'DEV-SN-' || lpad((1 + ((generated.g * 13) % 160))::text, 6, '0')
JOIN development.product_models AS model
  ON model.id = unit.product_model_id
ON CONFLICT (issue_no) DO UPDATE SET
  discovered_at = EXCLUDED.discovered_at,
  reporter_id = EXCLUDED.reporter_id,
  assignee_id = EXCLUDED.assignee_id,
  test_unit_id = EXCLUDED.test_unit_id,
  title = EXCLUDED.title,
  problem_detail = EXCLUDED.problem_detail,
  cause = EXCLUDED.cause,
  countermeasure = EXCLUDED.countermeasure,
  issue_type = EXCLUDED.issue_type,
  severity = EXCLUDED.severity,
  status = EXCLUDED.status,
  observed_hw_version = EXCLUDED.observed_hw_version,
  observed_sw_version = EXCLUDED.observed_sw_version,
  resolved_at = EXCLUDED.resolved_at;

WITH seeded_issues AS (
  SELECT
    issue.id AS issue_id,
    issue.issue_no,
    issue.discovered_at,
    right(issue.issue_no, 6)::integer AS seed_no
  FROM development.issues AS issue
  WHERE issue.issue_no BETWEEN 'DEV-000001' AND 'DEV-000600'
),
generated_comments AS (
  SELECT
    issue.*,
    sequence_no
  FROM seeded_issues AS issue
  CROSS JOIN LATERAL generate_series(
    1,
    1 + (issue.seed_no % 4)
  ) AS generated(sequence_no)
)
INSERT INTO development.issue_comments (
  issue_id,
  sequence_no,
  author_id,
  comment_type,
  body,
  created_at
)
SELECT
  generated.issue_id,
  generated.sequence_no,
  author.id,
  CASE generated.sequence_no
    WHEN 1 THEN 'INVESTIGATION'
    WHEN 2 THEN 'STATUS'
    WHEN 3 THEN 'DECISION'
    ELSE 'GENERAL'
  END,
  CASE generated.sequence_no
    WHEN 1 THEN '재현 조건을 확인했고 관련 로그와 측정 데이터를 첨부했습니다.'
    WHEN 2 THEN '원인 후보를 좁혀 추가 비교 시험을 진행하고 있습니다.'
    WHEN 3 THEN '대책 적용 시제품의 회귀 시험 결과가 기준을 만족했습니다.'
    ELSE '관련 팀 검토 결과를 반영했으며 다음 gate에서 상태를 갱신하겠습니다.'
  END,
  generated.discovered_at
    + generated.sequence_no * INTERVAL '2 days'
    + ((generated.seed_no * 11) % 480) * INTERVAL '1 minute'
FROM generated_comments AS generated
JOIN development.users AS author
  ON author.user_id = 'DEV-U'
    || lpad((1 + ((generated.seed_no + generated.sequence_no * 3) % 18))::text, 4, '0')
ON CONFLICT (issue_id, sequence_no) DO UPDATE SET
  author_id = EXCLUDED.author_id,
  comment_type = EXCLUDED.comment_type,
  body = EXCLUDED.body,
  created_at = EXCLUDED.created_at;

ANALYZE development.users;
ANALYZE development.product_models;
ANALYZE development.test_units;
ANALYZE development.issues;
ANALYZE development.issue_comments;

COMMIT;
