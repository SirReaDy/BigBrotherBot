"""Typed event vocabulary and the event object.

This replaces the legacy ``b3.events`` module, whose event ids were assigned at
runtime and *injected into module globals* (``EVT_CLIENT_SAY`` became a live module
attribute). Here events are a plain :class:`enum.Enum`, so they are static, importable,
type-checkable, and free of global mutable state.

The vocabulary itself is lifted from the classic codebase — it encodes years of
"what actually happens on a game server" and is the single most valuable contract to keep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from b3.domain.client import Client


class EventType(Enum):
    """The canonical B3 event vocabulary.

    Names mirror the legacy ``EVT_*`` constants (minus the prefix) so the mapping
    from old code stays obvious during the transplant.
    """

    # --- client lifecycle -------------------------------------------------
    CLIENT_CONNECT = auto()
    CLIENT_JOIN = auto()
    CLIENT_AUTH = auto()
    CLIENT_DISCONNECT = auto()
    CLIENT_UPDATE = auto()
    CLIENT_NAME_CHANGE = auto()
    CLIENT_TEAM_CHANGE = auto()

    # --- chat -------------------------------------------------------------
    CLIENT_SAY = auto()
    CLIENT_TEAM_SAY = auto()
    CLIENT_SQUAD_SAY = auto()
    CLIENT_PRIVATE_SAY = auto()

    # --- combat -----------------------------------------------------------
    CLIENT_KILL = auto()
    CLIENT_KILL_TEAM = auto()
    CLIENT_SUICIDE = auto()
    CLIENT_DAMAGE = auto()
    CLIENT_DAMAGE_SELF = auto()
    CLIENT_DAMAGE_TEAM = auto()
    CLIENT_GIB = auto()
    CLIENT_GIB_SELF = auto()
    CLIENT_GIB_TEAM = auto()
    CLIENT_ACTION = auto()
    CLIENT_ITEM_PICKUP = auto()
    #: A player entered the world with a fresh life. The classic bot's `EVT_CLIENT_SPAWN`, which was
    #: missing here because no parser had reported it until Frostbite — the engine whose only
    #: reliable statement of a player's team is the spawn line.
    CLIENT_SPAWN = auto()

    # --- moderation -------------------------------------------------------
    CLIENT_KICK = auto()
    CLIENT_BAN = auto()
    CLIENT_BAN_TEMP = auto()
    CLIENT_UNBAN = auto()
    CLIENT_WARN = auto()
    CLIENT_NOTICE = auto()

    # --- voice comms and voting -------------------------------------------
    #
    # The classic bot let each parser *invent* its events at runtime (`createEvent`), which is how
    # these ended up looking Urban-Terror-specific. They are not: a radio/voice command and a
    # callvote exist in most of the engines this bot supports, so they belong in the shared
    # vocabulary rather than in one game's corner. Spam control cares about radio in particular —
    # legacy went as far as monkey-patching the plugin at runtime to make it listen.
    CLIENT_RADIO = auto()
    CLIENT_CALLVOTE = auto()
    CLIENT_VOTE = auto()
    VOTE_PASSED = auto()
    VOTE_FAILED = auto()

    # --- game / round -----------------------------------------------------
    GAME_ROUND_START = auto()
    GAME_ROUND_END = auto()
    GAME_MAP_CHANGE = auto()
    GAME_WARMUP = auto()
    GAME_EXIT = auto()
    # Game-level objective outcomes, as opposed to something a player did (those are CLIENT_ACTION
    # with a name — "flag_captured", "bomb_planted" — which is how every engine here reports them).
    GAME_FLAG_RETURNED = auto()
    GAME_BOMB_EXPLODED = auto()
    GAME_SURVIVOR_WINNER = auto()

    # --- admin / plugin lifecycle ----------------------------------------
    ADMIN_COMMAND = auto()
    PLUGIN_ENABLED = auto()
    PLUGIN_DISABLED = auto()
    PLUGIN_LOADED = auto()
    PLUGIN_UNLOADED = auto()

    # --- runtime ----------------------------------------------------------
    STOP = auto()
    EXIT = auto()
    CUSTOM = auto()
    UNKNOWN = auto()


@dataclass(slots=True)
class Event:
    """A single event flowing through the bus.

    Deliberately a small struct (mirrors the legacy ``Event``): a type, an optional
    subject ``client`` and ``target``, a free-form ``data`` payload, and the time it
    occurred (epoch seconds; injected so tests can use a fake clock).
    """

    type: EventType
    data: Any = None
    client: "Client | None" = None
    target: "Client | None" = None
    time: float = 0.0
    # Arbitrary extra key/value context, used sparingly (e.g. custom event names).
    extra: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        bits = [self.type.name]
        if self.client is not None:
            bits.append(f"client={self.client!r}")
        if self.target is not None:
            bits.append(f"target={self.target!r}")
        if self.data is not None:
            bits.append(f"data={self.data!r}")
        return f"<Event {' '.join(bits)}>"


class VetoEvent(Exception):
    """Raised by a handler to stop propagation of the current event.

    Mirrors the legacy ``VetoEvent``: once raised, no further handlers run for this event.
    """
