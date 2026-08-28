"""Battlefield 3's admin commands — the teams, the scrambler, the VIP list and the server configs.

A port of the classic `poweradminbf3` plugin, the largest of the four Frostbite `poweradmin*` and the
one the other three are near-copies of. Its command names, levels and aliases are the classic's,
because operators' config files are full of them.

What it does falls into five groups. **Round control** — `!roundnext`, `!roundrestart`, `!endround`
and `!serverreboot`. **Players** — `!kill`, `!nuke`, `!changeteam` and `!swap`, which is the one that
needs squads: a Battlefield squad is four players who spawn on each other, so swapping two players
between teams means putting each into the *other's* squad. **Keeping the teams even** — `!autoassign`
puts an arriving player on the smaller side, `!autobalance` moves the most recent joiners when the
sides drift apart, and the **scrambler** deals everybody out again at a round or a map boundary, by
score where the server reported one. **Settings** — `!unlockmode`, `!vehicles`, `!idle`, `!gunmaster`.
And **the two lists a Battlefield server keeps**: the reserved slots (`!vips` and its family) and the
preset server configs (`!listconfig`, `!loadconfig`).

Changed from the classic, and the first is why two of its features were dead:

* **A log line raised inside the round-start handler.** The auto-scrambler's gamemode blacklist was
  announced with `r"...%s..." + " ...'" % blacklist` — where `%` binds to the *second* literal, which
  has no placeholder, so it raised `TypeError` every time the blacklist matched. Everything after it
  in that handler never ran, which is `_scramblingdone = True` and the autobalance timer. So on any
  server whose current gamemode was in the scrambler blacklist, **autoassign and autobalance were
  both dead for the whole map** — and the captured test asserted the surface behaviour ("no scramble
  happened") as correct.
* **Nothing sleeps, and nothing is a thread.** The classic slept a second in four command handlers,
  2.5 seconds in `!nuke`, and up to twenty seconds inside the autobalance run — all on the bot's one
  thread, so the whole bot stopped answering. The config loader used
  `thread.start_new_thread` and the announcements are a deadline on the plugin's one scheduled pass.
* **The score scramble sorted scores as text.** `sorted(scores, key=lambda x: x['score'])` over the
  values a Frostbite block reports, which are strings — so `"9"` ranked above `"100"` and the
  strategy that exists to even out skill dealt the teams almost at random. The sum in the same method
  used `int()`, which is what shows the intent.
* **One immunity rule, not three.** `!kill` refused a player at or above the admin's level, `!swap`
  refused one strictly above it, `!changeteam` refused one strictly above *and* only when the admin
  was under `no_level_check_level`, and **`!nuke` checked nothing at all** — so an admin who could not
  `!kill` one senior admin could kill every one of them at once. The classic's escape hatch is kept:
  an admin at or above `no_level_check_level` is not level-checked.
* **A swap with one spectator went ahead.** The guard was `A not in (1, 2) and B not in (1, 2)`, so it
  only refused when *both* players were teamless; with one of them at the deploy screen the other was
  moved to team 0.
* **A one-character name meant "swap with me".** `if len(pA) == 1 or otherparams is None` — so a
  player really called `x` could not be named as the first player of a swap.
* **The join order is kept by client, not by name.** `client_connect` appended `client.name` and
  `client_disconnect` removed the name the leave line carried, so a rename left an entry that never
  came out and the list grew for as long as the bot ran. The autobalancer walks that list to decide
  who moves, so it was walking ghosts.
* **`!endround` with no team no longer raises.** It read the four team scores into a dict and called
  `max()` on it — which raises `ValueError` on an empty dict, and the dict is empty on a gamemode
  with no team scores at all.
* **The server config directory is configured, not inside the plugin.** The classic looked in
  `@b3/plugins/poweradminbf3/serverconfigs`, i.e. inside its own source tree, which a pip-installed
  bot has no business writing to. It is a setting here, as it is in `poweradmincod7`, and only a file
  in that directory can be loaded — never a path.
* **A line in a server config that is neither a setting nor a map is reported.** The classic matched
  `vars.*` and map entries and dropped everything else in silence, so a config with an
  `admin.password` line in it looked like it had been applied in full.
* **`!gunmaster 12` read only the first character.** `int(data[0])` — latent, since this title has
  nine presets, and wrong the moment DICE adds a tenth.
* **`movedByBot` is gone.** The classic set that variable on every move and read it nowhere in this
  plugin.

**Not ported.** `!paversion`-style version printing was never here. `!punkbuster` was, and it has
**moved to the admin plugin** beside `!pbss`: it is the one command here that is not really
Battlefield-specific — every PunkBuster family has a `pb_sv_command` — and leaving it in meant the
Battlefield spelling of that verb living inside a plugin. It is the same command, at the same level;
the alias is `!pbcmd` rather than `!punk`.

`!swap` and `!nuke` are also `poweradminurt` aliases (`paswap`, `panuke`). Neither plugin is
restricted by title, so an operator loading both on one server will find the second one **refused**
those names, with both plugins named in the log — the first to load keeps them, which is the order of
their own `plugins:` list.
"""

from __future__ import annotations

import logging
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.core.plugin import Plugin
from b3.core.util import as_int, as_level, as_word, match_names, sanitize_rcon_value
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: How long the bot waits after announcing something before doing it, so the players reading the
#: announcement get a moment. The classic's own figures.
ANNOUNCE_SECONDS = 1.0
NUKE_SECONDS = 2.5

#: A scramble needs somebody to scramble. Two players are already "one each".
MIN_SCRAMBLE_PLAYERS = 3

#: Lines of a server config sent per scheduled pass, and the pass runs once a second.
CONFIG_LINES_PER_PASS = 4

#: The nine Gun Master weapon presets, by the index `vars.gunMasterWeaponsPreset` takes. Names only:
#: the classic carried the full weapon list for each and never sent or showed one.
GUNMASTER_PRESETS = (
    "Standard Weapon list",
    "Standard Weapon list REVERSED",
    "Light Weight",
    "Heavy Gear",
    "Pistol run!",
    "Snipers Heaven",
    "US arms race",
    "RU arms race",
    "EU arms race",
)

#: `vars.unlockMode` — which weapon unlocks players may bring.
UNLOCK_MODES = ("all", "common", "stats", "none")

#: The settings this plugin reads and writes, with the `vars.` prefix the engine wants. Named here
#: rather than spelled out at each call site so that the four commands that touch them agree.
UNLOCK_CVAR = "vars.unlockMode"
VEHICLES_CVAR = "vars.vehicleSpawnAllowed"
IDLE_CVAR = "vars.idleTimeout"
GUNMASTER_CVAR = "vars.gunMasterWeaponsPreset"

DEFAULTS: dict[str, object] = {
    # An admin at or above this level is not level-checked when acting on another player. The
    # classic's escape hatch, at its own default of fulladmin.
    "no_level_check_level": 60,
    # Players at or above this level are never moved by the autoassigner or the autobalancer.
    "no_autoassign_level": 20,
    "autoassign": False,
    "autobalance": False,
    # Seconds between autobalance passes. The classic's floor of 30 is kept: below it the bot spends
    # the round announcing moves.
    "autobalance_timer": 120,
    # How many players one side may be ahead by before anybody is moved. The classic's floor of 2 is
    # kept, because 1 means every arrival is moved straight back again.
    "team_swap_threshold": 3,
    # Allow one more of an imbalance at 20 players and another at 40, which is what stops a busy
    # server shuffling people back and forth all round.
    "team_swap_threshold_prop": False,
    # How long a `!yell` stays on screen.
    "yell_duration": 10,
    # Where this server's preset `.cfg` files are, for `!listconfig` and `!loadconfig`. No default:
    # the classic looked inside its own installed source tree.
    "config_dir": "",
}

SCRAMBLER_DEFAULTS: dict[str, object] = {
    # off | round | map — when the scrambler runs by itself.
    "mode": "off",
    # random | score — how it deals the teams out.
    "strategy": "random",
    # Gamemodes it never runs on. Squad Rush and Squad Deathmatch by default, as the classic shipped:
    # scrambling a game whose whole point is the squad you are in is not a favour to anybody.
    "gamemodes_blacklist": ["SquadRush0", "SquadDeathMatch0"],
}

MESSAGES = {
    "pab3_round_next": "moving to the next round",
    "pab3_round_restart": "restarting the round",
    "pab3_round_end": "ending the round; team {team} wins",
    "pab3_round_end_usage": "!endround [<team>] — and no team has a score, so name one",
    "pab3_reboot_confirm": "this restarts the game server — type !serverreboot yes to go ahead",
    "pab3_reboot": "restarting the game server",
    "pab3_unsupported": "this title has no verb for that",
    "pab3_kill_usage": "!kill <player> [<reason>]",
    "pab3_killed": "killed by an admin",
    "pab3_killed_reason": "killed by an admin: {reason}",
    "pab3_nuke_usage": "!nuke <all|{teams}> [<reason>]",
    "pab3_nuke_warning": "incoming nuke",
    "pab3_nuke_all": "killing everybody",
    "pab3_nuke_team": "killing the {team} team",
    "pab3_nuke_nobody": "nobody there to kill",
    "pab3_changeteam_usage": "!changeteam <player>",
    "pab3_changeteam": "{name} moved to the {team} team",
    "pab3_no_team": "the server has not said which team {name} is on",
    "pab3_swap_usage": "!swap <player> [<player>]",
    "pab3_swap_same": "{first} and {second} are in the same team and squad already",
    "pab3_swapped": "swapped {first} with {second}",
    "pab3_nextmap_usage": "!setnextmap <map>{extras}",
    "pab3_nextmap_unknown": "no map like {map} — try {maps}",
    "pab3_nextmap": "the next map will be {map}",
    "pab3_scramble_on": "the teams will be scrambled when this round ends",
    "pab3_scramble_off": "the scramble is cancelled",
    "pab3_scramble_mode_usage": "!scramblemode random|score",
    "pab3_scramble_mode": "the scrambler will deal by {mode}",
    "pab3_autoscramble_usage": "!autoscramble off|round|map",
    "pab3_autoscramble": "the scrambler runs {when}",
    "pab3_autoscramble_off": "the scrambler only runs when asked",
    "pab3_scramble_too_few": "too few players to scramble",
    "pab3_scrambling": "scrambling the teams",
    "pab3_toggle_usage": "!{command} on|off",
    "pab3_state": "{what} is {state}",
    "pab3_unlock_usage": "!unlockmode {modes}",
    "pab3_unlock": "the unlock mode is {mode}",
    "pab3_idle_usage": "!idle on|off|<minutes>",
    "pab3_idle_off": "idle kicking is off",
    "pab3_idle": "idle players are kicked after {minutes} minutes",
    "pab3_gunmaster": "gun master preset {number}: {name}",
    "pab3_gunmaster_usage": "!gunmaster [<0-{highest}>|show]",
    "pab3_gunmaster_list": "presets: {presets}",
    "pab3_gunmaster_set": "the next gun master round uses {number}: {name}",
    "pab3_autoassign": "arrivals are put on the smaller team: {state}",
    "pab3_autobalance": "the teams are balanced automatically: {state}",
    "pab3_autobalance_usage": "!autobalance on|off|now",
    "pab3_balancing": "balancing the teams",
    "pab3_balanced": "the teams are even",
    "pab3_moving": "moving {name} to the {team} team",
    "pab3_yell_usage": "!{command} <message>",
    "pab3_yell_player_usage": "!yellplayer <player> <message>",
    "pab3_vips_none": "no VIPs are connected",
    "pab3_vips": "connected VIPs: {vips}",
    "pab3_viplist_empty": "the VIP list is empty",
    "pab3_viplist_none": "no VIP matching {filter} among the {count} there are",
    "pab3_viplist_other": "other VIPs: {vips}",
    "pab3_viplist_more": "other VIPs: {vips} and {count} more",
    "pab3_vip_usage": "!{command} <player>",
    "pab3_vip_added": "{name} is a VIP now",
    "pab3_vip_removed": "{name} is no longer a VIP",
    "pab3_vip_absent": "there is no VIP called {name}",
    "pab3_vip_cleared": "the VIP list is empty now",
    "pab3_vip_loaded": "the VIP list was read from disk: {count} name(s)",
    "pab3_vip_saved": "the VIP list was written to disk: {count} name(s)",
    "pab3_cfg_dir_unset": "no config directory is configured for this plugin",
    "pab3_cfg_dir_missing": "the configured config directory does not exist",
    "pab3_cfg_none": "no .cfg files there",
    "pab3_cfg_list": "config files: {files}",
    "pab3_load_usage": "!loadconfig <name>",
    "pab3_load_unknown": "no config called {name}",
    "pab3_load_which": "did you mean {names}?",
    "pab3_load_busy": "{name} is still being sent — wait for it to finish",
    "pab3_load_started": "sending {name}: {lines} line(s)",
    "pab3_load_done": "{name} has been sent in full",
    "pab3_load_skipped": "{name}: line {line} is neither a setting nor a map, and was not sent",
    "pab3_load_failed": "{name} could not be read ({error})",
}


@dataclass(slots=True)
class Delayed:
    """Something announced now and done a few seconds later, on the scheduled pass."""

    due: float
    action: Callable[[], object]


@dataclass(slots=True)
class Loading:
    """A server config file going out a few lines at a time."""

    name: str
    lines: deque[str] = field(default_factory=deque)
    client: Client | None = None


class Poweradminbf3Plugin(Plugin):
    """Battlefield 3's admin commands, its team management and its two server-side lists."""

    requires_plugins = ("admin",)
    requires_parsers = ("bf3",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.scrambler: dict[str, object] = dict(SCRAMBLER_DEFAULTS)
        self._delayed: list[Delayed] = []
        self._loading: Loading | None = None
        #: Whoever asked for a scramble at the end of this round.
        self.scramble_planned = False
        #: The last round's scoreboard, for the score strategy. Empty until a round has ended, which
        #: is why that strategy falls back to random rather than pretending.
        self._last_scores: list[PlayerInfo] = []
        #: Players in the order they arrived — by *client*, not by name, so a rename cannot leave an
        #: entry behind. The autobalancer moves the most recent joiners first.
        self._joined: list[Client] = []
        #: Set once a round has finished, because balancing a round already in progress moves people
        #: mid-firefight; and cleared while a scramble is pending, so the balancer does not undo it.
        self._round_finished = False
        self._settled = True
        self._next_balance = 0.0
        self._last_idle_seconds = "300"

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.scrambler = {**SCRAMBLER_DEFAULTS, **(config.get("scrambler") or {})}
        # The classic's floors, and both matter: a threshold of 1 moves every arrival straight back,
        # and a timer under half a minute spends the round announcing moves.
        self.settings["team_swap_threshold"] = max(
            2, as_int(self.settings.get("team_swap_threshold"), 3)
        )
        self.settings["autobalance_timer"] = max(
            30, as_int(self.settings.get("autobalance_timer"), 120)
        )
        # `mode: off` in YAML is the boolean False, not the word — the trap that costs a word-valued
        # setting its whole vocabulary.
        self.scrambler["mode"] = as_word(self.scrambler.get("mode"), "off").lower()
        self.scrambler["strategy"] = as_word(self.scrambler.get("strategy"), "random").lower()
        if self.settings.get("autobalance") and not self.settings.get("autoassign"):
            # The classic's rule, and it is not arbitrary: balancing without assigning means the bot
            # evens the teams and then lets the next arrival unbalance them again.
            log.info("poweradminbf3: autobalance is on, so autoassign is too")
            self.settings["autoassign"] = True

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.schedule(self._run_pending, second="*", name="Poweradminbf3Plugin.pending")
        self.subscribe(EventType.GAME_ROUND_START, self.on_round_start)
        self.subscribe(EventType.GAME_ROUND_END, self.on_round_end)
        self.subscribe(EventType.GAME_ROUND_PLAYER_SCORES, self.on_scores)
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_team_change)
        directory = self.config_dir()
        if directory is not None and not directory.is_dir():
            log.warning(
                "poweradminbf3: config_dir %s does not exist, so !listconfig and !loadconfig have "
                "nothing to offer",
                directory,
            )

    def config_dir(self) -> Path | None:
        configured = str(self.settings.get("config_dir") or "").strip()
        return Path(configured).expanduser() if configured else None

    # -- the round -----------------------------------------------------------

    def on_round_start(self, event: Event) -> None:
        """A scramble happens here, and this is where the classic's handler used to raise.

        Everything below the blacklist check — the flag that lets the autoassigner run, and the next
        balance deadline — is why the `TypeError` in that log line mattered: it took both with it.
        """
        data = event.data if isinstance(event.data, dict) else {}
        first_of_map = str(data.get("roundsPlayed", "")).strip() in ("", "0")
        gametype = str(data.get("g_gametype", "") or self.console.game.gametype or "")
        if self.scramble_planned:
            self.scramble_planned = False
            self.scramble()
        elif self._blacklisted(gametype):
            log.info(
                "poweradminbf3: not scrambling, because %s is in the scrambler's blacklist",
                gametype,
            )
        elif self.scrambler["mode"] == "round" or (
            self.scrambler["mode"] == "map" and first_of_map
        ):
            self.scramble()
        self._settled = True
        self._schedule_balance()

    def on_round_end(self, event: Event) -> None:
        self._round_finished = True
        self._settled = False
        self._next_balance = 0.0

    def on_scores(self, event: Event) -> None:
        """The final scoreboard, which is the only thing that makes a score scramble possible."""
        rows = event.data
        if isinstance(rows, list):
            self._last_scores = [row for row in rows if isinstance(row, PlayerInfo)]

    def on_auth(self, event: Event) -> None:
        client = event.client
        if client is None:
            return
        if client not in self._joined:
            self._joined.append(client)
        self.autoassign(client)

    def on_disconnect(self, event: Event) -> None:
        client = event.client
        if client is not None and client in self._joined:
            # By client, not by name: the classic removed the name the leave line carried, so a
            # rename left an entry that never came out and the list grew all session.
            self._joined.remove(client)

    def on_team_change(self, event: Event) -> None:
        if event.client is not None:
            self.autoassign(event.client)

    def _blacklisted(self, gametype: str) -> bool:
        """Whether the auto-scrambler leaves this gamemode alone.

        A comma-separated string is accepted as well as a list, because the classic's config field
        was one and an imported config still says `SquadRush0,SquadDeathMatch0`.
        """
        listed = self.scrambler.get("gamemodes_blacklist") or []
        names = (
            [word for word in listed.replace(",", " ").split() if word]
            if isinstance(listed, str)
            else [str(name) for name in listed]
            if isinstance(listed, (list, tuple))
            else []
        )
        wanted = gametype.strip().lower()
        return any(name.strip().lower() == wanted for name in names)

    # -- round control -------------------------------------------------------

    @command("roundnext", level=40, alias="rnext")
    def cmd_roundnext(self, ctx: CommandContext) -> None:
        """roundnext - move to the next round without finishing this one"""
        self._announce_then(ctx, "pab3_round_next", "round_next")

    @command("roundrestart", level=40, alias="rrestart")
    def cmd_roundrestart(self, ctx: CommandContext) -> None:
        """roundrestart - restart the round being played"""
        self._announce_then(ctx, "pab3_round_restart", "round_restart")

    @command("endround", level=60, alias="eol")
    def cmd_endround(self, ctx: CommandContext) -> None:
        """endround [<team>] - end the round; the team named wins, or whoever is ahead"""
        wanted = ctx.args.strip()
        team = wanted if wanted.isdigit() else self.winning_team()
        if not team:
            # `max()` over an empty dict, in the classic. A gamemode with no team scores has none.
            ctx.reply(self.message("pab3_round_end_usage"))
            return
        self._announce_then(ctx, "pab3_round_end", "round_end", team=team)

    @command("serverreboot", level=100)
    def cmd_serverreboot(self, ctx: CommandContext) -> None:
        """serverreboot yes - restart the game server itself"""
        if not self.console.supports_server_verb("server_shutdown"):
            ctx.reply(self.message("pab3_unsupported"))
            return
        if ctx.args.strip().lower() not in ("yes", "y"):
            # The classic carried a `@todo: add dialog - Demand that the user wants to restart
            # really` above this command for years. This is that dialog.
            ctx.reply(self.message("pab3_reboot_confirm"))
            return
        log.warning("poweradminbf3: %s is restarting the game server", ctx.client.name)
        self.console.say(self.message("pab3_reboot"))
        self._after(ANNOUNCE_SECONDS, lambda: self.console.apply_server_verb("server_shutdown"))

    def _announce_then(self, ctx: CommandContext, key: str, verb: str, **values: str) -> None:
        """Say what is about to happen, then do it a moment later — never by sleeping."""
        if not self.console.supports_server_verb(verb):
            ctx.reply(self.message("pab3_unsupported"))
            return
        self.console.say(self.message(key, **values))
        self._after(ANNOUNCE_SECONDS, lambda: self.console.apply_server_verb(verb, **values))

    def winning_team(self) -> str:
        """The team with the highest score, in the engine's own spelling. Empty if nobody has one."""
        best, best_score = "", None
        for row in self._last_scores:
            if not row.team:
                continue
            if best_score is None or row.score > best_score:
                best, best_score = row.team, row.score
        if best:
            return best
        # No scoreboard yet this map: fall back to counting who is winning by team totals of the
        # players on the server, which is all a bot with no round-over event can honestly say.
        totals: dict[str, int] = {}
        for client in self.console.clients.connected():
            engine_team = self.console.team_id(client.team or "")
            if engine_team:
                totals[engine_team] = totals.get(engine_team, 0) + 1
        return max(totals, key=lambda key: totals[key]) if totals else ""

    # -- players -------------------------------------------------------------

    @command("kill", level=40)
    def cmd_kill(self, ctx: CommandContext) -> None:
        """kill <player> [<reason>] - kill a player without touching the scoreboard"""
        parts = ctx.args.split(None, 1)
        if not parts:
            ctx.reply(self.message("pab3_kill_usage"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        reason = parts[1].strip() if len(parts) > 1 else ""
        self.kill(target, reason)

    def kill(self, target: Client, reason: str = "") -> bool:
        """Kill one player and tell them why. False where this title has no verb for it."""
        if not self.console.apply_verb("kill", target):
            return False
        key = "pab3_killed_reason" if reason else "pab3_killed"
        self.console.tell(target, self.message(key, reason=reason))
        return True

    @command("nuke", level=40)
    def cmd_nuke(self, ctx: CommandContext) -> None:
        """nuke <all|red|blue> [<reason>] - kill everybody, or one team"""
        parts = ctx.args.split(None, 1)
        teams = "|".join(sorted({name for name in self._team_names() if name}))
        wanted = parts[0].strip().lower() if parts else ""
        reason = parts[1].strip() if len(parts) > 1 else ""
        if wanted != "all" and not self.console.team_id(wanted):
            ctx.reply(self.message("pab3_nuke_usage", teams=teams))
            return
        # The classic checked nothing here at all, so an admin who could not `!kill` one senior
        # admin could kill every one of them with this.
        targets = [
            client
            for client in self.console.clients.connected()
            if (wanted == "all" or (client.team or "").lower() == wanted)
            and self.outranks(ctx.client, client)
        ]
        if not targets:
            ctx.reply(self.message("pab3_nuke_nobody"))
            return
        self.console.say_big(self.message("pab3_nuke_warning"))
        key = "pab3_nuke_all" if wanted == "all" else "pab3_nuke_team"
        self.console.say(self.message(key, team=wanted))
        log.info("poweradminbf3: %s nuked %s (%d players)", ctx.client.name, wanted, len(targets))
        self._after(NUKE_SECONDS, lambda: [self.kill(target, reason) for target in targets])

    @command("changeteam", level=20)
    def cmd_changeteam(self, ctx: CommandContext) -> None:
        """changeteam <player> - move a player to the other team"""
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pab3_changeteam_usage"))
            return
        target = self.resolve_client(ctx, wanted[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        other = self.other_team(target)
        if not other:
            ctx.reply(self.message("pab3_no_team", name=target.name))
            return
        self.move(target, other)
        ctx.reply(self.message("pab3_changeteam", name=target.name, team=self._name_of(other)))

    @command("swap", level=20)
    def cmd_swap(self, ctx: CommandContext) -> None:
        """swap <player> [<player>] - swap two players' teams, squads and all"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("pab3_swap_usage"))
            return
        first = self.resolve_client(ctx, parts[0])
        if first is None:
            return
        # The classic read `len(pA) == 1` as "swap with me", so a player really called `x` could
        # never be the first half of a swap.
        second = ctx.client if len(parts) < 2 else self.resolve_client(ctx, parts[1])
        if second is None:
            return
        if not self.may_act_on(ctx, first) or (
            second is not ctx.client and not self.may_act_on(ctx, second)
        ):
            return
        team_a, team_b = (
            self.console.team_id(first.team or ""),
            self.console.team_id(second.team or ""),
        )
        # `and` in the classic, so a swap with one spectator went ahead and moved the other to
        # team 0.
        if not team_a or not team_b:
            ctx.reply(self.message("pab3_no_team", name=first.name if not team_a else second.name))
            return
        if team_a == team_b and first.squad == second.squad:
            ctx.reply(self.message("pab3_swap_same", first=first.name, second=second.name))
            return
        squad_a, squad_b = first.squad, second.squad
        # Three moves, not two: the first player has to leave their squad before the second can be
        # put into it, or the server refuses a full squad.
        self.move(first, team_b)
        self.move(second, team_a, squad_a)
        if squad_b:
            self.move(first, team_b, squad_b)
        ctx.reply(self.message("pab3_swapped", first=first.name, second=second.name))

    def move(self, client: Client, team: str, squad: str = "0") -> bool:
        """Put a player on a team, and in a squad if one is named."""
        moved = self.console.apply_verb("move", client, team=team, squad=squad or "0")
        if moved:
            client.team = self._name_of(team)
            client.squad = squad or "0"
        return moved

    def other_team(self, client: Client) -> str:
        """The team a `!changeteam` would put this player on, in the engine's spelling.

        Two sides, except in Squad Deathmatch, which has four — the classic's rule, and it is why the
        arithmetic is a rotation rather than a swap of 1 and 2.
        """
        current = self.console.team_id(client.team or "")
        if not current or not current.isdigit():
            return ""
        sides = 4 if (self.console.game.gametype or "").strip() == "SquadDeathMatch0" else 2
        return str((int(current) % sides) + 1)

    def _name_of(self, engine_team: str) -> str:
        for name in self._team_names():
            if name and self.console.team_id(name) == engine_team:
                return name
        return engine_team

    @staticmethod
    def _team_names() -> tuple[str, ...]:
        return ("red", "blue", "green", "yellow")

    # -- who may act on whom -------------------------------------------------

    def outranks(self, admin: Client, target: Client) -> bool:
        """Whether `admin` may act on `target`, without saying anything about it."""
        if target is admin or (target.id is not None and target.id == admin.id):
            return False
        if admin.max_level() >= as_level(self.settings.get("no_level_check_level"), 60):
            return True
        return target.max_level() < admin.max_level()

    def may_act_on(self, ctx: CommandContext, target: Client) -> bool:
        """The same rule, and it explains itself. One rule where the classic had three and a half."""
        if self.outranks(ctx.client, target):
            return True
        if target is ctx.client or (target.id is not None and target.id == ctx.client.id):
            ctx.reply(self.message("action_denied_self"))
            return False
        key = "action_denied_masked" if target.is_masked() else "action_denied_level"
        ctx.reply(self.message(key, name=target.name))
        return False

    # -- the next map --------------------------------------------------------

    @command("setnextmap", level=20, alias="snmap")
    def cmd_setnextmap(self, ctx: CommandContext) -> None:
        """setnextmap <map>[, <gamemode>[, <rounds>]] - the map to play after this one"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pab3_nextmap_usage", extras=self.console.map_usage()))
            return
        request = self.console.parse_map_request(wanted)
        maps = self.console.get_maps()
        found = match_names(request.name, [(m, self.console.map_display(m)) for m in maps])
        chosen = found[0] if len(found) == 1 else ""
        if not chosen:
            listed = ", ".join(self.console.map_display(m) for m in (found or maps)[:5])
            ctx.reply(self.message("pab3_nextmap_unknown", map=request.name, maps=listed))
            return
        self.set_next_map(chosen, request.extras)
        ctx.reply(self.message("pab3_nextmap", map=self.console.map_display(chosen)))

    def set_next_map(self, map_id: str, extras: dict[str, str]) -> None:
        """Point the server's map list at ``map_id`` for the next round.

        Three commands rather than one, because on this engine the next map is an *index into the
        rotation* and not a setting: put the map in just after the one being played, then say that
        the next index is that one. Done here with `rcon_words` rather than as a `Console` method,
        because the arguments (a gamemode and a round count per rotation entry) exist on no other
        engine and a port-wide method taking them would be a Frostbite method in disguise.
        """
        index = self._current_index() + 1
        gamemode = extras.get("gamemode") or self._current_gamemode()
        rounds = extras.get("rounds") or "2"
        self.console.rcon_words(
            "mapList.add "
            f'"{sanitize_rcon_value(map_id)}" "{sanitize_rcon_value(gamemode)}" '
            f'"{sanitize_rcon_value(rounds)}" {index}'
        )
        self.console.rcon_words(f"mapList.setNextMapIndex {index}")
        log.info("poweradminbf3: %s is next, at rotation index %d", map_id, index)

    def _current_index(self) -> int:
        reply = self.console.rcon_words("mapList.getMapIndices")
        return int(reply[0]) if reply and reply[0].isdigit() else 0

    def _current_gamemode(self) -> str:
        return (self.console.game.gametype or "ConquestLarge0").strip()

    # -- the scrambler -------------------------------------------------------

    @command("scramble", level=20)
    def cmd_scramble(self, ctx: CommandContext) -> None:
        """scramble - scramble the teams when this round ends, or cancel that"""
        self.scramble_planned = not self.scramble_planned
        key = "pab3_scramble_on" if self.scramble_planned else "pab3_scramble_off"
        ctx.reply(self.message(key))

    @command("scramblemode", level=20)
    def cmd_scramblemode(self, ctx: CommandContext) -> None:
        """scramblemode <random|score> - how the scrambler deals the teams out"""
        wanted = ctx.args.strip().lower()
        # The classic matched on the first letter alone, so `!scramblemode rubbish` meant random.
        if wanted not in ("random", "score"):
            ctx.reply(self.message("pab3_scramble_mode_usage"))
            return
        self.scrambler["strategy"] = wanted
        ctx.reply(self.message("pab3_scramble_mode", mode=wanted))

    @command("autoscramble", level=20)
    def cmd_autoscramble(self, ctx: CommandContext) -> None:
        """autoscramble <off|round|map> - when the scrambler runs by itself"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("off", "round", "map"):
            ctx.reply(self.message("pab3_autoscramble_usage"))
            return
        self.scrambler["mode"] = wanted
        if wanted == "off":
            ctx.reply(self.message("pab3_autoscramble_off"))
            return
        ctx.reply(
            self.message(
                "pab3_autoscramble",
                when="at every round start" if wanted == "round" else "at every map change",
            )
        )

    def scramble(self) -> bool:
        """Deal everybody out into two teams. False when there is nobody to deal.

        By score where the last round's scoreboard says so, otherwise at random. The scores are
        compared as **numbers**: the classic sorted the strings a Frostbite block reports, so `"9"`
        ranked above `"100"` and the strategy that exists to even out skill dealt almost at random.
        """
        players = self.scramble_order()
        if len(players) < MIN_SCRAMBLE_PLAYERS:
            log.info("poweradminbf3: %d player(s) is too few to scramble", len(players))
            return False
        self.console.say(self.message("pab3_scrambling"))
        for index, client in enumerate(players):
            self.move(client, str((index % 2) + 1))
        return True

    def scramble_order(self) -> list[Client]:
        """The players, in the order they are dealt out — strongest first for a score scramble."""
        players = [c for c in self.console.clients.connected() if not c.is_bot]
        if self.scrambler["strategy"] != "score":
            random.shuffle(players)
            return players
        scores = {row.name: row.score for row in self._last_scores}
        if not any(scores.values()):
            log.info("poweradminbf3: no scores from the last round, so scrambling at random")
            random.shuffle(players)
            return players
        # Anybody the scoreboard did not mention goes last, in a random order, which is what the
        # classic did with them too.
        known = [c for c in players if c.name in scores]
        unknown = [c for c in players if c.name not in scores]
        random.shuffle(unknown)
        known.sort(key=lambda c: scores[c.name], reverse=True)
        return known + unknown

    # -- keeping the teams even ----------------------------------------------

    @command("autoassign", level=20)
    def cmd_autoassign(self, ctx: CommandContext) -> None:
        """autoassign <on|off> - put arriving players on the smaller team"""
        state = self._toggle(ctx, "autoassign")
        if state is None:
            return
        if not state and self.settings.get("autobalance"):
            self.settings["autobalance"] = False
            ctx.reply(self.message("pab3_autobalance", state="off"))
        ctx.reply(self.message("pab3_autoassign", state="on" if state else "off"))

    @command("autobalance", level=20)
    def cmd_autobalance(self, ctx: CommandContext) -> None:
        """autobalance <on|off|now> - even the teams up as the round runs"""
        wanted = ctx.args.strip().lower()
        if wanted == "now":
            if not self.balance():
                ctx.reply(self.message("pab3_balanced"))
            return
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pab3_autobalance_usage"))
            return
        self.settings["autobalance"] = wanted == "on"
        if wanted == "on" and not self.settings.get("autoassign"):
            self.settings["autoassign"] = True
            ctx.reply(self.message("pab3_autoassign", state="on"))
        self._schedule_balance()
        ctx.reply(self.message("pab3_autobalance", state=wanted))

    def _toggle(self, ctx: CommandContext, setting: str) -> bool | None:
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pab3_toggle_usage", command=ctx.command.name))
            return None
        self.settings[setting] = wanted == "on"
        return wanted == "on"

    def autoassign(self, client: Client) -> bool:
        """Move an arriving player to the smaller team, if they landed on the bigger one."""
        if not self.settings.get("autoassign") or not self._round_finished or not self._settled:
            return False
        if client.max_level() >= as_level(self.settings.get("no_autoassign_level"), 20):
            return False
        counts = self.team_counts()
        if sum(counts.values()) < MIN_SCRAMBLE_PLAYERS:
            return False
        theirs = self.console.team_id(client.team or "")
        if not theirs:
            return False
        threshold = self.swap_threshold(sum(counts.values()))
        for other, count in counts.items():
            if other != theirs and counts.get(theirs, 0) - count >= threshold:
                log.info("poweradminbf3: moving %s to even the teams up", client.name)
                return self.move(client, other)
        return False

    def balance(self) -> bool:
        """Move the most recent joiners off the bigger team. True if anybody moved."""
        counts = self.team_counts()
        if len(counts) < 2:
            return False
        threshold = self.swap_threshold(sum(counts.values()))
        bigger = max(counts, key=lambda key: counts[key])
        smaller = min(counts, key=lambda key: counts[key])
        excess = counts[bigger] - counts[smaller]
        if excess < threshold:
            return False
        self.console.say(self.message("pab3_balancing"))
        exempt = as_level(self.settings.get("no_autoassign_level"), 20)
        moved = 0
        # Most recent first, which is the fairest answer available: somebody who has been playing
        # the whole round did not cause the imbalance.
        for client in reversed(self._joined):
            if moved >= excess // 2:
                break
            if self.console.team_id(client.team or "") != bigger:
                continue
            if client.max_level() >= exempt:
                continue
            if self.move(client, smaller):
                self.console.say(self.message("pab3_moving", name=client.name, team=smaller))
                moved += 1
        return moved > 0

    def team_counts(self) -> dict[str, int]:
        """How many players are on each team, keyed by the engine's team id."""
        counts: dict[str, int] = {}
        for client in self.console.clients.connected():
            engine_team = self.console.team_id(client.team or "")
            if engine_team:
                counts[engine_team] = counts.get(engine_team, 0) + 1
        return counts

    def swap_threshold(self, players: int) -> int:
        """How lopsided the teams may be before anybody is moved."""
        threshold = as_int(self.settings.get("team_swap_threshold"), 3)
        if self.settings.get("team_swap_threshold_prop"):
            threshold += (players > 20) + (players > 40)
        return threshold

    def _schedule_balance(self) -> None:
        if not self.settings.get("autobalance"):
            self._next_balance = 0.0
            return
        seconds = as_int(self.settings.get("autobalance_timer"), 120)
        self._next_balance = self.console.clock.now() + seconds

    # -- settings ------------------------------------------------------------

    @command("unlockmode", level=60)
    def cmd_unlockmode(self, ctx: CommandContext) -> None:
        """unlockmode [<all|common|stats|none>] - which weapon unlocks players may bring"""
        wanted = ctx.args.strip().lower()
        if not wanted:
            ctx.reply(
                self.message(
                    "pab3_unlock", mode=self.console.get_cvar(UNLOCK_CVAR) or "unknown to the bot"
                )
            )
            return
        if wanted not in UNLOCK_MODES:
            ctx.reply(self.message("pab3_unlock_usage", modes="|".join(UNLOCK_MODES)))
            return
        self.console.set_cvar(UNLOCK_CVAR, wanted)
        ctx.reply(self.message("pab3_unlock", mode=wanted))

    @command("vehicles", level=20)
    def cmd_vehicles(self, ctx: CommandContext) -> None:
        """vehicles [<on|off>] - whether vehicles spawn"""
        wanted = ctx.args.strip().lower()
        if not wanted:
            current = (self.console.get_cvar(VEHICLES_CVAR) or "").strip().lower()
            state = {"true": "on", "false": "off"}.get(current, "unknown to the bot")
            ctx.reply(self.message("pab3_state", what="vehicle spawning", state=state))
            return
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pab3_toggle_usage", command=ctx.command.name))
            return
        self.console.set_cvar(VEHICLES_CVAR, "true" if wanted == "on" else "false")
        ctx.reply(self.message("pab3_state", what="vehicle spawning", state=wanted))

    @command("idle", level=40)
    def cmd_idle(self, ctx: CommandContext) -> None:
        """idle [<on|off|minutes>] - kick players who stop playing"""
        wanted = ctx.args.strip().lower()
        current = (self.console.get_cvar(IDLE_CVAR) or "").strip()
        if not wanted:
            ctx.reply(self._idle_state(current))
            return
        if wanted == "off":
            if current and current != "0":
                self._last_idle_seconds = current
            self.console.set_cvar(IDLE_CVAR, "0")
            ctx.reply(self.message("pab3_idle_off"))
            return
        if wanted == "on":
            seconds = self._last_idle_seconds
        elif wanted.isdigit() and int(wanted) > 0:
            seconds = str(int(wanted) * 60)
        else:
            ctx.reply(self.message("pab3_idle_usage"))
            return
        self.console.set_cvar(IDLE_CVAR, seconds)
        ctx.reply(self._idle_state(seconds))

    def _idle_state(self, seconds: str) -> str:
        if not seconds:
            return self.message("pab3_state", what="idle kicking", state="unknown to the bot")
        if seconds == "0":
            return self.message("pab3_idle_off")
        minutes = as_int(seconds, 0) // 60
        return self.message("pab3_idle", minutes=minutes or 1)

    @command("gunmaster", level=40, alias="gm")
    def cmd_gunmaster(self, ctx: CommandContext) -> None:
        """gunmaster [<number>|show] - the weapon preset for the next Gun Master round"""
        highest = len(GUNMASTER_PRESETS) - 1
        wanted = ctx.args.strip().lower()
        if wanted == "show":
            listed = ", ".join(f"{n} {name}" for n, name in enumerate(GUNMASTER_PRESETS))
            ctx.reply(self.message("pab3_gunmaster_list", presets=listed))
            return
        if not wanted:
            number = as_int(self.console.get_cvar(GUNMASTER_CVAR), -1)
            if not 0 <= number <= highest:
                ctx.reply(self.message("pab3_gunmaster_usage", highest=highest))
                return
            ctx.reply(self.message("pab3_gunmaster", number=number, name=GUNMASTER_PRESETS[number]))
            return
        # `int(data[0])` in the classic — the first *character*, so a two-digit preset was unreachable
        # the moment DICE added a tenth.
        if not wanted.isdigit() or not 0 <= int(wanted) <= highest:
            ctx.reply(self.message("pab3_gunmaster_usage", highest=highest))
            return
        number = int(wanted)
        self.console.set_cvar(GUNMASTER_CVAR, str(number))
        ctx.reply(self.message("pab3_gunmaster_set", number=number, name=GUNMASTER_PRESETS[number]))

    # -- yelling -------------------------------------------------------------

    @command("yell", level=20, alias="y")
    def cmd_yell(self, ctx: CommandContext) -> None:
        """yell <message> - put a message on everybody's screen"""
        self._yell(ctx, "yell")

    @command("yellteam", level=20, alias="yt")
    def cmd_yellteam(self, ctx: CommandContext) -> None:
        """yellteam <message> - put a message on your team's screens"""
        self._yell(ctx, "yell_team", team=self.console.team_id(ctx.client.team or ""))

    @command("yellsquad", level=20, alias="ys")
    def cmd_yellsquad(self, ctx: CommandContext) -> None:
        """yellsquad <message> - put a message on your squad's screens"""
        self._yell(
            ctx,
            "yell_squad",
            team=self.console.team_id(ctx.client.team or ""),
            squad=ctx.client.squad or "0",
        )

    @command("yellplayer", level=20, alias="yp")
    def cmd_yellplayer(self, ctx: CommandContext) -> None:
        """yellplayer <player> <message> - put a message on one player's screen"""
        parts = ctx.args.split(None, 1)
        if len(parts) < 2:
            ctx.reply(self.message("pab3_yell_player_usage"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        if not self.console.apply_verb(
            "yell_player", target, text=parts[1].strip(), seconds=self._yell_seconds()
        ):
            ctx.reply(self.message("pab3_unsupported"))

    def _yell(self, ctx: CommandContext, verb: str, **values: str) -> None:
        text = ctx.args.strip()
        if not text:
            ctx.reply(self.message("pab3_yell_usage", command=ctx.command.name))
            return
        if not self.console.apply_server_verb(
            verb, text=text, seconds=self._yell_seconds(), **values
        ):
            ctx.reply(self.message("pab3_unsupported"))

    def _yell_seconds(self) -> str:
        return str(max(1, as_int(self.settings.get("yell_duration"), 10)))

    # -- the reserved slots list ---------------------------------------------

    @command("vips", level=20)
    def cmd_vips(self, ctx: CommandContext) -> None:
        """vips - the VIPs who are here"""
        here = self.connected_vips()
        if not here:
            ctx.reply(self.message("pab3_vips_none"))
            return
        ctx.reply(self.message("pab3_vips", vips=", ".join(here)))

    @command("viplist", level=40)
    def cmd_viplist(self, ctx: CommandContext) -> None:
        """viplist [<filter>] - the whole VIP list, or the part matching a filter"""
        vips = self.vip_list()
        if not vips:
            ctx.reply(self.message("pab3_viplist_empty"))
            return
        wanted = ctx.args.strip().lower()
        if wanted:
            vips = [name for name in vips if wanted in name.lower()]
            if not vips:
                ctx.reply(
                    self.message(
                        "pab3_viplist_none", filter=ctx.args.strip(), count=len(self.vip_list())
                    )
                )
                return
        here = [name for name in vips if name in self.connected_vips()]
        if here:
            ctx.reply(self.message("pab3_vips", vips=", ".join(here)))
        others = [name for name in vips if name not in here]
        if not others:
            return
        if len(others) > 15:
            ctx.reply(
                self.message(
                    "pab3_viplist_more", vips=", ".join(others[:15]), count=len(others) - 15
                )
            )
            return
        ctx.reply(self.message("pab3_viplist_other", vips=", ".join(others)))

    @command("vipadd", level=40, alias="vip")
    def cmd_vipadd(self, ctx: CommandContext) -> None:
        """vipadd <player> - give a player a reserved slot"""
        name = self._vip_name(ctx)
        if not name:
            return
        self.console.rcon_words(f'reservedSlotsList.add "{sanitize_rcon_value(name)}"')
        log.info("poweradminbf3: %s made %s a VIP", ctx.client.name, name)
        ctx.reply(self.message("pab3_vip_added", name=name))

    @command("vipremove", level=40, alias="viprm")
    def cmd_vipremove(self, ctx: CommandContext) -> None:
        """vipremove <player> - take a player's reserved slot away"""
        name = self._vip_name(ctx)
        if not name:
            return
        vips = {vip.lower(): vip for vip in self.vip_list()}
        listed = vips.get(name.lower())
        if listed is None:
            ctx.reply(self.message("pab3_vip_absent", name=name))
            return
        self.console.rcon_words(f'reservedSlotsList.remove "{sanitize_rcon_value(listed)}"')
        log.info("poweradminbf3: %s removed %s from the VIPs", ctx.client.name, listed)
        ctx.reply(self.message("pab3_vip_removed", name=listed))

    def _vip_name(self, ctx: CommandContext) -> str:
        """Whose slot this is about: a connected player, or a name typed for somebody who is not.

        Deliberately *not* `resolve_client`: the VIP list is names, and adding somebody who is not on
        the server right now is the ordinary case. A connected player who matches is preferred, so
        their name is spelled as the server spells it.
        """
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pab3_vip_usage", command=ctx.command.name))
            return ""
        found = self.console.find_clients(wanted)
        if len(found) > 1:
            listed = ", ".join(f"{c.name} [{c.cid}]" for c in found)
            ctx.reply(self.message("ambiguous_connected", count=len(found), candidates=listed))
            return ""
        if len(found) == 1:
            if not self.outranks(ctx.client, found[0]) and found[0] is not ctx.client:
                key = "action_denied_masked" if found[0].is_masked() else "action_denied_level"
                ctx.reply(self.message(key, name=found[0].name))
                return ""
            return found[0].name
        return wanted

    @command("vipclear", level=40)
    def cmd_vipclear(self, ctx: CommandContext) -> None:
        """vipclear - empty the VIP list"""
        self.console.rcon_words("reservedSlotsList.clear")
        log.warning("poweradminbf3: %s cleared the VIP list", ctx.client.name)
        ctx.reply(self.message("pab3_vip_cleared"))

    @command("vipload", level=60)
    def cmd_vipload(self, ctx: CommandContext) -> None:
        """vipload - re-read the VIP list from the server's disk"""
        self.console.rcon_words("reservedSlotsList.load")
        ctx.reply(self.message("pab3_vip_loaded", count=len(self.vip_list())))

    @command("vipsave", level=60)
    def cmd_vipsave(self, ctx: CommandContext) -> None:
        """vipsave - write the VIP list to the server's disk"""
        count = len(self.vip_list())
        self.console.rcon_words("reservedSlotsList.save")
        ctx.reply(self.message("pab3_vip_saved", count=count))

    def vip_list(self) -> list[str]:
        """Every name in the reserved slots list, paged out of the server.

        Read as **words**, not as text: a Battlefield player may be called `Sgt Pepper`, and once the
        reply is one string nothing can tell that from two players (`Console.rcon_words`).
        """
        names: list[str] = []
        while True:
            page = self.console.rcon_words(f"reservedSlotsList.list {len(names)}")
            fresh = [name for name in page if name and name not in names]
            if not fresh:
                return names
            names.extend(fresh)
            if len(names) > 500:  # a server answering every offset with the same page
                log.warning("poweradminbf3: the VIP list is still answering past 500 names")
                return names

    def connected_vips(self) -> list[str]:
        here = {client.name for client in self.console.clients.connected()}
        return [name for name in self.vip_list() if name in here]

    # -- preset server configs -----------------------------------------------

    @command("listconfig", level=60)
    def cmd_listconfig(self, ctx: CommandContext) -> None:
        """listconfig - the preset server configs this bot can send"""
        files = self._config_names(ctx)
        if files is None:
            return
        if not files:
            ctx.reply(self.message("pab3_cfg_none"))
            return
        ctx.reply(self.message("pab3_cfg_list", files=", ".join(sorted(files))))

    def _config_names(self, ctx: CommandContext) -> list[str] | None:
        directory = self.config_dir()
        if directory is None:
            ctx.reply(self.message("pab3_cfg_dir_unset"))
            return None
        if not directory.is_dir():
            ctx.reply(self.message("pab3_cfg_dir_missing"))
            return None
        return [item.stem for item in directory.iterdir() if item.suffix.lower() == ".cfg"]

    @command("loadconfig", level=60)
    def cmd_loadconfig(self, ctx: CommandContext) -> None:
        """loadconfig <name> - send a preset server config, a few lines at a time"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pab3_load_usage"))
            return
        if self._loading is not None:
            ctx.reply(self.message("pab3_load_busy", name=self._loading.name))
            return
        names = self._config_names(ctx)
        if names is None:
            return
        # Matched the way every other name in this bot is matched. The classic had its own ladder
        # here — exact, then substring, then a soundex hash ordered by Levenshtein distance — which
        # is the same idea arrived at before `match_names` existed.
        found = match_names(wanted, [(name, name) for name in names])
        if not found:
            ctx.reply(self.message("pab3_load_unknown", name=wanted))
            return
        if len(found) > 1:
            ctx.reply(self.message("pab3_load_which", names=", ".join(found[:5])))
            return
        directory = self.config_dir()
        if directory is None:
            return
        try:
            lines = _config_lines(directory / f"{found[0]}.cfg")
        except OSError as exc:
            ctx.reply(
                self.message("pab3_load_failed", name=found[0], error=exc.strerror or "unread")
            )
            return
        self._loading = Loading(name=found[0], lines=deque(lines), client=ctx.client)
        log.info("poweradminbf3: %s is sending %s", ctx.client.name, found[0])
        ctx.reply(self.message("pab3_load_started", name=found[0], lines=len(lines)))

    # -- the one scheduled pass ----------------------------------------------

    def _after(self, seconds: float, action: Callable[[], object]) -> None:
        self._delayed.append(Delayed(due=self.console.clock.now() + seconds, action=action))

    def _run_pending(self) -> None:
        """Everything this plugin owes on a deadline. Nothing sleeps and nothing is a thread."""
        now = self.console.clock.now()
        for delayed in list(self._delayed):
            if now >= delayed.due:
                self._delayed.remove(delayed)
                delayed.action()
        if self._next_balance and now >= self._next_balance:
            self._schedule_balance()
            if self.settings.get("autobalance") and self._round_finished and self._settled:
                self.balance()
        self._send_config_lines()

    def _send_config_lines(self) -> None:
        loading = self._loading
        if loading is None:
            return
        for _ in range(CONFIG_LINES_PER_PASS):
            if not loading.lines:
                break
            self.console.rcon_words(loading.lines.popleft())
        if loading.lines:
            return
        self._loading = None
        log.info("poweradminbf3: %s has been sent in full", loading.name)
        if loading.client is not None:
            self.console.tell(loading.client, self.message("pab3_load_done", name=loading.name))


def _config_lines(path: Path) -> list[str]:
    """The lines of a preset server config worth sending.

    A setting (`vars.something value`) or a rotation entry (`map gamemode rounds`). The classic
    matched exactly those two shapes and dropped every other line **in silence**, so a config with
    an `admin.password` line in it looked like it had been applied in full; anything unrecognised is
    logged here. A rotation entry is turned into the `mapList.add` it means, because a bare
    `MP_001 ConquestLarge0 2` is not a command.
    """
    lines: list[str] = []
    rotation: list[str] = []
    for number, raw in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith(("//", "#")):
            continue
        words = line.split()
        if words[0].startswith("vars."):
            lines.append(line)
            continue
        if len(words) == 3 and words[2].isdigit():
            rotation.append(f'mapList.add "{words[0]}" "{words[1]}" {words[2]}')
            continue
        log.warning(
            "poweradminbf3: %s line %d is neither a setting nor a map: %r", path, number, line
        )
    if rotation:
        # Cleared first, then rebuilt, then written to the server's own config so a restart keeps it.
        return [*lines, "mapList.clear", *rotation, "mapList.save"]
    return lines


__all__ = [
    "ANNOUNCE_SECONDS",
    "CONFIG_LINES_PER_PASS",
    "DEFAULTS",
    "GUNMASTER_PRESETS",
    "MESSAGES",
    "MIN_SCRAMBLE_PLAYERS",
    "NUKE_SECONDS",
    "SCRAMBLER_DEFAULTS",
    "UNLOCK_MODES",
    "Delayed",
    "Loading",
    "Poweradminbf3Plugin",
]
