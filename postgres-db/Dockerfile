# The database service: the whole self-hosted Supabase stack of
# docker-compose.yml (Postgres, Studio, the API gateway and the 8 services
# behind it) in one container, as Serve requires. Each service's program is
# copied from its official image, at the version docker-compose.yml pins, and
# supervisord runs them all (supervisord.conf, services/).
#
# Everything that must persist lives in DATA_DIR, the folder to mount
# persistent storage on: Postgres's files are in PGDATA.
#
# Build from this folder:  docker build -t librarian-supabase .
FROM supabase/postgres:17.6.1.136 AS db
FROM supabase/studio:2026.09.07-sha-7996410 AS studio
FROM envoyproxy/envoy:v1.39.1 AS gateway
FROM supabase/gotrue:v2.196.0 AS auth
FROM postgrest/postgrest:v14.17 AS rest
FROM supabase/realtime:v2.134.10 AS realtime
FROM supabase/storage-api:v1.74.0 AS storage
FROM darthsim/imgproxy:v3.31.4 AS imgproxy
FROM supabase/postgres-meta:v0.99.0 AS meta
FROM supabase/edge-runtime:v1.76.2 AS functions
FROM supabase/supavisor:2.9.12 AS pooler

# The storage image is built on Alpine: rebuild its one native module (not a
# prebuilt one) for glibc, with the same Node version.
FROM node:24.20.0-trixie AS storage-glibc
COPY --from=storage /app /app
RUN cd /app && npm rebuild fs-xattr

FROM debian:trixie-slim

# supervisord, the tools the scripts use, and the shared libraries PostgREST
# and realtime link against. The rest bring their own.
RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor curl ca-certificates openssl procps locales \
        libpq5 libgmp10 libstdc++6 libtinfo6 \
    && rm -rf /var/lib/apt/lists/* \
    && echo 'en_US.UTF-8 UTF-8' > /etc/locale.gen && locale-gen

ENV USER=supabase
ENV HOME=/home/$USER
# The folder to mount persistent storage on. Postgres keeps its files in a
# subfolder, because it must own its data folder with mode 0750 at most, and a
# mount point may belong to someone else.
ENV DATA_DIR=$HOME/data
ENV PGDATA=$DATA_DIR/pgdata
ENV PATH=$PATH:/nix/var/nix/profiles/default/bin:/usr/lib/postgresql/bin

# Serve runs containers as user id 1000. ~/.cache is where the functions
# runtime caches its imports.
RUN useradd -m -u 1000 $USER \
    && install -d -o $USER -g $USER -m 0700 $DATA_DIR \
    && install -d -o $USER -g $USER $HOME/.cache

# db: Postgres and its extensions are built with Nix, and bring their own libc
# under /nix. The entrypoint, config and init scripts come along.
COPY --from=db /nix /nix
COPY --from=db /usr/lib/postgresql /usr/lib/postgresql
COPY --from=db /usr/lib/groonga /usr/lib/groonga
COPY --from=db /usr/local/bin/docker-entrypoint.sh /usr/local/bin/
COPY --from=db /etc/postgresql /etc/postgresql
COPY --from=db /docker-entrypoint-initdb.d /docker-entrypoint-initdb.d
# start-script.sh copies this into DATA_DIR on the first start, as the
# compose stack's db-config volume did: it also receives pgsodium's root key.
COPY --from=db /etc/postgresql-custom /etc/postgresql-custom.dist
COPY config/db/ /docker-entrypoint-initdb.d/
# The socket folder, and peer logins from the OS user supabase as postgres
# (the image's init scripts connect that way).
RUN ln -s $DATA_DIR/postgresql-custom /etc/postgresql-custom \
    && install -d -o $USER -g $USER /var/run/postgresql \
    && echo "supabase_map  $USER  postgres" >> /etc/postgresql/pg_ident.conf

# studio and meta share Node 22, storage has Node 24.
COPY --from=studio /usr/local/bin/node /opt/node22/bin/node
COPY --from=studio --chown=$USER:$USER /app /opt/studio
COPY --from=meta --chown=$USER:$USER /usr/src/app /opt/meta
COPY --from=storage-glibc /usr/local/bin/node /opt/node24/bin/node
COPY --from=storage-glibc --chown=$USER:$USER /app /opt/storage

COPY --from=auth /usr/local/bin/auth /usr/local/bin/
COPY --from=auth /usr/local/etc/auth /usr/local/etc/auth
# /bin/postgrest in both builds: static on amd64, Ubuntu's /usr/bin on arm64.
COPY --from=rest /bin/postgrest /usr/local/bin/
COPY --from=realtime --chown=$USER:$USER /app /opt/realtime
COPY --from=pooler --chown=$USER:$USER /app /opt/supavisor
COPY --from=pooler /usr/lib/*-linux-gnu/libssl.so.1.1 /usr/lib/*-linux-gnu/libcrypto.so.1.1 /opt/supavisor/syslib/
COPY --from=imgproxy /usr/local/bin/imgproxy /usr/local/bin/
COPY --from=imgproxy /opt/imgproxy /opt/imgproxy
COPY --from=imgproxy /etc/fonts /etc/fonts
COPY --from=imgproxy /usr/share/fonts /usr/share/fonts
COPY --from=functions /usr/local/bin/edge-runtime /usr/local/bin/
COPY --from=gateway /usr/local/bin/envoy /usr/local/bin/

# The gateway writes its listener config next to the template at start.
COPY --chown=$USER:$USER config/envoy/ /etc/envoy/
COPY config/pooler.exs /etc/pooler/pooler.exs
# functions/main/index.ts serves every function from this folder, as in compose.
COPY functions/ /home/deno/functions/

WORKDIR $HOME
COPY --chmod=0755 services/ services/
COPY --chmod=0755 start-script.sh healthcheck.sh ./
COPY supervisord.conf ./

USER $USER

# 8765: the gateway (Studio and the APIs). 5432: Postgres. 5433 and 6543: the
# pooler, in session and transaction mode.
EXPOSE 8765 5432 5433 6543
HEALTHCHECK --interval=10s --timeout=10s --start-period=120s --retries=3 \
    CMD ["./healthcheck.sh"]
# Serve starts the container with the start-up script in WORKDIR.
CMD ["./start-script.sh"]
