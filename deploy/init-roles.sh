#!/bin/sh
set -eu

: "${KEFU_EXEC_PASSWORD:?Set a unique execution-role password}"
: "${KEFU_READ_PASSWORD:?Set a unique read-role password}"
: "${KEFU_WORK_PASSWORD:?Set a unique work-role password}"
: "${KEFU_MIGRATE_PASSWORD:?Set a unique migration-role password}"
: "${KEFU_IMPORT_PASSWORD:?Set a unique import-role password}"

# psql quotes values as SQL literals. Passwords never become interpolated SQL code.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
    --set database_name="$POSTGRES_DB" \
    --set exec_password="$KEFU_EXEC_PASSWORD" \
    --set read_password="$KEFU_READ_PASSWORD" \
    --set work_password="$KEFU_WORK_PASSWORD" \
    --set migration_password="$KEFU_MIGRATE_PASSWORD" \
    --set import_password="$KEFU_IMPORT_PASSWORD" <<'SQL'
CREATE ROLE kefu_migrate LOGIN PASSWORD :'migration_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE kefu_import LOGIN PASSWORD :'import_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE kefu_exec LOGIN PASSWORD :'exec_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE kefu_read LOGIN PASSWORD :'read_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE ROLE kefu_work LOGIN PASSWORD :'work_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
ALTER DATABASE :"database_name" OWNER TO kefu_migrate;
ALTER SCHEMA public OWNER TO kefu_migrate;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE EXTENSION IF NOT EXISTS vector;
SQL
