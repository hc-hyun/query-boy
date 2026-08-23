DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'query_man_control_writer'
  ) THEN
    CREATE ROLE query_man_control_writer
      NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
  END IF;
END;
$$;

ALTER ROLE query_man_control_writer
  NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;

REVOKE ALL ON SCHEMA control FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA control FROM PUBLIC;

DO $$
BEGIN
  EXECUTE format(
    'REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC',
    pg_catalog.current_database()
  );
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO query_man_control_writer',
    pg_catalog.current_database()
  );
END;
$$;

GRANT USAGE ON SCHEMA control TO query_man_control_writer;
REVOKE CREATE ON SCHEMA control FROM query_man_control_writer;
REVOKE ALL ON control.schema_migrations FROM query_man_control_writer;
GRANT SELECT, INSERT ON control.metadata_snapshots TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.active_metadata_revisions
  TO query_man_control_writer;
GRANT SELECT, INSERT ON control.source_profile_revisions
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.active_source_profiles
  TO query_man_control_writer;
GRANT SELECT, INSERT ON control.verified_query_contracts
  TO query_man_control_writer;
