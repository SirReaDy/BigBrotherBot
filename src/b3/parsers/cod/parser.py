"""CodParser — turns Infinity-Ward CoD log lines into typed events.

CoD server logs are semicolon-delimited (unlike space-delimited Quake3). This parser declares the
grammar with :func:`handles` and preserves the hard-won classification quirks documented in the
legacy ``cod.py``/``cod4.py``:

* attacker cid ``-1`` (world) or attacker == victim  -> suicide;
* same-team kill -> team kill, **but only when both teams are known** (CoD4 omits the attacker team
  on kill lines) and the weapon isn't an objective explosion (``briefcase_bomb_mp``);
* a stray ``0x15`` control byte prefixed to chat text is stripped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from b3.core.clients import ClientManager
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.base import Parser
from b3.parsers.cod.auth import AuthManager
from b3.parsers.cod.profile import GameProfile
from b3.parsers.registry import handles

log = logging.getLogger(__name__)

# Sentinel attacker cid for a world/environment kill.
WORLD_CID = "-1"
# Control byte some CoD servers prepend to chat messages.
CHAT_PREFIX_BYTE = "\x15"


@dataclass(slots=True)
class KillData:
    """Payload for kill/suicide/teamkill events (was a positional 4-tuple in the legacy code)."""

    weapon: str
    damage: int
    hit_location: str
    means_of_death: str


class CodParser(Parser):
    def __init__(
        self,
        profile: GameProfile,
        clients: ClientManager | None = None,
        *,
        auth: AuthManager | None = None,
    ) -> None:
        self.auth = auth
        self._warned_about_guids = False
        super().__init__(profile, clients)

    # -- helpers -----------------------------------------------------------

    def _get_or_create(self, cid: str, name: str | None = None, guid: str | None = None) -> Client:
        client = self.clients.get_by_cid(cid)
        if client is None:
            client = Client(cid=cid, name=name or "", guid=guid or "")
            self.clients.add(client)
        else:
            if name:
                client.name = name
            if guid and not client.guid:
                client.guid = guid
        return client

    def _canonical_team(self, raw: str) -> str | None:
        if not raw:
            return None
        return self.profile.teams.get(raw, raw)

    def _valid_guid(self, guid: str) -> str:
        """Reject partial/garbage GUIDs (shorter than the profile's minimum) and AI players.

        Says so the first time, because the silent version of this is miserable to diagnose: a
        server whose GUIDs are the wrong shape for the configured profile authenticates *nobody*,
        and every symptom of that ("admins have no level", "bans do nothing") points elsewhere.

        A **bot's** guid is dropped without a word, which is not the same thing: it is expected, and
        the AI still becomes a client — it occupies a slot and turns up in kills and chat. What it
        must not have is an identity, because every bot on the server shares one, and one database
        row would end up holding all of them with a single level and ban history between them.
        """
        if self.profile.is_bot_guid(guid):
            return ""
        if len(guid) >= self.profile.guid_min_length:
            return guid
        if guid and not self._warned_about_guids:
            self._warned_about_guids = True
            log.warning(
                "%s: ignoring guid %r — %s expects at least %d characters. Nobody will be "
                "authenticated while this is the case. Is `server.game` right for this server? "
                "(CoD4X needs sv_usesteam64id 1, which b3 sets on connect.)",
                self.profile.name,
                guid,
                self.profile.name,
                self.profile.guid_min_length,
            )
        return ""

    @staticmethod
    def _parse_cvar_string(text: str) -> dict[str, str]:
        # Format is \key\value\key\value; strip surrounding whitespace so a leading space
        # (e.g. "InitGame: \g_gametype\...") doesn't misalign the key/value pairing.
        tokens = [t for t in text.strip().split("\\") if t != ""]
        return dict(zip(tokens[::2], tokens[1::2]))

    # -- handlers ----------------------------------------------------------

    @handles(
        r"^K;(?P<vguid>[^;]*);(?P<vcid>-?\d+);(?P<vteam>[a-z]*);(?P<vname>[^;]*);"
        r"(?P<aguid>[^;]*);(?P<acid>-?\d+);(?P<ateam>[a-z]*);(?P<aname>[^;]*);"
        # `[\d.]*`, not `\d*`: the damage figure carries decimals on some engines (Plutonium T6
        # writes them on kill lines, and the CoD damage line below has always had them). An integer
        # pattern here does not mis-read those lines, it fails to match them at all — and an
        # unmatched log line is indistinguishable from a line the server never wrote.
        r"(?P<weapon>[^;]*);(?P<damage>[\d.]*);(?P<mod>[^;]*);(?P<hitloc>[^;]*)$"
    )
    def on_kill(self, m: "re.Match[str]") -> Event:
        vcid, acid = m["vcid"], m["acid"]
        victim = self._get_or_create(vcid, m["vname"])
        vteam = self._canonical_team(m["vteam"])
        if vteam:
            victim.team = vteam

        weapon = m["weapon"]
        kill = KillData(
            weapon=weapon,
            damage=int(float(m["damage"] or 0)),  # some engines write these with decimals
            hit_location=m["hitloc"],
            means_of_death=m["mod"],
        )

        # World death (attacker cid -1) or self-kill -> suicide.
        if acid == WORLD_CID or acid == vcid:
            return Event(EventType.CLIENT_SUICIDE, data=kill, client=victim, target=victim)

        attacker = self._get_or_create(acid, m["aname"])
        ateam = self._canonical_team(m["ateam"])
        if ateam:
            attacker.team = ateam

        is_teamkill = (
            vteam is not None
            and ateam is not None
            and vteam == ateam
            and weapon not in self.profile.non_teamkill_weapons
        )
        etype = EventType.CLIENT_KILL_TEAM if is_teamkill else EventType.CLIENT_KILL
        return Event(etype, data=kill, client=attacker, target=victim)

    @handles(
        r"^(?P<action>say|sayteam);(?P<guid>[^;]*);(?P<cid>-?\d+);(?P<name>[^;]*);(?P<text>.*)$"
    )
    def on_say(self, m: "re.Match[str]") -> Event:
        text = m["text"]
        if text[:1] == CHAT_PREFIX_BYTE:
            text = text[1:]
        client = self._get_or_create(m["cid"], m["name"], self._valid_guid(m["guid"]))
        etype = EventType.CLIENT_TEAM_SAY if m["action"] == "sayteam" else EventType.CLIENT_SAY
        return Event(etype, data=text, client=client)

    @handles(r"^J;(?P<guid>[^;]*);(?P<cid>\d+);(?P<name>.*)$")
    def on_join(self, m: "re.Match[str]") -> Event:
        client = self._get_or_create(m["cid"], m["name"], self._valid_guid(m["guid"]))
        client.authed = False
        if self.auth is not None:
            self.auth.schedule(m["cid"])
        return Event(EventType.CLIENT_JOIN, client=client)

    @handles(r"^Q;(?P<guid>[^;]*);(?P<cid>\d+);(?P<name>.*)$")
    def on_quit(self, m: "re.Match[str]") -> Event | None:
        cid = m["cid"]
        if self.auth is not None:
            self.auth.cancel(cid)
        client = self.clients.remove(cid)
        if client is None:
            return None
        return Event(EventType.CLIENT_DISCONNECT, client=client)

    @handles(r"^InitGame:(?P<data>.*)$")
    def on_init_game(self, m: "re.Match[str]") -> Event:
        data = self._parse_cvar_string(m["data"])
        return Event(EventType.GAME_ROUND_START, data=data)

    @handles(r"^ExitLevel:(?P<data>.*)$")
    def on_exit_level(self, m: "re.Match[str]") -> Event:
        """The map ended. Plugins hang end-of-round reporting off this."""
        return Event(EventType.GAME_EXIT, data=m["data"].strip())

    @handles(
        r"^D;(?P<vguid>[^;]*);(?P<vcid>-?\d+);(?P<vteam>[a-z]*);(?P<vname>[^;]*);"
        r"(?P<aguid>[^;]*);(?P<acid>-?\d+);(?P<ateam>[a-z]*);(?P<aname>[^;]*);"
        r"(?P<weapon>[^;]*);(?P<damage>[\d.]*);(?P<mod>[^;]*);(?P<hitloc>[^;]*)$"
    )
    def on_damage(self, m: "re.Match[str]") -> Event | None:
        """Damage between players — what a team-kill plugin actually watches.

        Classified like the kill line: self-damage, team damage (only when both teams are known),
        or plain damage. Damage dealt by the world has no attacker to blame, so it is dropped,
        as it was classically.
        """
        vcid, acid = m["vcid"], m["acid"]
        if acid == WORLD_CID:
            return None

        victim = self._get_or_create(vcid, m["vname"])
        vteam = self._canonical_team(m["vteam"])
        if vteam:
            victim.team = vteam
        damage = KillData(
            weapon=m["weapon"],
            damage=int(float(m["damage"] or 0)),  # the engine writes these with decimals
            hit_location=m["hitloc"],
            means_of_death=m["mod"],
        )
        if acid == vcid:
            return Event(EventType.CLIENT_DAMAGE_SELF, data=damage, client=victim, target=victim)

        attacker = self._get_or_create(acid, m["aname"])
        ateam = self._canonical_team(m["ateam"])
        if ateam:
            attacker.team = ateam
        team_damage = (
            vteam is not None
            and ateam is not None
            and vteam == ateam
            and m["weapon"] not in self.profile.non_teamkill_weapons
        )
        etype = EventType.CLIENT_DAMAGE_TEAM if team_damage else EventType.CLIENT_DAMAGE
        return Event(etype, data=damage, client=attacker, target=victim)

    @handles(
        r"^A;(?P<guid>[^;]*);(?P<cid>-?\d+);(?P<team>[a-z]*);(?P<name>[^;]*);(?P<action>[\w-]+)$"
    )
    def on_action(self, m: "re.Match[str]") -> Event:
        """A map objective: a flag taken, a bomb planted. The data is the action's name."""
        client = self._get_or_create(m["cid"], m["name"], self._valid_guid(m["guid"]))
        team = self._canonical_team(m["team"])
        if team:
            client.team = team
        return Event(EventType.CLIENT_ACTION, data=m["action"], client=client)

    @handles(r"^(?i:JT);(?P<guid>[^;]*);(?P<cid>\d+);(?P<team>[a-z]*);(?P<name>[^;]*);?$")
    def on_join_team(self, m: "re.Match[str]") -> Event:
        """CoD4's explicit team-change line. Note the trailing `;` the engine emits."""
        client = self._get_or_create(m["cid"], m["name"], self._valid_guid(m["guid"]))
        client.team = self._canonical_team(m["team"])
        return Event(EventType.CLIENT_TEAM_CHANGE, data=client.team, client=client)

    @handles(
        r"^tell;(?P<guid>[^;]*);(?P<cid>-?\d+);(?P<name>[^;]*);"
        r"(?P<tguid>[^;]*);(?P<tcid>-?\d+);(?P<tname>[^;]*);(?P<text>.*)$"
    )
    def on_tell(self, m: "re.Match[str]") -> Event:
        """A private message between two players."""
        text = m["text"]
        if text[:1] == CHAT_PREFIX_BYTE:
            text = text[1:]
        client = self._get_or_create(m["cid"], m["name"], self._valid_guid(m["guid"]))
        target = self._get_or_create(m["tcid"], m["tname"])
        return Event(EventType.CLIENT_PRIVATE_SAY, data=text, client=client, target=target)

    @handles(r"^Item;(?P<guid>[^;]*);(?P<cid>\d+);(?P<name>[^;]*);(?P<item>.*)$")
    def on_item(self, m: "re.Match[str]") -> Event:
        client = self._get_or_create(m["cid"], m["name"], self._valid_guid(m["guid"]))
        return Event(EventType.CLIENT_ITEM_PICKUP, data=m["item"], client=client)

    # Declared last on purpose: `JT;` is also two letters, and the router takes the first pattern
    # that matches in definition order.
    @handles(
        r"^(?P<action>[a-z]{2});(?P<guid>[^;]*);(?P<cid>\d+)"
        r"(?:;(?P<team>[a-z]*);(?P<name>[^;]*))?(?:;(?P<rest>.*))?$",
        re.IGNORECASE,
    )
    def on_short_action(self, m: "re.Match[str]") -> Event | None:
        """Treyarch writes one line type per objective action where Infinity-Ward writes one `A;`.

        Which tokens exist is profile data (``action_map``), so a World at War server reports its
        dog damage, flag captures and bomb plants, while the same two letters on a CoD4 server are
        left alone rather than guessed at.

        Identity comes from the guid and slot only — deliberately not from the name column. The
        classic parser reused its generic chat pattern here, which lined the *team* field up with
        the name group and so renamed the player to "allies" on every objective. The trailing
        fields are ignored: their layout differs per action and no captured sample of them exists,
        so this reports that the action happened rather than its details.
        """
        action = self.profile.action_map.get(m["action"].lower())
        if action is None:
            return None
        client = self._get_or_create(m["cid"], guid=self._valid_guid(m["guid"]))
        team = self._canonical_team(m["team"] or "")
        if team:
            client.team = team
        return Event(EventType.CLIENT_ACTION, data=action, client=client)
