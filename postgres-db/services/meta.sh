#!/bin/bash
# meta: postgres-meta (supabase/postgres-meta), the API Studio reads and
# changes the database through. It logs in as postgres, so it listens only on
# 127.0.0.1.
source "$(dirname "$0")/lib.sh"
log_as meta
wait_for_db
cd /opt/meta

export PG_META_HOST=127.0.0.1
export PG_META_PORT=$META_PORT
export PG_META_DB_HOST=$DB_HOST
export PG_META_DB_PORT=$DB_PORT
export PG_META_DB_NAME=$DB_NAME
export PG_META_DB_USER=postgres
export PG_META_DB_PASSWORD=$POSTGRES_PASSWORD
export CRYPTO_KEY=$PG_META_CRYPTO_KEY

exec /opt/node22/bin/node dist/server/server.js
