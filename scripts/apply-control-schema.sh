#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ -z "${PGDATABASE:-}" ]]; then
  echo "PGDATABASE must name the target control database." >&2
  exit 1
fi
if ! command -v psql >/dev/null 2>&1; then
  echo "psql is required to apply the control schema." >&2
  exit 1
fi

# ponytail: keep credentials in libpq environment/PGPASSFILE or managed auth, not argv.
psql \
  --no-psqlrc \
  --set=ON_ERROR_STOP=1 \
  --file="$project_dir/docker/postgres/init/05-control-plane.sql"
