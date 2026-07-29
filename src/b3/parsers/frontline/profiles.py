"""Frontlines: Fuel of War, as data. One title, and every verb takes a `Name=value` field.

Three things drive the profile:

* **Bans are keyed on the ProfileID**, the player's account id, not on the slot they happen to be in.
  A slot is reused by the next player to take it, so a slot-keyed ban would land on a stranger.
* **A ban's duration is in minutes, and `BanTime=0` means for ever.** There is one verb for both, so
  the permanent form is the timed form with a zero.
* **Private messages take the slot**, because `PLAYERSAY` addresses a `PlayerID`. So this engine needs
  both identities in its templates, which is why `%(guid)s` and `%(cid)s` both appear below.
"""

from __future__ import annotations

from b3.parsers.profile import GameProfile

#: The game's team ids. 255 is "unknown" and deliberately absent: an unmapped id becomes an empty
#: team, which is what the rest of the bot reads as "not stated".
FRONTLINE_TEAMS = {"0": "red", "1": "blue", "2": "spec"}

FRONTLINE = GameProfile(
    name="frontline",
    family="frontline",
    # A ProfileID is a plain account number -- `1561500` is the one the classic tests quote -- so the
    # 32-character default would reject every real identity this engine has.
    guid_min_length=4,
    # No world lines at all: nothing on this engine reports a death by the environment.
    world_cid="",
    teams=FRONTLINE_TEAMS,
    # This engine has no colour codes, and cuts a line at 200 characters.
    line_length=200,
    say_template="SAY %s",
    # There is no attention-getting verb. The classic parser wrapped the text in `#...#` and sent it
    # as an ordinary SAY; saying so here is more honest than pretending the engine has two.
    saybig_template="SAY %s",
    tell_template='PLAYERSAY PlayerID=%(cid)s SayText="%(text)s"',
    kick_template='KICK ProfileID=%(guid)s Reason="%(reason)s"',
    # One verb for both durations: zero minutes means permanent.
    ban_template='BAN ProfileID=%(guid)s BanTime=0 Reason="%(reason)s"',
    tempban_template='BAN ProfileID=%(guid)s BanTime=%(minutes)s Reason="%(reason)s"',
    tempban_max_minutes=0,  # no known ceiling
    unban_template="UNBAN ProfileID=%(guid)s",
    # Nothing on this engine can be asked synchronously -- the server never says which command it is
    # answering -- so the roster comes from the pushed `PlayerList:` reply and the parser keeps it.
    status_commands=(),
    status_patterns=(),
    # No cvars either, and `MapList` is not a synchronous reply, so the rotation is read from the
    # pushed reply and held by the parser (`FlParser.get_maps`).
    rotation_cvar="",
    maplist_command="",
    map_template="ForceMapChange %s",
    rotate_command="NEXTMAP",
)

ALL: dict[str, GameProfile] = {FRONTLINE.name: FRONTLINE}

__all__ = ["ALL", "FRONTLINE", "FRONTLINE_TEAMS"]
