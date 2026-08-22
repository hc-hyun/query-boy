CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE SCHEMA IF NOT EXISTS ai;

COMMENT ON SCHEMA ai IS
  'Reserved for future query-man control-plane read models.';

