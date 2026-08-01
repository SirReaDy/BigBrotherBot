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
        found = [
            p for p in self.penalties if not p.inactive and (types is None or p.type in types)
        ]
        return list(reversed(found))[:limit]

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
        self.clients = ClientManager()
        self.storage = storage or FakeStorage()
        self.command_registry = CommandRegistry()
        self.clock = FakeClock()
        self.messages = Messages()
        self.scheduler = Scheduler(self.clock)

        self.game = Game()
        self.said: list[str] = []
        self.said_big: list[str] = []
        self.told: list[tuple[Client, str]] = []
        # Server-query answers tests can set; every read verb reports "nothing known" by default.
        self.players: list[PlayerInfo] = []
        self.cvars: dict[str, str] = {}
        self.maps: list[str] = []
        self.next_map: str | None = None
        self.map_changes: list[str] = []
        self.rotations = 0
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

    def change_map(self, name: str) -> None:
        self.map_changes.append(name)

    def rotate_map(self) -> None:
        self.rotations += 1

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
