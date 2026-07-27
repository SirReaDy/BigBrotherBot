"""SQLAlchemy-backed implementation of the Storage protocol.

Maps between ORM rows (``models.py``) and the pure domain objects (``domain/``). All access goes
through short-lived Sessions; there is no process-wide lock and no hand-rolled reconnect throttle
(SQLAlchemy's connection pool with ``pool_pre_ping`` handles that when we move beyond sqlite).
"""

from __future__ import annotations

import logging

from sqlalchemy import create_engine, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from b3.core.clock import Clock, SystemClock
from b3.domain.client import Alias, Client, IpAlias, Penalty, PenaltyType
from b3.domain.permissions import DEFAULT_GROUPS, Group
from b3.storage.models import AliasRow, ClientRow, GroupRow, IpAliasRow, PenaltyRow
from b3.storage.schema import create_schema

log = logging.getLogger(__name__)

# Superadmin membership bit — a client is a superadmin if this bit is set in group_bits.
SUPERADMIN_BIT = 128


class SqlAlchemyStorage:
    """Storage on top of a SQLAlchemy engine. Satisfies :class:`b3.storage.base.Storage`."""

    def __init__(self, url: str, *, clock: Clock | None = None, echo: bool = False) -> None:
        self._url = url
        self._clock = clock or SystemClock()
        self._engine = create_engine(url, echo=echo, future=True)
        self._session_factory: sessionmaker[Session] = sessionmaker(bind=self._engine, future=True)

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> None:
        """Ensure the schema exists (create + seed on first run) and is Alembic-aware."""
        create_schema(self._engine, clock_epoch=self._clock.epoch())
        self._ensure_alembic_baseline()

    def _ensure_alembic_baseline(self) -> None:
        """Stamp the current Alembic head on a schema created from ORM metadata.

        Without this, a schema built by ``create_all`` would look 'unmigrated', and a later
        ``b3 db upgrade`` would try to recreate existing tables. Stamping records that the DB is
        already at head, so migrations pick up cleanly from here on.
        """
        try:
            from b3.storage import migrate

            if migrate.current_revision(self._url) is None:
                migrate.stamp(self._url, "head")
        except Exception:  # noqa: BLE001 - never let migration bookkeeping break startup
            log.debug("could not stamp alembic baseline for %s", self._url, exc_info=True)

    def close(self) -> None:
        self._engine.dispose()

    @property
    def engine(self) -> Engine:
        return self._engine

    def session(self) -> Session:
        """Open a new Session (used by the legacy importer for bulk work)."""
        return self._session_factory()

    # -- clients -----------------------------------------------------------

    def get_client_by_id(self, client_id: int) -> Client | None:
        with self._session_factory() as s:
            row = s.get(ClientRow, client_id)
            return _client_from_row(row) if row else None

    def get_client_by_guid(self, guid: str) -> Client | None:
        with self._session_factory() as s:
            row = s.scalar(select(ClientRow).where(ClientRow.guid == guid))
            return _client_from_row(row) if row else None

    def save_client(self, client: Client) -> Client:
        now = self._clock.epoch()
        with self._session_factory() as s:
            row: ClientRow | None = None
            if client.id is not None:
                row = s.get(ClientRow, client.id)
            if row is None and client.guid:
                row = s.scalar(select(ClientRow).where(ClientRow.guid == client.guid))
            if row is None:
                row = ClientRow(time_add=now)
                s.add(row)
            _apply_client_to_row(client, row)
            row.time_edit = now
            s.commit()
            client.id = row.id
            client.time_add = row.time_add
            client.time_edit = row.time_edit
            return client

    def count_clients(self) -> int:
        with self._session_factory() as s:
            return int(s.scalar(select(func.count()).select_from(ClientRow)) or 0)

    def search_clients(self, term: str, limit: int = 5) -> list[Client]:
        """Find stored clients by current name *or* any recorded alias.

        The legacy equivalent (``getClientsMatching``) matched the clients table only and returned
        the 5 most recently seen; we also search alias history, because the whole point of recording
        aliases is being able to find someone by a name they used to use.
        """
        if not term:
            return []
        pattern = f"%{term}%"
        with self._session_factory() as s:
            by_alias = select(AliasRow.client_id).where(AliasRow.alias.ilike(pattern))
            q = (
                select(ClientRow)
                .where(or_(ClientRow.name.ilike(pattern), ClientRow.id.in_(by_alias)))
                .order_by(ClientRow.time_edit.desc(), ClientRow.id.desc())
                .limit(limit)
            )
            return [_client_from_row(r) for r in s.scalars(q).all()]

    # -- groups ------------------------------------------------------------

    def get_groups(self) -> list[Group]:
        with self._session_factory() as s:
            rows = s.scalars(select(GroupRow).order_by(GroupRow.level.desc())).all()
            if not rows:
                return list(DEFAULT_GROUPS)
            return [Group(id=r.id, keyword=r.keyword, name=r.name, level=r.level) for r in rows]

    def has_superadmin(self) -> bool:
        with self._session_factory() as s:
            row = s.scalar(
                select(ClientRow).where(ClientRow.group_bits.op("&")(SUPERADMIN_BIT) != 0).limit(1)
            )
            return row is not None

    # -- penalties ---------------------------------------------------------

    def add_penalty(self, penalty: Penalty) -> Penalty:
        now = self._clock.epoch()
        with self._session_factory() as s:
            row = PenaltyRow(
                type=penalty.type.value,
                client_id=penalty.client_id,
                admin_id=penalty.admin_id,
                duration=penalty.duration,
                inactive=int(penalty.inactive),
                keyword=penalty.keyword,
                reason=penalty.reason,
                data=penalty.data,
                server_id=penalty.server_id,
                time_add=penalty.time_add or now,
                time_edit=now,
                time_expire=penalty.time_expire,
            )
            s.add(row)
            s.commit()
            penalty.id = row.id
            penalty.time_add = row.time_add
            return penalty

    def get_active_penalties(
        self, client_id: int, type_: PenaltyType | None = None
    ) -> list[Penalty]:
        now = self._clock.epoch()
        with self._session_factory() as s:
            q = select(PenaltyRow).where(
                PenaltyRow.client_id == client_id,
                PenaltyRow.inactive == 0,
                (PenaltyRow.time_expire == -1) | (PenaltyRow.time_expire > now),
            )
            if type_ is not None:
                q = q.where(PenaltyRow.type == type_.value)
            # Most recent first: callers take [0] as "the ban in force", matching the legacy
            # getClientLastPenalty(..., 'time_add DESC', 1).
            q = q.order_by(PenaltyRow.time_add.desc(), PenaltyRow.id.desc())
            return [_penalty_from_row(r) for r in s.scalars(q).all()]

    def disable_penalty(self, penalty_id: int | None) -> bool:
        """Lift a single penalty — `!warnremove` takes the latest one off a player."""
        if penalty_id is None:
            return False
        with self._session_factory() as s:
            row = s.get(PenaltyRow, penalty_id)
            if row is None or row.inactive:
                return False
            row.inactive = 1
            row.time_edit = self._clock.epoch()
            s.commit()
            return True

    def get_recent_penalties(
        self,
        types: tuple[PenaltyType, ...] | None = None,
        limit: int = 5,
        server_id: str | None = None,
    ) -> list[Penalty]:
        """The newest penalties still in force, across all clients (the legacy
        ``getLastPenalties``). Expired and lifted ones are excluded — `!lastbans` is about what is
        being enforced right now, not a history of everything ever issued.

        ``server_id`` narrows the answer to one game server, for operators sharing a database. It is
        opt-in on purpose: `!lastbans` shows every server's, because a shared ban list is shared."""
        now = self._clock.epoch()
        with self._session_factory() as s:
            q = select(PenaltyRow).where(
                PenaltyRow.inactive == 0,
                (PenaltyRow.time_expire == -1) | (PenaltyRow.time_expire > now),
            )
            if types:
                q = q.where(PenaltyRow.type.in_([t.value for t in types]))
            if server_id is not None:
                q = q.where(PenaltyRow.server_id == server_id)
            q = q.order_by(PenaltyRow.time_add.desc(), PenaltyRow.id.desc()).limit(limit)
            return [_penalty_from_row(r) for r in s.scalars(q).all()]

    def disable_penalties(self, client_id: int, type_: PenaltyType | None = None) -> int:
        now = self._clock.epoch()
        with self._session_factory() as s:
            q = select(PenaltyRow).where(
                PenaltyRow.client_id == client_id, PenaltyRow.inactive == 0
            )
            if type_ is not None:
                q = q.where(PenaltyRow.type == type_.value)
            rows = s.scalars(q).all()
            for r in rows:
                r.inactive = 1
                r.time_edit = now
            s.commit()
            return len(rows)

    # -- aliases -----------------------------------------------------------

    def add_alias(self, alias: Alias) -> Alias:
        now = self._clock.epoch()
        with self._session_factory() as s:
            existing = s.scalar(
                select(AliasRow).where(
                    AliasRow.alias == alias.value, AliasRow.client_id == alias.client_id
                )
            )
            if existing:
                existing.num_used += 1
                existing.time_edit = now
                s.commit()
                return _alias_from_row(existing)
            row = AliasRow(
                alias=alias.value,
                client_id=alias.client_id,
                num_used=alias.num_used,
                time_add=now,
                time_edit=now,
            )
            s.add(row)
            s.commit()
            alias.id = row.id
            return alias

    def get_aliases(self, client_id: int) -> list[Alias]:
        with self._session_factory() as s:
            rows = s.scalars(select(AliasRow).where(AliasRow.client_id == client_id)).all()
            return [_alias_from_row(r) for r in rows]

    def add_ip_alias(self, ip_alias: IpAlias) -> IpAlias:
        now = self._clock.epoch()
        with self._session_factory() as s:
            existing = s.scalar(
                select(IpAliasRow).where(
                    IpAliasRow.ip == ip_alias.value, IpAliasRow.client_id == ip_alias.client_id
                )
            )
            if existing:
                existing.num_used += 1
                existing.time_edit = now
                s.commit()
                return _ip_alias_from_row(existing)
            row = IpAliasRow(
                ip=ip_alias.value,
                client_id=ip_alias.client_id,
                num_used=ip_alias.num_used,
                time_add=now,
                time_edit=now,
            )
            s.add(row)
            s.commit()
            ip_alias.id = row.id
            return ip_alias

    def get_ip_aliases(self, client_id: int) -> list[IpAlias]:
        with self._session_factory() as s:
            rows = s.scalars(select(IpAliasRow).where(IpAliasRow.client_id == client_id)).all()
            return [_ip_alias_from_row(r) for r in rows]


# --- row <-> domain mapping helpers --------------------------------------


def _client_from_row(row: ClientRow) -> Client:
    return Client(
        id=row.id,
        guid=row.guid,
        pbid=row.pbid,
        ip=row.ip,
        name=row.name,
        connections=row.connections,
        group_bits=row.group_bits,
        mask_level=row.mask_level,
        auto_login=bool(row.auto_login),
        greeting=row.greeting,
        password=row.password,
        login=row.login,
        time_add=row.time_add,
        time_edit=row.time_edit,
    )


def _apply_client_to_row(client: Client, row: ClientRow) -> None:
    row.guid = client.guid
    row.pbid = client.pbid
    row.ip = client.ip
    row.name = client.name
    row.connections = client.connections
    row.group_bits = client.group_bits
    row.mask_level = client.mask_level
    row.auto_login = int(client.auto_login)
    row.greeting = client.greeting
    row.password = client.password
    row.login = client.login


def _penalty_from_row(row: PenaltyRow) -> Penalty:
    return Penalty(
        id=row.id,
        type=PenaltyType(row.type),
        client_id=row.client_id,
        admin_id=row.admin_id,
        duration=row.duration,
        inactive=bool(row.inactive),
        keyword=row.keyword,
        reason=row.reason,
        data=row.data,
        server_id=row.server_id,
        time_add=row.time_add,
        time_edit=row.time_edit,
        time_expire=row.time_expire,
    )


def _alias_from_row(row: AliasRow) -> Alias:
    return Alias(
        id=row.id,
        value=row.alias,
        client_id=row.client_id,
        num_used=row.num_used,
        time_add=row.time_add,
        time_edit=row.time_edit,
    )


def _ip_alias_from_row(row: IpAliasRow) -> IpAlias:
    return IpAlias(
        id=row.id,
        value=row.ip,
        client_id=row.client_id,
        num_used=row.num_used,
        time_add=row.time_add,
        time_edit=row.time_edit,
    )
