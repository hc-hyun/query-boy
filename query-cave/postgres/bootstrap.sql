\set ON_ERROR_STOP on

DO $$
BEGIN
  IF current_database() <> 'query_cave' THEN
    RAISE EXCEPTION 'Query Cave must be installed in query_cave';
  END IF;
  IF current_setting('server_version_num')::integer / 10000 <> 18 THEN
    RAISE EXCEPTION 'Query Cave requires PostgreSQL 18';
  END IF;
  IF current_setting('server_encoding') <> 'UTF8' THEN
    RAISE EXCEPTION 'Query Cave requires UTF8';
  END IF;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'query_cave_reader'
  ) THEN
    CREATE ROLE query_cave_reader
      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
      CONNECTION LIMIT 7;
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'query_cave_view_owner'
  ) THEN
    CREATE ROLE query_cave_view_owner
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
END;
$$;

ALTER ROLE query_cave_reader
  LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;
ALTER ROLE query_cave_view_owner
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

REVOKE SET ON PARAMETER temp_file_limit FROM PUBLIC;
GRANT SET ON PARAMETER temp_file_limit TO query_cave_reader;

REVOKE CONNECT, TEMPORARY ON DATABASE query_cave FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE query_cave TO query_cave_reader;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;
REVOKE ALL ON DATABASE postgres FROM query_cave_reader;

ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET default_transaction_read_only = on;
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET statement_timeout = '5s';
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET lock_timeout = '250ms';
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET transaction_timeout = '8s';
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET idle_in_transaction_session_timeout = '2s';
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET work_mem = '8MB';
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET temp_file_limit = '64MB';
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET max_parallel_workers_per_gather = 0;
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET jit = off;
ALTER ROLE query_cave_reader IN DATABASE query_cave
  SET search_path = pg_catalog;

COMMENT ON ROLE query_cave_reader IS
  'Restricted certificate-authenticated reader for Query Cave.';
COMMENT ON ROLE query_cave_view_owner IS
  'Non-login owner for the Query Cave curated view.';

BEGIN;

CREATE SCHEMA IF NOT EXISTS gotham_schema;
CREATE SCHEMA IF NOT EXISTS signal_schema;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA gotham_schema FROM PUBLIC;
REVOKE ALL ON SCHEMA signal_schema FROM PUBLIC;

CREATE TABLE IF NOT EXISTS gotham_schema.incidents_table (
  case_id bigint PRIMARY KEY,
  priority smallint NOT NULL,
  response_code integer NOT NULL,
  summary text NOT NULL,
  reported_on date NOT NULL,
  reported_at timestamp with time zone NOT NULL,
  risk_score numeric(12, 2) NOT NULL
);

COMMENT ON TABLE gotham_schema.incidents_table IS
  'Private synthetic incidents used by Query Cave.';

REVOKE ALL ON TABLE gotham_schema.incidents_table FROM PUBLIC;
REVOKE ALL ON TABLE gotham_schema.incidents_table FROM query_cave_view_owner;
REVOKE ALL ON TABLE gotham_schema.incidents_table FROM query_cave_reader;

TRUNCATE TABLE gotham_schema.incidents_table;
INSERT INTO gotham_schema.incidents_table (
  case_id,
  priority,
  response_code,
  summary,
  reported_on,
  reported_at,
  risk_score
)
VALUES
  (1, 10, 100, 'Rooftop signal inspection', DATE '2026-01-01', TIMESTAMPTZ '2026-01-01 00:00:00+00', 1.25),
  (2, 20, 200, 'Museum alarm review', DATE '2026-01-02', TIMESTAMPTZ '2026-01-02 00:00:00+00', 2.50),
  (3, 30, 300, 'Harbor patrol report', DATE '2026-01-03', TIMESTAMPTZ '2026-01-03 00:00:00+00', 3.75);

COMMIT;
