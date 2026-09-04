#!/usr/bin/env bash

set -Eeuo pipefail

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 \
  --file=/query-cave/source/views.sql

psql \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE
  result_oids oid[];
BEGIN
  IF (SELECT count(*) FROM signal_schema.case_files_view) <> 3 THEN
    RAISE EXCEPTION 'signal_schema.case_files_view must contain exactly three rows';
  END IF;

  SELECT array_agg(attribute.atttypid ORDER BY attribute.attnum)
  INTO result_oids
  FROM pg_catalog.pg_attribute AS attribute
  WHERE attribute.attrelid = 'signal_schema.case_files_view'::regclass
    AND attribute.attnum > 0
    AND NOT attribute.attisdropped;

  IF result_oids IS DISTINCT FROM ARRAY[20, 21, 23, 25, 1082, 1184, 1700]::oid[] THEN
    RAISE EXCEPTION 'signal_schema.case_files_view has unexpected result OIDs: %', result_oids;
  END IF;
END;
$$;
SQL
