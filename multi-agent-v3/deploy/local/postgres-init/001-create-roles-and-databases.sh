#!/bin/sh
set -eu

: "${MULTI_AGENT_V3_TEMPORAL_DB_PASSWORD:?missing Temporal DB password}"
: "${MULTI_AGENT_V3_APP_DB_PASSWORD:?missing V3 application DB password}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=temporal_password="$MULTI_AGENT_V3_TEMPORAL_DB_PASSWORD" \
  --set=app_password="$MULTI_AGENT_V3_APP_DB_PASSWORD" <<'EOSQL'
CREATE ROLE temporal_runtime LOGIN PASSWORD :'temporal_password';
CREATE ROLE multi_agent_v3_app LOGIN PASSWORD :'app_password';

CREATE DATABASE temporal OWNER temporal_runtime;
CREATE DATABASE temporal_visibility OWNER temporal_runtime;
CREATE DATABASE multi_agent_v3 OWNER multi_agent_v3_app;

REVOKE CONNECT ON DATABASE temporal FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal_visibility FROM PUBLIC;
REVOKE CONNECT ON DATABASE multi_agent_v3 FROM PUBLIC;
GRANT CONNECT ON DATABASE temporal TO temporal_runtime;
GRANT CONNECT ON DATABASE temporal_visibility TO temporal_runtime;
GRANT CONNECT ON DATABASE multi_agent_v3 TO multi_agent_v3_app;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname multi_agent_v3 <<'EOSQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO multi_agent_v3_app;
EOSQL
