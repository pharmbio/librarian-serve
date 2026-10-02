#!/bin/bash
# The container is healthy when all 11 services are: each one checked the way
# its healthcheck in docker-compose.yml did. Prints the ones that aren't.
#   docker exec supabase ./healthcheck.sh
source "$(dirname "$0")/services/lib.sh"

http() { curl -fsS -o /dev/null --max-time 3 "$@"; }
tcp() { timeout 1 bash -c "</dev/tcp/127.0.0.1/$1" 2>/dev/null; }

failed=()
pg_isready -q -h "$DB_HOST" -p "$DB_PORT" -U supabase_admin -d "$DB_NAME" || failed+=(db)
http "http://127.0.0.1:$AUTH_PORT/health" || failed+=(auth)
http "http://127.0.0.1:$REST_ADMIN_PORT/ready" || failed+=(rest)
http -H "Authorization: Bearer $ANON_KEY" "http://127.0.0.1:$REALTIME_PORT/api/tenants/realtime-dev/health" || failed+=(realtime)
http "http://127.0.0.1:$STORAGE_PORT/status" || failed+=(storage)
http "http://127.0.0.1:$IMGPROXY_PORT/health" || failed+=(imgproxy)
http "http://127.0.0.1:$META_PORT/health" || failed+=(meta)
tcp "$FUNCTIONS_PORT" || failed+=(functions)
http "http://127.0.0.1:$POOLER_API_PORT/api/health" || failed+=(pooler)
http "http://127.0.0.1:$STUDIO_PORT/api/platform/profile" || failed+=(studio)
tcp "$GATEWAY_PORT" || failed+=(gateway)
# And the app's schema has its latest migration (services/schema.sh).
latest=$(basename "$(ls /etc/librarian/migrations/*.sql | tail -1)" .sql)
[ "$(psql -X -tA -h /var/run/postgresql -U supabase_admin -d "$DB_NAME" \
    -c "select 1 from librarian.schema_migrations where version = '$latest'" 2>/dev/null)" = 1 ] \
    || failed+=(schema)

if [ "${#failed[@]}" -gt 0 ]; then
    echo "Not ready: ${failed[*]}"
    exit 1
fi
echo "All 11 services are up."
