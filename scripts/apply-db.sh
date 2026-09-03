#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

docker compose exec -T postgres \
  bash /docker-entrypoint-initdb.d/01-source-bootstrap.sh
