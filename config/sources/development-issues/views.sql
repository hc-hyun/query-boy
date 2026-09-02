BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.issue_overview (
  issue_id,
  issue_no,
  discovered_at,
  discovered_on,
  reporter_user_id,
  reporter_name,
  reporter_team,
  assignee_user_id,
  assignee_name,
  model_code,
  model_name,
  serial_number,
  hw_version,
  sw_version,
  title,
  problem_detail,
  cause,
  countermeasure,
  issue_type,
  severity,
  status,
  resolved_at,
  comment_count,
  last_comment_at
) AS
SELECT
  issue.id AS issue_id,
  issue.issue_no,
  issue.discovered_at,
  (issue.discovered_at AT TIME ZONE 'Asia/Seoul')::date AS discovered_on,
  reporter.user_id AS reporter_user_id,
  reporter.display_name AS reporter_name,
  reporter.team_name AS reporter_team,
  assignee.user_id AS assignee_user_id,
  assignee.display_name AS assignee_name,
  model.model_code,
  model.model_name,
  unit.serial_number,
  issue.observed_hw_version AS hw_version,
  issue.observed_sw_version AS sw_version,
  issue.title,
  issue.problem_detail,
  issue.cause,
  issue.countermeasure,
  issue.issue_type,
  issue.severity,
  issue.status,
  issue.resolved_at,
  COALESCE(comment_stats.comment_count, 0)::integer AS comment_count,
  comment_stats.last_comment_at
FROM development.issues AS issue
JOIN development.users AS reporter
  ON reporter.id = issue.reporter_id
LEFT JOIN development.users AS assignee
  ON assignee.id = issue.assignee_id
JOIN development.test_units AS unit
  ON unit.id = issue.test_unit_id
JOIN development.product_models AS model
  ON model.id = unit.product_model_id
LEFT JOIN (
  SELECT
    issue_id,
    count(*) AS comment_count,
    max(created_at) AS last_comment_at
  FROM development.issue_comments
  GROUP BY issue_id
) AS comment_stats
  ON comment_stats.issue_id = issue.id;

CREATE OR REPLACE VIEW ai.issue_comments (
  comment_id,
  issue_id,
  issue_no,
  discovered_at,
  model_name,
  serial_number,
  issue_title,
  sequence_no,
  comment_type,
  author_user_id,
  author_name,
  comment,
  commented_at
) AS
SELECT
  comment.id AS comment_id,
  issue.id AS issue_id,
  issue.issue_no,
  issue.discovered_at,
  model.model_name,
  unit.serial_number,
  issue.title AS issue_title,
  comment.sequence_no,
  comment.comment_type,
  author.user_id AS author_user_id,
  author.display_name AS author_name,
  comment.body AS comment,
  comment.created_at AS commented_at
FROM development.issue_comments AS comment
JOIN development.issues AS issue
  ON issue.id = comment.issue_id
JOIN development.users AS author
  ON author.id = comment.author_id
JOIN development.test_units AS unit
  ON unit.id = issue.test_unit_id
JOIN development.product_models AS model
  ON model.id = unit.product_model_id;

CREATE OR REPLACE VIEW ai.test_unit_overview (
  test_unit_id,
  model_code,
  model_name,
  product_family,
  serial_number,
  initial_hw_version,
  initial_sw_version,
  manufactured_at,
  issue_count,
  unresolved_issue_count,
  last_discovered_at
) AS
SELECT
  unit.id AS test_unit_id,
  model.model_code,
  model.model_name,
  model.product_family,
  unit.serial_number,
  unit.initial_hw_version,
  unit.initial_sw_version,
  unit.manufactured_at,
  COALESCE(issue_stats.issue_count, 0)::integer AS issue_count,
  COALESCE(issue_stats.unresolved_issue_count, 0)::integer AS unresolved_issue_count,
  issue_stats.last_discovered_at
FROM development.test_units AS unit
JOIN development.product_models AS model
  ON model.id = unit.product_model_id
LEFT JOIN (
  SELECT
    test_unit_id,
    count(*) AS issue_count,
    count(*) FILTER (WHERE status <> 'RESOLVED') AS unresolved_issue_count,
    max(discovered_at) AS last_discovered_at
  FROM development.issues
  GROUP BY test_unit_id
) AS issue_stats
  ON issue_stats.test_unit_id = unit.id;

COMMENT ON VIEW ai.issue_overview IS E'query-man:source=development-issues;view-contract=1\nGrain: one row per development issue. Safe join key to ai.issue_comments: issue_id. Time filters normally use discovered_at.';
COMMENT ON COLUMN ai.issue_overview.issue_id IS
  'Stable join key for the development issue.';
COMMENT ON COLUMN ai.issue_overview.discovered_on IS
  'Korea-local calendar date derived from discovered_at.';
COMMENT ON COLUMN ai.issue_overview.reporter_user_id IS
  'Business user ID of the issue reporter.';
COMMENT ON COLUMN ai.issue_overview.assignee_user_id IS
  'Business user ID of the current assignee; null means unassigned.';
COMMENT ON COLUMN ai.issue_overview.cause IS
  'Null while root-cause analysis is incomplete.';
COMMENT ON COLUMN ai.issue_overview.countermeasure IS
  'Null until a corrective action is planned.';
COMMENT ON COLUMN ai.issue_overview.comment_count IS
  'Number of comments for this issue; already aggregated and safe from comment fanout.';
COMMENT ON VIEW ai.issue_comments IS E'query-man:source=development-issues;view-contract=1\nGrain: one row per development issue comment. Many rows may share issue_id.';
COMMENT ON COLUMN ai.issue_comments.issue_id IS
  'Join key to ai.issue_overview.issue_id; joining expands one issue to many comments.';
COMMENT ON COLUMN ai.issue_comments.commented_at IS
  'Timestamp when the comment was written, stored with timezone.';
COMMENT ON VIEW ai.test_unit_overview IS E'query-man:source=development-issues;view-contract=1\nGrain: one row per development test unit, including units with no issues. Use issue_count as the pre-aggregated issue denominator.';
COMMENT ON COLUMN ai.test_unit_overview.test_unit_id IS
  'Stable test-unit key. ai.issue_overview intentionally exposes issue grain instead.';
COMMENT ON COLUMN ai.test_unit_overview.issue_count IS
  'Total issues found on this test unit; zero means no issue has been registered.';
COMMENT ON COLUMN ai.test_unit_overview.unresolved_issue_count IS
  'Issues whose status is not RESOLVED, already aggregated per test unit.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;
REVOKE ALL ON SCHEMA development FROM development_issues_view_owner;
GRANT USAGE ON SCHEMA development TO development_issues_view_owner;
REVOKE ALL ON
  development.users,
  development.product_models,
  development.test_units,
  development.issues,
  development.issue_comments
FROM development_issues_view_owner;
GRANT SELECT ON
  development.users,
  development.product_models,
  development.test_units,
  development.issues,
  development.issue_comments
TO development_issues_view_owner;

REVOKE ALL ON SCHEMA ai FROM development_issues_view_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO development_issues_view_owner;
ALTER VIEW ai.issue_overview OWNER TO development_issues_view_owner;
ALTER VIEW ai.issue_comments OWNER TO development_issues_view_owner;
ALTER VIEW ai.test_unit_overview OWNER TO development_issues_view_owner;
REVOKE CREATE ON SCHEMA ai FROM development_issues_view_owner;

REVOKE ALL ON SCHEMA development FROM development_issues_reader;
REVOKE ALL ON
  development.users,
  development.product_models,
  development.test_units,
  development.issues,
  development.issue_comments
FROM development_issues_reader;
REVOKE ALL ON SCHEMA ai FROM development_issues_reader;
GRANT USAGE ON SCHEMA ai TO development_issues_reader;
REVOKE ALL ON
  ai.issue_overview,
  ai.issue_comments,
  ai.test_unit_overview
FROM PUBLIC;
REVOKE ALL ON
  ai.issue_overview,
  ai.issue_comments,
  ai.test_unit_overview
FROM development_issues_reader;
GRANT SELECT ON
  ai.issue_overview,
  ai.issue_comments,
  ai.test_unit_overview
TO development_issues_reader;

COMMIT;
