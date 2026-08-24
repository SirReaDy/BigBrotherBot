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
from b3.parsers.profile import GameProfile, VersionCheck

#: Frostbite team ids, as they appear in events. 0 is "no team yet" — a player still at the deploy
#: screen — which must not be mistaken for a team, or everybody waiting to spawn looks like allies.
FROSTBITE_TEAMS = {"0": "", "1": "red", "2": "blue", "3": "green", "4": "yellow"}

#: The server rejects a longer reason, so this is a hard limit rather than good manners.
MAX_REASON = 80

#: How long a centre-screen message stays up, in seconds. `admin.yell` takes a duration and this
#: engine's big message is the only one here that does, so the figure has to live somewhere; it is
#: the classic bot's own default. `poweradminbf3` lets an operator choose a different one for the
#: `!yell` commands, which is why those pass it rather than reading this.
BIG_MESSAGE_SECONDS = 10

#: The same figure for Frostbite 1, which counts that duration in **milliseconds** — the classic
#: `bfbc2.py` passed `duration=2400` for a message meant to be readable, and the plugin above it
#: passed 900 for a one-second countdown step. So the ten this generation was being sent was ten
#: *milliseconds*: `admin.yell` was accepted, and the message was gone before the frame it drew on.
#: The unit is the whole reason Frostbite 1 spells its yell out separately below.
BIG_MESSAGE_MILLISECONDS = 10_000

#: The lowest build of each title these grammars were written against, lifted verbatim from the
#: classic parsers' own constants (`BF3_REQUIRED_VERSION` and its siblings). They are floors, not
#: targets: every server anybody is still running is far above them, and they exist to refuse one so
#: old that the events this parser expects had not been added yet. Medal of Honor 2010 has none,
#: because the classic checked only its name.
BF3_REQUIRED_BUILD = 1149977
BF4_REQUIRED_BUILD = 155011
BFH_REQUIRED_BUILD = 525698
MOHW_REQUIRED_BUILD = 323174
BFBC2_R9 = 527791

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
    # **This engine has a centre-screen message and the bot was not using it.** With no
    # `saybig_template` every `say_big` fell back to `say_template`, so `firstkill`'s announcement,
    # `spree`'s and anything else meant to be unmissable arrived as an ordinary chat line on all six
    # titles — the failure that leaves no trace anywhere. `admin.yell` is what the classic bot used
    # here, and Frostbite 1 names the `all` subset explicitly (`bfbc2.py` did) where Frostbite 2
    # leaves it off; both spellings are the ones that shipped and worked.
    saybig_template=f'admin.yell "%s" {BIG_MESSAGE_SECONDS} all',
    # Two things this engine can do to one player that no template above covers. `admin.killPlayer`
    # kills without touching the scoreboard, which is what makes it an admin verb rather than a
    # penalty, and `admin.movePlayer` is the only way to change somebody's team — the trailing
    # `true` forces the kill that a mid-life team change needs.
    player_verbs={
        "kill": 'admin.killPlayer "%(cid)s"',
        "move": 'admin.movePlayer "%(cid)s" %(team)s %(squad)s true',
    },
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
    # This engine has no cvars, so the server's own name, its player limit and its gametype only
    # exist in this reply. Every other family here gets them from a cvar dump.
    server_info_command="serverInfo",
    # **A Frostbite setting is set by naming it, with no verb in front.** The default template here
    # is `set <name> "<value>"`, which on this engine is the unknown command `set` followed by two
    # arguments — so every `Console.set_cvar` on all six titles was answered with `UnknownCommand`
    # and nothing changed, in silence. Reading was already right (`get_cvar_template` is the bare
    # name), which is what made the asymmetry invisible. Callers pass the full `vars.` name, as they
    # already do to read one.
    set_template='%(name)s "%(value)s"',
    # Battlefield ran PunkBuster, and the classic Frostbite parser built the service unconditionally.
    # It matters less here than on Quake 3, since an EA GUID is already a persistent identity — what
    # it buys is the second id, the screenshot request and PunkBuster's own ban list.
    #
    # **And it needs the second line to work at all.** On this engine a PunkBuster verb is not a
    # command, it is an argument: every command here is a word list with a known verb at the front,
    # so `PB_SV_Ver` sent bare is answered `UnknownCommand` — which is precisely what the startup
    # probe reads as "PunkBuster is not installed here". So the service was never built on any of the
    # six titles, and the profile saying `punkbuster=True` is what made that look intentional.
    punkbuster=True,
    punkbuster_template='punkBuster.pb_sv_command "%s"',
    # **Loading a named map is not a command on this engine, so there is no template for it.** It
    # means putting the map into the rotation at a computed index and then pointing the server at
    # that index — four commands, one of whose arguments is arithmetic over a reply — so it lives on
    # `FrostbiteClient.change_map`, which `Bot.change_map` reaches through the same seam it uses for
    # the player block. Left empty deliberately: what was here before was `admin.runNextRound "%s"`,
    # which is the Frostbite *1* rotate verb with a map name glued on that it does not take, so
    # `!map <anything>` advanced the round and loaded whatever was next. The map name went nowhere
    # and nothing reported it.
    map_template="",
    # `!map metro, rush, 2`. A comma, because a Frostbite map is called "Grand Bazaar" and splitting
    # on whitespace would make half the rotation untypeable.
    map_arguments=("gamemode", "rounds"),
    map_argument_separator=",",
    rotation_cvar="",  # the map list is a command, not a cvar, and it is paged
)

#: Frostbite 1 — Bad Company 2 and the 2010 Medal of Honor. Both report a level path, so their map
#: tables are matched by longest prefix (b3.parsers.frostbite.maps).
#:
#: **The one place the two generations part company.** Every verb the bot uses for chat, kicks and
#: bans is identical across all six titles, which is what makes one profile serve them — but the map
#: list is not: Frostbite 1 keeps a flat list of level names and moves it on with a verb of its own,
#: where Frostbite 2 keeps (map, gamemode, rounds) entries and uses `mapList.runNextRound`. Sending
#: either generation the other's verb is an unknown command, so `!maprotate` did nothing on these two
#: until now. `gamemode`/`rounds` go with it: this generation's map list has nowhere to put them, so
#: they are not offered rather than accepted and dropped.
#:
#: **And the two titles in it do not share their round verbs either**, which is the second thing
#: asserting sameness hid. Bad Company 2 moves the game on with `admin.runNextLevel` and restarts it
#: with `admin.restartMap`; the 2010 Medal of Honor has neither and uses `admin.runNextRound` and
#: `admin.restartRound`. The classic wrote a parser per title and each used its own pair, so nothing
#: there ever had to notice; here one profile served both and carried Medal of Honor's, so
#: `!maprotate` and `!map` on Bad Company 2 ended with a verb that title has not got — the map was
#: inserted and pointed at, and then the round never advanced. Declared per title below.
_FROSTBITE1 = replace(_BASE, map_arguments=(), map_argument_separator=",")
BFBC2 = replace(
    _FROSTBITE1,
    name="bfbc2",
    map_names=maps.BFBC2_MAPS,
    version_check=VersionCheck("version", "BFBC2", BFBC2_R9),
    rotate_command="admin.runNextLevel",
    # Milliseconds on this generation — see BIG_MESSAGE_MILLISECONDS. Ten of them is not a message.
    saybig_template=f'admin.yell "%s" {BIG_MESSAGE_MILLISECONDS} all',
    server_verbs={
        # This title's round control, which is spelled in levels rather than rounds: there is no
        # `admin.runNextRound` here. Named `cyclemap` and `map_restart` because that is what they do
        # and what every other family here calls them.
        "cyclemap": "admin.runNextLevel",
        "map_restart": "admin.restartMap",
        # `admin.runScript` runs a file of RCON commands that lives on the *game server*. The same
        # verb name Urban Terror's `exec` carries, for the same reason: a plugin asking "can this
        # server run a config file for me?" should not have to know which engine it is talking to.
        "exec": 'admin.runScript "%(file)s"',
        # The centre-screen message with a duration the caller chose. Frostbite 1 names the `all`
        # subset explicitly, and counts in milliseconds — hence `ms` rather than F2's `seconds`, so
        # that a plugin written for the other generation cannot quietly pass the wrong unit.
        "yell": 'admin.yell "%(text)s" %(ms)s all',
    },
    player_verbs={
        **_BASE.player_verbs,
        "yell_player": 'admin.yell "%(text)s" %(ms)s player "%(cid)s"',
    },
)
#: Medal of Honor 2010 is the one title with no build floor — the classic checked its name only.
MOH = replace(
    _FROSTBITE1,
    name="moh",
    map_names=maps.MOH_MAPS,
    version_check=VersionCheck("version", "MOH"),
    rotate_command="admin.runNextRound",
)

#: Frostbite 2 — BF3, BF4, Hardline and MoH Warfighter.
#:
#: The round verbs here are this generation's own spelling: Frostbite 1 has its own pair per title,
#: declared above. The yell subsets — a team, a squad — are Frostbite 2's, because the classic sent
#: those only here; `poweradminbfbc2` reaches its team and squad with the one yell verb Bad Company 2
#: is on record as taking rather than being given an invented grammar.
_FROSTBITE2 = replace(
    _BASE,
    rotate_command="mapList.runNextRound",
    saybig_template=f'admin.yell "%s" {BIG_MESSAGE_SECONDS}',
    server_verbs={
        # Round control. `round_next` advances without ending the current round properly;
        # `round_end` needs the winning team, which the server will not decide for itself.
        "round_next": "mapList.runNextRound",
        "round_restart": "mapList.restartRound",
        "round_end": "mapList.endRound %(team)s",
        # Restarts the game server itself. Named here rather than sent as raw rcon so that a plugin
        # can *ask* whether the title has it before offering an admin a command that does nothing.
        "server_shutdown": "admin.shutDown",
        # The centre-screen message, with the duration the caller chose rather than the profile
        # default — an operator sets it in `poweradminbf3`. A yell can be aimed at everybody, at a
        # team, at one squad or at one player, and the subset grammar is the engine's own.
        "yell": 'admin.yell "%(text)s" %(seconds)s',
        "yell_team": 'admin.yell "%(text)s" %(seconds)s team %(team)s',
        "yell_squad": 'admin.yell "%(text)s" %(seconds)s squad %(team)s %(squad)s',
    },
    player_verbs={
        **_BASE.player_verbs,
        "yell_player": 'admin.yell "%(text)s" %(seconds)s player "%(cid)s"',
    },
)
BF3 = replace(
    _FROSTBITE2,
    name="bf3",
    map_names=maps.BF3_MAPS,
    version_check=VersionCheck("version", "BF3", BF3_REQUIRED_BUILD),
)
BF4 = replace(
    _FROSTBITE2,
    name="bf4",
    map_names=maps.BF4_MAPS,
    version_check=VersionCheck("version", "BF4", BF4_REQUIRED_BUILD),
)
#: Hardline calls itself `BFHL`, not `BFH` — the title id and the server's own word for it differ,
#: which is exactly why this is data rather than derived from `name`.
BFH = replace(
    _FROSTBITE2,
    name="bfh",
    map_names=maps.BFH_MAPS,
    version_check=VersionCheck("version", "BFHL", BFH_REQUIRED_BUILD),
)
MOHW = replace(
    _FROSTBITE2,
    name="mohw",
    map_names=maps.MOHW_MAPS,
    version_check=VersionCheck("version", "MOHW", MOHW_REQUIRED_BUILD),
)

#: The two titles whose map list is the Frostbite 1 shape. Read by `cli` to tell the client which
#: dialect of the map list it is talking to — the only per-title thing that layer needs to know.
LEGACY_MAPLIST = frozenset({"bfbc2", "moh"})

#: Every Frostbite title, by the id used in `server.game`.
ALL: dict[str, GameProfile] = {p.name: p for p in (BFBC2, MOH, BF3, BF4, BFH, MOHW)}

__all__ = [
    "ALL",
    "BIG_MESSAGE_MILLISECONDS",
    "BF3",
    "BF3_REQUIRED_BUILD",
    "BF4",
    "BF4_REQUIRED_BUILD",
    "BFBC2",
    "BFBC2_R9",
    "BFH",
    "BFH_REQUIRED_BUILD",
    "MOHW_REQUIRED_BUILD",
    "FROSTBITE_TEAMS",
    "LEGACY_MAPLIST",
    "MAX_REASON",
    "MOH",
    "MOHW",
]
