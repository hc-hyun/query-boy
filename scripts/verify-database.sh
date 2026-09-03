#!/usr/bin/env bash

set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

fixture_env="$project_dir/.env.fixture.example"
compose=(
  docker compose
  --project-name query-man-fixture
  --env-file "$fixture_env"
  --file "$project_dir/compose.yaml"
  --file "$project_dir/compose.fixture.yaml"
)

cleanup() {
  local status="$?"
  trap - EXIT INT TERM

  if ((status != 0)); then
    "${compose[@]}" logs --no-color --tail=200 postgres || true
  fi
  if ! "${compose[@]}" down -v --remove-orphans; then
    echo "failed to remove database fixture resources" >&2
    if ((status == 0)); then
      status=1
    fi
  fi
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

set -a
# shellcheck disable=SC1090
source "$fixture_env"
set +a

"${compose[@]}" down -v --remove-orphans
"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait --wait-timeout 120 postgres

uv run pytest -m 'integration and not load'
uv run pytest -m load tests/test_load.py

echo "database fixture checks passed"
