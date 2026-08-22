\connect development_issues

DO $$
DECLARE
  actual_count bigint;
BEGIN
  SELECT count(*) INTO actual_count FROM development.users;
  IF actual_count <> 18 THEN
    RAISE EXCEPTION 'development.users expected 18 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM development.product_models;
  IF actual_count <> 6 THEN
    RAISE EXCEPTION 'development.product_models expected 6 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM development.test_units;
  IF actual_count <> 160 THEN
    RAISE EXCEPTION 'development.test_units expected 160 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM development.issues;
  IF actual_count <> 600 THEN
    RAISE EXCEPTION 'development.issues expected 600 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM development.issue_comments;
  IF actual_count <> 1500 THEN
    RAISE EXCEPTION 'development.issue_comments expected 1500 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM ai.issue_overview;
  IF actual_count <> 600 THEN
    RAISE EXCEPTION 'ai.issue_overview expected 600 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM ai.test_unit_overview;
  IF actual_count <> 160 THEN
    RAISE EXCEPTION 'ai.test_unit_overview expected 160 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM development.issue_comments AS comment
  JOIN development.issues AS issue ON issue.id = comment.issue_id
  WHERE comment.created_at < issue.discovered_at;
  IF actual_count <> 0 THEN
    RAISE EXCEPTION 'development comment chronology violations: %', actual_count;
  END IF;

  IF has_table_privilege(
    'development_issues_reader',
    'development.issues',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'development reader unexpectedly has base-table SELECT';
  END IF;

  IF NOT has_table_privilege(
    'development_issues_reader',
    'ai.issue_overview',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'development reader is missing AI-view SELECT';
  END IF;

  IF NOT has_table_privilege(
    'development_issues_reader',
    'ai.test_unit_overview',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'development reader is missing test-unit view SELECT';
  END IF;

  IF NOT has_table_privilege(
    'development_issues_reader',
    'ai.issue_comments',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'development reader is missing issue-comment view SELECT';
  END IF;

  IF has_schema_privilege('development_issues_reader', 'development', 'USAGE') THEN
    RAISE EXCEPTION 'development reader unexpectedly has base-schema USAGE';
  END IF;

  IF has_schema_privilege('development_issues_reader', 'public', 'USAGE') THEN
    RAISE EXCEPTION 'development reader unexpectedly has public-schema USAGE';
  END IF;

  IF has_database_privilege(
    'development_issues_reader',
    current_database(),
    'TEMP'
  ) THEN
    RAISE EXCEPTION 'development reader unexpectedly has database TEMP';
  END IF;

  IF has_schema_privilege('development_issues_reader', 'ai', 'CREATE') THEN
    RAISE EXCEPTION 'development reader unexpectedly has AI-schema CREATE';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'development_issues_reader'
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolreplication
      AND NOT rolbypassrls
      AND rolconnlimit = 3
  ) THEN
    RAISE EXCEPTION 'development reader role attributes violate policy';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'development_issues_view_owner'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolreplication
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'development view-owner role attributes violate policy';
  END IF;

  IF has_table_privilege(
    'development_issues_reader',
    'public.pg_stat_statements',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'development reader unexpectedly has pg_stat_statements SELECT';
  END IF;

  IF obj_description('ai.issue_overview'::regclass, 'pg_class') IS NULL THEN
    RAISE EXCEPTION 'ai.issue_overview is missing relation metadata';
  END IF;
END;
$$;

SELECT
  'development_issues' AS database_name,
  (SELECT count(*) FROM development.issues) AS primary_rows,
  (SELECT count(*) FROM development.issue_comments) AS comment_rows,
  (SELECT count(*) FROM development.issues WHERE status <> 'RESOLVED') AS unresolved_rows;

\connect market_voc

DO $$
DECLARE
  actual_count bigint;
BEGIN
  SELECT count(*) INTO actual_count FROM voc.users;
  IF actual_count <> 24 THEN
    RAISE EXCEPTION 'voc.users expected 24 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM voc.product_models;
  IF actual_count <> 8 THEN
    RAISE EXCEPTION 'voc.product_models expected 8 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM voc.devices;
  IF actual_count <> 400 THEN
    RAISE EXCEPTION 'voc.devices expected 400 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM voc.cases;
  IF actual_count <> 1200 THEN
    RAISE EXCEPTION 'voc.cases expected 1200 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM voc.case_comments;
  IF actual_count <> 3000 THEN
    RAISE EXCEPTION 'voc.case_comments expected 3000 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM ai.voc_overview;
  IF actual_count <> 1200 THEN
    RAISE EXCEPTION 'ai.voc_overview expected 1200 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM ai.device_overview;
  IF actual_count <> 400 THEN
    RAISE EXCEPTION 'ai.device_overview expected 400 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM voc.devices AS device
  WHERE NOT EXISTS (
    SELECT 1 FROM voc.cases AS case_row WHERE case_row.device_id = device.id
  );
  IF actual_count <> 40 THEN
    RAISE EXCEPTION 'market devices without VOC expected 40, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM voc.cases AS case_row
  JOIN voc.devices AS device ON device.id = case_row.device_id
  JOIN voc.product_models AS model ON model.id = device.product_model_id
  WHERE case_row.defect_category = 'HINGE'
    AND model.product_family <> 'NURI';
  IF actual_count <> 0 THEN
    RAISE EXCEPTION 'non-NURI hinge VOC rows expected 0, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM voc.case_comments AS comment
  JOIN voc.cases AS case_row ON case_row.id = comment.case_id
  WHERE comment.created_at < case_row.received_at;
  IF actual_count <> 0 THEN
    RAISE EXCEPTION 'VOC comment chronology violations: %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM voc.cases AS case_row
  JOIN voc.devices AS device ON device.id = case_row.device_id
  JOIN voc.product_models AS model ON model.id = device.product_model_id
  WHERE model.release_date > device.manufactured_at
     OR device.sold_at > case_row.occurred_at;
  IF actual_count <> 0 THEN
    RAISE EXCEPTION 'market product chronology violations: %', actual_count;
  END IF;

  IF has_table_privilege('market_voc_reader', 'voc.cases', 'SELECT') THEN
    RAISE EXCEPTION 'market VOC reader unexpectedly has base-table SELECT';
  END IF;

  IF NOT has_table_privilege('market_voc_reader', 'ai.voc_overview', 'SELECT') THEN
    RAISE EXCEPTION 'market VOC reader is missing AI-view SELECT';
  END IF;

  IF NOT has_table_privilege('market_voc_reader', 'ai.device_overview', 'SELECT') THEN
    RAISE EXCEPTION 'market VOC reader is missing device view SELECT';
  END IF;

  IF NOT has_table_privilege('market_voc_reader', 'ai.voc_comments', 'SELECT') THEN
    RAISE EXCEPTION 'market VOC reader is missing VOC-comment view SELECT';
  END IF;

  IF has_schema_privilege('market_voc_reader', 'voc', 'USAGE') THEN
    RAISE EXCEPTION 'market VOC reader unexpectedly has base-schema USAGE';
  END IF;

  IF has_schema_privilege('market_voc_reader', 'public', 'USAGE') THEN
    RAISE EXCEPTION 'market VOC reader unexpectedly has public-schema USAGE';
  END IF;

  IF has_database_privilege('market_voc_reader', current_database(), 'TEMP') THEN
    RAISE EXCEPTION 'market VOC reader unexpectedly has database TEMP';
  END IF;

  IF has_schema_privilege('market_voc_reader', 'ai', 'CREATE') THEN
    RAISE EXCEPTION 'market VOC reader unexpectedly has AI-schema CREATE';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'market_voc_reader'
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolreplication
      AND NOT rolbypassrls
      AND rolconnlimit = 3
  ) THEN
    RAISE EXCEPTION 'market VOC reader role attributes violate policy';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'market_voc_view_owner'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolreplication
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'market VOC view-owner role attributes violate policy';
  END IF;

  IF has_table_privilege(
    'market_voc_reader',
    'public.pg_stat_statements',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'market VOC reader unexpectedly has pg_stat_statements SELECT';
  END IF;

  IF obj_description('ai.voc_overview'::regclass, 'pg_class') IS NULL THEN
    RAISE EXCEPTION 'ai.voc_overview is missing relation metadata';
  END IF;
END;
$$;

SELECT
  'market_voc' AS database_name,
  (SELECT count(*) FROM voc.cases) AS primary_rows,
  (SELECT count(*) FROM voc.case_comments) AS comment_rows,
  (
    SELECT count(*)
    FROM voc.cases
    WHERE status NOT IN ('RESOLVED', 'CLOSED')
  ) AS unresolved_rows;
