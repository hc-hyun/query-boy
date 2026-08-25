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
ALTER ROLE query_man_control_writer RESET ALL;

REVOKE ALL ON SCHEMA control FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA control FROM PUBLIC;

ALTER DEFAULT PRIVILEGES IN SCHEMA control REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA control REVOKE ALL ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA control REVOKE ALL ON SEQUENCES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA control
  REVOKE ALL ON TABLES FROM query_man_control_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA control
  REVOKE ALL ON FUNCTIONS FROM query_man_control_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA control
  REVOKE ALL ON SEQUENCES FROM query_man_control_writer;

DO $$
DECLARE
  granted_role name;
BEGIN
  FOR granted_role IN
    SELECT parent_role.rolname
    FROM pg_catalog.pg_auth_members AS membership
    JOIN pg_catalog.pg_roles AS parent_role
      ON parent_role.oid = membership.roleid
    JOIN pg_catalog.pg_roles AS member_role
      ON member_role.oid = membership.member
    WHERE member_role.rolname = 'query_man_control_writer'
  LOOP
    EXECUTE format(
      'REVOKE %I FROM query_man_control_writer',
      granted_role
    );
  END LOOP;
END;
$$;

DO $$
BEGIN
  EXECUTE format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC',
    pg_catalog.current_database()
  );
  EXECUTE format(
    'REVOKE ALL PRIVILEGES ON DATABASE %I FROM query_man_control_writer',
    pg_catalog.current_database()
  );
  EXECUTE format(
    'GRANT CONNECT ON DATABASE %I TO query_man_control_writer',
    pg_catalog.current_database()
  );
END;
$$;

REVOKE ALL ON SCHEMA control FROM query_man_control_writer;
REVOKE ALL ON ALL TABLES IN SCHEMA control FROM query_man_control_writer;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA control FROM query_man_control_writer;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA control FROM query_man_control_writer;

GRANT USAGE ON SCHEMA control TO query_man_control_writer;
GRANT SELECT, INSERT ON control.metadata_snapshots TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.active_metadata_revisions
  TO query_man_control_writer;
GRANT SELECT, INSERT ON control.source_profile_revisions
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.active_source_profiles
  TO query_man_control_writer;
GRANT SELECT, INSERT ON control.verified_query_contracts
  TO query_man_control_writer;
GRANT SELECT, INSERT ON control.source_mutation_receipts
  TO query_man_control_writer;
GRANT USAGE ON SEQUENCE control.source_mutation_receipts_event_id_seq
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.runtime_replicas
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.runtime_source_observations
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.source_resource_observations
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON control.gateway_usage_rollups
  TO query_man_control_writer;
GRANT SELECT, INSERT, UPDATE ON control.gateway_usage_report_cursors
  TO query_man_control_writer;

DO $$
DECLARE
  writer_oid oid;
  relation_row record;
  column_row record;
  privilege_name text;
  expected boolean;
BEGIN
  SELECT role_row.oid
  INTO writer_oid
  FROM pg_catalog.pg_roles AS role_row
  WHERE role_row.rolname = 'query_man_control_writer'
    AND NOT role_row.rolcanlogin
    AND NOT role_row.rolsuper
    AND NOT role_row.rolcreatedb
    AND NOT role_row.rolcreaterole
    AND NOT role_row.rolinherit
    AND NOT role_row.rolreplication
    AND NOT role_row.rolbypassrls
    AND role_row.rolconfig IS NULL;
  IF writer_oid IS NULL THEN
    RAISE EXCEPTION 'Control writer role hardening did not converge';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_auth_members
    WHERE member = writer_oid
  ) THEN
    RAISE EXCEPTION 'Control writer retains an inherited role membership';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_database
    WHERE datname = pg_catalog.current_database()
      AND datdba = writer_oid
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_namespace
    WHERE nspname = 'control'
      AND nspowner = writer_oid
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'control'
      AND relation.relowner = writer_oid
  ) OR EXISTS (
    SELECT 1
    FROM pg_catalog.pg_proc AS routine
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = routine.pronamespace
    WHERE namespace.nspname = 'control'
      AND routine.proowner = writer_oid
  ) THEN
    RAISE EXCEPTION 'Control writer must not own the database or control objects';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM (
      SELECT
        database_row.datdba AS owner_oid,
        acl.grantor,
        acl.grantee,
        acl.is_grantable
      FROM pg_catalog.pg_database AS database_row
      CROSS JOIN LATERAL pg_catalog.aclexplode(
        coalesce(
          database_row.datacl,
          pg_catalog.acldefault('d', database_row.datdba)
        )
      ) AS acl
      WHERE database_row.datname = pg_catalog.current_database()

      UNION ALL

      SELECT
        namespace.nspowner AS owner_oid,
        acl.grantor,
        acl.grantee,
        acl.is_grantable
      FROM pg_catalog.pg_namespace AS namespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(
        coalesce(
          namespace.nspacl,
          pg_catalog.acldefault('n', namespace.nspowner)
        )
      ) AS acl
      WHERE namespace.nspname = 'control'

      UNION ALL

      SELECT
        relation.relowner AS owner_oid,
        acl.grantor,
        acl.grantee,
        acl.is_grantable
      FROM pg_catalog.pg_class AS relation
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(
        relation.relacl
      ) AS acl
      WHERE namespace.nspname = 'control'

      UNION ALL

      SELECT
        relation.relowner AS owner_oid,
        acl.grantor,
        acl.grantee,
        acl.is_grantable
      FROM pg_catalog.pg_attribute AS attribute
      JOIN pg_catalog.pg_class AS relation
        ON relation.oid = attribute.attrelid
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = relation.relnamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(
        attribute.attacl
      ) AS acl
      WHERE namespace.nspname = 'control'
        AND attribute.attnum > 0
        AND NOT attribute.attisdropped

      UNION ALL

      SELECT
        routine.proowner AS owner_oid,
        acl.grantor,
        acl.grantee,
        acl.is_grantable
      FROM pg_catalog.pg_proc AS routine
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = routine.pronamespace
      CROSS JOIN LATERAL pg_catalog.aclexplode(
        coalesce(
          routine.proacl,
          pg_catalog.acldefault('f', routine.proowner)
        )
      ) AS acl
      WHERE namespace.nspname = 'control'
    ) AS effective_acl
    WHERE effective_acl.grantee = 0
      OR (
        effective_acl.grantee = writer_oid
        AND (
          effective_acl.is_grantable
          OR effective_acl.grantor <> effective_acl.owner_oid
        )
      )
      OR effective_acl.grantee NOT IN (
        writer_oid,
        effective_acl.owner_oid
      )
  ) THEN
    RAISE EXCEPTION 'Control ACL retains an unexpected grantee or grant option';
  END IF;

  IF NOT pg_catalog.has_database_privilege(
    'query_man_control_writer', pg_catalog.current_database(), 'CONNECT'
  ) OR pg_catalog.has_database_privilege(
    'query_man_control_writer', pg_catalog.current_database(), 'CREATE'
  ) OR pg_catalog.has_database_privilege(
    'query_man_control_writer', pg_catalog.current_database(), 'TEMPORARY'
  ) OR NOT pg_catalog.has_schema_privilege(
    'query_man_control_writer', 'control', 'USAGE'
  ) OR pg_catalog.has_schema_privilege(
    'query_man_control_writer', 'control', 'CREATE'
  ) THEN
    RAISE EXCEPTION 'Control writer database or schema ACL did not converge';
  END IF;

  FOR relation_row IN
    SELECT relation.oid, relation.relname, relation.relkind
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = relation.relnamespace
    WHERE namespace.nspname = 'control'
      AND relation.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  LOOP
    IF relation_row.relkind = 'S' THEN
      FOREACH privilege_name IN ARRAY ARRAY['USAGE', 'SELECT', 'UPDATE']
      LOOP
        expected := relation_row.relname = 'source_mutation_receipts_event_id_seq'
          AND privilege_name = 'USAGE';
        IF pg_catalog.has_sequence_privilege(
          'query_man_control_writer', relation_row.oid, privilege_name
        ) IS DISTINCT FROM expected THEN
          RAISE EXCEPTION 'Control writer sequence ACL did not converge';
        END IF;
      END LOOP;
    ELSE
      FOREACH privilege_name IN ARRAY ARRAY[
        'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'TRUNCATE',
        'REFERENCES', 'TRIGGER', 'MAINTAIN'
      ]
      LOOP
        expected := CASE privilege_name
          WHEN 'SELECT' THEN relation_row.relname IN (
            'metadata_snapshots',
            'active_metadata_revisions',
            'source_profile_revisions',
            'active_source_profiles',
            'verified_query_contracts',
            'source_mutation_receipts',
            'runtime_replicas',
            'runtime_source_observations',
            'source_resource_observations',
            'gateway_usage_rollups',
            'gateway_usage_report_cursors'
          )
          WHEN 'INSERT' THEN relation_row.relname IN (
            'metadata_snapshots',
            'active_metadata_revisions',
            'source_profile_revisions',
            'active_source_profiles',
            'verified_query_contracts',
            'source_mutation_receipts',
            'runtime_replicas',
            'runtime_source_observations',
            'source_resource_observations',
            'gateway_usage_rollups',
            'gateway_usage_report_cursors'
          )
          WHEN 'UPDATE' THEN relation_row.relname IN (
            'active_metadata_revisions',
            'active_source_profiles',
            'runtime_replicas',
            'runtime_source_observations',
            'source_resource_observations',
            'gateway_usage_rollups',
            'gateway_usage_report_cursors'
          )
          WHEN 'DELETE' THEN relation_row.relname = 'gateway_usage_rollups'
          ELSE false
        END;
        IF pg_catalog.has_table_privilege(
          'query_man_control_writer', relation_row.oid, privilege_name
        ) IS DISTINCT FROM expected THEN
          RAISE EXCEPTION 'Control writer table ACL did not converge';
        END IF;
      END LOOP;

      FOR column_row IN
        SELECT attribute.attnum
        FROM pg_catalog.pg_attribute AS attribute
        WHERE attribute.attrelid = relation_row.oid
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
      LOOP
        FOREACH privilege_name IN ARRAY ARRAY[
          'SELECT', 'INSERT', 'UPDATE', 'REFERENCES'
        ]
        LOOP
          expected := CASE privilege_name
            WHEN 'SELECT' THEN relation_row.relname IN (
              'metadata_snapshots',
              'active_metadata_revisions',
              'source_profile_revisions',
              'active_source_profiles',
              'verified_query_contracts',
              'source_mutation_receipts',
              'runtime_replicas',
              'runtime_source_observations',
              'source_resource_observations',
              'gateway_usage_rollups',
              'gateway_usage_report_cursors'
            )
            WHEN 'INSERT' THEN relation_row.relname IN (
              'metadata_snapshots',
              'active_metadata_revisions',
              'source_profile_revisions',
              'active_source_profiles',
              'verified_query_contracts',
              'source_mutation_receipts',
              'runtime_replicas',
              'runtime_source_observations',
              'source_resource_observations',
              'gateway_usage_rollups',
              'gateway_usage_report_cursors'
            )
            WHEN 'UPDATE' THEN relation_row.relname IN (
              'active_metadata_revisions',
              'active_source_profiles',
              'runtime_replicas',
              'runtime_source_observations',
              'source_resource_observations',
              'gateway_usage_rollups',
              'gateway_usage_report_cursors'
            )
            ELSE false
          END;
          IF pg_catalog.has_column_privilege(
            'query_man_control_writer',
            relation_row.oid,
            column_row.attnum,
            privilege_name
          ) IS DISTINCT FROM expected THEN
            RAISE EXCEPTION 'Control writer column ACL did not converge';
          END IF;
        END LOOP;
      END LOOP;
    END IF;
  END LOOP;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_proc AS routine
    JOIN pg_catalog.pg_namespace AS namespace
      ON namespace.oid = routine.pronamespace
    WHERE namespace.nspname = 'control'
      AND pg_catalog.has_function_privilege(
        'query_man_control_writer', routine.oid, 'EXECUTE'
      )
  ) THEN
    RAISE EXCEPTION 'Control writer function ACL did not converge';
  END IF;
END;
$$;
