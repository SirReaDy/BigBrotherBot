"""FlParser — Frontlines: Fuel of War, whose roster reply *is* its event stream.

Every other family here learns about a player from a line that names the event: a connect, a join, a
disconnect. **This engine has none of those.** Nothing is written when somebody arrives and nothing is
written when they leave; the only statement of who is playing is the ``PlayerList:`` reply, which the
client asks for every three seconds. So this parser does what no other one has to: it **diffs the
roster** and publishes the joins and departures itself.

That has two consequences worth being explicit about, because both are the kind of thing that quietly
produces a wrong answer:

* **The refresh interval is the event latency.** A join is reported up to one interval late, and so is a
  part. The classic parser polled every three seconds for exactly this reason, and it is why
  `b3.net.frontline.PLAYERLIST_INTERVAL` is short enough to look wasteful.
* **A truncated reply must not empty the room.** If the roster were taken at face value, one short read
  during a map change would report every player as having left, and the bot would announce a dozen
  departures that never happened. The reply states its own count — ``Players=7/32`` — so this parser
  prunes *only* when the number of rows it parsed matches the number the server said it was sending,
  and says so in the log when they disagree.

The rest of the grammar:

* Identity is three things at once. ``ID`` is the slot (the ``cid``, and it is reused by the next player
  to take that slot), ``ProfileID`` is the persistent account id (the ``guid``, which is what bans are
  keyed on), and chat lines carry **only the name** — so chat is resolved by exact name.
* The roster carries no IP address. That comes from PunkBuster's own lines, when PunkBuster logging is
  on, and from nowhere else.
* Nothing is reported at all until the bot sends ``CHATLOGGING TRUE`` and ``DebugLogging TRUE``; that
  is `b3.net.frontline.FrontlineClient.open`'s job, not this parser's, but it explains why a silent
  server is a client problem rather than an empty one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.net.frontline import RECONNECTED_NOTICE
from b3.parsers.base import Parser
from b3.parsers.profile import GameProfile
from b3.parsers.registry import handles

log = logging.getLogger(__name__)

#: The header of a `PLAYERLIST` reply::
#:
#:     PlayerList: Map=CQ-Gnaw Time=739 Players=0/32 Tickets=500,500 Round=2/3
#:
#: Every field is optional to *us* except the counts, because a build that drops one should not cost us
#: the roster. Note `Tickets` is a **pair**: the classic parser's own pattern asked for `,\\d+` and so
#: could never match, which is why its map name, round number and slot count were never recorded.
PLAYERLIST_HEADER_RE = re.compile(
    r"^PlayerList:"
    r"(?:\s+Map=(?P<map_name>\S+))?"
    r"(?:\s+Time=(?P<remaining>-?\d+))?"
    r"(?:\s+Players=(?P<players>\d+)/(?P<slots>\d+))?"
    r"(?:\s+Tickets=(?P<tickets_a>-?\d+),(?P<tickets_b>-?\d+))?"
    r"(?:\s+Round=(?P<round>\d+)/(?P<rounds>\d+))?"
)

#: The columns of a roster row, tab separated, in the order the server sends them.
PLAYER_COLUMNS = (
    "ID",
    "Name",
    "Ping",
    "Team",
    "Squad",
    "Score",
    "Kills",
    "Deaths",
    "TK",
    "CP",
    "Time",
    "Idle",
    "Loadout",
    "Role",
    "RoleLvl",
    "Vehicle",
    "Hash",
    "ProfileID",
)

#: A row of a `MapList` reply: `<index>\\t<name>\\t<gametype>`.
MAP_ROW_RE = re.compile(r"^(?P<index>\d+)\t(?P<map_name>[^\t]+)(?:\t(?P<gametype>[^\t]*))?$")

#: How the game reports a duration that means "for ever".
PERMANENT_DURATION = "-1"


@dataclass(frozen=True, slots=True)
class ServerBan:
    """A ban the *server* reports, which may not be one the bot asked for."""

    guid: str
    name: str
    #: Minutes, or None for permanent.
    minutes: int | None
    pb_hash: str = ""


class FlParser(Parser):
    """Frontlines: Fuel of War. Selected by ``family="frontline"`` in the profile."""

    #: These lines carry no timestamp, and one of them (`Time=739`) would be eaten by a stripper that
    #: went looking for `\\d+:\\d+`. Nothing to strip, so nothing is stripped.
    _timestamp_re = re.compile(r"(?!)")  # matches nothing

    def __init__(self, profile: GameProfile, clients: ClientManager | None = None) -> None:
        super().__init__(profile, clients)
        #: slot -> the numbers from the last roster reply.
        self._stats: dict[str, dict[str, int]] = {}
        #: The rotation, from the last `MapList` reply. Read back through :meth:`get_maps`.
        self._maps: list[str] = []
        #: What the server last said the current and next maps are.
        self.current_map = ""
        self.next_map = ""
        #: The round number from the last roster header, so a new round is distinguishable from the
        #: same round being reported again three seconds later.
        self._round = ""
        #: Set once a roster reply has been read, so an empty room is distinguishable from ignorance.
        self._roster_known = False
        #: slot -> (ip, punkbuster id, name) seen before the roster knew that slot existed.
        #:
        #: PunkBuster computes a player's GUID the moment they connect, which is *before* the next
        #: roster refresh names them -- and it is the only time an IP is ever reported on this engine.
        #: Dropping those lines because the slot is not known yet costs that player their address for
        #: the whole session, so they wait here instead. The name is kept as the check: slots are
        #: reused, and applying the previous occupant's address to the next one would be worse than
        #: having none.
        self._pending_pb: dict[str, tuple[str, str, str]] = {}

    # -- identity ----------------------------------------------------------

    def _by_name(self, name: str) -> Client | None:
        """The connected player with exactly this name — chat lines carry nothing else."""
        name = name.strip()
        for client in self.clients.connected():
            if client.name == name:
                return client
        return None

    def _team(self, raw: str) -> str:
        return self.profile.teams.get(raw.strip(), "")

    # -- the roster, which is this engine's whole event stream --------------

    @handles(r"^PlayerList:(?P<rest>.*)$", re.DOTALL | re.IGNORECASE)
    def on_player_list(self, m: "re.Match[str]") -> list[Event]:
        """The reply to `PLAYERLIST`, and the only place a join or a part is ever visible.

        Returns the events the *difference* implies: a join for every slot that was not there before, a
        disconnect for every one that has gone. Also updates the map and round from the header, which
        is the only place those appear either.
        """
        events: list[Event] = []
        lines = m.string.splitlines()
        header = PLAYERLIST_HEADER_RE.match(lines[0]) if lines else None

        if header is not None:
            events.extend(self._read_header(header))

        rows = self._read_rows(lines)
        stated = int(header["players"]) if header is not None and header["players"] else None

        # Joins first: a slot that nobody was in before.
        for cid, row in rows.items():
            existing = self.clients.get_by_cid(cid)
            guid = row.get("ProfileID", "").strip()
            name = row.get("Name", "").strip()
            if existing is None:
                if not guid or guid == "0":
                    # No account id yet — the player is still connecting. Skipped rather than added
                    # with a blank guid, which would be a client the database cannot key on. The
                    # classic parser meant to do this and compared its string to the integer 0.
                    log.debug("frontline: slot %s has no ProfileID yet; not a player yet", cid)
                    continue
                client = Client(cid=cid, guid=guid, name=name)
                client.team = self._team(row.get("Team", ""))
                self.clients.add(client)
                # Their numbers come from this same row. Recording them only on a *later* refresh
                # would leave anybody who joins and does nothing reporting a ping of zero for ever --
                # and the ping is one of the two things this reply exists to carry.
                self._record_stats(cid, row)
                self._apply_pending_pb(client, name)
                events.append(Event(EventType.CLIENT_JOIN, client=client))
            else:
                events.extend(self._update(existing, row))

        # Then departures, but only if the reply can be trusted to be complete.
        if stated is not None and len(rows) != stated:
            log.warning(
                "frontline: the roster said %d players and carried %d rows; keeping everyone, "
                "because pruning on a short read would report departures that never happened",
                stated,
                len(rows),
            )
        else:
            for client in list(self.clients.connected()):
                if client.cid is not None and client.cid not in rows:
                    gone = self.clients.remove(client.cid)
                    self._stats.pop(client.cid, None)
                    self._pending_pb.pop(client.cid, None)
                    if gone is not None:
                        events.append(Event(EventType.CLIENT_DISCONNECT, client=gone))

        self._roster_known = True
        return events

    def _read_header(self, header: "re.Match[str]") -> list[Event]:
        """Map, round and ticket state — none of which is reported anywhere else on this engine.

        Published **only when it changes**, which is the point worth being careful about: this header
        arrives every three seconds, and a round-start event on each of them would reset the round
        clock continuously and make every "round lasted N minutes" figure read three seconds.
        """
        cvars: dict[str, str] = {}
        map_name = (header["map_name"] or "").strip()
        if map_name:
            cvars["mapname"] = map_name
        if header["slots"]:
            cvars["sv_maxclients"] = header["slots"]
        if header["round"] and header["rounds"]:
            cvars["g_currentround"] = header["round"]
            cvars["g_maxrounds"] = header["rounds"]
        if header["tickets_a"] and header["tickets_b"]:
            cvars["tickets"] = f"{header['tickets_a']},{header['tickets_b']}"
        if header["remaining"]:
            cvars["remaining_time"] = header["remaining"]

        new_map = bool(map_name) and map_name != self.current_map
        new_round = bool(header["round"]) and header["round"] != self._round
        self.current_map = map_name or self.current_map
        self._round = header["round"] or self._round
        if not (new_map or new_round):
            return []
        # A map change and a round change are the same event to the runtime, which tells them apart by
        # whether `mapname` differs from what it had — see `Bot._on_round_start`.
        return [Event(EventType.GAME_ROUND_START, data=cvars)]

    def _read_rows(self, lines: list[str]) -> dict[str, dict[str, str]]:
        """The rows of a roster reply, keyed by slot.

        Line 0 is the header and line 1 the column names, which are read rather than assumed: a build
        that adds a column would otherwise shift every field silently.
        """
        if len(lines) < 2:
            return {}
        columns = tuple(part.strip() for part in lines[1].split("\t"))
        if "ID" not in columns or "ProfileID" not in columns:
            log.warning(
                "frontline: the roster's column header is not recognised (%r); "
                "falling back to the documented order",
                lines[1][:120],
            )
            columns = PLAYER_COLUMNS

        rows: dict[str, dict[str, str]] = {}
        for line in lines[2:]:
            if not line.strip():
                continue
            values = line.split("\t")
            row = dict(zip(columns, values, strict=False))
            cid = row.get("ID", "").strip()
            if not cid:
                log.debug("frontline: a roster row with no slot: %r", line[:120])
                continue
            rows[cid] = row
        return rows

    def _update(self, client: Client, row: dict[str, str]) -> list[Event]:
        """Fold a roster row into a player we already know. Reports a team change if there is one."""
        events: list[Event] = []
        name = row.get("Name", "").strip()
        if name and client.name != name:
            client.name = name
        team = self._team(row.get("Team", ""))
        if team and client.team != team:
            client.team = team
            events.append(Event(EventType.CLIENT_TEAM_CHANGE, data=team, client=client))
        if client.cid is not None:
            self._record_stats(client.cid, row)
        return events

    def _record_stats(self, cid: str, row: dict[str, str]) -> None:
        """The numbers from one roster row. Nothing else on this engine reports any of them."""
        self._stats[cid] = {
            "ping": _int(row.get("Ping")),
            "score": _int(row.get("Score")),
            "kills": _int(row.get("Kills")),
            "deaths": _int(row.get("Deaths")),
            "team_kills": _int(row.get("TK")),
        }

    def get_players(self) -> list[PlayerInfo]:
        """The roster, with the numbers from the last reply. Read by `Bot.get_players`."""
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

    # -- chat --------------------------------------------------------------

    @handles(
        r'^CHAT: PlayerName="(?P<name>[^"]*)" Channel="(?P<channel>[^"]*)" Message="(?P<text>.*)"$',
        re.DOTALL,
    )
    def on_chat(self, m: "re.Match[str]") -> Event | None:
        """Chat, which names the player and nothing else — so it is resolved by exact name.

        A player the bot has not seen in a roster reply yet produces no event rather than a nameless
        client: this engine reuses slot numbers, and inventing an identity from a name would attach
        somebody's history to whoever is holding that name now.
        """
        client = self._by_name(m["name"])
        if client is None:
            log.debug("frontline: chat from %r, who is not on the roster yet", m["name"])
            return None
        channel = m["channel"].strip()
        text = m["text"]
        if channel.lower() == "say":
            return Event(EventType.CLIENT_SAY, data=text, client=client)
        if channel.lower() in ("teamsay", "squadsay"):
            # Squad chat is not public, and this bot has no narrower vocabulary for it. Reported as
            # team chat with the channel kept, rather than as a broadcast it never was.
            return Event(
                EventType.CLIENT_TEAM_SAY, data=text, client=client, extra={"channel": channel}
            )
        # A channel nobody has seen. Published rather than dropped, so `b3 probe` shows it exists.
        log.info("frontline: chat on an unknown channel %r", channel)
        return Event(
            EventType.CUSTOM,
            data=text,
            client=client,
            extra={"kind": "chat", "channel": channel},
        )

    # -- the server's own bookkeeping --------------------------------------

    @handles(
        r'^Banned Player: PlayerName="(?P<name>[^"]*)" PlayerID=(?P<cid>-1|\d+) '
        r"ProfileID=(?P<guid>\d+) Hash=(?P<pb_hash>\S*) BanDuration=(?P<duration>-?\d+)"
        r"(?P<permanent> Permanently)?$"
    )
    def on_server_ban(self, m: "re.Match[str]") -> Event | None:
        """The server confirming a ban — which may be one an admin made at the console, not ours.

        Published as a custom event carrying the ban, rather than swallowed: a ban the bot did not make
        is exactly the thing an operator wants to see in the log, and the bot's own bans are recorded
        from the command side regardless.
        """
        minutes = (
            None if m["duration"] == PERMANENT_DURATION or m["permanent"] else int(m["duration"])
        )
        ban = ServerBan(guid=m["guid"], name=m["name"], minutes=minutes, pb_hash=m["pb_hash"])
        return Event(EventType.CUSTOM, data=ban, extra={"kind": "server_ban", "guid": ban.guid})

    @handles(
        r'^Kicked Player as part of ban: PlayerName="(?P<name>[^"]*)" PlayerID=(?P<cid>-1|\d+) '
        r"ProfileID=(?P<guid>\d+) Hash=(?P<pb_hash>\S*) BanDuration=(?P<duration>-?\d+)$"
    )
    def on_kicked_for_ban(self, m: "re.Match[str]") -> Event | None:
        """The kick half of a ban. Understood and deliberately quiet: the ban line that follows says
        the same thing, and reporting both would double every ban in the log.

        Matched all the same, because the alternative is `b3 probe` calling it an unknown line — and
        the classic parser's pattern did not cover it, so it warned "please report this" on every ban.
        """
        return None

    @handles(
        r'^UnBanned Player: PlayerName="(?P<name>[^"]*)" PlayerID=(?:-1|\d+) '
        r"ProfileID=(?P<guid>\d+) Hash=(?P<pb_hash>\S*)$"
    )
    def on_server_unban(self, m: "re.Match[str]") -> Event | None:
        return Event(
            EventType.CUSTOM,
            data=m["guid"],
            extra={"kind": "server_unban", "guid": m["guid"], "name": m["name"]},
        )

    @handles(r"^UnBan failed! Player ProfileID or Hash is not banned: (?P<guid>.*)$")
    def on_unban_failed(self, m: "re.Match[str]") -> Event | None:
        """Reported rather than ignored, which is what the classic parser did with it.

        An `!unban` that the server refuses is otherwise indistinguishable from one that worked: the
        bot clears its own record either way, and nobody finds out until the player walks back in.
        """
        log.warning("frontline: the server says %s was not banned", m["guid"].strip())
        return Event(
            EventType.CUSTOM,
            data=m["guid"].strip(),
            extra={"kind": "server_unban_failed", "guid": m["guid"].strip()},
        )

    # -- maps --------------------------------------------------------------

    @handles(r"^MapList:(?P<rest>.*)$", re.DOTALL | re.IGNORECASE)
    def on_map_list(self, m: "re.Match[str]") -> Event | None:
        """The rotation. Kept, not published: it is a fact about the server, not something that
        happened. Read back through :meth:`get_maps`.

        Every row is taken. The classic parser kept only names beginning `FL-`, which drops the
        `CQ-` maps its own docstring quotes — so `!maps` would have shown a fraction of the rotation.
        """
        maps: list[str] = []
        for line in m.string.splitlines()[2:]:
            row = MAP_ROW_RE.match(line.rstrip())
            if row is not None:
                maps.append(row["map_name"].strip())
        if maps:
            self._maps = maps
        return None

    def get_maps(self) -> list[str]:
        """The rotation as last reported. Read by `Bot.get_maps`."""
        return list(self._maps)

    @handles(r"^CurrentMap is: (?P<map_name>.+)$")
    def on_current_map(self, m: "re.Match[str]") -> Event | None:
        self.current_map = m["map_name"].strip()
        return None

    @handles(r"^NextMap is: (?P<map_name>.+)$")
    def on_next_map(self, m: "re.Match[str]") -> Event | None:
        self.next_map = m["map_name"].strip()
        return None

    def get_next_map(self) -> str | None:
        """What the server last said is next. Read by `Bot.get_next_map`."""
        return self.next_map or None

    @handles(r"^Forced transition to next map$")
    def on_forced_transition(self, m: "re.Match[str]") -> Event | None:
        """A map change was ordered. It does not name the new map — the next roster header does — so
        this only invalidates what we thought was current."""
        self.current_map = ""
        self.next_map = ""
        return None

    # -- PunkBuster, which is the only source of an IP address --------------

    @handles(
        r"^(?:.*: )?Player GUID Computed (?P<pbid>[0-9a-f]+)\(-\) \(slot #(?P<cid>\d+)\) "
        r"(?P<ip>[0-9.]+):(?P<port>\d+) (?P<name>.+)$"
    )
    def on_punkbuster_guid(self, m: "re.Match[str]") -> Event | None:
        """PunkBuster naming a slot's address — **the only place an IP appears on this engine.**

        Without PunkBuster logging on, no player here ever gets an IP, which is worth knowing before
        relying on anything that needs one.

        This line normally arrives *before* the roster reply that introduces the player, because
        PunkBuster computes the GUID at connect time and the roster is only asked for every few
        seconds. So an unknown slot is the common case rather than the odd one, and the address is
        held until the roster catches up.
        """
        return self._note_address(m["cid"], m["ip"], m["pbid"], m["name"])

    @handles(
        r"^(?:.*: )?(?P<cid>\d+)\s+(?P<pbid>[a-z0-9]{32})?\(-\)\s+(?P<ip>[0-9.]+):(?P<port>\d+)\s+"
        r"\S+\s+\d+\s+[\d.]+\s+\d+\s+\(.\)\s+\"(?P<name>.+)\"$"
    )
    def on_punkbuster_row(self, m: "re.Match[str]") -> Event | None:
        """A row of PunkBuster's own player list, which carries the same address information."""
        return self._note_address(m["cid"], m["ip"], m["pbid"] or "", m["name"])

    def _note_address(self, cid: str, ip: str, pbid: str, name: str) -> Event | None:
        """Attach an address to a slot, or hold it until the roster names that slot."""
        client = self.clients.get_by_cid(cid)
        if client is None:
            self._pending_pb[cid] = (ip, pbid, name.strip())
            return None
        client.ip = ip
        if pbid:
            client.pbid = pbid
        return Event(EventType.CLIENT_UPDATE, client=client)

    def _apply_pending_pb(self, client: Client, name: str) -> None:
        """Give a freshly-joined player the address PunkBuster reported before we knew them.

        Only if the name matches. A slot that changed hands between the PunkBuster line and the roster
        reply would otherwise hand one player another's address, which is a worse answer than none.
        """
        held = self._pending_pb.pop(client.cid or "", None)
        if held is None:
            return
        ip, pbid, held_name = held
        if held_name and held_name != name:
            log.debug(
                "frontline: dropping a held address for slot %s: PunkBuster said %r, the roster says "
                "%r, so the slot changed hands",
                client.cid,
                held_name,
                name,
            )
            return
        client.ip = ip
        if pbid:
            client.pbid = pbid

    # -- the connection talking about itself --------------------------------

    @handles(rf"^{RECONNECTED_NOTICE}$")
    def on_reconnected(self, m: "re.Match[str]") -> list[Event]:
        """The client telling us it had to reconnect, so the roster is worthless.

        It matters more here than in any other family: this engine reports no departures at all, so
        every player who left while the socket was down would otherwise stay on the roster for ever.
        Each is reported as a disconnect rather than dropped in silence — a plugin counting time
        played should see them go.
        """
        events: list[Event] = []
        for client in list(self.clients.connected()):
            if client.cid is None:
                continue
            gone = self.clients.remove(client.cid)
            self._stats.pop(client.cid, None)
            if gone is not None:
                events.append(Event(EventType.CLIENT_DISCONNECT, client=gone))
        self._roster_known = False
        self._pending_pb.clear()
        if events:
            log.info("frontline: dropped %d player(s) the reconnect made unverifiable", len(events))
        return events

    @handles(
        r"^WELCOME! Frontlines: Fuel of War \(RCON\) VER=(?P<version>\S+) CHALLENGE=(?P<rest>.*)$"
    )
    def on_welcome(self, m: "re.Match[str]") -> Event | None:
        """Understood and quiet. The client consumes this during the login; it reaches the parser only
        if something is out of step, and a challenge is not for the log."""
        return None

    @handles(r"^Login SUCCESS! User:(?P<user>.*)$", re.IGNORECASE)
    def on_login_success(self, m: "re.Match[str]") -> Event | None:
        return None

    @handles(r"^(?P<name>\w+) now (?P<value>.*)$")
    def on_server_var(self, m: "re.Match[str]") -> Event | None:
        """`ChatLogging now TRUE`, and its two siblings. Reported, because if these are ever *off*
        the bot goes deaf and this line is the only warning."""
        log.info("frontline: %s is now %s", m["name"], m["value"].strip())
        return Event(
            EventType.CUSTOM,
            data=m["value"].strip(),
            extra={"kind": "server_var", "name": m["name"]},
        )

    @handles(r"^DEBUG: (?P<channel>[A-Za-z]+): (?P<text>.*)$", re.DOTALL)
    def on_debug(self, m: "re.Match[str]") -> Event | None:
        """The engine's debug log, which this connection carries whether we want it or not.

        Matched so that `b3 probe` can tell noise from a gap in the grammar: without a handler, every
        one of these counts as a line nobody understood, and the two or three that matter would be
        lost in them. `RendezVous` is the one with meaning — it names a profile id that has just
        arrived — but the roster is asked for every few seconds anyway, so it adds nothing but latency
        to act on it here.
        """
        return None


def _int(value: str | None) -> int:
    """A number from a roster cell, or 0.

    Tolerant on purpose: every one of these columns is a statistic, and a build that writes `n/a` in
    one of them should cost that cell rather than the whole roster — which is the only thing on this
    engine that reports who is playing at all.
    """
    try:
        return int((value or "").strip())
    except ValueError:
        return 0


__all__ = [
    "MAP_ROW_RE",
    "PLAYERLIST_HEADER_RE",
    "PLAYER_COLUMNS",
    "FlParser",
    "ServerBan",
]
