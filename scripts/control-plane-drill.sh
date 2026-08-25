#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"

drill_database="query_man_restore_drill"
migration_stage=""

existing="$({ docker compose exec -T postgres psql \
  --username query_man_admin \
  --dbname postgres \
  --tuples-only \
  --no-align \
  --command="SELECT 1 FROM pg_catalog.pg_database WHERE datname = '$drill_database'"; } | tr -d '[:space:]')"
if [[ -n "$existing" ]]; then
  echo "Refusing to overwrite existing database: $drill_database" >&2
  exit 1
fi

cleanup() {
  if [[ -n "$migration_stage" ]]; then
    docker compose exec -T postgres rm -r -- "$migration_stage" >/dev/null
  fi
  docker compose exec -T postgres dropdb \
    --username query_man_admin \
    --if-exists \
    "$drill_database" >/dev/null
}
trap cleanup EXIT

docker compose exec -T postgres createdb \
  --username query_man_admin \
  "$drill_database"

migration_stage="$(
  docker compose exec -T postgres \
    mktemp -d /tmp/query-man-control-drill.XXXXXX | tr -d '\r'
)"
docker compose cp \
  "$project_dir/docker/postgres/init/control-migrations/." \
  "postgres:$migration_stage" >/dev/null

docker compose exec -T postgres pg_dump \
  --username query_man_admin \
  --dbname query_man \
  --format=custom \
  --schema control \
  --no-owner \
  --no-privileges \
| docker compose exec -T postgres pg_restore \
  --username query_man_admin \
  --dbname "$drill_database" \
  --no-owner \
  --no-privileges \
  --exit-on-error >/dev/null

for _migration_pass in 1 2; do
  docker compose exec -T \
    --env PGUSER=query_man_admin \
    --env PGDATABASE="$drill_database" \
    postgres \
    bash "$migration_stage/apply.sh" >/dev/null
done

for table_name in \
  schema_migrations \
  metadata_snapshots \
  active_metadata_revisions \
  source_profile_revisions \
  active_source_profiles \
  verified_query_contracts \
  source_mutation_receipts \
  runtime_replicas \
  runtime_source_observations
do
  source_count="$(docker compose exec -T postgres psql \
    --username query_man_admin --dbname query_man --tuples-only --no-align \
    --command="SELECT count(*) FROM control.$table_name")"
  restored_count="$(docker compose exec -T postgres psql \
    --username query_man_admin --dbname "$drill_database" --tuples-only --no-align \
    --command="SELECT count(*) FROM control.$table_name")"
  if [[ "$source_count" != "$restored_count" ]]; then
    echo "Restore count mismatch: control.$table_name" >&2
    exit 1
  fi
done

schema_contract="$(docker compose exec -T postgres psql \
  --username query_man_admin \
  --dbname "$drill_database" \
  --tuples-only \
  --no-align \
  --set=ON_ERROR_STOP=1 \
  --command="
    SELECT
      (SELECT count(*)
       FROM pg_catalog.pg_constraint AS constraint_row
       JOIN pg_catalog.pg_namespace AS namespace_row
         ON namespace_row.oid = constraint_row.connamespace
       WHERE namespace_row.nspname = 'control'
         AND constraint_row.contype = 'f'),
      (SELECT count(*)
       FROM pg_catalog.pg_trigger AS trigger_row
       JOIN pg_catalog.pg_class AS relation_row
         ON relation_row.oid = trigger_row.tgrelid
       JOIN pg_catalog.pg_namespace AS namespace_row
         ON namespace_row.oid = relation_row.relnamespace
       WHERE namespace_row.nspname = 'control'
         AND NOT trigger_row.tgisinternal),
      (SELECT bool_and(
         filename ~ '^[0-9]{4}_[a-z0-9_]+[.]sql$'
         AND checksum ~ '^sha256:[a-f0-9]{64}$'
       ) AND count(*) > 0
       FROM control.schema_migrations),
      has_database_privilege(
        'query_man_control_writer', pg_catalog.current_database(), 'CONNECT'
      )
      AND has_schema_privilege('query_man_control_writer', 'control', 'USAGE')
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.schema_migrations',
        'SELECT,INSERT,UPDATE,DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.metadata_snapshots', 'SELECT,INSERT'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.metadata_snapshots', 'UPDATE,DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.active_metadata_revisions',
        'SELECT,INSERT,UPDATE'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.active_metadata_revisions', 'DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.source_profile_revisions', 'SELECT,INSERT'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.source_profile_revisions', 'UPDATE,DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.active_source_profiles',
        'SELECT,INSERT,UPDATE'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.active_source_profiles', 'DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.verified_query_contracts', 'SELECT,INSERT'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.verified_query_contracts', 'UPDATE,DELETE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.source_mutation_receipts', 'SELECT,INSERT'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.source_mutation_receipts', 'UPDATE,DELETE'
      )
      AND has_sequence_privilege(
        'query_man_control_writer',
        'control.source_mutation_receipts_event_id_seq', 'USAGE'
      )
      AND NOT has_sequence_privilege(
        'query_man_control_writer',
        'control.source_mutation_receipts_event_id_seq', 'SELECT,UPDATE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.runtime_replicas',
        'SELECT,INSERT,UPDATE'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.runtime_replicas', 'DELETE,TRUNCATE'
      )
      AND has_table_privilege(
        'query_man_control_writer', 'control.runtime_source_observations',
        'SELECT,INSERT,UPDATE'
      )
      AND NOT has_table_privilege(
        'query_man_control_writer', 'control.runtime_source_observations',
        'DELETE,TRUNCATE'
      );")"

if [[ "$schema_contract" != "8|4|t|t" ]]; then
  echo "Restored control schema contract mismatch: $schema_contract" >&2
  exit 1
fi

docker compose exec -T postgres psql \
  --username query_man_admin \
  --dbname "$drill_database" \
  --set=ON_ERROR_STOP=1 \
  --command="
    INSERT INTO control.metadata_snapshots (source_id, revision, snapshot)
    VALUES (
      'restore-drill-sentinel',
      'sha256:0000000000000000000000000000000000000000000000000000000000000000',
      '{\"relations\": []}'::jsonb
    )
    ON CONFLICT DO NOTHING;
    DO \$\$
    BEGIN
      BEGIN
        UPDATE control.metadata_snapshots
        SET snapshot = snapshot
        WHERE source_id = 'restore-drill-sentinel';
        RAISE EXCEPTION 'immutable trigger did not reject the update';
      EXCEPTION
        WHEN raise_exception THEN
          IF SQLERRM = 'immutable trigger did not reject the update' THEN
            RAISE;
          END IF;
      END;
    END;
    \$\$;
    INSERT INTO control.source_mutation_receipts (
      idempotency_key, request_hash, operation, source_id, actor, reason,
      expected_generation, expected_state_version, outcome, http_status,
      error_code, result
    ) VALUES (
      '00000000-0000-4000-8000-000000000000',
      'hmac-sha256:0000000000000000000000000000000000000000000000000000000000000000',
      'deactivate_source', 'restore-drill-sentinel', 'RestoreDrill',
      'drill/CTRL-05', 0, 0, 'rejected', 400,
      'SOURCE_VALIDATION_FAILED', '{}'::jsonb
    );
    DO \$\$
    BEGIN
      BEGIN
        UPDATE control.source_mutation_receipts
        SET result = result
        WHERE idempotency_key = '00000000-0000-4000-8000-000000000000';
        RAISE EXCEPTION 'mutation receipt update was not rejected';
      EXCEPTION
        WHEN raise_exception THEN
          IF SQLERRM = 'mutation receipt update was not rejected' THEN
            RAISE;
          END IF;
      END;
      BEGIN
        DELETE FROM control.source_mutation_receipts
        WHERE idempotency_key = '00000000-0000-4000-8000-000000000000';
        RAISE EXCEPTION 'mutation receipt delete was not rejected';
      EXCEPTION
        WHEN raise_exception THEN
          IF SQLERRM = 'mutation receipt delete was not rejected' THEN
            RAISE;
          END IF;
      END;
    END;
    \$\$;" >/dev/null

echo "control-plane restore drill: PASS (custom archive, 9 tables, migration ledger, 8 FKs, 4 triggers, immutable history/receipts, replica observations, writer ACL)"
