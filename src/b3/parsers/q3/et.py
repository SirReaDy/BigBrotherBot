"""EtParser — the log lines Wolfenstein: Enemy Territory adds to the shared Quake3 grammar.

The TODO entry that asked for this ("ET has objective/revive/shove lines") turned out to describe
something the classic ``et.py`` never implemented. What it *does* implement, read from the source,
is more useful than objectives:

* ``ConnectInfo:`` — **identity**. Plain ET has no ``cl_guid``, so the shared parser finds no id for
  anybody and every player stays anonymous: no level, no ban history, no aliases. The PunkBuster id
  on this line is the identity, and it arrives with the IP address as well.
* ``Gib:`` — gibbing, as distinct from a plain kill. The event vocabulary has always had
  CLIENT_GIB/GIB_TEAM/GIB_SELF and nothing emitted them.
* ``sayc:`` / ``sayteamc:`` — ET's chat lines identify the speaker **by slot**, where the shared
  Quake3 chat line names them. Matching by slot is strictly better: two players can wear the same
  name, and a name full of colour codes is a poor key.
* ``warmup:`` and ``restartgame:`` — the two round-boundary lines the classic parser mapped.

Not ported, and tracked in TODO.md §1.4: ETPro's own ``Etpro:``/``Qmm:``/``PrivMsg`` lines, which
serve an ETPro-specific admin mod rather than the bot.
"""

from __future__ import annotations

import re

from b3.core.events import Event, EventType
from b3.parsers.cod.parser import KillData
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.registry import handles


class EtParser(Q3Parser):
    """Wolfenstein: Enemy Territory and ETPro. Selected by ``family="et"`` in the profile."""

    @handles(
        r"^ConnectInfo:\s*(?P<cid>\d+):\s*(?P<pbid>[0-9A-Z]{32}):\s*(?P<name>[^:]+):\s*"
        r"(?P<num1>\d+):\s*(?P<num2>\d+):\s*(?P<ip>[0-9.]+):(?P<port>\d+)\s*$",
        re.IGNORECASE,
    )
    def on_connect_info(self, m: "re.Match[str]") -> Event:
        """``ConnectInfo: 0: <32-hex PB id>: ^w[^2AS^w]^2Lead: 3: 3: 24.153.180.106:2794``.

        The only place an ET server states who somebody is. The PunkBuster id goes in as the guid —
        it is the id the classic bot stored too, so an imported legacy database still matches.
        """
        client = self._get_or_create(m["cid"], m["name"])
        guid = self._valid_guid(m["pbid"])
        if guid:
            client.guid = guid
        client.ip = m["ip"]
        return Event(EventType.CLIENT_UPDATE, client=client)

    @handles(r"^Gib:\s*(?P<acid>\d+)\s+(?P<vcid>\d+)\s+(?P<weapon>\d+):\s*(?P<text>.*)$")
    def on_gib(self, m: "re.Match[str]") -> Event | None:
        """``Gib: 1 18 9: <killer> gibbed <victim> by MOD_MP40``.

        Attacker first, matching the kill line: id Tech 3 logs these as
        ``G_LogPrintf("...: %i %i %i", killer, victim, mod)``. The classic ``et.py`` read its kill
        and gib lines the other way round, which inverted every ET kill — see TODO.md §1.4.
        """
        attacker = self.clients.get_by_cid(m["acid"])
        victim = self.clients.get_by_cid(m["vcid"])
        if attacker is None or victim is None:
            return None
        data = KillData(weapon=m["weapon"], damage=100, hit_location="", means_of_death=m["weapon"])
        if attacker.cid == victim.cid:
            return Event(EventType.CLIENT_GIB_SELF, data=data, client=victim, target=victim)
        if attacker.team is not None and attacker.team == victim.team:
            return Event(EventType.CLIENT_GIB_TEAM, data=data, client=attacker, target=victim)
        return Event(EventType.CLIENT_GIB, data=data, client=attacker, target=victim)

    @handles(r"^(?P<action>say|sayteam)c:\s*(?P<cid>\d+):\s*(?P<name>[^:]*):\s?(?P<text>.*)$")
    def on_say_by_slot(self, m: "re.Match[str]") -> Event | None:
        """``sayc: 0: ^w[^2AS^w]^2Lead:  sorry...`` — chat with the speaker's slot in it.

        Preferred over the shared name-matching handler wherever a server writes it, because the
        slot is unambiguous. A slot the bot has never seen yields nothing rather than a nameless
        client, exactly as the name-based path does.
        """
        client = self.clients.get_by_cid(m["cid"])
        if client is None:
            return None
        etype = EventType.CLIENT_TEAM_SAY if m["action"] == "sayteam" else EventType.CLIENT_SAY
        return Event(etype, data=m["text"], client=client)

    @handles(r"^warmup:\s*(?P<data>.*)$", re.IGNORECASE)
    def on_warmup(self, m: "re.Match[str]") -> Event:
        return Event(EventType.GAME_WARMUP, data=m["data"].strip())

    @handles(r"^restartgame:\s*(?P<data>.*)$", re.IGNORECASE)
    def on_restart_game(self, m: "re.Match[str]") -> Event:
        """A round restart ends the round as far as the rest of the bot is concerned."""
        return Event(EventType.GAME_ROUND_END, data=m["data"].strip())
