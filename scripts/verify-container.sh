#!/usr/bin/env bash

set -euo pipefail

container_id="$(docker compose ps -q query-man)"
if [[ -z "$container_id" ]]; then
  echo "query-man container is not running" >&2
  exit 1
fi

published_port="$(docker compose port query-man 3000 | sed 's/.*://')"
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

container_uid="$(docker compose exec -T query-man id -u | tr -d '\r')"
if [[ "$container_uid" == "0" ]]; then
  echo "query-man container is running as root" >&2
  exit 1
fi

if [[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container_id")" != "true" ]]; then
  echo "query-man root filesystem is writable" >&2
  exit 1
fi

docker compose exec -T query-man sh -c \
  'test ! -e /app/.env && test ! -e /app/.git && test ! -e /app/tests'

operator_token="$(
  docker compose exec -T query-man printenv QUERY_MAN_OPERATOR_TOKEN | tr -d '\r\n'
)"
source_status="$(
  printf 'Authorization: Bearer %s\n' "$operator_token" |
    curl -sS --max-time 5 --max-filesize 1048576 \
      -o /dev/null -w '%{http_code}' -H @- "${base_url}/sources"
)"
if [[ "$source_status" != "200" ]]; then
  echo "authenticated /sources returned ${source_status}, expected 200" >&2
  exit 1
fi
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
