#!/bin/bash
# studio: the Supabase Studio dashboard (supabase/studio). Reached through the
# gateway, which asks for DASHBOARD_USERNAME and DASHBOARD_PASSWORD.
source "$(dirname "$0")/lib.sh"
log_as studio
cd /opt/studio

export HOSTNAME=127.0.0.1
export PORT=$STUDIO_PORT

export STUDIO_PG_META_URL=http://127.0.0.1:$META_PORT
export POSTGRES_PORT=$DB_PORT POSTGRES_HOST=$DB_HOST POSTGRES_DB=$DB_NAME POSTGRES_PASSWORD
export POSTGRES_USER_READ_WRITE=postgres

export PG_META_CRYPTO_KEY
export PGRST_DB_SCHEMAS
export PGRST_DB_MAX_ROWS=${PGRST_DB_MAX_ROWS:-1000}
export PGRST_DB_EXTRA_SEARCH_PATH=${PGRST_DB_EXTRA_SEARCH_PATH:-public}

export DEFAULT_ORGANIZATION_NAME=$STUDIO_DEFAULT_ORGANIZATION
export DEFAULT_PROJECT_NAME=$STUDIO_DEFAULT_PROJECT
export OPENAI_API_KEY

export SUPABASE_URL=http://127.0.0.1:$GATEWAY_PORT
export SUPABASE_PUBLIC_URL
export SUPABASE_ANON_KEY=$ANON_KEY
export SUPABASE_SERVICE_KEY=$SERVICE_ROLE_KEY
export AUTH_JWT_SECRET=$JWT_SECRET
export SUPABASE_PUBLISHABLE_KEY SUPABASE_SECRET_KEY

# Logs need the analytics services of docker-compose.logs.yml, not included.
export ENABLED_FEATURES_LOGS_ALL=false

export SNIPPETS_MANAGEMENT_FOLDER=$DATA_DIR/snippets
export EDGE_FUNCTIONS_MANAGEMENT_FOLDER=/home/deno/functions

exec /opt/node22/bin/node apps/studio/server.js
