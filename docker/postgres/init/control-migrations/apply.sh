#!/usr/bin/env bash
set -Eeuo pipefail

export LC_ALL=C

migration_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${PGDATABASE:-}" ]]; then
  echo "PGDATABASE must name the target control database." >&2
  exit 1
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required to apply control migrations." >&2
  exit 1
fi
if ! command -v sha256sum >/dev/null 2>&1; then
  echo "sha256sum is required to verify control migrations." >&2
  exit 1
fi

shopt -s nullglob
migration_paths=("$migration_dir"/[0-9][0-9][0-9][0-9]_*.sql)
if (( ${#migration_paths[@]} == 0 )); then
  echo "No numbered control migrations were found." >&2
  exit 1
fi

declare -A expected_names=()
declare -A expected_checksums=()
expected_version=1
for migration_path in "${migration_paths[@]}"; do
  migration_name="$(basename "$migration_path")"
  if [[ ! "$migration_name" =~ ^([0-9]{4})_[a-z0-9_]+\.sql$ ]]; then
    echo "Invalid control migration filename: $migration_name" >&2
    exit 1
  fi
  migration_version=$((10#${BASH_REMATCH[1]}))
  if (( migration_version != expected_version )); then
    echo "Control migration sequence must be contiguous at version $expected_version." >&2
    exit 1
  fi
  expected_names[$migration_version]="$migration_name"
  expected_checksums[$migration_version]="sha256:$(sha256sum "$migration_path" | cut -d ' ' -f 1)"
  ((expected_version += 1))
done
latest_version=$((expected_version - 1))

psql \
  --no-psqlrc \
  --quiet \
  --set=ON_ERROR_STOP=1 \
  --command="SET lock_timeout = '5s'" \
  --command="BEGIN" \
  --command="SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('query-man-control-migrations', 0))" \
  --command="CREATE SCHEMA IF NOT EXISTS control" \
  --command="CREATE TABLE IF NOT EXISTS control.schema_migrations (
    version bigint PRIMARY KEY CHECK (version > 0),
    filename text NOT NULL UNIQUE CHECK (filename ~ '^[0-9]{4}_[a-z0-9_]+[.]sql$'),
    checksum text NOT NULL CHECK (checksum ~ '^sha256:[a-f0-9]{64}$'),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    applied_by name NOT NULL DEFAULT current_user
  )" \
  --command="REVOKE ALL ON SCHEMA control FROM PUBLIC" \
  --command="REVOKE ALL ON control.schema_migrations FROM PUBLIC" \
  --command="COMMIT" >/dev/null

applied_rows="$(psql \
  --no-psqlrc \
  --quiet \
  --tuples-only \
  --no-align \
  --field-separator='|' \
  --set=ON_ERROR_STOP=1 \
  --command="SELECT version, filename, checksum FROM control.schema_migrations ORDER BY version")"

while IFS='|' read -r applied_version applied_name applied_checksum; do
  [[ -z "$applied_version" ]] && continue
  if [[ -z "${expected_names[$applied_version]+present}" ]]; then
    echo "Control database has unknown migration version $applied_version." >&2
    exit 1
  fi
  if [[ "${expected_names[$applied_version]}" != "$applied_name" ]] || \
     [[ "${expected_checksums[$applied_version]}" != "$applied_checksum" ]]; then
    echo "Applied control migration $applied_version differs from the repository." >&2
    exit 1
  fi
done <<< "$applied_rows"

for migration_path in "${migration_paths[@]}"; do
  migration_name="$(basename "$migration_path")"
  migration_version=$((10#${migration_name:0:4}))
  migration_checksum="${expected_checksums[$migration_version]}"

  {
    cat <<'SQL'
BEGIN;
SET LOCAL lock_timeout = '5s';
SELECT pg_catalog.pg_advisory_xact_lock(
  pg_catalog.hashtextextended('query-man-control-migrations', 0)
);
SELECT EXISTS (
  SELECT 1
  FROM control.schema_migrations
  WHERE version = :migration_version
    AND (filename <> :'migration_name' OR checksum <> :'migration_checksum')
) AS migration_mismatch \gset
\if :migration_mismatch
  \echo 'Applied control migration differs from the repository.'
  ROLLBACK;
  \quit 3
\endif
SELECT NOT EXISTS (
  SELECT 1 FROM control.schema_migrations WHERE version = :migration_version
) AS migration_pending \gset
\if :migration_pending
  \echo Applying control migration :migration_name
SQL
    cat "$migration_path"
    cat <<'SQL'
INSERT INTO control.schema_migrations (version, filename, checksum)
VALUES (:migration_version, :'migration_name', :'migration_checksum');
\else
  \echo Control migration :migration_name is already applied
\endif
COMMIT;
SQL
  } | psql \
    --no-psqlrc \
    --quiet \
    --set=ON_ERROR_STOP=1 \
    --set=migration_version="$migration_version" \
    --set=migration_name="$migration_name" \
    --set=migration_checksum="$migration_checksum" >/dev/null
done

psql \
  --no-psqlrc \
  --quiet \
  --set=ON_ERROR_STOP=1 \
  --single-transaction \
  --file="$migration_dir/reconcile-security.sql" >/dev/null

applied_count="$(psql \
  --no-psqlrc \
  --quiet \
  --tuples-only \
  --no-align \
  --set=ON_ERROR_STOP=1 \
  --command="SELECT count(*) FROM control.schema_migrations")"
if [[ "$applied_count" != "$latest_version" ]]; then
  echo "Control migration ledger is incomplete." >&2
  exit 1
fi

echo "control migrations: current version $latest_version"
