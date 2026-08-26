#!/usr/bin/env bash
set -Eeuo pipefail

validation_file="/query-man-fixtures/90-validate-mvp.sql"
acceptance_marker='\connect support_tickets'

if ! grep --fixed-strings --line-regexp --quiet "$acceptance_marker" "$validation_file"; then
  echo "Static fixture validation boundary is missing from $validation_file" >&2
  exit 1
fi

awk '$0 == "\\connect support_tickets" { exit } { print }' "$validation_file" \
  | psql \
      --username "$POSTGRES_USER" \
      --dbname "$POSTGRES_DB" \
      --set=ON_ERROR_STOP=1
