-- Creates the two PostgreSQL logins SchemaGraph uses and grants them access
-- to ONE database. Run it once per database, as a superuser:
--
--   export PG_ADMIN_PASSWORD=...  PG_READONLY_PASSWORD=...
--   createdb adventureworks                      # if it doesn't exist yet
--   psql -d adventureworks -f scripts/create_roles.sql
--
-- Requires psql 15+ (\getenv). The passwords come from the same environment
-- variables that config.yml names, so they never appear in a file or on the
-- command line. Safe to re-run: roles are created only if missing, and grants
-- are simply re-applied. If you change the role names, change config.yml too.

\set ON_ERROR_STOP on
\getenv admin_pw PG_ADMIN_PASSWORD
\getenv readonly_pw PG_READONLY_PASSWORD

-- ---------------------------------------------------------------------------
-- 1. Roles (cluster-wide, not per database)
-- ---------------------------------------------------------------------------

-- admin: extraction, inference, data loading. Not a superuser.
SELECT 'CREATE ROLE sg_admin LOGIN NOSUPERUSER CREATEDB NOCREATEROLE NOREPLICATION'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sg_admin')
\gexec

-- readonly: runs every LLM-generated query. No privilege beyond SELECT.
SELECT 'CREATE ROLE sg_readonly LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'sg_readonly')
\gexec

ALTER ROLE sg_admin    PASSWORD :'admin_pw';
ALTER ROLE sg_readonly PASSWORD :'readonly_pw';

-- Defaults at the role level: a third layer behind the connection options set
-- in config.py. Keep the timeout in sync with postgres.statement_timeout_ms.
ALTER ROLE sg_readonly SET default_transaction_read_only = on;
ALTER ROLE sg_readonly SET statement_timeout = '10s';

-- ---------------------------------------------------------------------------
-- 2. Access to the current database
-- ---------------------------------------------------------------------------

GRANT CONNECT ON DATABASE :"DBNAME" TO sg_readonly;
GRANT ALL     ON DATABASE :"DBNAME" TO sg_admin;

-- Nobody gets to create objects in public by default.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

DO $$
DECLARE
    s name;
BEGIN
    FOR s IN
        SELECT nspname FROM pg_namespace
        WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'
    LOOP
        -- readonly: look up objects and SELECT from them. Nothing else.
        EXECUTE format('GRANT USAGE ON SCHEMA %I TO sg_readonly', s);
        EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO sg_readonly', s);
        -- Tables the admin role creates later (data loading) are readable too.
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE sg_admin IN SCHEMA %I GRANT SELECT ON TABLES TO sg_readonly', s);

        -- admin: full control, so it can sample values and load data even
        -- when the database was originally restored by a superuser.
        EXECUTE format('GRANT USAGE, CREATE ON SCHEMA %I TO sg_admin', s);
        EXECUTE format('GRANT ALL ON ALL TABLES IN SCHEMA %I TO sg_admin', s);
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 3. Check the result (read the output; "writable" must be 0)
-- ---------------------------------------------------------------------------

SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin
FROM pg_roles WHERE rolname IN ('sg_admin', 'sg_readonly');

SELECT
    count(*) FILTER (WHERE has_table_privilege('sg_readonly', c.oid, 'SELECT')) AS readable,
    count(*) FILTER (WHERE has_table_privilege('sg_readonly', c.oid, 'INSERT')
                        OR has_table_privilege('sg_readonly', c.oid, 'UPDATE')
                        OR has_table_privilege('sg_readonly', c.oid, 'DELETE')) AS writable
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema';