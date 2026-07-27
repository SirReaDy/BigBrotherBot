"""Programmatic Alembic driver.

Lets the runtime and the CLI evolve a database without the ``alembic`` command line: it points an
Alembic ``Config`` at the packaged migration scripts and sets the URL from the b3 config.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def stamp(url: str, revision: str = "head") -> None:
    """Mark a database as being at ``revision`` without running migrations.

    Used after creating a fresh schema from the ORM metadata, so subsequent ``upgrade`` calls
    know the starting point.
    """
    command.stamp(alembic_config(url), revision)


def current_revision(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def head_revision(url: str) -> str | None:
    return ScriptDirectory.from_config(alembic_config(url)).get_current_head()
