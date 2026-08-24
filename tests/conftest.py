"""Shared test doubles: an in-memory Console + Storage satisfying the runtime ports."""

from __future__ import annotations

import pytest

from b3.core.bus import EventBus
from b3.core.clients import ClientManager
from b3.core.clock import FakeClock
from b3.core.commands import CommandContext, CommandRegistry
from b3.core.game import Game, PlayerInfo
from b3.core.messages import Messages
from b3.core.scheduler import Scheduler
from b3.core.util import format_time
from b3.core.events import Event, EventType
from b3.domain.client import NEVER_EXPIRES, Alias, Client, IpAlias, Penalty, PenaltyType
from b3.domain.permissions import DEFAULT_GROUPS, Group
from b3.parsers.profile import GameProfile


class FakeStorage:
    """Minimal Storage stand-in for command/plugin tests."""

    def __init__(self, superadmin: bool = False) -> None:
        self._superadmin = superadmin
        self.groups: list[Group] = list(DEFAULT_GROUPS)
        self.saved: list[Client] = []
        self.penalties: list[Penalty] = []
        self.aliases: dict[int, list[Alias]] = {}
        self.ip_aliases: dict[int, list[IpAlias]] = {}
        self.disabled: list[tuple[int, PenaltyType | None]] = []
        self.clients_by_id: dict[int, Client] = {}
        self.search_results: list[Client] = []
        #: What "now" is for penalty expiry. Tests that care set it; the default keeps every
        #: unexpired penalty active.
        self.now_epoch = 0
        self._engine: object | None = None
        #: Whether this storage offers an engine at all. A backend that does not is a real case —
        #: a plugin that owns tables has to degrade loudly rather than break — so a test can say so
        #: by setting this False before it starts the plugin.
        self.gives_engine = True

    @property
    def engine(self) -> object | None:
        """A real, in-memory SQLAlchemy engine, created on first use.

        `Plugin.storage_engine()` is how a plugin owns its own tables (`callvote`'s vote history,
        `jumper`'s records), and a fake with no engine forced those plugins to be tested against a
        whole `Bot` — so the half of them that is *not* about persistence was tested through three
        more layers than it needed. In memory, so each test gets its own empty database and nothing
        reaches a disk.
        """
        if not self.gives_engine:
            return None
        if self._engine is None:
            from sqlalchemy import create_engine
            from sqlalchemy.pool import StaticPool

            # One connection, shared: `:memory:` is per-connection, so a pool would hand out
            # databases that cannot see each other's tables.
            self._engine = create_engine(
                "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
            )
        return self._engine

    def has_superadmin(self) -> bool:
        return self._superadmin

    def get_groups(self) -> list[Group]:
        return list(self.groups)

    def save_client(self, client: Client) -> Client:
        if client.id is None:
            client.id = len(self.saved) + 1
        self.saved.append(client)
        self.clients_by_id[client.id] = client
        return client

    def get_client_by_id(self, client_id: int) -> Client | None:
        return self.clients_by_id.get(client_id)

    def get_client_by_guid(self, guid: str) -> Client | None:
        """The saved client with this guid.

        Note the fidelity limit: this hands back the very object that was saved, where the real
        storage rebuilds one from a row. A test that needs "what is actually on disk" — a session
        change that must *not* be persisted, for instance — has to use a real database.
        """
        for client in reversed(self.saved):
            if client.guid == guid:
                return client
        return None

    def search_clients(self, term: str, limit: int = 5) -> list[Client]:
        return list(self.search_results)

    def add_penalty(self, penalty: Penalty) -> Penalty:
        self.penalties.append(penalty)
        return penalty

    def get_active_penalties(
        self, client_id: int, type_: PenaltyType | None = None
    ) -> list[Penalty]:
        found = [
            p
            for p in self.penalties
            if p.client_id == client_id and not p.inactive and (type_ is None or p.type == type_)
        ]
        return list(reversed(found))  # newest first, as the real storage promises

    def disable_penalty(self, penalty_id: int | None) -> bool:
        for penalty in self.penalties:
            if penalty.id == penalty_id and not penalty.inactive:
                penalty.inactive = True
                return True
        return False

    def get_recent_penalties(self, types=None, limit: int = 5):  # noqa: ANN001, ANN201
        found = [p for p in self.penalties if not p.inactive and (types is None or p.type in types)]
        return list(reversed(found))[:limit]

    def banned_ips(self) -> set[str]:
        found = set()
        for penalty in self.penalties:
            if penalty.inactive or penalty.type not in (PenaltyType.BAN, PenaltyType.TEMPBAN):
                continue
            if not penalty.is_active(self._clock_epoch()):
                continue
            client = self.clients_by_id.get(penalty.client_id)
            if client is not None and client.ip:
                found.add(client.ip)
        return found

    def _clock_epoch(self) -> int:
        return self.now_epoch

    def disable_penalties(self, client_id: int, type_: PenaltyType | None = None) -> int:
        self.disabled.append((client_id, type_))
        matched = self.get_active_penalties(client_id, type_)
        for penalty in matched:
            penalty.inactive = True
        return len(matched)

    def add_alias(self, alias: Alias) -> Alias:
        self.aliases.setdefault(alias.client_id, []).append(alias)
        return alias

    def get_aliases(self, client_id: int) -> list[Alias]:
        return self.aliases.get(client_id, [])

    def add_ip_alias(self, ip_alias: IpAlias) -> IpAlias:
        self.ip_aliases.setdefault(ip_alias.client_id, []).append(ip_alias)
        return ip_alias

    def get_ip_aliases(self, client_id: int) -> list[IpAlias]:
        return self.ip_aliases.get(client_id, [])


class FakeConsole:
    """Records outgoing actions so tests can assert on them."""

    def __init__(self, storage: FakeStorage | None = None) -> None:
        self.bus = EventBus()
        self.clock = FakeClock()
        self.clients = ClientManager(self.clock)
        self.storage = storage or FakeStorage()
        self.command_registry = CommandRegistry()
        self.messages = Messages()
        self.scheduler = Scheduler(self.clock)

        self.game = Game()
        self.said: list[str] = []
        self.said_big: list[str] = []
        self.said_dead: list[str] = []
        #: Whether this server is running PunkBuster, and who a screenshot was asked of.
        self.punkbuster = True
        self.screenshots: list[Client] = []
        #: Lines handed to PunkBuster through the port, and canned replies for them. Recorded apart
        #: from `rcon_sent` because *how* the verb is carried is the title's business, not a
        #: plugin's — see `Console.send_punkbuster`.
        self.punkbuster_sent: list[str] = []
        self.punkbuster_replies: dict[str, str] = {}
        self.told: list[tuple[Client, str]] = []
        # Server-query answers tests can set; every read verb reports "nothing known" by default.
        self.players: list[PlayerInfo] = []
        self.cvars: dict[str, str] = {}
        self.maps: list[str] = []
        #: Engine team id -> the bot's name for it, as a profile carries. Frostbite's, since it is
        #: the only family whose verbs take the id.
        self.teams: dict[str, str] = {"0": "", "1": "red", "2": "blue", "3": "green", "4": "yellow"}
        self.map_names: dict[str, str] = {}  # lowercased id -> display name, as a profile carries
        self.next_map: str | None = None
        #: The cvar this title holds the next map in — `g_nextmap` on Urban Terror, nothing on most.
        self.next_map_cvar = ""
        self.map_changes: list[str] = []
        #: Extras passed with each map change, one entry per `map_changes` entry.
        self.map_extras: list[dict[str, str]] = []
        #: Which title's `!map` grammar to answer with. The default takes a bare name, like almost
        #: every engine here; a test for the two that take more replaces it with their profile.
        self.map_profile = GameProfile(name="fake")
        self.rotations = 0
        #: Vote cancellations attempted, and whether this title can cancel one at all (Urban Terror
        #: is the only family here whose engine has the verb).
        self.vote_cancels = 0
        #: Engine verbs this title has (`GameProfile.player_verbs`) and the ones a plugin applied.
        #: Urban Terror's set by default, since it is the only family here that has any.
        self.player_verbs: set[str] = {"slap", "nuke", "kill", "mute", "forceteam", "swap"}
        self.verbs_applied: list[tuple[str, Client, dict[str, str]]] = []
        #: What the server answers a verb asked through `ask_verb`, keyed by verb name. Only a
        #: handful of verbs have a reply worth reading — Urban Terror's `startserverdemo` says which
        #: file it has begun writing — so this is empty by default and a test sets what it needs.
        self.verb_replies: dict[str, str] = {}
        #: The verbs that name no player, and the ones a plugin ran.
        self.server_verbs: set[str] = {
            "swapteams",
            "shuffleteams",
            "map_restart",
            "reload",
            "cyclemap",
            "exec",
        }
        self.server_verbs_applied: list[tuple[str, dict[str, str]]] = []
        #: Slot -> when its mute runs out, as the runtime tracks it.
        self.muted: dict[str, float] = {}
        self.votes_can_be_cancelled = True
        #: Operator-defined rcon lines sent through `send_rcon`, and canned replies for them.
        self.rcon_sent: list[str] = []
        self.rcon_replies: dict[str, str] = {}
        #: Replies for `rcon_words`, where a word may itself contain a space (Frostbite).
        self.rcon_words_replies: dict[str, list[str]] = {}
        self.kicked: list[tuple[Client, str, Client | None]] = []
        self.banned: list[tuple[Client, str, Client | None]] = []
        self.tempbanned: list[tuple[Client, int, str, Client | None]] = []
        self.warned: list[tuple[Client, str, Client | None]] = []
        self.unbanned: list[tuple[Client, str, Client | None]] = []
        self.noticed: list[tuple[Client, str, Client | None]] = []
        self.paused_minutes: list[float] = []
        self.exit_code: int | None = None
        self.reloads = 0
        self._by_handle: dict[str, Client] = {}
        self._connected: dict[str, list[Client]] = {}  # handles that match several players
        self._lookup: dict[str, list[Client]] = {}
        # Loaded plugins, as the real Console exposes them (tests populate this when they need it).
        self.plugins: dict[str, object] = {}

    def get_plugin(self, name: str) -> object | None:
        return self.plugins.get(name)

    # in-game output
    def say(self, text: str) -> None:
        self.said.append(text)

    def say_big(self, text: str) -> None:
        self.said_big.append(text)

    def tell(self, client: Client, text: str) -> None:
        self.told.append((client, text))

    def say_dead(self, text: str) -> None:
        self.said_dead.append(text)
        for client in self.clients.connected():
            if not client.alive:
                self.told.append((client, f"[DEAD] {text}"))

    def smart_say(self, client: Client, text: str) -> None:
        if client.alive and client.team != "spec":
            self.say(text)
        else:
            self.say_dead(text)

    # server queries + control
    def get_players(self) -> list[PlayerInfo]:
        return list(self.players)

    def get_cvar(self, name: str) -> str | None:
        return self.cvars.get(name)

    def set_cvar(self, name: str, value: str) -> None:
        self.cvars[name] = value

    def get_map(self) -> str | None:
        return self.game.map_name or None

    def get_maps(self) -> list[str]:
        return list(self.maps)

    def get_next_map(self) -> str | None:
        return self.next_map

    def set_next_map(self, name: str) -> bool:
        """A title with no such cvar answers no; tests set `next_map_cvar` when they want one."""
        if not self.next_map_cvar:
            return False
        self.cvars[self.next_map_cvar] = name
        self.next_map = name
        return True

    def parse_map_request(self, text: str):  # noqa: ANN201 - MapRequest, imported lazily
        return self.map_profile.parse_map_request(text)

    def map_usage(self) -> str:
        return self.map_profile.map_usage()

    def change_map(self, name: str, extras=None) -> None:  # noqa: ANN001
        self.map_changes.append(name)
        self.map_extras.append(dict(extras or {}))

    def team_id(self, team: str) -> str:
        """As a profile answers it — `teams` read backwards. Set `console.teams` to a title's."""
        wanted = (team or "").strip().lower()
        return next((k for k, v in self.teams.items() if v.lower() == wanted), "") if wanted else ""

    def map_display(self, map_id: str) -> str:
        return self.map_names.get(map_id.lower(), map_id)

    def request_screenshot(self, client: Client) -> bool:
        self.screenshots.append(client)
        return self.punkbuster

    def send_punkbuster(self, command: str) -> str | None:
        """As the runtime answers it: None on a server with no PunkBuster, the reply otherwise."""
        if not self.punkbuster:
            return None
        self.punkbuster_sent.append(command)
        return self.punkbuster_replies.get(command, "")

    def send_rcon(self, command: str) -> str:
        self.rcon_sent.append(command)
        return self.rcon_replies.get(command, "")

    def rcon_words(self, command: str) -> list[str]:
        """The word-list reply — `rcon_words_replies` when a test set one, else the flat reply."""
        self.rcon_sent.append(command)
        if command in self.rcon_words_replies:
            return list(self.rcon_words_replies[command])
        reply = self.rcon_replies.get(command, "")
        return [reply] if reply else []

    def rotate_map(self) -> None:
        self.rotations += 1

    def mute(self, client: Client, minutes: float) -> bool:
        """The runtime's mute, with the same "a longer mute wins" rule."""
        if "mute" not in self.player_verbs:
            return False
        until = self.clock.now() + max(1, int(minutes * 60))
        if until <= self.muted.get(client.cid or "", 0.0):
            return True
        self.muted[client.cid or ""] = until
        return self.apply_verb("mute", client, seconds=str(max(1, int(minutes * 60))))

    def unmute(self, client: Client) -> bool:
        self.muted.pop(client.cid or "", None)
        if "mute" not in self.player_verbs:
            return False
        return self.apply_verb("mute", client, seconds="0")

    def muted_until(self, client: Client) -> float:
        return self.muted.get(client.cid or "", 0.0)

    def lift_expired_mutes(self) -> None:
        """What the runtime's scheduled task does; tests call it directly."""
        now = self.clock.now()
        for cid, until in list(self.muted.items()):
            if now >= until:
                del self.muted[cid]
                client = self.clients.get_by_cid(cid)
                if client is not None:
                    self.apply_verb("mute", client, seconds="0")

    def supports_server_verb(self, name: str) -> bool:
        return name in self.server_verbs

    def ask_verb(self, name: str, client: Client, **values: str) -> str | None:
        """As the runtime answers it: the verb is applied and the server's reply comes back.

        `verb_replies` is keyed by verb name, since a test cares which verb was asked and not how the
        title spells it.
        """
        if not self.apply_verb(name, client, **values):
            return None
        return self.verb_replies.get(name, "")

    def ask_server_verb(self, name: str, **values: str) -> str | None:
        if not self.apply_server_verb(name, **values):
            return None
        return self.verb_replies.get(name, "")

    def apply_server_verb(self, name: str, **values: str) -> bool:
        if name not in self.server_verbs:
            return False
        self.server_verbs_applied.append((name, dict(values)))
        return True

    def supports_verb(self, name: str) -> bool:
        return name in self.player_verbs

    def apply_verb(self, name: str, client: Client, **values: str) -> bool:
        """Records the attempt. `player_verbs` is what a title's profile declares."""
        if name not in self.player_verbs:
            return False
        self.verbs_applied.append((name, client, dict(values)))
        return True

    def can_cancel_vote(self) -> bool:
        return self.votes_can_be_cancelled

    def cancel_vote(self) -> bool:
        """Records the attempt; a title with no veto verb reports that it cannot."""
        if not self.votes_can_be_cancelled:
            return False
        self.vote_cancels += 1
        return True

    def sync(self) -> list[Client]:
        return self.clients.connected()

    # bot lifecycle
    def format_time(self, epoch: float | None = None) -> str:
        return format_time(self.clock.now() if epoch is None else epoch)

    def pause(self, minutes: float) -> None:
        self.paused_minutes.append(minutes)

    def is_paused(self) -> bool:
        return bool(self.paused_minutes) and self.paused_minutes[-1] > 0

    def shutdown(self, restart: bool = False) -> None:
        self.exit_code = 221 if restart else 0

    def reload_config(self) -> object:
        self.reloads += 1
        return None

    async def run_command(self, issuer: Client, text: str) -> bool:
        from b3.core.commands import CommandProcessor

        return await CommandProcessor(self.command_registry, self).handle(issuer, text)

    # moderation
    def kick(self, client, reason="", admin=None):  # noqa: ANN001
        self.kicked.append((client, reason, admin))

    def ban(self, client, reason="", admin=None):  # noqa: ANN001
        self.banned.append((client, reason, admin))

    def tempban(self, client, minutes, reason="", admin=None):  # noqa: ANN001
        self.tempbanned.append((client, minutes, reason, admin))

    def warn(self, client, reason="", admin=None, minutes=0):  # noqa: ANN001
        # Records and publishes like the real Console does — escalation hangs off both.
        self.warned.append((client, reason, admin))
        if client.id is not None:
            self.storage.add_penalty(
                Penalty(
                    type=PenaltyType.WARNING,
                    client_id=client.id,
                    admin_id=admin.id if admin else None,
                    id=len(self.storage.penalties) + 1,
                    duration=minutes,
                    reason=reason,
                    time_expire=self.clock.epoch() + minutes * 60 if minutes else NEVER_EXPIRES,
                )
            )
        self.bus.publish_soon(Event(EventType.CLIENT_WARN, client=client, data=reason))

    def notice(self, client, reason="", admin=None):  # noqa: ANN001
        self.noticed.append((client, reason, admin))

    def unban(self, client, reason="", admin=None):  # noqa: ANN001
        self.unbanned.append((client, reason, admin))
        if client.id is not None:
            for type_ in (PenaltyType.BAN, PenaltyType.TEMPBAN):
                self.storage.disable_penalties(client.id, type_)

    # lookup + reply
    def find_clients(self, handle: str) -> list[Client]:
        if handle in self._connected:
            return self._connected[handle]
        found = self._by_handle.get(handle)
        return [found] if found is not None else []

    def find_client(self, handle: str) -> Client | None:
        matches = self.find_clients(handle)
        return matches[0] if matches else None

    def lookup_clients(self, term: str) -> list[Client]:
        if term in self._lookup:
            return self._lookup[term]
        found = self._by_handle.get(term)
        return [found] if found is not None else []

    def register_client(self, handle: str, client: Client) -> None:
        self._by_handle[handle] = client

    def register_clients(self, handle: str, clients: list[Client]) -> None:
        """Make a handle match several connected players."""
        self._connected[handle] = clients

    def register_lookup(self, term: str, clients: list[Client]) -> None:
        self._lookup[term] = clients

    def command_reply(self, ctx: CommandContext, text: str) -> None:
        if ctx.loud:
            self.say(text)
        else:
            self.tell(ctx.client, text)


@pytest.fixture()
def console() -> FakeConsole:
    return FakeConsole()
