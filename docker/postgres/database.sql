-- Creates one application database and its privileges (ADR 0007). Run as a superuser
-- after roles.sql:  psql -v dbname=kxerp -f database.sql
-- Idempotent: safe to run again.

SELECT format('CREATE DATABASE %I OWNER kx_owner ENCODING ''UTF8'' TEMPLATE template0', :'dbname')
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = :'dbname') \gexec

SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'dbname') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO kx_app, kx_migrator', :'dbname') \gexec

\connect :dbname

ALTER SCHEMA public OWNER TO kx_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO kx_app;

-- Everything the migrator creates (as kx_owner) is usable by the app, except where a
-- migration revokes it again (audit_log keeps INSERT and SELECT only).
ALTER DEFAULT PRIVILEGES FOR ROLE kx_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kx_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kx_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO kx_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kx_owner IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO kx_app;
