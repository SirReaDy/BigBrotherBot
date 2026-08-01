"""World of Padman 1.5, whose combat lines differ from the shared Quake3 grammar.

Two log lines are written differently by this title, and neither matches the shared patterns:

    Kill: 2 MOD_INJECTOR 0                     attacker, means-of-death name, victim
    Kill: 1 18 9: Bob killed Jim by MOD_MP40   every other Quake3 title

The shared kill pattern requires the trailing ``: <text> by <MOD>``, so it matches nothing here.
Both this and the ``Damage:`` line below are handled by this subclass.

Neither line is confirmed against a running server: the classic tree has no captured tests for this
title, so the source is that parser's own regexes and the sample lines in its comments. They are
mutually consistent — both put the means of death in the middle field — but a real log would be
better evidence.
"""

from __future__ import annotations

import re

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.cod.parser import KillData
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.registry import handles

#: Slot the engine uses for "not a player". A damage line can name it on either side.
WORLD = "1022"

#: Slots outside this range are the engine misreporting. The classic parser clamped them on both its
#: kill and damage handlers, noting that the cid in a damage line is sometimes over 64.
MAX_SLOT = 64


class Wop15Parser(Q3Parser):
    """World of Padman 1.5. Selected by ``family="wop15"``."""

    @handles(r"^Kill:\s*(?P<acid>\d+)\s+(?P<mod>[0-9A-Za-z_]+)\s+(?P<vcid>\d+)\s*$")
    def on_wop_kill(self, m: "re.Match[str]") -> Event | None:
        """``Kill: 2 MOD_INJECTOR 0`` — attacker, means of death, victim.

        A separate handler rather than an override of the shared one: the two patterns cannot both
        match a line, since this one ends at the victim's slot and the shared one requires a colon
        and a sentence after it.
        """
        attacker = self._slot(m["acid"])
        victim = self._slot(m["vcid"])
        if attacker is None or victim is None:
            return None
        kill = KillData(weapon=m["mod"], damage=100, hit_location="", means_of_death=m["mod"])
        return self._combat_event(attacker, victim, kill, fatal=True)

    @handles(
        r"^Damage:\s*(?P<vcid>\d+)\s+(?P<mod>[0-9A-Za-z_]+)\s+(?P<acid>\d+)\s+"
        r"(?P<damage>\d+)\s+(?P<mod2>\d+)\s*$"
    )
    def on_wop_damage(self, m: "re.Match[str]") -> Event | None:
        """``Damage: 2 1022 2 50 7`` — victim, means of death, attacker, damage, means of death.

        Non-fatal damage, the same information Urban Terror's ``Hit:`` line carries, which is what
        team-damage policing needs.

        The field order puts the weapon in the middle, matching this title's kill line. The purpose
        of the second means-of-death column is unknown, so it is passed through in ``extra`` rather
        than dropped or interpreted.
        """
        victim = self._slot(m["vcid"])
        attacker = self._slot(m["acid"])
        if attacker is None or victim is None:
            return None
        kill = KillData(
            weapon=m["mod"], damage=int(m["damage"]), hit_location="", means_of_death=m["mod"]
        )
        event = self._combat_event(attacker, victim, kill, fatal=False)
        if event is not None:
            event.extra["means_of_death_id"] = m["mod2"]
        return event

    # -- helpers ---------------------------------------------------------------

    def _slot(self, cid: str) -> Client | None:
        """The client in a slot, treating an out-of-range slot as the world.

        A slot outside 0..63 cannot be a player, so it is read as the world rather than used to
        create one. The classic parser clamped both of these lines the same way.
        """
        if not -1 < int(cid) < MAX_SLOT:
            cid = WORLD
        return self.clients.get_by_cid(cid)

    def _combat_event(
        self, attacker: Client, victim: Client, kill: KillData, *, fatal: bool
    ) -> Event | None:
        if attacker.cid == victim.cid == WORLD:
            return None  # the world hurting the world: nothing happened to anybody
        if attacker.cid == victim.cid:
            etype = EventType.CLIENT_SUICIDE if fatal else EventType.CLIENT_DAMAGE_SELF
            return Event(etype, data=kill, client=victim, target=victim)
        if attacker.team is not None and attacker.team == victim.team:
            etype = EventType.CLIENT_KILL_TEAM if fatal else EventType.CLIENT_DAMAGE_TEAM
        else:
            etype = EventType.CLIENT_KILL if fatal else EventType.CLIENT_DAMAGE
        return Event(etype, data=kill, client=attacker, target=victim)
