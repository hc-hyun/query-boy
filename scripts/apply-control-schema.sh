#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

if [[ -z "${PGDATABASE:-}" ]]; then
  echo "PGDATABASE must name the target control database." >&2
  exit 1
fi
# ponytail: keep credentials in libpq environment/PGPASSFILE or managed auth, not argv.
bash "$project_dir/docker/postgres/init/control-migrations/apply.sh"
