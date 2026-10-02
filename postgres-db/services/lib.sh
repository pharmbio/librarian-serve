# Sourced by every service script. One script per service of Supabase's
# docker-compose.yml, setting the same environment, with every other service
# on 127.0.0.1 instead of its compose host name.
#
# The settings: postgres-db/.env, which the image includes as ~/.env, each line
# read literally. What docker run passes (-e, --env-file) wins over it.
load_env() {
    local line key
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in '' | '#'*) continue ;; esac
        key=${line%%=*}
        [ -n "${!key:-}" ] || export "$line"
    done < "$1"
}
load_env "$HOME/.env"

# Ports inside the container. Only the gateway (9876), Postgres (5432) and the
# pooler (5433, 6543) are for outside use; the others are reached through the
# gateway, as in the compose stack.
GATEWAY_PORT=9876
STUDIO_PORT=3000
REST_PORT=3001        # 3000 in compose, which Studio has here
REST_ADMIN_PORT=3002  # 3001 in compose
REALTIME_PORT=4000
POOLER_API_PORT=4001  # 4000 in compose, which realtime has here
STORAGE_PORT=5000
STORAGE_ADMIN_PORT=5002  # 5001 by default, which imgproxy has
IMGPROXY_PORT=5001
META_PORT=8080
FUNCTIONS_PORT=9000
AUTH_PORT=9999

DB_HOST=127.0.0.1
DB_PORT=${POSTGRES_PORT:-5432}
DB_NAME=${POSTGRES_DB:-postgres}

# Prefixes every line the service prints with its name, in `docker logs`. The
# prefixer ignores the stop signal, which supervisord sends to the service's
# whole process group, so that it outlives the service and passes on its last
# lines, instead of leaving it writing into a closed pipe.
log_as() {
    exec > >(trap '' TERM INT; exec sed -u "s/^/[$1] /") 2>&1
}

# Waits until Postgres accepts connections over TCP. During the first start
# it listens only on its socket while it creates the database, so this also
# waits for the init scripts to finish.
wait_for_db() {
    until pg_isready -q -h "$DB_HOST" -p "$DB_PORT" -U supabase_admin -d "$DB_NAME"; do
        sleep 1
    done
}
