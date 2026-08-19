"""Reserves a nickname for the player it belongs to.

A port of the classic `nickreg` plugin. Impersonating an admin is the cheapest trick on a game server —
type their name, tell somebody the server is being restarted, and see what happens — so a player can
register the names that are theirs, and anybody else using one is warned to change it. Registrations
outlive a session, because the whole point is what happens when the owner is not there.

Kept from the classic: the three commands (`!registernick`, `!deletenick`, `!listnick`), the cap on how
many names one player may hold, the periodic sweep as well as the check on a name change, the grace
period after a map change, and superadmins being exempt.

Changed, and the first two are faults rather than preferences:

* **A name is compared, not pattern-matched.** The classic asked `SELECT * FROM nicks WHERE name LIKE
  '<the player's own name>'`, with the name pasted into the SQL and only single quotes escaped. Two
  things follow, and both are real: `%` and `_` in a player's name are **wildcards** in a `LIKE`, so a
  player calling themselves `%` matched every registered nickname and was warned for stealing all of
  them — and anything the hand-rolled quote escape missed was SQL. Names are compared for equality
  here, through the ORM, with no SQL in this plugin at all.
* **The sweep actually re-checks.** `execute_crontab` had an unconditional `continue` inside the branch
  that reads a player's last check time, so once anybody had been checked *once* they were skipped for
  the rest of the session. What looked like a check every thirty seconds was a check on arrival and
  never again — the log even printed "last check run Xs ago" on the way past.
* **No thread per name change.** The classic started a raw thread to run one query.

Names are normalised the way players see them: colour codes stripped, trimmed, lower-cased. `^1Adm^7in`
and `admin` are the same nickname to everybody in the server, so they are the same nickname here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, strip_colors
from b3.domain.client import Client

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

#: How long a nickname warning lives. The classic's figure: long enough to escalate if they keep it,
#: short enough that somebody who changed their name is not carrying it for a week.
WARNING_DURATION = 10

DEFAULTS: dict[str, object] = {
    # Level needed to register a nickname at all. `mod` is the classic's default and the point of the
    # plugin: the names worth protecting are the ones players recognise as staff.
    "min_level": 20,
    # Level needed to list or delete *somebody else's* registrations.
    "manage_level": 100,
    # How many names one player may hold.
    "max_nicks": 3,
    # Seconds between sweeps of the connected players, and the grace period after a map change.
    "interval": 30,
}

MESSAGES = {
    "nick_registered": "{name} is now reserved for you",
    "nick_already": "{name} is already registered",
    "nick_too_many": "you already have {count} registered nicknames",
    "nick_deleted": "deleted the registration for {name}",
    "nick_unknown": "no registered nickname called {name}",
    "nick_none": "{name} has no registered nicknames",
    "nick_list": "{name} has registered: {nicks}",
    "nick_not_yours": "you cannot manage {name}'s registered nicknames",
    "nick_stolen": "that nickname is registered to somebody else — please change it",
}


class Base(DeclarativeBase):
    """This plugin's own table. The core's schema is Alembic-managed and is not touched here."""


class RegisteredNick(Base):
    """One reserved nickname, and who it belongs to.

    ``name`` is the normalised form (colour codes stripped, trimmed, lower-cased) and is unique, which
    is what makes "is this taken?" a lookup rather than a scan. ``display`` keeps what the owner
    actually typed, so `!listnick` can show `^1Adm^7in` rather than the flattened version.
    """

    __tablename__ = "nickreg_nicks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_id: Mapped[int] = mapped_column(Integer, index=True)
    display: Mapped[str] = mapped_column(String(64), default="")


def normalise(name: str) -> str:
    """A nickname reduced to what a comparison should care about."""
    return strip_colors(name).strip().lower()


class NickregPlugin(Plugin):
    """Reserves nicknames, and warns whoever else is wearing one."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self._engine: Engine | None = None
        #: Nothing is checked until this time. Set after a map change, because a player still loading
        #: has whatever name the engine gives them and is not stealing anything.
        self._quiet_until = 0.0

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        for name, key in (
            ("registernick", "min_level"),
            ("deletenick", "min_level"),
            ("listnick", "min_level"),
        ):
            registered = self.console.command_registry.get(name)
            if registered is not None:
                registered.min_level = as_int(self.settings.get(key), 20)

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self._engine = self._open_storage()
        self.subscribe(EventType.CLIENT_NAME_CHANGE, self.on_name_change)
        self.subscribe(EventType.CLIENT_AUTH, self.on_name_change)
        self.subscribe(EventType.GAME_MAP_CHANGE, self.on_map_change)
        interval = max(5, as_int(self.settings.get("interval"), 30))
        # The sweep catches what the events cannot: a player who connected while the plugin was
        # disabled, or on an engine that does not report a name change at all.
        self.schedule(self.sweep, second=f"*/{interval}", name="NickregPlugin.sweep")
        self.on_load_config()

    def _open_storage(self) -> Engine | None:
        try:
            engine = self.storage_engine()
        except RuntimeError as exc:
            log.warning("nickreg: %s; no nickname can be registered", exc)
            return None
        try:
            Base.metadata.create_all(engine)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop the bot over its own table
            log.error("nickreg: could not create its table (%s)", exc)
            return None
        return engine  # type: ignore[return-value]

    # -- the register --------------------------------------------------------

    def owner_of(self, name: str) -> RegisteredNick | None:
        """The registration for a name, or None. An equality lookup, never a pattern match."""
        if self._engine is None:
            return None
        with Session(self._engine) as session:
            return session.get(RegisteredNick, normalise(name))

    def nicks_of(self, client_id: int) -> list[RegisteredNick]:
        if self._engine is None:
            return []
        with Session(self._engine) as session:
            return list(
                session.scalars(
                    select(RegisteredNick).where(RegisteredNick.client_id == client_id)
                ).all()
            )

    def register(self, client: Client, name: str) -> None:
        if self._engine is None or client.id is None:
            return
        with Session(self._engine) as session:
            session.add(
                RegisteredNick(name=normalise(name), client_id=client.id, display=name.strip())
            )
            session.commit()

    def forget(self, name: str) -> bool:
        if self._engine is None:
            return False
        with Session(self._engine) as session:
            row = session.get(RegisteredNick, normalise(name))
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def count_for(self, client_id: int) -> int:
        if self._engine is None:
            return 0
        with Session(self._engine) as session:
            total = session.scalar(
                select(func.count())
                .select_from(RegisteredNick)
                .where(RegisteredNick.client_id == client_id)
            )
            return int(total or 0)

    # -- checking ------------------------------------------------------------

    def on_map_change(self, event: Event) -> None:
        """Say nothing for a while: a player loading a map has whatever name the engine gave them."""
        self._quiet_until = self.console.clock.now() + as_int(self.settings.get("interval"), 30)

    def on_name_change(self, event: Event) -> None:
        client = event.client
        if client is not None:
            self.check(client)

    def sweep(self) -> None:
        """Check everybody, for the arrivals no event covered.

        This is where the classic's real bug was: its equivalent had an unconditional `continue` inside
        the branch that reads a player's last-checked time, so anybody checked once was skipped for the
        rest of their session. The cooldown here does what that one was written to do.
        """
        if self.console.clock.now() < self._quiet_until:
            return
        for client in list(self.console.clients.connected()):
            self.check(client)

    def check(self, client: Client) -> bool:
        """Warn this player if their name belongs to somebody else. True if warned.

        Rate-limited per player, because a name change is an event a player can send as fast as they
        like, and each one would otherwise be a database lookup and possibly a warning.
        """
        if client.id is None or client.is_bot or self._engine is None:
            return False
        if client.max_level() >= 100:
            return False  # a superadmin is not impersonating anybody
        now = self.console.clock.now()
        interval = as_int(self.settings.get("interval"), 30)
        last = float(client.get_var(self, "checked_at", 0.0) or 0.0)
        if last and now - last < interval:
            return False
        client.set_var(self, "checked_at", now)
        found = self.owner_of(client.name)
        if found is None or found.client_id == client.id:
            return False
        log.info(
            "nickreg: %s is using %r, which is registered to @%d",
            client.name,
            client.name,
            found.client_id,
        )
        self.console.warn(client, reason=self.message("nick_stolen"), minutes=WARNING_DURATION)
        return True

    # -- commands ------------------------------------------------------------

    @command(level=20, alias="regnick")
    def cmd_registernick(self, ctx: CommandContext) -> None:
        """registernick - reserve the name you are using now"""
        client = ctx.client
        if client.id is None or self._engine is None:
            return
        name = client.name.strip()
        existing = self.owner_of(name)
        if existing is not None:
            ctx.reply(self.message("nick_already", name=name))
            return
        limit = as_int(self.settings.get("max_nicks"), 3)
        if self.count_for(client.id) >= limit:
            ctx.reply(self.message("nick_too_many", count=limit))
            return
        self.register(client, name)
        log.info("nickreg: %s registered %r", client.name, name)
        ctx.reply(self.message("nick_registered", name=name))

    @command(level=20, alias="delnick")
    def cmd_deletenick(self, ctx: CommandContext) -> None:
        """deletenick <nickname> - drop a registration (yours, or anybody's if you may manage them)"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("usage", usage="deletenick <nickname>"))
            return
        found = self.owner_of(wanted)
        if found is None:
            ctx.reply(self.message("nick_unknown", name=wanted))
            return
        if found.client_id != ctx.client.id and not self._may_manage(ctx, found.client_id):
            return
        self.forget(wanted)
        log.info("nickreg: %s deleted the registration for %r", ctx.client.name, wanted)
        ctx.reply(self.message("nick_deleted", name=found.display or wanted))

    @command(level=20)
    def cmd_listnick(self, ctx: CommandContext) -> None:
        """listnick [player] - what nicknames are reserved for you, or for somebody else"""
        target = ctx.client
        handle = ctx.args.strip()
        if handle:
            found = self.resolve_client(ctx, handle)
            if found is None:
                return
            target = found
        if target.id != ctx.client.id and not self._may_manage(ctx, target.id or 0, target):
            return
        nicks = self.nicks_of(target.id or 0)
        if not nicks:
            ctx.reply(self.message("nick_none", name=target.name))
            return
        ctx.reply(
            self.message(
                "nick_list",
                name=target.name,
                nicks=", ".join(n.display or n.name for n in nicks),
            )
        )

    def _may_manage(self, ctx: CommandContext, owner_id: int, owner: Client | None = None) -> bool:
        """Whether the caller may touch somebody else's registrations, refusing out loud if not.

        Two conditions, both the classic's: they need `manage_level`, and they cannot act on somebody
        *above* them. The second is what stops a senior admin quietly freeing a superadmin's name.
        """
        level = as_int(self.settings.get("manage_level"), 100)
        name = owner.name if owner is not None else f"@{owner_id}"
        if ctx.client.max_level() < level:
            ctx.reply(self.message("nick_not_yours", name=name))
            return False
        if owner is not None and owner.max_level() > ctx.client.max_level():
            ctx.reply(self.message("nick_not_yours", name=name))
            return False
        return True


__all__ = [
    "DEFAULTS",
    "MESSAGES",
    "WARNING_DURATION",
    "Base",
    "NickregPlugin",
    "RegisteredNick",
    "normalise",
]
