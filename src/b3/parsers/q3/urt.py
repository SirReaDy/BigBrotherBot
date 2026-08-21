"""UrtParser — the log lines Urban Terror adds to the shared Quake3 grammar.

On top of the lines every Quake3 server writes, Urban Terror reports:

* ``Hit:`` — every non-fatal hit, with a hit location. This is what a team-kill plugin watches:
  without it a UrT server only reports completed kills, so shooting your own team repeatedly
  without finishing them off produces nothing.
* ``Radio:`` — the voice-command menu, which spam control policies as a chat channel.
* ``saytell:`` — private chat, including commands whispered to the bot.
* ``Flag:`` / ``Flag Return:`` / ``FlagCaptureTime:`` — capture-the-flag.
* ``Bomb ...`` / ``Bombholder is`` / ``Pop!`` — bomb mode.
* ``Callvote:`` / ``Vote:`` / ``VotePassed:`` / ``VoteFailed:`` — the voting system.
* ``ClientSpawn:``, ``Assist:`` (4.3 only) and ``SurvivorWinner:``.

The two lookup tables below come from the classic ``iourt41.py``/``iourt42.py``: hit lines number
weapons differently from kill lines (``19`` is the LR300 on one scale and another weapon on the
other), and a hit's damage is a weapon×hit-location lookup because the line carries no damage
figure.

Not implemented: UrT's ``auth`` identity service (a separate socket to Frozen Sand's account
servers, which needs a live server to confirm it still answers), and the freeze-tag and jump-run
lines, which exist to serve plugins that have not been ported.
"""

from __future__ import annotations

import logging
import re

from b3.core.events import Event, EventType
from b3.parsers.cod.parser import KillData
from b3.parsers.q3.parser import Q3Parser, chat_text
from b3.parsers.registry import handles

log = logging.getLogger(__name__)

#: Means-of-death ids, from the classic parser's constants. Only the ones referenced below are
#: named; the rest travel through as numbers, exactly as they do on the kill line.
UT_MOD_KNIFE = "12"
UT_MOD_KNIFE_THROWN = "13"
UT_MOD_BERETTA = "14"
UT_MOD_DEAGLE = "15"
UT_MOD_SPAS = "16"
UT_MOD_UMP45 = "17"
UT_MOD_MP5K = "18"
UT_MOD_LR300 = "19"
UT_MOD_G36 = "20"
UT_MOD_PSG1 = "21"
UT_MOD_HK69 = "22"
UT_MOD_BLED = "23"
UT_MOD_KICKED = "24"
UT_MOD_HEGRENADE = "25"
UT_MOD_SR8 = "28"
UT_MOD_AK103 = "30"
UT_MOD_NEGEV = "35"
UT_MOD_HK69_HIT = "37"
UT_MOD_M4 = "38"
UT_MOD_GOOMBA = "40"
MOD_TELEFRAG = "5"

#: A ``Hit:`` line numbers weapons on its own scale. Without this translation every hit would be
#: attributed to the wrong gun — and then priced with the wrong gun's damage table.
HIT_WEAPON_TO_KILL_WEAPON = {
    "1": UT_MOD_KNIFE,
    "2": UT_MOD_BERETTA,
    "3": UT_MOD_DEAGLE,
    "4": UT_MOD_SPAS,
    "5": UT_MOD_MP5K,
    "6": UT_MOD_UMP45,
    "8": UT_MOD_LR300,
    "9": UT_MOD_G36,
    "10": UT_MOD_PSG1,
    "14": UT_MOD_SR8,
    "15": UT_MOD_AK103,
    "17": UT_MOD_NEGEV,
    "19": UT_MOD_M4,
    "21": UT_MOD_HEGRENADE,
    "22": UT_MOD_KNIFE_THROWN,
}

#: Damage per weapon by hit location: head, helmet, torso, kevlar, arms, legs, body, killed.
#: A ``Hit:`` line carries no damage figure, so this table *is* the damage — measured by the
#: community (the classic parser credits Garreth) and carried over verbatim.
HIT_LOCATIONS = ("head", "helmet", "torso", "kevlar", "arms", "legs", "body", "killed")
DAMAGE = {
    MOD_TELEFRAG: (0, 0, 0, 0, 0, 0, 0, 0),
    UT_MOD_KNIFE: (100, 60, 44, 35, 20, 20, 44, 100),
    UT_MOD_KNIFE_THROWN: (100, 60, 44, 35, 20, 20, 44, 100),
    UT_MOD_BERETTA: (100, 34, 30, 20, 11, 11, 30, 100),
    UT_MOD_DEAGLE: (100, 66, 57, 38, 22, 22, 57, 100),
    UT_MOD_SPAS: (25, 25, 25, 25, 25, 25, 25, 100),
    UT_MOD_UMP45: (100, 51, 44, 29, 17, 17, 44, 100),
    UT_MOD_MP5K: (50, 34, 30, 20, 11, 11, 30, 100),
    UT_MOD_LR300: (100, 51, 44, 29, 17, 17, 44, 100),
    UT_MOD_G36: (100, 51, 44, 29, 17, 17, 44, 100),
    UT_MOD_PSG1: (100, 63, 97, 63, 36, 36, 97, 100),
    UT_MOD_HK69: (50, 50, 50, 50, 50, 50, 50, 100),
    UT_MOD_BLED: (15, 15, 15, 15, 15, 15, 15, 15),
    UT_MOD_KICKED: (20, 20, 20, 20, 20, 20, 20, 100),
    UT_MOD_HEGRENADE: (50, 50, 50, 50, 50, 50, 50, 100),
    UT_MOD_SR8: (100, 100, 100, 100, 50, 50, 100, 100),
    UT_MOD_AK103: (100, 58, 51, 34, 19, 19, 51, 100),
    UT_MOD_NEGEV: (50, 34, 30, 20, 11, 11, 30, 100),
    UT_MOD_HK69_HIT: (20, 20, 20, 20, 20, 20, 20, 100),
    UT_MOD_M4: (100, 51, 44, 29, 17, 17, 44, 100),
    UT_MOD_GOOMBA: (100, 100, 100, 100, 100, 100, 100, 100),
}

#: What a hit is worth when the tables have nothing to say — a new weapon, a mod, a hit location the
#: engine grew. The classic value: enough that a team-kill plugin still counts the hit.
UNKNOWN_DAMAGE = 15

#: `Flag: <cid> <subtype>: <text>` — the subtype says what happened to it.
FLAG_ACTIONS = {"0": "flag_dropped", "1": "flag_returned", "2": "flag_captured"}

#: `Bomb was planted by 2` and friends. "has been collected" shares the shape.
BOMB_ACTIONS = {
    "planted": "bomb_planted",
    "defused": "bomb_defused",
    "tossed": "bomb_tossed",
    "collected": "bomb_collected",
}


class UrtParser(Q3Parser):
    """Urban Terror 4.1/4.2/4.3. Selected by ``family="urt"`` in the profile."""

    # -- damage --------------------------------------------------------------

    @handles(
        r"^Hit:\s*(?P<vcid>\d+)\s+(?P<acid>\d+)\s+(?P<hitloc>\d+)\s+(?P<weapon>\d+):\s*"
        r"(?P<text>.*)$"
    )
    def on_hit(self, m: "re.Match[str]") -> Event | None:
        """``Hit: <victim> <attacker> <hitloc> <weapon>: Bob hit Jim in the Head``.

        Note the field order: the **victim comes first** here, the opposite way round from the kill
        line, where the attacker leads. Getting that backwards would blame every team hit on the
        player who took it.

        Unlike the CoD damage line, both clients must already be known: a hit is a poor moment to
        invent a player, and the classic parser dropped the line for the same reason.
        """
        victim = self.clients.get_by_cid(m["vcid"])
        attacker = self.clients.get_by_cid(m["acid"])
        if victim is None or attacker is None:
            return None

        weapon = HIT_WEAPON_TO_KILL_WEAPON.get(m["weapon"])
        if weapon is None:
            log.warning("unknown weapon id %r on a UrT Hit line", m["weapon"])
            weapon = m["weapon"]
        location = self._hit_location(m["hitloc"])
        damage = KillData(
            weapon=weapon,
            damage=self._damage_points(weapon, m["hitloc"]),
            hit_location=location,
            means_of_death=weapon,
        )
        # Remembered for the `Kill:` line, which states the weapon and not the part of the body. The
        # kill is the event plugins listen to, so without this the fact that a shot was a headshot is
        # in the log and nowhere else. Consumed by `Q3Parser.on_kill`.
        if victim.cid is not None:
            self._last_hit[victim.cid] = location
        if attacker.cid == victim.cid:
            return Event(EventType.CLIENT_DAMAGE_SELF, data=damage, client=victim, target=victim)
        if attacker.team is not None and attacker.team == victim.team:
            return Event(EventType.CLIENT_DAMAGE_TEAM, data=damage, client=attacker, target=victim)
        return Event(EventType.CLIENT_DAMAGE, data=damage, client=attacker, target=victim)

    @staticmethod
    def _damage_points(weapon: str, hitloc: str) -> int:
        table = DAMAGE.get(weapon)
        try:
            index = int(hitloc)
        except ValueError:  # pragma: no cover - the pattern only matches digits
            return UNKNOWN_DAMAGE
        if table is None or not 0 <= index < len(table):
            return UNKNOWN_DAMAGE
        return table[index]

    @staticmethod
    def _hit_location(hitloc: str) -> str:
        index = int(hitloc)
        return HIT_LOCATIONS[index] if 0 <= index < len(HIT_LOCATIONS) else hitloc

    # -- voice comms and voting ---------------------------------------------

    @handles(
        r"^Radio:\s*(?P<cid>\d+)\s+-\s+(?P<group>\d+)\s+-\s+(?P<msg>\d+)\s+-\s+"
        r'"(?P<location>.*)"\s+-\s+"(?P<text>.*)"\s*$'
    )
    def on_radio(self, m: "re.Match[str]") -> Event | None:
        """``Radio: 0 - 7 - 2 - "New Alley" - "I'm going for the flag"``."""
        client = self.clients.get_by_cid(m["cid"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_RADIO,
            data={
                "msg_group": m["group"],
                "msg_id": m["msg"],
                "location": m["location"],
                "text": m["text"],
            },
            client=client,
        )

    @handles(r'^Callvote:\s*(?P<cid>\d+)\s+-\s+"(?P<vote>.*)"\s*$')
    def on_callvote(self, m: "re.Match[str]") -> Event | None:
        client = self.clients.get_by_cid(m["cid"])
        if client is None:
            return None
        return Event(EventType.CLIENT_CALLVOTE, data=m["vote"], client=client)

    @handles(r"^Vote:\s*(?P<cid>\d+)\s+-\s+(?P<value>.+?)\s*$")
    def on_vote(self, m: "re.Match[str]") -> Event | None:
        client = self.clients.get_by_cid(m["cid"])
        if client is None:
            return None
        return Event(EventType.CLIENT_VOTE, data=m["value"], client=client)

    @handles(
        r'^Vote(?P<outcome>Passed|Failed):\s*(?P<yes>\d+)\s+-\s+(?P<no>\d+)\s+-\s+"(?P<what>.*)"\s*$'
    )
    def on_vote_result(self, m: "re.Match[str]") -> Event:
        etype = EventType.VOTE_PASSED if m["outcome"].lower() == "passed" else EventType.VOTE_FAILED
        return Event(etype, data={"yes": int(m["yes"]), "no": int(m["no"]), "what": m["what"]})

    # -- private chat and spawns ---------------------------------------------

    @handles(r"^saytell:\s*(?P<cid>\d+)\s+(?P<tcid>\d+)\s+(?P<name>.+?):\s?(?P<text>.*)$")
    def on_saytell(self, m: "re.Match[str]") -> Event | None:
        """``saytell: 15 16 repelSteeltje: nno`` — speaker's slot, target's slot, speaker's name.

        Private chat, which is how a command is whispered to the bot rather than said out loud. The
        two slots can be equal (``saytell: 15 15 …``), so they are not required to differ.
        """
        client = self._chat_client(m["cid"], m["name"])
        if client is None:
            return None
        return Event(
            EventType.CLIENT_PRIVATE_SAY,
            data=chat_text(m["text"]),
            client=client,
            target=self.clients.get_by_cid(m["tcid"]),
        )

    @handles(r"^ClientSpawn:\s*(?P<cid>\d+)\s*$")
    def on_spawn(self, m: "re.Match[str]") -> Event | None:
        """``ClientSpawn: 0`` — several plugins key respawn logic on this."""
        client = self.clients.get_by_cid(m["cid"])
        if client is None:
            return None
        return Event(EventType.CLIENT_SPAWN, client=client)

    # -- objectives ----------------------------------------------------------

    @handles(r"^Flag:\s*(?P<cid>\d+)\s+(?P<subtype>\d+):\s*(?P<text>.*)$")
    def on_flag(self, m: "re.Match[str]") -> Event | None:
        """``Flag: 1 2: team_CTF_blueflag`` — dropped, returned or captured by a player."""
        action = FLAG_ACTIONS.get(m["subtype"])
        if action is None:
            return None
        return self._action(m["cid"], action)

    @handles(r"^Flag Return:\s*(?P<color>.+?)\s*$")
    def on_flag_return(self, m: "re.Match[str]") -> Event:
        """``Flag Return: RED`` — the game returning a flag, with no player to credit."""
        return Event(EventType.GAME_FLAG_RETURNED, data=m["color"])

    @handles(r"^Bomb\s+(?:was|has been)\s+(?P<subaction>[a-z]+)\s+by\s+(?P<cid>\d+).*$")
    def on_bomb(self, m: "re.Match[str]") -> Event | None:
        """``Bomb was planted by 2`` / ``Bomb has been collected by 2``."""
        action = BOMB_ACTIONS.get(m["subaction"].lower())
        if action is None:
            return None
        return self._action(m["cid"], action)

    @handles(r"^Bombholder is\s+(?P<cid>\d+)\s*$")
    def on_bombholder(self, m: "re.Match[str]") -> Event | None:
        return self._action(m["cid"], "bomb_holder_spawn")

    @handles(r"^Pop!\s*$")
    def on_pop(self, _m: "re.Match[str]") -> Event:
        """The bomb went off. No player, no slot — just the bang."""
        return Event(EventType.GAME_BOMB_EXPLODED)

    @handles(r"^SurvivorWinner:\s*(?P<who>.+?)\s*$")
    def on_survivor_winner(self, m: "re.Match[str]") -> Event:
        """``SurvivorWinner: Red`` (a team) or ``SurvivorWinner: 0`` (a slot)."""
        return Event(EventType.GAME_SURVIVOR_WINNER, data=m["who"])

    @handles(r"^FlagCaptureTime:\s*(?P<cid>\d+):\s*(?P<ms>\d+)\s*$")
    def on_flag_capture_time(self, m: "re.Match[str]") -> Event | None:
        """``FlagCaptureTime: 0: 1234567890`` — how long that capture took, in milliseconds.

        The slot is followed by a colon, unlike the space-separated fields most of these lines use.
        """
        client = self.clients.get_by_cid(m["cid"])
        if client is None:
            return None
        return Event(EventType.CLIENT_FLAG_CAPTURE_TIME, data=int(m["ms"]), client=client)

    # -- 4.3 only -------------------------------------------------------------

    @handles(r"^Assist:\s*(?P<acid>\d+)\s+(?P<kcid>\d+)\s+(?P<vcid>\d+):\s*(?P<text>.*)$")
    def on_assist(self, m: "re.Match[str]") -> Event | None:
        """``Assist: 0 14 15: -[TPF]-PtitBigorneau assisted Bot1 to kill Bot2`` — 4.3 only.

        Three slots: the assister, the killer, the victim. The assist is credited to the assister,
        so they are the event's `client`; `target` is the player who died and the killer is in
        `extra`.
        """
        assister = self.clients.get_by_cid(m["acid"])
        if assister is None:
            return None
        return Event(
            EventType.CLIENT_ASSIST,
            client=assister,
            target=self.clients.get_by_cid(m["vcid"]),
            extra={"killer": self.clients.get_by_cid(m["kcid"])},
        )
