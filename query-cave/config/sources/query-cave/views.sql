BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW signal_schema.case_files_view (
  case_id,
  priority,
  response_code,
  summary,
  reported_on,
  reported_at,
  risk_score
)
WITH (security_barrier = true)
AS
SELECT
  incident.case_id,
  incident.priority,
  incident.response_code,
  incident.summary,
  incident.reported_on,
  incident.reported_at,
  incident.risk_score
FROM gotham_schema.incidents_table AS incident;

COMMENT ON VIEW signal_schema.case_files_view IS E'query-man:source=query-cave;view-contract=1\nGrain: one synthetic incident prepared as a safe Query Cave case file.';
COMMENT ON COLUMN signal_schema.case_files_view.case_id IS
  'Stable synthetic case identifier represented as bigint.';
COMMENT ON COLUMN signal_schema.case_files_view.priority IS
  'Small integer response priority.';
COMMENT ON COLUMN signal_schema.case_files_view.response_code IS
  'Integer response classification code.';
COMMENT ON COLUMN signal_schema.case_files_view.summary IS
  'Synthetic incident summary without personal data.';
COMMENT ON COLUMN signal_schema.case_files_view.reported_on IS
  'Synthetic calendar date when the incident was reported.';
COMMENT ON COLUMN signal_schema.case_files_view.reported_at IS
  'Synthetic timestamp with time zone when the incident was reported.';
COMMENT ON COLUMN signal_schema.case_files_view.risk_score IS
  'Synthetic exact numeric risk score.';

REVOKE ALL ON SCHEMA signal_schema FROM PUBLIC;

REVOKE ALL ON SCHEMA gotham_schema FROM query_cave_view_owner;
GRANT USAGE ON SCHEMA gotham_schema TO query_cave_view_owner;
REVOKE ALL ON TABLE gotham_schema.incidents_table FROM query_cave_view_owner;
GRANT SELECT ON TABLE gotham_schema.incidents_table TO query_cave_view_owner;

REVOKE ALL ON SCHEMA signal_schema FROM query_cave_view_owner;
GRANT USAGE, CREATE ON SCHEMA signal_schema TO query_cave_view_owner;
ALTER VIEW signal_schema.case_files_view OWNER TO query_cave_view_owner;
REVOKE CREATE ON SCHEMA signal_schema FROM query_cave_view_owner;

REVOKE ALL ON SCHEMA gotham_schema FROM query_cave_reader;
REVOKE ALL ON TABLE gotham_schema.incidents_table FROM query_cave_reader;
REVOKE ALL ON SCHEMA signal_schema FROM query_cave_reader;
GRANT USAGE ON SCHEMA signal_schema TO query_cave_reader;
REVOKE ALL ON TABLE signal_schema.case_files_view FROM PUBLIC;
REVOKE ALL ON TABLE signal_schema.case_files_view FROM query_cave_reader;
GRANT SELECT ON TABLE signal_schema.case_files_view TO query_cave_reader;

COMMIT;
