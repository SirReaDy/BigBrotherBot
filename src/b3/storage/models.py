"""SQLAlchemy ORM models.

Replaces the legacy string-concatenating ``QueryBuilder`` (a live SQL-injection surface) with
parameterized, dialect-abstracted SQLAlchemy 2.0 mapped classes. Column *names* mirror the legacy
schema so old databases map 1:1 on import, with two deliberate modernizations:

* real ``FOREIGN KEY`` constraints (legacy MySQL was MyISAM with none), and
* IP columns widened to 45 chars so IPv6 fits (legacy was ``VARCHAR(16)`` — IPv4 only).

Timestamps stay Unix epoch integers for data compatibility.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ClientRow(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    guid: Mapped[str] = mapped_column(String(36), unique=True, index=True, default="")
    pbid: Mapped[str] = mapped_column(String(32), default="")
    ip: Mapped[str] = mapped_column(String(45), default="")  # widened for IPv6
    name: Mapped[str] = mapped_column(String(32), index=True, default="")
    connections: Mapped[int] = mapped_column(default=0)
    group_bits: Mapped[int] = mapped_column(index=True, default=0)
    mask_level: Mapped[int] = mapped_column(default=0)
    auto_login: Mapped[int] = mapped_column(default=0)
    greeting: Mapped[str] = mapped_column(String(128), default="")
    password: Mapped[str | None] = mapped_column(String(32), default=None)
    login: Mapped[str | None] = mapped_column(String(255), default=None)
    time_add: Mapped[int] = mapped_column(default=0)
    time_edit: Mapped[int] = mapped_column(default=0)


class GroupRow(Base):
    __tablename__ = "groups"

    # NOTE: id is a membership BIT (power of two), NOT autoincrement. See domain.permissions.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(32), default="")
    keyword: Mapped[str] = mapped_column(String(32), unique=True)
    level: Mapped[int] = mapped_column(default=0)
    time_add: Mapped[int] = mapped_column(default=0)
    time_edit: Mapped[int] = mapped_column(default=0)


class PenaltyRow(Base):
    __tablename__ = "penalties"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(16), index=True, default="Ban")
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True, default=None)
    duration: Mapped[int] = mapped_column(default=0)  # minutes
    inactive: Mapped[int] = mapped_column(index=True, default=0)  # soft-delete flag
    keyword: Mapped[str] = mapped_column(String(16), index=True, default="")
    reason: Mapped[str] = mapped_column(String(255), default="")
    data: Mapped[str] = mapped_column(String(255), default="")
    # Which game server issued this penalty (config `bot.server_id`). Indexed because the only
    # reason to store it is to filter or group by it. "" = not stated, which is what every row
    # written before this column existed, and every imported legacy row, reads as.
    server_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    time_add: Mapped[int] = mapped_column(index=True, default=0)
    time_edit: Mapped[int] = mapped_column(default=0)
    time_expire: Mapped[int] = mapped_column(index=True, default=-1)  # -1 == never


class AliasRow(Base):
    __tablename__ = "aliases"
    __table_args__ = (UniqueConstraint("alias", "client_id", name="uq_alias_client"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    alias: Mapped[str] = mapped_column(String(32))
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    num_used: Mapped[int] = mapped_column(default=1)
    time_add: Mapped[int] = mapped_column(default=0)
    time_edit: Mapped[int] = mapped_column(default=0)


class IpAliasRow(Base):
    __tablename__ = "ipaliases"
    __table_args__ = (UniqueConstraint("ip", "client_id", name="uq_ipalias_client"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    ip: Mapped[str] = mapped_column(String(45))  # widened for IPv6
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    num_used: Mapped[int] = mapped_column(default=1)
    time_add: Mapped[int] = mapped_column(default=0)
    time_edit: Mapped[int] = mapped_column(default=0)


class DataRow(Base):
    """Generic key/value bot metadata (schema version, misc state)."""

    __tablename__ = "data"

    data_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    data_value: Mapped[str] = mapped_column(String(255), default="")
