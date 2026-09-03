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
    "${compose[@]}" logs --no-color --tail=200 api postgres || true
  fi
  if ! "${compose[@]}" down -v --remove-orphans; then
    echo "failed to remove container fixture resources" >&2
    if ((status == 0)); then
      status=1
    fi
  fi
  exit "$status"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" down -v --remove-orphans
"${compose[@]}" config --quiet
"${compose[@]}" up -d --wait --wait-timeout 120 postgres

vcs_ref="${QUERY_MAN_VCS_REF:-local}"
"${compose[@]}" build --build-arg QUERY_MAN_VCS_REF="$vcs_ref" api
"${compose[@]}" up -d --no-build --wait --wait-timeout 120 api

container_id="$("${compose[@]}" ps -q api)"
if [[ -z "$container_id" ]]; then
  echo "api container is not running" >&2
  exit 1
fi

revision="$(docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$container_id")"
if [[ "$revision" != "$vcs_ref" ]]; then
  echo "container revision ${revision} does not match ${vcs_ref}" >&2
  exit 1
fi

published_port="$("${compose[@]}" port api 3000 | sed 's/.*://')"
base_url="http://127.0.0.1:${published_port}"
readiness="$(curl -fsS "${base_url}/ready")"
if [[ "$readiness" != '{"status":"ready"}' ]]; then
  echo "unexpected readiness response: ${readiness}" >&2
  exit 1
fi

unauthenticated_status="$(
  curl -sS -o /dev/null -w '%{http_code}' "${base_url}/sources"
)"
if [[ "$unauthenticated_status" != "401" ]]; then
  echo "unauthenticated /sources returned ${unauthenticated_status}, expected 401" >&2
  exit 1
fi

container_uid="$("${compose[@]}" exec -T api id -u | tr -d '\r')"
if [[ "$container_uid" == "0" ]]; then
  echo "api container is running as root" >&2
  exit 1
fi

if [[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_id")" != "true" ]]; then
  echo "api root filesystem is writable" >&2
  exit 1
fi

"${compose[@]}" exec -T api sh -c \
  'test ! -e /app/.env && test ! -e /app/.git && test ! -e /app/tests'

query_token="$(
  "${compose[@]}" exec -T api printenv QUERY_MAN_QUERY_TOKEN | tr -d '\r\n'
)"
source_status="$(
  printf 'Authorization: Bearer %s\n' "$query_token" |
    curl -sS --max-time 5 --max-filesize 1048576 \
      -o /dev/null -w '%{http_code}' -H @- "${base_url}/sources"
)"
if [[ "$source_status" != "200" ]]; then
  echo "authenticated /sources returned ${source_status}, expected 200" >&2
  exit 1
fi
meta_status="$(
  printf 'Authorization: Bearer %s\n' "$query_token" |
    curl -sS --max-time 5 --max-filesize 1048576 \
      -o /dev/null -w '%{http_code}' -H @- \
      -H 'Content-Type: application/json' \
      --data '{"source_id":"fixture-source"}' "${base_url}/meta"
)"
unset query_token
if [[ "$meta_status" != "200" ]]; then
  echo "authenticated /meta returned ${meta_status}, expected 200" >&2
  exit 1
fi
operator_token="$(
  "${compose[@]}" exec -T api printenv QUERY_MAN_OPERATOR_TOKEN | tr -d '\r\n'
)"
admin_status="$(
  printf 'Authorization: Bearer %s\n' "$operator_token" |
    curl -sS --max-time 5 --max-filesize 1048576 \
      -o /dev/null -w '%{http_code}' -H @- "${base_url}/admin/metrics"
)"
unset operator_token
if [[ "$admin_status" != "200" ]]; then
  echo "authenticated /admin/metrics returned ${admin_status}, expected 200" >&2
  exit 1
fi

echo "container readiness and hardening checks passed"
