"""Source titles. Insurgency first; Counter-Strike 2 lands beside it.

**Everything the bot says on this family goes through SourceMod**, which is a third-party server
plugin rather than part of the game. That is not a preference: a stock Source server has no verb for
a private message and no verb for a ban that survives a restart, so ``sm_psay`` and ``sm_addban`` are
the only way `!ban` and every command reply work at all. The runtime checks for it on connect and
refuses to start without it — see :meth:`b3.parsers.source.parser.SourceParser.check_sourcemod`.

Two things in here came out of the classic bot's **captured** ``status`` reply rather than from its
code, and both would be silent faults:

* the table has **two row shapes** — a human row carries a duration, ping, loss, rate and address,
  an AI row is just ``#224 "Moe" BOT active`` — and one pattern has to match both. Candidate patterns
  are tried until one yields *any* row, so a pattern for humans alone would win on a mixed server and
  hand back a roster with no bots in it, which `Bot._reconcile` reads as "those players left" and
  disconnects every one of them, every sync.
* the header labels are **padded into a column** (``map     : cs_foobar``), which is why
  `b3.parsers.status.MAP_LINE_RE` had to stop insisting on ``map:``.
"""

from __future__ import annotations

import re

from b3.parsers.profile import GameProfile, RequiredMod

#: SourceMod, and how to prove it is there. ``sm version`` on a server without it answers
#: ``Unknown command "sm"``, which contains none of the expected text — the classic parser checked
#: exactly this command and exited when it failed, and that was the right instinct.
SOURCEMOD = RequiredMod(name="SourceMod", command="sm version", expect="SourceMod")

#: Insurgency's team tokens. ``#Team_Unassigned`` is a *recognised* token that means "no team yet",
#: which is a different thing from a token this bot does not know — see
#: :meth:`b3.parsers.source.parser.SourceParser._set_team`, where the difference is load-bearing.
INSURGENCY_TEAMS = {
    "#Team_Security": "red",
    "#Team_Insurgent": "blue",
    "#Team_Unassigned": "",
    "Spectator": "spec",
}

#: One row of a Source ``status`` reply. Both shapes, in one pattern, for the reason in the module
#: docstring. Captured examples, from the classic bot's ``test_insurgency.py``::
#:
#:     #194 2 "courgette" STEAM_1:0:1111111 33:48 67 0 active 20000 11.222.111.222:27005
#:     #224 "Moe" BOT active
#:
#: The second number on the human row is not in the table's own header and the classic parser threw
#: it away; the first number is the one the log lines use as ``<194>``, so that is the slot.
#: Everything after the id is optional because an AI player has none of it, and the duration allows
#: an hours field for a session over an hour long.
SOURCE_STATUS_ROW_RE = re.compile(
    r"^#\s*(?P<slot>\d+)\s+"
    r"(?:\d+\s+)?"  # an index the header does not mention, present only on human rows
    r'"(?P<name>.*)"\s+'
    r"(?P<guid>\S+)"
    r"(?:\s+(?P<duration>\d+:\d+(?::\d+)?)\s+(?P<ping>\d+)\s+(?P<loss>\S+))?"
    r"\s+(?P<state>\S+)"
    r"(?:\s+(?P<rate>\d+)\s+(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):(?P<port>\d+))?"
    r"\s*$"
)

#: The guid a Source server reports for an AI player. Every bot on the server shares it, so it must
#: never be authenticated — `GameProfile.is_bot_guid` blanks it on both the log path and the status
#: path. The classic parser instead minted ``"BOT" + cid`` to keep them apart, which does keep them
#: out of each other's records but puts a database row in for every bot in every session; blanking is
#: what the rest of this bot does and it costs nothing real, since nobody bans a bot.
BOT_GUID = "BOT"

#: What the server calls itself when the console speaks. Not a player, and never authenticated.
CONSOLE_GUID = "Console"

INSURGENCY = GameProfile(
    name="insurgency",
    family="source",
    # `STEAM_0:0:0` is the shortest real id at eleven characters, so nothing this short is one --
    # which also rejects "BOT" (3) and "Console" (7) on length alone, before the bot-guid check.
    guid_min_length=8,
    # No world slot: a death caused by the map arrives as `committed suicide with "world"`, naming
    # the player and no killer, rather than as a kill by a reserved slot id.
    world_cid="",
    bot_guids=frozenset({BOT_GUID}),
    teams=INSURGENCY_TEAMS,
    # The classic parser's own figure for this engine's console.
    line_length=120,
    required_mod=SOURCEMOD,
    # SourceMod's verbs. `sm_psay` takes the Steam id rather than a slot, which is why
    # `tell_template` grew `%(guid)s` when Homefront needed the same thing.
    say_template="sm_say %s",
    saybig_template="sm_hsay %s",
    tell_template='sm_psay #%(guid)s "%(text)s"',
    kick_template='sm_kick #%(cid)s "%(reason)s"',
    # `sm_addban <minutes> <steamid> [reason]`, where 0 minutes means permanent.
    #
    # **Two commands, and the second is not optional.** `sm_addban` writes the ban list; it does not
    # remove the player who is on the server right now, so on its own it bans somebody and leaves
    # them playing until the map ends. The classic parser called its kick straight after the ban for
    # exactly this reason, and the end-to-end run against the fake server is what caught its absence
    # here: the ban was recorded, the server's own list had the entry, and the culprit was still in
    # the roster. `Bot._send_penalty` sends one command per line and skips the kick when the target
    # has no slot, which is what makes the same template serve an offline ban.
    ban_template='sm_addban 0 "%(guid)s" "%(reason)s"\nsm_kick #%(cid)s "%(reason)s"',
    tempban_template=(
        'sm_addban %(minutes)s "%(guid)s" "%(reason)s"\nsm_kick #%(cid)s "%(reason)s"'
    ),
    tempban_max_minutes=0,  # no known ceiling
    # `sm_unban <steamid|ip>` — the **id**, not the name. `%(target)s` is the player's name, which
    # this verb does not accept, so a lifted ban would have stayed on the server's list.
    unban_template='sm_unban "%(guid)s"',
    # A Source server sets a cvar by being given its name and value; there is no `set` verb, and
    # sending one would look like an unknown command rather than fail.
    set_template='%(name)s "%(value)s"',
    status_patterns=(SOURCE_STATUS_ROW_RE,),
    identity_field="guid",
    map_template="changelevel %s",
    # No rotation cvar on this engine. `maps *` lists every map *installed*, which is what `!map`
    # needs for partial matching -- and is emphatically not a rotation, hence `next_map_cvar`.
    rotation_cvar="",
    maplist_command="maps *",
    next_map_cvar="sm_nextmap",
    # Nothing rotates the map by itself here: the classic read `sm_nextmap` and then loaded it, which
    # is what `Bot.rotate_map` does with `next_map_cvar` plus `map_template`.
    rotate_command="",
)

ALL: dict[str, GameProfile] = {INSURGENCY.name: INSURGENCY}

__all__ = [
    "ALL",
    "BOT_GUID",
    "CONSOLE_GUID",
    "INSURGENCY",
    "INSURGENCY_TEAMS",
    "SOURCEMOD",
    "SOURCE_STATUS_ROW_RE",
]
