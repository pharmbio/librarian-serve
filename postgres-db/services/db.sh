#!/bin/bash
# db: Supabase's Postgres (supabase/postgres). The image's entrypoint creates
# the database on the first start, in an empty PGDATA, and runs
# /docker-entrypoint-initdb.d once: Supabase's schema, then config/db/.
source "$(dirname "$0")/lib.sh"
log_as db

export POSTGRES_HOST=/var/run/postgresql
export PGPORT=$DB_PORT POSTGRES_PORT=$DB_PORT
export PGPASSWORD=$POSTGRES_PASSWORD
export PGDATABASE=$DB_NAME POSTGRES_DB=$DB_NAME
export JWT_EXP=$JWT_EXPIRY

# From the supabase/postgres image.
export POSTGRES_USER=supabase_admin
export POSTGRES_INITDB_ARGS="--allow-group-access --locale-provider=icu --encoding=UTF-8 --icu-locale=en_US.UTF-8"
export LANG=en_US.UTF-8 LANGUAGE=en_US:en LC_ALL=en_US.UTF-8
export LOCALE_ARCHIVE=/nix/var/nix/profiles/default/lib/locale/locale-archive
export GRN_PLUGINS_DIR=/usr/lib/groonga/plugins

# A container that was killed leaves its postmaster.pid behind. In its own
# container Postgres would find its old PID free again, but here another
# service may have that PID, and Postgres would refuse to start. The file is
# stale unless that PID is a running postgres.
pidfile=$PGDATA/postmaster.pid
if [ -f "$pidfile" ] && [ "$(cat "/proc/$(head -1 "$pidfile")/comm" 2>/dev/null)" != postgres ]; then
    echo "Removing the stale $pidfile"
    rm -f "$pidfile"
fi

# log_min_messages=fatal keeps realtime's polling queries out of the logs.
exec docker-entrypoint.sh postgres \
    -c config_file=/etc/postgresql/postgresql.conf \
    -c data_directory="$PGDATA" \
    -c log_min_messages=fatal
