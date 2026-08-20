#!/bin/sh
set -eu

: "${POSTGRES_SEEDS:?missing PostgreSQL host}"
: "${POSTGRES_USER:?missing PostgreSQL user}"

attempt=1
max_attempts=60
until nc -z -w 5 "$POSTGRES_SEEDS" "${DB_PORT:-5432}"; do
  if [ "$attempt" -ge "$max_attempts" ]; then
    echo "PostgreSQL did not become ready after $max_attempts attempts" >&2
    exit 1
  fi
  attempt=$((attempt + 1))
  sleep 1
done

temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" -u "$POSTGRES_USER" \
  -p "${DB_PORT:-5432}" --db temporal setup-schema -v 0.0
temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" -u "$POSTGRES_USER" \
  -p "${DB_PORT:-5432}" --db temporal update-schema \
  -d /etc/temporal/schema/postgresql/v12/temporal/versioned

temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" -u "$POSTGRES_USER" \
  -p "${DB_PORT:-5432}" --db temporal_visibility setup-schema -v 0.0
temporal-sql-tool --plugin postgres12 --ep "$POSTGRES_SEEDS" -u "$POSTGRES_USER" \
  -p "${DB_PORT:-5432}" --db temporal_visibility update-schema \
  -d /etc/temporal/schema/postgresql/v12/visibility/versioned
