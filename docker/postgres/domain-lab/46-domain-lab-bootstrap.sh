#!/usr/bin/env bash
set -Eeuo pipefail

: "${DEVELOPMENT_ISSUES_READER_PASSWORD:?missing development reader password}"
: "${MARKET_VOC_READER_PASSWORD:?missing market VOC reader password}"
: "${RETAIL_COMMERCE_READER_PASSWORD:?missing retail commerce reader password}"
: "${PARCEL_LOGISTICS_READER_PASSWORD:?missing parcel logistics reader password}"
: "${ENERGY_TELEMETRY_READER_PASSWORD:?missing energy telemetry reader password}"
: "${CLINICAL_OPERATIONS_READER_PASSWORD:?missing clinical operations reader password}"
: "${SAAS_BILLING_READER_PASSWORD:?missing SaaS billing reader password}"

declare -ar databases=(
  development_issues
  market_voc
  retail_commerce
  parcel_logistics
  energy_telemetry
  clinical_operations
  saas_billing
)
declare -ar schema_files=(
  development-issues-schema.sql
  market-voc-schema.sql
  retail-commerce-schema.sql
  parcel-logistics-schema.sql
  energy-telemetry-schema.sql
  clinical-operations-schema.sql
  saas-billing-schema.sql
)
declare -ar view_files=(
  development-issues/views.sql
  market-voc/views.sql
  retail-commerce/views.sql
  parcel-logistics/views.sql
  energy-telemetry/views.sql
  clinical-operations/views.sql
  saas-billing/views.sql
)
declare -ar readers=(
  development_issues_reader
  market_voc_reader
  retail_commerce_reader
  parcel_logistics_reader
  energy_telemetry_reader
  clinical_operations_reader
  saas_billing_reader
)
declare -ar owners=(
  development_issues_view_owner
  market_voc_view_owner
  retail_commerce_view_owner
  parcel_logistics_view_owner
  energy_telemetry_view_owner
  clinical_operations_view_owner
  saas_billing_view_owner
)
declare -ar passwords=(
  "$DEVELOPMENT_ISSUES_READER_PASSWORD"
  "$MARKET_VOC_READER_PASSWORD"
  "$RETAIL_COMMERCE_READER_PASSWORD"
  "$PARCEL_LOGISTICS_READER_PASSWORD"
  "$ENERGY_TELEMETRY_READER_PASSWORD"
  "$CLINICAL_OPERATIONS_READER_PASSWORD"
  "$SAAS_BILLING_READER_PASSWORD"
)
declare -ar seed_files=(
  development-issues-seed.sql
  market-voc-seed.sql
  ""
  ""
  ""
  ""
  ""
)

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --command "REVOKE SET ON PARAMETER temp_file_limit FROM PUBLIC"

for index in "${!databases[@]}"; do
  database="${databases[$index]}"
  reader="${readers[$index]}"
  owner="${owners[$index]}"
  reader_password="${passwords[$index]}"

  psql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --set=admin_user="$POSTGRES_USER" \
    --set=database="$database" \
    --set=reader="$reader" \
    --set=owner="$owner" \
    --set=reader_password="$reader_password" <<'SQL'
SELECT format(
  'CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 7',
  :'reader',
  :'reader_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'reader') \gexec

SELECT format(
  'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS',
  :'owner'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') \gexec

SELECT format('ALTER ROLE %I PASSWORD %L', :'reader', :'reader_password') \gexec
SELECT format(
  'ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 7',
  :'reader'
) \gexec
SELECT format('GRANT SET ON PARAMETER temp_file_limit TO %I', :'reader') \gexec

SELECT format(
  'CREATE DATABASE %I OWNER %I ENCODING %L TEMPLATE template0',
  :'database',
  :'admin_user',
  'UTF8'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database') \gexec

SELECT format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', :'database') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database', :'reader') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET default_transaction_read_only = on', :'reader', :'database') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET statement_timeout = %L', :'reader', :'database', '5s') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET transaction_timeout = %L', :'reader', :'database', '8s') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET lock_timeout = %L', :'reader', :'database', '250ms') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET idle_in_transaction_session_timeout = %L', :'reader', :'database', '2s') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET work_mem = %L', :'reader', :'database', '8MB') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET temp_file_limit = %L', :'reader', :'database', '64MB') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET max_parallel_workers_per_gather = 0', :'reader', :'database') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET jit = off', :'reader', :'database') \gexec
SELECT format('ALTER ROLE %I IN DATABASE %I SET search_path = pg_catalog', :'reader', :'database') \gexec
SELECT format('COMMENT ON ROLE %I IS %L', :'reader', 'Restricted domain-lab reader used only for curated ai views.') \gexec
SQL
done

for database in "${databases[@]}"; do
  for reader in "${readers[@]}"; do
    if [[ "$reader" != "${database}_reader" ]]; then
      psql \
        --username "$POSTGRES_USER" \
        --dbname "$POSTGRES_DB" \
        --set=ON_ERROR_STOP=1 \
        --set=database="$database" \
        --set=reader="$reader" <<'SQL'
SELECT format('REVOKE ALL ON DATABASE %I FROM %I', :'database', :'reader') \gexec
SQL
    fi
  done
done

for reader in "${readers[@]}"; do
  for database in query_man postgres; do
    psql \
      --username "$POSTGRES_USER" \
      --dbname "$POSTGRES_DB" \
      --set=ON_ERROR_STOP=1 \
      --set=database="$database" \
      --set=reader="$reader" <<'SQL'
SELECT format('REVOKE ALL ON DATABASE %I FROM %I', :'database', :'reader') \gexec
SQL
  done
done

for index in "${!databases[@]}"; do
  psql \
    --username "$POSTGRES_USER" \
    --dbname "${databases[$index]}" \
    --set=ON_ERROR_STOP=1 \
    --set=query_man_skip_views=1 \
    --file "/query-man-domain-lab/${schema_files[$index]}"
  psql \
    --username "$POSTGRES_USER" \
    --dbname "${databases[$index]}" \
    --set=ON_ERROR_STOP=1 \
    --file "/query-man-domain-lab-sources/${view_files[$index]}"
  if [[ -n "${seed_files[$index]}" ]]; then
    psql \
      --username "$POSTGRES_USER" \
      --dbname "${databases[$index]}" \
      --set=ON_ERROR_STOP=1 \
      --file "/query-man-domain-lab/${seed_files[$index]}"
  fi
done
