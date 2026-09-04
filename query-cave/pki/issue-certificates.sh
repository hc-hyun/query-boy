#!/usr/bin/env bash

set -Eeuo pipefail

: "${QUERY_CAVE_HOST_UID:?missing host UID}"
: "${QUERY_CAVE_HOST_GID:?missing host GID}"

state=/query-cave-state
authority="$state/authority"
server="$state/server"
host_credentials="$state/host/query-cave"
api_credentials="$state/api/query-cave"
check_credentials="$state/check/query-cave"
admin_credentials="$state/admin"
probe_credentials="$state/probes"

install -d -m 0700 \
  "$authority" "$server" "$host_credentials" "$api_credentials" "$check_credentials" \
  "$admin_credentials" "$probe_credentials"

create_ca() {
  local name="$1"

  openssl genrsa -out "$authority/${name}.key" 2048
  openssl req -x509 -new -sha256 -days 2 \
    -subj "/CN=query-cave-${name}" \
    -key "$authority/${name}.key" \
    -out "$authority/${name}.crt"
}

create_ca server-ca
create_ca client-ca
create_ca untrusted-ca

openssl genrsa -out "$server/server.key" 2048
openssl req -new -subj '/CN=postgres' \
  -key "$server/server.key" \
  -out "$authority/server.csr"
printf '%s\n' \
  'subjectAltName=DNS:postgres,IP:127.0.0.1' \
  'extendedKeyUsage=serverAuth' >"$authority/server.ext"
openssl x509 -req -sha256 -days 2 \
  -in "$authority/server.csr" \
  -CA "$authority/server-ca.crt" \
  -CAkey "$authority/server-ca.key" \
  -CAcreateserial \
  -extfile "$authority/server.ext" \
  -out "$server/server.crt"
cp "$authority/client-ca.crt" "$server/client-ca.crt"

issue_client() {
  local common_name="$1"
  local ca_name="$2"
  local output_directory="$3"
  local certificate_name="$4"

  openssl genrsa -out "$output_directory/${certificate_name}.key" 2048
  openssl req -new \
    -subj "/CN=${common_name}" \
    -key "$output_directory/${certificate_name}.key" \
    -out "$authority/${certificate_name}.csr"
  printf '%s\n' 'extendedKeyUsage=clientAuth' >"$authority/${certificate_name}.ext"
  openssl x509 -req -sha256 -days 2 \
    -in "$authority/${certificate_name}.csr" \
    -CA "$authority/${ca_name}.crt" \
    -CAkey "$authority/${ca_name}.key" \
    -CAcreateserial \
    -extfile "$authority/${certificate_name}.ext" \
    -out "$output_directory/${certificate_name}.crt"
}

issue_client query-man-query-cave client-ca "$host_credentials" client
cp "$authority/server-ca.crt" "$host_credentials/ca.crt"
cp "$host_credentials/client.crt" "$api_credentials/client.crt"
cp "$host_credentials/client.key" "$api_credentials/client.key"
cp "$host_credentials/ca.crt" "$api_credentials/ca.crt"
cp "$host_credentials/client.crt" "$check_credentials/client.crt"
cp "$host_credentials/client.key" "$check_credentials/client.key"
cp "$host_credentials/ca.crt" "$check_credentials/ca.crt"

issue_client query-cave-admin client-ca "$admin_credentials" admin
cp "$authority/server-ca.crt" "$admin_credentials/ca.crt"

issue_client query-cave-unmapped client-ca "$probe_credentials" unmapped
issue_client query-man-query-cave untrusted-ca "$probe_credentials" untrusted
cp "$authority/server-ca.crt" "$probe_credentials/ca.crt"

chmod 0600 "$server/server.key" "$host_credentials/client.key" \
  "$api_credentials/client.key" "$check_credentials/client.key" \
  "$admin_credentials/admin.key" \
  "$probe_credentials/unmapped.key" "$probe_credentials/untrusted.key"
chmod 0644 "$server/server.crt" "$server/client-ca.crt" \
  "$host_credentials/ca.crt" "$host_credentials/client.crt" \
  "$api_credentials/ca.crt" "$api_credentials/client.crt" \
  "$check_credentials/ca.crt" "$check_credentials/client.crt" \
  "$admin_credentials/ca.crt" "$admin_credentials/admin.crt" \
  "$probe_credentials/ca.crt" "$probe_credentials/unmapped.crt" \
  "$probe_credentials/untrusted.crt"

chown -R "${QUERY_CAVE_HOST_UID}:${QUERY_CAVE_HOST_GID}" "$state"
chmod 0711 "$state" "$server"
chown 999:999 "$server/server.key"
chown -R "${QUERY_CAVE_HOST_UID}:10001" "$state/api" "$api_credentials"
chmod 0750 "$state/api" "$api_credentials"
chown root:10001 "$api_credentials/client.key"
chmod 0640 "$api_credentials/client.key"
chown -R "${QUERY_CAVE_HOST_UID}:999" "$state/check" "$check_credentials"
chmod 0750 "$state/check" "$check_credentials"
chown 999:999 "$check_credentials/client.key"

openssl x509 -in "$host_credentials/client.crt" -noout -subject -nameopt RFC2253
