#!/bin/bash
# storage: Supabase Storage (supabase/storage-api), with its files under
# DATA_DIR. STORAGE_BACKEND=s3 and the GLOBAL_S3_* variables, set on the
# container, switch it to S3, as docker-compose.s3.yml does.
source "$(dirname "$0")/lib.sh"
log_as storage
wait_for_db
cd /opt/storage

export SERVER_HOST=127.0.0.1
export SERVER_PORT=$STORAGE_PORT
export SERVER_ADMIN_PORT=$STORAGE_ADMIN_PORT

export ANON_KEY
export SERVICE_KEY=$SERVICE_ROLE_KEY
export POSTGREST_URL=http://127.0.0.1:$REST_PORT
export AUTH_JWT_SECRET=$JWT_SECRET

export DATABASE_URL="postgres://supabase_storage_admin:${POSTGRES_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
export STORAGE_PUBLIC_URL=$SUPABASE_PUBLIC_URL
export REQUEST_ALLOW_X_FORWARDED_PATH=true
export FILE_SIZE_LIMIT=${FILE_SIZE_LIMIT:-52428800}
export STORAGE_BACKEND=${STORAGE_BACKEND:-file}
export GLOBAL_S3_BUCKET
export FILE_STORAGE_BACKEND_PATH=$DATA_DIR/storage
export TENANT_ID=$STORAGE_TENANT_ID
export REGION
export ENABLE_IMAGE_TRANSFORMATION=true
export IMGPROXY_URL=http://127.0.0.1:$IMGPROXY_PORT
export S3_PROTOCOL_ACCESS_KEY_ID S3_PROTOCOL_ACCESS_KEY_SECRET

export NODE_ENV=production
exec /opt/node24/bin/node dist/start/server.js
