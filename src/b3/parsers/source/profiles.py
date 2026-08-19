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

from b3.parsers.profile import GameProfile, OptionalMod, RequiredMod

#: SourceMod, and how to prove it is there. ``sm version`` on a server without it answers
#: ``Unknown command "sm"``, which contains none of the expected text — the classic parser checked
#: exactly this command and exited when it failed, and that was the right instinct.
SOURCEMOD = RequiredMod(name="SourceMod", command="sm version", expect="SourceMod")

#: "B3 Say", a SourceMod plugin written for the classic bot. Not needed — everything works without
#: it — but where it is installed its verbs draw the bot's messages far better on screen than the
#: stock ``sm_`` ones, so the classic parser asked ``sm plugins list`` at connect and switched to
#: them. Optional rather than required, which is the whole distinction from `SOURCEMOD` above: a
#: server without SourceMod cannot be administered at all, and a server without this one simply
#: looks slightly worse.
B3_SAY = OptionalMod(
    name="B3 Say",
    command="sm plugins list",
    overrides={
        "say_template": "b3_say %s",
        "saybig_template": "b3_hsay %s",
        "tell_template": 'b3_psay #%(guid)s "%(text)s"',
        # And stop prefixing, as the classic did. The plugin draws these messages distinctly on its
        # own, so `(b3):` in front of them is clutter — and clutter that costs part of the line
        # budget, on an engine whose chat limit is 120 characters.
        "prefix_messages": False,
    },
)

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
    optional_mods=(B3_SAY,),
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
    # `changelevel <map> [gametype]`. The second argument is why `map_arguments` exists: without it
    # `!map market push` sent `changelevel market` and the admin got the right map in whatever mode
    # the server happened to be configured for -- the mode they asked for was simply dropped, with no
    # error, which is this codebase's most-repeated failure shape. The trailing placeholder renders
    # empty when no mode is given and the spacing is collapsed by `Bot.change_map`, so a bare
    # `!map market` still sends exactly `changelevel market`.
    map_template="changelevel %(map)s %(gamemode)s",
    map_arguments=("gamemode",),
    # A space, not a comma: this is the separator the verb itself uses, so what an admin types reads
    # the same as what reaches the server.
    map_argument_separator=" ",
    # No rotation cvar on this engine. `maps *` lists every map *installed*, which is what `!map`
    # needs for partial matching -- and is emphatically not a rotation, hence `next_map_cvar`.
    rotation_cvar="",
    maplist_command="maps *",
    next_map_cvar="sm_nextmap",
    # Nothing rotates the map by itself here: the classic read `sm_nextmap` and then loaded it, which
    # is what `Bot.rotate_map` does with `next_map_cvar` plus `map_template`.
    rotate_command="",
)

#: Counter-Strike 2's team tokens, mapped exactly as the classic ``csgo.py`` mapped them
#: (``getTeam``): TERRORIST is blue and CT is red. The pairing is arbitrary either way, and matching
#: the classic one is what keeps a plugin's stored per-team figures meaning the same thing after an
#: import. ``Unassigned`` is a recognised token for "no team yet", which is not the same as a token
#: this bot does not know -- see `SourceParser._set_team`, where the difference decides whether a
#: player's team is cleared or left alone.
CS2_TEAMS = {
    "TERRORIST": "blue",
    "CT": "red",
    "Unassigned": "",
    "Spectator": "spec",
}

#: **Counter-Strike 2, and it is not Insurgency with different team names.**
#:
#: The grammar is shared -- every line captured in the classic ``test_csgo.py`` is read by
#: `SourceParser` without a change, which is unsurprising since the two titles shared one parser
#: classically. What is *not* shared is everything the bot says back, because **SourceMod has no CS2
#: build**: Source 2 broke the interface it hooks, and the community moved to Metamod:Source with
#: CounterStrikeSharp instead. So none of Insurgency's verbs exist here -- no ``sm_say``, no
#: ``sm_psay``, no ``sm_addban`` -- and this title is built on what a stock server answers:
#:
#: * ``say`` and ``kickid <userid> [reason]`` are native and documented.
#: * ``changelevel <map>`` is native.
#: * **There is no native private message.** `tell_template` therefore goes out as a public ``say``
#:   with the player's name in front of it. That is a real loss of privacy for `!command` replies and
#:   it is deliberate: the alternative is sending a verb the server does not have, which fails
#:   silently, and a reply nobody receives is worse than one everybody sees.
#: * **There is no reliable ban.** ``banid``/``writeid`` survive from Source 1 but are reported
#:   unreliable on CS2 builds, so `ban_template` is the kick verb and enforcement is this bot's own:
#:   the record is kept here and re-applied when the player comes back. `Bot.unban` recognises the
#:   ban-equals-kick case and says so rather than sending an operator to hunt through a server-side
#:   list that was never written. The same position ``plutoiw5``/``plutot6`` and MW2/MW3 are in.
#:
#: A server running CounterStrikeSharp with an admin plugin could do all three properly, and that is
#: worth a second profile once somebody with such a server can confirm the verb names. Guessing them
#: is what this codebase keeps finding to be the expensive mistake, so it is not done here.
CS2 = GameProfile(
    name="cs2",
    family="source",
    # Eight, and the length is measured *after* normalisation rather than before, which is what makes
    # it safe here: `SourceParser._ensure` canonicalises first, so what this sees is always the legacy
    # form, and the shortest possible one is `STEAM_1:0:0` at eleven characters. Raw `[U:1:1]` is
    # seven, the same as `Console`, so a check applied to the wire form could not have separated them.
    guid_min_length=8,
    normalise_steam_ids=True,
    # As on Insurgency: the map killing somebody arrives as `committed suicide with "world"`, naming
    # one player, so there is no world slot to attribute it to.
    world_cid="",
    bot_guids=frozenset({BOT_GUID}),
    teams=CS2_TEAMS,
    # The classic parser's figure for this engine's console.
    line_length=120,
    # No required mod: a stock CS2 server can be administered, just not privately (see above).
    say_template="say %s",
    # No centre-screen verb natively; `Bot.say_big` falls back to `say` when this is empty.
    saybig_template="",
    # Public, because the engine offers nothing else. Named so the player can see it was meant for
    # them, and so everybody else can see who is being answered rather than reading a reply out of
    # nowhere.
    tell_template="say [%(name)s] %(text)s",
    kick_template='kickid %(cid)s "%(reason)s"',
    # Deliberately the kick verb: see the note above on `banid`. `Bot.unban` keys the "no server-side
    # ban list" message off this equality, so the two must stay identical.
    ban_template='kickid %(cid)s "%(reason)s"',
    tempban_template='kickid %(cid)s "%(reason)s"',
    tempban_max_minutes=0,
    # Nothing to lift server-side, because nothing was ever written there.
    unban_template=None,
    set_template='%(name)s "%(value)s"',
    status_patterns=(SOURCE_STATUS_ROW_RE,),
    identity_field="guid",
    map_template="changelevel %s",
    rotation_cvar="",
    maplist_command="maps *",
    # SourceMod publishes `sm_nextmap`; a stock CS2 server does not, so there is nothing to read and
    # `Bot.get_next_map` correctly answers "nobody knows" rather than guessing from the map list.
    next_map_cvar="",
    rotate_command="",
)

ALL: dict[str, GameProfile] = {p.name: p for p in (INSURGENCY, CS2)}

__all__ = [
    "ALL",
    "B3_SAY",
    "BOT_GUID",
    "CONSOLE_GUID",
    "CS2",
    "CS2_TEAMS",
    "INSURGENCY",
    "INSURGENCY_TEAMS",
    "SOURCEMOD",
    "SOURCE_STATUS_ROW_RE",
]
