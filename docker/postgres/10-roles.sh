#!/usr/bin/env bash
# Veritabanı rolleri ve veritabanı (docs/02-mimari.md §3, docs/09-guvenlik-kvkk.md §2).
#
#   site_yonetim_owner  tabloların sahibi; göçleri (alembic) çalıştırır. Süper kullanıcı DEĞİL.
#   site_yonetim_app    uygulamanın bağlandığı rol: yalnız SELECT/INSERT/UPDATE/DELETE.
#                       Süper kullanıcı değil, BYPASSRLS yok → RLS her sorguda geçerli.
#
# postgres imajı ilk açılışta bunu kendisi çalıştırır (/docker-entrypoint-initdb.d).
# CI ve diğer ortamlar: PGHOST/PGUSER/PGPASSWORD ayarlı iken `bash docker/postgres/10-roles.sh`.
# Parolalar ortam değişkeninden gelir; betikte sır yok. Tekrar çalıştırılabilir.
set -euo pipefail

: "${DB_OWNER_PASSWORD:?DB_OWNER_PASSWORD tanımlı değil}"
: "${DB_APP_PASSWORD:?DB_APP_PASSWORD tanımlı değil}"
APP_DB_NAME="${APP_DB_NAME:-site_yonetim}"

psql -v ON_ERROR_STOP=1 --no-psqlrc --dbname "${POSTGRES_DB:-postgres}" \
  -v owner_password="$DB_OWNER_PASSWORD" \
  -v app_password="$DB_APP_PASSWORD" \
  -v db_name="$APP_DB_NAME" <<'SQL'
SELECT format('CREATE ROLE site_yonetim_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'owner_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'site_yonetim_owner') \gexec
SELECT format('CREATE ROLE site_yonetim_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'site_yonetim_app') \gexec

SELECT format('CREATE DATABASE %I OWNER site_yonetim_owner ENCODING %L TEMPLATE template0', :'db_name', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name') \gexec

REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO site_yonetim_app;

\connect :"db_name"
ALTER SCHEMA public OWNER TO site_yonetim_owner;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO site_yonetim_app;
ALTER DEFAULT PRIVILEGES FOR ROLE site_yonetim_owner IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO site_yonetim_app;
ALTER DEFAULT PRIVILEGES FOR ROLE site_yonetim_owner IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO site_yonetim_app;
SQL
