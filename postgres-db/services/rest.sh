#!/bin/bash
# rest: PostgREST (postgrest/postgrest), the REST API on the schemas in
# PGRST_DB_SCHEMAS.
source "$(dirname "$0")/lib.sh"
log_as rest
wait_for_db

export PGRST_DB_URI="postgres://authenticator:${POSTGRES_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
# Always with librarian, the schema of Librarian's app, which reaches its
# tables only through this API. Only the role librarian may use it.
case ",$PGRST_DB_SCHEMAS," in
    *,librarian,*) ;;
    *) PGRST_DB_SCHEMAS="$PGRST_DB_SCHEMAS,librarian" ;;
esac
export PGRST_DB_SCHEMAS
export PGRST_DB_MAX_ROWS=${PGRST_DB_MAX_ROWS:-1000}
export PGRST_DB_EXTRA_SEARCH_PATH=${PGRST_DB_EXTRA_SEARCH_PATH:-public}
export PGRST_DB_ANON_ROLE=anon

export PGRST_SERVER_HOST=127.0.0.1
export PGRST_SERVER_PORT=$REST_PORT
export PGRST_ADMIN_SERVER_HOST=127.0.0.1
export PGRST_ADMIN_SERVER_PORT=$REST_ADMIN_PORT

# A plain-text symmetric secret, a single JWK, or a JWKS.
export PGRST_JWT_SECRET=${JWT_JWKS:-$JWT_SECRET}

export PGRST_DB_USE_LEGACY_GUCS=false
export PGRST_APP_SETTINGS_JWT_EXP=$JWT_EXPIRY

exec postgrest
