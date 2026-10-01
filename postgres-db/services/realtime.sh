#!/bin/bash
# realtime: Supabase Realtime (supabase/realtime). Does what the image's
# run.sh does: migrate, seed the self-hosted tenant, then start. The seed
# stores the database address for the tenant, taken from DB_HOST below.
source "$(dirname "$0")/lib.sh"
log_as realtime
wait_for_db
set -e
cd /opt/realtime

export PORT=$REALTIME_PORT
export DB_HOST DB_PORT DB_NAME
export DB_USER=supabase_admin
export DB_PASSWORD=$POSTGRES_PASSWORD
export DB_AFTER_CONNECT_QUERY='SET search_path TO _realtime'
export DB_ENC_KEY=${REALTIME_DB_ENC_KEY:-supabaserealtime}

export API_JWT_SECRET=$JWT_SECRET
export SECRET_KEY_BASE
export METRICS_JWT_SECRET=$JWT_SECRET
export DNS_NODES="''"
export APP_NAME=realtime
export SEED_SELF_HOST=true
export RUN_JANITOR=true
export DISABLE_HEALTHCHECK_LOGGING=true

# From the supabase/realtime image, IPv4 as in compose. Erlang's port mapper
# and gen_rpc stay on 127.0.0.1: there is no other node to talk to.
export MIX_ENV=prod SLOT_NAME_SUFFIX=
export ERL_AFLAGS="-proto_dist inet_tcp"
export ERL_EPMD_ADDRESS=127.0.0.1
export GEN_RPC_SOCKET_IP=127.0.0.1
export ERL_CRASH_DUMP=/tmp/realtime_erl_crash.dump
ulimit -Sn 10000 2>/dev/null || true

echo "Running migrations"
bin/migrate
echo "Seeding selfhosted Realtime"
bin/realtime eval 'Realtime.Release.seeds(Realtime.Repo)'
echo "Starting Realtime"
exec bin/server
