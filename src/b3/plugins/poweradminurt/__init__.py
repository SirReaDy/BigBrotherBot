"""Urban Terror's own admin commands, and the policies that run behind them.

A port of the classic `poweradminurt`: 3,846 lines across `iourt41.py` and two per-version subclasses
of itself, holding forty-nine commands and eleven features that run on their own.

**Player control.** `!paslap`, `!panuke`, `!pakill`, `!pamute` / `!paunmute` — each one an engine verb,
so each asks whether this title has it and says so plainly when it does not.

**Moving players between teams.** `!paforce` (with its lock), `!paswap`, `!paswapteams` and
`!pashuffleteams`. The last two name no player at all, which is what `GameProfile.server_verbs` is for.

**The team balancer**, which is not a command but a policy: it keeps the sides the same size, moving
whoever joined theirs most recently. `!pateams` is its manual trigger. With it comes the classic's
`ignoreSet` — a quiet window after anybody moves players about on purpose, without which the balancer
spends its next pass undoing a `!pashuffleteams`.

**The skill balancer**, which is the same idea about a different quantity: two sides of four are even
in numbers and not in anything else. Each player gets a score from what they have done *since they
joined the team they are on* — kill ratio, net contribution per minute, headshot ratio, and what they
did for the mission — and a shuffle deals everybody out repeatedly and keeps the evenest arrangement
found. `!paskuffle` shuffles, `!pabalance` gets the same result by moving as few players as it can,
`!paunskuffle` deliberately makes the teams unfair so the other two can be watched working,
`!paadvise` says which side is winning and how badly, and `!paautoskuffle` chooses what happens
automatically. Where the score cannot be improved any further, the shuffle distributes the *snipers*
instead: a rifle that kills in one shot decides a game on its own if they all end up on one side.

**The name checker**, which is three rules about names: two players may not wear one name, a handful
of names nobody may wear at all (the default nickname somebody never changed, and `all`, which is the
word admin commands use to mean everybody), and a limit on how often one player may rename during a
map — somebody who changes name every thirty seconds cannot be talked about, complained about or
reported. It does **not** overlap `nickreg`, which was worth checking before writing it: that plugin
answers "is this a name registered to somebody else?", and this one answers "is this a name at all?".

**Bot support**, which keeps the server populated with AI players — but only on the maps an operator
has said are safe. The classic's own configuration file shouts "BOTSUPPORT IS VERY UNSTABLE! IT MAY
CRASH YOUR SERVER PLENTY!" and answers itself with a list of maps bots have been seen to survive; that
list *is* the feature, so an empty one means no map rather than every map.

**The spectator check**, which asks whoever has been watching rather than playing to do one or the
other, once the server is busy enough that somebody wants the slot. It warns; `admin`'s own escalation
is what eventually removes them, which is the classic's design and the right one — the number of
warnings before a kick is a server's policy, not this feature's.

**The match settings** — twenty-one commands that each write one cvar, so they are a *table*
(`GAMETYPES`, `TOGGLES`, `NUMBERS` below) rather than twenty-one near-identical methods. The gametype
switches (`!pactf`, `!pabomb`, `!pajump`, …), the limits (`!pacaplimit`, `!patimelimit`,
`!pafraglimit`), the toggles (`!painstagib`, `!pahardcore`, `!pafunstuff`, `!paskins`,
`!pawaverespawns`), the numbers (`!pasetgravity`, `!parespawndelay`, `!parespawngod`, `!pahotpotato`,
the wave delays), `!pastamina`, `!pamoon`, `!pasetnextmap` and `!pagear`.

**The server commands.** `!pabigtext`, `!paset`, `!paget`, `!pavote`, `!pamaprestart`, `!pamapreload`,
`!pacyclemap`, `!paexec` and `!papublic`.

**Still to come:** the other policies — headshot counter, rotation manager, match mode, vote delay.

**Not ported, deliberately:**

* `!paveto` — `callvote` owns vote cancellation, and it is not Urban-Terror-only. The classic's
  `callvote` plugin actually *unregistered* `paveto` from the admin plugin when it loaded, which is two
  plugins negotiating over one command name at runtime; here `callvote`'s `!veto` simply is the command.
* `!paversion` — it printed the plugin's own version string. `!plugin info` answers that for every
  plugin.
* **`radio_spam_protection`** — it is `spamcontrol`'s `radio:` section, for the same reason `censorurt`
  is `censor`'s `mute:` section: scoring a flooded channel is that plugin's job, and this one had a
  second scorer of its own only because the two were written by different people. It lived in the
  Urban Terror 4.2 subclass alone, so a 4.1 server never had it at all.

Changed from the classic, most of them faults:

* **One immunity rule, not three.** `!paslap` refused to touch anybody at or above `slap_safe_level`
  (60) unless the admin was level 90+; `!panuke` had **no check at all**, so an admin who could not slap
  a fellow admin could nuke one; and `!pamute` used a third rule (strictly-higher level). Here one
  setting covers all four player commands: you cannot use them on somebody at or above your own level.
* **A multi-slap is not a thread.** `!paslap bob 25` started a raw thread that slept a second between
  each of twenty-five writes. Repeats are a scheduled deadline here, so nothing sleeps and the whole
  thing stops when the plugin is disabled or the player leaves.
* **A mute always has an end.** The classic sent `mute <cid>` with an empty duration when none was
  given, which on these builds *toggles* — so a mute could outlive the bot that set it, with nothing
  recording that it was on. `!pamute` takes minutes and defaults to a configured number; the runtime
  holds the deadline (`Console.mute`), which is also what stops it fighting with `censor`'s ladder.
* **`!pavote` reads the cvar it is about to change.** The classic remembered the value at *bot start*
  for `reset` and the value at the last `off` for `on`, in instance attributes — so `!pavote on` after a
  restart re-enabled voting with whatever the plugin's default happened to be. Both are read from the
  server here, and `on` restores what was there before the last `off` in this session, falling back to
  the engine's own default.
* **The balancer works out the whole move in one pass** instead of moving, re-reading and moving again.
  The classic looped up to twenty-five times: force one player, then ask the server for `g_redteamlist`
  and `g_blueteamlist` and count the characters. The counts it got back could not include the move it
  had just made — an rcon round trip is faster than the server processing a `forceteam` and writing the
  new team out — so it picked the same player again, and again. Here the teams are counted from the
  roster the bot already keeps, how many have to move is arithmetic, and they all move at once.
* **A switch is only reversed if it is the switch that unbalanced the teams.** The classic's
  team-change handler asked "are the teams uneven?" and not "did *this* player make them uneven", so
  with red on five and blue on two, somebody joining **blue** — helping — was forced to "the smaller
  team", which is the one they had just joined, and charged two fabricated suicide events as a
  stats-harvest penalty. The message explaining the penalty was inside `if xlrstats is loaded`, so on a
  server without it the points came off in silence. There is no penalty here: they are put back, and
  told why.
* **Nothing is announced unless something happens.** The classic announced "Autobalancing Teams!" the
  moment it found the teams uneven, and only then looked for somebody to move — so on a server where
  every candidate was an admin or locked, it announced a balance it then did not perform, every
  interval, for as long as the teams stayed uneven.

* **The skill score is keyed on the slot, not the database id.** The classic keyed it on `Client.id`,
  which is None for anybody the bot has not authenticated — and on a Quake3 server without `cl_guid`
  that is everybody. Every unauthenticated player shared the key `None` and overwrote each other's
  figures, so the shuffle was built from one player's score wearing everybody's name.
* **A head hit is counted here rather than by an unrelated feature.** The head-hit figures the skill
  score reads were written *only* by `headshotcounter`, a separate feature that is off by default. On
  any server that had not switched it on, `hsratio` was zero for everybody — and a measure that is the
  same for everybody is dropped from the score, so one of its three weighted components never
  contributed at all.
* **The score is divided by the weight that counted.** The classic divided by the total weight of
  every measure, including the ones it had just skipped and the two it read from `xlrstats` — so on a
  server without that plugin every score came out at roughly half of what the weights say. That
  matters because the autobalance threshold on CTF and bomb mode is compared against this figure, so
  `skillbalance_difference` quietly meant about twice what an operator reading it would think.
* **A shuffle has to beat the arrangement being played.** The classic's search started from nothing,
  so the first deal it tried always won: on teams that were already even it moved half the server
  about and then reported the difference as unchanged. The search starts from the current teams here,
  and `!paskuffle` on even teams says it cannot improve on them.
* **The wait between shuffles is the wait it advertises.** "Teams changed recently, please wait a
  while" was behind an `and` of three conditions, one of which was "a quiet window is open" — so a
  player who asked twice in a row almost always got two shuffles.
* **`!paadvise` refuses rather than reporting on an empty server.** It printed "Avg kill ratio diff is
  0.00, skill diff is 0.00" to nobody in particular.

* **The spectator check reads `g_maxGameClients` when it runs.** The classic read it once in
  `onStartup`, straight into an attribute with no error handling — so on a server that answered
  nothing for that cvar the read raised and the **whole plugin** failed to load, taking all
  forty-nine commands with it. Reading it per check also means an operator who changes it mid-map
  gets what they asked for.
* **Its exemption level defaults to something that exempts somebody.** The classic's default was
  `0`, and the rule is "at or above this level is exempt" — so a server that set the interval and
  left the rest of the section alone ran the check, skipped every player on it, and said nothing.
* **A bot is counted but never warned.** It fills a slot somebody wants, so it counts towards the
  server being full; it cannot read a warning, so it does not get one. The classic warned bots, and
  the escalation eventually kicked one — whereupon its own bot support added another.

* **Names are compared as players see them.** The classic compared the raw strings, so `^1Bob` and
  `^2Bob` — the same name to everybody looking at the scoreboard — were not duplicates of each other,
  which is the exact case a duplicate check exists for. Its check for the default nickname went
  further and called `stripColors` on **its own constant**, which has no colours in it, rather than on
  the player's name, so `^1New UrT Player` walked straight past.
* **The forbidden names are a list.** `checkbadnames` was a boolean that turned on one hard-coded
  word.
* **A bot is not warned for sharing a name with another bot**, which several of them routinely do; the
  classic warned every one of them on every sweep. And the rename limit's exemption is a level rather
  than the classic's hard-coded `9`, which is not a group anybody has.

* **A plugin that is not running bots touches no bot cvar.** The classic wrote `g_spskill` on every
  config load whether or not bot support was on, and only ever wrote `bot_enable 1` — so switching
  the feature off and reloading left the engine's bot subsystem running with the count at zero, while
  a server whose operator manages bots by hand had its skill setting rewritten by a feature that was
  switched off. Nothing here writes a bot cvar until it has something to say about bots.
* **A map bots are not allowed on turns them off**, rather than being left alone. The classic's
  `botsupport()` did nothing at all in that case and relied on whoever called it having disabled them
  first — which its own config reload did not, so reloading on a map outside the list left the bots it
  had started running.

And two faults that were not in this plugin at all: `!paforce … lock` had never held anybody, because
**no Quake3 parser published `CLIENT_TEAM_CHANGE`**. The team is a field of the infostring rather than
a line of its own, so nothing noticed the field changing, and a subscriber to an event nobody raises
looks exactly like a subscriber to something that never happens. `CLIENT_NAME_CHANGE` had it too, for
the same reason: `censor` could not catch a player who connected with a clean name and then changed it,
and `nickreg` could not catch one putting on an admin's name mid-session. See
`Q3Parser._userinfo_events`.
"""

from __future__ import annotations

import logging
import random
import re
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from b3.core.commands import Command, CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_float, as_int, as_word, normalise_name
from b3.domain.client import Client
from b3.plugins.firstkill import is_headshot

log = logging.getLogger(__name__)

#: What the table-driven commands are: a handler taking the command's context.
HandlerType = Callable[[CommandContext], None]

#: The cvar Urban Terror gates voting with. `!pavote off` sets it to 0; `on` puts back what it was.
VOTE_CVAR = "g_allowvote"

#: What Urban Terror ships as `g_allowvote` when nobody has changed it — used only if the server had
#: voting off already when `!pavote on` was typed, so that the command does *something* sensible.
VOTE_DEFAULT = "536870908"

#: How many times a slap or a nuke may be repeated. The classic's own bound, and a sane one: 25
#: slaps at one a second is nearly half a minute of somebody being thrown about.
MAX_REPEATS = 25

#: Seconds between the repeats of a multi-slap, as the classic slept.
REPEAT_SECONDS = 1.0

#: Urban Terror's gametype numbers, by the command that selects them. From the classic's own
#: `setCvar('g_gametype', …)` calls — the numbers are the engine's and are not guessable.
GAMETYPES: dict[str, tuple[int, str]] = {
    "paffa": (0, "free for all"),
    "palms": (1, "last man standing"),
    "patdm": (3, "team deathmatch"),
    "pats": (4, "team survivor"),
    "paftl": (5, "follow the leader"),
    "pacah": (6, "capture and hold"),
    "pactf": (7, "capture the flag"),
    "pabomb": (8, "bomb mode"),
    "pajump": (9, "jump mode"),
    "pafreeze": (10, "freeze tag"),
    "pagungame": (11, "gun game"),
}

#: `on`/`off` commands: the cvar, and what each word writes to it.
TOGGLES: dict[str, tuple[str, str, str]] = {
    "painstagib": ("g_instagib", "1", "0"),
    "pahardcore": ("g_hardcore", "1", "0"),
    "pafunstuff": ("g_funstuff", "1", "0"),
    "paskins": ("g_skins", "1", "0"),
    "pawaverespawns": ("g_waverespawns", "1", "0"),
}

#: Commands that take a number and write it to a cvar. The bounds are this port's: the classic passed
#: whatever was typed straight through, so `!pasetgravity banana` set the gravity to `banana` and
#: `!patimelimit -5` was accepted by the plugin and then ignored by the server.
NUMBERS: dict[str, tuple[str, int, int, str]] = {
    "pacaplimit": ("capturelimit", 0, 100, "captures to win"),
    "patimelimit": ("timelimit", 0, 1440, "minutes in a round"),
    "pafraglimit": ("fraglimit", 0, 1000, "frags to win"),
    "pasetgravity": ("g_gravity", 0, 10000, "gravity (800 is normal)"),
    "parespawndelay": ("g_respawnDelay", 0, 300, "seconds before respawning"),
    "parespawngod": ("g_respawnProtection", 0, 60, "seconds of protection after respawning"),
    "pahotpotato": ("g_hotpotato", 0, 300, "seconds a flag may be held"),
    "pabluewave": ("g_bluewave", 0, 300, "seconds between blue respawn waves"),
    "paredwave": ("g_redwave", 0, 300, "seconds between red respawn waves"),
}

#: `!pastamina`, which is three named values rather than on/off.
STAMINA = {"default": "0", "regain": "1", "infinite": "2"}

#: `!pagear`. Urban Terror's `g_gear` bits **forbid** a weapon, so `all` is 0 and `none` is 63 — which
#: reads backwards and is the engine's, not ours.
GEAR_BITS = {"nade": 1, "snipe": 2, "spas": 4, "pistol": 8, "auto": 16, "negev": 32}
GEAR_ALL = 0
GEAR_NONE = sum(GEAR_BITS.values())

#: Gravity for `!pamoon on`, and what `off` restores when nothing was recorded.
MOON_GRAVITY = "100"
NORMAL_GRAVITY = "800"

#: A config file `!paexec` will run. Checked rather than sanitised: these engines read `;` as a command
#: separator, so a name with one in it is not a filename that needs cleaning up — it is somebody trying
#: to run two commands, and the right answer is no.
CONFIG_FILE_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

#: The two sides a balancer can move somebody between. `free` (Quake3's no-team, used by the
#: deathmatch gametypes) and `spec` are not sides: there is nothing to balance.
SIDES = ("red", "blue")

#: Urban Terror's gametype numbers by short name — the reverse of `GAMETYPES` above, so the numbers
#: are stated once. `game.gametype` holds the raw `g_gametype` cvar, which is a number.
GAMETYPE_NAMES = {number: name[2:] for name, (number, _what) in GAMETYPES.items()}

#: The classic's spellings for two of them, so a config carried across from it still means what it
#: said: it wrote `bm` for bomb mode and `dm` for free-for-all.
GAMETYPE_ALIASES = {"bm": "bomb", "dm": "ffa"}

#: What an admin may type for a team, and what the engine's `forceteam` calls it. `free` is not a team:
#: it releases a lock, which is the classic's own spelling and worth keeping because operators know it.
TEAMS = {
    "red": "red",
    "r": "red",
    "blue": "blue",
    "b": "blue",
    "spec": "s",
    "spectator": "s",
    "s": "s",
}

DEFAULTS: dict[str, object] = {
    # Level for the commands that act on a player.
    "player_command_level": 40,
    # Level for the ones that change a server setting.
    "server_command_level": 60,
    # Nobody may slap, nuke, kill or mute a player at or above their own level. The classic had three
    # different rules for this across three commands, one of which was no rule at all.
    "protect_peers": True,
    # Minutes for `!pamute` when the admin does not say.
    "default_mute_minutes": 5,
    # The word `!papublic off` builds the private password from. Digits are added to it so that the
    # password changes each time; without a word here the command refuses rather than setting a
    # password made only of digits, which is what the classic did while claiming it had refused.
    "private_password": "",
    # How many digits to add.
    "password_digits": 2,
    # -- the team balancer --------------------------------------------------------------------
    # Minutes between automatic checks; 0 turns the balancer off, which is the classic's own switch.
    "teambalance_interval": 30,
    # How many players one team may have more than the other before it counts as unbalanced.
    "teambalance_difference": 1,
    # Nobody at or above this level is moved, so an admin can go and help the weaker team.
    "teambalance_max_level": 60,
    # How a balance is announced: `bigtext` across the screen, `say` in the chat, or `off`. The
    # classic wrote 2, 1 and 0 for these.
    "teambalance_announce": "bigtext",
    # Gametypes to balance. Named, not numbered, and the classic's `bm`/`dm` spellings are understood.
    "teambalance_gametypes": "tdm, ctf, cah, ftl",
    # Gametypes played in rounds, where moving somebody mid-round takes them out of a round they are
    # playing: a balance falls due at the end of the round instead. A setting rather than the
    # classic's hard-coded pair, because which gametypes are round-based is a property of the server's
    # config as much as of the game.
    "teambalance_round_based": "ts, bomb",
    # Whether a player who unbalances the teams by switching is put straight back.
    "teambalance_on_team_change": True,
    # Seconds of quiet after anything moves players on purpose — a shuffle, a swap, a round start, the
    # bot adopting a server full of players. The classic's `ignoreSet`, and what stops the balancer
    # spending its next pass undoing a `!pashuffleteams`.
    "teambalance_quiet_seconds": 60,
    # Whether `!paforce <player> <team> lock` survives a map change. Off, as the classic had it: a lock
    # is usually for the rest of this map.
    "team_locks_permanent": False,
    # Level for `!pateams`. The classic let a regular ask, and asking only ever evens the teams.
    "teams_command_level": 2,
    # -- the skill balancer -------------------------------------------------------------------
    # What to do when the sides are uneven in *skill* rather than in numbers: `off`, `advise` (say
    # so), `balance` (move as few players as possible) or `shuffle` (deal everybody out again).
    # The classic wrote 0, 1, 2 and 3, which are still accepted.
    "skillbalance_mode": "off",
    # Minutes between skill checks; 0 leaves the commands working and nothing automatic.
    "skillbalance_interval": 0,
    # How lopsided the game has to feel before the mode above acts on it.
    "skillbalance_difference": 0.5,
    # Below this many players the figures are noise, so nothing is said and nothing is done.
    "skillbalance_min_players": 3,
    # Minutes a player below moderator must wait between asking for another shuffle.
    "skillbalance_min_interval": 2,
    # The most of the server `!pabalance` will move, as a fraction. Beyond it, a full shuffle.
    "skuffle_max_move_fraction": 0.3,
    # A player carrying an SR8 or PSG-1 counts as a sniper worth separating from the other snipers
    # only above this kill ratio. Below it they are carrying a rifle, not using one.
    "sniper_kill_ratio": 1.2,
    # -- the spectator check ------------------------------------------------------------------
    # Minutes between checks; 0 turns it off. Somebody warned repeatedly is kicked by `admin`'s own
    # escalation, so how quickly that happens is this interval times the warnings it takes.
    "speccheck_interval": 0,
    # Minutes a player may watch rather than play once the server is busy.
    "speccheck_max_spec_minutes": 5,
    # How many connected clients make the server busy enough to police the spectators. 0 works it
    # out from the server's own slots, minus any reserved ones.
    "speccheck_min_players": 0,
    # Nobody at or above this level is asked to play or leave. The classic's default for this was
    # **0**, which exempts everybody — so a server that set the interval and left the rest alone ran
    # the check, skipped every player, and reported nothing.
    "speccheck_max_level": 20,
    # How long each warning lives.
    "speccheck_warn_minutes": 5,
    # -- the name checker ---------------------------------------------------------------------
    # Minutes between sweeps for duplicate and forbidden names; 0 turns them off.
    "namecheck_interval": 0,
    # Whether two players wearing one name are both warned for it.
    "namecheck_duplicates": True,
    # Names nobody may wear, comma-separated and compared as players see them. `New UrT Player` is
    # the default nickname — somebody who never set one — and `all` is the word admin commands use
    # to mean everybody, so a player wearing it makes those commands ambiguous. The classic had a
    # setting called `checkbadnames` that turned on exactly one hard-coded word.
    "namecheck_forbidden_names": "New UrT Player, all",
    # How many times a player may change name during one map before being removed; 0 allows any
    # number. Somebody who renames every thirty seconds cannot be talked about or reported.
    "namecheck_max_changes": 7,
    # Nobody at or above this level is checked. The classic hard-coded 9 for the rename limit and
    # checked nothing at all for the rest.
    "namecheck_max_level": 20,
    # -- bot support --------------------------------------------------------------------------
    # Whether the server tops itself up with AI players. The classic's own config file shouts that
    # this may crash the server, which is why the list of maps below exists.
    "botsupport_enable": False,
    # How good they are, 1-5.
    "botsupport_skill": 4,
    # How many clients the server is kept topped up to.
    "botsupport_min_players": 4,
    # The maps bots are allowed on, and nothing outside it. Empty means no map: for a feature whose
    # own documentation warns it may take the server down, "not configured" has to mean "off".
    "botsupport_maps": "",
}

MESSAGES = {
    "pa_usage_player": "name a player: !{command} <player>",
    "pa_protected": "{name} is not somebody you can do that to",
    "pa_repeat_range": "that has to be a number from 1 to {limit}",
    "pa_slapped": "{name} has been slapped",
    "pa_nuked": "{name} has been nuked",
    "pa_killed": "{name} has been killed",
    "pa_muted": "{name} is muted for {minutes} minutes",
    "pa_unmuted": "{name} can talk again",
    "pa_unavailable": "this game has no {verb} command",
    "pa_cvar": "{name} is {value}",
    "pa_cvar_unset": "{name} is not set",
    "pa_cvar_changed": "{name} is now {value}",
    "pa_usage_set": "!paset <cvar> <value>",
    "pa_usage_get": "!paget <cvar>",
    "pa_usage_bigtext": "!pabigtext <what to announce>",
    "pa_vote_usage": "!pavote on|off",
    "pa_vote_on": "voting is on",
    "pa_vote_off": "voting is off",
    "pa_gametype": "the game is now {what}",
    "pa_toggle_usage": "!{command} on|off",
    "pa_toggle": "{what} is {state}",
    "pa_number_usage": "!{command} <{what}>",
    "pa_number_range": "{what} has to be a number from {low} to {high}",
    "pa_number_set": "{what}: {value}",
    "pa_stamina_usage": "!pastamina default|regain|infinite",
    "pa_stamina": "stamina is {what}",
    "pa_gear": "allowed: {allowed}",
    "pa_gear_none": "no weapons are allowed",
    "pa_gear_usage": "!pagear all|none|reset|+weapon|-weapon (weapons: {weapons})",
    "pa_gear_changed": "gear changed; allowed: {allowed}",
    "pa_nextmap_usage": "!pasetnextmap <map>",
    "pa_nextmap": "the next map will be {map}",
    "pa_nextmap_unsupported": "this game has no next-map setting",
    "pa_moon": "gravity is {value}",
    "pa_force_usage": "!paforce <player> <red|blue|spec|free> [lock]",
    "pa_forced": "{name} moved to {team}",
    "pa_forced_you": "you have been moved to {team}",
    "pa_forced_locked": "you have been moved to {team} and cannot switch",
    "pa_force_released": "{name} may choose their own team again",
    "pa_force_not_locked": "{name} was not locked to anything",
    "pa_force_denied": "you are locked to {team}",
    "pa_swap_usage": "!paswap <player> [player]",
    "pa_swap_same_team": "{first} and {second} are on the same team",
    "pa_swap_spectator": "{name} is a spectator, so there is nothing to swap",
    "pa_swapped": "{first} and {second} have changed places",
    "pa_teams_swapped": "the teams have been swapped",
    "pa_teams_shuffled": "the teams have been shuffled",
    "pa_map_restarted": "restarting the map",
    "pa_map_reloaded": "reloading the map",
    "pa_map_cycled": "moving on to the next map",
    "pa_exec_usage": "!paexec <config file>",
    "pa_exec_bad_name": "{name} is not a config file name I will run",
    "pa_exec": "running {name}",
    "pa_public_usage": "!papublic on|off",
    "pa_public_on": "the server is public again",
    "pa_public_off": "the server is going private",
    "pa_public_password": "the password is {password} — type !pamapreload to apply it",
    "pa_public_no_password": "set private_password in the plugin config first",
    "pa_teams_balancing": "balancing the teams",
    "pa_teams_already": "the teams are already even",
    "pa_teams_balanced": "the teams are even now",
    "pa_teams_stuck": "the teams are uneven, but there is nobody I am allowed to move",
    "pa_teams_pending": "the teams will be evened up at the end of this round",
    "pa_teams_moved_you": "you have been moved to {team} to even up the teams",
    "pa_teams_put_back": "that would have made the teams uneven, so you are back on {team}",
    "pa_skill_shuffling": "shuffling the teams by skill",
    "pa_skill_unshuffling": "putting the best players together — brace yourselves",
    "pa_skill_balancing": "evening the teams up by skill",
    "pa_skill_was_now": "team skill difference was {was}, now {now}",
    "pa_skill_no_improvement": "the teams cannot be made any more even than they are",
    "pa_skill_figures": "kill-ratio difference is {felt}, skill difference is {skill}",
    "pa_skill_fair": "the teams look fair",
    "pa_skill_now": "{team} team is now {word}",
    "pa_skill_remains": "{team} team remains {word}",
    "pa_skill_stronger": "{team} team has become {word}",
    "pa_skill_weaker": "{team} team is just {word}",
    "pa_skill_use_bal": " — !bal would even them up",
    "pa_skill_no_action": " — nothing worth doing about it yet",
    "pa_skill_too_soon": "the teams changed recently; give it a minute",
    "pa_skill_too_few": "not enough players to tell",
    "pa_skill_moved": "you are on {team} team now, to even the sides up",
    "pa_skill_moved_best": "you are on {team} team now — they needed the help",
    "pa_skill_mode": "skill balancing is {mode}; options are {options}",
    "pa_skill_mode_set": "skill balancing is now {mode}",
    "pa_skill_mode_usage": "!paautoskuffle {options}",
    "pa_spec_reason": "spectator too long on a full server",
    "pa_name_duplicate": "somebody else is already using that name",
    "pa_name_forbidden": "that name is not allowed here",
    "pa_name_changes_kick": "too many name changes",
    "pa_name_changes_left": "{count} more name changes allowed on this map",
}


#: What "skill" is made of, and how much each part counts. The classic's own weights, minus the two
#: it read from `xlrstats` — a plugin this project does not have, whose absence it filled with the
#: same constant for every player, which is a measure that says nothing and is dropped below anyway.
WEIGHTS = {
    "killratio": 1.0,
    "teamcontrib": 0.5,
    "hsratio": 0.3,
    # The mission counts for more than the scoreboard: a flag carrier who dies a lot is winning.
    "flagperf": 3.0,
    "bombperf": 3.0,
}

#: The measures that are damped for a player who has only just arrived. Two kills in ten seconds is
#: not evidence of anything, and the classic's readme records this being added after sprees right
#: after joining were spiking the arithmetic.
DAMPED = ("killratio", "teamcontrib", "hsratio")

#: Minutes on a team before a player's combat figures count in full.
DAMPING_MINUTES = 5.0

#: Below this, two floats are the same number. The classic's own `epsilon`.
EPSILON = 0.0001

#: The sliding window the "how does this game feel" figure is measured over, in minutes: shorter on
#: a busy server, because the same evidence arrives in less time.
WINDOW_MIN_MINUTES = 2.0
WINDOW_MAX_MINUTES = 4.0

#: How many arrangements a shuffle tries, and the score difference below which it stops trying to
#: improve the balance and starts distributing the snipers instead.
SHUFFLE_TRIES = 100
SHUFFLE_SLACK = 0.1

#: The letters Urban Terror's `gear` field uses for the two one-shot rifles: SR8 and PSG-1.
SNIPER_GEAR = ("Z", "N")

#: A player carrying one of those with a worse kill ratio than this is not a sniper worth spreading
#: out — the classic's own rule, and the reason it works.
SNIPER_KILL_RATIO = 1.2

#: `|felt difference|` is multiplied by this before being described in words. The readme that shipped
#: with the feature says 6 and gives narrower bands; the code said 5 and these bands, and the code is
#: what ran, so it is what is kept.
ADVICE_SCALE = 5.0

#: Above this, the game is worth doing something about. "A constant carefully reviewed by an eminent
#: team of trained Swedish scientistians", says the classic, and it is as good a number as any.
UNFAIR = 2.31

#: How one side's dominance is described, by upper bound.
ADVICE_WORDS: tuple[tuple[float, str], ...] = (
    (1.0, ""),
    (2.0, "stronger"),
    (4.0, "dominating"),
    (6.0, "overpowering"),
    (8.0, "supreme"),
    (10.0, "godlike"),
    (float("inf"), "probably cheating"),
)

#: The cvars Urban Terror keeps its AI players behind: whether the subsystem is on at all, how many
#: clients the server should top itself up to, and how good they are (1-5).
BOT_ENABLE_CVAR = "bot_enable"
BOT_COUNT_CVAR = "bot_minplayers"
BOT_SKILL_CVAR = "g_spskill"

#: How few name changes have to be left before the player is told how many. The classic's number.
NAME_CHANGE_WARNING_AT = 4

#: What `!paautoskuffle` accepts, including the classic's numbers.
SKILL_MODES = {
    "off": "off",
    "0": "off",
    "none": "off",
    "advise": "advise",
    "1": "advise",
    "balance": "balance",
    "2": "balance",
    "shuffle": "shuffle",
    "3": "shuffle",
    "skuffle": "shuffle",
}
SKILL_MODE_LIST = "off, advise, balance, shuffle"


@dataclass
class SkillRecord:
    """What one player has done since they joined the team they are on.

    Reset on a team change rather than compared against a saved baseline, which is what the classic
    did with nine `prev_` variables it had to remember to re-take in four places.
    """

    joined: float = 0.0
    kills: int = 0
    deaths: int = 0
    team_kills: int = 0
    head_hits: int = 0
    flag_taken: bool = False
    flag_captured: int = 0
    flag_returned: int = 0
    bomb_planted: int = 0
    bomb_defused: int = 0
    #: `(when, +1 for a kill / -1 for a death)`, for the sliding window the advice is measured over.
    history: list[tuple[float, int]] = field(default_factory=list)


@dataclass
class ShuffleResult:
    """The best pair of sides a search found, and how they compare with the ones being played."""

    old_diff: float
    scores: dict[str, float]
    #: The evenest arrangement found above the slack threshold.
    blue: list[Client] | None = None
    red: list[Client] | None = None
    diff: float | None = None
    #: The best arrangement found *within* the slack threshold, judged by how the snipers fall.
    sniper_blue: list[Client] | None = None
    sniper_red: list[Client] | None = None
    sniper_diff: float | None = None

    def best(self) -> tuple[list[Client] | None, list[Client] | None]:
        """An arrangement already even enough beats a merely evener one."""
        if self.sniper_blue is not None:
            return self.sniper_blue, self.sniper_red
        return self.blue, self.red

    def chosen_diff(self) -> float | None:
        return self.sniper_diff if self.sniper_blue is not None else self.diff


@dataclass
class Repeat:
    """A slap or a nuke that has more to come."""

    verb: str
    client: Client
    remaining: int
    due: float
    admin: Client | None = None


class PoweradminurtPlugin(Plugin):
    """Urban Terror's admin commands: the player and server-setting ones."""

    requires_plugins = ("admin",)
    #: Not `requires_parsers`: what each command needs is a *verb*, and it asks. A title that has
    #: none registers the commands and answers that it cannot — which is how an operator finds out,
    #: rather than by the plugin refusing to load with a list of game names in the message.
    requires_parsers = None

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.pending: list[Repeat] = []
        #: What `g_allowvote` was before the last `!pavote off` in this session.
        self._vote_was: str | None = None
        #: What `g_gravity` was before `!pamoon on`, and what `g_gear` was when this plugin started.
        #: Read from the server rather than configured, so `off`/`reset` put back what was there.
        self._gravity_was: str | None = None
        self._gear_was: int | None = None
        #: Epoch until which the automatic checks stand down — the classic's `_ignoreTill`.
        self._quiet_until: float = 0.0
        #: A balance that fell due mid-round on a round-based gametype, waiting for the round to end.
        self._balance_pending = False
        #: Whether the round has ended. A balance asked for after that need not wait for anything.
        self._round_ended = False
        #: A skill balance held over until the round ends. Separate from `_balance_pending` because
        #: the two policies can each fall due in the same round and mean different things.
        self._skill_pending: Callable[[], None] | None = None
        #: When the teams were last changed by either balancer, for the rate limit and the damping.
        self._last_balance = 0.0
        #: The last thing `advise` said, so it can say "remains" and "has become" rather than
        #: repeating itself: `(team, word, magnitude)`.
        self._last_advice: tuple[str, str, float] | None = None
        #: Whether this plugin currently has the server topping itself up with AI players, and
        #: whether it has ever written the bot cvars at all. Both so that a server whose operator
        #: manages bots by hand is not quietly switched over by a plugin that is not using them.
        self._bots_running = False
        self._bot_cvars_written = False
        #: Seeded in tests so a shuffle can be asserted on. The classic used the module-level
        #: `random`, which is why none of its shuffling had a test.
        self.random = random.Random()

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        player_level = as_int(self.settings.get("player_command_level"), 40)
        server_level = as_int(self.settings.get("server_command_level"), 60)
        for name in ("paslap", "panuke", "pakill", "pamute", "paunmute", "paforce", "paswap"):
            self._set_level(name, player_level)
        for name in (
            "pabigtext",
            "paset",
            "paget",
            "pavote",
            "paswapteams",
            "pashuffleteams",
            "pamaprestart",
            "pamapreload",
            "pacyclemap",
            "papublic",
        ):
            self._set_level(name, server_level)
        for name in (
            *GAMETYPES,
            *TOGGLES,
            *NUMBERS,
            "pastamina",
            "pamoon",
            "pagear",
            "pasetnextmap",
        ):
            self._set_level(name, server_level)
        self._set_level("pateams", as_int(self.settings.get("teams_command_level"), 2))

    def register_commands(self) -> None:
        """The decorated commands, then the twenty-one table-driven ones."""
        super().register_commands()
        self._register_settings()

    def _register_settings(self) -> None:
        """Register one command per row of the setting tables.

        Written this way because the classic wrote them out one method at a time, and they drifted:
        `!painstagib` validated its argument and `!pafraglimit` two hundred lines away did not, so
        `!pafraglimit banana` set `fraglimit` to `banana` and told the admin it had worked.
        """
        level = as_int(self.settings.get("server_command_level"), 60)
        for name, (number, what) in GAMETYPES.items():
            self._add(name, level, self._gametype_handler(number, what), f"{name} - {what}")
        for name in TOGGLES:
            self._add(name, level, self._toggle_handler(name), f"{name} <on|off>")
        for name, (_cvar, low, high, what) in NUMBERS.items():
            self._add(name, level, self._number_handler(name), f"{name} <{low}-{high}> - {what}")
        self._add("pastamina", level, self.cmd_pastamina, "pastamina <default|regain|infinite>")
        self._add("pamoon", level, self.cmd_pamoon, "pamoon <on|off> - low gravity")
        self._add("pagear", level, self.cmd_pagear, "pagear [all|none|reset|+weapon|-weapon]")
        self._add(
            "pasetnextmap",
            level,
            self.cmd_pasetnextmap,
            "pasetnextmap <map> - the map after this one",
        )

    def _add(self, name: str, level: int, handler: HandlerType, help_text: str) -> None:
        cmd = Command(name=name, handler=handler, min_level=level, help=help_text, plugin=self)
        self.console.command_registry.register(cmd)
        self._commands.append(cmd)

    def _gametype_handler(self, number: int, what: str) -> HandlerType:
        def handler(ctx: CommandContext) -> None:
            self.console.set_cvar("g_gametype", str(number))
            log.info("poweradminurt: %s set the gametype to %s", ctx.client.name, what)
            ctx.reply(self.message("pa_gametype", what=what))

        return handler

    def _toggle_handler(self, name: str) -> HandlerType:
        cvar, on_value, off_value = TOGGLES[name]

        def handler(ctx: CommandContext) -> None:
            wanted = ctx.args.strip().lower()
            if wanted not in ("on", "off"):
                ctx.reply(self.message("pa_toggle_usage", command=name))
                return
            self.console.set_cvar(cvar, on_value if wanted == "on" else off_value)
            ctx.reply(self.message("pa_toggle", what=cvar, state=wanted))

        return handler

    def _number_handler(self, name: str) -> HandlerType:
        cvar, low, high, what = NUMBERS[name]

        def handler(ctx: CommandContext) -> None:
            text = ctx.args.strip()
            if not text:
                ctx.reply(self.message("pa_number_usage", command=name, what=what))
                return
            if not text.lstrip("-").isdigit() or not low <= int(text) <= high:
                ctx.reply(self.message("pa_number_range", what=what, low=low, high=high))
                return
            self.console.set_cvar(cvar, text)
            ctx.reply(self.message("pa_number_set", what=what, value=text))

        return handler

    def cmd_pastamina(self, ctx: CommandContext) -> None:
        """pastamina <default|regain|infinite> - how fast players get their breath back"""
        wanted = ctx.args.strip().lower()
        if wanted not in STAMINA:
            ctx.reply(self.message("pa_stamina_usage"))
            return
        self.console.set_cvar("g_stamina", STAMINA[wanted])
        ctx.reply(self.message("pa_stamina", what=wanted))

    def cmd_pamoon(self, ctx: CommandContext) -> None:
        """pamoon <on|off> - low gravity"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pa_toggle_usage", command="pamoon"))
            return
        if wanted == "on":
            # Read before it is changed, so `off` puts back what this server actually had rather than
            # a number out of the plugin's own config, which is what the classic restored.
            current = self.console.get_cvar("g_gravity")
            if current and current != MOON_GRAVITY:
                self._gravity_was = current
            self.console.set_cvar("g_gravity", MOON_GRAVITY)
            ctx.reply(self.message("pa_moon", value=MOON_GRAVITY))
            return
        restored = self._gravity_was or NORMAL_GRAVITY
        self.console.set_cvar("g_gravity", restored)
        ctx.reply(self.message("pa_moon", value=restored))

    def cmd_pagear(self, ctx: CommandContext) -> None:
        """pagear [all|none|reset|+weapon|-weapon] - which weapons players may carry"""
        current = as_int(self.console.get_cvar("g_gear") or "0", 0)
        if self._gear_was is None:
            self._gear_was = current
        wanted = ctx.args.strip().lower()
        if not wanted:
            # The classic answered this with `console.write(...)`, which sends an **rcon command**: the
            # admin was told nothing at all and the server was sent a line of colour-coded chat.
            ctx.reply(self._describe_gear(current))
            return
        if wanted == "all":
            new = GEAR_ALL
        elif wanted == "none":
            new = GEAR_NONE
        elif wanted == "reset":
            new = self._gear_was
        elif wanted[:1] in ("+", "-"):
            bit = next(
                (value for weapon, value in GEAR_BITS.items() if weapon.startswith(wanted[1:5])),
                None,
            )
            if bit is None:
                ctx.reply(self.message("pa_gear_usage", weapons=", ".join(GEAR_BITS)))
                return
            # A set bit *forbids* the weapon, so `+` clears it. The engine's convention, not ours.
            new = current & ~bit if wanted[:1] == "+" else current | bit
        else:
            ctx.reply(self.message("pa_gear_usage", weapons=", ".join(GEAR_BITS)))
            return
        self.console.set_cvar("g_gear", str(new))
        ctx.reply(self._describe_gear(new, changed=True))

    def _describe_gear(self, gear: int, changed: bool = False) -> str:
        allowed = [weapon for weapon, bit in GEAR_BITS.items() if not gear & bit]
        if not allowed:
            return self.message("pa_gear_none")
        key = "pa_gear_changed" if changed else "pa_gear"
        return self.message(key, allowed=", ".join(allowed))

    def cmd_pasetnextmap(self, ctx: CommandContext) -> None:
        """pasetnextmap <map> - the map to load after this one"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pa_nextmap_usage"))
            return
        # Asked of the runtime rather than writing `g_nextmap` here: which cvar holds it is a fact
        # about the title, and `!nextmap` and `callvote`'s announcement read the same one.
        if not self.console.set_next_map(wanted):
            ctx.reply(self.message("pa_nextmap_unsupported"))
            return
        ctx.reply(self.message("pa_nextmap", map=self.console.map_display(wanted)))

    def _set_level(self, name: str, level: int) -> None:
        registered = self.console.command_registry.get(name)
        if registered is not None:
            registered.min_level = level

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.schedule(self._run_repeats, second="*", name="PoweradminurtPlugin.repeats")
        self.subscribe(EventType.CLIENT_DISCONNECT, self._on_disconnect)
        # What makes `!paforce ... lock` mean anything: `forceteam` moves a player once and nothing
        # stops them switching back. It is also how the balancer learns who joined a team last.
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_team_change)
        self.subscribe(EventType.GAME_ROUND_START, self._on_round_start)
        self.subscribe(EventType.GAME_ROUND_END, self._on_round_end)
        self.subscribe(EventType.GAME_EXIT, self._on_game_exit)
        # What the skill balancer measures. Counted here rather than read from `stats`: that plugin
        # keeps three of the nine figures a skill score needs, has no notion of "since you joined
        # this team", and is optional — a balancer that silently stops working when an unrelated
        # plugin is switched off is worse than three integers counted twice.
        self.subscribe(EventType.CLIENT_KILL, self._on_kill)
        self.subscribe(EventType.CLIENT_KILL_TEAM, self._on_team_kill)
        self.subscribe(EventType.CLIENT_ACTION, self._on_action)
        self.on_load_config()
        interval = as_int(self.settings.get("teambalance_interval"), 30)
        if interval > 0:
            self.schedule(
                self._teamcheck, minute=f"*/{min(interval, 59)}", name="PoweradminurtPlugin.teams"
            )
        self.subscribe(EventType.CLIENT_NAME_CHANGE, self.on_name_change)
        self.subscribe(EventType.GAME_MAP_CHANGE, self._on_map_change)
        name_interval = as_int(self.settings.get("namecheck_interval"), 0)
        if name_interval > 0:
            self.schedule(
                self.namecheck,
                minute=f"*/{min(name_interval, 59)}",
                name="PoweradminurtPlugin.names",
            )
        spec_interval = as_int(self.settings.get("speccheck_interval"), 0)
        if spec_interval > 0:
            self.schedule(
                self.speccheck,
                minute=f"*/{min(spec_interval, 59)}",
                name="PoweradminurtPlugin.spectators",
            )
        skill_interval = as_int(self.settings.get("skillbalance_interval"), 0)
        if skill_interval > 0:
            self.schedule(
                self.skillcheck,
                minute=f"*/{min(skill_interval, 59)}",
                name="PoweradminurtPlugin.skill",
            )
        self._apply_bot_settings()
        # A bot that has just started is about to adopt a server full of players, each of whom looks
        # like somebody who has this moment joined a team. Nothing automatic runs until that settles.
        self.hold_off()
        missing = [
            verb
            for verb in ("slap", "nuke", "kill", "mute")
            if not self.console.supports_verb(verb)
        ]
        if missing:
            log.info(
                "poweradminurt: this game has no %s verb, so those commands will say so when used",
                ", ".join(missing),
            )

    def on_disable(self) -> None:
        """A plugin switched off stops slapping. The classic's threads carried on regardless."""
        self.pending.clear()

    # -- the player commands -------------------------------------------------

    @command("paslap", level=40, alias="slap")
    def cmd_paslap(self, ctx: CommandContext) -> None:
        """paslap <player> [times] - throw a player about, up to 25 times"""
        self._punish(ctx, "slap", "pa_slapped")

    @command("panuke", level=40, alias="nuke")
    def cmd_panuke(self, ctx: CommandContext) -> None:
        """panuke <player> [times] - nuke a player, up to 25 times"""
        self._punish(ctx, "nuke", "pa_nuked")

    @command("pakill", level=40)
    def cmd_pakill(self, ctx: CommandContext) -> None:
        """pakill <player> - kill a player where they stand"""
        self._punish(ctx, "kill", "pa_killed", repeatable=False)

    def _punish(
        self, ctx: CommandContext, verb: str, said: str, *, repeatable: bool = True
    ) -> None:
        """The shape all three share: find the player, check the rank, then do it once or N times."""
        if not self.console.supports_verb(verb):
            ctx.reply(self.message("pa_unavailable", verb=verb))
            return
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("pa_usage_player", command=ctx.command.name))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        if self._protected(ctx.client, target):
            ctx.reply(self.message("pa_protected", name=target.name))
            return
        times = 1
        if repeatable and len(parts) > 1:
            times = as_int(parts[1], 0)
            if not 1 <= times <= MAX_REPEATS:
                ctx.reply(self.message("pa_repeat_range", limit=MAX_REPEATS))
                return
        self.console.apply_verb(verb, target)
        if times > 1:
            # The rest are deadlines. The classic slept a second between writes inside a raw thread,
            # one thread per command, which nothing could stop once started.
            self.pending.append(
                Repeat(
                    verb=verb,
                    client=target,
                    remaining=times - 1,
                    due=self.console.clock.now() + REPEAT_SECONDS,
                    admin=ctx.client,
                )
            )
        ctx.reply(self.message(said, name=target.name))

    @command("pamute", level=40, alias="mute")
    def cmd_pamute(self, ctx: CommandContext) -> None:
        """pamute <player> [minutes] - stop a player talking for a while"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("pa_usage_player", command=ctx.command.name))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        if self._protected(ctx.client, target):
            ctx.reply(self.message("pa_protected", name=target.name))
            return
        minutes = as_int(self.settings.get("default_mute_minutes"), 5)
        if len(parts) > 1:
            minutes = as_int(parts[1], minutes)
        minutes = max(1, minutes)
        if not self.console.mute(target, minutes):
            ctx.reply(self.message("pa_unavailable", verb="mute"))
            return
        ctx.reply(self.message("pa_muted", name=target.name, minutes=minutes))

    @command("paunmute", level=40)
    def cmd_paunmute(self, ctx: CommandContext) -> None:
        """paunmute <player> - let a player talk again"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("pa_usage_player", command=ctx.command.name))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        if not self.console.unmute(target):
            ctx.reply(self.message("pa_unavailable", verb="mute"))
            return
        ctx.reply(self.message("pa_unmuted", name=target.name))

    def _protected(self, admin: Client, target: Client) -> bool:
        """Whether `admin` may not do this to `target`.

        One rule for all four commands. The classic had `slap_safe_level` on `!paslap`, nothing at all
        on `!panuke`, and a third comparison on `!pamute` — so which of your fellow admins you could
        throw about depended on which verb you reached for.
        """
        if not self.settings.get("protect_peers"):
            return False
        return target is not admin and target.max_level() >= admin.max_level()

    def _on_disconnect(self, event: Event) -> None:
        client = event.client
        if client is None:
            return
        client.del_var(self, "locked_to")
        for key in ("name_changes", "name_seen", "name_kicked"):
            client.del_var(self, key)
        before = len(self.pending)
        self.pending = [r for r in self.pending if r.client is not client]
        if len(self.pending) != before:
            log.debug("poweradminurt: %s left, so the rest of their slapping stops", client.name)

    def _run_repeats(self) -> None:
        """One pass over the outstanding repeats. Nothing sleeps and nothing is a thread."""
        now = self.console.clock.now()
        for repeat in list(self.pending):
            if now < repeat.due:
                continue
            if self.console.clients.get_by_cid(repeat.client.cid or "") is not repeat.client:
                self.pending.remove(repeat)
                continue
            self.console.apply_verb(repeat.verb, repeat.client)
            repeat.remaining -= 1
            repeat.due = now + REPEAT_SECONDS
            if repeat.remaining <= 0:
                self.pending.remove(repeat)

    # -- moving players between teams ----------------------------------------

    @command("paforce", level=40, alias="force")
    def cmd_paforce(self, ctx: CommandContext) -> None:
        """paforce <player> <red|blue|spec|free> [lock] - move a player, and optionally hold them"""
        if not self.console.supports_verb("forceteam"):
            ctx.reply(self.message("pa_unavailable", verb="forceteam"))
            return
        parts = ctx.args.split()
        if len(parts) < 2:
            ctx.reply(self.message("pa_force_usage"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        wanted = parts[1].strip().lower()
        lock = len(parts) > 2 and parts[2].strip().lower() == "lock"
        if wanted == "free":
            if self.locked_to(target) is None:
                ctx.reply(self.message("pa_force_not_locked", name=target.name))
                return
            target.del_var(self, "locked_to")
            ctx.reply(self.message("pa_force_released", name=target.name))
            self.console.tell(target, self.message("pa_force_released", name=target.name))
            return
        team = TEAMS.get(wanted)
        if team is None:
            ctx.reply(self.message("pa_force_usage"))
            return
        if self._protected(ctx.client, target):
            ctx.reply(self.message("pa_protected", name=target.name))
            return
        if lock:
            target.set_var(self, "locked_to", team)
        else:
            target.del_var(self, "locked_to")
        self.console.apply_verb("forceteam", target, team=team)
        # An admin has just moved somebody on purpose: the balancer must not undo it on its next
        # pass. The classic's `ignoreSet(30)`, and the reason it existed.
        self.hold_off()
        readable = "spectator" if team == "s" else team
        ctx.reply(self.message("pa_forced", name=target.name, team=readable))
        self.console.tell(
            target,
            self.message("pa_forced_locked" if lock else "pa_forced_you", team=readable),
        )

    def locked_to(self, client: Client) -> str | None:
        """The team this player is held on, or None."""
        held = client.get_var(self, "locked_to")
        return held if isinstance(held, str) else None

    def on_team_change(self, event: Event) -> None:
        """Put a locked player back, remember when they moved, and even the teams if they broke them.

        The classic did the lock too, and it is the only reason the lock means anything: `forceteam`
        moves somebody once, and nothing stops them switching straight back. On this family, though,
        the event it hangs on had never been published at all — the team is a field of the infostring
        rather than a line of its own, so nothing noticed the field changing. See
        `Q3Parser._userinfo_events`.
        """
        client = event.client
        if client is None:
            return
        # When they joined this team, which is what the balancer sorts on. Stamped before anything
        # can return, so that a locked player and an admin have one too.
        now = self.console.clock.now()
        client.set_var(self, "team_time", now)
        # And their skill figures start again: what they did for the other side is not evidence
        # about this one.
        client.set_var(self, "skill", SkillRecord(joined=now))
        held = self.locked_to(client)
        if held is not None:
            current = (client.team or "").strip().lower()
            wanted = "spec" if held == "s" else held
            if current == wanted:
                return
            self.console.apply_verb("forceteam", client, team=held)
            self.console.tell(
                client, self.message("pa_force_denied", team="spectator" if held == "s" else held)
            )
            return
        self._balance_after_switch(client)

    # -- the team balancer ---------------------------------------------------

    def hold_off(self, seconds: float | None = None) -> None:
        """Stand the automatic checks down for a while — the classic's `ignoreSet`.

        Every deliberate move of players calls this. Without it the balancer's next pass undoes a
        `!pashuffleteams`, and the flurry of team changes a round start produces reads as everybody
        switching at once.
        """
        if seconds is None:
            seconds = float(as_int(self.settings.get("teambalance_quiet_seconds"), 60))
        self._quiet_until = self.console.clock.now() + max(0.0, seconds)

    def holding_off(self) -> bool:
        """Whether the automatic checks are currently standing down."""
        return self.console.clock.now() < self._quiet_until

    def _gametypes(self, key: str) -> set[str]:
        """One of the two gametype lists, as the names the engine's numbers map to."""
        raw = str(self.settings.get(key) or "")
        named = {word.strip().lower() for word in re.split(r"[\s,]+", raw) if word.strip()}
        return {GAMETYPE_ALIASES.get(name, name) for name in named}

    def gametype(self) -> str:
        """The gametype being played, by name. Empty when the bot has not seen an `InitGame` yet."""
        return GAMETYPE_NAMES.get(as_int(self.console.game.gametype, -1), "")

    def _round_based(self) -> bool:
        """Whether a move now would take somebody out of a round they are in the middle of."""
        return self.gametype() in self._gametypes("teambalance_round_based")

    def _sides(self) -> dict[str, list[Client]]:
        """Who is on each side, from the roster the bot already keeps.

        Not from `g_redteamlist` and `g_blueteamlist`, which is what the classic asked the server for
        on every pass of its loop: the reply cannot yet include the `forceteam` just sent, so it read
        the same imbalance back and moved the same player again.
        """
        sides: dict[str, list[Client]] = {side: [] for side in SIDES}
        for client in self.console.clients.connected():
            side = (client.team or "").strip().lower()
            if side in sides:
                sides[side].append(client)
        return sides

    def _may_move(self, client: Client) -> bool:
        """Whether the balancer is allowed to pick this player up."""
        if self.locked_to(client) is not None:
            return False
        return client.max_level() < as_int(self.settings.get("teambalance_max_level"), 60)

    def _joined_team_at(self, client: Client) -> float:
        """When this player last joined the team they are on.

        Falls back to when the bot first saw them, which is the honest answer for somebody already
        playing when it started — and is what makes "whoever joined most recently" mean something on
        the very first check.
        """
        stamped = client.get_var(self, "team_time")
        return float(stamped) if isinstance(stamped, int | float) else client.connected_at

    def _move(self, client: Client, side: str) -> None:
        """Send the player across, and believe it happened.

        Recording the new team here rather than waiting for the server to say so is what lets a whole
        balance be worked out in one pass. If the move did not take, the next infostring line for that
        slot says a different team and the bot corrects itself — publishing the team change it would
        have published anyway.
        """
        self.console.apply_verb("forceteam", client, team=side)
        client.team = side
        client.set_var(self, "team_time", self.console.clock.now())
        self.console.tell(client, self.message("pa_teams_moved_you", team=side))

    def balance(self) -> tuple[list[Client], bool]:
        """Even the teams up. Returns who moved, and whether they were uneven to begin with.

        The classic moved one player, asked the server to count the teams again, and went round up to
        twenty-five times. How many have to move is arithmetic: each one taken off the bigger side and
        put on the smaller changes the difference by two.
        """
        sides = self._sides()
        red, blue = sides["red"], sides["blue"]
        tolerance = max(1, as_int(self.settings.get("teambalance_difference"), 1))
        surplus = abs(len(red) - len(blue))
        if surplus <= tolerance:
            return [], False
        bigger, smaller = (red, "blue") if len(red) > len(blue) else (blue, "red")
        wanted = (surplus - tolerance + 1) // 2
        movable = sorted(
            (c for c in bigger if self._may_move(c)), key=self._joined_team_at, reverse=True
        )
        moving = movable[:wanted]
        if not moving:
            return [], True
        self._announce(self.message("pa_teams_balancing"))
        for client in moving:
            self._move(client, smaller)
        # Whoever just moved did not choose to, so nothing should read it as them unbalancing
        # anything, and the next pass must not pick them straight back up.
        self.hold_off()
        return moving, True

    def _announce(self, text: str) -> None:
        """Say it the way the operator asked — or not at all.

        The classic announced the moment it found the teams uneven and only then looked for somebody
        to move, so a server whose bigger team was all admins was told the teams were being balanced
        every interval, forever, while nothing happened.
        """
        how = as_word(self.settings.get("teambalance_announce"), "bigtext")
        if how == "bigtext":
            self.console.say_big(text)
        elif how == "say":
            self.console.say(text)

    def _teamcheck(self) -> None:
        """The scheduled pass — the classic's `teamcheck` cronjob."""
        if not self.is_enabled() or self.holding_off():
            return
        if self.gametype() not in self._gametypes("teambalance_gametypes"):
            return
        if self._round_based() and not self._round_ended:
            # Moving somebody now takes them out of a round they are playing.
            self._balance_pending = True
            return
        self.balance()

    def _balance_after_switch(self, client: Client) -> None:
        """A player switched teams. Put them back only if *they* are what made the teams uneven.

        The classic asked whether the teams were uneven, not whether this switch had made them so —
        with red on five and blue on two, somebody joining blue was "forced to the smaller team",
        which is the team they had just joined, and charged two fabricated suicide events as an
        anti-stats-harvesting penalty. There is no penalty here: the switch is reversed, and only when
        reversing it helps.
        """
        if not self.settings.get("teambalance_on_team_change") or self.holding_off():
            return
        if not self.is_enabled() or not self.console.supports_verb("forceteam"):
            return
        side = (client.team or "").strip().lower()
        if side not in SIDES or not self._may_move(client):
            return
        if self.gametype() not in self._gametypes("teambalance_gametypes"):
            return
        other = "blue" if side == "red" else "red"
        sides = self._sides()
        tolerance = max(1, as_int(self.settings.get("teambalance_difference"), 1))
        if len(sides[side]) - len(sides[other]) <= tolerance:
            return
        self.console.apply_verb("forceteam", client, team=other)
        client.team = other
        client.set_var(self, "team_time", self.console.clock.now())
        self.console.tell(client, self.message("pa_teams_put_back", team=other))

    @command("pateams", level=2, alias="teams")
    def cmd_pateams(self, ctx: CommandContext) -> None:
        """pateams - even the teams up now, moving whoever joined theirs most recently"""
        if not self.console.supports_verb("forceteam"):
            ctx.reply(self.message("pa_unavailable", verb="forceteam"))
            return
        if self._round_based() and not self._round_ended:
            self._balance_pending = True
            ctx.reply(self.message("pa_teams_pending"))
            return
        moved, uneven = self.balance()
        if not uneven:
            ctx.reply(self.message("pa_teams_already"))
        elif moved:
            ctx.reply(self.message("pa_teams_balanced"))
        else:
            # The classic said "Teams are now balanced" here, having moved nobody at all.
            ctx.reply(self.message("pa_teams_stuck"))

    def _on_map_change(self, _event: Event) -> None:
        """A new map: the bots may or may not be welcome on it."""
        self.botsupport()

    def _on_round_start(self, _event: Event) -> None:
        self._round_ended = False
        self._last_balance = self.console.clock.now()
        self._forget_contributions()
        self.hold_off()

    def _on_round_end(self, _event: Event) -> None:
        """A balance that fell due mid-round happens now, which is what waiting for it was for.

        A skill balance wins over a head-count one when both are owed: it evens the numbers too.
        """
        self._round_ended = True
        pending, self._skill_pending = self._skill_pending, None
        if pending is not None:
            self._balance_pending = False
            pending()
            return
        if self._balance_pending:
            self._balance_pending = False
            self.balance()

    def _on_game_exit(self, _event: Event) -> None:
        """The map is over: locks lapse unless the operator wanted them permanent."""
        # The bots go before the next map loads: whether they are welcome on it is not yet known.
        self._set_bot_count(0)
        self._balance_pending = False
        self._skill_pending = None
        self._reset_name_changes()
        self.hold_off()
        if self.settings.get("team_locks_permanent"):
            return
        for client in self.console.clients.connected():
            client.del_var(self, "locked_to")

    # -- bot support ---------------------------------------------------------

    def _bot_maps(self) -> set[str]:
        """The maps bots may run on, lower-cased. Empty means none, and that is deliberate."""
        raw = str(self.settings.get("botsupport_maps") or "")
        return {part.strip().lower() for part in re.split(r"[\s,]+", raw) if part.strip()}

    def _set_bot_count(self, count: int) -> None:
        """Write the count — but never write a zero over a server this plugin has not touched.

        A plugin that is not running bots should leave the engine's bot cvars alone: writing
        `bot_minplayers 0` at startup on a server whose operator manages bots by hand would turn
        them off, and on a title that has no such cvar it invents one. So the "off" writes only
        happen once the "on" writes have.
        """
        if count <= 0 and not self._bots_running:
            return
        self._bots_running = count > 0
        self.console.set_cvar(BOT_COUNT_CVAR, str(max(0, count)))

    def botsupport(self) -> None:
        """Keep the server populated with AI players, but only where that is known to be safe.

        The classic's own configuration file shouts "BOTSUPPORT IS VERY UNSTABLE! IT MAY CRASH YOUR
        SERVER PLENTY!" and answers it with a list of maps bots have been seen to survive. That list
        is the feature, so it is kept — and an empty one means no map, rather than every map.
        """
        if not self.is_enabled() or not self.settings.get("botsupport_enable"):
            self._set_bot_count(0)
            return
        current = (self.console.game.map_name or "").strip().lower()
        if current and current in self._bot_maps():
            wanted = as_int(self.settings.get("botsupport_min_players"), 4)
            log.info("poweradminurt: filling %s up to %d players with bots", current, wanted)
            self._set_bot_count(wanted)
            return
        # Off, rather than left alone. The classic's version did nothing here and relied on whoever
        # called it having turned the bots off first — which its config reload did not do, so a
        # reload on a map not in the list left the bots it had started running.
        self._set_bot_count(0)

    def _apply_bot_settings(self) -> None:
        """Tell the engine what the bots should be like, and whether it should have any at all."""
        enabled = bool(self.settings.get("botsupport_enable"))
        if not enabled:
            # Only if this plugin turned them on in the first place. The classic only ever wrote
            # `bot_enable 1`, so switching the feature off in the config and reloading left the
            # engine's bot subsystem running with only the count at zero — and it wrote the skill
            # level on every config load whether or not it was running bots at all.
            if self._bot_cvars_written:
                self.botsupport()
                self.console.set_cvar(BOT_ENABLE_CVAR, "0")
                self._bot_cvars_written = False
            return
        self._bot_cvars_written = True
        self.console.set_cvar(BOT_ENABLE_CVAR, "1")
        skill = min(5, max(1, as_int(self.settings.get("botsupport_skill"), 4)))
        self.console.set_cvar(BOT_SKILL_CVAR, str(skill))
        self.botsupport()

    # -- the name checker ----------------------------------------------------

    def _name_list(self, key: str) -> set[str]:
        """One of the two lists of names nobody may wear, normalised for comparison."""
        raw = str(self.settings.get(key) or "")
        return {normalise_name(part) for part in raw.split(",") if part.strip()}

    def _name_exempt(self, client: Client) -> bool:
        # A bot cannot read a warning, and several bots on one server routinely wear one name — the
        # classic warned every one of them, on every sweep, for duplicating each other.
        if client.is_bot:
            return True
        return client.max_level() >= as_int(self.settings.get("namecheck_max_level"), 20)

    def namecheck(self) -> None:
        """Warn players wearing a name nobody should be wearing.

        Names are compared **as players see them** — colour codes stripped, trimmed, lower-cased.
        The classic compared the raw strings, so `^1Bob` and `^2Bob`, which are the same name to
        everybody looking at the scoreboard, were not duplicates of each other; and its check for the
        default nickname called `stripColors` on its own constant, which has no colours in it,
        instead of on the player's name, so `^1New UrT Player` went past unnoticed. Both of those
        misses are the exact case each check exists for.
        """
        if not self.is_enabled() or self.holding_off():
            return
        forbidden = self._name_list("namecheck_forbidden_names")
        by_name: dict[str, list[Client]] = {}
        for client in self.console.clients.connected():
            if self._name_exempt(client):
                continue
            by_name.setdefault(normalise_name(client.name), []).append(client)
        for name, wearing in by_name.items():
            if not name:
                continue
            if self.settings.get("namecheck_duplicates") and len(wearing) > 1:
                # Both are warned: which of them took it from the other is not knowable from here.
                for client in wearing:
                    log.info("poweradminurt: %s shares a name with somebody", client.name)
                    self.console.warn(client, reason=self.message("pa_name_duplicate"))
            if name in forbidden:
                for client in wearing:
                    log.info("poweradminurt: %s is wearing a forbidden name", client.name)
                    self.console.warn(client, reason=self.message("pa_name_forbidden"))

    def _name_changes(self, client: Client) -> int:
        held = client.get_var(self, "name_changes")
        return held if isinstance(held, int) else 0

    def on_name_change(self, event: Event) -> None:
        """Count a rename, and remove somebody who will not settle on a name.

        A player who changes name every thirty seconds cannot be talked about, complained about or
        reported, which is the whole reason for the limit.
        """
        client = event.client
        if client is None or not self.is_enabled():
            return
        allowed = as_int(self.settings.get("namecheck_max_changes"), 7)
        if allowed <= 0 or self._name_exempt(client):
            return
        name = normalise_name(client.name)
        # Urban Terror appends `_<slot>` to a name already in use, so a player who drops and
        # reconnects comes back as `Bob_3` — which is the engine renaming them, not them renaming
        # themselves. The classic did this too, and it is worth keeping.
        suffix = f"_{client.cid}"
        if client.cid and name.endswith(suffix):
            name = name[: -len(suffix)]
        previous = client.get_var(self, "name_seen")
        client.set_var(self, "name_seen", name)
        if isinstance(previous, str) and name == previous:
            # The same name as last time once the engine's suffix is off: nothing was renamed.
            return
        count = self._name_changes(client) + 1
        client.set_var(self, "name_changes", count)
        if count > allowed:
            if client.get_var(self, "name_kicked"):
                # Already asked to leave. Whether they have actually gone is the server's business,
                # and kicking them once per rename in the meantime helps nobody.
                return
            client.set_var(self, "name_kicked", True)
            log.info("poweradminurt: kicking %s after %d name changes", client.name, count)
            self.console.kick(client, reason=self.message("pa_name_changes_kick"))
            return
        left = allowed - count
        if left < NAME_CHANGE_WARNING_AT:
            self.console.tell(client, self.message("pa_name_changes_left", count=left))

    def _reset_name_changes(self) -> None:
        """A new map is a fresh start, as it was in the classic."""
        for client in self.console.clients.connected():
            client.del_var(self, "name_changes")
            client.del_var(self, "name_kicked")

    # -- the spectator check -------------------------------------------------

    def _slots_are_managed(self) -> bool:
        """Whether the *server* is deciding who plays and who watches.

        `g_maxGameClients` caps how many of the connected clients may be on a team, so above it the
        server puts people in the spectators itself. Warning them for sitting there would be the bot
        punishing players for something the server did to them.

        Read now rather than at startup, which is where the classic read it — once, into an attribute,
        with no error handling: on a server that answered nothing for that cvar the read raised inside
        `onStartup` and the **whole plugin** failed to load, taking all forty-nine commands with it.
        """
        value = (self.console.get_cvar("g_maxGameClients") or "").strip()
        return value not in ("", "0")

    def _spec_min_players(self) -> int:
        """How many connected clients make this server busy enough to police the spectators.

        Zero means "work it out": the public slots, which is every slot minus the reserved ones. The
        classic worked that out once when the config loaded, so a server whose reserved slots changed
        went on using the old number until it was restarted.
        """
        configured = as_int(self.settings.get("speccheck_min_players"), 0)
        if configured > 0:
            return configured
        total = self.console.game.max_players or as_int(self.console.get_cvar("sv_maxclients"), 0)
        private = as_int(self.console.get_cvar("sv_privateClients"), 0)
        return max(1, total - private)

    def speccheck(self) -> None:
        """Warn whoever has been watching rather than playing while others want a slot."""
        if not self.is_enabled() or self.holding_off():
            return
        if self._slots_are_managed():
            return
        connected = list(self.console.clients.connected())
        # Bots are counted, because a bot fills a slot somebody wants, and never warned, because a
        # warning is a thing you say to a person. The classic warned them, and the escalation
        # eventually kicked one — whereupon `botsupport` added it straight back.
        if len(connected) < self._spec_min_players():
            return
        max_level = as_int(self.settings.get("speccheck_max_level"), 20)
        allowed = as_int(self.settings.get("speccheck_max_spec_minutes"), 5) * 60
        minutes = as_int(self.settings.get("speccheck_warn_minutes"), 5)
        now = self.console.clock.now()
        for client in connected:
            if client.is_bot or (client.team or "").strip().lower() != "spec":
                continue
            if client.max_level() >= max_level:
                continue
            if self.locked_to(client) is not None:
                # An admin put them there on purpose.
                continue
            if now - self._joined_team_at(client) <= allowed:
                continue
            log.info("poweradminurt: warning %s for watching on a full server", client.name)
            self.console.warn(client, reason=self.message("pa_spec_reason"), minutes=minutes)

    # -- the skill balancer --------------------------------------------------

    def record(self, client: Client) -> SkillRecord:
        """This player's figures since they joined the team they are on.

        The classic kept nine separate client variables plus a `prev_` copy of each, and subtracted
        one from the other on every read — a baseline it had to remember to re-take in four places.
        The record is simply reset when they change teams, which is the same arithmetic with nothing
        to keep in step.
        """
        held = client.get_var(self, "skill")
        if not isinstance(held, SkillRecord):
            held = SkillRecord(joined=self.console.clock.now())
            client.set_var(self, "skill", held)
        return held

    def _on_kill(self, event: Event) -> None:
        killer, victim = event.client, event.target
        if killer is None or victim is None:
            return
        now = self.console.clock.now()
        mine, theirs = self.record(killer), self.record(victim)
        mine.kills += 1
        theirs.deaths += 1
        mine.history.append((now, 1))
        theirs.history.append((now, -1))
        if is_headshot(event.data):
            # Counted here, not in the headshot counter. In the classic the head-hit figures the
            # skill score reads were written *only* by `headshotcounter`, which is a separate feature
            # and off by default — so on any server that had not turned it on, `hsratio` was zero for
            # everybody, and a key whose values are all equal is dropped from the score entirely.
            # One of the three weighted components of "skill" therefore never contributed.
            mine.head_hits += 1

    def _on_team_kill(self, event: Event) -> None:
        if event.client is not None:
            self.record(event.client).team_kills += 1

    def _on_action(self, event: Event) -> None:
        """Flags and the bomb — what a player did for the mission rather than for their score."""
        client, action = event.client, str(event.data or "")
        if client is None:
            return
        record = self.record(client)
        if action in ("flag_taken", "team_CTF_redflag", "team_CTF_blueflag"):
            record.flag_taken = True
        elif action == "flag_captured":
            record.flag_captured += 1
        elif action == "flag_returned":
            record.flag_returned += 1
        elif action == "bomb_planted":
            record.bomb_planted += 1
        elif action == "bomb_defused":
            record.bomb_defused += 1

    def _forget_contributions(self) -> None:
        """Start the measurement again — at a round start, and after any shuffle.

        Skill is measured over the round being played. Carrying a score across a shuffle would have
        the balancer judging players by how they did on the team it has just taken them off.
        """
        self._last_advice = None
        now = self.console.clock.now()
        for client in self.console.clients.connected():
            client.set_var(self, "skill", SkillRecord(joined=now))

    def _figures(self, client: Client) -> dict[str, float]:
        """The five measures the score is built from, for one player."""
        record = self.record(client)
        age = max(0.0, self.console.clock.now() - record.joined) / 60.0
        kills, deaths = max(0, record.kills), max(0, record.deaths)
        team_kills = max(0, record.team_kills)
        return {
            "age": age,
            # A head hit can outnumber kills, so this is capped rather than being a true ratio.
            "hsratio": min(1.0, record.head_hits / (1.0 + kills)),
            "killratio": kills / (1.0 + deaths + team_kills),
            "teamcontrib": (kills - deaths - team_kills) / (age + 1.0),
            "flagperf": 10.0 * int(record.flag_taken)
            + 20.0 * record.flag_captured
            + record.flag_returned,
            "bombperf": float(record.bomb_planted + record.bomb_defused),
        }

    def scores(self, clients: Sequence[Client]) -> dict[str, float]:
        """A relative skill score in 0..1 for each player, keyed by slot.

        Keyed by **cid**, not by database id as the classic was: a player the bot has not
        authenticated has no id, and on a Quake3 server without `cl_guid` that is everybody — so
        every unauthenticated player shared the key `None` and overwrote each other's figures, and
        the shuffle was built from one player's score wearing everybody's name.

        Each measure is scaled against the range across the players present, weighted, and summed.
        A measure that is the same for everybody says nothing about who is better and is left out.
        The three combat measures are damped for somebody who has only just arrived, since two kills
        in ten seconds is not evidence of anything.
        """
        figures = {c.cid: self._figures(c) for c in clients if c.cid}
        if not figures:
            return {}
        lows = {key: min(f[key] for f in figures.values()) for key in WEIGHTS}
        highs = {key: max(f[key] for f in figures.values()) for key in WEIGHTS}
        # Only the measures that actually vary. The classic divided by the total weight of *every*
        # measure including the ones it had skipped and the two it read from xlrstats — so on a
        # server without that plugin every score came out roughly half of what the weights say, and
        # since the autobalance threshold on CTF and bomb mode is compared against this figure, that
        # setting meant about twice what an operator reading it would think.
        live = [key for key in WEIGHTS if highs[key] - lows[key] >= EPSILON]
        total = sum(WEIGHTS[key] for key in live)
        if total <= 0.0:
            return {cid: 0.0 for cid in figures}
        out: dict[str, float] = {}
        for cid, figure in figures.items():
            damping = min(1.0, figure["age"] / DAMPING_MINUTES)
            score = 0.0
            for key in live:
                scaled = WEIGHTS[key] * (figure[key] - lows[key]) / (highs[key] - lows[key])
                score += damping * scaled if key in DAMPED else scaled
            out[cid] = score / total
        return out

    def _team_score(self, team: Sequence[Client], scores: dict[str, float]) -> float:
        return sum(scores.get(c.cid or "", 0.0) for c in team)

    def _score_diff(
        self, blue: Sequence[Client], red: Sequence[Client], scores: dict[str, float]
    ) -> float:
        return self._team_score(blue, scores) - self._team_score(red, scores)

    def _recent_ratios(self, blue: list[Client], red: list[Client]) -> tuple[float, float]:
        """Each side's best players' kill rate over the last few minutes.

        The readme that came with this feature is the argument for it: players complain when they are
        killed quickly and repeatedly, and get bored when they are doing the killing, so what a game
        *feels* like is the top of each side rather than the average of it. The window shortens as
        the server gets busier, because a busy server produces the same evidence in less time.
        """
        if not blue or not red:
            return 0.0, 0.0
        now = self.console.clock.now()
        per_minute = sum(
            1
            for client in self.console.clients.connected()
            for when, _what in self.record(client).history
            if when > now - 60.0
        )
        window = max(WINDOW_MIN_MINUTES, WINDOW_MAX_MINUTES - 0.1 * per_minute)
        start = now - window * 60.0

        def rate(client: Client) -> float:
            kills = deaths = 0
            for when, what in self.record(client).history:
                if when > start:
                    if what > 0:
                        kills += 1
                    else:
                        deaths += 1
            return kills / (1.0 + deaths)

        ranked_blue = sorted(blue, key=rate, reverse=True)
        ranked_red = sorted(red, key=rate, reverse=True)
        n = min(len(ranked_blue), len(ranked_red))
        if n > 3:
            n = 3 + (n - 3) // 2
        best_blue = sum(rate(c) for c in ranked_blue[:n]) / n / window
        best_red = sum(rate(c) for c in ranked_red[:n]) / n / window
        return best_blue, best_red

    def advice_figures(self, min_players: int = 0) -> tuple[float | None, float | None]:
        """How lopsided the game is (the felt figure), and how lopsided the skill is (the measured
        one). Both None when there are too few players for either to mean anything."""
        clients = list(self.console.clients.connected())
        blue = [c for c in clients if (c.team or "") == "blue"]
        red = [c for c in clients if (c.team or "") == "red"]
        if min_players and len(blue) + len(red) < min_players:
            return None, None
        scores = self.scores(clients)
        diff = self._score_diff(blue, red, scores)
        if self.gametype() == "tdm":
            best_blue, best_red = self._recent_ratios(blue, red)
            return best_blue - best_red, diff
        # Kill ratios do not describe a game whose point is the flag, so there the felt figure is
        # the measured one, damped by how recently the teams were last changed.
        since = self.console.clock.now() - self._last_balance
        minutes = max(1, as_int(self.settings.get("skillbalance_min_interval"), 2))
        damping = min(1.0, since / (1.0 + 60.0 * minutes))
        return 1.21 * diff * damping, diff

    # -- shuffling -----------------------------------------------------------

    def _count_snipers(self, team: Sequence[Client]) -> int:
        """How many of this side carry a one-shot rifle and can use it.

        A sniper nest on one side decides the game on its own, so a shuffle that cannot improve the
        score any further distributes these instead. Somebody carrying an SR8 with a poor kill ratio
        is not a sniper, which is the classic's own rule and a good one.
        """
        floor = as_float(self.settings.get("sniper_kill_ratio"), SNIPER_KILL_RATIO)
        count = 0
        for client in team:
            record = self.record(client)
            if max(0, record.kills) / (1.0 + max(0, record.deaths)) < floor:
                continue
            if any(letter in (client.gear or "") for letter in SNIPER_GEAR):
                count += 1
        return count

    def _random_teams(self, clients: Sequence[Client]) -> tuple[list[Client], list[Client]]:
        """Deal the players into two sides at random, leaving locked players where they are."""
        blue: list[Client] = []
        red: list[Client] = []
        free: list[Client] = []
        for client in clients:
            side = (client.team or "").strip().lower()
            if side not in SIDES:
                continue
            if self.locked_to(client) is not None:
                (blue if side == "blue" else red).append(client)
            else:
                free.append(client)
        self.random.shuffle(free)
        n = (len(free) + len(blue) + len(red)) // 2 - len(blue)
        n = max(0, min(len(free), n))
        blue.extend(free[:n])
        red.extend(free[n:])
        return blue, red

    @staticmethod
    def _count_moves(old: Sequence[Client], new: Sequence[Client]) -> int:
        """How many of `old` are not in `new`. By identity: the classic compared *names*, so two
        players called `Player` counted as one and a name change mid-shuffle counted as a move."""
        keep = {id(c) for c in new}
        return sum(1 for c in old if id(c) not in keep)

    def search_teams(
        self, tries: int, slack: float, max_move_fraction: float | None = None
    ) -> ShuffleResult:
        """Deal the players out `tries` times and keep the most even arrangement found."""
        clients = list(self.console.clients.connected())
        scores = self.scores(clients)
        old_blue = [c for c in clients if (c.team or "") == "blue"]
        old_red = [c for c in clients if (c.team or "") == "red"]
        playing = len(old_blue) + len(old_red)
        old_diff = self._score_diff(old_blue, old_red, scores)
        result = ShuffleResult(old_diff=old_diff, scores=scores)
        # Seeded with the arrangement being played, so that an arrangement has to be *better* than
        # what is there to be chosen. The classic seeded with nothing, so the first deal it tried
        # always won and `!paskuffle` moved half the server about on already-even teams while
        # reporting the difference as unchanged.
        result.diff = old_diff
        result.sniper_diff = old_diff if abs(old_diff) <= slack else None
        best_snipers = (
            abs(self._count_snipers(old_blue) - self._count_snipers(old_red))
            if abs(old_diff) <= slack
            else None
        )

        if max_move_fraction is None and abs(len(old_blue) - len(old_red)) > 1:
            # Uneven by head count as well: any arrangement at all beats this one.
            blue, red = self._random_teams(clients)
            result.blue, result.red = blue, red
            result.diff = self._score_diff(blue, red, scores)
            result.sniper_diff = None
            best_snipers = None
        for _ in range(tries):
            blue, red = self._random_teams(clients)
            if max_move_fraction is not None:
                moves = self._count_moves(old_blue, blue) + self._count_moves(old_red, red)
                if moves > max(2, round(max_move_fraction * playing)):
                    continue
            diff = self._score_diff(blue, red, scores)
            if abs(diff) <= slack:
                # Even enough. Judge these by how the snipers fall instead.
                snipers = abs(self._count_snipers(blue) - self._count_snipers(red))
                better = best_snipers is None or snipers < best_snipers
                same_but_evener = (
                    best_snipers is not None
                    and snipers == best_snipers
                    and result.sniper_diff is not None
                    and abs(diff) < abs(result.sniper_diff) - EPSILON
                )
                if better or same_but_evener:
                    best_snipers = snipers
                    result.sniper_blue, result.sniper_red = blue, red
                    result.sniper_diff = diff
            elif result.diff is None or abs(diff) < abs(result.diff) - EPSILON:
                result.blue, result.red = blue, red
                result.diff = diff
        return result

    def shuffle_into(
        self, blue: Sequence[Client], red: Sequence[Client], scores: dict[str, float] | None = None
    ) -> int:
        """Move everybody who is not already where they should be. Returns how many moved.

        The order matters and is the classic's: this engine refuses a `forceteam` that would overfill
        a side, so the moves start from whichever side has more players. When the two are equal there
        is no such move, and one player is parked in the spectators to make room and brought back at
        the end.
        """
        going_blue = [c for c in blue if (c.team or "") != "blue"]
        going_red = [c for c in red if (c.team or "") != "red"]
        if not going_blue and not going_red:
            return 0
        sides = self._sides()
        count = {"blue": len(sides["blue"]), "red": len(sides["red"])}
        self.hold_off()
        moves = len(going_blue) + len(going_red)
        parked: Client | None = None
        if going_blue and count["blue"] == count["red"]:
            self.random.shuffle(going_blue)
            parked = going_blue.pop()
            self.console.apply_verb("forceteam", parked, team="s")
            count["red"] -= 1
            moves -= 1
        best = (
            max((scores or {}).get(c.cid or "", 0.0) for c in [*going_blue, *going_red])
            if (scores and (going_blue or going_red))
            else None
        )
        told: list[tuple[Client, str]] = []
        for _ in range(moves):
            if going_blue and (count["blue"] < count["red"] or not going_red):
                client, side, other = going_blue.pop(), "blue", "red"
            elif going_red:
                client, side, other = going_red.pop(), "red", "blue"
            else:
                break
            self.console.apply_verb("forceteam", client, team=side)
            client.team = side
            client.set_var(self, "team_time", self.console.clock.now())
            count[side] += 1
            count[other] -= 1
            if scores is not None:
                key = (
                    "pa_skill_moved_best"
                    if best is not None and scores.get(client.cid or "", 0.0) >= best
                    else "pa_skill_moved"
                )
                told.append((client, self.message(key, team=side, other=other)))
        if parked is not None:
            self.console.apply_verb("forceteam", parked, team="blue")
            parked.team = "blue"
            parked.set_var(self, "team_time", self.console.clock.now())
        # Told afterwards, as the classic did: a message sent while the player is being moved between
        # teams is one the client can drop on the floor.
        for client, text in told:
            self.console.tell(client, text)
        return moves

    # -- advice --------------------------------------------------------------

    def advise(self, avgdiff: float, mode: str) -> None:
        """Say which side is winning, and how badly.

        `mode` is `quiet` (name the stronger side and nothing else), `advise` (add what to do about
        it) or `unfair` (add it only when it is worth doing something about).
        """
        absdiff = ADVICE_SCALE * abs(avgdiff)
        unfair = absdiff > UNFAIR
        word = next((w for limit, w in ADVICE_WORDS if absdiff < limit), ADVICE_WORDS[-1][1])
        if absdiff < ADVICE_WORDS[0][0]:
            self._last_advice = None
            self.console.say(self.message("pa_skill_fair"))
            return
        team = "red" if avgdiff < 0 else "blue"
        previous = self._last_advice
        if previous is not None and previous[0] == team:
            _team, old_word, old_absdiff = previous
            if word == old_word:
                text = self.message("pa_skill_remains", team=team, word=word)
            elif absdiff > old_absdiff:
                text = self.message("pa_skill_stronger", team=team, word=word)
            else:
                text = self.message("pa_skill_weaker", team=team, word=word)
                if absdiff < 4:
                    # Coming back together on its own; nothing to advise.
                    unfair = False
        else:
            text = self.message("pa_skill_now", team=team, word=word)
        if unfair and mode in ("advise", "unfair"):
            text += self.message("pa_skill_use_bal")
        elif not unfair and mode == "advise":
            text += self.message("pa_skill_no_action")
        self._last_advice = (team, word, absdiff)
        self.console.say(text)

    # -- the scheduled check -------------------------------------------------

    def skillcheck(self) -> None:
        """The skill balancer's own pass, separate from the head-count one."""
        if not self.is_enabled() or self.holding_off():
            return
        mode = self.skill_mode()
        if mode == "off":
            return
        if self.gametype() not in self._gametypes("teambalance_gametypes"):
            return
        avgdiff, diff = self.advice_figures(
            min_players=as_int(self.settings.get("skillbalance_min_players"), 3)
        )
        if avgdiff is None or diff is None:
            return
        threshold = as_float(self.settings.get("skillbalance_difference"), 0.5)
        unbalanced = abs(avgdiff) >= threshold
        if (unbalanced or mode == "advise") and abs(avgdiff) > 0.2:
            self.console.say(
                self.message("pa_skill_figures", felt=f"{avgdiff:.2f}", skill=f"{diff:.2f}")
            )
            self.advise(avgdiff, "unfair" if mode == "advise" else "quiet")
        if not unbalanced or mode == "advise":
            return
        action = self.balance_by_skill if mode == "balance" else self.skuffle
        if self._round_based() and not self._round_ended:
            self._skill_pending = action
            return
        action()

    def skill_mode(self) -> str:
        """What the balancer does when it finds the sides uneven in skill."""
        raw = as_word(self.settings.get("skillbalance_mode"), "off")
        mode = SKILL_MODES.get(raw)
        if mode is None:
            log.warning(
                "poweradminurt: skillbalance_mode %r is not one of %s; leaving it off",
                raw,
                SKILL_MODE_LIST,
            )
            return "off"
        return mode

    def _too_soon(self, ctx: CommandContext | None) -> bool:
        """Whether this player has to wait before asking for another shuffle.

        The classic's rule was an `and` of three conditions including "a quiet window is open", so
        the wait it advertised almost never applied. Here it is what it says: below moderator, not
        more often than the configured interval.
        """
        if ctx is None or ctx.client.max_level() >= 20:
            return False
        minutes = max(0, as_int(self.settings.get("skillbalance_min_interval"), 2))
        if self.console.clock.now() - self._last_balance >= minutes * 60:
            return False
        ctx.reply(self.message("pa_skill_too_soon"))
        return True

    def _defer(self, ctx: CommandContext | None, action: Callable[[], None]) -> bool:
        """Hold a shuffle over until the round ends, if we are in the middle of one."""
        if not self._round_based() or self._round_ended:
            return False
        self._skill_pending = action
        if ctx is not None:
            ctx.reply(self.message("pa_teams_pending"))
        return True

    def skuffle(self, ctx: CommandContext | None = None) -> None:
        """Shuffle everybody into the most evenly-matched pair of sides found."""
        result = self.search_teams(SHUFFLE_TRIES, SHUFFLE_SLACK)
        blue, red = result.best()
        if ctx is not None and blue is not None and red is not None:
            # Whoever asked stays where they are: being thrown across the map for asking is jarring,
            # and the arrangement is just as even the other way round.
            side = (ctx.client.team or "").strip().lower()
            wanted = blue if side == "blue" else red
            if side in SIDES and ctx.client not in wanted:
                blue, red = red, blue
        moves = 0
        if blue is not None and red is not None:
            self._announce(self.message("pa_skill_shuffling"))
            moves = self.shuffle_into(blue, red, result.scores)
        if moves:
            self.console.say(
                self.message(
                    "pa_skill_was_now",
                    was=f"{result.old_diff:.2f}",
                    now=f"{(result.chosen_diff() or 0.0):.2f}",
                )
            )
        else:
            self.console.say(self.message("pa_skill_no_improvement"))
        self._finish_balance()

    def balance_by_skill(self, ctx: CommandContext | None = None) -> None:
        """Even the sides by skill while moving as few players as possible."""
        fraction = as_float(self.settings.get("skuffle_max_move_fraction"), 0.3)
        result = self.search_teams(SHUFFLE_TRIES, SHUFFLE_SLACK, max_move_fraction=fraction)
        blue, red = result.best()
        if blue is None or red is None:
            # A few moves could not improve on what is there; a full shuffle is the honest answer.
            self.skuffle(ctx)
            return
        self._announce(self.message("pa_skill_balancing"))
        self.shuffle_into(blue, red, result.scores)
        self.console.say(
            self.message(
                "pa_skill_was_now",
                was=f"{result.old_diff:.2f}",
                now=f"{(result.chosen_diff() or 0.0):.2f}",
            )
        )
        self._finish_balance()

    def _finish_balance(self) -> None:
        self._forget_contributions()
        self._last_balance = self.console.clock.now()

    @command("paskuffle", level=20, alias="sk")
    def cmd_paskuffle(self, ctx: CommandContext) -> None:
        """paskuffle - shuffle everybody into teams matched by skill"""
        if not self.console.supports_verb("forceteam"):
            ctx.reply(self.message("pa_unavailable", verb="forceteam"))
            return
        if self._too_soon(ctx) or self._defer(ctx, self.skuffle):
            return
        self.skuffle(ctx)

    @command("pabalance", level=2, alias="bal")
    def cmd_pabalance(self, ctx: CommandContext) -> None:
        """pabalance - even the teams by skill, moving as few players as possible"""
        if not self.console.supports_verb("forceteam"):
            ctx.reply(self.message("pa_unavailable", verb="forceteam"))
            return
        if self._too_soon(ctx) or self._defer(ctx, self.balance_by_skill):
            return
        self.balance_by_skill(ctx)

    @command("paunskuffle", level=60, alias="unsk")
    def cmd_paunskuffle(self, ctx: CommandContext) -> None:
        """paunskuffle - put the best players on one side, to try the balancer out"""
        if not self.console.supports_verb("forceteam"):
            ctx.reply(self.message("pa_unavailable", verb="forceteam"))
            return
        clients = list(self.console.clients.connected())
        scores = self.scores(clients)
        playing = [c for c in clients if (c.team or "") in SIDES]
        playing.sort(key=lambda c: scores.get(c.cid or "", 0.0))
        half = len(playing) // 2
        self._announce(self.message("pa_skill_unshuffling"))
        self.shuffle_into(playing[:half], playing[half:])
        self._forget_contributions()

    @command("paadvise", level=2, alias="advise")
    def cmd_paadvise(self, ctx: CommandContext) -> None:
        """paadvise - say which side is stronger, and whether to do anything about it"""
        avgdiff, diff = self.advice_figures(
            min_players=as_int(self.settings.get("skillbalance_min_players"), 3)
        )
        if avgdiff is None or diff is None:
            ctx.reply(self.message("pa_skill_too_few"))
            return
        self.console.say(
            self.message("pa_skill_figures", felt=f"{avgdiff:.2f}", skill=f"{diff:.2f}")
        )
        self.advise(avgdiff, "advise")

    @command("paautoskuffle", level=60, alias="ask")
    def cmd_paautoskuffle(self, ctx: CommandContext) -> None:
        """paautoskuffle [off|advise|balance|shuffle] - what to do about uneven skill"""
        wanted = ctx.args.strip().lower()
        if not wanted:
            ctx.reply(
                self.message("pa_skill_mode", mode=self.skill_mode(), options=SKILL_MODE_LIST)
            )
            return
        mode = SKILL_MODES.get(wanted)
        if mode is None:
            ctx.reply(self.message("pa_skill_mode_usage", options=SKILL_MODE_LIST))
            return
        self.settings["skillbalance_mode"] = mode
        ctx.reply(self.message("pa_skill_mode_set", mode=mode))
        self.skillcheck()

    @command("paswap", level=40, alias="swap")
    def cmd_paswap(self, ctx: CommandContext) -> None:
        """paswap <player> [player] - swap two players between their teams"""
        if not self.console.supports_verb("swap"):
            ctx.reply(self.message("pa_unavailable", verb="swap"))
            return
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("pa_swap_usage"))
            return
        first = self.resolve_client(ctx, parts[0])
        if first is None:
            return
        second = ctx.client
        if len(parts) > 1:
            found = self.resolve_client(ctx, parts[1])
            if found is None:
                return
            second = found
        for player in (first, second):
            if (player.team or "").strip().lower() in ("spec", "spectator", "s"):
                ctx.reply(self.message("pa_swap_spectator", name=player.name))
                return
        if first is second:
            ctx.reply(self.message("pa_swap_same_team", first=first.name, second=second.name))
            return
        if (first.team or "") == (second.team or ""):
            ctx.reply(self.message("pa_swap_same_team", first=first.name, second=second.name))
            return
        self.console.apply_verb("swap", first, other=second.cid or "")
        self.hold_off()
        ctx.reply(self.message("pa_swapped", first=first.name, second=second.name))

    @command("paswapteams", level=60, alias="swapteams")
    def cmd_paswapteams(self, ctx: CommandContext) -> None:
        """paswapteams - swap the two teams around"""
        if not self.console.apply_server_verb("swapteams"):
            ctx.reply(self.message("pa_unavailable", verb="swapteams"))
            return
        self.hold_off()
        ctx.reply(self.message("pa_teams_swapped"))

    @command("pashuffleteams", level=60, alias="shuffleteams")
    def cmd_pashuffleteams(self, ctx: CommandContext) -> None:
        """pashuffleteams - shuffle everybody into new teams"""
        if not self.console.apply_server_verb("shuffleteams"):
            ctx.reply(self.message("pa_unavailable", verb="shuffleteams"))
            return
        self.hold_off()
        ctx.reply(self.message("pa_teams_shuffled"))

    # -- the server commands -------------------------------------------------

    @command("pamaprestart", level=60, alias="maprestart")
    def cmd_pamaprestart(self, ctx: CommandContext) -> None:
        """pamaprestart - restart the map now"""
        self._server_verb(ctx, "map_restart", "pa_map_restarted")

    @command("pamapreload", level=60, alias="mapreload")
    def cmd_pamapreload(self, ctx: CommandContext) -> None:
        """pamapreload - reload the map, which is what applies a changed password"""
        self._server_verb(ctx, "reload", "pa_map_reloaded")

    @command("pacyclemap", level=60, alias="cyclemap")
    def cmd_pacyclemap(self, ctx: CommandContext) -> None:
        """pacyclemap - move on to the next map"""
        self._server_verb(ctx, "cyclemap", "pa_map_cycled")

    def _server_verb(self, ctx: CommandContext, verb: str, said: str) -> None:
        if not self.console.apply_server_verb(verb):
            ctx.reply(self.message("pa_unavailable", verb=verb))
            return
        log.info("poweradminurt: %s ran %s", ctx.client.name, verb)
        ctx.reply(self.message(said))

    @command("paexec", level=80)
    def cmd_paexec(self, ctx: CommandContext) -> None:
        """paexec <config file> - run a config file on the server"""
        name = ctx.args.strip()
        if not name:
            ctx.reply(self.message("pa_exec_usage"))
            return
        if not CONFIG_FILE_RE.match(name):
            # Checked, not cleaned: `exec` takes a filename, and a "filename" with a semicolon in it
            # is somebody running a second command, not a name that wants tidying. Level 80 as well,
            # above the other server commands, because what a config file contains is anything.
            ctx.reply(self.message("pa_exec_bad_name", name=name))
            return
        if not self.console.apply_server_verb("exec", file=name):
            ctx.reply(self.message("pa_unavailable", verb="exec"))
            return
        log.info("poweradminurt: %s ran the config file %r", ctx.client.name, name)
        ctx.reply(self.message("pa_exec", name=name))

    @command("papublic", level=60, alias="public")
    def cmd_papublic(self, ctx: CommandContext) -> None:
        """papublic <on|off> - open the server to everybody, or put a password on it"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pa_public_usage"))
            return
        if wanted == "on":
            self.console.set_cvar("g_password", "")
            self.console.say(self.message("pa_public_on"))
            return
        word = str(self.settings.get("private_password") or "").strip()
        if not word:
            # The classic printed this refusal *after* building the password, so the branch could
            # never run: with no word configured it set a password of two random digits and told the
            # admin that was the password.
            ctx.reply(self.message("pa_public_no_password"))
            return
        password = word + self._digits()
        self.console.set_cvar("g_password", password)
        self.console.say(self.message("pa_public_off"))
        # Privately, and *not* into the log: the classic wrote `private password set to: %s` at debug,
        # so the server's password ended up in a file somebody else can read.
        self.console.tell(ctx.client, self.message("pa_public_password", password=password))
        log.info("poweradminurt: %s put a password on the server", ctx.client.name)

    def _digits(self) -> str:
        """The digits appended to the private password, so it changes each time.

        `secrets` rather than `random`: it is a password. The classic used `random.randint`, which is
        seeded predictably and is documented as unsuitable for exactly this.
        """
        count = max(0, as_int(self.settings.get("password_digits"), 2))
        return "".join(secrets.choice("123456789") for _ in range(count))

    # -- the server commands -------------------------------------------------

    @command("pabigtext", level=60)
    def cmd_pabigtext(self, ctx: CommandContext) -> None:
        """pabigtext <text> - announce something in the biggest text the game has"""
        text = ctx.args.strip()
        if not text:
            ctx.reply(self.message("pa_usage_bigtext"))
            return
        self.console.say_big(text)

    @command("paset", level=60)
    def cmd_paset(self, ctx: CommandContext) -> None:
        """paset <cvar> <value> - change a server setting"""
        parts = ctx.args.split(None, 1)
        if len(parts) < 2:
            ctx.reply(self.message("pa_usage_set"))
            return
        name, value = parts[0], parts[1].strip()
        self.console.set_cvar(name, value)
        log.info("poweradminurt: %s set %s to %r", ctx.client.name, name, value)
        ctx.reply(self.message("pa_cvar_changed", name=name, value=value))

    @command("paget", level=60)
    def cmd_paget(self, ctx: CommandContext) -> None:
        """paget <cvar> - read a server setting"""
        name = ctx.args.strip()
        if not name:
            ctx.reply(self.message("pa_usage_get"))
            return
        value = self.console.get_cvar(name)
        if value is None or value == "":
            ctx.reply(self.message("pa_cvar_unset", name=name))
            return
        ctx.reply(self.message("pa_cvar", name=name, value=value))

    @command("pavote", level=60)
    def cmd_pavote(self, ctx: CommandContext) -> None:
        """pavote <on|off> - let players call votes, or stop them"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pa_vote_usage"))
            return
        if wanted == "off":
            # Read now rather than at startup: the classic kept the bot-start value in an attribute,
            # so `!pavote on` after a restart put back whatever the plugin happened to remember.
            current = self.console.get_cvar(VOTE_CVAR)
            if current and current != "0":
                self._vote_was = current
            self.console.set_cvar(VOTE_CVAR, "0")
            ctx.reply(self.message("pa_vote_off"))
            return
        self.console.set_cvar(VOTE_CVAR, self._vote_was or VOTE_DEFAULT)
        ctx.reply(self.message("pa_vote_on"))


__all__ = [
    "CONFIG_FILE_RE",
    "DEFAULTS",
    "ADVICE_SCALE",
    "ADVICE_WORDS",
    "DAMPED",
    "DAMPING_MINUTES",
    "BOT_COUNT_CVAR",
    "BOT_ENABLE_CVAR",
    "BOT_SKILL_CVAR",
    "GAMETYPES",
    "GAMETYPE_ALIASES",
    "GAMETYPE_NAMES",
    "SHUFFLE_SLACK",
    "SHUFFLE_TRIES",
    "SIDES",
    "NAME_CHANGE_WARNING_AT",
    "SKILL_MODES",
    "SNIPER_GEAR",
    "SNIPER_KILL_RATIO",
    "TEAMS",
    "UNFAIR",
    "WEIGHTS",
    "ShuffleResult",
    "SkillRecord",
    "GEAR_ALL",
    "GEAR_BITS",
    "GEAR_NONE",
    "MAX_REPEATS",
    "MESSAGES",
    "MOON_GRAVITY",
    "NORMAL_GRAVITY",
    "NUMBERS",
    "REPEAT_SECONDS",
    "STAMINA",
    "TOGGLES",
    "VOTE_CVAR",
    "VOTE_DEFAULT",
    "PoweradminurtPlugin",
    "Repeat",
]
