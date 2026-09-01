#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

compose=(
  docker compose
  --env-file .env
  --file compose.yaml
  --file compose.fixture.yaml
  --file compose.domain-lab.yaml
)

marker="$("${compose[@]}" exec -T postgres \
  psql --username query_man_admin --dbname retail_commerce \
    --tuples-only --no-align \
    --command "SELECT pg_catalog.current_setting('query_boy.domain_lab', true)")"
if [[ "$marker" != "on" ]]; then
  echo "refusing comment migration: domain-lab marker is not on" >&2
  exit 1
fi

databases=(
  retail_commerce
  parcel_logistics
  energy_telemetry
  clinical_operations
  saas_billing
)
schema_files=(
  retail-commerce-schema.sql
  parcel-logistics-schema.sql
  energy-telemetry-schema.sql
  clinical-operations-schema.sql
  saas-billing-schema.sql
)
expected_columns=(53 45 47 51 59)
expected_comments=(26 22 21 20 28)

for index in "${!databases[@]}"; do
  database="${databases[$index]}"
  schema_file="docker/postgres/domain-lab/${schema_files[$index]}"
  awk '/^COMMENT ON COLUMN ai\./ { print }' "$schema_file" | \
    "${compose[@]}" exec -T postgres \
      psql --username query_man_admin --dbname "$database" \
        --set=ON_ERROR_STOP=1 >/dev/null

  coverage="$("${compose[@]}" exec -T postgres \
    psql --username query_man_admin --dbname "$database" \
      --tuples-only --no-align --field-separator='|' --set=ON_ERROR_STOP=1 <<'SQL'
SELECT count(*),
       count(*) FILTER (
         WHERE pg_catalog.col_description(relation.oid, attribute.attnum) IS NOT NULL
       )
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
JOIN pg_catalog.pg_attribute AS attribute
  ON attribute.attrelid = relation.oid
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
WHERE namespace.nspname = 'ai'
  AND relation.relkind = 'v';
SQL
)"
  expected="${expected_columns[$index]}|${expected_comments[$index]}"
  if [[ "$coverage" != "$expected" ]]; then
    echo "domain-lab comment coverage mismatch for $database" >&2
    exit 1
  fi
  printf '%s|columns=%s|comments=%s\n' \
    "$database" "${expected_columns[$index]}" "${expected_comments[$index]}"
done
