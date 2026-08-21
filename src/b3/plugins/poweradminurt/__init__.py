"""Urban Terror's own admin commands — the ones that act on a player or a setting.

A port of the classic `poweradminurt`, **in slices**. The original is 3,846 lines across `iourt41.py`
and two per-version subclasses of itself, holding forty-nine commands and eleven background features
(team balancer, skill balancer, name checker, spectator check, bot support, headshot counter, rotation
manager, match mode…). Porting it as one change would be a change nobody could review, so it comes in
parts and this module's docstring records which.

**Slice 1: player control and server settings.** `!paslap`, `!panuke`, `!pakill`, `!pamute`,
`!paunmute`, `!pabigtext`, `!paset`, `!paget` and `!pavote`.

**Slice 2: the match settings** — twenty-one commands that each write one cvar, so they are a *table*
(`GAMETYPES`, `TOGGLES`, `NUMBERS` below) rather than twenty-one near-identical methods. The gametype switches (`!pactf`,
`!pabomb`, `!pajump`, …), the limits (`!pacaplimit`, `!patimelimit`, `!pafraglimit`), the toggles
(`!painstagib`, `!pahardcore`, `!pafunstuff`, `!paskins`, `!pawaverespawns`), the numbers
(`!pasetgravity`, `!parespawndelay`, `!parespawngod`, `!pahotpotato`, the wave delays), `!pastamina`,
`!pamoon`, `!pasetnextmap` and `!pagear`.

**Slice 3: moving players between teams.** `!paforce` (with its lock), `!paswap`, `!paswapteams` and
`!pashuffleteams`. Two of those name no player at all, which is what `GameProfile.server_verbs` is for.

**Slice 3b: the rest of the server commands.** `!pamaprestart`, `!pamapreload`, `!pacyclemap`,
`!paexec` and `!papublic` — the first three are one `server_verb` each.

**Later slices:** the background features, each of which is its own plugin-sized thing. `!pateams` and
`!pabalance` belong with the **team balancer** rather than here: they are its manual trigger, and porting
them without it would mean writing the balancing twice.

**Not ported, deliberately:**

* `!paveto` — `callvote` owns vote cancellation, and it is not Urban-Terror-only. The classic's
  `callvote` plugin actually *unregistered* `paveto` from the admin plugin when it loaded, which is two
  plugins negotiating over one command name at runtime; here `callvote`'s `!veto` simply is the command.
* `!paversion` — it printed the plugin's own version string. `!plugin info` answers that for every
  plugin.

Changed from the classic, and the first two are faults:

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
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from b3.core.commands import Command, CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.client import Client

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
}


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
        # stops them switching back.
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_team_change)
        self.on_load_config()
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

    # -- slice 3: moving players between teams -------------------------------

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
        """Put a locked player back where they were told to be.

        The classic did this too, and it is the only reason the lock means anything: `forceteam` moves
        somebody once, and nothing stops them switching straight back.
        """
        client = event.client
        if client is None:
            return
        held = self.locked_to(client)
        if held is None:
            return
        current = (client.team or "").strip().lower()
        wanted = "spec" if held == "s" else held
        if current == wanted:
            return
        self.console.apply_verb("forceteam", client, team=held)
        self.console.tell(
            client, self.message("pa_force_denied", team="spectator" if held == "s" else held)
        )

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
        ctx.reply(self.message("pa_swapped", first=first.name, second=second.name))

    @command("paswapteams", level=60, alias="swapteams")
    def cmd_paswapteams(self, ctx: CommandContext) -> None:
        """paswapteams - swap the two teams around"""
        if not self.console.apply_server_verb("swapteams"):
            ctx.reply(self.message("pa_unavailable", verb="swapteams"))
            return
        ctx.reply(self.message("pa_teams_swapped"))

    @command("pashuffleteams", level=60, alias="shuffleteams")
    def cmd_pashuffleteams(self, ctx: CommandContext) -> None:
        """pashuffleteams - shuffle everybody into new teams"""
        if not self.console.apply_server_verb("shuffleteams"):
            ctx.reply(self.message("pa_unavailable", verb="shuffleteams"))
            return
        ctx.reply(self.message("pa_teams_shuffled"))

    # -- slice 3b: the rest of the server commands ---------------------------

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
    "GAMETYPES",
    "TEAMS",
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
