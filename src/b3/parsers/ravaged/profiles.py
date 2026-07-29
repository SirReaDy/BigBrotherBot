"""Ravaged, as data. One title, and the only engine here that counts a ban in **days**.

Three things drive the profile:

* **Everything is keyed on the Steam id**, and it is also the `cid`: the log lines carry it on every
  line and the commands take it, so there is nothing to gain from keying on a name.
* **A ban is a `kickban` with a duration in days** — there is no separate permanent verb, so "forever"
  is expressed as 365 days, exactly as the classic parser did (it passed 525600 minutes). That is why
  `%(days)s` exists: this is the first engine to want the duration in that unit.
* **Chat is HTML.** `say <FONT COLOR='#RRGGBB'> text` — the colour is part of the verb rather than a
  setting, and the three flavours of message use three different colours, which is how a player tells
  a broadcast from a private reply.
"""

from __future__ import annotations

from b3.parsers.profile import GameProfile

#: Ravaged's two sides. 0 is the Scavengers and 1 the Resistance — the classic parser mapped them onto
#: red and blue, which is the vocabulary the rest of this bot uses.
RAVAGED_TEAMS = {"0": "red", "1": "blue"}

#: The colours the classic parser used, kept so that an operator moving from it sees the same thing.
SAY_COLOUR = "F2C880"
BIG_COLOUR = "FC00E2"
PRIVATE_COLOUR = "00FC48"

#: A "permanent" ban, in the only unit this engine's ban verb takes. 365 days is what the classic
#: parser sent (525600 minutes); the bot's own record is what actually makes it permanent.
PERMANENT_DAYS = 365

RAVAGED = GameProfile(
    name="ravaged",
    family="ravaged",
    # A Steam id, and nothing shorter is one.
    guid_min_length=17,
    world_cid="",
    teams=RAVAGED_TEAMS,
    say_template=f"say <FONT COLOR='#{SAY_COLOUR}'> %s",
    saybig_template=f"say <FONT COLOR='#{BIG_COLOUR}'> %s",
    # `playersay` takes the player's Steam id, which is also their cid here.
    tell_template=f"playersay %(cid)s <FONT COLOR='#{PRIVATE_COLOUR}'> %(text)s",
    kick_template='kick %(cid)s "%(reason)s"',
    # No permanent verb: a ban is a very long tempban, and the engine counts in days.
    ban_template=f'kickban %(guid)s "%(reason)s" {PERMANENT_DAYS}',
    tempban_template='kickban %(guid)s "%(reason)s" %(days)s',
    tempban_max_minutes=0,  # no known ceiling
    unban_template="unban %(guid)s",
    # The player list is a synchronous reply on this engine, unlike the other pushed families -- but
    # its shape is nothing like a Quake3 status table, so the parser reads it rather than a pattern.
    status_commands=(),
    status_patterns=(),
    rotation_cvar="",  # no cvars at all; the map list is a command instead
    maplist_command="getmaplist false",
    # Two commands, because one does not do it: `addmap` puts the map next in the rotation and
    # `nextmap` goes there. The classic parser sent exactly this pair.
    map_template="addmap %s 1\nnextmap",
    rotate_command="nextmap",
)

ALL: dict[str, GameProfile] = {RAVAGED.name: RAVAGED}

__all__ = ["ALL", "BIG_COLOUR", "PERMANENT_DAYS", "PRIVATE_COLOUR", "RAVAGED", "RAVAGED_TEAMS", "SAY_COLOUR"]
