"""Q3Parser — turns Quake3-engine log lines into typed events.

The second engine family. Where a CoD log is semicolon-delimited and repeats a player's identity on
every line, a Quake3 log is space-delimited and gives identity **once**, in an "infostring" when the
player connects or changes something:

    ClientUserinfoChanged: 3 n\\Bob\\t\\2\\cl_guid\\A337702493AF67BB0B0F8565CE8BC6C

Everything after that refers to slot 3 and nothing else. That single difference is why this is a
separate parser rather than another profile: the CoD parser can read a name off any line, and this
one has to remember.

The grammar covers what the classic `q3a/abstractParser.py` handled — connect, userinfo, begin,
disconnect, kill, chat, items, awards, and the round boundaries. Per-title extras (Urban Terror's
radio and hit lines, ET's objectives) belong in subclasses or handlers added later; they are noted
at the bottom of `profiles.py` rather than half-done here.
"""

from __future__ import annotations

import logging
import re

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.base import Parser
from b3.parsers.cod.parser import KillData
from b3.parsers.profile import GameProfile
from b3.parsers.registry import handles

log = logging.getLogger(__name__)

#: `\key\value\key\value`, the Quake3 infostring format.
INFO_PAIR = re.compile(r"\\([^\\]+)\\([^\\]*)")

#: Means-of-death strings that mean the player did it to themselves.
SELF_INFLICTED = frozenset({"MOD_SUICIDE", "MOD_FALLING", "MOD_WATER", "MOD_LAVA", "MOD_SLIME",
                            "MOD_CRUSH", "MOD_TRIGGER_HURT", "MOD_TARGET_LASER"})


def parse_infostring(text: str) -> dict[str, str]:
    """Turn ``\\name\\Bob\\t\\2`` into a dict, tolerating a missing leading backslash."""
    if not text.startswith("\\"):
        text = "\\" + text
    return dict(INFO_PAIR.findall(text))


class Q3Parser(Parser):
    """One parser for every Quake3-engine title; the differences are in the profile."""

    def __init__(
        self,
        profile: GameProfile,
        clients: ClientManager | None = None,
    ) -> None:
        # ET-style servers announce a connection and *then* send the infostring on its own line,
        # so the slot has to be carried between the two.
        self._pending_cid: str | None = None
        self._warned_about_guids = False
        super().__init__(profile, clients)

    # -- helpers -----------------------------------------------------------

    def _get_or_create(self, cid: str, name: str | None = None) -> Client:
        client = self.clients.get_by_cid(cid)
        if client is None:
            client = Client(cid=cid, name=name or "")
            self.clients.add(client)
        elif name:
            client.name = name
        return client

    def _valid_guid(self, guid: str) -> str:
        if len(guid) >= self.profile.guid_min_length:
            return guid
        if guid and not self._warned_about_guids:
            self._warned_about_guids = True
            log.warning(
                "%s: ignoring guid %r — %s expects at least %d characters, so nobody will be "
                "authenticated. Does this server set cl_guid? (some Quake3 titles need "
                "`sv_punkbuster` or an auth mod for stable ids)",
                self.profile.name,
                guid,
                self.profile.name,
                self.profile.guid_min_length,
            )
        return ""

    def _apply_userinfo(self, cid: str, info: dict[str, str]) -> Client:
        """Fold an infostring into the client for that slot — the only place identity arrives."""
        client = self._get_or_create(cid)
        name = info.get("name") or info.get("n")
        if name:
            client.name = name
        guid = self._valid_guid(info.get("cl_guid") or info.get("guid") or "")
        if guid and not client.guid:
            client.guid = guid
        if info.get("ip"):
            client.ip = info["ip"].split(":")[0]
        team = info.get("team") or info.get("t")
        if team is not None:
            client.team = self.profile.teams.get(team, team)
        return client

    # -- client lifecycle ---------------------------------------------------

    @handles(r"^ClientConnect:\s*(?P<cid>\d+)\s*$")
    def on_connect(self, m: "re.Match[str]") -> Event:
        """Slot taken. The infostring may be on this line's heels, so remember the slot."""
        self._pending_cid = m["cid"]
        return Event(EventType.CLIENT_CONNECT, client=self._get_or_create(m["cid"]))

    @handles(r"^ClientUserinfo(?:Changed)?:\s*(?P<cid>\d+)\s*(?P<info>.*)$")
    def on_userinfo(self, m: "re.Match[str]") -> Event:
        client = self._apply_userinfo(m["cid"], parse_infostring(m["info"]))
        return Event(EventType.CLIENT_UPDATE, client=client)

    @handles(r"^Userinfo:\s*(?P<info>\\.*)$")
    def on_bare_userinfo(self, m: "re.Match[str]") -> Event | None:
        """ET sends the infostring on its own line, after ClientConnect/ClientBegin."""
        cid, self._pending_cid = self._pending_cid, None
        if cid is None:
            return None
        return Event(
            EventType.CLIENT_UPDATE, client=self._apply_userinfo(cid, parse_infostring(m["info"]))
        )

    @handles(r"^ClientBegin:\s*(?P<cid>\d+)\s*$")
    def on_begin(self, m: "re.Match[str]") -> Event:
        """The player is in the game. This is the join the rest of the bot cares about."""
        self._pending_cid = m["cid"]
        client = self._get_or_create(m["cid"])
        client.authed = False
        return Event(EventType.CLIENT_JOIN, client=client)

    @handles(r"^ClientDisconnect:\s*(?P<cid>\d+)\s*$")
    def on_disconnect(self, m: "re.Match[str]") -> Event | None:
        client = self.clients.remove(m["cid"])
        if client is None:
            return None
        return Event(EventType.CLIENT_DISCONNECT, client=client)

    # -- combat -------------------------------------------------------------

    @handles(
        r"^Kill:\s*(?P<acid>\d+)\s+(?P<vcid>\d+)\s+(?P<weapon>\d+):\s*"
        # A means-of-death name can contain DIGITS — MOD_MP40, UT_MOD_M4, UT_MOD_AK103,
        # UT_MOD_LR300, UT_MOD_G36, UT_MOD_PSG1, UT_MOD_SR8, UT_MOD_MP5K, UT_MOD_HK69. This class
        # was `[A-Z_]+`, which matched none of them, so kills by most of Urban Terror's weapons
        # produced no event at all. The failure is invisible from the inside: an unmatched line is
        # indistinguishable from a line the game never wrote.
        r"(?P<text>.*?) by (?P<mod>[A-Z0-9_]+)\s*$"
    )
    def on_kill(self, m: "re.Match[str]") -> Event:
        """``Kill: <killer> <victim> <weapon>: <name> killed <name> by <MOD>``.

        The names in the text are decorative — they carry colour codes and can be duplicated — so
        the slot numbers are what identify anybody.
        """
        acid, vcid = m["acid"], m["vcid"]
        victim = self._get_or_create(vcid)
        kill = KillData(
            weapon=m["weapon"], damage=100, hit_location="", means_of_death=m["mod"]
        )

        if acid == self.profile.world_cid or acid == vcid or m["mod"] in SELF_INFLICTED:
            return Event(EventType.CLIENT_SUICIDE, data=kill, client=victim, target=victim)

        attacker = self._get_or_create(acid)
        team_kill = (
            attacker.team is not None
            and attacker.team == victim.team
            and m["mod"] not in self.profile.non_teamkill_weapons
        )
        etype = EventType.CLIENT_KILL_TEAM if team_kill else EventType.CLIENT_KILL
        return Event(etype, data=kill, client=attacker, target=victim)

    # -- chat ----------------------------------------------------------------
    #
    # Quake3 chat lines identify the speaker by *name*, not slot, which is why the bot keeps the
    # name from the last infostring. A name it has never seen produces no event rather than a
    # client with no identity.

    @handles(r"^(?P<action>say|sayteam):\s*(?P<name>.+?):\s?(?P<text>.*)$")
    def on_say(self, m: "re.Match[str]") -> Event | None:
        client = self._by_name(m["name"])
        if client is None:
            return None
        etype = EventType.CLIENT_TEAM_SAY if m["action"] == "sayteam" else EventType.CLIENT_SAY
        return Event(etype, data=m["text"], client=client)

    @handles(r"^tell:\s*(?P<name>.+?)\s+to\s+(?P<tname>.+?):\s?(?P<text>.*)$")
    def on_tell(self, m: "re.Match[str]") -> Event | None:
        client = self._by_name(m["name"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_PRIVATE_SAY,
            data=m["text"],
            client=client,
            target=self._by_name(m["tname"]),
        )

    def _by_name(self, name: str) -> Client | None:
        name = name.strip()
        return next((c for c in self.clients.connected() if c.name == name), None)

    # -- world ----------------------------------------------------------------

    @handles(r"^Item:\s*(?P<cid>\d+)\s+(?P<item>.+?)\s*$")
    def on_item(self, m: "re.Match[str]") -> Event:
        return Event(
            EventType.CLIENT_ITEM_PICKUP, data=m["item"], client=self._get_or_create(m["cid"])
        )

    @handles(r"^Award:\s*(?P<cid>\d+)\s+(?P<award>.+?)\s*$")
    def on_award(self, m: "re.Match[str]") -> Event:
        return Event(
            EventType.CLIENT_ACTION, data=m["award"], client=self._get_or_create(m["cid"])
        )

    @handles(r"^InitGame:\s*(?P<info>.*)$")
    def on_init_game(self, m: "re.Match[str]") -> Event:
        return Event(EventType.GAME_ROUND_START, data=parse_infostring(m["info"]))

    @handles(r"^ShutdownGame:\s*(?P<data>.*)$")
    def on_shutdown(self, m: "re.Match[str]") -> Event:
        return Event(EventType.GAME_ROUND_END, data=m["data"].strip())

    @handles(r"^Exit:\s*(?P<data>.*)$")
    def on_exit(self, m: "re.Match[str]") -> Event:
        """Why the map ended — "Timelimit hit.", "Fraglimit hit." and so on."""
        return Event(EventType.GAME_EXIT, data=m["data"].strip())
