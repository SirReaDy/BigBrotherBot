"""The six Frostbite titles, as data.

Two protocol generations — ``frostbite`` (Bad Company 2, Medal of Honor 2010) and ``frostbite2``
(BF3, BF4, Battlefield Hardline, MoH Warfighter) — and the striking thing about them is that the
**RCON verbs are identical**. The generations differ in the events they emit and the fields their
player block carries, both of which are read by name rather than by position, so all six titles are a
handful of lines each. That is the same result the eight Call of Duty titles gave, arrived at from the
opposite direction.

Three things are true of every title here and of no other family:

* **A player's handle is their name.** `admin.kickPlayer` takes a name; there are no slot numbers. So
  `cid` holds the name, and a rename mid-session is a genuinely different player as far as the verbs
  are concerned.
* **Bans are by GUID and are native.** `banList.add guid <id> seconds <n> <reason>` means a temporary
  ban is enforced by the server, and it survives a bot restart because it lives in the server's own
  ban list. Unlike BattlEye, the *removal* takes the GUID too, so `!unban` is one command.
* **Reasons are capped at 80 characters.** The server rejects the command outright past that, so this
  is not cosmetic: an over-long reason means the ban does not happen.
"""

from __future__ import annotations

from dataclasses import replace

from b3.parsers.frostbite import maps
from b3.parsers.profile import GameProfile

#: Frostbite team ids, as they appear in events. 0 is "no team yet" — a player still at the deploy
#: screen — which must not be mistaken for a team, or everybody waiting to spawn looks like allies.
FROSTBITE_TEAMS = {"0": "", "1": "red", "2": "blue", "3": "green", "4": "yellow"}

#: The server rejects a longer reason, so this is a hard limit rather than good manners.
MAX_REASON = 80

_BASE = GameProfile(
    name="",  # each title names itself below
    family="frostbite",
    # An EA GUID is long; nothing shorter is a real one. There is no "world" slot on this engine —
    # a kill with no killer arrives with an empty name, which the parser handles by name, not by id.
    guid_min_length=16,
    world_cid="",
    teams=FROSTBITE_TEAMS,
    max_reason_length=MAX_REASON,
    # `admin.say <text> all` is everyone; `admin.say <text> player <name>` is one person. The values
    # are quoted because this engine takes *word lists* — see b3.net.frostbite.split_command.
    say_template='admin.say "%s" all',
    tell_template='admin.say "%(text)s" player "%(cid)s"',
    kick_template='admin.kickPlayer "%(cid)s" "%(reason)s"',
    # By GUID, so it holds when they come back under another name — and it lives in the server's own
    # ban list, so it holds when the bot is not running.
    ban_template='banList.add guid "%(guid)s" perm "%(reason)s"',
    tempban_template='banList.add guid "%(guid)s" seconds %(seconds)s "%(reason)s"',
    tempban_max_minutes=0,  # no engine ceiling
    unban_template='banList.remove guid "%(guid)s"',
    # The event stream is opt-in: without this the connection is silent, and the bot would look
    # broken while being perfectly connected. Sent by the client on login rather than left here,
    # because it has to happen *before* anything can be read.
    startup_commands=(),
    # There is no log file and no status table: `admin.listPlayers` returns a structured block, read
    # by the client itself (b3.parsers.frostbite.status).
    status_commands=("admin.listPlayers all",),
    map_template='admin.runNextRound "%s"',
    rotate_command="mapList.runNextRound",
    rotation_cvar="",  # the map list is a command, not a cvar
)

#: Frostbite 1 — Bad Company 2 and the 2010 Medal of Honor. Both report a level path, so their map
#: tables are matched by longest prefix (b3.parsers.frostbite.maps).
BFBC2 = replace(_BASE, name="bfbc2", map_names=maps.BFBC2_MAPS)
MOH = replace(_BASE, name="moh", map_names=maps.MOH_MAPS)

#: Frostbite 2 — BF3, BF4, Hardline and MoH Warfighter.
BF3 = replace(_BASE, name="bf3", map_names=maps.BF3_MAPS)
BF4 = replace(_BASE, name="bf4", map_names=maps.BF4_MAPS)
BFH = replace(_BASE, name="bfh", map_names=maps.BFH_MAPS)
MOHW = replace(_BASE, name="mohw", map_names=maps.MOHW_MAPS)

#: Every Frostbite title, by the id used in `server.game`.
ALL: dict[str, GameProfile] = {p.name: p for p in (BFBC2, MOH, BF3, BF4, BFH, MOHW)}

__all__ = ["ALL", "BF3", "BF4", "BFBC2", "BFH", "FROSTBITE_TEAMS", "MAX_REASON", "MOH", "MOHW"]
