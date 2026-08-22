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
      AND rolconnlimit = 7
  ) THEN
    RAISE EXCEPTION 'development reader role attributes violate policy';
  END IF;

  IF NOT has_parameter_privilege(
    'development_issues_reader',
    'temp_file_limit',
    'SET'
  ) THEN
    RAISE EXCEPTION 'development reader cannot enforce its temporary-file budget';
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
      AND rolconnlimit = 7
  ) THEN
    RAISE EXCEPTION 'market VOC reader role attributes violate policy';
  END IF;

  IF NOT has_parameter_privilege(
    'market_voc_reader',
    'temp_file_limit',
    'SET'
  ) THEN
    RAISE EXCEPTION 'market VOC reader cannot enforce its temporary-file budget';
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

\connect support_tickets

DO $$
DECLARE
  actual_count bigint;
BEGIN
  SELECT count(*) INTO actual_count FROM support.tickets;
  IF actual_count <> 120 THEN
    RAISE EXCEPTION 'support.tickets expected 120 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM ai.ticket_overview;
  IF actual_count <> 120 THEN
    RAISE EXCEPTION 'ai.ticket_overview expected 120 rows, got %', actual_count;
  END IF;

  IF has_table_privilege('support_tickets_reader', 'support.tickets', 'SELECT') THEN
    RAISE EXCEPTION 'support reader unexpectedly has base-table SELECT';
  END IF;

  IF NOT has_table_privilege('support_tickets_reader', 'ai.ticket_overview', 'SELECT') THEN
    RAISE EXCEPTION 'support reader is missing AI-view SELECT';
  END IF;

  IF has_schema_privilege('support_tickets_reader', 'support', 'USAGE') THEN
    RAISE EXCEPTION 'support reader unexpectedly has base-schema USAGE';
  END IF;

  IF has_database_privilege('support_tickets_reader', current_database(), 'TEMP') THEN
    RAISE EXCEPTION 'support reader unexpectedly has database TEMP';
  END IF;

  IF has_schema_privilege('support_tickets_reader', 'ai', 'CREATE') THEN
    RAISE EXCEPTION 'support reader unexpectedly has AI-schema CREATE';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'support_tickets_reader'
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolreplication
      AND NOT rolbypassrls
      AND rolconnlimit = 7
  ) THEN
    RAISE EXCEPTION 'support reader role attributes violate policy';
  END IF;

  IF NOT has_parameter_privilege(
    'support_tickets_reader',
    'temp_file_limit',
    'SET'
  ) THEN
    RAISE EXCEPTION 'support reader cannot enforce its temporary-file budget';
  END IF;
END;
$$;

SELECT
  'support_tickets' AS database_name,
  count(*) AS primary_rows,
  count(*) FILTER (WHERE status <> 'RESOLVED') AS unresolved_rows
FROM support.tickets;

\connect commerce_edges

DO $$
DECLARE
  actual_count bigint;
  actual_amount numeric(12, 2);
BEGIN
  SELECT count(*) INTO actual_count FROM commerce.orders;
  IF actual_count <> 4 THEN
    RAISE EXCEPTION 'commerce.orders expected 4 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM commerce.order_lines;
  IF actual_count <> 6 THEN
    RAISE EXCEPTION 'commerce.order_lines expected 6 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM ai."Order";
  IF actual_count <> 4 THEN
    RAISE EXCEPTION 'ai."Order" expected 4 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count FROM ai."OrderLine";
  IF actual_count <> 6 THEN
    RAISE EXCEPTION 'ai."OrderLine" expected 6 rows, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM commerce.orders AS order_row
  WHERE NOT EXISTS (
    SELECT 1
    FROM commerce.order_lines AS line
    WHERE line.order_id = order_row.order_id
  );
  IF actual_count <> 1 THEN
    RAISE EXCEPTION 'commerce orders without lines expected 1, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM commerce.orders
  WHERE discount_amount IS NULL;
  IF actual_count <> 1 THEN
    RAISE EXCEPTION 'commerce null discounts expected 1, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM commerce.orders
  WHERE promised_on IS NULL;
  IF actual_count <> 1 THEN
    RAISE EXCEPTION 'commerce null promised dates expected 1, got %', actual_count;
  END IF;

  SELECT count(*) INTO actual_count
  FROM commerce.order_lines
  WHERE returned_at IS NOT NULL;
  IF actual_count <> 2 THEN
    RAISE EXCEPTION 'commerce returned lines expected 2, got %', actual_count;
  END IF;

  SELECT sum("NetAmount") INTO actual_amount FROM ai."Order";
  IF actual_amount <> 324.75 THEN
    RAISE EXCEPTION 'commerce net amount expected 324.75, got %', actual_amount;
  END IF;

  SELECT count(*) INTO actual_count
  FROM commerce.order_lines AS line
  JOIN commerce.orders AS order_row ON order_row.order_id = line.order_id
  WHERE line.returned_at < order_row.placed_at;
  IF actual_count <> 0 THEN
    RAISE EXCEPTION 'commerce return chronology violations: %', actual_count;
  END IF;

  IF EXISTS (
    SELECT expected.column_name, expected.data_type
    FROM (
      VALUES
        ('OrderID', 'uuid'),
        ('PlacedAt', 'timestamp with time zone'),
        ('DiscountAmount', 'numeric(12,2)'),
        ('NetAmount', 'numeric(12,2)'),
        ('PromisedOn', 'date'),
        ('Attributes', 'jsonb')
    ) AS expected(column_name, data_type)
    EXCEPT
    SELECT
      attribute.attname::text,
      pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'ai."Order"'::regclass
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) THEN
    RAISE EXCEPTION 'ai."Order" type contract mismatch';
  END IF;

  IF EXISTS (
    SELECT expected.column_name, expected.data_type
    FROM (
      VALUES
        ('OrderID', 'uuid'),
        ('LineNo', 'smallint'),
        ('Quantity', 'integer'),
        ('UnitPrice', 'numeric(12,2)'),
        ('LineAmount', 'numeric(12,2)'),
        ('ReturnedAt', 'timestamp with time zone')
    ) AS expected(column_name, data_type)
    EXCEPT
    SELECT
      attribute.attname::text,
      pg_catalog.format_type(attribute.atttypid, attribute.atttypmod)
    FROM pg_catalog.pg_attribute AS attribute
    WHERE attribute.attrelid = 'ai."OrderLine"'::regclass
      AND attribute.attnum > 0
      AND NOT attribute.attisdropped
  ) THEN
    RAISE EXCEPTION 'ai."OrderLine" type contract mismatch';
  END IF;

  IF has_table_privilege('commerce_edges_reader', 'commerce.orders', 'SELECT') THEN
    RAISE EXCEPTION 'commerce reader unexpectedly has orders SELECT';
  END IF;

  IF has_table_privilege('commerce_edges_reader', 'commerce.order_lines', 'SELECT') THEN
    RAISE EXCEPTION 'commerce reader unexpectedly has order-lines SELECT';
  END IF;

  IF NOT has_table_privilege('commerce_edges_reader', 'ai."Order"', 'SELECT') THEN
    RAISE EXCEPTION 'commerce reader is missing quoted Order view SELECT';
  END IF;

  IF NOT has_table_privilege('commerce_edges_reader', 'ai."OrderLine"', 'SELECT') THEN
    RAISE EXCEPTION 'commerce reader is missing quoted OrderLine view SELECT';
  END IF;

  IF has_schema_privilege('commerce_edges_reader', 'commerce', 'USAGE') THEN
    RAISE EXCEPTION 'commerce reader unexpectedly has base-schema USAGE';
  END IF;

  IF has_schema_privilege('commerce_edges_reader', 'public', 'USAGE') THEN
    RAISE EXCEPTION 'commerce reader unexpectedly has public-schema USAGE';
  END IF;

  IF has_database_privilege('commerce_edges_reader', current_database(), 'TEMP') THEN
    RAISE EXCEPTION 'commerce reader unexpectedly has database TEMP';
  END IF;

  IF has_schema_privilege('commerce_edges_reader', 'ai', 'CREATE') THEN
    RAISE EXCEPTION 'commerce reader unexpectedly has AI-schema CREATE';
  END IF;

  IF NOT has_schema_privilege('commerce_edges_view_owner', 'commerce', 'USAGE')
     OR NOT has_table_privilege(
       'commerce_edges_view_owner',
       'commerce.orders',
       'SELECT'
     )
     OR NOT has_table_privilege(
       'commerce_edges_view_owner',
       'commerce.order_lines',
       'SELECT'
     ) THEN
    RAISE EXCEPTION 'commerce view owner is missing required base-object privileges';
  END IF;

  IF has_schema_privilege('commerce_edges_view_owner', 'ai', 'CREATE') THEN
    RAISE EXCEPTION 'commerce view owner unexpectedly retains AI-schema CREATE';
  END IF;

  IF EXISTS (
    SELECT 1
    FROM pg_catalog.pg_class AS relation
    JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
    JOIN pg_catalog.pg_roles AS owner_role ON owner_role.oid = relation.relowner
    WHERE namespace.nspname = 'ai'
      AND relation.relname IN ('Order', 'OrderLine')
      AND (
        owner_role.rolname <> 'commerce_edges_view_owner'
        OR NOT coalesce(relation.reloptions @> ARRAY['security_barrier=true'], false)
      )
  ) THEN
    RAISE EXCEPTION 'quoted commerce view ownership or security barrier mismatch';
  END IF;

  IF NOT has_database_privilege('commerce_edges_reader', 'commerce_edges', 'CONNECT')
     OR has_database_privilege('commerce_edges_reader', 'development_issues', 'CONNECT')
     OR has_database_privilege('commerce_edges_reader', 'market_voc', 'CONNECT')
     OR has_database_privilege('commerce_edges_reader', 'support_tickets', 'CONNECT') THEN
    RAISE EXCEPTION 'commerce reader cross-database CONNECT isolation mismatch';
  END IF;

  IF has_database_privilege('development_issues_reader', 'commerce_edges', 'CONNECT')
     OR has_database_privilege('market_voc_reader', 'commerce_edges', 'CONNECT')
     OR has_database_privilege('support_tickets_reader', 'commerce_edges', 'CONNECT') THEN
    RAISE EXCEPTION 'existing reader unexpectedly connects to commerce_edges';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'commerce_edges_reader'
      AND rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolreplication
      AND NOT rolbypassrls
      AND rolconnlimit = 7
  ) THEN
    RAISE EXCEPTION 'commerce reader role attributes violate policy';
  END IF;

  IF NOT has_parameter_privilege(
    'commerce_edges_reader',
    'temp_file_limit',
    'SET'
  ) THEN
    RAISE EXCEPTION 'commerce reader cannot enforce its temporary-file budget';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_roles
    WHERE rolname = 'commerce_edges_view_owner'
      AND NOT rolcanlogin
      AND NOT rolsuper
      AND NOT rolcreatedb
      AND NOT rolcreaterole
      AND NOT rolinherit
      AND NOT rolreplication
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION 'commerce view-owner role attributes violate policy';
  END IF;

  IF NOT EXISTS (
    SELECT 1
    FROM pg_catalog.pg_db_role_setting AS setting
    JOIN pg_catalog.pg_roles AS role_row ON role_row.oid = setting.setrole
    JOIN pg_catalog.pg_database AS database_row ON database_row.oid = setting.setdatabase
    WHERE role_row.rolname = 'commerce_edges_reader'
      AND database_row.datname = 'commerce_edges'
      AND 'TimeZone=UTC' = ANY(setting.setconfig)
  ) THEN
    RAISE EXCEPTION 'commerce reader timezone is not fixed to UTC';
  END IF;

  IF has_table_privilege(
    'commerce_edges_reader',
    'public.pg_stat_statements',
    'SELECT'
  ) THEN
    RAISE EXCEPTION 'commerce reader unexpectedly has pg_stat_statements SELECT';
  END IF;

  IF obj_description('ai."Order"'::regclass, 'pg_class') IS NULL
     OR obj_description('ai."OrderLine"'::regclass, 'pg_class') IS NULL THEN
    RAISE EXCEPTION 'quoted commerce views are missing relation metadata';
  END IF;
END;
$$;

SELECT
  'commerce_edges' AS database_name,
  (SELECT count(*) FROM commerce.orders) AS order_rows,
  (SELECT count(*) FROM commerce.order_lines) AS line_rows,
  (SELECT count(*) FROM commerce.orders WHERE discount_amount IS NULL) AS null_discount_rows,
  (SELECT count(*) FROM commerce.order_lines WHERE returned_at IS NOT NULL) AS returned_rows;
