"""Schema creation and seed data.

For Phase A we create the schema directly from the ORM metadata and seed the canonical groups.
Alembic migrations (initial = legacy-verbatim for imports, follow-up = FK/IPv6 widening) are a
Phase-A follow-up; this module is what a fresh install uses to get a working database.
"""

from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from b3.domain.permissions import DEFAULT_GROUPS
from b3.storage.models import Base, DataRow, GroupRow

SCHEMA_VERSION = "2.0.0"


def create_schema(engine: Engine, clock_epoch: int = 0) -> None:
    """Create all tables (if absent) and seed groups + the schema version stamp."""
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        _seed_groups(session, clock_epoch)
        _stamp_version(session)
        session.commit()


def _seed_groups(session: Session, clock_epoch: int) -> None:
    existing = session.scalar(select(GroupRow).limit(1))
    if existing is not None:
        return
    for g in DEFAULT_GROUPS:
        session.add(
            GroupRow(
                id=g.id,
                keyword=g.keyword,
                name=g.name,
                level=g.level,
                time_add=clock_epoch,
                time_edit=clock_epoch,
            )
        )


def _stamp_version(session: Session) -> None:
    row = session.get(DataRow, "schema_version")
    if row is None:
        session.add(DataRow(data_key="schema_version", data_value=SCHEMA_VERSION))
    else:
        row.data_value = SCHEMA_VERSION
