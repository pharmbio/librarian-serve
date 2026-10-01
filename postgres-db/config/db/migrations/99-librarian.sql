-- The login and schema of Librarian's app (../../../../app). Runs once, as
-- supabase_admin, when the database is first created.
--
-- The tables live in their own schema, not in public: PostgREST serves public
-- to anyone holding the anon key, and the users table holds password hashes.
-- The app logs in as librarian, which can only reach this schema.
\set librarian_password `echo "$LIBRARIAN_DB_PASSWORD"`

create role librarian with login password :'librarian_password';
create schema librarian authorization librarian;
alter role librarian set search_path = librarian;

-- Studio's Table Editor and SQL editor connect as postgres.
grant librarian to postgres;
