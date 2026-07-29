"""Homefront, as data. One title, and a command vocabulary unlike anything else here.

Three things drive the profile:

* **Every penalty verb is keyed on the Steam id**, and so is a private message: `adminpm <uid> <text>`.
  The player's *name* is the slot handle (`cid`), because that is what the chat lines carry — so this
  title needs both, which is why `%(guid)s` appears here and in ``tell_template``.
* **The ban verb names the admin who issued it**, which no other engine's does. It goes out as
  `admin kickban "<uid>" "<admin>" "[B3] <reason>"`, and the classic parser read that name off the
  admin object without checking there was one — so an automatic ban (from a plugin, or the console)
  raised `AttributeError` instead of banning anybody. Here the runtime supplies the bot's own name
  when nobody typed the command.
* **Nothing can be asked synchronously.** `RETRIEVE PLAYERLIST` is answered by *pushed* `PLAYER`
  messages, one per player, so ``status_commands`` is empty — the same statement Altitude makes — and
  the roster is assembled by the parser instead.
"""

from __future__ import annotations

from b3.parsers.profile import GameProfile

#: Homefront team ids. 0 is the KPA, 1 the USA; 2 is spectators and 3 is "none yet", which maps to no
#: team for the reason Frostbite's 0 does — a placeholder read as a team makes everybody who has not
#: joined one look like team-mates, and every early kill look like a team kill.
HOMEFRONT_TEAMS = {"0": "red", "1": "blue", "2": "spec", "3": ""}

HOMEFRONT = GameProfile(
    name="homefront",
    family="homefront",
    # A Steam id is 17 digits, and the classic parser warns about anything else — `00` is what the
    # server reports for a banned player trying to connect, and `0` for one it has not resolved yet.
    guid_min_length=17,
    world_cid="",
    teams=HOMEFRONT_TEAMS,
    say_template="adminsay %s",
    saybig_template="adminbigsay %s",
    # By Steam id, not by slot: this engine has no notion of one.
    tell_template="adminpm %(guid)s %(text)s",
    kick_template='admin kick "%(guid)s"',
    ban_template='admin kickban "%(guid)s" "%(admin)s" "[B3] %(reason)s"',
    # No native timed ban: the engine has kick and kickban and nothing between them, so a tempban is
    # a kick that the bot re-applies from its own record when the player comes back.
    tempban_template=None,
    unban_template="admin unban %(guid)s",
    # Nothing to ask. The reply to `RETRIEVE PLAYERLIST` arrives as pushed messages, so the parser
    # keeps the roster and `Bot.get_players` reads it from there.
    status_commands=(),
    status_patterns=(),
    rotation_cvar="",  # this engine has no cvars at all
    map_template="",  # and no way to load a named map: only "next"
    rotate_command="admin nextmap",
)

ALL: dict[str, GameProfile] = {HOMEFRONT.name: HOMEFRONT}

__all__ = ["ALL", "HOMEFRONT", "HOMEFRONT_TEAMS"]
