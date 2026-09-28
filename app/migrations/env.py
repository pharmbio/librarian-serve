"""Alembic environment: migrates the database on the connection that
DbClient.migrate() hands over or, run from the command line, the one at
DATABASE_URL."""

import os

from alembic import context

from db_client import make_engine
from models import Base

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Print the SQL instead of running it (``alembic upgrade head --sql``)."""
    context.configure(
        url=make_engine(os.environ["DATABASE_URL"]).url.render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _migrate(connection) -> None:
    # Batch mode rebuilds a table to change it, since SQLite's ALTER TABLE
    # can't do most changes in place (the tests run on SQLite).
    context.configure(
        connection=connection, target_metadata=target_metadata, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _migrate(connection)
        return
    with make_engine(os.environ["DATABASE_URL"]).connect() as connection:
        _migrate(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
