#!/usr/bin/env bash
set -Eeuo pipefail

: "${DEVELOPMENT_ISSUES_READER_PASSWORD:?missing development reader password}"
: "${MARKET_VOC_READER_PASSWORD:?missing market VOC reader password}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=admin_user="$POSTGRES_USER" \
  --set=development_reader_password="$DEVELOPMENT_ISSUES_READER_PASSWORD" \
  --set=market_voc_reader_password="$MARKET_VOC_READER_PASSWORD" <<'SQL'
SELECT
  format(
    'CREATE ROLE development_issues_reader LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 7',
    :'development_reader_password'
  )
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'development_issues_reader'
) \gexec

SELECT
  format(
    'CREATE ROLE market_voc_reader LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 7',
    :'market_voc_reader_password'
  )
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'market_voc_reader'
) \gexec

SELECT
  'CREATE ROLE development_issues_view_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'development_issues_view_owner'
) \gexec

SELECT
  'CREATE ROLE market_voc_view_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'market_voc_view_owner'
) \gexec

SELECT format(
  'ALTER ROLE development_issues_reader PASSWORD %L',
  :'development_reader_password'
) \gexec

SELECT format(
  'ALTER ROLE market_voc_reader PASSWORD %L',
  :'market_voc_reader_password'
) \gexec


ALTER ROLE development_issues_reader
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;
ALTER ROLE market_voc_reader
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;

REVOKE SET ON PARAMETER temp_file_limit FROM PUBLIC;
GRANT SET ON PARAMETER temp_file_limit TO
  development_issues_reader,
  market_voc_reader;

SELECT format(
  'CREATE DATABASE development_issues OWNER %I ENCODING %L TEMPLATE template0',
  :'admin_user',
  'UTF8'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'development_issues'
) \gexec

SELECT format(
  'CREATE DATABASE market_voc OWNER %I ENCODING %L TEMPLATE template0',
  :'admin_user',
  'UTF8'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'market_voc'
) \gexec

REVOKE CONNECT, TEMPORARY ON DATABASE development_issues FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE market_voc FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE query_man FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;

GRANT CONNECT ON DATABASE development_issues TO development_issues_reader;
GRANT CONNECT ON DATABASE market_voc TO market_voc_reader;

REVOKE ALL ON DATABASE market_voc FROM development_issues_reader;
REVOKE ALL ON DATABASE development_issues FROM market_voc_reader;

ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET default_transaction_read_only = on;
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET statement_timeout = '5s';
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET lock_timeout = '250ms';
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET transaction_timeout = '8s';
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET idle_in_transaction_session_timeout = '2s';
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET work_mem = '8MB';
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET temp_file_limit = '64MB';
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET max_parallel_workers_per_gather = 0;
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET jit = off;
ALTER ROLE development_issues_reader IN DATABASE development_issues
  SET search_path = pg_catalog;

ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET default_transaction_read_only = on;
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET statement_timeout = '5s';
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET lock_timeout = '250ms';
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET transaction_timeout = '8s';
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET idle_in_transaction_session_timeout = '2s';
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET work_mem = '8MB';
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET temp_file_limit = '64MB';
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET max_parallel_workers_per_gather = 0;
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET jit = off;
ALTER ROLE market_voc_reader IN DATABASE market_voc
  SET search_path = pg_catalog;

COMMENT ON ROLE development_issues_reader IS
  'Restricted login used by the query gateway for development issue views.';
COMMENT ON ROLE market_voc_reader IS
  'Restricted login used by the query gateway for market VOC views.';
SQL
