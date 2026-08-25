\connect development_issues

BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS development;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements FROM PUBLIC;
REVOKE ALL ON TABLE public.pg_stat_statements_info FROM PUBLIC;
REVOKE ALL ON SCHEMA development FROM PUBLIC;

COMMENT ON SCHEMA development IS
  'Source tables for development issues found during product development and verification.';
COMMENT ON SCHEMA ai IS
  'Curated, read-only query surfaces for AI clients.';

CREATE TABLE IF NOT EXISTS development.users (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id text NOT NULL UNIQUE,
  display_name text NOT NULL,
  team_name text NOT NULL,
  email text NOT NULL UNIQUE,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT development_users_user_id_format
    CHECK (user_id ~ '^DEV-U[0-9]{4}$'),
  CONSTRAINT development_users_name_not_blank
    CHECK (btrim(display_name) <> ''),
  CONSTRAINT development_users_team_not_blank
    CHECK (btrim(team_name) <> '')
);

CREATE TABLE IF NOT EXISTS development.product_models (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  model_code text NOT NULL UNIQUE,
  model_name text NOT NULL,
  product_family text NOT NULL,
  release_date date NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  CONSTRAINT development_product_models_code_not_blank
    CHECK (btrim(model_code) <> ''),
  CONSTRAINT development_product_models_name_not_blank
    CHECK (btrim(model_name) <> '')
);

CREATE TABLE IF NOT EXISTS development.test_units (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  product_model_id bigint NOT NULL
    REFERENCES development.product_models(id) ON DELETE RESTRICT,
  serial_number text NOT NULL UNIQUE,
  initial_hw_version text NOT NULL,
  initial_sw_version text NOT NULL,
  manufactured_at date NOT NULL,
  CONSTRAINT development_test_units_serial_not_blank
    CHECK (btrim(serial_number) <> ''),
  CONSTRAINT development_test_units_hw_not_blank
    CHECK (btrim(initial_hw_version) <> ''),
  CONSTRAINT development_test_units_sw_not_blank
    CHECK (btrim(initial_sw_version) <> '')
);

CREATE TABLE IF NOT EXISTS development.issues (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  issue_no text NOT NULL UNIQUE,
  discovered_at timestamptz NOT NULL,
  reporter_id bigint NOT NULL
    REFERENCES development.users(id) ON DELETE RESTRICT,
  assignee_id bigint
    REFERENCES development.users(id) ON DELETE RESTRICT,
  test_unit_id bigint NOT NULL
    REFERENCES development.test_units(id) ON DELETE RESTRICT,
  title text NOT NULL,
  problem_detail text NOT NULL,
  cause text,
  countermeasure text,
  issue_type text NOT NULL,
  severity text NOT NULL,
  status text NOT NULL,
  observed_hw_version text NOT NULL,
  observed_sw_version text NOT NULL,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT development_issues_number_format
    CHECK (issue_no ~ '^DEV-[0-9]{6}$'),
  CONSTRAINT development_issues_title_not_blank
    CHECK (btrim(title) <> ''),
  CONSTRAINT development_issues_detail_not_blank
    CHECK (btrim(problem_detail) <> ''),
  CONSTRAINT development_issues_type_valid
    CHECK (issue_type IN ('HARDWARE', 'SOFTWARE', 'FIRMWARE', 'MECHANICAL', 'INTERFACE')),
  CONSTRAINT development_issues_severity_valid
    CHECK (severity IN ('CRITICAL', 'HIGH', 'MEDIUM', 'LOW')),
  CONSTRAINT development_issues_status_valid
    CHECK (status IN ('OPEN', 'ANALYZING', 'ACTION_PLANNED', 'VERIFYING', 'RESOLVED')),
  CONSTRAINT development_issues_resolution_consistent
    CHECK ((status = 'RESOLVED') = (resolved_at IS NOT NULL)),
  CONSTRAINT development_issues_resolution_after_discovery
    CHECK (resolved_at IS NULL OR resolved_at >= discovered_at),
  CONSTRAINT development_issues_cause_not_blank
    CHECK (cause IS NULL OR btrim(cause) <> ''),
  CONSTRAINT development_issues_countermeasure_not_blank
    CHECK (countermeasure IS NULL OR btrim(countermeasure) <> '')
);

CREATE TABLE IF NOT EXISTS development.issue_comments (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  issue_id bigint NOT NULL
    REFERENCES development.issues(id) ON DELETE CASCADE,
  sequence_no smallint NOT NULL,
  author_id bigint NOT NULL
    REFERENCES development.users(id) ON DELETE RESTRICT,
  comment_type text NOT NULL,
  body text NOT NULL,
  created_at timestamptz NOT NULL,
  CONSTRAINT development_issue_comments_sequence_positive
    CHECK (sequence_no > 0),
  CONSTRAINT development_issue_comments_type_valid
    CHECK (comment_type IN ('INVESTIGATION', 'STATUS', 'DECISION', 'GENERAL')),
  CONSTRAINT development_issue_comments_body_not_blank
    CHECK (btrim(body) <> ''),
  CONSTRAINT development_issue_comments_issue_sequence_unique
    UNIQUE (issue_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS development_test_units_model_idx
  ON development.test_units(product_model_id);
CREATE INDEX IF NOT EXISTS development_issues_discovered_idx
  ON development.issues(discovered_at DESC);
CREATE INDEX IF NOT EXISTS development_issues_status_date_idx
  ON development.issues(status, discovered_at DESC);
CREATE INDEX IF NOT EXISTS development_issues_type_severity_idx
  ON development.issues(issue_type, severity);
CREATE INDEX IF NOT EXISTS development_issues_reporter_idx
  ON development.issues(reporter_id);
CREATE INDEX IF NOT EXISTS development_issues_assignee_open_idx
  ON development.issues(assignee_id, status)
  WHERE status <> 'RESOLVED';
CREATE INDEX IF NOT EXISTS development_issues_test_unit_idx
  ON development.issues(test_unit_id);
CREATE INDEX IF NOT EXISTS development_issue_comments_issue_date_idx
  ON development.issue_comments(issue_id, created_at, id);

COMMENT ON TABLE development.users IS
  'Grain: one internal development or quality user.';
COMMENT ON COLUMN development.users.user_id IS
  'Stable business user identifier displayed in issue and comment results.';
COMMENT ON TABLE development.product_models IS
  'Grain: one product model used by development test units.';
COMMENT ON COLUMN development.product_models.model_name IS
  'Human-readable product model name.';
COMMENT ON TABLE development.test_units IS
  'Grain: one physical development test unit with a unique serial number.';
COMMENT ON COLUMN development.test_units.initial_hw_version IS
  'Hardware version when the test unit was registered; an issue keeps its own observed snapshot.';
COMMENT ON COLUMN development.test_units.initial_sw_version IS
  'Software version when the test unit was registered; an issue keeps its own observed snapshot.';
COMMENT ON TABLE development.issues IS
  'Grain: one development issue. Causes and countermeasures may be null while analysis is incomplete.';
COMMENT ON COLUMN development.issues.discovered_at IS
  'Timestamp when the issue was discovered, stored with timezone.';
COMMENT ON COLUMN development.issues.reporter_id IS
  'User who initially registered the issue.';
COMMENT ON COLUMN development.issues.assignee_id IS
  'User currently responsible for the issue; null means unassigned.';
COMMENT ON COLUMN development.issues.problem_detail IS
  'Detailed reproduction conditions and observed problem.';
COMMENT ON COLUMN development.issues.cause IS
  'Confirmed or leading cause; null means analysis is not complete.';
COMMENT ON COLUMN development.issues.countermeasure IS
  'Implemented or planned corrective action; null means no action is fixed yet.';
COMMENT ON COLUMN development.issues.observed_hw_version IS
  'Hardware version observed at the time of this issue.';
COMMENT ON COLUMN development.issues.observed_sw_version IS
  'Software version observed at the time of this issue.';
COMMENT ON TABLE development.issue_comments IS
  'Grain: one chronological comment attached to one development issue.';

CREATE OR REPLACE VIEW ai.issue_overview AS
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

CREATE OR REPLACE VIEW ai.issue_comments AS
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

CREATE OR REPLACE VIEW ai.test_unit_overview AS
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

COMMENT ON VIEW ai.issue_overview IS
  'Grain: one row per development issue. Safe join key to ai.issue_comments: issue_id. Time filters normally use discovered_at.';
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
COMMENT ON VIEW ai.issue_comments IS
  'Grain: one row per development issue comment. Many rows may share issue_id.';
COMMENT ON COLUMN ai.issue_comments.issue_id IS
  'Join key to ai.issue_overview.issue_id; joining expands one issue to many comments.';
COMMENT ON COLUMN ai.issue_comments.commented_at IS
  'Timestamp when the comment was written, stored with timezone.';
COMMENT ON VIEW ai.test_unit_overview IS
  'Grain: one row per development test unit, including units with no issues. Use issue_count as the pre-aggregated issue denominator.';
COMMENT ON COLUMN ai.test_unit_overview.test_unit_id IS
  'Stable test-unit key. ai.issue_overview intentionally exposes issue grain instead.';
COMMENT ON COLUMN ai.test_unit_overview.issue_count IS
  'Total issues found on this test unit; zero means no issue has been registered.';
COMMENT ON COLUMN ai.test_unit_overview.unresolved_issue_count IS
  'Issues whose status is not RESOLVED, already aggregated per test unit.';

GRANT USAGE ON SCHEMA development TO development_issues_view_owner;
GRANT SELECT ON ALL TABLES IN SCHEMA development TO development_issues_view_owner;
ALTER DEFAULT PRIVILEGES FOR ROLE query_man_admin IN SCHEMA development
  GRANT SELECT ON TABLES TO development_issues_view_owner;

GRANT USAGE, CREATE ON SCHEMA ai TO development_issues_view_owner;
ALTER VIEW ai.issue_overview OWNER TO development_issues_view_owner;
ALTER VIEW ai.issue_comments OWNER TO development_issues_view_owner;
ALTER VIEW ai.test_unit_overview OWNER TO development_issues_view_owner;

REVOKE ALL ON SCHEMA development FROM development_issues_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA development FROM development_issues_reader;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA development FROM development_issues_reader;
GRANT USAGE ON SCHEMA ai TO development_issues_reader;
GRANT SELECT ON
  ai.issue_overview,
  ai.issue_comments,
  ai.test_unit_overview
TO development_issues_reader;

CREATE SCHEMA IF NOT EXISTS tenant_ai;
REVOKE ALL ON SCHEMA tenant_ai FROM PUBLIC;

CREATE TABLE IF NOT EXISTS tenant_ai.private_records (
  record_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tenant_id text NOT NULL,
  label text NOT NULL,
  CONSTRAINT tenant_private_record_unique UNIQUE (tenant_id, label)
);

ALTER TABLE tenant_ai.private_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenant_ai.private_records FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_private_records_reader ON tenant_ai.private_records;
CREATE POLICY tenant_private_records_reader
ON tenant_ai.private_records
FOR SELECT
TO development_issues_reader
USING (tenant_id = pg_catalog.current_setting('query_man.tenant_id', true));
DROP POLICY IF EXISTS tenant_private_records_admin ON tenant_ai.private_records;
CREATE POLICY tenant_private_records_admin
ON tenant_ai.private_records
FOR ALL
TO query_man_admin
USING (true)
WITH CHECK (true);

CREATE OR REPLACE VIEW tenant_ai.record_overview
WITH (security_barrier = true, security_invoker = true)
AS
SELECT record_id, label
FROM tenant_ai.private_records;

COMMENT ON VIEW tenant_ai.record_overview IS
  'RLS fixture: one row per record visible to the trusted tenant context.';
GRANT USAGE ON SCHEMA tenant_ai TO development_issues_reader;
GRANT SELECT ON tenant_ai.private_records, tenant_ai.record_overview
  TO development_issues_reader;
REVOKE CREATE ON SCHEMA tenant_ai FROM development_issues_reader;

COMMIT;
