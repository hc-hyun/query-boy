BEGIN;

SET LOCAL search_path = pg_catalog;
SET LOCAL lock_timeout = '250ms';

CREATE OR REPLACE VIEW ai.fixture_records (
  record_id,
  small_value,
  integer_value,
  text_value,
  date_value,
  timestamp_value,
  numeric_value
)
WITH (security_barrier = true)
AS
SELECT
  record.record_id,
  record.small_value,
  record.integer_value,
  record.text_value,
  record.date_value,
  record.timestamp_value,
  record.numeric_value
FROM fixture.fixture_records AS record;

COMMENT ON VIEW ai.fixture_records IS E'query-man:source=fixture-source;view-contract=1\nGrain: one deterministic fixture record exposing all seven supported scalar result types.';
COMMENT ON COLUMN ai.fixture_records.record_id IS
  'Stable fixture row identifier represented as bigint.';
COMMENT ON COLUMN ai.fixture_records.small_value IS
  'Deterministic smallint value.';
COMMENT ON COLUMN ai.fixture_records.integer_value IS
  'Deterministic integer value.';
COMMENT ON COLUMN ai.fixture_records.text_value IS
  'Deterministic text value.';
COMMENT ON COLUMN ai.fixture_records.date_value IS
  'Deterministic date value.';
COMMENT ON COLUMN ai.fixture_records.timestamp_value IS
  'Deterministic timestamp with time zone value.';
COMMENT ON COLUMN ai.fixture_records.numeric_value IS
  'Deterministic exact numeric value.';

REVOKE ALL ON SCHEMA ai FROM PUBLIC;

REVOKE ALL ON SCHEMA fixture FROM query_man_fixture_owner;
GRANT USAGE ON SCHEMA fixture TO query_man_fixture_owner;
REVOKE ALL ON TABLE fixture.fixture_records FROM query_man_fixture_owner;
GRANT SELECT ON TABLE fixture.fixture_records TO query_man_fixture_owner;

REVOKE ALL ON SCHEMA ai FROM query_man_fixture_owner;
GRANT USAGE, CREATE ON SCHEMA ai TO query_man_fixture_owner;
ALTER VIEW ai.fixture_records OWNER TO query_man_fixture_owner;
REVOKE CREATE ON SCHEMA ai FROM query_man_fixture_owner;

REVOKE ALL ON SCHEMA fixture FROM query_man_fixture_reader;
REVOKE ALL ON TABLE fixture.fixture_records FROM query_man_fixture_reader;
REVOKE ALL ON SCHEMA ai FROM query_man_fixture_reader;
GRANT USAGE ON SCHEMA ai TO query_man_fixture_reader;
REVOKE ALL ON TABLE ai.fixture_records FROM PUBLIC;
REVOKE ALL ON TABLE ai.fixture_records FROM query_man_fixture_reader;
GRANT SELECT ON TABLE ai.fixture_records TO query_man_fixture_reader;

COMMIT;
