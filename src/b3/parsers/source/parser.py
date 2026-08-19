"""SourceParser — the Half-Life log standard, which every Source title writes.

The grammar is documented (``HL_Log_Standard`` on Valve's wiki) *and* captured, which is a first
here: `tests/test_insurgency.py` re-expresses the classic bot's ``test_insurgency.py`` line by line,
so every pattern below is backed by a line a real server actually wrote. The shape that matters is
the identity quad, and it is on almost every line::

    "courgette<194><STEAM_1:0:1111111><#Team_Security>" say "!help"
     name     cid   guid              team

Five things in the captured data would each cost a whole category of events if taken from a careless
reading, and every one of them is silent:

* **The fourth field is not always a team.** A bot-stuck line carries a *number* there
  (``"Minh<338><BOT><193>"``), and a ``STEAM USERID validated`` line repeats the **Steam id**
  (``"courgette<18><STEAM_1:1:1111111><STEAM_1:1:1111111>"``). So an unrecognised token must leave the
  player's team **alone** rather than clear it — which the captured tests state outright: after
  ``#Team_Security``, a token of ``f00`` leaves them on Security. Blanking on unknown, which is what
  the other families here do, would drop half the roster out of its team on a coop server.
* **A cvar reply uses ``=``, not ``is``** (``"tv_password" = ""``), including the empty value and
  values with spaces (``"nextlevel" = "heights_coop checkpoint"``).
* **The status table has two row shapes** and an AI row has no ping, rate or address at all. See
  `b3.parsers.source.profiles.SOURCE_STATUS_ROW_RE`; getting it wrong evicts every bot on every sync.
* **A kill line may carry coordinates**, in square brackets, after *either* player.
* **A death caused by the map is a suicide with ``"world"``**, naming one player; there is no world
  slot to attribute it to.

And one fault in the classic parser, of the kind this project keeps finding: its ignore list contains
``^Dropped .+ from server (Disconnected\\.)$`` with the parentheses **unescaped**, so it is a group
rather than a literal and the pattern can only match ``…from serverDisconnected.`` — a line no server
writes. That line was therefore never ignored and was reported as unhandled for the whole life of the
parser. It is escaped here.

Two deliberate departures from the classic, both about not lying:

* ``GR_STATE_STARTGAME`` published ``EVT_CLIENT_JOIN`` for **one arbitrary player** there (a ``return``
  inside the loop meant whoever the roster listed first, and nobody else). A join for one player at
  match start is not a fact about anything, so this publishes the round start and leaves the roster to
  `Bot.sync`.
* A kick or ban seen in the log is published as a **custom** event rather than as ``CLIENT_KICK`` /
  ``CLIENT_BAN_TEMP``. Those two are what the bot publishes when *it* penalises somebody, and the
  server echoes our own ``sm_kick``/``sm_addban`` back down this log — so reusing them would record
  every penalty twice. A console kick, which no bot issued, keeps ``CLIENT_KICK``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.base import Parser
from b3.parsers.profile import GameProfile
from b3.parsers.registry import handles

log = logging.getLogger(__name__)

#: The identity quad, as a reusable fragment. The name is greedy so that a name containing ``<`` or
#: ``>`` still resolves — the last position that lets the rest of the line match is the right one.
_WHO = r'"(?P<name>.+)<(?P<cid>\d+)><(?P<guid>[^>]*)><(?P<team>[^>]*)>"'

#: Optional world coordinates, which some builds append to each player on a kill line.
_AT = r"(?: \[-?\d+ -?\d+ -?\d+\])?"

#: A trailing property list: ``(headshot)``, ``(name "#unknown_controlpoint")``. A key with no value
#: is a boolean true, which is the HL standard's own rule.
PROPERTY_RE = re.compile(r'\((?P<key>[^\s()]+)(?P<sep>| "(?P<value>[^"]*)")\)')

#: One row of a ``maps *`` reply: ``PENDING:  (fs) buhriz.bsp``.
MAP_ROW_RE = re.compile(r"^PENDING:\s+\(fs\)\s+(?P<map_name>.+)\.bsp\s*$", re.MULTILINE)

#: One row of a ``sm plugins list`` reply: ``01 "B3 Say" (1.0.0) by Courgette``. The classic
#: parser's pattern, kept as it was. The quotes are what make it readable: a plugin name contains
#: spaces, so anything splitting on whitespace finds "B3 and stops.
SM_PLUGIN_ROW_RE = re.compile(
    r'^(?P<index>.+) "(?P<name>.+)" \((?P<version>.+)\) by (?P<author>.+)$', re.MULTILINE
)

#: Lines that are real, understood, and of no interest — matched so that `b3 probe` reports them as
#: handled rather than as gaps in the grammar. One alternation rather than a dozen handlers, because
#: nothing here needs telling apart.
IGNORED = (
    r"^(?:"
    r"//.*"  # a comment in the log file
    r"|server cvars (?:start|end)"
    r"|\[basechat\.smx\] .*"
    r"|\[META\] Loaded \d+ plugins? ?(?:\(\d+ already loaded\))?\.?"
    r"|Log file (?:started|closed).*"
    r"|\s*path_goal .*"
    r"|Vote succeeded.*"
    r'|".+" STEAM USERID validated'
    r"|Dropped .+ from server \(.*\)"  # the classic left these parentheses unescaped
    r"|Molotov projectile spawned at .*"
    r"|server_message: \".*\".*"
    r")$"
)

#: Gamerules states, and what each one means. ``GR_STATE_PREROUND`` is deliberately mapped to
#: nothing: it is the countdown, and publishing a round start for it as well as for
#: ``GR_STATE_RND_RUNNING`` would double every round.
GAMERULES = {
    "GR_STATE_PREGAME": EventType.GAME_WARMUP,
    "GR_STATE_STARTGAME": EventType.GAME_ROUND_START,
    "GR_STATE_PREROUND": None,
    "GR_STATE_RND_RUNNING": EventType.GAME_ROUND_START,
    "GR_STATE_POSTROUND": EventType.GAME_ROUND_END,
    "GR_STATE_GAME_OVER": EventType.GAME_EXIT,
}

#: Team triggers that really do end a round. Gated, unlike the *player* triggers below, because this
#: one drives logic: publishing a round end for an unrecognised team trigger would run every
#: end-of-round handler in the bot at a moment when the round did not end.
ROUND_END_TRIGGERS = frozenset(
    {
        "Round_Win",
        "obj_captured",
        "obj_destroyed",
        "SFUI_Notice_Target_Saved",
        "SFUI_Notice_Target_Bombed",
        "SFUI_Notice_Terrorists_Win",
        "SFUI_Notice_CTs_Win",
        "SFUI_Notice_Bomb_Defused",
        "SFUI_Notice_Round_Draw",
    }
)

#: The disconnect reason the stock console kick produces. Not one of ours: `sm_kick` sends the
#: admin's reason, so this really is somebody typing at the server console.
CONSOLE_KICK_REASON = "Kicked by Console"


@dataclass(frozen=True, slots=True)
class KillData:
    """Payload for a Source kill.

    Two fields, because two are what the engine reports. There is **no damage figure** on this
    protocol — the classic parser passed a hard-coded ``100``, which reads downstream as a fact — and
    the hit location is known only as a boolean: the line carries ``(headshot)`` or it does not.
    """

    weapon: str
    hit_location: str


def parse_properties(text: str) -> dict[str, str | bool]:
    """Read a trailing property list. A bare ``(key)`` means true, per the HL log standard."""
    found: dict[str, str | bool] = {}
    for match in PROPERTY_RE.finditer(text or ""):
        found[match["key"]] = True if match["sep"] == "" else (match["value"] or "")
    return found


class SourceParser(Parser):
    """Insurgency and Counter-Strike 2. Selected by ``family="source"`` in the profile."""

    #: ``L 04/01/2014 - 12:56:51: `` — nothing like the ``mm:ss`` prefix the Tech 3 engines use. Some
    #: lines (the Gamerules ones) arrive with no prefix at all, which costs nothing: `re.sub` leaves a
    #: line it does not match alone.
    _timestamp_re = re.compile(r"^L \d{2}/\d{2}/\d+ - \d{2}:\d{2}:\d{2}:\s*")

    def __init__(self, profile: GameProfile, clients: ClientManager | None = None) -> None:
        super().__init__(profile, clients)
        #: Cvar values seen in the log, so a value the server volunteered does not need asking for.
        self.cvars: dict[str, str] = {}
        #: Every map the server has installed, from the last `maps *` reply.
        self._maps: list[str] = []
        #: Team tokens this title's table does not have, so each is complained about once.
        self._unknown_teams: set[str] = set()

    # -- identity ----------------------------------------------------------

    def _valid_guid(self, guid: str) -> bool:
        """Whether this id identifies a person.

        Three things fail: a bot's shared ``BOT``, the server's own ``Console``, and anything too
        short to be a real Steam id. All three would otherwise be authenticated — and since every bot
        on the server reports the same one, they would share a single database row with one level and
        one ban history between them.
        """
        guid = guid.strip()
        if not guid or len(guid) < self.profile.guid_min_length:
            return False
        return not self.profile.is_bot_guid(guid)

    def _ensure(
        self, cid: str, guid: str, name: str = "", team: str | None = None
    ) -> Client | None:
        """The player in this slot, created on first sight.

        Slots are reused, so a slot holding a *different* id is somebody else: the stale record is
        dropped rather than renamed, because inheriting it would give the new arrival the previous
        player's level and ban history.
        """
        cid = cid.strip()
        if not cid:
            return None
        # Folded to one spelling before anything is compared or stored, because the comparison two
        # lines down decides whether this is the same player: CS2 writes `[U:1:N]` here and answers
        # `status` with a Steam64, so without this the sync below would read every player as a
        # stranger who had taken their slot, and drop the record it had just built. A no-op on every
        # title that reports one form (see `GameProfile.canonical_guid`).
        guid = self.profile.canonical_guid(guid.strip())
        client = self.clients.get_by_cid(cid)
        if client is not None and guid and client.guid and client.guid != guid:
            self.clients.remove(cid)
            client = None
        if client is None:
            client = Client(
                cid=cid,
                guid=guid if self._valid_guid(guid) else "",
                name=name.strip(),
                # Recorded before the guid is dropped: `BOT` is every bot's guid on this engine, so
                # afterwards there is nothing left to tell an AI player from an unidentified one.
                is_bot=self.profile.is_bot_guid(guid.strip()),
            )
            self.clients.add(client)
        else:
            if name.strip():
                client.name = name.strip()
            if not client.guid and self._valid_guid(guid):
                client.guid = guid
        if team is not None:
            self._set_team(client, team)
        return client

    def _set_team(self, client: Client, raw: str) -> None:
        """Apply a team token, **ignoring one this title does not know**.

        The captured tests are explicit about this and it is not the obvious behaviour: a token of
        ``f00`` leaves the player where they were. It has to work that way, because the fourth field
        of the identity quad is not always a team — a bot-stuck line puts a number there and a
        validation line repeats the Steam id — so treating "not in the table" as "no team" would keep
        wiping the team of anyone those lines mention.
        """
        token = raw.strip()
        if token in self.profile.teams:
            client.team = self.profile.teams[token]

    def _team_name(self, raw: str) -> str:
        return self.profile.teams.get(raw.strip(), "")

    # -- lines of no interest ---------------------------------------------

    @handles(IGNORED)
    def on_ignored(self, m: "re.Match[str]") -> None:
        """Understood and uninteresting. See :data:`IGNORED`."""
        return None

    # -- cvars -------------------------------------------------------------

    @handles(r'^"(?P<name>[^"\s]+)" = "(?P<value>.*)"$')
    def on_cvar(self, m: "re.Match[str]") -> Event | None:
        """``"nextlevel" = "heights_coop checkpoint"`` — note the value may hold spaces, or nothing."""
        self.cvars[m["name"]] = m["value"]
        return None

    @handles(r'^server_cvar: "(?P<name>[^"\s]+)" "(?P<value>.*)"$')
    def on_server_cvar(self, m: "re.Match[str]") -> Event | None:
        self.cvars[m["name"]] = m["value"]
        return None

    # -- who is on the server ----------------------------------------------

    @handles(rf'^{_WHO} connected, address "(?P<ip>.+)"$')
    def on_connected(self, m: "re.Match[str]") -> Event | None:
        """The connection, and the only log line carrying an address.

        ``"none"`` is what a bot's address is, so it is not recorded — an AI player with an IP would
        be authenticated by it on a title configured for IP identification.
        """
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        ip = m["ip"].strip()
        if ip and ip != "none":
            client.ip = ip.split(":")[0]
        return Event(EventType.CLIENT_CONNECT, client=client)

    @handles(rf"^{_WHO} entered the game$")
    def on_entered(self, m: "re.Match[str]") -> Event | None:
        """In the game and playing — the join, and where authentication starts."""
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        client.authed = False
        return Event(EventType.CLIENT_JOIN, client=client)

    @handles(rf'^{_WHO} disconnected \(reason "(?P<reason>.*)"\)$')
    def on_disconnected(self, m: "re.Match[str]") -> list[Event]:
        """Leaving, with the reason — which is how a console kick is recognised at all.

        Both events are published, kick first, because the kick is why the disconnect happened. The
        client is removed only after they are built, so both carry a resolvable player.
        """
        reason = m["reason"]
        client = self.clients.get_by_cid(m["cid"].strip())
        if client is None:
            return []
        events: list[Event] = []
        if reason == CONSOLE_KICK_REASON:
            # Nobody's `sm_kick`: this is the stock console verb, so it is a genuine third-party
            # penalty and keeps the event the bot uses for its own.
            events.append(Event(EventType.CLIENT_KICK, data=reason, client=client))
        self.clients.remove(m["cid"].strip())
        events.append(Event(EventType.CLIENT_DISCONNECT, data=m["cid"].strip(), client=client))
        return events

    @handles(rf'^{_WHO} changed name to "(?P<new_name>.*)"$')
    def on_name_change(self, m: "re.Match[str]") -> Event | None:
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        new_name = m["new_name"].strip()
        client.name = new_name
        return Event(EventType.CLIENT_NAME_CHANGE, data=new_name, client=client)

    @handles(rf'^{_WHO} joined team "(?P<new_team>[^"]*)"$')
    def on_joined_team(self, m: "re.Match[str]") -> Event | None:
        """The team is in the message; the quad still holds the old one."""
        return self._change_team(m["cid"], m["guid"], m["name"], m["team"], m["new_team"])

    @handles(
        r'^"(?P<name>.+)<(?P<cid>\d+)><(?P<guid>[^>]*)>(?:<(?P<team>[^>]*)>)?" '
        r"switched from team <(?P<from_team>[^>]*)> to <(?P<new_team>[^>]*)>$"
    )
    def on_switched_team(self, m: "re.Match[str]") -> Event | None:
        """The same event said differently — and the identity here may carry **three fields or four**.

        The captured line has four (``"courgette<194><STEAM_1:0:1111111><CT>" switched from team
        <TERRORIST> to <Unassigned>``), which is where the classic parser went wrong: its pattern
        allowed three and used a greedy ``.+`` for the id, so on this line the id it captured was
        ``STEAM_1:0:1111111><CT`` — the Steam id with the team glued to it. That matches no database
        record, so a player switching teams was liable to be created a second time under a corrupt id,
        losing their level and their bans for the rest of the session. Making the fourth field
        *optional* is what reads both shapes without inventing an id.

        The team switched **to** is taken from the message, not from the identity: the two disagree on
        the captured line, and the message is the statement of what just happened.
        """
        return self._change_team(m["cid"], m["guid"], m["name"], m["team"] or "", m["new_team"])

    def _change_team(
        self, cid: str, guid: str, name: str, old_team: str, new_team: str
    ) -> Event | None:
        if new_team.strip() not in self.profile.teams:
            # Nothing changed, so nothing is published: a team-change event carrying the team they
            # were already on is a statement that something happened when it did not. Warned once per
            # token instead, because the only way this line is reached is a token missing from the
            # title's table — which is a gap worth being told about rather than a quiet no-op.
            token = new_team.strip()
            if token not in self._unknown_teams:
                self._unknown_teams.add(token)
                log.warning(
                    "source: %s has no team called %r; team changes to it are being ignored",
                    self.profile.name,
                    token,
                )
            return None
        if self._team_name(new_team) == "":
            # Moving to no team is also what the server writes as somebody *leaves*, so this must not
            # be the line that creates a client — it would resurrect a player who has just gone.
            client = self.clients.get_by_cid(cid.strip())
            if client is None:
                return None
            self._set_team(client, new_team)
        else:
            client = self._ensure(cid, guid, name, old_team)
            if client is None:
                return None
            self._set_team(client, new_team)
        return Event(EventType.CLIENT_TEAM_CHANGE, data=client.team, client=client)

    # -- chat --------------------------------------------------------------

    @handles(rf'^{_WHO} say "(?P<text>.*)"$')
    def on_say(self, m: "re.Match[str]") -> Event | None:
        """Public chat. The console talks on this line too, and is not a player."""
        if not self._valid_guid(m["guid"]) and m["guid"].strip() == "Console":
            return None
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(EventType.CLIENT_SAY, data=m["text"], client=client)

    @handles(rf'^{_WHO} say_team "(?P<text>.*)"$')
    def on_say_team(self, m: "re.Match[str]") -> Event | None:
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(EventType.CLIENT_TEAM_SAY, data=m["text"], client=client)

    # -- combat ------------------------------------------------------------

    @handles(
        rf'^"(?P<name>.+)<(?P<cid>\d+)><(?P<guid>[^>]*)><(?P<team>[^>]*)>"{_AT} killed '
        rf'"(?P<v_name>.+)<(?P<v_cid>\d+)><(?P<v_guid>[^>]*)><(?P<v_team>[^>]*)>"{_AT} '
        r'with "(?P<weapon>[^"]*)"(?P<properties>.*)$'
    )
    def on_killed(self, m: "re.Match[str]") -> Event | None:
        """A kill, with the coordinates both players may carry allowed for and discarded.

        Classified as a team kill only when both teams are *known* and equal: on this engine an
        unassigned player has an empty team, and comparing two empties would report every early-round
        kill between two unassigned players as a team kill.
        """
        attacker = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        victim = self._ensure(m["v_cid"], m["v_guid"], m["v_name"], m["v_team"])
        if attacker is None or victim is None:
            return None
        props = parse_properties(m["properties"])
        kill = KillData(
            weapon=m["weapon"], hit_location="head" if props.get("headshot") else "body"
        )
        if attacker.cid == victim.cid:
            return Event(EventType.CLIENT_SUICIDE, data=kill, client=victim, target=victim)
        team_kill = bool(attacker.team) and attacker.team == victim.team
        etype = EventType.CLIENT_KILL_TEAM if team_kill else EventType.CLIENT_KILL
        return Event(etype, data=kill, client=attacker, target=victim)

    @handles(
        r'^"(?P<name>.+)<(?P<cid>\d+)><(?P<guid>[^>]*)><(?P<team>[^>]*)>" assisted killing '
        r'"(?P<v_name>.+)<(?P<v_cid>\d+)><(?P<v_guid>[^>]*)><(?P<v_team>[^>]*)>"(?P<properties>.*)$'
    )
    def on_assist(self, m: "re.Match[str]") -> Event | None:
        attacker = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        victim = self._ensure(m["v_cid"], m["v_guid"], m["v_name"], m["v_team"])
        if attacker is None or victim is None:
            return None
        return Event(
            EventType.CLIENT_ASSIST, data="assisted killing", client=attacker, target=victim
        )

    @handles(rf'^{_WHO}{_AT} committed suicide with "(?P<weapon>[^"]*)"$')
    def on_suicide(self, m: "re.Match[str]") -> Event | None:
        """Including a death caused by the map, whose weapon is ``world``."""
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_SUICIDE,
            data=KillData(weapon=m["weapon"], hit_location="body"),
            client=client,
            target=client,
        )

    # -- things players do -------------------------------------------------

    @handles(rf'^{_WHO} triggered "(?P<action>[^"]+)"(?P<properties>.*)$')
    def on_triggered(self, m: "re.Match[str]") -> Event | None:
        """Every player-triggered action, named rather than filtered against a list.

        The classic published only a fixed set of names and warned about the rest, which means a
        title with an objective it had never seen produced nothing at all. ``CLIENT_ACTION`` carries
        the name, so passing an unrecognised one through is harmless and strictly more useful — unlike
        the *team* triggers, which drive round-end logic and stay gated.
        """
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        action = m["action"]
        props = parse_properties(m["properties"])
        if action == "clantag":
            # No field on Client for this, so it is published rather than stored: dropping it would
            # lose the only place the engine states a player's clan.
            return Event(
                EventType.CUSTOM,
                data=str(props.get("value", "")),
                client=client,
                extra={"kind": "clantag"},
            )
        if action in ("weaponstats", "weaponstats2"):
            # SourceMod's SuperLogs, when installed. Its own kind so a stats plugin can find it
            # without having to know that this engine calls it a trigger.
            return Event(
                EventType.CUSTOM, data=action, client=client, extra={"kind": action, **props}
            )
        return Event(EventType.CLIENT_ACTION, data=action, client=client, extra=dict(props))

    @handles(rf'^{_WHO} purchased "(?P<item>[^"]*)"$')
    def on_purchased(self, m: "re.Match[str]") -> Event | None:
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_ACTION, data="purchased", client=client, extra={"item": m["item"]}
        )

    @handles(rf"^{_WHO} threw (?P<item>.+?){_AT}$")
    def on_threw(self, m: "re.Match[str]") -> Event | None:
        """``threw molotov [59 386 -225]`` — the item is bare here, not quoted."""
        client = self._ensure(m["cid"], m["guid"], m["name"], m["team"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_ACTION,
            data="threw",
            client=client,
            extra={"item": m["item"].strip()},
        )

    @handles(rf"^{_WHO} stuck \(position \"[^\"]*\"\) \(duration \"[^\"]*\"\)(?: .*)?$")
    def on_stuck(self, m: "re.Match[str]") -> Event | None:
        """An AI player wedged in the scenery. Understood, and nothing to do about it.

        Matched rather than left to fall through because these arrive in *bulk* on a coop server, and
        an unmatched line is what `b3 probe` reports as a hole in the grammar. Note the trailing
        ``.*``: the engine sometimes appends the next log line's ``path_goal`` to this one.
        """
        return None

    # -- the match ---------------------------------------------------------

    @handles(r"^Gamerules: entering state '(?P<state>[^']+)'$")
    def on_gamerules(self, m: "re.Match[str]") -> Event | None:
        state = m["state"]
        if state not in GAMERULES:
            log.warning(
                "source: unknown Gamerules state %r; if this is a real state it wants a mapping",
                state,
            )
            return None
        etype = GAMERULES[state]
        if etype is None:
            return None
        if etype is EventType.GAME_ROUND_START:
            # No cvars to report from this line: an empty dict means "a round started" and leaves the
            # map alone, where a dict with a `mapname` would be read as a map change.
            return Event(etype, data={})
        return Event(etype)

    @handles(r'^Loading map "(?P<map_name>[^"]+)"$')
    def on_loading_map(self, m: "re.Match[str]") -> Event | None:
        return Event(EventType.GAME_ROUND_START, data={"mapname": m["map_name"]})

    @handles(r"^-+ Mapchange to (?P<map_name>\S+) -+$")
    def on_mapchange(self, m: "re.Match[str]") -> Event | None:
        return Event(EventType.GAME_ROUND_START, data={"mapname": m["map_name"]})

    @handles(r'^Started map "(?P<map_name>[^"]+)" \(CRC "-?\d+"\)$')
    def on_started_map(self, m: "re.Match[str]") -> Event | None:
        """The map is up. Carries the name too, so a bot that missed the load still learns it."""
        return Event(EventType.GAME_ROUND_START, data={"mapname": m["map_name"]})

    @handles(r'^Team "(?P<team>[^"]+)" triggered "(?P<action>[^"]+)"(?P<properties>.*)$')
    def on_team_triggered(self, m: "re.Match[str]") -> Event | None:
        action = m["action"]
        if action not in ROUND_END_TRIGGERS:
            log.info("source: team trigger %r is not treated as a round end", action)
            return None
        return Event(
            EventType.GAME_ROUND_END,
            data={
                "team": self._team_name(m["team"]),
                "event_name": action,
                "properties": parse_properties(m["properties"]),
            },
        )

    @handles(r'^Team "(?P<team>[^"]+)" scored "(?P<points>-?\d+)" with "(?P<players>\d+)" players$')
    def on_team_score(self, m: "re.Match[str]") -> Event | None:
        """Understood; the bot keeps no team scoreboard, so there is nothing to record it in."""
        return None

    # -- the server talking about itself -----------------------------------

    @handles(r'^rcon from "(?P<ip>[^"]+):(?P<port>\d+)":\s*Bad Password$')
    def on_bad_rcon_password(self, m: "re.Match[str]") -> Event | None:
        """Somebody's password was refused — possibly ours, which is worth saying loudly.

        The log is the only place this is visible when the bot uses fire-and-forget writes, since
        nothing reads the reply to those.
        """
        log.error(
            "source: the server refused an RCON password from %s; if that is this bot, "
            "server.rcon_password is wrong",
            m["ip"],
        )
        return Event(EventType.CUSTOM, data=m["ip"], extra={"kind": "bad_rcon_password"})

    @handles(r'^rcon from "(?P<ip>[^"]+):(?P<port>\d+)": command "(?P<cmd>.*)"$')
    def on_rcon(self, m: "re.Match[str]") -> Event | None:
        """Every command the bot sends is echoed here. Recognised so it is not reported as a gap."""
        return None

    @handles(
        r'^Banid: "(?P<name>.+)<(?P<cid>\d+)><(?P<guid>[^>]*)><(?P<team>[^>]*)>" was banned '
        r'"(?P<duration>[^"]*)" by "(?P<admin>[^"]*)"$'
    )
    def on_banid(self, m: "re.Match[str]") -> Event | None:
        """The server recording a ban — including the echo of the bot's own ``sm_addban``.

        Custom rather than ``CLIENT_BAN_TEMP`` precisely because of that echo: the bot publishes that
        event when it bans somebody and writes the penalty itself, so treating this line as a ban too
        would record every one of them twice.
        """
        return Event(
            EventType.CUSTOM,
            data=m["guid"],
            client=self.clients.get_by_cid(m["cid"].strip()),
            extra={
                "kind": "server_ban",
                "guid": m["guid"],
                "duration": m["duration"],
                "admin": m["admin"],
            },
        )

    @handles(
        r'^\[basecommands\.smx\] ".*<\d+><[^>]*><[^>]*>" kicked '
        r'"(?P<name>.+)<(?P<cid>\d+)><(?P<guid>[^>]*)><(?P<team>[^>]*)>"(?P<properties>.*)$'
    )
    def on_sm_kicked(self, m: "re.Match[str]") -> Event | None:
        """SourceMod announcing a kick, ours included — so custom, for the reason above."""
        props = parse_properties(m["properties"])
        return Event(
            EventType.CUSTOM,
            data=str(props.get("reason", "")),
            client=self.clients.get_by_cid(m["cid"].strip()),
            extra={"kind": "server_kick", "guid": m["guid"], **props},
        )

    @handles(r"^(?P<data>Your server (?:needs to be restarted|is out of date).*)$")
    def on_restart_required(self, m: "re.Match[str]") -> Event | None:
        """The server asking to be restarted for an update. An operator wants to know."""
        log.warning("source: %s", m["data"])
        return Event(EventType.CUSTOM, data=m["data"], extra={"kind": "restart_required"})

    # -- answers to questions ----------------------------------------------

    def read_maps(self, reply: str) -> list[str]:
        """Parse a ``maps *`` reply into the maps the server has installed.

        Read by `Bot.get_maps` through the seam Ravaged established. **Not a rotation** — it is
        everything on disk, in whatever order the filesystem gave — which is why the profile also
        names `next_map_cvar`: deriving "next" from this order would state a falsehood about the
        rotation rather than decline to answer.
        """
        self._maps = [m["map_name"].strip() for m in MAP_ROW_RE.finditer(reply)]
        return list(self._maps)

    def read_installed_mods(self, reply: str) -> list[str]:
        """Names of the SourceMod plugins a ``sm plugins list`` reply reports.

        The row shape is SourceMod's, and the classic parser's regex for it, verbatim::

            01 "B3 Say" (1.0.0) by Courgette

        Only the **name** is returned. The classic kept the index, version and author too and never
        read any of them; what the decision actually turns on is whether a plugin is present, so
        that is what this answers. See `Bot.apply_optional_mods`.

        The quoting is what makes this readable at all: a plugin name has spaces in it, so anything
        splitting on whitespace would find "B3" and never match.
        """
        return [m["name"] for m in SM_PLUGIN_ROW_RE.finditer(reply)]


__all__ = [
    "CONSOLE_KICK_REASON",
    "GAMERULES",
    "IGNORED",
    "MAP_ROW_RE",
    "PROPERTY_RE",
    "ROUND_END_TRIGGERS",
    "SM_PLUGIN_ROW_RE",
    "KillData",
    "SourceParser",
    "parse_properties",
]
