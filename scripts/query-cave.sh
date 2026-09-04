#!/usr/bin/env bash

set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
state_base="${XDG_STATE_HOME:-${HOME:?HOME must be set}/.local/state}"
local_root="${state_base%/}/query-man/query-cave"
state_dir="$local_root/state"
runtime_env="$local_root/runtime.env"

compose=(
  env
  -u QUERY_CAVE_ADMIN_PASSWORD
  -u QUERY_CAVE_HOST_GID
  -u QUERY_CAVE_HOST_UID
  -u QUERY_CAVE_POSTGRES_PORT
  -u QUERY_CAVE_ROOT_DIRECTORY
  -u QUERY_CAVE_STATE_DIRECTORY
  -u QUERY_MAN_DATABASE_CREDENTIAL_MOUNT
  -u QUERY_MAN_OPERATOR_TOKEN
  -u QUERY_MAN_PORT
  -u QUERY_MAN_POSTGRES_HOST
  -u QUERY_MAN_QUERY_TOKEN
  docker compose
  --project-name query-cave-local
  --env-file "$runtime_env"
  --profile query-man
  --file "$project_dir/compose.yaml"
  --file "$project_dir/query-cave/compose.yaml"
)

usage() {
  echo "usage: $0 {up|status|down}" >&2
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "required command not found: $1" >&2
    exit 1
  fi
}

write_runtime_env() {
  local admin_password api_port operator_token postgres_port query_token temporary_env

  require_command openssl
  admin_password="$(openssl rand -hex 32)"
  operator_token="$(openssl rand -hex 32)"
  query_token="$(openssl rand -hex 32)"
  api_port="${QUERY_CAVE_API_PORT:-33000}"
  postgres_port="${QUERY_CAVE_POSTGRES_PORT:-55432}"

  install -d -m 0700 "$local_root" "$state_dir"
  temporary_env="$local_root/runtime.env.tmp"
  (
    umask 077
    {
      printf 'QUERY_CAVE_ADMIN_PASSWORD=%s\n' "$admin_password"
      printf 'QUERY_CAVE_HOST_GID=%s\n' "$(id -g)"
      printf 'QUERY_CAVE_HOST_UID=%s\n' "$(id -u)"
      printf 'QUERY_CAVE_POSTGRES_PORT=%s\n' "$postgres_port"
      printf 'QUERY_CAVE_ROOT_DIRECTORY=%s\n' "$project_dir/query-cave"
      printf 'QUERY_CAVE_STATE_DIRECTORY=%s\n' "$state_dir"
      printf 'QUERY_MAN_DATABASE_CREDENTIAL_MOUNT=%s\n' "$state_dir/api"
      printf 'QUERY_MAN_OPERATOR_TOKEN=%s\n' "$operator_token"
      printf 'QUERY_MAN_PORT=%s\n' "$api_port"
      printf 'QUERY_MAN_POSTGRES_HOST=postgres\n'
      printf 'QUERY_MAN_QUERY_TOKEN=%s\n' "$query_token"
    } >"$temporary_env"
  )
  mv "$temporary_env" "$runtime_env"
}

ensure_runtime_env() {
  if [[ ! -f "$runtime_env" ]]; then
    write_runtime_env
  fi
}

remove_local_state() {
  if [[ "$local_root" != "${state_base%/}/query-man/query-cave" ]]; then
    echo "refusing to remove unexpected Query Cave state path" >&2
    return 1
  fi
  if [[ -e "$local_root" ]]; then
    find "$local_root" -depth -delete
  fi
}

show_status() {
  local api_container api_port readiness

  if [[ ! -f "$runtime_env" ]]; then
    echo "Query Cave is stopped"
    return 1
  fi

  "${compose[@]}" ps --all
  api_container="$("${compose[@]}" ps --status running -q api)"
  if [[ -z "$api_container" ]]; then
    echo "Query Cave API is not running"
    return 1
  fi

  api_port="$("${compose[@]}" port api 3000 | sed 's/.*://')"
  if readiness="$(curl -fsS --max-time 3 "http://127.0.0.1:${api_port}/ready")" \
    && [[ "$readiness" == '{"status":"ready"}' ]]; then
    echo "Query Cave API: ready at http://127.0.0.1:${api_port}"
    return 0
  fi

  echo "Query Cave API is running but not ready" >&2
  return 1
}

command="${1:-}"
case "$command" in
  up)
    require_command curl
    require_command docker
    ensure_runtime_env
    cd "$project_dir"
    "${compose[@]}" config --quiet
    if [[ -n "$("${compose[@]}" ps --status running -q api)" ]]; then
      echo "Query Cave is already running"
      show_status
      exit
    fi
    "${compose[@]}" build \
      --build-arg "QUERY_MAN_VCS_REF=${QUERY_MAN_VCS_REF:-local}" api
    if ! "${compose[@]}" up -d --no-build --force-recreate --wait --wait-timeout 120 api; then
      "${compose[@]}" logs --no-color --tail=200 api postgres certificates || true
      echo "Query Cave startup failed; run '$0 down' to remove its local resources" >&2
      exit 1
    fi
    show_status
    echo "Stop and remove Query Cave with: $0 down"
    ;;
  status)
    require_command curl
    require_command docker
    show_status
    ;;
  down)
    require_command docker
    ensure_runtime_env
    cd "$project_dir"
    "${compose[@]}" down -v --remove-orphans
    remove_local_state
    echo "Query Cave stopped; its synthetic data and temporary credentials were removed"
    ;;
  *)
    usage
    exit 2
    ;;
esac
