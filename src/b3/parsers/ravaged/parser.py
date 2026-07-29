"""RavParser — Ravaged's game-log lines, which are regex-shaped after all.

The first family since Quake 3 to use the ``@handles`` router rather than dispatching on a field, and
that is not an accident: the classic parser attached its patterns to handlers with a decorator of its
own (``@ger.gameEvent``), so the grammar transplants almost verbatim. Every pattern below came from
there, and every one is backed by a **captured** line in the classic test suite — this is the first
family built from measured data rather than from a protocol description::

    "<12312312312312312><>" connected, address "192.168.0.1"
    "courgette<12312312312312312><0>" entered the game
    "courgette<12312312312312312><1>" joined team "1"
    "courgette<12312312312312312><0>"disconnected
    "courgette<12312312312312312><1>" say "<FONT COLOR='#FF0000'> hi"
    "Name1<11111111111111><0>" killed "Name2<2222222222222><1>" with "the_weapon"
    "Name1<11111111111111><0>" killed  with UTDmgType_VehicleCollision
    Round finished, winning team is "0"

Four quirks in that data are worth knowing before touching anything here, because each is a place a
reasonable-looking pattern would silently miss lines:

* **`disconnected` has no space before it.** The line really is ``"…<0>"disconnected``.
* **A player's identity is a triple** — ``"<name><steam id><team>"`` — and on the *connect* line the
  name is empty, because the server does not know it yet. The name may also contain spaces.
* **The weapon on a kill line may or may not be quoted.** Both forms appear in the captured data.
* **A kill can have no victim at all**: ``killed  with UTDmgType_VehicleCollision``, with two spaces
  where the victim would be. That is the game killing somebody, and the classic parser read it as a
  suicide — so it is one here too, rather than a kill by a player called "".

Chat arrives wrapped in an HTML colour tag, and team chat is additionally prefixed ``(Team) ``. Both
are stripped, because a plugin matching on ``!command`` cannot be expected to know about either.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.parsers.base import Parser
from b3.parsers.profile import GameProfile
from b3.parsers.registry import handles

log = logging.getLogger(__name__)

#: One row of `getplayerlist`, captured from the classic tests:
#: ``courgette 21 pts 4:8 38ms steamid: 12312312312312312``
PLAYER_ROW_RE = re.compile(
    r"^(?P<name>.+?) "
    r"(?P<score>-?\d+) pts "
    r"(?P<kills>-?\d+):"
    r"(?P<deaths>-?\d+) "
    r"(?P<ping>-?\d+)ms steamid: "
    r"(?P<guid>\d+)$",
    re.MULTILINE,
)

#: One row of `getmaplist false`: ``0 CTR_Canyon``. **Index 0 is the map being played now**, and 1 is
#: the one after it — which is how the classic parser answers both `!map` and `!nextmap` from one call.
MAP_ROW_RE = re.compile(r"^(?P<index>\d+) (?P<map_name>\S+)$", re.MULTILINE)

#: The colour tag the game wraps chat in, and the prefix it puts on team chat.
COLOUR_TAG_RE = re.compile(r"^<FONT COLOR='#[A-F0-9]+'>\s*", re.IGNORECASE)
TEAM_PREFIX = "(Team) "


@dataclass(frozen=True, slots=True)
class KillData:
    """Payload for a Ravaged kill: the damage type, and nothing else.

    The engine reports no damage figure and no hit location, so the classic parser's ``(100, weapon,
    None)`` invented the first and the third.
    """

    weapon: str


class RavParser(Parser):
    """Ravaged. Selected by ``family="ravaged"`` in the profile."""

    # There is no timestamp on these lines, so the base class's stripper has nothing to do — but it is
    # harmless, and overriding it would only invite the question of why.

    def __init__(self, profile: GameProfile, clients: ClientManager | None = None) -> None:
        super().__init__(profile, clients)
        #: Steam id -> the last score/kills/deaths/ping seen in a player-list reply.
        self._stats: dict[str, dict[str, int]] = {}

    # -- identity ----------------------------------------------------------

    def _ensure(self, guid: str, name: str = "", team: str | None = None) -> Client | None:
        """The player with this Steam id, created on first sight.

        ``cid`` *is* the Steam id here, unlike every other family: this engine's own commands take it
        (``kick <steamid>``), and the log lines carry it on every line, so there is nothing to gain by
        keying on the name.
        """
        guid = guid.strip()
        if not guid:
            return None
        client = self.clients.get_by_cid(guid)
        if client is None:
            client = Client(cid=guid, guid=guid, name=name.strip())
            self.clients.add(client)
        elif name.strip():
            client.name = name.strip()
        if team is not None:
            self._set_team(client, team)
        return client

    def _set_team(self, client: Client, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return  # the connect line's empty team: nothing said, so nothing changed
        client.team = self.profile.teams.get(raw, "")

    # -- who is on the server ----------------------------------------------

    @handles(r'^"(?P<name>.*?)<(?P<guid>\d+)><(?P<team>.*)>" connected, address "(?P<ip>\S+)"$')
    def on_connected(self, m: "re.Match[str]") -> Event | None:
        """The connection, which carries the IP — and an **empty name**, since the game has none yet.

        Reported as a connect rather than a join, because that is what it is: the identity is here but
        the player is not in the game until the next line. The classic parser also treated this as the
        moment to create the client, which is why the name being absent matters — a client created with
        an empty name would keep it if nothing filled it in later.
        """
        client = self._ensure(m["guid"], m["name"], m["team"])
        if client is None:
            return None
        client.ip = m["ip"].split(":")[0]
        return Event(EventType.CLIENT_CONNECT, client=client)

    @handles(r'^"(?P<name>.+?)<(?P<guid>\d+)><(?P<team>.*)>" entered the game$')
    def on_entered(self, m: "re.Match[str]") -> Event | None:
        """In the game and playing — and this line has the name, so it is the join."""
        client = self._ensure(m["guid"], m["name"], m["team"])
        if client is None:
            return None
        client.authed = False  # so the runtime authenticates them against the database
        return Event(EventType.CLIENT_JOIN, client=client)

    @handles(r'^"(?P<name>.+?)<(?P<guid>\d+)><(?:.*)>" joined team "(?P<new_team>.+)"$')
    def on_joined_team(self, m: "re.Match[str]") -> Event | None:
        """Note the team is in the *message*, not in the identity triple, which holds the old one."""
        client = self._ensure(m["guid"], m["name"])
        if client is None:
            return None
        self._set_team(client, m["new_team"])
        return Event(EventType.CLIENT_TEAM_CHANGE, data=client.team, client=client)

    @handles(r'^"(?P<name>.+?)<(?P<guid>\d+)><(?P<team>.*)>"\s*disconnected$')
    def on_disconnected(self, m: "re.Match[str]") -> Event | None:
        """``\\s*`` because there is genuinely **no space** before the word in this line."""
        client = self.clients.remove(m["guid"])
        if client is None:
            return None
        self._stats.pop(m["guid"], None)
        return Event(EventType.CLIENT_DISCONNECT, client=client)

    # -- chat --------------------------------------------------------------

    @handles(r'^"(?P<name>.+?)<(?P<guid>\d+)><(?P<team>.*)>" say "(?P<text>.*)"$')
    def on_say(self, m: "re.Match[str]") -> Event | None:
        client = self._ensure(m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(EventType.CLIENT_SAY, data=_clean_chat(m["text"]), client=client)

    @handles(r'^"(?P<name>.+?)<(?P<guid>\d+)><(?P<team>.*)>" say_team "(?P<text>.*)"$')
    def on_say_team(self, m: "re.Match[str]") -> Event | None:
        client = self._ensure(m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(EventType.CLIENT_TEAM_SAY, data=_clean_chat(m["text"]), client=client)

    @handles(r'^Server say "(?P<text>.*)"$')
    def on_server_say(self, m: "re.Match[str]") -> Event | None:
        """The server talking, which includes the bot's own announcements coming back.

        A custom event, not chat, so nothing routes it to the command processor: a bot that read its own
        output as player speech would answer itself. (This server does not echo `say` on the connection
        the way Homefront and Altitude do, but it does write these lines, and the guard costs nothing.)
        """
        return Event(
            EventType.CUSTOM, data=_clean_chat(m["text"]), extra={"kind": "server_say"}
        )

    @handles(r'^Server say_team "(?P<text>.*)" to team "(?P<team>.*)"$')
    def on_server_say_team(self, m: "re.Match[str]") -> Event | None:
        return Event(
            EventType.CUSTOM,
            data=_clean_chat(m["text"]),
            extra={"kind": "server_say", "team": m["team"]},
        )

    # -- combat ------------------------------------------------------------

    @handles(
        r'^"(?P<name_a>.+?)<(?P<guid_a>\d+)><(?P<team_a>.*)>" killed '
        r'"(?P<name_b>.+?)<(?P<guid_b>\d+)><(?P<team_b>.*)>" with "?(?P<weapon>\S+?)"?$'
    )
    def on_killed(self, m: "re.Match[str]") -> Event | None:
        """``with "?…"?`` because the captured data has the weapon both quoted and bare."""
        attacker = self._ensure(m["guid_a"], m["name_a"], m["team_a"])
        victim = self._ensure(m["guid_b"], m["name_b"], m["team_b"])
        if attacker is None or victim is None:
            return None
        kill = KillData(weapon=m["weapon"])
        if attacker is victim:
            return Event(EventType.CLIENT_SUICIDE, data=kill, client=victim, target=victim)
        team_kill = bool(attacker.team) and attacker.team == victim.team
        etype = EventType.CLIENT_KILL_TEAM if team_kill else EventType.CLIENT_KILL
        return Event(etype, data=kill, client=attacker, target=victim)

    @handles(
        r'^"(?P<name>.+?)<(?P<guid>\d+)><(?P<team>.*)>" committed suicide with "(?P<weapon>\S+)"$'
    )
    def on_suicide(self, m: "re.Match[str]") -> Event | None:
        client = self._ensure(m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_SUICIDE,
            data=KillData(weapon=m["weapon"]),
            client=client,
            target=client,
        )

    @handles(r'^"(?P<name>.+?)<(?P<guid>\d+)><(?P<team>.*)>" killed  with (?P<weapon>\S+)$')
    def on_killed_by_the_world(self, m: "re.Match[str]") -> Event | None:
        """A kill with **no victim** — two spaces where one would be — is the world killing them.

        A vehicle collision, in the captured example. Published as a suicide, as the classic parser did:
        there is no attacker to blame and no second player to name, and inventing one called "" would
        put an unnameable client in the database.
        """
        client = self._ensure(m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_SUICIDE,
            data=KillData(weapon=m["weapon"]),
            client=client,
            target=client,
        )

    # -- the match ---------------------------------------------------------

    @handles(r'^Loading map "(?P<map_name>\S+)"$')
    def on_loading_map(self, m: "re.Match[str]") -> Event | None:
        """Published with the cvar-shaped payload the runtime records a map from.

        The gametype comes out of the name: this game's maps are called ``CTR_Canyon`` and
        ``Thrust_Oilrig``, where the prefix *is* the mode — which is how the classic parser derived it,
        there being no other way to ask.
        """
        name = m["map_name"]
        mode = name.split("_", 1)[0] if "_" in name else ""
        return Event(EventType.GAME_ROUND_START, data={"mapname": name, "g_gametype": mode})

    @handles(r"^Round started$")
    def on_round_started(self, m: "re.Match[str]") -> Event | None:
        return Event(EventType.GAME_ROUND_START, data={})

    @handles(r'^Round finished, winning team is "(?P<team>.*)"$')
    def on_round_finished(self, m: "re.Match[str]") -> Event | None:
        team = m["team"].strip()
        return Event(
            EventType.GAME_ROUND_END,
            data=self.profile.teams.get(team, team),
            extra={"team_id": team},
        )

    # -- the rcon connection talking about itself ---------------------------

    @handles(r"^\((?P<ip>.+):(?P<port>\d+) has connected remotely\)$")
    def on_rcon_connected(self, m: "re.Match[str]") -> Event | None:
        """Somebody opened an admin connection — us, or another tool, or a person.

        Worth reporting rather than ignoring: on a server where the bot is the only admin tool, a
        second one appearing is something an operator would want to know about.
        """
        return Event(
            EventType.CUSTOM,
            data=m["ip"],
            extra={"kind": "rcon_connected", "ip": m["ip"], "port": int(m["port"])},
        )

    @handles(r"^RCon:\((?P<login>\S+?)(?P<ip>[0-9.]+):(?P<port>\d+) has disconnected from RCon\)$")
    def on_rcon_disconnected(self, m: "re.Match[str]") -> Event | None:
        """Note the login and the address run together in this line, with no separator at all."""
        return Event(
            EventType.CUSTOM,
            data=m["login"],
            extra={"kind": "rcon_disconnected", "login": m["login"], "ip": m["ip"]},
        )

    # -- the player list, which arrives as a reply rather than an event -----

    @handles(r"^(?P<count>\d+) players:\s*(?P<rows>.*)$", re.DOTALL)
    def on_player_list(self, m: "re.Match[str]") -> Event | None:
        """The reply to `getplayerlist`, routed here like any other line.

        Unlike the other pushed families, this engine *answers questions* — so the client asks for the
        roster on a timer and hands the reply over as though the server had volunteered it. That keeps
        every piece of parsing in the parser and every piece of scheduling in the client, which is the
        split the rest of this bot uses; the alternative was a client that knows how to read a player
        row, or a parser that owns a timer.

        No event: it is an answer, not something that happened. What it updates is the roster read back
        through :meth:`get_players`.
        """
        self.read_players(m.string)
        return None

    def get_players(self) -> list[PlayerInfo]:
        """The roster this parser has assembled, with the last scores and pings the server reported.

        Read by :meth:`b3.runtime.bot.Bot.get_players` through the seam Altitude established. It reports
        the players it knows about and never fewer: the log tells it when somebody leaves (this engine
        reports every disconnect), so the periodic reply is a refresh rather than the authority.
        """
        players: list[PlayerInfo] = []
        for client in self.clients.connected():
            if client.cid is None:
                continue
            stats = self._stats.get(client.cid, {})
            players.append(
                PlayerInfo(
                    cid=client.cid,
                    name=client.name,
                    guid=client.guid,
                    ip=client.ip,
                    ping=stats.get("ping", 0),
                    score=stats.get("score", 0),
                )
            )
        return players

    def read_players(self, reply: str) -> list[PlayerInfo]:
        """Parse a `getplayerlist` reply. Also updates the roster, since it names everyone on it."""
        players: list[PlayerInfo] = []
        for m in PLAYER_ROW_RE.finditer(reply):
            guid, name = m["guid"], m["name"].strip()
            self._ensure(guid, name)
            self._stats[guid] = {
                "score": int(m["score"]),
                "kills": int(m["kills"]),
                "deaths": int(m["deaths"]),
                "ping": int(m["ping"]),
            }
            players.append(
                PlayerInfo(
                    cid=guid,
                    name=name,
                    guid=guid,
                    ping=int(m["ping"]),
                    score=int(m["score"]),
                )
            )
        return players

    @staticmethod
    def read_maps(reply: str) -> list[str]:
        """Parse a `getmaplist false` reply into rotation order. Index 0 is the current map."""
        return [m["map_name"] for m in MAP_ROW_RE.finditer(reply)]


def _clean_chat(text: str) -> str:
    """Strip the game's colour tag, and the team prefix it adds on top of it.

    Both are the game's own decoration. A plugin matching `!command` should not have to know that this
    engine wraps chat in HTML, and the classic parser stripped them for the same reason.
    """
    text = text.strip()
    if text.startswith(TEAM_PREFIX):
        text = text[len(TEAM_PREFIX) :]
    return COLOUR_TAG_RE.sub("", text).strip()


__all__ = ["COLOUR_TAG_RE", "MAP_ROW_RE", "PLAYER_ROW_RE", "KillData", "RavParser"]
