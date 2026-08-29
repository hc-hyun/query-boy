#!/usr/bin/env bash
set -Eeuo pipefail

validation_file="/query-man-fixtures/90-validate-mvp.sql"

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --file "$validation_file"
