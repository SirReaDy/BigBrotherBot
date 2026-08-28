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

#: `CTF:` action codes, from the classic q3/oa081 parsers.
CTF_ACTIONS = {
    "0": "flag_taken",
    "1": "flag_captured",
    "2": "flag_returned",
    "3": "flag_carrier_kill",
}

#: The `fid` column of a `CTF:` line — which flag, not which team scored.
CTF_FLAG_COLOURS = {"1": "RED", "2": "BLUE"}

#: Means-of-death strings that mean the player did it to themselves.
SELF_INFLICTED = frozenset(
    {
        "MOD_SUICIDE",
        "MOD_FALLING",
        "MOD_WATER",
        "MOD_LAVA",
        "MOD_SLIME",
        "MOD_CRUSH",
        "MOD_TRIGGER_HURT",
        "MOD_TARGET_LASER",
    }
)


def parse_infostring(text: str) -> dict[str, str]:
    """Turn ``\\name\\Bob\\t\\2`` into a dict, tolerating a missing leading backslash."""
    if not text.startswith("\\"):
        text = "\\" + text
    return dict(INFO_PAIR.findall(text))


def chat_text(text: str) -> str:
    """Drop the leading 0x15 some Quake3 clients prefix chat with.

    Without this a command arrives as ``\\x15!help``, whose first character is no longer the command
    prefix, so it is read as ordinary chat and never runs.
    """
    return text[1:] if text[:1] == "\x15" else text


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
        #: Where each player was last hit, by slot, for the engines that report hits separately from
        #: kills. A `Kill:` line names the weapon and never the part of the body, so without this a
        #: kill carries no hit location at all and nothing downstream can tell a headshot from a shot
        #: in the foot. Urban Terror is the case (see `b3.parsers.q3.urt`); the classic bot did the
        #: same thing under the name `lastDamageTaken`.
        self._last_hit: dict[str, str] = {}
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

    def _bounded_name(self, name: str, cid: str) -> tuple[str, bool]:
        """Cut a name to the length the protocol allows, and report whether it had to be cut.

        Urban Terror 4.2 lets a client connect with a nickname longer than the 32 characters the
        userinfo string holds, overflowing it (BigBrotherBot/big-brother-bot#346). The name is
        always truncated, since it reaches RCON command lines and any formatted table. Whether the
        player is kicked as well is a configuration question, so it is left to the runtime.
        """
        limit = self.profile.name_max_length
        if not limit or len(name) <= limit:
            return name, False
        log.warning(
            "%s: slot %s has a %d-character name, over this engine's limit of %d — truncating. "
            "This is the userinfo-overflow exploit; set server.allow_long_names: true to allow it "
            "instead of kicking.",
            self.profile.name,
            cid,
            len(name),
            limit,
        )
        return name[:limit], True

    def _apply_userinfo(self, cid: str, info: dict[str, str]) -> tuple[Client, bool, bool]:
        """Fold an infostring into the client for that slot — the only place identity arrives.

        Returns the client and whether this line *moved* or *renamed* them. Neither is a log line of
        its own on this engine: the team and the name are fields of the infostring, and the only way
        to know somebody switched or renamed is that a field now says something else.
        """
        client = self._get_or_create(cid)
        name = info.get("name") or info.get("n")
        renamed = False
        if name:
            before = client.name
            client.name, client.name_overflow = self._bounded_name(name, cid)
            # A name the bot already knew, now different. Not the first one it learns: that is the
            # player arriving, which `CLIENT_AUTH` already covers for everything that cares.
            renamed = bool(before) and before != client.name
        guid = self._valid_guid(info.get("cl_guid") or info.get("guid") or "")
        if guid and not client.guid:
            client.guid = guid
        if info.get("ip"):
            client.ip = info["ip"].split(":")[0]
        if info.get("gear"):
            client.gear = info["gear"]
        # `authl` is the player's **Frozen Sand account name**, and Urban Terror 4.2/4.3 put it in
        # the userinfo the server already sends. Recorded on `pbid` — the second-identity field, the
        # same one PunkBuster fills — because that is exactly what it is: an account survives a new
        # `cl_guid`, which is what makes it worth more than the id a ban is keyed on today.
        #
        # This is the half of §1.3's open Frozen Sand item that needs **no** network at all. The
        # account *service* is still unported, because nothing here can confirm Frozen Sand's
        # servers still answer; the account *name* was arriving in every one of these lines and
        # being thrown away. The classic reads it here too, and skips its own auth query when it is
        # present — which is the evidence that this field is the authoritative spelling.
        if info.get("authl"):
            client.pbid = info["authl"]
        team = info.get("team") or info.get("t")
        moved = False
        if team is not None:
            mapped = self.profile.teams.get(team, team)
            moved = mapped != client.team
            client.team = mapped
        return client, moved, renamed

    def read_userinfo(self, cid: str, reply: str) -> str | None:
        """Turn a ``dumpuser <cid>`` reply into the log line it is the same information as.

        This engine answers `dumpuser` with a fixed-width table rather than an infostring::

            userinfo
            --------
            ip                  62.235.246.103:27960
            name                Shinki
            cl_guid             8982B13A8DCEE4C77A32E6AC4DD7EEDF

        The key ends at column 20 and the value is the rest, which is what makes it fixed-width
        rather than whitespace-separated: a player named ``Bob the Builder`` has spaces in the value
        and a split on whitespace would keep only the first word of it.

        The reply is rebuilt into a ``ClientUserinfo:`` line and handed back rather than applied
        here, so that identity arriving this way goes through *exactly* the same path as identity
        arriving from the log — the guid length check, the name truncation, the team mapping and the
        event. A second implementation of that would be a second place for it to be wrong.

        Returns None when the slot holds nobody. The engine says so with a line of prose rather than
        an error, and the classic parser's note records what it means: the player has gone, but their
        body is still in the game.
        """
        lines = [line.rstrip() for line in reply.splitlines() if line.strip()]
        if not lines or lines[0].strip() != "userinfo":
            log.debug("%s: dumpuser %s answered %r", self.profile.name, cid, reply.strip()[:120])
            return None
        pairs: list[str] = []
        for line in lines[1:]:
            if line.strip() == "--------":
                continue
            key, value = line[:20].strip(), line[20:].strip()
            if key:
                pairs.append(f"\\{key}\\{value}")
        if not pairs:
            return None
        return f"ClientUserinfo: {cid} {''.join(pairs)}"

    # -- client lifecycle ---------------------------------------------------

    @handles(r"^ClientConnect:\s*(?P<cid>\d+)\s*$")
    def on_connect(self, m: "re.Match[str]") -> Event:
        """Slot taken. The infostring may be on this line's heels, so remember the slot."""
        self._pending_cid = m["cid"]
        return Event(EventType.CLIENT_CONNECT, client=self._get_or_create(m["cid"]))

    @handles(r"^ClientUserinfo(?:Changed)?:\s*(?P<cid>\d+)\s*(?P<info>.*)$")
    def on_userinfo(self, m: "re.Match[str]") -> list[Event]:
        client, moved, renamed = self._apply_userinfo(m["cid"], parse_infostring(m["info"]))
        return self._userinfo_events(client, moved, renamed)

    @handles(r"^Userinfo:\s*(?P<info>\\.*)$")
    def on_bare_userinfo(self, m: "re.Match[str]") -> list[Event]:
        """ET sends the infostring on its own line, after ClientConnect/ClientBegin."""
        cid, self._pending_cid = self._pending_cid, None
        if cid is None:
            return []
        client, moved, renamed = self._apply_userinfo(cid, parse_infostring(m["info"]))
        return self._userinfo_events(client, moved, renamed)

    def _userinfo_events(self, client: Client, moved: bool, renamed: bool) -> list[Event]:
        """The update, and a team change or a rename when the line carried one.

        This family published **no** ``CLIENT_TEAM_CHANGE`` at all, which is not a gap anybody can
        see: subscribers simply never ran. `poweradminurt`'s ``!paforce … lock`` — whose whole point
        is to put a player back when they switch — had therefore never held anybody on Urban Terror,
        and `afk` never counted a team change as a sign of life. The classic bot raised this from the
        ``Client.team`` setter, so every parser got it free; here each parser says it, and this one
        had not been told to.

        A player whose team becomes known for the first time counts as having changed: joining a team
        *is* the move a balancer has to react to, and the classic said so the same way — its clients
        started on ``TEAM_UNKNOWN`` and the first real team fired the event. A *name* becoming known
        for the first time does not: that is the player arriving, which `CLIENT_AUTH` already covers
        for the two plugins that check a name on the way in.

        ``CLIENT_NAME_CHANGE`` had the same problem as the team change and for the same reason — only
        the Source parser published it — so `censor` could not catch somebody who connected with a
        clean name and then changed it, `nickreg` could not catch somebody putting on an admin's name
        mid-session, and `afk` did not count renaming as a sign of life.
        """
        events = [Event(EventType.CLIENT_UPDATE, client=client)]
        if moved:
            events.append(Event(EventType.CLIENT_TEAM_CHANGE, data=client.team, client=client))
        if renamed:
            events.append(Event(EventType.CLIENT_NAME_CHANGE, data=client.name, client=client))
        return events

    @handles(r"^ClientBegin:\s*(?P<cid>\d+)\s*$")
    def on_begin(self, m: "re.Match[str]") -> Event:
        """The player is in the game. This is the join the rest of the bot cares about."""
        self._pending_cid = m["cid"]
        client = self._get_or_create(m["cid"])
        client.authed = False
        return Event(EventType.CLIENT_JOIN, client=client)

    @handles(r"^ClientDisconnect:\s*(?P<cid>\d+)\s*$")
    def on_disconnect(self, m: "re.Match[str]") -> Event | None:
        self._last_hit.pop(m["cid"], None)  # the slot may be somebody else's in a moment
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
            weapon=m["weapon"],
            damage=100,
            # Consumed, not read: the next kill of this player is a different life, and a hit
            # location left lying around would be attributed to it.
            hit_location=self._last_hit.pop(vcid, ""),
            means_of_death=m["mod"],
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
    # Quake3's own chat line identifies the speaker by name, which is why the bot keeps the name
    # from the last infostring. A name it has never seen produces no event rather than a client
    # with no identity. Urban Terror and World of Padman put the slot in front of the name; both
    # forms are handled here rather than in a subclass, since they share one verb.

    @handles(r"^(?P<action>say|sayteam):\s*(?:(?P<cid>\d+)\s+)?(?P<rest>.+)$")
    def on_say(self, m: "re.Match[str]") -> Event | None:
        """``say: Bob: hello``, or ``say: 6 ^5Marcel^2[^6CZARMY^2]: !help`` where a slot is given.

        The speaker and the text are split by :meth:`_split_chat` rather than by the pattern. A
        name is free text on this engine and **a colon is legal in one**, so no regex can tell the
        colon that ends the name from one inside it.
        """
        found = self._split_chat(m["cid"], m["rest"])
        if found is None:
            return None
        client, text = found
        etype = EventType.CLIENT_TEAM_SAY if m["action"] == "sayteam" else EventType.CLIENT_SAY
        return Event(etype, data=chat_text(text), client=client)

    def _split_chat(self, cid: str | None, rest: str) -> tuple[Client, str] | None:
        """Split ``<name>: <text>`` when the name may itself contain a colon.

        The names the bot already knows are tried first, **longest first**, which is the same rule
        the BattlEye parser follows and for the same reason: the engine names the speaker rather
        than numbering them, so the only reliable place to end the name is where a name it knows
        ends. A pattern splitting at the first colon read `joe:foo: !help` as *joe* saying
        `foo: !help`, which meant **a player with a colon in their name could not use a single
        command** — the prefix was no longer at the front of what the command processor was handed.
        The captured tests are a list of the spellings that broke: `joe:`, `jo:e`, `j:oe`,
        `joe:foo`, each of them with and without a colon in the message too.

        Matching by name before slot is deliberate and pre-dates this: Urban Terror sometimes
        reports a chat line against the *wrong* slot, and commands run with the speaker's permission
        level, so attributing a line to whoever happens to occupy that slot is worth guarding
        against. The slot is the fallback for a name the bot has not seen.
        """
        for client in sorted(self.clients.connected(), key=lambda c: len(c.name), reverse=True):
            if client.name and rest.startswith(f"{client.name}:"):
                return client, rest[len(client.name) + 1 :].lstrip(" ")
        # A name we do not know. The slot is all that is left, and it is only worth trusting when
        # the server gave one — otherwise this is chat from somebody who never connected.
        by_cid = self.clients.get_by_cid(cid) if cid is not None else None
        if by_cid is None:
            return None
        _name, sep, text = rest.partition(":")
        if not sep:
            return None
        return by_cid, text.lstrip(" ")

    @handles(r"^tell:\s*(?P<name>.+?)\s+to\s+(?P<tname>.+?):\s?(?P<text>.*)$")
    def on_tell(self, m: "re.Match[str]") -> Event | None:
        client = self._by_name(m["name"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_PRIVATE_SAY,
            data=chat_text(m["text"]),
            client=client,
            target=self._by_name(m["tname"]),
        )

    def _chat_client(self, cid: str | None, name: str) -> Client | None:
        """Who spoke, given a slot that may be absent and may be wrong.

        Urban Terror sometimes reports a chat line against the wrong slot, so the name is checked
        against it and wins where the two disagree. Commands run with the speaker's permission
        level, which makes attributing a line to the wrong player worth guarding against.
        """
        by_name = self._by_name(name)
        if cid is None:
            return by_name
        by_cid = self.clients.get_by_cid(cid)
        if by_cid is not None and by_cid.name == name.strip():
            return by_cid
        return by_name if by_name is not None else by_cid

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
        return Event(EventType.CLIENT_ACTION, data=m["award"], client=self._get_or_create(m["cid"]))

    @handles(r"^CTF:\s+(?P<cid>\d+)\s+(?P<fid>\d+)\s+(?P<type>\d+):\s*(?P<text>.*)$")
    def on_ctf(self, m: "re.Match[str]") -> Event | None:
        """``CTF: 2 2 1: Burpman captured the BLUE flag!`` — slot, flag team, action.

        Capture-the-flag on plain Quake 3 and OpenArena; Urban Terror writes ``Flag:`` instead.

        The flag's colour is taken from the ``fid`` column rather than from the sentence, which is
        the server's English announcement and would not match on a translated or modded server.
        """
        action = CTF_ACTIONS.get(m["type"], f"flag_action_{m['type']}")
        if action == "flag_returned":
            # Returned by the game rather than credited to a player, as `Flag Return:` is.
            return Event(
                EventType.GAME_FLAG_RETURNED, data=CTF_FLAG_COLOURS.get(m["fid"], m["fid"])
            )
        return self._action(m["cid"], action)

    def _action(self, cid: str, action: str) -> Event | None:
        """Publish a player objective as CLIENT_ACTION, the way every other engine reports one."""
        client = self.clients.get_by_cid(cid)
        if client is None:
            return None
        return Event(EventType.CLIENT_ACTION, data=action, client=client)

    @handles(r"^InitGame:\s*(?P<info>.*)$")
    def on_init_game(self, m: "re.Match[str]") -> Event:
        # Nobody carries a wound across a round: a player killed by the fall damage of the next map
        # would otherwise be reported as having been shot in the head on the last one.
        self._last_hit.clear()
        return Event(EventType.GAME_ROUND_START, data=parse_infostring(m["info"]))

    @handles(r"^ShutdownGame:\s*(?P<data>.*)$")
    def on_shutdown(self, m: "re.Match[str]") -> Event:
        return Event(EventType.GAME_ROUND_END, data=m["data"].strip())

    @handles(r"^Exit:\s*(?P<data>.*)$")
    def on_exit(self, m: "re.Match[str]") -> Event:
        """Why the map ended — "Timelimit hit.", "Fraglimit hit." and so on."""
        return Event(EventType.GAME_EXIT, data=m["data"].strip())
