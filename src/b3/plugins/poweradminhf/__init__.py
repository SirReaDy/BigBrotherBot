"""Homefront's admin commands — the teams, the match, and what this engine can do to a player.

A port of the classic `poweradminhf`. It sat with the three Frostbite `poweradmin*` plugins for years
and was filed with them in this project's own notes, but it is **Homefront**
(`requiresParsers = ['homefront']`) and the resemblance is a family one: the same author copied the
same match manager into it, without changing the commands it sends.

Four groups. **The teams** — `!pateams` balances them now, `!pateambalance` switches the automatic
check on and off, `!paautobalance` hands the job to the server's own balancer, and `!pachangeteam`
and `!paspectate` move one player. **Players** — `!pakill`. **The match** — `!pamatch on` asks
everybody to type `!ready`, waits for them, counts down and moves the server on, with the plugins an
operator lists switched off for the duration. And **the small ones** — `!payell`, `!paident`,
`!panextmap`.

Changed from the classic, and the first two are the same fault twice:

* **`!paautobalance off` sent the server the single word `false`.** The line was
  `write('admin SetAutoBalance %s' % 'true' if on else 'false')`, and `%` binds tighter than the
  conditional — so the whole thing reads `('admin SetAutoBalance %s' % 'true') if on else 'false'`.
  Turning the balancer *on* worked; turning it off sent one meaningless word, and the command said
  nothing either way, so an admin had no way to tell. (`poweradminbf3`'s dead scrambler was the same
  mistake with a different operator.)
* **`!pateambalance` with no argument reported the wrong state.** `"team balancing is %s" % 'on' if
  x else 'off'` binds the same way, so with the balancer off the player was sent the bare word
  `off` — no sentence at all.
* **The "you unbalanced the teams" warning could never fire.** It tested `client.name in
  biggestteam`, and `getTeams()` collects `c.cid` — which on this engine is the player's **Steam
  id**, since a 2011 change moved every rcon verb here to Steam ids and left this comparison behind.
  So the automatic check warned nobody and moved nobody, ever.
* **The match manager was a Battlefield one, copied whole.** Its nags and its countdown send
  `('admin.yell', …)` and its finish sends `('admin.restartMap',)` — **Frostbite** verbs, as
  *tuples*, to a Homefront parser whose `write` takes a string. So `!pamatch on` announced a match,
  registered `!ready`, and then every nag, the entire countdown and the restart at the end of it went
  nowhere. The ready-up here is `b3.core.matchmode`, shared with the two Frostbite plugins that
  carried the same forty lines, and it ends with the one verb this engine actually has:
  `admin nextmap`.
* **A vote ending before a vote had started raised.** `onClientVoteEnd` read
  `self._currentVote.data` with nothing checking that a vote had ever been seen, so a bot started
  while a vote was running crashed the handler. The whole vote protector is
  **`callvote`'s `protect:` section** now — it is a policy about votes, and that plugin already holds
  the running vote, the level table and the veto. On this title it does what this plugin did (warn
  the caller, lift a ban that passes); on an engine that can cancel a vote it stops it outright,
  which this one could not.
* **Nothing sleeps and nothing is a thread.** The classic ran the whole countdown on a chain of
  `threading.Timer`s. Announcements are a deadline on this plugin's one scheduled pass.
* **One immunity rule.** The classic checked nothing at all before killing, moving or spectating
  anybody, so any moderator could `!pakill` a senior admin. The `poweradminbf3` rule applies: you may
  not act on your equals or your betters, unless you are at or above `no_level_check_level`.
* **`!paident` answers privately.** It printed a player's Steam id through `sayLoudOrPM`, so
  `!!paident bob` put it in public chat. Its help said "the ip and guid" and it only ever showed the
  id, which is honest for this engine: Homefront reports no addresses at all.
* **Match mode does not cost the server its balancer.** `!pamatch on` set the automatic check's own
  flag to off and `!pamatch off` never set it back. It is suspended for the duration here and comes
  back afterwards. Match mode also re-enabled *every* plugin on its list when it ended, so a plugin
  the operator had deliberately switched off came back on because a match had happened.
* **A group keyword that is not one is reported.** `voteprotector.auto_unban_level` went through
  `getGroupLevel`, which raises on an unknown keyword — outside the two exceptions the `try` named,
  so a typo took the plugin down at load. Levels are read with `b3.domain.permissions.level_for`
  here, as everywhere else, and that setting now lives on `callvote`.

**Not ported.** `!paversion` printed the plugin's own version string and author; there is no
per-plugin version here, and `!plugin info` answers the question.

What this engine can do to a player is on its profile now (`kill`, `switch_team`, `spectate`, and the
server's `autobalance` and `cyclemap`), which is what let this plugin be written without a single
Homefront command inside it. One of them is worth knowing about: **`switch_team` takes no team.**
`admin forceteamswitch` puts a player on the other side and that is all it can be asked, so the
balancer here moves players *off* the bigger team rather than *onto* the smaller one — the same thing
on a two-sided game, and the only thing this engine offers.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.matchmode import ReadyUp, Step
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.client import Client
from b3.domain.permissions import level_for

log = logging.getLogger(__name__)

#: How long the bot waits after announcing something before doing it, so the players reading the
#: announcement get a moment.
ANNOUNCE_SECONDS = 1.0

#: How long the automatic team check stands down after the bot starts, a round starts, or the bot
#: moves somebody itself. The classic's minute: a round start is a flurry of team changes, and reading
#: them one at a time means moving half the server back again.
QUIET_SECONDS = 60.0
QUIET_AFTER_MOVE = 10.0

DEFAULTS: dict[str, object] = {
    # Warn whoever makes the teams uneven and move them back. Off as the classic shipped it.
    "teambalance": False,
    # Players at or above this level are never moved by the automatic check. The classic had no such
    # exemption, so it moved the admin who had just joined to sort the teams out.
    "no_balance_level": 20,
    # An admin at or above this level is not level-checked when acting on another player. The classic
    # checked nothing anywhere in this plugin.
    "no_level_check_level": 60,
    # How many players one side may be ahead by before anybody is moved. The classic's figure, and
    # its floor: 1 means every arrival is moved straight back again.
    "max_difference": 1,
    # Plugins switched off while a match is on, and back on when it ends. The classic's own list.
    "match_plugins": "spree, tk, pingwatch",
}

MESSAGES = {
    "pahf_balanced": "the teams are even: {first} v {second}",
    "pahf_balancing": "balancing the teams",
    "pahf_moving": "moving {name} to the other team",
    "pahf_unbalanced_you": "please do not make the teams uneven",
    "pahf_balance_state": "the automatic team check is {state}",
    "pahf_autobalance": "the server's own team balancer is {state}",
    "pahf_toggle_usage": "!{command} on|off",
    "pahf_changeteam_usage": "!pachangeteam <player>",
    "pahf_changeteam": "{name} moved to the other team",
    "pahf_spectate_usage": "!paspectate <player>",
    "pahf_spectated": "{name} moved to the spectators",
    "pahf_kill_usage": "!pakill <player> [<reason>]",
    "pahf_killed": "{name} was killed by an admin",
    "pahf_killed_reason": "{name} was killed by an admin: {reason}",
    "pahf_unsupported": "this title has no verb for that",
    "pahf_nextmap": "moving on to the next map",
    "pahf_yell_usage": "!payell <message>",
    "pahf_ident": "{name}: {guid} (this game reports no addresses)",
    "pahf_match_usage": "!pamatch on|off",
    "pahf_match_on": "match mode is on",
    "pahf_match_off": "match mode is off",
    "pahf_match_plugins_off": "switched off for the match: {plugins}",
    "pahf_match_plugins_on": "switched back on: {plugins}",
    "pahf_match_ready": "everybody type !ready when you are",
    "pahf_match_waiting_you": "we are waiting for you - type !ready",
    "pahf_match_waiting": "waiting for {players}",
    "pahf_match_all_ready": "everybody is ready - counting down",
    "pahf_match_count": "match starting in {count}",
    "pahf_match_start": "fight!",
    "pahf_ready_none": "no match is being started, so there is nothing to be ready for",
    "pahf_ready_late": "the countdown has started; you cannot change your mind now",
    "pahf_ready_yes": "you are ready",
    "pahf_ready_no": "you are not ready any more",
}

#: How many players the classic would name when saying who a match was waiting for.
NAMES_IN_A_LINE = 6


@dataclass(slots=True)
class Delayed:
    """Something announced now and done a few seconds later, on the scheduled pass."""

    due: float
    action: Callable[[], object]


class PoweradminhfPlugin(Plugin):
    """Homefront's teams, its match mode, and the three things it can do to a player."""

    requires_plugins = ("admin",)
    requires_parsers = ("homefront",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.match_mode = False
        #: The plugins *this* switched off, so that only those are switched back on.
        self._disabled_for_match: list[str] = []
        #: The ready-up, shared with the Frostbite plugins that carried the same forty lines.
        self.ready_up = ReadyUp(console.clock)
        self._delayed: list[Delayed] = []
        self._quiet_until = 0.0

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.settings["max_difference"] = max(1, as_int(self.settings.get("max_difference"), 1))

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.schedule(self._run_pending, second="*", name="PoweradminhfPlugin.pending")
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_team_change)
        self.subscribe(EventType.GAME_ROUND_START, self.on_round_start)
        self.hold_off()

    def on_disable(self) -> None:
        """A disabled plugin has no business counting a match down that nobody can see."""
        self.ready_up.cancel()
        self._delayed.clear()

    def _level(self, key: str, default: int) -> int:
        """A level setting, read the way every other plugin reads one.

        The classic put `voteprotector.auto_unban_level` through `getGroupLevel`, which raises on a
        keyword it does not know — from outside the two exceptions its `try` named, so a typo took
        the whole plugin down at load.
        """
        level = level_for(self.settings.get(key))
        if level is None:
            log.warning(
                "poweradminhf: %s is %r, which is neither a group keyword nor a level 0-100; "
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

    def on_round_start(self, _event: Event) -> None:
        self.hold_off()

    def on_team_change(self, event: Event) -> None:
        """Somebody changed team, which is the only moment the classic ever checked the teams.

        The check itself never fired there: it asked whether the player's *name* was in a list of
        Steam ids.
        """
        client = event.client
        if client is None:
            return
        moved_by_us = client.get_var(self, "moved_by_bot", False)
        client.del_var(self, "moved_by_bot")
        self._stamp(client)
        if moved_by_us or not self.settings.get("teambalance") or self.holding_off():
            return
        if not self._may_be_moved(client):
            return
        counts = self.team_counts()
        theirs = self.side_of(client)
        if not theirs or len(counts) < 2:
            return
        smaller = min(counts, key=lambda key: counts[key])
        if theirs == smaller or counts[theirs] - counts[smaller] <= self._difference():
            return
        self.console.tell(client, self.message("pahf_unbalanced_you"))
        self.switch(client)

    def _stamp(self, client: Client) -> None:
        """Remember when this player joined the team they are on; the balancer sorts on it."""
        client.set_var(self, "teamtime", self.console.clock.now())

    # -- the teams -----------------------------------------------------------

    @staticmethod
    def side_of(client: Client) -> str:
        """Which playing side this player is on, or "" for the spectators and the undecided."""
        side = (client.team or "").strip().lower()
        return side if side in ("red", "blue") else ""

    def team_counts(self) -> dict[str, int]:
        """How many players are on each playing side.

        Counted from the bot's own client list, and by *team* rather than by handle: the classic
        collected Steam ids and then compared a name against them.
        """
        counts: dict[str, int] = {}
        for client in self.console.clients.connected():
            side = self.side_of(client)
            if side:
                counts[side] = counts.get(side, 0) + 1
        return counts

    def _difference(self) -> int:
        return max(1, as_int(self.settings.get("max_difference"), 1))

    def _may_be_moved(self, client: Client) -> bool:
        if client.is_bot:
            return False
        return client.max_level() < self._level("no_balance_level", 20)

    def switch(self, client: Client) -> bool:
        """Put a player on the other side. False where the server would not take it.

        `admin forceteamswitch` takes **no team**: it swaps whoever is named. So this is "move them
        off the side they are on", which on a two-sided game is the same thing as moving them to the
        other one, and is the only thing this engine offers.
        """
        if not self.console.apply_verb("switch_team", client):
            return False
        client.set_var(self, "moved_by_bot", True)
        client.team = "blue" if self.side_of(client) == "red" else "red"
        self._stamp(client)
        self.hold_off(QUIET_AFTER_MOVE)
        return True

    def balance(self) -> bool:
        """Move the most recent joiners off the bigger side. True if anybody moved.

        Announced only once somebody is actually going to move — and the classic's own choice of who:
        most recent team join first, which is the fairest answer available, since a player who has
        been there all round did not cause the imbalance.
        """
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
            if self.side_of(client) == bigger and self._may_be_moved(client)
        ]
        movable.sort(key=lambda c: c.get_var(self, "teamtime", 0.0), reverse=True)
        moving = movable[: gap // 2]
        if not moving:
            return False
        self.console.say(self.message("pahf_balancing"))
        for client in moving:
            if self.switch(client):
                self.console.say(self.message("pahf_moving", name=client.name))
        return True

    @command("pateams", level=20, alias="teams")
    def cmd_pateams(self, ctx: CommandContext) -> None:
        """pateams - even the teams up now"""
        counts = self.team_counts()
        if not self.balance():
            first, second = (sorted(counts.values(), reverse=True) + [0, 0])[:2]
            ctx.reply(self.message("pahf_balanced", first=first, second=second))

    @command("pateambalance", level=40)
    def cmd_pateambalance(self, ctx: CommandContext) -> None:
        """pateambalance [<on|off>] - warn and move whoever makes the teams uneven"""
        wanted = ctx.args.strip().lower()
        if not wanted:
            # The classic's answer here was `"... %s" % 'on' if x else 'off'`, which sends the bare
            # word `off` when the balancer is off: `%` binds tighter than the conditional.
            state = "on" if self.settings.get("teambalance") else "off"
            ctx.reply(self.message("pahf_balance_state", state=state))
            return
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pahf_toggle_usage", command=ctx.command.name))
            return
        self.settings["teambalance"] = wanted == "on"
        ctx.reply(self.message("pahf_balance_state", state=wanted))

    @command("paautobalance", level=40, alias="autobalance")
    def cmd_paautobalance(self, ctx: CommandContext) -> None:
        """paautobalance <on|off> - hand the job to the server's own team balancer"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pahf_toggle_usage", command=ctx.command.name))
            return
        # `'admin SetAutoBalance %s' % 'true' if on else 'false'` in the classic, where `%` binds
        # tighter than the conditional — so `off` sent the server the single word `false`.
        if not self.console.apply_server_verb(
            "autobalance", state="true" if wanted == "on" else "false"
        ):
            ctx.reply(self.message("pahf_unsupported"))
            return
        # And it said nothing at all either way, so an admin had no way to tell which had happened.
        ctx.reply(self.message("pahf_autobalance", state=wanted))

    # -- who may act on whom -------------------------------------------------

    def outranks(self, admin: Client, target: Client) -> bool:
        """Whether ``admin`` may act on ``target``, without saying anything about it."""
        if target is admin or (target.id is not None and target.id == admin.id):
            return False
        if admin.max_level() >= self._level("no_level_check_level", 60):
            return True
        return target.max_level() < admin.max_level()

    def may_act_on(self, ctx: CommandContext, target: Client) -> bool:
        """The same rule, explaining itself. The classic made no check anywhere in this plugin."""
        if self.outranks(ctx.client, target):
            return True
        if target is ctx.client or (target.id is not None and target.id == ctx.client.id):
            ctx.reply(self.message("action_denied_self"))
            return False
        key = "action_denied_masked" if target.is_masked() else "action_denied_level"
        ctx.reply(self.message(key, name=target.name))
        return False

    # -- players -------------------------------------------------------------

    @command("pachangeteam", level=60, alias="ct")
    def cmd_pachangeteam(self, ctx: CommandContext) -> None:
        """pachangeteam <player> - move a player to the other team"""
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pahf_changeteam_usage"))
            return
        target = self.resolve_client(ctx, wanted[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        if not self.switch(target):
            ctx.reply(self.message("pahf_unsupported"))
            return
        ctx.reply(self.message("pahf_changeteam", name=target.name))

    @command("paspectate", level=60, alias="spectate")
    def cmd_paspectate(self, ctx: CommandContext) -> None:
        """paspectate <player> - move a player to the spectators"""
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pahf_spectate_usage"))
            return
        target = self.resolve_client(ctx, wanted[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        if not self.console.apply_verb("spectate", target):
            ctx.reply(self.message("pahf_unsupported"))
            return
        target.team = "spec"
        self.hold_off(QUIET_AFTER_MOVE)
        ctx.reply(self.message("pahf_spectated", name=target.name))

    @command("pakill", level=60, alias="kill")
    def cmd_pakill(self, ctx: CommandContext) -> None:
        """pakill <player> [<reason>] - kill a player"""
        parts = ctx.args.split(None, 1)
        if not parts:
            ctx.reply(self.message("pahf_kill_usage"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        if not self.console.apply_verb("kill", target):
            ctx.reply(self.message("pahf_unsupported"))
            return
        # Announced *after* the kill went out. The classic put its announcement on the screen first
        # and said the reason afterwards, so a kill the server refused was announced anyway.
        reason = parts[1].strip() if len(parts) > 1 else ""
        key = "pahf_killed_reason" if reason else "pahf_killed"
        self.console.say_big(self.message(key, name=target.name, reason=reason))

    # -- the small ones ------------------------------------------------------

    @command("panextmap", level=20, alias="endmap")
    def cmd_panextmap(self, ctx: CommandContext) -> None:
        """panextmap - end this map and move on to the next in the rotation"""
        if not self.console.supports_server_verb("cyclemap"):
            ctx.reply(self.message("pahf_unsupported"))
            return
        self.console.say(self.message("pahf_nextmap"))
        self._after(ANNOUNCE_SECONDS, lambda: self.console.apply_server_verb("cyclemap"))

    @command("payell", level=20, alias="yell")
    def cmd_payell(self, ctx: CommandContext) -> None:
        """payell <message> - put a message where everybody will see it"""
        text = ctx.args.strip()
        if not text:
            ctx.reply(self.message("pahf_yell_usage"))
            return
        self.console.say_big(f"{ctx.client.name}: {text}")

    @command("paident", level=20, alias="id")
    def cmd_paident(self, ctx: CommandContext) -> None:
        """paident [<player>] - the Steam id of a player

        Answered privately, always. The classic said it through `sayLoudOrPM`, so `!!paident bob` put
        somebody's Steam id in public chat — and its help offered an IP address this engine never
        reports at all.
        """
        wanted = ctx.args.split()
        target = ctx.client if not wanted else self.resolve_client(ctx, wanted[0])
        if target is None:
            return
        self.console.tell(
            ctx.client,
            self.message("pahf_ident", name=target.name, guid=target.guid or "no Steam id"),
        )

    # -- match mode ----------------------------------------------------------

    @command("pamatch", level=20, alias="match")
    def cmd_pamatch(self, ctx: CommandContext) -> None:
        """pamatch <on|off> - hold a match: everybody readies up, then the map moves on"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pahf_match_usage"))
            return
        changed = self._set_match_mode(wanted == "on")
        if wanted == "on":
            self.console.say(self.message("pahf_match_on"))
            if changed:
                ctx.reply(self.message("pahf_match_plugins_off", plugins=", ".join(changed)))
            self.ready_up.begin()
            self.console.say(self.message("pahf_match_ready"))
            self.console.say_big(self.message("pahf_match_ready"))
            return
        self.ready_up.cancel()
        self.console.say(self.message("pahf_match_off"))
        if changed:
            ctx.reply(self.message("pahf_match_plugins_on", plugins=", ".join(changed)))

    def _set_match_mode(self, on: bool) -> list[str]:
        """Turn match mode on or off. Returns the plugins whose state this changed.

        The team check is *suspended* rather than switched off — `holding_off()` is true while a match
        is on. The classic set its own setting to off and never set it back.
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
        the admin plugin's command dictionary to add and remove it, and a `!pamatch off` that raised
        anywhere before that left `!ready` behind for good.
        """
        if not self.ready_up.running():
            ctx.reply(self.message("pahf_ready_none"))
            return
        state = self.ready_up.toggle(ctx.client)
        if state is None:
            ctx.reply(self.message("pahf_ready_late"))
            return
        ctx.reply(self.message("pahf_ready_yes" if state else "pahf_ready_no"))

    def _run_ready_up(self) -> None:
        """One pass of the ready-up, which is where all of its announcing happens.

        Everything goes out through `say`/`say_big`, which on this title are `adminsay` and
        `adminbigsay`. The classic sent Battlefield's `admin.yell` from here — as a tuple, to a
        parser that takes a string — so none of it arrived, and the match never started anything.
        """
        tick = self.ready_up.tick(self.console.clients.connected())
        if tick is None:
            return
        if tick.step is Step.WAITING:
            for client in tick.waiting:
                self.console.tell(client, self.message("pahf_match_waiting_you"))
            if 0 < len(tick.waiting) <= NAMES_IN_A_LINE:
                named = ", ".join(client.name for client in tick.waiting)
                self.console.say(self.message("pahf_match_waiting", players=named))
            return
        if tick.step is Step.READY:
            self.console.say(self.message("pahf_match_all_ready"))
            return
        if tick.step is Step.COUNT:
            self.console.say_big(self.message("pahf_match_count", count=tick.count))
            return
        self.console.say_big(self.message("pahf_match_start"))
        # `admin.restartMap` in the classic, which is Battlefield's. This engine cannot restart a map
        # at all: `admin nextmap` is the only thing it will do to the rotation, so a match starts on
        # the next map rather than on this one again.
        self.console.apply_server_verb("cyclemap")

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
        self._run_ready_up()


__all__ = [
    "ANNOUNCE_SECONDS",
    "DEFAULTS",
    "MESSAGES",
    "QUIET_AFTER_MOVE",
    "QUIET_SECONDS",
    "Delayed",
    "PoweradminhfPlugin",
]
