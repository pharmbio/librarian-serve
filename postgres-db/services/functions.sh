#!/bin/bash
# functions: the Edge Functions runtime (supabase/edge-runtime), serving
# /home/deno/functions (functions/ in this folder). It starts with only its own
# variables, as its compose container did: the main function hands the whole
# environment to every function, so it must not see the other services' secrets.
source "$(dirname "$0")/lib.sh"
log_as functions

exec env -i \
    PATH="$PATH" HOME="$HOME" \
    JWT_SECRET="$JWT_SECRET" \
    SUPABASE_URL="http://127.0.0.1:$GATEWAY_PORT" \
    SUPABASE_PUBLIC_URL="$SUPABASE_PUBLIC_URL" \
    SUPABASE_ANON_KEY="$ANON_KEY" \
    SUPABASE_SERVICE_ROLE_KEY="$SERVICE_ROLE_KEY" \
    SUPABASE_PUBLISHABLE_KEYS="{\"default\":\"${SUPABASE_PUBLISHABLE_KEY:-}\"}" \
    SUPABASE_SECRET_KEYS="{\"default\":\"${SUPABASE_SECRET_KEY:-}\"}" \
    SUPABASE_DB_URL="postgresql://postgres:${POSTGRES_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}" \
    VERIFY_JWT="$FUNCTIONS_VERIFY_JWT" \
    edge-runtime start --ip 127.0.0.1 --port "$FUNCTIONS_PORT" --main-service /home/deno/functions/main
