"""Alembic environment.

Uses the ORM metadata as the migration target so autogenerate stays in sync with the models. The
database URL is taken from (in order): a URL set programmatically on the Alembic config, the
``B3_DATABASE_URL`` environment variable, or the ``sqlalchemy.url`` in alembic.ini.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from b3.storage.models import Base

config = context.config

# Resolve the database URL (programmatic override > env var > ini).
_url = config.get_main_option("sqlalchemy.url")
if os.environ.get("B3_DATABASE_URL"):
    _url = os.environ["B3_DATABASE_URL"]
    config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,  # sqlite-friendly ALTERs
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("no sqlalchemy.url configured for the migration run")
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
