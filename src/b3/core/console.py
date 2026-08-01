"""The Console port.

The legacy ``console`` was a god object threaded into every domain object and plugin. Here it is a
small, explicit interface (a ``Protocol``) describing exactly what plugins are allowed to ask of the
runtime: issue in-game output, apply moderation actions, resolve a player handle, and reach the
shared services (bus, storage, command registry, clock). The concrete runtime (Phase E) implements
it; tests provide a fake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from b3.domain.client import Client

if TYPE_CHECKING:
    from b3.core.bus import EventBus
    from b3.core.clock import Clock
    from b3.core.clients import ClientManager
    from b3.core.commands import CommandContext, CommandRegistry
    from b3.core.game import Game, PlayerInfo
    from b3.core.messages import Messages
    from b3.core.plugin import Plugin
    from b3.core.scheduler import Scheduler
    from b3.storage.base import Storage


@runtime_checkable
class Console(Protocol):
    # shared services
    #
    # `storage` is a read-only property rather than a plain attribute deliberately. A Protocol
    # *attribute* is invariant: it demands the exact declared type, so a runtime holding a
    # `SqlAlchemyStorage` would not satisfy `storage: Storage` — which is precisely the
    # "Bot does not satisfy Console" complaint that has followed this project around. Declared as
    # a property it is covariant, any Storage implementation fits, and a concrete class still
    # satisfies it with an ordinary assigned attribute.
    bus: "EventBus"
    clients: "ClientManager"
    command_registry: "CommandRegistry"
    clock: "Clock"
    scheduler: "Scheduler"  # timed work (b3.core.scheduler)
    messages: "Messages"  # operator-customisable text + line wrapping
    game: "Game"  # live match state (map, gametype, timings, cvars)

    @property
    def storage(self) -> "Storage": ...

    # in-game output
    def say(self, text: str) -> None: ...
    def tell(self, client: Client, text: str) -> None: ...
    def say_big(self, text: str) -> None:
        """Announce in the engine's largest text style, falling back to `say`."""
        ...

    # moderation actions
    def kick(self, client: Client, reason: str = "", admin: Client | None = None) -> None: ...
    def ban(self, client: Client, reason: str = "", admin: Client | None = None) -> None: ...
    def tempban(
        self, client: Client, minutes: int, reason: str = "", admin: Client | None = None
    ) -> None: ...
    def warn(
        self, client: Client, reason: str = "", admin: Client | None = None, minutes: int = 0
    ) -> None:
        """Record a warning; ``minutes`` gives it a lifetime (0 = until cleared)."""
        ...

    def notice(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        """Record an admin note about a player. No in-game effect."""
        ...
    def unban(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        """Lift every active ban on a client and tell the server to un-ban them."""
        ...

    # lookup + reply routing
    def find_clients(self, handle: str) -> list[Client]:
        """Every *connected* player a handle could mean — exact cid, then exact name, then substring."""
        ...

    def find_client(self, handle: str) -> Client | None:
        """The best guess for a handle. Commands should use :meth:`find_clients` and refuse when
        it is ambiguous; this is for callers with no admin to ask."""
        ...

    def lookup_clients(self, term: str) -> list[Client]:
        """Resolve a handle against the *database*, so offline players can be acted on."""
        ...

    def command_reply(self, ctx: "CommandContext", text: str) -> None: ...

    async def run_command(self, issuer: Client, text: str) -> bool:
        """Run a chat line as if ``issuer`` had typed it (drives `!runas`)."""
        ...

    # server queries + control (RCON round trips; None/[] when there is no server to ask)
    def get_players(self) -> list["PlayerInfo"]:
        """The server's live player table — slot, name, guid, IP, ping, score."""
        ...

    def get_cvar(self, name: str) -> str | None: ...
    def set_cvar(self, name: str, value: str) -> None: ...

    def map_display(self, map_id: str) -> str:
        """The name a player would call this map, or the raw id when the title has no table."""
        ...

    def get_map(self) -> str | None:
        """The map currently running, asked of the server (not the cached `game.map_name`)."""
        ...

    def get_maps(self) -> list[str]:
        """Maps in the server's rotation."""
        ...

    def get_next_map(self) -> str | None: ...
    def change_map(self, name: str) -> None: ...
    def rotate_map(self) -> None: ...

    def sync(self) -> list[Client]:
        """Reconcile the in-memory client list with the server's, and return who is really on."""
        ...

    # bot lifecycle
    def format_time(self, epoch: float | None = None) -> str:
        """Render a timestamp (default: now) in the bot's configured time zone."""
        ...

    def pause(self, minutes: float) -> None:
        """Stop acting on log lines for a while; 0 resumes immediately."""
        ...

    def is_paused(self) -> bool: ...
    def shutdown(self, restart: bool = False) -> None:
        """Ask the run loop to stop, optionally signalling a restart to whatever supervises us."""
        ...

    def reload_config(self) -> object:
        """Re-read the main config file (messages, wrapping, time zone)."""
        ...

    # the loaded plugins
    #
    # Deliberately withheld until something needed it, because a plugin reaching for another
    # plugin's object is how the classic bot ended up with every plugin depending on `admin`.
    # Runtime enable/disable is the case that justifies it: it has to act on plugins by name.
    @property
    def plugins(self) -> dict[str, "Plugin"]: ...

    def get_plugin(self, name: str) -> "Plugin | None":
        """A loaded plugin by name, or None. Disabled plugins are loaded and returned too."""
        ...
