#!/bin/bash
# Starts the whole Supabase stack (the Dockerfile's CMD, and what Serve runs):
# checks the configuration, prepares the data folder, then hands over to
# supervisord, which runs the 11 services (supervisord.conf).
set -euo pipefail
source "$HOME/services/lib.sh"   # the settings: the container's, and settings.env

missing=()
for var in POSTGRES_PASSWORD JWT_SECRET ANON_KEY SERVICE_ROLE_KEY \
    DASHBOARD_USERNAME DASHBOARD_PASSWORD SECRET_KEY_BASE VAULT_ENC_KEY \
    PG_META_CRYPTO_KEY SUPABASE_PUBLIC_URL API_EXTERNAL_URL SITE_URL \
    POOLER_TENANT_ID; do
    [ -n "${!var:-}" ] || missing+=("$var")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Not set: ${missing[*]}. Set them on the container (see postgres-db/.env.example): on the hosting platform, or with docker run --env-file postgres-db/.env." >&2
    exit 1
fi

# Everything that must outlive the container lives in DATA_DIR, the mount:
#   pgdata/             Postgres's data files
#   postgresql-custom/  Postgres's custom config and pgsodium's root key
#                       (/etc/postgresql-custom links here)
#   storage/            the files uploaded to Storage
#   snippets/           Studio's saved SQL snippets
mkdir -p "$DATA_DIR/storage" "$DATA_DIR/snippets"
if [ ! -d "$DATA_DIR/postgresql-custom" ]; then
    cp -a /etc/postgresql-custom.dist "$DATA_DIR/postgresql-custom"
fi

exec supervisord -c "$HOME/supervisord.conf"
