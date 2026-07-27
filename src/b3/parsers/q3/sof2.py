"""Sof2Parser — the one line Soldier of Fortune 2 adds to the shared Quake3 grammar.

SoF2's ``Hit:`` line is the same idea as Urban Terror's and better behaved: it states the damage
figure outright, so there is no weapon×location table to carry. That makes hit detection — and with
it any team-damage policy — work on SoF2 and its Gold/Platinum variant.

    Hit: 0 4 520 368 0: xlr8or hit someone at location 520 for 368

Everything else SoF2 writes is the shared grammar, which is why this file is short: the classic
``sof2.py`` re-implemented connect/userinfo/kill/item from scratch, and comparing them line by line
shows no behavioural difference from the shared handlers worth carrying over.
"""

from __future__ import annotations

import re

from b3.core.events import Event, EventType
from b3.parsers.cod.parser import KillData
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.registry import handles


class Sof2Parser(Q3Parser):
    """Soldier of Fortune 2. Selected by ``family="sof2"`` in the profile."""

    @handles(
        r"^Hit:\s*(?P<vcid>\d+)\s+(?P<acid>\d+)\s+(?P<hitloc>\d+)\s+(?P<damage>\d+)\s+"
        r"(?P<weapon>\d+):\s*(?P<text>.*)$"
    )
    def on_hit(self, m: "re.Match[str]") -> Event | None:
        """``Hit: <victim> <attacker> <hitloc> <damage> <weapon>: text``.

        Victim first, like Urban Terror's hit line and unlike either game's *kill* line — that
        asymmetry is in the engine, not a transcription slip.
        """
        victim = self.clients.get_by_cid(m["vcid"])
        attacker = self.clients.get_by_cid(m["acid"])
        if victim is None or attacker is None:
            return None

        data = KillData(
            weapon=m["weapon"],
            damage=int(m["damage"]),  # SoF2 states it, so no lookup table is needed
            hit_location=m["hitloc"],
            means_of_death=m["weapon"],
        )
        if attacker.cid == victim.cid:
            return Event(EventType.CLIENT_DAMAGE_SELF, data=data, client=victim, target=victim)
        if attacker.team is not None and attacker.team == victim.team:
            return Event(EventType.CLIENT_DAMAGE_TEAM, data=data, client=attacker, target=victim)
        return Event(EventType.CLIENT_DAMAGE, data=data, client=attacker, target=victim)
