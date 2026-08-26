#!/usr/bin/env bash
set -Eeuo pipefail

: "${DEVELOPMENT_ISSUES_READER_PASSWORD:?missing development reader password}"
: "${MARKET_VOC_READER_PASSWORD:?missing market VOC reader password}"

# The default fixture is the current two-source launch. Preserved managed onboarding
# databases are provisioned only by compose.acceptance.yaml.
include_acceptance_fixtures="${QUERY_MAN_INCLUDE_ACCEPTANCE_FIXTURES:-0}"
if [[ "$include_acceptance_fixtures" != "0" && "$include_acceptance_fixtures" != "1" ]]; then
  echo "QUERY_MAN_INCLUDE_ACCEPTANCE_FIXTURES must be 0 or 1" >&2
  exit 1
fi

support_tickets_reader_password="${SUPPORT_TICKETS_READER_PASSWORD:-}"
commerce_edges_reader_password="${COMMERCE_EDGES_READER_PASSWORD:-}"
if [[ "$include_acceptance_fixtures" == "1" ]]; then
  : "${support_tickets_reader_password:?missing support tickets reader password}"
  : "${commerce_edges_reader_password:?missing commerce edges reader password}"
fi

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=admin_user="$POSTGRES_USER" \
  --set=development_reader_password="$DEVELOPMENT_ISSUES_READER_PASSWORD" \
  --set=market_voc_reader_password="$MARKET_VOC_READER_PASSWORD" \
  --set=include_acceptance_fixtures="$include_acceptance_fixtures" \
  --set=support_tickets_reader_password="$support_tickets_reader_password" \
  --set=commerce_edges_reader_password="$commerce_edges_reader_password" <<'SQL'
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

\if :include_acceptance_fixtures
SELECT
  format(
    'CREATE ROLE support_tickets_reader LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 7',
    :'support_tickets_reader_password'
  )
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'support_tickets_reader'
) \gexec

SELECT
  format(
    'CREATE ROLE commerce_edges_reader LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 7',
    :'commerce_edges_reader_password'
  )
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'commerce_edges_reader'
) \gexec
\endif

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

\if :include_acceptance_fixtures
SELECT
  'CREATE ROLE support_tickets_view_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'support_tickets_view_owner'
) \gexec

SELECT
  'CREATE ROLE commerce_edges_view_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS'
WHERE NOT EXISTS (
  SELECT 1 FROM pg_roles WHERE rolname = 'commerce_edges_view_owner'
) \gexec
\endif

SELECT format(
  'ALTER ROLE development_issues_reader PASSWORD %L',
  :'development_reader_password'
) \gexec

SELECT format(
  'ALTER ROLE market_voc_reader PASSWORD %L',
  :'market_voc_reader_password'
) \gexec

\if :include_acceptance_fixtures
SELECT format(
  'ALTER ROLE support_tickets_reader PASSWORD %L',
  :'support_tickets_reader_password'
) \gexec

SELECT format(
  'ALTER ROLE commerce_edges_reader PASSWORD %L',
  :'commerce_edges_reader_password'
) \gexec
\endif

ALTER ROLE development_issues_reader
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;
ALTER ROLE market_voc_reader
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;
\if :include_acceptance_fixtures
ALTER ROLE support_tickets_reader
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;
ALTER ROLE commerce_edges_reader
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS
  CONNECTION LIMIT 7;
\endif

REVOKE SET ON PARAMETER temp_file_limit FROM PUBLIC;
GRANT SET ON PARAMETER temp_file_limit TO
  development_issues_reader,
  market_voc_reader;
\if :include_acceptance_fixtures
GRANT SET ON PARAMETER temp_file_limit TO
  support_tickets_reader,
  commerce_edges_reader;
\endif

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

\if :include_acceptance_fixtures
SELECT format(
  'CREATE DATABASE support_tickets OWNER %I ENCODING %L TEMPLATE template0',
  :'admin_user',
  'UTF8'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'support_tickets'
) \gexec

SELECT format(
  'CREATE DATABASE commerce_edges OWNER %I ENCODING %L TEMPLATE template0',
  :'admin_user',
  'UTF8'
)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_database WHERE datname = 'commerce_edges'
) \gexec
\endif

REVOKE CONNECT, TEMPORARY ON DATABASE development_issues FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE market_voc FROM PUBLIC;
\if :include_acceptance_fixtures
REVOKE CONNECT, TEMPORARY ON DATABASE support_tickets FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE commerce_edges FROM PUBLIC;
\endif
REVOKE CONNECT, TEMPORARY ON DATABASE query_man FROM PUBLIC;
REVOKE CONNECT, TEMPORARY ON DATABASE postgres FROM PUBLIC;

GRANT CONNECT ON DATABASE development_issues TO development_issues_reader;
GRANT CONNECT ON DATABASE market_voc TO market_voc_reader;
\if :include_acceptance_fixtures
GRANT CONNECT ON DATABASE support_tickets TO support_tickets_reader;
GRANT CONNECT ON DATABASE commerce_edges TO commerce_edges_reader;
\endif

REVOKE ALL ON DATABASE market_voc FROM development_issues_reader;
REVOKE ALL ON DATABASE development_issues FROM market_voc_reader;
\if :include_acceptance_fixtures
REVOKE ALL ON DATABASE development_issues FROM support_tickets_reader;
REVOKE ALL ON DATABASE market_voc FROM support_tickets_reader;
REVOKE ALL ON DATABASE support_tickets FROM development_issues_reader;
REVOKE ALL ON DATABASE support_tickets FROM market_voc_reader;
REVOKE ALL ON DATABASE commerce_edges FROM development_issues_reader;
REVOKE ALL ON DATABASE commerce_edges FROM market_voc_reader;
REVOKE ALL ON DATABASE commerce_edges FROM support_tickets_reader;
REVOKE ALL ON DATABASE development_issues FROM commerce_edges_reader;
REVOKE ALL ON DATABASE market_voc FROM commerce_edges_reader;
REVOKE ALL ON DATABASE support_tickets FROM commerce_edges_reader;
\endif

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

\if :include_acceptance_fixtures
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET default_transaction_read_only = on;
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET statement_timeout = '5s';
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET lock_timeout = '250ms';
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET transaction_timeout = '8s';
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET idle_in_transaction_session_timeout = '2s';
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET work_mem = '8MB';
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET temp_file_limit = '64MB';
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET max_parallel_workers_per_gather = 0;
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET jit = off;
ALTER ROLE support_tickets_reader IN DATABASE support_tickets
  SET search_path = pg_catalog;

ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET default_transaction_read_only = on;
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET statement_timeout = '5s';
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET lock_timeout = '250ms';
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET transaction_timeout = '8s';
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET idle_in_transaction_session_timeout = '2s';
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET work_mem = '8MB';
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET temp_file_limit = '64MB';
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET max_parallel_workers_per_gather = 0;
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET jit = off;
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET search_path = pg_catalog;
ALTER ROLE commerce_edges_reader IN DATABASE commerce_edges
  SET timezone = 'UTC';
\endif

COMMENT ON ROLE development_issues_reader IS
  'Restricted login used by the query gateway for development issue views.';
COMMENT ON ROLE market_voc_reader IS
  'Restricted login used by the query gateway for market VOC views.';
\if :include_acceptance_fixtures
COMMENT ON ROLE support_tickets_reader IS
  'Restricted login used by the query gateway for support ticket views.';
COMMENT ON ROLE commerce_edges_reader IS
  'Restricted login used by the query gateway for quoted commerce views.';
\endif
SQL
