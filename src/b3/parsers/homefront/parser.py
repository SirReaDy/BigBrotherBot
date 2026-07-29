"""HfParser — the messages a Homefront server pushes down its admin connection.

Each arrives as ``EVENT: data`` on one of five channels, and only two of them carry anything::

    LOGIN: Courgette 76561197963239764
    TEAM CHANGE: 76561197963239764 1
    KILL: Courgette EXP_Frag Freelander
    PLAYER: 76561197963239764\\n1\\nB3bot\\nCourgette\\n7\\n2
    BROADCAST: Courgette says: !help                      (on the chatter channel)

So this is the third family whose grammar is not regex-shaped: the event name is a field, and this
dispatches on it through a dict. The channel matters as much as the name — the same text means
different things on different channels — which is why :mod:`b3.net.homefront` hands each message over
as JSON ``[channel, data]`` rather than flattening it to a line.

Four things about this engine are worth knowing before changing anything here.

**Identity is a Steam id, and the handle is the name.** Chat and kill lines name the *player*; every
penalty verb takes the *Steam id*. So `cid` holds the name (as on Frostbite) and `guid` the id, and a
kill line may carry either — the classic parser tested each field against a 17-digit pattern to find
out, and so does this.

**The chatter channel repeats itself, including the bot.** Every message on it arrives twice: once as
`BROADCAST: <name> says: <text>`, and again as a plain repeat. The bot's own `adminsay` output comes
back the same way. Only the BROADCAST form becomes chat; the repeat is reported as a custom event so
that nothing routes it to the command processor, because a bot that reads its own announcements as
player speech answers itself.

**There is no synchronous way to see who is on the server.** `RETRIEVE PLAYERLIST` is answered by one
pushed `PLAYER` message per player, so the roster is assembled here and read back through
`Bot.get_players` — the seam Altitude established.

**A kill with the damage type `Suicided` is how a player leaving looks**, not only how a self-kill
looks. It is published as a suicide either way, which is what the classic parser did; the disconnect
that follows is what actually removes them.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.net.homefront import (
    CHANNEL_CHATTER,
    CHANNEL_CLIENT_NOTICE,
    CHANNEL_SERVER,
    RECONNECTED_NOTICE,
)
from b3.parsers.base import Parser
from b3.parsers.profile import GameProfile

log = logging.getLogger(__name__)

#: A Steam id, and the only way to tell one from a player's name in a field that may hold either.
STEAM_ID_RE = re.compile(r"^[0-9]{17}$")

#: What the server reports instead of an id for somebody it will not let in, or has not resolved yet.
NOT_AN_ID = frozenset({"0", "00"})

#: What the winning-team id on a ROUND OVER means. The same numbers appear in `TEAM CHANGE` with
#: different meanings — 2 is the spectators there and a *draw* here — so the two are kept apart.
ROUND_RESULTS = {"0": "red", "1": "blue", "2": "tie"}

#: `BROADCAST: <name> [(team|squad)]says: <text>` — the only chatter form that is a player speaking.
BROADCAST_RE = re.compile(r"^(?P<name>.+?) (?:\((?P<scope>team|squad)\))?says: (?P<text>.*)$")

#: The event name and its payload, out of `EVENT: data`. Names contain spaces (`TEAM CHANGE`), which
#: become underscores when looking up the handler — the classic parser's convention, kept so that
#: reading one against the other stays easy.
MESSAGE_RE = re.compile(r"^(?P<event>[A-Z ]+): (?P<data>.*)$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class KillData:
    """Payload for a Homefront kill.

    The engine reports the damage type and nothing else — no damage figure, no hit location — so the
    classic parser's hard-coded `100` damage and `body` hit location were both invented. `weapon` is
    the name the rest of the bot uses for "what did it", and here that is the damage type.
    """

    weapon: str


class HfParser(Parser):
    """Homefront. Selected by ``family="homefront"`` in the profile."""

    def __init__(self, profile: GameProfile, clients: ClientManager | None = None) -> None:
        super().__init__(profile, clients)
        #: Steam id -> ping, from the pushed PLAYERPING messages.
        self._pings: dict[str, int] = {}
        #: Steam id -> (kills, deaths, clan), from the pushed PLAYER messages. Kept because the
        #: classic parser kept it and a stats plugin will want it; nothing here acts on it.
        self._stats: dict[str, dict[str, Any]] = {}
        #: The server's own ban list, from BAN ITEM messages. See `get_bans`.
        self._bans: dict[str, str] = {}
        #: event name (underscored) -> handler.
        self._handlers: dict[str, Callable[[str], Event | list[Event] | None]] = {
            "LOGIN": self._on_login,
            "UID": self._on_uid,
            "LOGOUT": self._on_logout,
            "TEAM_CHANGE": self._on_team_change,
            "CLAN_CHANGE": self._on_clan_change,
            "KILL": self._on_kill,
            "ROUND_OVER": self._on_round_over,
            "CHANGE_LEVEL": self._on_change_level,
            "PLAYERPING": self._on_player_ping,
            "PLAYER": self._on_player,
            "BAN_ITEM": self._on_ban_item,
            "BAN_ADDED": self._on_ban_added,
            "BAN_REMOVE": self._on_ban_removed,
            "VOTESTART": self._on_vote_start,
            "VOTE": self._on_vote,
            "VOTEEND": self._on_vote_end,
        }

    # -- dispatch ----------------------------------------------------------

    def parse_line(self, line: str) -> list[Event]:
        """Read one ``[channel, data]`` message and route it on its channel and event name."""
        decoded = self._decode(line)
        if decoded is None:
            return []
        channel, data = decoded
        if channel == CHANNEL_CLIENT_NOTICE:
            return self._as_list(self._on_client_notice(data))
        if channel == CHANNEL_CHATTER:
            return self._as_list(self._on_chatter(data))
        if channel != CHANNEL_SERVER:
            # BROADCAST, NORMAL and GAMEPLAY carry nothing this bot reads. Saying so at debug rather
            # than warning: a quiet channel is not a fault.
            log.debug("homefront: ignoring channel %s", channel)
            return []

        match = MESSAGE_RE.match(data)
        if match is None:
            log.debug("homefront: server message in no known form: %r", data[:80])
            return []
        name = match["event"].strip().replace(" ", "_")
        handler = self._handlers.get(name)
        if handler is None:
            log.debug("homefront: no handler for %s", name)
            return []
        return self._as_list(handler(match["data"]))

    @staticmethod
    def _decode(line: str) -> tuple[int, str] | None:
        line = line.strip()
        if not line:
            return None
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.warning("homefront: expected a JSON [channel, data] pair, got %r", line[:60])
            return None
        if not isinstance(message, list) or len(message) != 2:
            log.warning("homefront: expected a [channel, data] pair, got %r", message)
            return None
        channel, data = message
        return int(channel), str(data)

    @staticmethod
    def _as_list(result: Event | list[Event] | None) -> list[Event]:
        if result is None:
            return []
        return [result] if isinstance(result, Event) else list(result)

    def _on_client_notice(self, data: str) -> list[Event] | None:
        """A note from the client rather than a message from the server — there is one, see below.

        The connection came back after an outage, which means the roster cannot be trusted: every
        `LOGOUT` that happened while the socket was down was missed, so anybody who left is still on
        the list. The classic parser emptied its client list on a connection close for exactly this
        reason. Ours reports each of them as a disconnect first, rather than dropping them silently —
        anything counting who is present should hear about it — and the client asks for the player
        list at once, so whoever is really there arrives again a moment later.
        """
        if data != RECONNECTED_NOTICE:
            log.debug("homefront: unknown client notice %r", data)
            return None
        stale = self.clients.connected()
        if not stale:
            return None
        log.info(
            "homefront: connection re-established; forgetting %d player(s) learned before it dropped",
            len(stale),
        )
        events: list[Event] = []
        for client in stale:
            if client.cid is not None:
                self.clients.remove(client.cid)
            events.append(
                Event(
                    EventType.CLIENT_DISCONNECT,
                    data="",
                    client=client,
                    extra={"reason": "connection lost", "inferred": True},
                )
            )
        self._pings.clear()
        return events

    # -- identity ----------------------------------------------------------

    def _client_by_guid(self, guid: str) -> Client | None:
        for client in self.clients.connected():
            if client.guid == guid:
                return client
        return None

    def _resolve(self, handle: str) -> Client | None:
        """The player a kill line names, which may be a Steam id *or* a name."""
        handle = handle.strip()
        if not handle:
            return None
        if STEAM_ID_RE.match(handle):
            return self._client_by_guid(handle)
        return self.clients.get_by_cid(handle)

    def _ensure(self, name: str, guid: str) -> tuple[Client | None, bool]:
        """The client called ``name`` with this Steam id, creating it if this is the first sight.

        Returns ``(client, created)``. A player already known under this id keeps their record and
        gains the new name, which is how a rename mid-session stays one person: the id is the identity
        and the name is only the handle.
        """
        if guid in NOT_AN_ID:
            # `00` is a banned player being turned away and `0` is one the server has not resolved.
            # Neither is an identity, and creating a client for it would put a row in the database that
            # every unidentified player would then share.
            log.debug("homefront: ignoring a connection with no usable id (%r)", guid)
            return None, False
        existing = self._client_by_guid(guid)
        if existing is not None:
            if name and existing.cid != name:
                # The handle every chat line uses has changed, so the roster key has to move with it.
                if existing.cid is not None:
                    self.clients.remove(existing.cid)
                existing.cid = name
                existing.name = name
                self.clients.add(existing)
            return existing, False
        client = Client(cid=name, name=name, guid=guid)
        self.clients.add(client)
        return client, True

    # -- who is on the server ----------------------------------------------

    def _on_login(self, data: str) -> Event | None:
        """``LOGIN: <name> <steam id>`` — a player connected, identity included.

        Also arrives after a map change for players who never left, which is why it is idempotent.
        """
        parsed = _split_name_and_id(data)
        if parsed is None:
            log.warning("homefront: could not read a LOGIN: %r", data)
            return None
        name, guid = parsed
        client, _created = self._ensure(name, guid)
        if client is None:
            return None
        client.authed = False  # so the runtime authenticates them against the database
        return Event(EventType.CLIENT_JOIN, client=client)

    def _on_uid(self, data: str) -> Event | None:
        """``UID: <name> <steam id>`` — the same pair again, sometimes without a LOGIN before it.

        Publishes a join only when this is the first sight of the player. The classic parser created
        the client here silently, which meant a player the bot met this way was never authenticated —
        no level, no ban check — until they said something.
        """
        parsed = _split_name_and_id(data)
        if parsed is None:
            log.warning("homefront: could not read a UID: %r", data)
            return None
        name, guid = parsed
        client, created = self._ensure(name, guid)
        if client is None:
            return None
        if not created:
            return None
        client.authed = False
        return Event(EventType.CLIENT_JOIN, client=client)

    def _on_logout(self, data: str) -> Event | None:
        """``LOGOUT: <steam id>``."""
        client = self._client_by_guid(data.strip())
        if client is None:
            return None
        if client.cid is not None:
            self.clients.remove(client.cid)
        self._pings.pop(data.strip(), None)
        return Event(EventType.CLIENT_DISCONNECT, client=client)

    def _on_player(self, data: str) -> Event | None:
        """``PLAYER: <uid>\\n<team>\\n<clan>\\n<name>\\n<kills>\\n<deaths>`` — one row of the roster.

        Newline-separated *inside* one message, which is the reason this transport cannot use newlines
        as its own delimiter. Reported as no event: it is the answer to a question the bot asked, and
        the roster it maintains is read back through :meth:`get_players`.
        """
        fields = data.split("\n")
        if len(fields) < 6:
            log.warning("homefront: a PLAYER row with %d fields: %r", len(fields), data[:80])
            return None
        guid, team, clan, name, kills, deaths = (f.strip() for f in fields[:6])
        client, created = self._ensure(name, guid)
        if client is None:
            return None
        client.team = self.profile.teams.get(team, "")
        self._stats[guid] = {
            "clan": clan,
            "kills": _as_int(kills),
            "deaths": _as_int(deaths),
        }
        if created:
            # The bot started mid-match, or missed the login. This is the only place it can learn
            # about them, so it is a join like any other rather than a silent adoption.
            client.authed = False
            return Event(EventType.CLIENT_JOIN, client=client)
        return None

    def _on_player_ping(self, data: str) -> Event | None:
        """``PLAYERPING: <steam id> <ping>``."""
        parts = data.split()
        if len(parts) != 2:
            return None
        self._pings[parts[0]] = _as_int(parts[1])
        return None

    def get_players(self) -> list[PlayerInfo]:
        """The roster, from the pushed PLAYER messages — there is nothing to ask synchronously.

        Read by :meth:`b3.runtime.bot.Bot.get_players` through the seam Altitude established, and it
        reports the players this bot knows about, never fewer: the periodic `RETRIEVE PLAYERLIST` is a
        snapshot, and a player who joined a moment ago is legitimately not in it yet. `LOGOUT` is what
        removes somebody, and this engine reports it.
        """
        players: list[PlayerInfo] = []
        for client in self.clients.connected():
            if client.cid is None:
                continue
            stats = self._stats.get(client.guid, {})
            players.append(
                PlayerInfo(
                    cid=client.cid,
                    name=client.name,
                    guid=client.guid,
                    ping=self._pings.get(client.guid, 0),
                    score=int(stats.get("kills", 0)),
                )
            )
        return players

    def get_bans(self) -> dict[str, str]:
        """The server's own ban list as last reported: Steam id -> name.

        Populated from `BAN ITEM` messages, which arrive in answer to `RETRIEVE BANLIST`. Nothing acts
        on it yet; it is what an `!unban` that verifies itself would read, the way the BattlEye client
        re-reads its ban list to confirm a removal.
        """
        return dict(self._bans)

    # -- teams, clans ------------------------------------------------------

    def _on_team_change(self, data: str) -> Event | None:
        """``TEAM CHANGE: <steam id> <team id>``."""
        parts = data.split()
        if len(parts) < 2:
            return None
        client = self._client_by_guid(parts[0])
        if client is None:
            return None
        client.team = self.profile.teams.get(parts[1], "")
        return Event(EventType.CLIENT_TEAM_CHANGE, data=client.team, client=client)

    def _on_clan_change(self, data: str) -> Event | None:
        """``CLAN CHANGE: <steam id> <clan>``.

        No engine here but this one has clans, so it stays a custom event rather than becoming a name
        in the shared vocabulary that only one family could ever emit.
        """
        guid, _, clan = data.partition(" ")
        client = self._client_by_guid(guid.strip())
        if client is None:
            return None
        clan = clan.strip()
        self._stats.setdefault(guid.strip(), {})["clan"] = clan
        return Event(EventType.CUSTOM, data=clan, client=client, extra={"kind": "clan_change"})

    # -- combat ------------------------------------------------------------

    def _on_kill(self, data: str) -> Event | None:
        """``KILL: <killer> <damage type> <victim>``, where either party may be a name or a Steam id.

        `Suicided` as the damage type is how the server reports a player *leaving* as well as a
        genuine self-kill; both are published as a suicide, and the LOGOUT that follows is what
        removes them.
        """
        match = re.match(r"^(?P<attacker>.+?)\s+(?P<weapon>[A-Za-z0-9_-]+)\s+(?P<victim>.+)$", data)
        if match is None:
            log.warning("homefront: could not read a KILL: %r", data)
            return None
        victim = self._resolve(match["victim"])
        if victim is None:
            return None
        kill = KillData(weapon=match["weapon"])
        attacker = self._resolve(match["attacker"])
        if attacker is None or attacker is victim or match["weapon"] == "Suicided":
            return Event(EventType.CLIENT_SUICIDE, data=kill, client=victim, target=victim)
        team_kill = bool(attacker.team) and attacker.team == victim.team
        etype = EventType.CLIENT_KILL_TEAM if team_kill else EventType.CLIENT_KILL
        return Event(etype, data=kill, client=attacker, target=victim)

    # -- the match ---------------------------------------------------------

    def _on_round_over(self, data: str) -> Event | None:
        """``ROUND OVER: <team id>`` — 0 the KPA, 1 the USA, **2 a tie**.

        The winner is *not* looked up in the profile's team table, even though both are small integers
        from the same engine: there, 2 means the spectators. Reporting a drawn round as "spec" is the
        kind of wrong that any plugin counting wins would carry forward silently.
        """
        team = data.strip()
        winner = ROUND_RESULTS.get(team, self.profile.teams.get(team, team))
        return Event(
            EventType.GAME_ROUND_END,
            data=winner,
            extra={"team_id": team, "tie": team == "2"},
        )

    def _on_change_level(self, data: str) -> Event | None:
        """``CHANGE LEVEL: <map>``.

        Published with the cvar-shaped payload the runtime reads to update
        :class:`b3.core.game.Game`, which is how every other family reports a new map.
        """
        return Event(EventType.GAME_ROUND_START, data={"mapname": data.strip().lower()})

    # -- bans the server tells us about ------------------------------------

    def _on_ban_item(self, data: str) -> Event | None:
        """``BAN ITEM: <name> <steam id>`` — one row of the server's ban list."""
        parsed = _split_name_and_id(data)
        if parsed is None:
            return None
        name, guid = parsed
        self._bans[guid] = name
        return None

    def _on_ban_added(self, data: str) -> Event | None:
        """``BAN ADDED: <name> <steam id>``, including bans this bot asked for.

        The classic parser *announced* this to the whole server with `adminbigsay`. That is policy, not
        parsing — and it announced our own bans a second time, after the admin plugin had already said
        so. Reported as an event; anything that wants to shout about it can.
        """
        parsed = _split_name_and_id(data)
        if parsed is None:
            return None
        name, guid = parsed
        self._bans[guid] = name
        return Event(
            EventType.CUSTOM, data=guid, extra={"kind": "ban_added", "name": name, "guid": guid}
        )

    def _on_ban_removed(self, data: str) -> Event | None:
        """``BAN REMOVE: <steam id>``."""
        guid = data.strip()
        self._bans.pop(guid, None)
        return Event(EventType.CUSTOM, data=guid, extra={"kind": "ban_removed", "guid": guid})

    # -- votes -------------------------------------------------------------

    def _on_vote_start(self, data: str) -> Event | None:
        """``VOTESTART: <steam id> <type> [target]``."""
        parts = data.split()
        if not parts:
            return None
        client = self._client_by_guid(parts[0])
        vote_type = parts[1] if len(parts) > 1 else ""
        target = self._client_by_guid(parts[2]) if len(parts) > 2 else None
        return Event(
            EventType.CLIENT_CALLVOTE,
            data=vote_type,
            client=client,
            target=target,
            extra={"arguments": parts[1:]},
        )

    def _on_vote(self, data: str) -> Event | None:
        """``VOTE: <steam id> <0|1>`` — 1 is in favour."""
        parts = data.split()
        if len(parts) < 2:
            return None
        client = self._client_by_guid(parts[0])
        if client is None:
            return None
        return Event(EventType.CLIENT_VOTE, data=parts[1], client=client)

    def _on_vote_end(self, data: str) -> Event | None:
        """``VOTEEND: <yes votes> <percent for> <passed|failed>``.

        Becomes `VOTE_PASSED` or `VOTE_FAILED`, which are in the shared vocabulary — the classic bot
        created its own event for this at runtime, so nothing could listen for a vote on two games.
        """
        parts = data.split()
        if len(parts) < 3:
            return None
        passed = parts[2].strip().lower() == "passed"
        etype = EventType.VOTE_PASSED if passed else EventType.VOTE_FAILED
        return Event(
            etype,
            data=parts[2].strip(),
            extra={"yes_votes": _as_int(parts[0]), "percent_for": parts[1]},
        )

    # -- chat --------------------------------------------------------------

    def _on_chatter(self, data: str) -> Event | None:
        """Everything said on the server, and everything said *by* it.

        Each message arrives twice — once as `BROADCAST: <name> says: <text>`, once as a plain repeat —
        and the bot's own `adminsay` output comes back the same way. Only the BROADCAST form becomes
        chat; the repeat is a custom event, so nothing routes it to the command processor. Without that
        split the bot reads its own announcements as player speech and answers itself.
        """
        if not data.startswith("BROADCAST: "):
            return Event(EventType.CUSTOM, data=data, extra={"kind": "server_say"})
        match = BROADCAST_RE.match(data[len("BROADCAST: ") :])
        if match is None:
            log.debug("homefront: broadcast in no known form: %r", data[:80])
            return None
        client = self.clients.get_by_cid(match["name"].strip())
        if client is None:
            # Chat from somebody the bot never saw connect. The name is all the line carries, and a
            # client invented from it would have no identity — the same call the Quake3 parser makes.
            log.debug("homefront: chat from an unknown player %r", match["name"])
            return None
        scope = match["scope"]
        etype = {
            "team": EventType.CLIENT_TEAM_SAY,
            "squad": EventType.CLIENT_SQUAD_SAY,
        }.get(scope or "", EventType.CLIENT_SAY)
        return Event(etype, data=match["text"], client=client)


def _split_name_and_id(data: str) -> tuple[str, str] | None:
    """``<name> <steam id>`` — split at the *last* space, because a name may contain spaces."""
    name, _, guid = data.strip().rpartition(" ")
    if not guid.isdigit():
        return None
    return name.strip(), guid


def _as_int(value: str, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["BROADCAST_RE", "NOT_AN_ID", "ROUND_RESULTS", "STEAM_ID_RE", "HfParser", "KillData"]
