-- Database roles (ADR 0007). Run once per cluster as a superuser:
--   psql -v app_password=... -v migrator_password=... -f roles.sql
-- Idempotent: safe to run again.

SELECT 'CREATE ROLE kx_owner NOLOGIN'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kx_owner') \gexec

SELECT format('CREATE ROLE kx_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'migrator_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kx_migrator') \gexec

SELECT format('CREATE ROLE kx_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kx_app') \gexec

GRANT kx_owner TO kx_migrator;
