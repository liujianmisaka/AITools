#!/bin/sh
set -eu

: "${MULTI_AGENT_V2_TEMPORAL_DB_PASSWORD:?missing Temporal DB password}"
: "${MULTI_AGENT_V2_CONTROL_DB_PASSWORD:?missing Control DB password}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set=temporal_password="$MULTI_AGENT_V2_TEMPORAL_DB_PASSWORD" \
  --set=control_password="$MULTI_AGENT_V2_CONTROL_DB_PASSWORD" <<'EOSQL'
CREATE ROLE temporal_runtime LOGIN PASSWORD :'temporal_password';
CREATE ROLE multi_agent_app LOGIN PASSWORD :'control_password';

CREATE DATABASE temporal OWNER temporal_runtime;
CREATE DATABASE temporal_visibility OWNER temporal_runtime;
CREATE DATABASE multi_agent_v2 OWNER multi_agent_app;

REVOKE CONNECT ON DATABASE temporal FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal_visibility FROM PUBLIC;
REVOKE CONNECT ON DATABASE multi_agent_v2 FROM PUBLIC;
GRANT CONNECT ON DATABASE temporal TO temporal_runtime;
GRANT CONNECT ON DATABASE temporal_visibility TO temporal_runtime;
GRANT CONNECT ON DATABASE multi_agent_v2 TO multi_agent_app;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname multi_agent_v2 <<'EOSQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO multi_agent_app;
EOSQL
