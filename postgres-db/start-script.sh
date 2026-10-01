#!/bin/bash
# Starts the whole Supabase stack (the Dockerfile's CMD, and what Serve runs):
# checks the configuration, prepares the data folder, then hands over to
# supervisord, which runs the 11 services (supervisord.conf).
set -euo pipefail

missing=()
for var in POSTGRES_PASSWORD JWT_SECRET ANON_KEY SERVICE_ROLE_KEY \
    DASHBOARD_USERNAME DASHBOARD_PASSWORD SECRET_KEY_BASE VAULT_ENC_KEY \
    PG_META_CRYPTO_KEY SUPABASE_PUBLIC_URL API_EXTERNAL_URL POOLER_TENANT_ID; do
    [ -n "${!var:-}" ] || missing+=("$var")
done
# config/db/migrations/99-librarian.sql sets the librarian role's password from
# this, and runs only once: without it, the app could never log in.
if [ ! -s "$PGDATA/PG_VERSION" ] && [ -z "${LIBRARIAN_DB_PASSWORD:-}" ]; then
    missing+=(LIBRARIAN_DB_PASSWORD)
fi
if [ "${#missing[@]}" -gt 0 ]; then
    echo "Not set: ${missing[*]}. See .env.example." >&2
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
