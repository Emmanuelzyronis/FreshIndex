#!/bin/sh
set -eu

psql --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=cdc_user="$CDC_DB_USER" \
  --set=cdc_password="$CDC_DB_PASSWORD" \
  --set=writer_user="$WRITER_DB_USER" \
  --set=writer_password="$WRITER_DB_PASSWORD" <<'SQL'
SELECT format(
    'CREATE ROLE %I LOGIN REPLICATION PASSWORD %L',
    :'cdc_user', :'cdc_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'cdc_user')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN REPLICATION PASSWORD %L',
    :'cdc_user', :'cdc_password'
)
\gexec

SELECT format(
    'CREATE ROLE %I LOGIN PASSWORD %L',
    :'writer_user', :'writer_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'writer_user')
\gexec

SELECT format(
    'ALTER ROLE %I WITH LOGIN NOREPLICATION PASSWORD %L',
    :'writer_user', :'writer_password'
)
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'cdc_user')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'cdc_user')
\gexec
SELECT format('GRANT SELECT ON TABLE public.products TO %I', :'cdc_user')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'writer_user')
\gexec
SELECT format('GRANT USAGE ON SCHEMA public TO %I', :'writer_user')
\gexec
SELECT format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.products TO %I', :'writer_user')
\gexec
SELECT format('GRANT USAGE, SELECT ON SEQUENCE public.products_id_seq TO %I', :'writer_user')
\gexec
SQL
