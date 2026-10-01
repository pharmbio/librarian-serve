#!/bin/bash
# pooler: Supavisor (supabase/supavisor), the connection pooler, on 5433
# (session mode) and 6543 (transaction mode). Postgres itself has 5432 here.
source "$(dirname "$0")/lib.sh"
log_as pooler
wait_for_db
set -e
cd /opt/supavisor

# Its Erlang runtime is built against OpenSSL 1.1, copied from its image.
export LD_LIBRARY_PATH=/opt/supavisor/syslib

export PORT=$POOLER_API_PORT
export PROXY_PORT_SESSION=${POOLER_PROXY_PORT_SESSION:-5433}
export PROXY_PORT_TRANSACTION=${POOLER_PROXY_PORT_TRANSACTION:-6543}

# Read by config/pooler.exs for the tenant.
export POSTGRES_HOST=$DB_HOST POSTGRES_PORT=$DB_PORT POSTGRES_DB=$DB_NAME POSTGRES_PASSWORD
export POOLER_TENANT_ID POOLER_DEFAULT_POOL_SIZE POOLER_MAX_CLIENT_CONN
export POOLER_POOL_MODE=transaction

export DATABASE_URL="ecto://supabase_admin:${POSTGRES_PASSWORD}@${DB_HOST}:${DB_PORT}/_supabase"
export CLUSTER_POSTGRES=true
export SECRET_KEY_BASE VAULT_ENC_KEY
export API_JWT_SECRET=$JWT_SECRET
export METRICS_JWT_SECRET=$JWT_SECRET
export REGION=local
export DB_POOL_SIZE=$POOLER_DB_POOL_SIZE

# From the supabase/supavisor image, IPv4 as in compose.
export MIX_ENV=prod
export ERL_AFLAGS="-proto_dist inet_tcp"
export ERL_EPMD_ADDRESS=127.0.0.1
export ERL_CRASH_DUMP=/tmp/supavisor_erl_crash.dump
ulimit -n 100000 2>/dev/null || true

bin/migrate
bin/supavisor eval "$(cat /etc/pooler/pooler.exs)"
exec bin/server
