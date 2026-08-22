#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

docker compose exec -T postgres \
  bash /docker-entrypoint-initdb.d/01-source-bootstrap.sh

for sql_file in \
  05-control-plane.sql \
  10-development-issues-schema.sql \
  20-development-issues-seed.sql \
  30-market-voc-schema.sql \
  40-market-voc-seed.sql \
  50-support-tickets-schema.sql \
  60-support-tickets-seed.sql \
  70-commerce-edges-schema.sql \
  80-commerce-edges-seed.sql \
  90-validate-mvp.sql
do
  docker compose exec -T postgres \
    psql \
      --username query_man_admin \
      --dbname query_man \
      --set=ON_ERROR_STOP=1 \
      --file="/docker-entrypoint-initdb.d/$sql_file"
done
