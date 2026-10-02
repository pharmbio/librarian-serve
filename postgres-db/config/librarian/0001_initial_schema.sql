-- The tables of Librarian's app (../../../app): users, sessions, queries and
-- runs. services/schema.sh applies this once, as the role librarian, in the
-- schema librarian.
--
-- The same tables as the app's former Alembic migration 0001, which a
-- database it set up already has: hence "if not exists". The app supplies
-- every value, ids and times included. Times are naive UTC.

create table if not exists users (
    id varchar(16) primary key,
    -- Stored lowercased by the app, so a plain unique index is case-insensitive.
    email varchar(254) not null unique,
    password_hash text not null,
    institution varchar(200) not null,
    position varchar(200) not null,
    created_at timestamp not null,
    last_login_at timestamp
);

create table if not exists sessions (
    -- sha256 of the cookie token: the token itself never leaves the app.
    token_hash varchar(64) primary key,
    user_id varchar(16) not null references users (id) on delete cascade,
    created_at timestamp not null,
    expires_at timestamp not null
);
create index if not exists ix_sessions_user_id on sessions (user_id);
create index if not exists ix_sessions_expires_at on sessions (expires_at);

create table if not exists queries (
    id varchar(16) primary key,
    user_id varchar(16) not null references users (id) on delete cascade,
    text text not null,
    full_text_enrichment boolean not null,
    created_at timestamp not null
);
create index if not exists queries_by_user on queries (user_id, created_at);

create table if not exists runs (
    id varchar(16) primary key,
    query_id varchar(16) not null references queries (id) on delete cascade,
    status varchar(16) not null
        constraint runs_status check (status in ('pending', 'running', 'completed', 'failed')),
    answer text,
    evidence json,
    paper_count integer,
    duration_s double precision,
    error text,
    metadata json not null,
    created_at timestamp not null,
    updated_at timestamp not null,
    started_at timestamp,
    finished_at timestamp
);
create index if not exists runs_by_query on runs (query_id, created_at);
create index if not exists runs_by_status on runs (status, created_at);
