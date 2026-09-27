#!/bin/bash
# Runs once, on first start of an empty postgres volume.
# Creates per-service login roles; privileges are granted by migration 0002
# to the group roles these logins belong to (docs/design/06 §6).
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v api_pw="$INTRADAY_API_DB_PASSWORD" \
     -v engine_pw="$INTRADAY_ENGINE_DB_PASSWORD" \
     -v owner="$POSTGRES_USER" <<'SQL'
CREATE ROLE intraday_api_role NOLOGIN;
CREATE ROLE intraday_engine_role NOLOGIN;
CREATE ROLE intraday_api LOGIN PASSWORD :'api_pw' IN ROLE intraday_api_role;
CREATE ROLE intraday_engine LOGIN PASSWORD :'engine_pw' IN ROLE intraday_engine_role;
-- Replay mode writes here, never into the paper-trading database.
CREATE DATABASE intraday_sim OWNER :"owner";
SQL
