\set ON_ERROR_STOP on

DO $$
BEGIN
  IF current_database() <> 'query_man' THEN
    RAISE EXCEPTION 'fixture must be installed in query_man';
  END IF;
  IF current_setting('server_version_num')::integer / 10000 <> 18 THEN
    RAISE EXCEPTION 'fixture requires PostgreSQL 18';
  END IF;
  IF current_setting('server_encoding') <> 'UTF8' THEN
    RAISE EXCEPTION 'fixture requires UTF8';
  END IF;
END;
$$;

SELECT format(
  'CREATE ROLE query_man_fixture_reader LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 7',
  :'fixture_reader_password'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'query_man_fixture_reader'
) \gexec

SELECT
  'CREATE ROLE query_man_fixture_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'query_man_fixture_owner'
) \gexec

SELECT format(
  'ALTER ROLE query_man_fixture_reader PASSWORD %L',
  :'fixture_reader_password'
) \gexec

ALTER ROLE query_man_fixture_reader
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;
ALTER ROLE query_man_fixture_owner
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

REVOKE SET ON PARAMETER temp_file_limit FROM PUBLIC;
GRANT SET ON PARAMETER temp_file_limit TO query_man_fixture_reader;

REVOKE CONNECT, TEMPORARY ON DATABASE query_man FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE query_man TO query_man_fixture_reader;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE postgres FROM query_man_fixture_reader;

ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET default_transaction_read_only = on;
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET statement_timeout = '5s';
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET lock_timeout = '250ms';
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET transaction_timeout = '8s';
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET idle_in_transaction_session_timeout = '2s';
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET work_mem = '8MB';
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET temp_file_limit = '64MB';
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET max_parallel_workers_per_gather = 0;
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET jit = off;
ALTER ROLE query_man_fixture_reader IN DATABASE query_man
  SET search_path = pg_catalog;

COMMENT ON ROLE query_man_fixture_reader IS
  'Restricted reader for the test-local Query Man fixture.';
COMMENT ON ROLE query_man_fixture_owner IS
  'Non-login owner for the test-local curated view.';

BEGIN;

CREATE SCHEMA IF NOT EXISTS fixture;
CREATE SCHEMA IF NOT EXISTS ai;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA fixture FROM PUBLIC;
REVOKE ALL ON SCHEMA ai FROM PUBLIC;

CREATE TABLE IF NOT EXISTS fixture.fixture_records (
  record_id bigint PRIMARY KEY,
  small_value smallint NOT NULL,
  integer_value integer NOT NULL,
  text_value text NOT NULL,
  date_value date NOT NULL,
  timestamp_value timestamp with time zone NOT NULL,
  numeric_value numeric(12, 2) NOT NULL
);

COMMENT ON TABLE fixture.fixture_records IS
  'Private base rows for deterministic PostgreSQL integration tests.';

REVOKE ALL ON TABLE fixture.fixture_records FROM PUBLIC;
REVOKE ALL ON TABLE fixture.fixture_records FROM query_man_fixture_owner;
REVOKE ALL ON TABLE fixture.fixture_records FROM query_man_fixture_reader;

TRUNCATE TABLE fixture.fixture_records;
INSERT INTO fixture.fixture_records (
  record_id,
  small_value,
  integer_value,
  text_value,
  date_value,
  timestamp_value,
  numeric_value
)
VALUES
  (1, 10, 100, 'alpha', DATE '2026-01-01', TIMESTAMPTZ '2026-01-01 00:00:00+00', 1.25),
  (2, 20, 200, 'beta', DATE '2026-01-02', TIMESTAMPTZ '2026-01-02 00:00:00+00', 2.50),
  (3, 30, 300, 'gamma', DATE '2026-01-03', TIMESTAMPTZ '2026-01-03 00:00:00+00', 3.75);

COMMIT;

\ir /query-man-source-views/fixture-source.sql

DO $$
DECLARE
  result_oids oid[];
BEGIN
  IF (SELECT count(*) FROM ai.fixture_records) <> 3 THEN
    RAISE EXCEPTION 'ai.fixture_records must contain exactly three rows';
  END IF;

  SELECT array_agg(attribute.atttypid ORDER BY attribute.attnum)
  INTO result_oids
  FROM pg_catalog.pg_attribute AS attribute
  WHERE attribute.attrelid = 'ai.fixture_records'::regclass
    AND attribute.attnum > 0
    AND NOT attribute.attisdropped;

  IF result_oids IS DISTINCT FROM ARRAY[20, 21, 23, 25, 1082, 1184, 1700]::oid[] THEN
    RAISE EXCEPTION 'ai.fixture_records has unexpected result OIDs: %', result_oids;
  END IF;
END;
$$;
