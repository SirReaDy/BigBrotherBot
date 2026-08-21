"""Client, Penalty and Alias domain objects.

Rewritten as dataclasses instead of the legacy property-bag ``Client`` whose ~50 hand-written
getter/setter pairs carried side effects (assigning ``client.name`` created an alias and fired an
event). Here the objects are plain data; behaviour that used to hide in setters becomes explicit
methods, and persistence + event emission live in the storage/runtime layers, not on the object.

The identity model is preserved exactly: ``guid`` is the natural key, ``id`` the surrogate,
and ``cid`` the transient in-game slot number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from b3.domain.permissions import DEFAULT_GROUPS, Group, max_level

# Sentinel: a penalty that never expires (permanent). Matches the legacy time_expire == -1.
NEVER_EXPIRES = -1


class PenaltyType(str, Enum):
    """Penalty kinds. Values match the legacy ``penalties.type`` enum for data compatibility."""

    BAN = "Ban"
    TEMPBAN = "TempBan"
    KICK = "Kick"
    WARNING = "Warning"
    NOTICE = "Notice"


@dataclass(slots=True)
class Penalty:
    type: PenaltyType
    client_id: int
    admin_id: int | None = None
    id: int | None = None
    duration: int = 0  # minutes (0 == permanent for a TempBan)
    inactive: bool = False  # soft-delete / lifted flag
    keyword: str = ""
    reason: str = ""
    data: str = ""
    # Which game server issued this, from `bot.server_id`. "" = not stated (every legacy row).
    server_id: str = ""
    time_add: int = 0
    time_edit: int = 0
    time_expire: int = NEVER_EXPIRES  # epoch seconds; -1 == never

    def is_active(self, now_epoch: int) -> bool:
        """Legacy rule: ``inactive == 0 AND (time_expire == -1 OR time_expire > now)``."""
        if self.inactive:
            return False
        return self.time_expire == NEVER_EXPIRES or self.time_expire > now_epoch


@dataclass(slots=True)
class Alias:
    value: str
    client_id: int
    id: int | None = None
    num_used: int = 1
    time_add: int = 0
    time_edit: int = 0


@dataclass(slots=True)
class IpAlias:
    value: str
    client_id: int
    id: int | None = None
    num_used: int = 1
    time_add: int = 0
    time_edit: int = 0


@dataclass(slots=True)
class Client:
    # --- persisted identity / fields (map 1:1 to the clients table) -------
    guid: str = ""  # natural key
    id: int | None = None  # surrogate PK
    pbid: str = ""
    ip: str = ""
    name: str = ""
    group_bits: int = 0
    connections: int = 0
    mask_level: int = 0
    auto_login: bool = False
    greeting: str = ""
    password: str | None = None
    login: str | None = None
    time_add: int = 0
    time_edit: int = 0

    # --- transient runtime state (never persisted) ------------------------
    cid: str | None = None  # in-game slot id
    #: An AI player rather than a person. Set where the guid is recognised as a bot's and dropped —
    #: after that the guid is empty, so the fact is not recoverable later. Plugins need it: an AFK
    #: check that asks a bot whether it is still there, or counts bots as the humans a server must
    #: keep, is a plugin doing arithmetic on players who cannot answer. The classic bot's
    #: `Client.bot`.
    is_bot: bool = False
    #: When this bot first saw this player in this slot, as epoch seconds. Not the same thing as
    #: `time_add`, which is when their *database row* was created — a regular who first joined two
    #: years ago and reconnected a minute ago has an ancient `time_add`. Anything that means "who
    #: arrived most recently" has to read this: the classic bot's `makeroom` sorted by `timeAdd` and
    #: so freed a slot by kicking the newest *account* on the server rather than the newest arrival.
    #: Stamped by `ClientManager.add`, the one funnel every parser creates a client through. For a
    #: player already on the server when the bot started it is when the bot adopted them, which is
    #: the earliest moment anything here can honestly claim to know about.
    connected_at: float = 0.0
    #: When this player was last here, as an epoch — 0 for somebody the database has never seen.
    #: Captured at authentication and not derived later, because `time_edit` on the row *is* "last
    #: seen" and saving the session overwrites it with now: after the save there is nothing left to
    #: read. The classic bot's `Client.lastVisit`, and `welcome` is what wants it.
    last_visit: int = 0
    authed: bool = False
    rejected: bool = False  # a ban was re-applied on auth; do not serve this client
    # The server reported a name longer than the protocol allows (see GameProfile.name_max_length).
    # `name` has already been truncated to fit; this records that it happened, since the runtime
    # rather than the parser decides whether to kick for it.
    name_overflow: bool = False
    team: str | None = None
    #: Whether this player is currently in play, as opposed to waiting to respawn. The classic
    #: bot's ``STATE_ALIVE``/``STATE_DEAD``, kept for one reason: some engines show a dead player
    #: only what other dead players are saying, so a bot that answers a `!command` with a plain
    #: `say` is answering into a channel the person who typed it cannot read. `Console.say_dead`
    #: and `Console.smart_say` are what use it. Defaults to alive, because a player the bot has just
    #: adopted mid-round is far more likely to be playing than to be waiting, and being wrong in
    #: that direction sends them a message they can see.
    alive: bool = True
    _vars: dict[int, dict[str, Any]] = field(default_factory=dict)

    def require_id(self) -> int:
        """This client's database id, insisting that it has one.

        ``id`` is optional because a client parsed off a log line has not been saved yet. Code that
        has just loaded a client *from* storage knows better, and this is how it says so: the
        alternative is passing None into a query, which matches nothing and reports "no bans found"
        for a player who has plenty.
        """
        if self.id is None:
            raise ValueError(f"client {self.name!r} is not stored yet, so it has no id")
        return self.id

    # -- permissions -------------------------------------------------------

    def max_level(self, groups: tuple[Group, ...] = DEFAULT_GROUPS) -> int:
        """Effective permission level from ``group_bits`` (0-100)."""
        return max_level(self.group_bits, groups)

    def in_group(self, group: Group) -> bool:
        if group.id == 0:
            return self.group_bits == 0
        return bool(self.group_bits & group.id)

    def add_group(self, group: Group) -> None:
        self.group_bits |= group.id

    def set_group(self, group: Group) -> None:
        """Make this the client's *only* group — the legacy ``setGroup``.

        `!putgroup bob mod` means "Bob is a mod", not "Bob is a mod as well as whatever he was":
        promotion and demotion both have to work, and OR-ing a lower group onto a higher one would
        silently do nothing.
        """
        self.group_bits = group.id

    def remove_group(self, group: Group) -> None:
        self.group_bits &= ~group.id

    def display_level(self, groups: tuple[Group, ...] = DEFAULT_GROUPS) -> int:
        """The level other players are shown — the mask if one is set, else the real level.

        A mask hides rank; it never grants or removes it. :meth:`max_level` stays the single source
        of truth for what the client may *do*, exactly as in the classic bot.
        """
        return self.mask_level or self.max_level(groups)

    def is_masked(self) -> bool:
        return self.mask_level > 0

    # -- per-plugin scratch storage (replaces legacy setvar/var/delvar) ----

    def set_var(self, plugin: object, key: str, value: Any) -> None:
        self._vars.setdefault(id(plugin), {})[key] = value

    def get_var(self, plugin: object, key: str, default: Any = None) -> Any:
        return self._vars.get(id(plugin), {}).get(key, default)

    def del_var(self, plugin: object, key: str) -> None:
        self._vars.get(id(plugin), {}).pop(key, None)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Client id={self.id} cid={self.cid} guid={self.guid!r} name={self.name!r}>"
