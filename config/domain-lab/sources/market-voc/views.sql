BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.voc_overview (
  voc_id,
  voc_no,
  occurred_at,
  received_at,
  received_on,
  registered_by_user_id,
  registered_by_name,
  registered_by_team,
  assigned_to_user_id,
  assigned_to_name,
  model_code,
  model_name,
  product_family,
  serial_number,
  manufacturing_lot,
  hw_version,
  sw_version,
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
  resolved_at,
  resolution_code,
  comment_count,
  last_comment_at
) AS
SELECT
  case_row.id AS voc_id,
  case_row.voc_no,
  case_row.occurred_at,
  case_row.received_at,
  (case_row.received_at AT TIME ZONE 'Asia/Seoul')::date AS received_on,
  registrant.user_id AS registered_by_user_id,
  registrant.display_name AS registered_by_name,
  registrant.team_name AS registered_by_team,
  assignee.user_id AS assigned_to_user_id,
  assignee.display_name AS assigned_to_name,
  model.model_code,
  model.model_name,
  model.product_family,
  device.serial_number,
  device.manufacturing_lot,
  case_row.observed_hw_version AS hw_version,
  case_row.observed_sw_version AS sw_version,
  case_row.voc_type,
  case_row.title,
  case_row.problem_detail,
  case_row.analysis_cause,
  case_row.response_action,
  case_row.defect_category,
  case_row.severity,
  case_row.status,
  case_row.intake_channel,
  case_row.market_region,
  case_row.country_code,
  case_row.resolved_at,
  case_row.resolution_code,
  COALESCE(comment_stats.comment_count, 0)::integer AS comment_count,
  comment_stats.last_comment_at
FROM voc.cases AS case_row
JOIN voc.users AS registrant
  ON registrant.id = case_row.registered_by_id
LEFT JOIN voc.users AS assignee
  ON assignee.id = case_row.assigned_to_id
JOIN voc.devices AS device
  ON device.id = case_row.device_id
JOIN voc.product_models AS model
  ON model.id = device.product_model_id
LEFT JOIN (
  SELECT
    case_id,
    count(*) AS comment_count,
    max(created_at) AS last_comment_at
  FROM voc.case_comments
  GROUP BY case_id
) AS comment_stats
  ON comment_stats.case_id = case_row.id;

CREATE OR REPLACE VIEW ai.voc_comments (
  comment_id,
  voc_id,
  voc_no,
  received_at,
  model_name,
  serial_number,
  voc_title,
  sequence_no,
  visibility,
  comment_type,
  author_user_id,
  author_name,
  comment,
  commented_at
) AS
SELECT
  comment.id AS comment_id,
  case_row.id AS voc_id,
  case_row.voc_no,
  case_row.received_at,
  model.model_name,
  device.serial_number,
  case_row.title AS voc_title,
  comment.sequence_no,
  comment.visibility,
  comment.comment_type,
  author.user_id AS author_user_id,
  author.display_name AS author_name,
  comment.body AS comment,
  comment.created_at AS commented_at
FROM voc.case_comments AS comment
JOIN voc.cases AS case_row
  ON case_row.id = comment.case_id
JOIN voc.users AS author
  ON author.id = comment.author_id
JOIN voc.devices AS device
  ON device.id = case_row.device_id
JOIN voc.product_models AS model
  ON model.id = device.product_model_id;

CREATE OR REPLACE VIEW ai.device_overview (
  device_id,
  model_code,
  model_name,
  product_family,
  serial_number,
  manufacturing_lot,
  manufactured_at,
  sold_at,
  shipped_hw_version,
  shipped_sw_version,
  voc_count,
  unresolved_voc_count,
  last_received_at
) AS
SELECT
  device.id AS device_id,
  model.model_code,
  model.model_name,
  model.product_family,
  device.serial_number,
  device.manufacturing_lot,
  device.manufactured_at,
  device.sold_at,
  device.shipped_hw_version,
  device.shipped_sw_version,
  COALESCE(voc_stats.voc_count, 0)::integer AS voc_count,
  COALESCE(voc_stats.unresolved_voc_count, 0)::integer AS unresolved_voc_count,
  voc_stats.last_received_at
FROM voc.devices AS device
JOIN voc.product_models AS model
  ON model.id = device.product_model_id
LEFT JOIN (
  SELECT
    device_id,
    count(*) AS voc_count,
    count(*) FILTER (WHERE status NOT IN ('RESOLVED', 'CLOSED')) AS unresolved_voc_count,
    max(received_at) AS last_received_at
  FROM voc.cases
  GROUP BY device_id
) AS voc_stats
  ON voc_stats.device_id = device.id;

COMMENT ON VIEW ai.voc_overview IS E'query-man:source=market-voc;view-contract=1\nGrain: one row per market VOC case. Safe join key to ai.voc_comments: voc_id. Intake-period filters use received_at, not occurred_at.';
COMMENT ON COLUMN ai.voc_overview.voc_id IS
  'Stable join key for the market VOC case.';
COMMENT ON COLUMN ai.voc_overview.received_on IS
  'Korea-local calendar date derived from received_at.';
COMMENT ON COLUMN ai.voc_overview.assigned_to_user_id IS
  'Business user ID of the current assignee; null means unassigned.';
COMMENT ON COLUMN ai.voc_overview.analysis_cause IS
  'Null while technical analysis is incomplete.';
COMMENT ON COLUMN ai.voc_overview.response_action IS
  'Null until a customer response or corrective action is fixed.';
COMMENT ON COLUMN ai.voc_overview.hw_version IS
  'Hardware version observed for this case, not necessarily the shipped version.';
COMMENT ON COLUMN ai.voc_overview.sw_version IS
  'Software version observed for this case, not necessarily the shipped version.';
COMMENT ON COLUMN ai.voc_overview.comment_count IS
  'Number of comments for this VOC; already aggregated and safe from comment fanout.';
COMMENT ON VIEW ai.voc_comments IS E'query-man:source=market-voc;view-contract=1\nGrain: one row per VOC comment. Many rows may share voc_id; PUBLIC comments are customer-visible.';
COMMENT ON COLUMN ai.voc_comments.voc_id IS
  'Join key to ai.voc_overview.voc_id; joining expands one VOC to many comments.';
COMMENT ON COLUMN ai.voc_comments.visibility IS
  'PUBLIC is customer-visible; INTERNAL is visible only to company staff.';
COMMENT ON VIEW ai.device_overview IS E'query-man:source=market-voc;view-contract=1\nGrain: one row per sold device, including devices with no VOC. Use voc_count for device-normalized market analysis.';
COMMENT ON COLUMN ai.device_overview.device_id IS
  'Stable device key. ai.voc_overview intentionally exposes VOC case grain instead.';
COMMENT ON COLUMN ai.device_overview.voc_count IS
  'Total VOC cases for this device; zero means the device has no registered VOC.';
COMMENT ON COLUMN ai.device_overview.unresolved_voc_count IS
  'VOC cases not in RESOLVED or CLOSED status, already aggregated per device.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;
REVOKE ALL ON SCHEMA voc FROM market_voc_view_owner;
GRANT USAGE ON SCHEMA voc TO market_voc_view_owner;
REVOKE ALL ON
  voc.users,
  voc.product_models,
  voc.devices,
  voc.cases,
  voc.case_comments
FROM market_voc_view_owner;
GRANT SELECT ON
  voc.users,
  voc.product_models,
  voc.devices,
  voc.cases,
  voc.case_comments
TO market_voc_view_owner;

REVOKE ALL ON SCHEMA ai FROM market_voc_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO market_voc_view_owner;
ALTER VIEW ai.voc_overview OWNER TO market_voc_view_owner;
ALTER VIEW ai.voc_comments OWNER TO market_voc_view_owner;
ALTER VIEW ai.device_overview OWNER TO market_voc_view_owner;
REVOKE CREATE ON SCHEMA ai FROM market_voc_view_owner;

REVOKE ALL ON SCHEMA voc FROM market_voc_reader;
REVOKE ALL ON
  voc.users,
  voc.product_models,
  voc.devices,
  voc.cases,
  voc.case_comments
FROM market_voc_reader;
REVOKE ALL ON SCHEMA ai FROM market_voc_reader;
GRANT USAGE ON SCHEMA ai TO market_voc_reader;
REVOKE ALL ON
  ai.voc_overview,
  ai.voc_comments,
  ai.device_overview
FROM PUBLIC;
REVOKE ALL ON
  ai.voc_overview,
  ai.voc_comments,
  ai.device_overview
FROM market_voc_reader;
GRANT SELECT ON
  ai.voc_overview,
  ai.voc_comments,
  ai.device_overview
TO market_voc_reader;

COMMIT;
