#!/usr/bin/env bash

set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_parent="${TMPDIR:-/tmp}"
state_dir="$(mktemp -d "${state_parent%/}/query-cave.XXXXXX")"

export QUERY_CAVE_ADMIN_PASSWORD
export QUERY_CAVE_HOST_GID
export QUERY_CAVE_HOST_UID
export QUERY_CAVE_POSTGRES_PORT="${QUERY_CAVE_POSTGRES_PORT:-55432}"
export QUERY_CAVE_ROOT_DIRECTORY="$project_dir/query-cave"
export QUERY_CAVE_STATE_DIRECTORY="$state_dir"

QUERY_CAVE_ADMIN_PASSWORD="$(openssl rand -hex 32)"
QUERY_CAVE_HOST_GID="$(id -g)"
QUERY_CAVE_HOST_UID="$(id -u)"

compose=(
  docker compose
  --project-name query-cave-verification
  --file "$QUERY_CAVE_ROOT_DIRECTORY/compose.yaml"
)

cleanup() {
  local status="$?"
  trap - EXIT INT TERM

  if ((status != 0)); then
    "${compose[@]}" logs --no-color --tail=200 postgres certificates || true
  fi
  if ! "${compose[@]}" down -v --remove-orphans; then
    echo "failed to remove Query Cave database resources" >&2
    if ((status == 0)); then
      status=1
    fi
  fi
  if [[ "$state_dir" == "${state_parent%/}"/query-cave.* ]]; then
    find "$state_dir" -depth -delete
  else
    echo "refusing to remove unexpected Query Cave state path" >&2
    status=1
  fi
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$project_dir"
"${compose[@]}" down -v --remove-orphans
"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait --wait-timeout 120 postgres
"${compose[@]}" run --rm --no-deps reader-check

uv run pytest -m 'integration and not load'
uv run pytest -m load tests/test_load.py

echo "Query Cave database checks passed"
