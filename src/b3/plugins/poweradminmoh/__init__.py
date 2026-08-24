"""The 2010 Medal of Honor's admin commands — the teams, the scrambler and the reserved slots.

A port of the classic `poweradminmoh` plugin, and the twin of `poweradminbfbc2`: same engine
generation, same ready-up match mode, a team balancer with the same shape. What is here and not
there is the **scrambler** (`!scramble`, `!scramblemode`, `!autoscramble`), `!swap`, and the
**reserved spectator slots** this title keeps a list of. What is *not* here is any kind of yell: this
is the one Frostbite title with no `admin.yell`, which is why its match countdown is chat.

Five groups. **The teams** — `!teams` balances them now, `!teambalance` switches the automatic check
on and off, `!changeteam` and `!spect` move one player, and `!swap` exchanges two. **The scrambler**
— deals everybody out again at a round or a map boundary, by the previous round's score where the
server reported one. **The match** — `!match on` asks everybody to type `!ready`, waits for them,
counts down and restarts the round, with the plugins an operator lists switched off for the
duration. **The round and the map** — `!runnextround`, `!restartround`, `!setnextmap`. And **the
reserved slots** — `!reserveslot` and `!unreserveslot`.

Changed from the classic:

* **`admin.movePlayer` takes three arguments on this title, not four.** The classic's own changelog
  records that as "a major fix … this affected all team balancing features", which is exactly what a
  four-argument move on a three-argument verb costs: the server refuses every one, so `!changeteam`,
  `!swap`, `!spect`, the balancer and the scrambler all do nothing. The verb is the title's, on the
  profile, so there is one place for it to be right.
* **A config section the plugin looked for would take it down.** `loadMatchMode` reads
  `matchmode_configs` and assigns into `self._gameconfig` — an attribute that does not exist on the
  class. `AttributeError` is not in the tuple that `except` names, so an operator who wrote the
  section the code reads lost the whole plugin at load time. Nothing ever read `_gameconfig` either:
  the feature it was collecting for was never written. Gone rather than finished, because the plugin
  it was copied from (`poweradminurt`) has that feature and this one has no gametype commands to
  hang it on.
* **`!swap` with no arguments raised `TypeError`.** The guard was `if not input:` — the *builtin*
  `input`, which is always truthy — so the check for empty data never ran and the line below indexed
  `None`.
* **A swap with one spectator went ahead.** `A not in (1, 2) and B not in (1, 2)` refused only when
  *both* players were teamless, so with one of them spectating the other was moved to team 0. The
  same fault `poweradminbf3` had, in the same command.
* **The score scramble sorted the scores as text.** `sorted(scores, key=lambda x: x['score'])` over
  the strings a Frostbite block reports, so `"9"` outranked `"100"` and the strategy that exists to
  even out skill dealt almost at random. Numbers here.
* **The scrambler dealt out spectators and bots.** It scrambled `clients.getList()`, everybody the
  bot knows about, so anybody watching was put on a side.
* **A friendly answer that could never be given.** `!reserveslot` compared `err.message` — a string
  — to `['PlayerAlreadyInList']`, a list, so "already has access" was unreachable and the admin got
  the raw refusal. A refusal is a *reply* here rather than an exception (`Console.rcon_words`), and
  it is read as one.
* **`!setnextmap` of a map nobody has answered with the letters of what was typed.**
  `", ".join(data)` over a string joins its characters, so `!setnextmap nuketown` came back "did you
  mean n, u, k, e, t, o, w, n?".
* **`movedByBot` was set before the move and never cleared if it failed.** This is the only plugin in
  the classic tree that reads that variable, and a move the server refused left it set — so the
  player's next *genuine* team change was ignored, and their team-join time was never updated. Set
  after the server has taken the move here.
* **Nothing sleeps.** The classic slept a second in `!runnextround` and `!restartround`, on the bot's
  one thread, and ran the match countdown on a chain of `threading.Timer`s.
* **`!scramblemode rubbish` meant random.** It read the first character only.
* **Match mode cost the server its team balancer permanently.** `!match on` set the balancer's own
  flag to off and `!match off` never set it back. It is suspended for the duration here — a match is
  played by teams the players have agreed between themselves — and comes back afterwards. Match mode
  also re-enabled *every* plugin on its list when it ended, so a plugin the operator had deliberately
  switched off came back on because a match had happened.
* **One immunity rule.** The classic level-checked in the balancer only (`maxlevel`), so any
  moderator could `!kill`, `!spect` or `!swap` a senior admin. The `poweradminbf3` rule applies: you
  may not act on your equals or your betters, unless you are at or above `no_level_check_level`.
* **The scrambler is not shared between servers.** It was a *class* attribute, so every instance of
  the plugin used one object whose `_plugin` pointed at whichever was built last.

**Not ported.** `!pb_sv_command` is `!punkbuster` in the admin plugin, where every PunkBuster family
can reach it.

Two things below the plugin came out of writing it. This title has **no centre-screen message** — the
classic's `moh.py` overrides `saybig` to call `say`, which is a parser author saying "there is no
`admin.yell` here" — so its profile carries no `saybig_template` and `say_big` falls back to chat
rather than being answered `UnknownCommand`. And **team 3 is the spectators** on this title: its own
parser says so, this plugin sends a player there for `!spect`, and it has no four-sided gamemode for
teams 3 and 4 to be playing sides in.

`!kill`, `!teams`, `!swap`, `!changeteam`, `!scramble` and `!setnextmap` are command or alias names
`poweradminurt` and `poweradminbf3` also use. Neither of those is restricted to this title (
`poweradminurt` asks for verbs instead), so an operator who loads two of them on one server will find
the second one **refused** the clashing names, with both plugins named in the log.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.core.matchmode import ReadyUp, Step
from b3.core.plugin import Plugin
from b3.core.util import as_int, as_word, match_names, sanitize_rcon_value
from b3.domain.client import Client
from b3.domain.permissions import level_for

log = logging.getLogger(__name__)

#: How long the bot waits after announcing something before doing it. The classic slept exactly this
#: long, on the bot's own thread.
ANNOUNCE_SECONDS = 1.0

#: How long the automatic team check stands down for after the bot starts, a round starts, somebody
#: leaves, or the bot itself moves a player. The classic's own two figures: a minute for a round
#: start, ten seconds after a disconnection, because a player leaving unbalances the teams without
#: anybody having done anything wrong.
QUIET_SECONDS = 60.0
QUIET_AFTER_MOVE = 10.0

#: A scramble needs somebody to scramble. Two players are already one each.
MIN_SCRAMBLE_PLAYERS = 3

#: What this title calls its list of reserved spectator slots. Four verbs, and the list has to be
#: read from and written back to the server's disk around any change, which is why every command
#: here sends three.
SLOTS_LOAD = "reservedSpectateSlots.load"
SLOTS_SAVE = "reservedSpectateSlots.save"
SLOTS_ADD = "reservedSpectateSlots.addPlayer"
SLOTS_REMOVE = "reservedSpectateSlots.removePlayer"

#: The server's own words for "that name is already in the list" and "it was never there". Read as
#: replies rather than caught as exceptions — see `Console.rcon_words`.
ALREADY_LISTED = "playeralreadyinlist"
NOT_LISTED = "playernotinlist"

DEFAULTS: dict[str, object] = {
    # Move players between teams when the sides drift apart. Off as the classic shipped it.
    "teambalance": False,
    # Minutes between automatic checks; 0 turns the timer off and leaves `!teams` and the check on a
    # team change. The classic capped this at 59 because it wrote the number into a crontab minute
    # field, and the cap is kept: an hour between checks is not a balancer.
    "check_interval": 1,
    # How many players one side may be ahead by before anybody is moved. Floored at 1 — 0 would move
    # every arrival straight back again — and capped at 9, both the classic's.
    "max_difference": 1,
    # Players at or above this level are never moved by the balancer. The classic's `maxlevel`, at
    # its own default.
    "no_balance_level": 40,
    # An admin at or above this level is not level-checked when acting on another player.
    "no_level_check_level": 60,
    # Plugins switched off while a match is on, and back on when it ends. The classic's own list.
    "match_plugins": "spree, tk, pingwatch, censor, spamcontrol",
}

SCRAMBLER_DEFAULTS: dict[str, object] = {
    # off | round | map — when the scrambler runs by itself.
    "mode": "off",
    # random | score — how it deals the teams out.
    "strategy": "random",
}

MESSAGES = {
    "pamoh_balanced": "the teams are even: {first} v {second}",
    "pamoh_balancing": "balancing the teams",
    "pamoh_moving": "moving {name} to the other team",
    "pamoh_unbalanced_you": "please do not make the teams uneven",
    "pamoh_balance_state": "the automatic team check is {state}",
    "pamoh_toggle_usage": "!{command} on|off",
    "pamoh_changeteam_usage": "!changeteam <player>",
    "pamoh_changeteam": "{name} moved to the other team",
    "pamoh_no_team": "the server has not said which team {name} is on",
    "pamoh_spect_usage": "!spect <player>",
    "pamoh_spectated": "{name} moved to the spectators",
    "pamoh_swap_usage": "!swap <player> [<player>]",
    "pamoh_swap_same": "{first} and {second} are on the same team already",
    "pamoh_swapped": "swapped {first} with {second}",
    "pamoh_kill_usage": "!kill <player> [<reason>]",
    "pamoh_killed": "{name} was killed by an admin",
    "pamoh_killed_reason": "{name} was killed by an admin: {reason}",
    "pamoh_unsupported": "this title has no verb for that",
    "pamoh_round_next": "moving to the next round",
    "pamoh_round_restart": "restarting the round",
    "pamoh_nextmap_usage": "!setnextmap <map>",
    "pamoh_nextmap_unknown": "no map like {map} — try {maps}",
    "pamoh_nextmap": "the next map will be {map}",
    "pamoh_scramble_on": "the teams will be scrambled when this round ends",
    "pamoh_scramble_off": "the scramble is cancelled",
    "pamoh_scramble_mode_usage": "!scramblemode random|score",
    "pamoh_scramble_mode": "the scrambler will deal by {mode}",
    "pamoh_autoscramble_usage": "!autoscramble off|round|map",
    "pamoh_autoscramble": "the scrambler runs {when}",
    "pamoh_autoscramble_off": "the scrambler only runs when asked",
    "pamoh_scramble_too_few": "too few players to scramble",
    "pamoh_scrambling": "scrambling the teams",
    "pamoh_slot_usage": "!{command} <player>",
    "pamoh_slot_added": "{name} may use a reserved slot now",
    "pamoh_slot_listed": "{name} already has a reserved slot",
    "pamoh_slot_removed": "{name} may no longer use a reserved slot",
    "pamoh_slot_absent": "{name} had no reserved slot",
    "pamoh_slot_refused": "the server would not change the reserved slots: {reason}",
    "pamoh_slot_you_added": "you may use the reserved slots now",
    "pamoh_slot_you_removed": "you may no longer use the reserved slots",
    "pamoh_match_usage": "!match on|off",
    "pamoh_match_on": "match mode is on",
    "pamoh_match_off": "match mode is off",
    "pamoh_match_plugins_off": "switched off for the match: {plugins}",
    "pamoh_match_plugins_on": "switched back on: {plugins}",
    "pamoh_match_ready": "everybody type !ready when you are",
    "pamoh_match_waiting_you": "we are waiting for you — type !ready",
    "pamoh_match_waiting": "waiting for {players}",
    "pamoh_match_all_ready": "everybody is ready — counting down",
    "pamoh_match_count": "match starting in {count}",
    "pamoh_match_start": "fight!",
    "pamoh_ready_none": "no match is being started, so there is nothing to be ready for",
    "pamoh_ready_late": "the countdown has started; you cannot change your mind now",
    "pamoh_ready_yes": "you are ready",
    "pamoh_ready_no": "you are not ready any more",
}

#: How many players the classic would name when saying who a match was waiting for.
NAMES_IN_A_LINE = 6


@dataclass(slots=True)
class Delayed:
    """Something announced now and done a few seconds later, on the scheduled pass."""

    due: float
    action: Callable[[], object]


class PoweradminmohPlugin(Plugin):
    """The 2010 Medal of Honor's teams, its scrambler, its match mode and its reserved slots."""

    requires_plugins = ("admin",)
    requires_parsers = ("moh",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.scrambler: dict[str, object] = dict(SCRAMBLER_DEFAULTS)
        self.match_mode = False
        #: The plugins *this* switched off, so that only those are switched back on.
        self._disabled_for_match: list[str] = []
        #: The ready-up, shared with the other titles that have one (b3.core.matchmode).
        self.ready_up = ReadyUp(console.clock)
        #: An instance attribute, unlike the classic's class-level `Scrambler()`.
        self.scramble_planned = False
        #: The last round's scoreboard, which is the only thing that makes a score scramble possible.
        self._last_scores: list[PlayerInfo] = []
        self._delayed: list[Delayed] = []
        self._quiet_until = 0.0
        self._next_balance = 0.0

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.scrambler = {**SCRAMBLER_DEFAULTS, **(config.get("scrambler") or {})}
        # The classic's floor and ceiling, both of which matter: 0 moves every arrival straight back,
        # and the interval went into a crontab minute field so it could not exceed 59 anyway.
        self.settings["max_difference"] = min(9, max(1, as_int(self.settings["max_difference"], 1)))
        self.settings["check_interval"] = min(
            59, max(0, as_int(self.settings["check_interval"], 1))
        )
        # `mode: off` in YAML is the boolean False, not the word — the trap that costs a word-valued
        # setting its whole vocabulary.
        self.scrambler["mode"] = as_word(self.scrambler.get("mode"), "off").lower()
        self.scrambler["strategy"] = as_word(self.scrambler.get("strategy"), "random").lower()
        if self.scrambler["mode"] not in ("off", "round", "map"):
            log.warning(
                "poweradminmoh: scrambler mode %r is not off, round or map; leaving it off",
                self.scrambler["mode"],
            )
            self.scrambler["mode"] = "off"
        if self.scrambler["strategy"] not in ("random", "score"):
            log.warning(
                "poweradminmoh: scrambler strategy %r is not random or score; using random",
                self.scrambler["strategy"],
            )
            self.scrambler["strategy"] = "random"

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.schedule(self._run_pending, second="*", name="PoweradminmohPlugin.pending")
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_team_change)
        self.subscribe(EventType.GAME_ROUND_START, self.on_round_start)
        self.subscribe(EventType.GAME_ROUND_PLAYER_SCORES, self.on_scores)
        self.hold_off()
        self._schedule_balance()

    def on_disable(self) -> None:
        """A disabled plugin has no business counting a match down that nobody can see."""
        self.ready_up.cancel()
        self._delayed.clear()

    def _level(self, key: str, default: int) -> int:
        level = level_for(self.settings.get(key))
        if level is None:
            log.warning(
                "poweradminmoh: %s is %r, which is neither a group keyword nor a level 0-100; "
                "using %d",
                key,
                self.settings.get(key),
                default,
            )
            return default
        return level

    # -- the quiet window ----------------------------------------------------

    def hold_off(self, seconds: float = QUIET_SECONDS) -> None:
        """Stand the automatic team check down for a while — the classic's `_ignoreBalancingTill`."""
        self._quiet_until = max(self._quiet_until, self.console.clock.now() + seconds)

    def holding_off(self) -> bool:
        """Whether the check is standing down. A match counts: the teams are agreed."""
        return self.match_mode or self.console.clock.now() < self._quiet_until

    # -- events --------------------------------------------------------------

    def on_auth(self, event: Event) -> None:
        if event.client is not None:
            self._stamp(event.client)

    def on_disconnect(self, _event: Event) -> None:
        # Somebody leaving unbalances the teams without anybody having done anything wrong, and the
        # slot is often filled within seconds. The classic's ten.
        self.hold_off(QUIET_AFTER_MOVE)

    def on_scores(self, event: Event) -> None:
        """The final scoreboard, which is what makes a score scramble possible at all."""
        rows = event.data
        if isinstance(rows, list):
            self._last_scores = [row for row in rows if isinstance(row, PlayerInfo)]

    def on_round_start(self, _event: Event) -> None:
        """A scramble happens here, and nothing is balanced for the first minute."""
        first_of_map = self.console.game.rounds in (0, 1)
        self.hold_off()
        self._schedule_balance()
        if self.scramble_planned:
            self.scramble_planned = False
            self.scramble()
            return
        if self.match_mode:
            return
        mode = self.scrambler["mode"]
        if mode == "round" or (mode == "map" and first_of_map):
            self.scramble()

    def on_team_change(self, event: Event) -> None:
        """Somebody changed team, which is when the classic checked whether they should have."""
        client = event.client
        if client is None:
            return
        moved_by_us = client.get_var(self, "moved_by_bot", False)
        client.del_var(self, "moved_by_bot")
        if moved_by_us:
            # The bot moved them, so their team time is the bot's business and not a fresh choice.
            return
        self._stamp(client)
        if not self.settings.get("teambalance") or self.holding_off():
            return
        if not self._may_be_moved(client):
            return
        counts = self.team_counts()
        theirs = self.console.team_id(client.team or "")
        if not theirs or theirs == self.console.team_id("spec") or len(counts) < 2:
            return
        smaller = min(counts, key=lambda key: counts[key])
        if theirs == smaller or counts[theirs] - counts[smaller] <= self._difference():
            return
        self.console.tell(client, self.message("pamoh_unbalanced_you"))
        self.move(client, smaller)

    def _stamp(self, client: Client) -> None:
        """Remember when this player joined the team they are on; the balancer sorts on it."""
        client.set_var(self, "teamtime", self.console.clock.now())

    # -- the teams -----------------------------------------------------------

    def team_counts(self) -> dict[str, int]:
        """How many players are on each playing team, keyed by the engine's team id."""
        spectators = self.console.team_id("spec")
        counts: dict[str, int] = {}
        for client in self.console.clients.connected():
            engine_team = self.console.team_id(client.team or "")
            if engine_team and engine_team != spectators:
                counts[engine_team] = counts.get(engine_team, 0) + 1
        return counts

    def _difference(self) -> int:
        return min(9, max(1, as_int(self.settings.get("max_difference"), 1)))

    def _may_be_moved(self, client: Client) -> bool:
        if client.is_bot:
            return False
        return client.max_level() < self._level("no_balance_level", 40)

    def balance(self) -> bool:
        """Move the most recent joiners off the bigger team. True if anybody moved."""
        counts = self.team_counts()
        if len(counts) < 2:
            return False
        bigger = max(counts, key=lambda key: counts[key])
        smaller = min(counts, key=lambda key: counts[key])
        gap = counts[bigger] - counts[smaller]
        if gap <= self._difference():
            return False
        movable = [
            client
            for client in self.console.clients.connected()
            if self.console.team_id(client.team or "") == bigger and self._may_be_moved(client)
        ]
        # Most recent team join first — the classic's own fix, made here and never carried over to
        # `poweradminbfbc2`, which still moved whoever had been on that team longest.
        movable.sort(key=lambda c: c.get_var(self, "teamtime", 0.0), reverse=True)
        moving = movable[: gap // 2]
        if not moving:
            return False
        self.console.say(self.message("pamoh_balancing"))
        for client in moving:
            if self.move(client, smaller):
                self.console.say(self.message("pamoh_moving", name=client.name))
        return True

    def move(self, client: Client, team: str) -> bool:
        """Put a player on a team. False where the server would not take it.

        `moved_by_bot` is set **after** the server has taken the move, not before: the classic set it
        first and left it set when the move failed, so the player's next genuine team change was
        ignored and their team-join time never updated.
        """
        if not self.console.apply_verb("move", client, team=team, squad="0"):
            return False
        client.set_var(self, "moved_by_bot", True)
        client.team = self._team_name(team)
        self._stamp(client)
        self.hold_off(QUIET_AFTER_MOVE)
        return True

    def _team_name(self, engine_team: str) -> str:
        for name in ("red", "blue", "spec"):
            if self.console.team_id(name) == engine_team:
                return name
        return engine_team

    def other_team(self, client: Client) -> str:
        """The team a `!changeteam` would put this player on, in the engine's spelling."""
        current = self.console.team_id(client.team or "")
        if current not in ("1", "2"):
            return ""
        return "2" if current == "1" else "1"

    @command("teams", level=20)
    def cmd_teams(self, ctx: CommandContext) -> None:
        """teams - even the teams up now"""
        counts = self.team_counts()
        if not self.balance():
            first, second = (sorted(counts.values(), reverse=True) + [0, 0])[:2]
            ctx.reply(self.message("pamoh_balanced", first=first, second=second))

    @command("teambalance", level=20)
    def cmd_teambalance(self, ctx: CommandContext) -> None:
        """teambalance <on|off> - move players across when the sides drift apart"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pamoh_toggle_usage", command=ctx.command.name))
            return
        self.settings["teambalance"] = wanted == "on"
        self._schedule_balance()
        ctx.reply(self.message("pamoh_balance_state", state=wanted))

    @command("changeteam", level=20)
    def cmd_changeteam(self, ctx: CommandContext) -> None:
        """changeteam <player> - move a player to the other team"""
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pamoh_changeteam_usage"))
            return
        target = self.resolve_client(ctx, wanted[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        other = self.other_team(target)
        if not other:
            ctx.reply(self.message("pamoh_no_team", name=target.name))
            return
        if not self.move(target, other):
            ctx.reply(self.message("pamoh_unsupported"))
            return
        ctx.reply(self.message("pamoh_changeteam", name=target.name))

    @command("spect", level=20)
    def cmd_spect(self, ctx: CommandContext) -> None:
        """spect <player> - move a player to the spectators"""
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pamoh_spect_usage"))
            return
        target = self.resolve_client(ctx, wanted[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        spectators = self.console.team_id("spec")
        if not spectators or not self.move(target, spectators):
            ctx.reply(self.message("pamoh_unsupported"))
            return
        ctx.reply(self.message("pamoh_spectated", name=target.name))

    @command("swap", level=20)
    def cmd_swap(self, ctx: CommandContext) -> None:
        """swap <player> [<player>] - exchange two players' teams"""
        parts = ctx.args.split()
        # `if not input:` in the classic — the *builtin*, always truthy — so this check never ran and
        # the line below it indexed `None`.
        if not parts:
            ctx.reply(self.message("pamoh_swap_usage"))
            return
        first = self.resolve_client(ctx, parts[0])
        if first is None:
            return
        second = ctx.client if len(parts) < 2 else self.resolve_client(ctx, parts[1])
        if second is None:
            return
        if not self.may_act_on(ctx, first) or (
            second is not ctx.client and not self.may_act_on(ctx, second)
        ):
            return
        team_a = self.console.team_id(first.team or "")
        team_b = self.console.team_id(second.team or "")
        # `and` in the classic, so a swap with one player spectating went ahead and moved the other
        # to team 0.
        if team_a not in ("1", "2") or team_b not in ("1", "2"):
            ctx.reply(
                self.message(
                    "pamoh_no_team", name=first.name if team_a not in ("1", "2") else second.name
                )
            )
            return
        if team_a == team_b:
            ctx.reply(self.message("pamoh_swap_same", first=first.name, second=second.name))
            return
        self.move(first, team_b)
        self.move(second, team_a)
        ctx.reply(self.message("pamoh_swapped", first=first.name, second=second.name))

    def _schedule_balance(self) -> None:
        """When the next automatic pass falls due. The classic's crontab, as a deadline."""
        minutes = as_int(self.settings.get("check_interval"), 1)
        if not self.settings.get("teambalance") or minutes <= 0:
            self._next_balance = 0.0
            return
        self._next_balance = self.console.clock.now() + minutes * 60

    # -- who may act on whom -------------------------------------------------

    def outranks(self, admin: Client, target: Client) -> bool:
        """Whether ``admin`` may act on ``target``, without saying anything about it."""
        if target is admin or (target.id is not None and target.id == admin.id):
            return False
        if admin.max_level() >= self._level("no_level_check_level", 60):
            return True
        return target.max_level() < admin.max_level()

    def may_act_on(self, ctx: CommandContext, target: Client) -> bool:
        """The same rule, explaining itself. The classic checked a level in the balancer only."""
        if self.outranks(ctx.client, target):
            return True
        if target is ctx.client or (target.id is not None and target.id == ctx.client.id):
            ctx.reply(self.message("action_denied_self"))
            return False
        key = "action_denied_masked" if target.is_masked() else "action_denied_level"
        ctx.reply(self.message(key, name=target.name))
        return False

    # -- players -------------------------------------------------------------

    @command("kill", level=40)
    def cmd_kill(self, ctx: CommandContext) -> None:
        """kill <player> [<reason>] - kill a player without touching the scoreboard"""
        parts = ctx.args.split(None, 1)
        if not parts:
            ctx.reply(self.message("pamoh_kill_usage"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        if not self.console.apply_verb("kill", target):
            ctx.reply(self.message("pamoh_unsupported"))
            return
        reason = parts[1].strip() if len(parts) > 1 else ""
        key = "pamoh_killed_reason" if reason else "pamoh_killed"
        self.console.say(self.message(key, name=target.name, reason=reason))

    # -- the round and the map -----------------------------------------------

    @command("runnextround", level=40, alias="nextrnd")
    def cmd_runnextround(self, ctx: CommandContext) -> None:
        """runnextround - move to the next round without finishing this one"""
        self._announce_then(ctx, "pamoh_round_next", "round_next")

    @command("restartround", level=40, alias="restartrnd")
    def cmd_restartround(self, ctx: CommandContext) -> None:
        """restartround - restart the round being played"""
        self._announce_then(ctx, "pamoh_round_restart", "round_restart")

    def _announce_then(self, ctx: CommandContext, key: str, verb: str) -> None:
        """Say what is about to happen, then do it a moment later — never by sleeping."""
        if not self.console.supports_server_verb(verb):
            ctx.reply(self.message("pamoh_unsupported"))
            return
        self.console.say(self.message(key))
        self._after(ANNOUNCE_SECONDS, lambda: self.console.apply_server_verb(verb))

    @command("setnextmap", level=20, alias="snmap")
    def cmd_setnextmap(self, ctx: CommandContext) -> None:
        """setnextmap <map> - the map to play after this one"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pamoh_nextmap_usage"))
            return
        maps = self.console.get_maps()
        chosen = wanted
        if maps:
            found = match_names(wanted, [(m, self.console.map_display(m)) for m in maps])
            if len(found) != 1:
                # `", ".join(data)` in the classic, over a *string* — so an unknown map was answered
                # with the letters of what the admin had typed, offered as maps.
                listed = ", ".join(self.console.map_display(m) for m in (found or maps)[:5])
                ctx.reply(self.message("pamoh_nextmap_unknown", map=wanted, maps=listed))
                return
            chosen = found[0]
        self.set_next_map(chosen, maps)
        ctx.reply(self.message("pamoh_nextmap", map=self.console.map_display(chosen)))

    def set_next_map(self, map_id: str, maps: list[str]) -> None:
        """Point the rotation at ``map_id`` for the next round.

        This generation holds a flat list of level names and the next map is an *index into it*, not
        a setting — which is why this is here rather than behind `Console.set_next_map`.
        """
        if map_id in maps:
            self.console.rcon_words(f"mapList.nextLevelIndex {maps.index(map_id)}")
            return
        index = self._next_index(len(maps))
        self.console.rcon_words(f'mapList.insert {index} "{sanitize_rcon_value(map_id)}"')
        self.console.rcon_words(f"mapList.nextLevelIndex {index}")

    def _next_index(self, rotation_length: int) -> int:
        reply = self.console.rcon_words("mapList.nextLevelIndex")
        if reply and reply[0].lstrip("-").isdigit() and int(reply[0]) >= 0:
            return int(reply[0])
        return rotation_length

    # -- the scrambler -------------------------------------------------------

    @command("scramble", level=20)
    def cmd_scramble(self, ctx: CommandContext) -> None:
        """scramble - scramble the teams when this round ends, or cancel that"""
        self.scramble_planned = not self.scramble_planned
        key = "pamoh_scramble_on" if self.scramble_planned else "pamoh_scramble_off"
        ctx.reply(self.message(key))

    @command("scramblemode", level=20)
    def cmd_scramblemode(self, ctx: CommandContext) -> None:
        """scramblemode <random|score> - how the scrambler deals the teams out"""
        wanted = ctx.args.strip().lower()
        # The classic matched on the first letter alone, so `!scramblemode rubbish` meant random.
        if wanted not in ("random", "score"):
            ctx.reply(self.message("pamoh_scramble_mode_usage"))
            return
        self.scrambler["strategy"] = wanted
        ctx.reply(self.message("pamoh_scramble_mode", mode=wanted))

    @command("autoscramble", level=20)
    def cmd_autoscramble(self, ctx: CommandContext) -> None:
        """autoscramble <off|round|map> - when the scrambler runs by itself"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("off", "round", "map"):
            ctx.reply(self.message("pamoh_autoscramble_usage"))
            return
        self.scrambler["mode"] = wanted
        if wanted == "off":
            ctx.reply(self.message("pamoh_autoscramble_off"))
            return
        ctx.reply(
            self.message(
                "pamoh_autoscramble",
                when="at every round start" if wanted == "round" else "at every map change",
            )
        )

    def scramble(self) -> bool:
        """Deal everybody out into the two teams. False when there is nobody to deal.

        By score where the last round's scoreboard says so, otherwise at random — and the scores are
        compared as **numbers**: the classic sorted the strings a Frostbite block reports, so `"9"`
        ranked above `"100"` and the strategy that exists to even out skill dealt almost at random.
        """
        players = self.scramble_order()
        if len(players) < MIN_SCRAMBLE_PLAYERS:
            log.info("poweradminmoh: %d player(s) is too few to scramble", len(players))
            return False
        self.console.say(self.message("pamoh_scrambling"))
        for index, client in enumerate(players):
            self.move(client, str((index % 2) + 1))
        return True

    def scramble_order(self) -> list[Client]:
        """The players to deal out, strongest first for a score scramble.

        Spectators and bots are left out. The classic dealt out `clients.getList()` — everybody the
        bot knew about — so anybody watching was put on a side.
        """
        spectators = self.console.team_id("spec")
        players = [
            client
            for client in self.console.clients.connected()
            if not client.is_bot and self.console.team_id(client.team or "") != spectators
        ]
        if self.scrambler["strategy"] != "score":
            random.shuffle(players)
            return players
        scores = {row.name: row.score for row in self._last_scores}
        if not any(scores.values()):
            log.info("poweradminmoh: no scores from the last round, so scrambling at random")
            random.shuffle(players)
            return players
        known = [c for c in players if c.name in scores]
        unknown = [c for c in players if c.name not in scores]
        random.shuffle(unknown)
        known.sort(key=lambda c: scores[c.name], reverse=True)
        return known + unknown

    # -- the reserved spectator slots ----------------------------------------

    @command("reserveslot", level=40, alias="rslot")
    def cmd_reserveslot(self, ctx: CommandContext) -> None:
        """reserveslot <player> - let a player use one of the reserved slots"""
        target = self._slot_target(ctx)
        if target is None:
            return
        refusal = self._slots_write(SLOTS_ADD, target.name)
        if refusal == ALREADY_LISTED:
            # The classic compared a *string* to `['PlayerAlreadyInList']`, so this answer could
            # never be given and the admin got the raw refusal instead.
            ctx.reply(self.message("pamoh_slot_listed", name=target.name))
            return
        if refusal:
            ctx.reply(self.message("pamoh_slot_refused", reason=refusal))
            return
        log.info("poweradminmoh: %s gave %s a reserved slot", ctx.client.name, target.name)
        ctx.reply(self.message("pamoh_slot_added", name=target.name))
        self.console.tell(target, self.message("pamoh_slot_you_added"))

    @command("unreserveslot", level=40, alias="uslot")
    def cmd_unreserveslot(self, ctx: CommandContext) -> None:
        """unreserveslot <player> - take a player's reserved slot away"""
        target = self._slot_target(ctx)
        if target is None:
            return
        refusal = self._slots_write(SLOTS_REMOVE, target.name)
        if refusal == NOT_LISTED:
            ctx.reply(self.message("pamoh_slot_absent", name=target.name))
            return
        if refusal:
            ctx.reply(self.message("pamoh_slot_refused", reason=refusal))
            return
        log.info("poweradminmoh: %s took %s's reserved slot away", ctx.client.name, target.name)
        ctx.reply(self.message("pamoh_slot_removed", name=target.name))
        self.console.tell(target, self.message("pamoh_slot_you_removed"))

    def _slot_target(self, ctx: CommandContext) -> Client | None:
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pamoh_slot_usage", command=ctx.command.name))
            return None
        return self.resolve_client(ctx, wanted[0])

    def _slots_write(self, verb: str, name: str) -> str:
        """Change the reserved-slots list and write it back. The server's refusal, or "".

        Three commands, as the classic sent: the list lives on the server's disk, so it is read
        before the change and written after it, or the change is lost at the next restart.
        """
        self.console.rcon_words(SLOTS_LOAD)
        reply = self.console.rcon_words(f'{verb} "{sanitize_rcon_value(name)}"')
        refusal = reply[0].strip().lower() if reply and reply[0].strip() else ""
        if refusal:
            return refusal
        self.console.rcon_words(SLOTS_SAVE)
        return ""

    # -- match mode ----------------------------------------------------------

    @command("match", level=20)
    def cmd_match(self, ctx: CommandContext) -> None:
        """match <on|off> - hold a match: everybody readies up, then the round restarts"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pamoh_match_usage"))
            return
        changed = self._set_match_mode(wanted == "on")
        if wanted == "on":
            self.console.say(self.message("pamoh_match_on"))
            if changed:
                ctx.reply(self.message("pamoh_match_plugins_off", plugins=", ".join(changed)))
            self.ready_up.begin()
            self.console.say(self.message("pamoh_match_ready"))
            return
        self.ready_up.cancel()
        self.console.say(self.message("pamoh_match_off"))
        if changed:
            ctx.reply(self.message("pamoh_match_plugins_on", plugins=", ".join(changed)))

    def _set_match_mode(self, on: bool) -> list[str]:
        """Turn match mode on or off. Returns the plugins whose state this changed.

        The team balancer is *suspended* rather than switched off — `holding_off()` is true while a
        match is on. The classic set the balancer's own setting to off and never set it back.
        """
        self.match_mode = on
        changed: list[str] = []
        if on:
            for name in self._match_plugins():
                plugin = self.console.get_plugin(name)
                # Only the ones actually running: the classic re-enabled every plugin on its list
                # when the match ended, so a plugin the operator had switched off came back on.
                if plugin is None or not getattr(plugin, "is_enabled", bool)():
                    continue
                plugin.disable()
                changed.append(name)
            self._disabled_for_match = list(changed)
            return changed
        for name in self._disabled_for_match:
            plugin = self.console.get_plugin(name)
            if plugin is None:
                continue
            plugin.enable()
            changed.append(name)
        self._disabled_for_match = []
        return changed

    def _match_plugins(self) -> list[str]:
        raw = self.settings.get("match_plugins")
        listed = raw if isinstance(raw, (list, tuple)) else re.split(r"[\s,]+", str(raw or ""))
        return [str(name).strip() for name in listed if str(name).strip()]

    @command("ready", level=0)
    def cmd_ready(self, ctx: CommandContext) -> None:
        """ready - say you are ready for the match to start

        Registered always, rather than created and deleted with the match: the classic reached into
        the admin plugin's command dictionary to add and remove it, and a `!match off` that raised
        anywhere before that left `!ready` behind for good.
        """
        if not self.ready_up.running():
            ctx.reply(self.message("pamoh_ready_none"))
            return
        state = self.ready_up.toggle(ctx.client)
        if state is None:
            ctx.reply(self.message("pamoh_ready_late"))
            return
        ctx.reply(self.message("pamoh_ready_yes" if state else "pamoh_ready_no"))

    def _run_ready_up(self) -> None:
        """One pass of the ready-up, which is where all of its announcing happens.

        Chat, not a yell: this title has no `admin.yell`, so `say_big` is a chat line here anyway and
        pretending otherwise would only make the code look wrong.
        """
        tick = self.ready_up.tick(self.console.clients.connected())
        if tick is None:
            return
        if tick.step is Step.WAITING:
            for client in tick.waiting:
                self.console.tell(client, self.message("pamoh_match_waiting_you"))
            if 0 < len(tick.waiting) <= NAMES_IN_A_LINE:
                named = ", ".join(client.name for client in tick.waiting)
                self.console.say(self.message("pamoh_match_waiting", players=named))
            return
        if tick.step is Step.READY:
            self.console.say(self.message("pamoh_match_all_ready"))
            return
        if tick.step is Step.COUNT:
            self.console.say(self.message("pamoh_match_count", count=tick.count))
            return
        self.console.say(self.message("pamoh_match_start"))
        self.console.apply_server_verb("round_restart")

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
            if not self.holding_off():
                self.balance()
        self._run_ready_up()


__all__ = [
    "ANNOUNCE_SECONDS",
    "DEFAULTS",
    "MESSAGES",
    "MIN_SCRAMBLE_PLAYERS",
    "QUIET_AFTER_MOVE",
    "QUIET_SECONDS",
    "SCRAMBLER_DEFAULTS",
    "Delayed",
    "PoweradminmohPlugin",
]
