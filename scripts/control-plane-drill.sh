#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

drill_database="query_man_restore_drill"

existing="$({ docker compose exec -T postgres psql \
  --username query_man_admin \
  --dbname postgres \
  --tuples-only \
  --no-align \
  --command="SELECT 1 FROM pg_catalog.pg_database WHERE datname = '$drill_database'"; } | tr -d '[:space:]')"
if [[ -n "$existing" ]]; then
  echo "Refusing to overwrite existing database: $drill_database" >&2
  exit 1
fi

cleanup() {
  docker compose exec -T postgres dropdb \
    --username query_man_admin \
    --if-exists \
    "$drill_database" >/dev/null
}
trap cleanup EXIT

docker compose exec -T postgres createdb \
  --username query_man_admin \
  "$drill_database"

docker compose exec -T postgres pg_dump \
  --username query_man_admin \
  --dbname query_man \
  --format=custom \
  --schema control \
  --no-owner \
  --no-privileges \
| docker compose exec -T postgres pg_restore \
  --username query_man_admin \
  --dbname "$drill_database" \
  --no-owner \
  --no-privileges \
  --exit-on-error >/dev/null

for _migration_pass in 1 2; do
  docker compose exec -T postgres psql \
    --username query_man_admin \
    --dbname "$drill_database" \
    --set=ON_ERROR_STOP=1 \
    --file=/docker-entrypoint-initdb.d/05-control-plane.sql >/dev/null
done

for table_name in \
  metadata_snapshots \
  active_metadata_revisions \
  source_profile_revisions \
  active_source_profiles \
  verified_query_contracts
do
  source_count="$(docker compose exec -T postgres psql \
    --username query_man_admin --dbname query_man --tuples-only --no-align \
    --command="SELECT count(*) FROM control.$table_name")"
  restored_count="$(docker compose exec -T postgres psql \
    --username query_man_admin --dbname "$drill_database" --tuples-only --no-align \
    --command="SELECT count(*) FROM control.$table_name")"
  if [[ "$source_count" != "$restored_count" ]]; then
    echo "Restore count mismatch: control.$table_name" >&2
    exit 1
  fi
done

schema_contract="$(docker compose exec -T postgres psql \
  --username query_man_admin \
  --dbname "$drill_database" \
  --tuples-only \
  --no-align \
  --set=ON_ERROR_STOP=1 \
  --command="
    SELECT
      (SELECT count(*)
       FROM pg_catalog.pg_constraint AS constraint_row
       JOIN pg_catalog.pg_namespace AS namespace_row
         ON namespace_row.oid = constraint_row.connamespace
       WHERE namespace_row.nspname = 'control'
         AND constraint_row.contype = 'f'),
      (SELECT count(*)
       FROM pg_catalog.pg_trigger AS trigger_row
       JOIN pg_catalog.pg_class AS relation_row
         ON relation_row.oid = trigger_row.tgrelid
       JOIN pg_catalog.pg_namespace AS namespace_row
         ON namespace_row.oid = relation_row.relnamespace
       WHERE namespace_row.nspname = 'control'
         AND NOT trigger_row.tgisinternal),
      has_database_privilege(
        'query_man_control_writer', pg_catalog.current_database(), 'CONNECT'
      )
      AND has_schema_privilege('query_man_control_writer', 'control', 'USAGE')
      AND has_table_privilege(
        'query_man_control_writer', 'control.metadata_snapshots', 'SELECT,INSERT'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.metadata_snapshots', 'UPDATE,DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.active_metadata_revisions',
        'SELECT,INSERT,UPDATE'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.active_metadata_revisions', 'DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.source_profile_revisions', 'SELECT,INSERT'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.source_profile_revisions', 'UPDATE,DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.active_source_profiles',
        'SELECT,INSERT,UPDATE'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.active_source_profiles', 'DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.verified_query_contracts', 'SELECT,INSERT'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.verified_query_contracts', 'UPDATE,DELETE'
      );")"

if [[ "$schema_contract" != "4|3|t" ]]; then
  echo "Restored control schema contract mismatch: $schema_contract" >&2
  exit 1
fi

echo "control-plane restore drill: PASS (custom archive, 5 tables, 4 FKs, 3 triggers, writer ACL)"
