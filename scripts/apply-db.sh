#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

docker compose exec -T postgres \
  bash /docker-entrypoint-initdb.d/01-source-bootstrap.sh

docker compose exec -T postgres \
  psql \
    --username query_man_admin \
    --dbname query_man \
    --set=ON_ERROR_STOP=1 \
    --set=query_man_skip_views=1 \
    --file=/docker-entrypoint-initdb.d/10-development-issues-schema.sql

docker compose exec -T postgres \
  psql \
    --username query_man_admin \
    --dbname development_issues \
    --set=ON_ERROR_STOP=1 \
    --file=/query-man-source-views/development-issues.sql

docker compose exec -T postgres \
  psql \
    --username query_man_admin \
    --dbname query_man \
    --set=ON_ERROR_STOP=1 \
    --file=/docker-entrypoint-initdb.d/20-development-issues-seed.sql

docker compose exec -T postgres \
  psql \
    --username query_man_admin \
    --dbname query_man \
    --set=ON_ERROR_STOP=1 \
    --set=query_man_skip_views=1 \
    --file=/docker-entrypoint-initdb.d/30-market-voc-schema.sql

docker compose exec -T postgres \
  psql \
    --username query_man_admin \
    --dbname market_voc \
    --set=ON_ERROR_STOP=1 \
    --file=/query-man-source-views/market-voc.sql

docker compose exec -T postgres \
  psql \
    --username query_man_admin \
    --dbname query_man \
    --set=ON_ERROR_STOP=1 \
    --file=/docker-entrypoint-initdb.d/40-market-voc-seed.sql

docker compose exec -T postgres \
  bash /docker-entrypoint-initdb.d/45-validate-static-fixtures.sh
