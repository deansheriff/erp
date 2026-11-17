#!/usr/bin/env bash
# Runs once when the Postgres container's data directory is first created.
set -euo pipefail

psql_admin() {
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres "$@"
}

psql_admin \
  -v app_password="$KX_APP_DB_PASSWORD" \
  -v migrator_password="$KX_MIGRATOR_DB_PASSWORD" \
  -f /docker-entrypoint-initdb.d/sql/roles.sql

for db in kxerp kxerp_test; do
  psql_admin -v dbname="$db" -f /docker-entrypoint-initdb.d/sql/database.sql
done
