"""Altitude, as data. One title, one family, and a transport unlike any other here.

Two things about this engine drive everything in the profile:

* **Commands go into a file, and nothing ever answers.** So ``status_command`` and ``rotation_cvar``
  are deliberately empty. They are not oversights: an empty ``status_command`` is what stops the
  runtime asking a question this engine cannot answer, which would otherwise be appended to the
  command file as a bogus command. The roster comes from the log instead — see
  :mod:`b3.parsers.altitude.parser`.
* **The verbs disagree about what identifies a player.** ``kick`` and ``serverWhisper`` take the
  player's *name*; ``addBan`` and ``removeBan`` take their *vapor id*. Neither takes the slot number
  that the log lines are keyed on, which is why ``%(name)s`` and ``%(guid)s`` appear here and
  ``%(cid)s`` does not.
"""

from __future__ import annotations

from b3.parsers.profile import GameProfile

#: Altitude team ids as they appear in the log, mapped to names.
#:
#: 2 is spectators. 3-11 are the plane colours, and the classic parser flattened every one of them
#: to "no team" — so nothing could tell a red player from a blue one, in a game with a two-team ball
#: mode. They are named here instead. That cannot resurrect the team-kill events the classic parser
#: said this game does not have: friendly fire is off, so the server never reports a kill between two
#: players on one team, whatever the bot would call it.
#:
#: 0 and 1 map to "" — no team — for the reason Frostbite's team 0 does: a placeholder id that gets
#: treated as a team makes everyone who has not picked one look like teammates.
ALTITUDE_TEAMS = {
    "0": "",
    "1": "",
    "2": "spec",
    "3": "red",
    "4": "blue",
    "5": "green",
    "6": "yellow",
    "7": "orange",
    "8": "purple",
    "9": "azure",
    "10": "brown",
    "11": "pink",
}

ALTITUDE = GameProfile(
    name="altitude",
    family="altitude",
    # The log's own slot for "not a player" — a plane flying into the ground reports player -1.
    world_cid="-1",
    # A vapor id is a 36-character UUID. Nothing shorter is one, so nothing shorter authenticates.
    guid_min_length=36,
    teams=ALTITUDE_TEAMS,
    say_template="serverMessage %s",
    # By name, because that is what the verb takes. A name with a space in it is a real problem
    # here and it is recorded in the internal TODO rather than papered over.
    tell_template="serverWhisper %(name)s %(text)s",
    saybig_template=None,  # no centre-screen verb; falls back to serverMessage
    kick_template="kick %(name)s",
    # Two verbs, because `addBan` only adds to the ban list -- it does not remove the player who is
    # already connected. The newline is how this transport says "and then": see
    # b3.net.altitude.AltitudeCommandFile.write. `1 forever` is the game's own idiom for a permanent
    # ban: the duration is ignored when the unit is `forever`.
    ban_template="addBan %(guid)s 1 forever %(reason)s\nkick %(name)s",
    tempban_template="addBan %(guid)s %(minutes)s minute %(reason)s\nkick %(name)s",
    tempban_max_minutes=0,  # no known ceiling
    unban_template="removeBan %(guid)s",
    # The same verb, offered a second way and for a different job: **this game bans a player it
    # kicks by vote for two minutes of its own accord**, and `callvote`'s `protect:` section lifts
    # that for somebody it would have defended. It cannot use `Console.unban` for it — that would
    # also lift whatever ban the *bot* holds on them, which on a vote-kicked admin is precisely the
    # record nobody wants dropped — so the engine verb is offered on its own.
    player_verbs={"lift_ban": "removeBan %(guid)s"},
    # Nothing can be asked of this engine. Empty is load-bearing -- see the module docstring.
    status_commands=(),
    status_patterns=(),
    rotation_cvar="",
    map_template="changeMap %s",
    rotate_command="",  # no verb: the classic parser's rotateMap() was an empty method
    # Never used: there is no RCON socket to frame anything for. Left at the default rather than
    # inventing a value, since b3.net.rcon is not involved in this family at all.
    rcon_dialect="quake3",
)

ALL: dict[str, GameProfile] = {ALTITUDE.name: ALTITUDE}

__all__ = ["ALL", "ALTITUDE", "ALTITUDE_TEAMS"]
