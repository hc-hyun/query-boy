#!/usr/bin/env bash
set -Eeuo pipefail

: "${DEVELOPMENT_ISSUES_READER_PASSWORD:?missing fixture reader password}"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --set=fixture_reader_password="$DEVELOPMENT_ISSUES_READER_PASSWORD" \
  --file=/query-man-fixtures/10-fixture-schema.sql
