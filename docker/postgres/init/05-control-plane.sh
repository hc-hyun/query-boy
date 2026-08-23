#!/usr/bin/env bash
set -Eeuo pipefail

export PGDATABASE="${POSTGRES_DB:?missing POSTGRES_DB}"
export PGUSER="${POSTGRES_USER:?missing POSTGRES_USER}"

bash /docker-entrypoint-initdb.d/control-migrations/apply.sh
