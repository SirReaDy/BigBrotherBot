"""Urban Terror's own admin commands — the ones that act on a player or a setting.

A port of the classic `poweradminurt`, **in slices**. The original is 3,846 lines across `iourt41.py`
and two per-version subclasses of itself, holding forty-nine commands and eleven background features
(team balancer, skill balancer, name checker, spectator check, bot support, headshot counter, rotation
manager, match mode…). Porting it as one change would be a change nobody could review, so it comes in
parts and this module's docstring records which.

**This slice: player control and server settings.** `!paslap`, `!panuke`, `!pakill`, `!pamute`,
`!paunmute`, `!pabigtext`, `!paset`, `!paget` and `!pavote` — everything that needed the engine-verb
seam or an existing `Console` verb, and nothing that needs new parser work.

**Later slices, in the order they are worth doing:** the gametype and match-setting commands (twenty of
them, almost all a cvar and a value — `!pagear`, `!paffa`, `!pactf`, `!pasetgravity`, `!pacaplimit`…),
then team management (`!paforce`, `!pateams`, `!pabalance`, `!paswap`, the shuffles), then the
background features, each of which is its own plugin-sized thing.

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
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.client import Client

log = logging.getLogger(__name__)

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

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        player_level = as_int(self.settings.get("player_command_level"), 40)
        server_level = as_int(self.settings.get("server_command_level"), 60)
        for name in ("paslap", "panuke", "pakill", "pamute", "paunmute"):
            self._set_level(name, player_level)
        for name in ("pabigtext", "paset", "paget", "pavote"):
            self._set_level(name, server_level)

    def _set_level(self, name: str, level: int) -> None:
        registered = self.console.command_registry.get(name)
        if registered is not None:
            registered.min_level = level

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.schedule(self._run_repeats, second="*", name="PoweradminurtPlugin.repeats")
        self.subscribe(EventType.CLIENT_DISCONNECT, self._on_disconnect)
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
    "DEFAULTS",
    "MAX_REPEATS",
    "MESSAGES",
    "REPEAT_SECONDS",
    "VOTE_CVAR",
    "VOTE_DEFAULT",
    "PoweradminurtPlugin",
    "Repeat",
]
