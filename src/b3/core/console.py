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
    from collections.abc import Mapping

    from b3.core.bus import EventBus
    from b3.core.clock import Clock
    from b3.core.clients import ClientManager
    from b3.core.commands import CommandContext, CommandRegistry
    from b3.core.game import Game, PlayerInfo
    from b3.core.messages import Messages
    from b3.core.plugin import Plugin
    from b3.core.scheduler import Scheduler
    from b3.parsers.profile import MapRequest
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

    def send_rcon(self, command: str) -> str:
        """Send a command **an operator wrote** to the game server, and return its reply.

        Deliberately the last thing on this port, and deliberately narrow in what it is for: the
        engine-specific verbs a plugin needs are already here as `kick`, `ban`, `change_map` and the
        rest, and a plugin reaching for raw rcon instead of those is a plugin that will only work on
        one game. This exists for `customcommands`, whose entire purpose is to run a line of rcon the
        *operator* typed into their own config file — which nothing else can express.

        The caller is responsible for what it puts in the string. Anything a **player** supplied has
        to go through `b3.core.util.sanitize_rcon_value` first, or a name containing a quote or a
        semicolon becomes a second command.
        """
        ...

    def say_dead(self, text: str) -> None:
        """Say something only the players waiting to respawn will see.

        No engine here has a verb for it; it is the same private message sent to each dead player,
        so it works anywhere there is a `tell`.
        """
        ...

    def smart_say(self, client: "Client", text: str) -> None:
        """Answer where ``client`` will actually see it — to the server, or to the dead.

        On several of these engines a dead or spectating player is shown only the dead chat, so a
        plain `say` reaches everybody except the person who asked.
        """
        ...

    def map_display(self, map_id: str) -> str:
        """The name a player would call this map, or the raw id when the title has no table."""
        ...

    def request_screenshot(self, client: "Client") -> bool:
        """Ask PunkBuster for a screenshot of this player's view. False where there is no PunkBuster.

        The picture goes to the game server's own PunkBuster folder, so the only thing this can
        report is whether it was asked for.
        """
        ...

    def get_map(self) -> str | None:
        """The map currently running, asked of the server (not the cached `game.map_name`)."""
        ...

    def get_maps(self) -> list[str]:
        """Maps in the server's rotation."""
        ...

    def get_next_map(self) -> str | None: ...

    def set_next_map(self, name: str) -> bool:
        """Say which map comes after this one. False where this engine has no such setting.

        The counterpart of `get_next_map`, and it belongs here for the same reason: *which* cvar holds
        it is a fact about the title (`GameProfile.next_map_cvar`), and three things want it — this,
        `!nextmap`, and `callvote`'s announcement when somebody votes for a map change.
        """
        ...

    def parse_map_request(self, text: str) -> "MapRequest":
        """Split a `!map` argument into a map and this engine's extra arguments, if it takes any."""
        ...

    def map_usage(self) -> str:
        """The `!map` arguments past the map name, written in this engine's own separator.

        Empty on almost every title, which is what makes `!map <name>` the usage line everywhere
        else without the plugin having to know which engines are the exceptions.
        """
        ...

    def change_map(self, name: str, extras: "Mapping[str, str] | None" = None) -> None:
        """Load a named map. ``extras`` holds whatever else this engine's map verb takes —
        `GameProfile.map_arguments` names them, and a title that declares none never gets any."""
        ...

    def mute(self, client: Client, minutes: float) -> bool:
        """Silence a player for a while. False where this engine has no verb for it.

        Here rather than in a plugin because **two** plugins want it — `censor`'s escalating mute and
        `poweradminurt`'s `!pamute` — and two owners of the same engine state fight: whichever holds
        the shorter deadline lifts the other's mute early. The policy (how long, for what) stays in
        the plugins; the mechanism and the deadline are one thing, here.
        """
        ...

    def unmute(self, client: Client) -> bool:
        """Let a player talk again, now, whatever deadline was running."""
        ...

    def muted_until(self, client: Client) -> float:
        """When this player's mute runs out, as epoch seconds; 0.0 if they are not muted.

        The question a second plugin has to be able to ask before muting somebody: "is one already
        running, and for longer than mine?"
        """
        ...

    def supports_verb(self, name: str) -> bool:
        """Whether this engine has a verb for doing ``name`` to a player — `slap`, `mute`, and so on.

        Asked before offering it, which is the whole point: the classic bot's `inflictCustomPenalty`
        sent a command into the dark and reported nothing, so a plugin could offer "slap" on a title
        with no such verb and the operator would see a penalty that silently did nothing.
        `GameProfile.player_verbs` is where the answers live.
        """
        ...

    def apply_verb(self, name: str, client: Client, **values: str) -> bool:
        """Do ``name`` to a player. False when this engine has no such verb, or the template's
        arguments were not all supplied — never a silent half-sent command."""
        ...

    def supports_server_verb(self, name: str) -> bool:
        """Whether this engine has a verb for ``name`` that names no player — `shuffleteams`, say."""
        ...

    def apply_server_verb(self, name: str, **values: str) -> bool:
        """Run one. False when this engine has no such verb or an argument was not supplied."""
        ...

    def can_cancel_vote(self) -> bool:
        """Whether this engine has a verb for stopping a vote in progress at all.

        Asked rather than discovered, and that is the point of it: a plugin policing who may call a
        vote has to know in advance whether refusing one is something it can carry out. Telling a
        player they are not allowed a vote which then passes anyway is worse than saying nothing.
        Urban Terror is the only family here that answers yes.
        """
        ...

    def cancel_vote(self) -> bool:
        """Stop the vote currently running. False where the engine has no verb for it."""
        ...

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
