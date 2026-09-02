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
source_ids=(
  retail-commerce
  parcel-logistics
  energy-telemetry
  clinical-operations
  saas-billing
)
expected_columns=(53 45 47 51 59)
expected_comments=(26 22 21 20 28)

for index in "${!databases[@]}"; do
  database="${databases[$index]}"
  source_id="${source_ids[$index]}"
  views_file="config/domain-lab/sources/$source_id/views.sql"
  awk '/^COMMENT ON (VIEW|COLUMN) ai\./ { print }' "$views_file" | \
    "${compose[@]}" exec -T postgres \
      psql --username query_man_admin --dbname "$database" \
        --set=ON_ERROR_STOP=1 >/dev/null

  coverage="$("${compose[@]}" exec -T postgres \
    psql --username query_man_admin --dbname "$database" \
      --tuples-only --no-align --field-separator='|' --set=ON_ERROR_STOP=1 \
      --set=source_id="$source_id" <<'SQL'
SELECT count(*),
       count(*) FILTER (
         WHERE pg_catalog.col_description(relation.oid, attribute.attnum) IS NOT NULL
       ),
       (
         SELECT count(*)
         FROM pg_catalog.pg_class AS view_relation
         JOIN pg_catalog.pg_namespace AS view_namespace
           ON view_namespace.oid = view_relation.relnamespace
         WHERE view_namespace.nspname = 'ai'
           AND view_relation.relkind = 'v'
       ),
       (
         SELECT count(*)
         FROM pg_catalog.pg_class AS view_relation
         JOIN pg_catalog.pg_namespace AS view_namespace
           ON view_namespace.oid = view_relation.relnamespace
         WHERE view_namespace.nspname = 'ai'
           AND view_relation.relkind = 'v'
           AND pg_catalog.split_part(
             pg_catalog.obj_description(view_relation.oid, 'pg_class'),
             E'\n',
             1
           ) = pg_catalog.format(
             'query-man:source=%s;view-contract=1',
             :'source_id'
           )
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
  expected="${expected_columns[$index]}|${expected_comments[$index]}|3|3"
  if [[ "$coverage" != "$expected" ]]; then
    echo "domain-lab comment coverage mismatch for $database" >&2
    exit 1
  fi
  printf '%s|columns=%s|comments=%s\n' \
    "$database" "${expected_columns[$index]}" "${expected_comments[$index]}"
done
