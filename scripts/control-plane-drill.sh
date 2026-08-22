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
  --schema control \
  --no-owner \
  --no-privileges \
| docker compose exec -T postgres psql \
  --username query_man_admin \
  --dbname "$drill_database" \
  --set=ON_ERROR_STOP=1 >/dev/null

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

echo "control-plane restore drill: PASS"
