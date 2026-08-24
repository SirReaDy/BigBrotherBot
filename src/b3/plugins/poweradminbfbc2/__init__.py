"""Bad Company 2's admin commands — the teams, the playlists, the yells and the match.

A port of the classic `poweradminbfbc2` plugin. Bad Company 2 is Frostbite **1**, so this is a near
copy of `poweradminbf3` with a generation's worth of differences in it: the round verbs are spelled
in levels rather than rounds, the map list is a flat list of level names, the centre-screen message
counts its duration in milliseconds, and a game mode is chosen by *playlist* rather than per map.
Command names, levels and aliases are the classic's, because operators' config files are full of
them.

What it does falls into five groups. **The teams** — `!pateams` balances them now, `!pateambalance`
switches the automatic check on and off, and `!pachangeteam` and `!paspectate` move one player.
**The match** — `!pamatch on` asks everybody to type `!ready`, waits for them, counts down and
restarts the map, with the plugins an operator lists switched off for the duration. **The map** —
`!pamaprestart`, `!pamapreload`, `!pasetnextmap`, `!pamaplist` and the four playlist commands
(`!paconquest`, `!parush`, `!pasqdm`, `!pasqrush`). **Talking to people** — five `!payell` commands
and `!pakill`. And **the server** — `!paset`, `!paget`, `!paserverinfo`, `!paident` and
`!runscript`.

Changed from the classic, and the first three are why the team balancer never balanced anything:

* **`getTeams()` counted team 2 as team 1.** Both branches of its `if` appended to the same list, so
  the second team was always empty. Every reader of it — the automatic check, `!pateams` — therefore
  saw a gap the size of team 1, so on any server with two players on one side the teams were
  permanently "unbalanced".
* **`teambalance()` raised `AttributeError` every time it ran.** It read the join time with
  `c.isvar(self, 'teamtime')`, which returns a **bool**, and then asked that bool for `.value`. The
  raise came *after* `saybig('Autobalancing Teams!')`, so the server announced a balance and then
  moved nobody, every time, on both the automatic path and `!pateams`.
* **A team id was compared to the string `'1'`** where the parser makes it an `int`, so the
  comparison was never true. `!pachangeteam` therefore moved *everybody* to team 2 — including the
  players already on it, who stayed put while the bot reported "forced to the other team" — and the
  automatic check moved its offender to team 2 whichever side they were on.
* **The balancer moved the wrong players.** It sorted by join time ascending and took the front,
  which is the players who have been on that team longest. The player who just made the teams uneven
  is the one at the *other* end. (`poweradminmoh` fixed this in its own copy and this one never got
  it.) Most recent first here, which is also what `poweradminbf3` does.
* **Four of the five yell commands did not yell.** `!payellteam`, `!payellenemy`, `!payellsquad` and
  `!payellplayer` all called `client.message(...)`, which is an ordinary private chat line. Only
  `!payell` reached the screen. They are yells here, sent one player at a time with the one
  `admin.yell` subset this title is on record as taking — the team and squad subsets are not
  invented, because the classic never sent them from here.
* **`!pamaplist` sent one string where the protocol takes words.** `write(('mapList.load %s' % data))`
  is a *string*, not the one-tuple it looks like, so the whole thing went out as a single word. The
  filename was moot anyway: `mapList.load` on R9 — the oldest build this bot accepts — reads the
  server's own `mapList.txt` and takes no argument, so this command re-reads the rotation from disk
  and refuses an argument rather than pretending to load a named file.
* **`!pamapreload` emptied the rotation and then re-read it from disk.** `mapList.clear`,
  `mapList.append <current>`, `admin.runNextLevel`, `mapList.load` — the last of which discards the
  first two, so any rotation an admin had built in memory was silently replaced by whatever was on
  the server's disk. Reloading the current map needs neither: point the next-level index at the map
  being played and cycle.
* **Nothing sleeps.** The classic slept a second in `!pamaprestart` and `!pamapreload` — on the
  bot's one thread, so the whole bot stopped answering — and ran the whole match countdown on a
  chain of `threading.Timer`s. Announcements are a deadline on this plugin's one scheduled pass.
* **`!paset` crashed on `!paset friendlyFire`.** `data.split(' ', 1)[1]` on one word is an
  `IndexError`. `!paget` of a setting the server does not answer for handed `None.value` back for
  the same reason.
* **Match mode turned the team balancer off for good.** `!pamatch on` set the balancer to off and
  `!pamatch off` never set it back, so a match cost the server its balancer until the bot was
  restarted. It also re-enabled *every* plugin on its list when the match ended, so a plugin the
  operator had deliberately switched off came back on because a match had happened; only the ones
  this switched off are switched back on.
* **One immunity rule.** The classic checked nothing at all before killing, moving or spectating a
  player, so any moderator could `!pakill` a senior admin. The `poweradminbf3` rule applies here:
  you may not act on your equals or your betters, unless you are at or above
  `no_level_check_level`.
* **`!paident` answers privately.** It printed a player's IP address through `sayLoudOrPM`, so
  `!!paident bob` put somebody's IP in public chat.

**Not ported.** `!paversion` printed the plugin's own version string and author; there is no
per-plugin version here, and `!b3` already says what the bot is. `!pb_sv_command` is **`!punkbuster`
in the admin plugin** (aliased `!pbcmd`, which is the name this plugin's config gave it): every
PunkBuster family has that verb, and each of the classic's Frostbite plugins carried its own copy
with the Battlefield spelling written in.

Writing this turned up three faults below the plugin, all fixed in the Frostbite profile: this
title's rotate verb is `admin.runNextLevel` and it was being sent Medal of Honor's
`admin.runNextRound`, so `!map` inserted the map, pointed the server at it and then never loaded it;
its centre-screen message counts **milliseconds**, so the ten seconds the profile asked for was ten
milliseconds; and neither of the Frostbite 1 titles had any round or yell verbs declared at all.

`!pakill`, `!pateams`, `!paset` and `!paget` are also `poweradminurt` command names, and that plugin
is not restricted by title. An operator who loads both on one server will find the second one
**refused** those names, with both plugins named in the log; the first to load keeps them.
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
from b3.core.util import as_int, match_names, sanitize_rcon_value
from b3.domain.client import Client
from b3.domain.permissions import level_for

log = logging.getLogger(__name__)

#: How long the bot waits after announcing something before doing it, so the players reading the
#: announcement get a moment. The classic slept exactly this long, on the bot's own thread.
ANNOUNCE_SECONDS = 1.0

#: How long the automatic team check stands down for after the bot starts, a round starts, or the
#: bot itself moves somebody. The classic's minute: the flurry of team changes a round start produces
#: reads as everybody unbalancing the teams at once.
QUIET_SECONDS = 60.0

#: A yell's duration goes out in **milliseconds** on this generation of the engine. An operator sets
#: seconds, because nobody thinks in milliseconds.
MS = 1000

#: The four playlists this title ships, as `admin.setPlaylist` spells them, keyed by the command that
#: chooses one. A playlist is the set of game modes the server will run; unlike Frostbite 2 there is
#: no per-map mode, so this is how a Bad Company 2 server changes what it is playing.
PLAYLISTS = {
    "paconquest": "CONQUEST",
    "parush": "RUSH",
    "pasqdm": "SQDM",
    "pasqrush": "SQRUSH",
}

#: A setting's name, as the engine spells one. Checked rather than trusted: the value goes out inside
#: a quoted word, but the *name* is the command itself, so a space in it is a second command.
CVAR_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(\.[A-Za-z][A-Za-z0-9]*)*$")

#: The prefix a Frostbite setting lives under. An operator may type either half.
CVAR_PREFIX = "vars."

#: `admin.runScript` runs a file of RCON commands that lives on the game server. A filename, and
#: nothing that could be a second command — the same check `poweradminurt` makes on `!paexec`.
SCRIPT_FILE_RE = re.compile(r"^[\w.-]+\.cfg$", re.IGNORECASE)

DEFAULTS: dict[str, object] = {
    # Move players between teams when the sides drift apart, and warn whoever did it. Off as the
    # classic shipped it.
    "teambalance": False,
    # Players at or above this level are never moved by the automatic check. The classic had no such
    # exemption, so it moved the admin who had just joined to sort the teams out.
    "no_balance_level": 20,
    # An admin at or above this level is not level-checked when acting on another player. The
    # classic's escape hatch, from `poweradminbf3` where it is written down; the classic here checked
    # nothing at all.
    "no_level_check_level": 60,
    # How many players one side may be ahead by before anybody is moved. The classic's figure, and
    # its floor: 1 means every arrival is moved straight back again.
    "max_difference": 1,
    # How long a `!payell` stays on screen, in seconds.
    "yell_duration": 10,
    # Plugins switched off while a match is on, and back on when it ends. The classic's own list.
    "match_plugins": "spree, tk, pingwatch",
}

MESSAGES = {
    "pabc2_balanced": "the teams are even: {first} v {second}",
    "pabc2_balancing": "balancing the teams",
    "pabc2_moving": "moving {name} to the other team",
    "pabc2_unbalanced_you": "please do not make the teams uneven",
    "pabc2_balance_state": "the automatic team check is {state}",
    "pabc2_toggle_usage": "!{command} on|off",
    "pabc2_changeteam_usage": "!pachangeteam <player>",
    "pabc2_changeteam": "{name} moved to the other team",
    "pabc2_no_team": "the server has not said which team {name} is on",
    "pabc2_spectate_usage": "!paspectate <player>",
    "pabc2_spectated": "{name} taken out of the game",
    "pabc2_kill_usage": "!pakill <player> [<reason>]",
    "pabc2_killed": "{name} was killed by an admin",
    "pabc2_killed_reason": "{name} was killed by an admin: {reason}",
    "pabc2_unsupported": "this title has no verb for that",
    "pabc2_map_restart": "restarting the map",
    "pabc2_map_reload": "reloading the map",
    "pabc2_maplist_usage": "!pamaplist - re-reads the server's own map list; it takes no filename",
    "pabc2_maplist": "the server's map list has been re-read: {count} map(s)",
    "pabc2_nextmap_usage": "!pasetnextmap <map>",
    "pabc2_nextmap_unknown": "no map like {map} — try {maps}",
    "pabc2_nextmap": "the next map will be {map}",
    "pabc2_playlist": "the playlist is {playlist} from the next map on — !map <map> to start it now",
    "pabc2_playlist_failed": "the server would not set the {playlist} playlist",
    "pabc2_yell_usage": "!{command} <message>",
    "pabc2_yell_player_usage": "!payellplayer <player> <message>",
    "pabc2_yell_nobody": "there is nobody to yell at",
    "pabc2_set_usage": "!paset <setting> <value>",
    "pabc2_set_bad_name": "{name} is not a setting name",
    "pabc2_set": "{name} is now {value}",
    "pabc2_get_usage": "!paget <setting>",
    "pabc2_get": "{name} is {value}",
    "pabc2_get_unknown": "the server did not say what {name} is",
    "pabc2_serverinfo": "{hostname}: {players}/{max} on {map}, playing {gametype}",
    "pabc2_ident": "{name}: slot {cid}, {ip}, {guid}",
    "pabc2_script_usage": "!runscript <file.cfg>",
    "pabc2_script_bad_name": "{name} is not a config file I will run",
    "pabc2_script": "running {name} on the server",
    "pabc2_match_usage": "!pamatch on|off",
    "pabc2_match_on": "match mode is on",
    "pabc2_match_off": "match mode is off",
    "pabc2_match_plugins_off": "switched off for the match: {plugins}",
    "pabc2_match_plugins_on": "switched back on: {plugins}",
    "pabc2_match_ready": "everybody type !ready when you are",
    "pabc2_match_waiting_you": "we are waiting for you — type !ready",
    "pabc2_match_waiting": "waiting for {players}",
    "pabc2_match_all_ready": "everybody is ready — counting down",
    "pabc2_match_count": "match starting in {count}",
    "pabc2_match_start": "fight!",
    "pabc2_ready_none": "no match is being started, so there is nothing to be ready for",
    "pabc2_ready_late": "the countdown has started; you cannot change your mind now",
    "pabc2_ready_yes": "you are ready",
    "pabc2_ready_no": "you are not ready any more",
}

#: How many players the classic would name when saying who a match was waiting for. Past that the
#: line is longer than the chat area and says nothing anybody can read.
NAMES_IN_A_LINE = 6


@dataclass(slots=True)
class Delayed:
    """Something announced now and done a few seconds later, on the scheduled pass."""

    due: float
    action: Callable[[], object]


class Poweradminbfbc2Plugin(Plugin):
    """Bad Company 2's admin commands, its team balancer and its ready-up match mode."""

    requires_plugins = ("admin",)
    requires_parsers = ("bfbc2",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.match_mode = False
        #: The plugins *this* switched off, so that only those are switched back on.
        self._disabled_for_match: list[str] = []
        #: The ready-up, shared with the other two titles that have one (b3.core.matchmode).
        self.ready_up = ReadyUp(console.clock)
        self._delayed: list[Delayed] = []
        #: Until when the automatic team check stands down — a round start, a move of the bot's own.
        self._quiet_until = 0.0

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        # 1 means every arrival is moved straight back again, so it is the floor rather than a value.
        self.settings["max_difference"] = max(1, as_int(self.settings.get("max_difference"), 1))

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.schedule(self._run_pending, second="*", name="Poweradminbfbc2Plugin.pending")
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_team_change)
        self.subscribe(EventType.GAME_ROUND_START, self.on_round_start)
        self.hold_off()

    def on_disable(self) -> None:
        """A disabled plugin has no business counting a match down that nobody can see."""
        self.ready_up.cancel()
        self._delayed.clear()

    def _level(self, key: str, default: int) -> int:
        """A level setting, read the way every other plugin reads one."""
        level = level_for(self.settings.get(key))
        if level is None:
            log.warning(
                "poweradminbfbc2: %s is %r, which is neither a group keyword nor a level 0-100; "
                "using %d",
                key,
                self.settings.get(key),
                default,
            )
            return default
        return level

    # -- the quiet window ----------------------------------------------------

    def hold_off(self, seconds: float = QUIET_SECONDS) -> None:
        """Stand the automatic team check down for a while.

        Every deliberate move calls this. Without it the check reads the bot's own balancing as
        players unbalancing the teams, and answers a round start — where everybody changes team at
        once — by moving half of them back.
        """
        self._quiet_until = max(self._quiet_until, self.console.clock.now() + seconds)

    def holding_off(self) -> bool:
        """Whether the automatic check is standing down. A match counts: the teams are agreed."""
        return self.match_mode or self.console.clock.now() < self._quiet_until

    # -- events --------------------------------------------------------------

    def on_auth(self, event: Event) -> None:
        if event.client is not None:
            self._stamp(event.client)

    def on_team_change(self, event: Event) -> None:
        """Somebody changed team, which is the only moment the classic ever checked the teams."""
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
        theirs = self.console.team_id(client.team or "")
        if not theirs or len(counts) < 2:
            return
        smaller = min(counts, key=lambda key: counts[key])
        if counts[theirs] - counts[smaller] <= self._difference():
            return
        if theirs == smaller:
            return
        self.console.tell(client, self.message("pabc2_unbalanced_you"))
        self.move(client, smaller)

    def on_round_start(self, _event: Event) -> None:
        self.hold_off()

    def _stamp(self, client: Client) -> None:
        """Remember when this player joined the team they are on; the balancer sorts on it."""
        client.set_var(self, "teamtime", self.console.clock.now())

    # -- the teams -----------------------------------------------------------

    def team_counts(self) -> dict[str, int]:
        """How many players are on each team, keyed by the engine's team id.

        Counted from the bot's own client list rather than asked of the server. The classic asked —
        and then dropped team 2 into team 1's list, so the answer it got back was wrong in a way no
        amount of asking would have fixed.
        """
        counts: dict[str, int] = {}
        for client in self.console.clients.connected():
            engine_team = self.console.team_id(client.team or "")
            if engine_team:
                counts[engine_team] = counts.get(engine_team, 0) + 1
        return counts

    def _difference(self) -> int:
        return max(1, as_int(self.settings.get("max_difference"), 1))

    def _may_be_moved(self, client: Client) -> bool:
        if client.is_bot:
            return False
        return client.max_level() < self._level("no_balance_level", 20)

    def balance(self) -> bool:
        """Move the most recent joiners off the bigger team. True if anybody moved.

        Announced only once somebody is actually going to move: the classic said "Autobalancing
        Teams!" first and then raised, so the announcement was the only part that ever worked.
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
            if self.console.team_id(client.team or "") == bigger and self._may_be_moved(client)
        ]
        # Most recent team join first. The classic sorted the other way and moved the players who had
        # been on that team longest, which is precisely the wrong end of the list.
        movable.sort(key=lambda c: c.get_var(self, "teamtime", 0.0), reverse=True)
        moving = movable[: gap // 2]
        if not moving:
            return False
        self.console.say(self.message("pabc2_balancing"))
        for client in moving:
            if self.move(client, smaller):
                self.console.say(self.message("pabc2_moving", name=client.name))
        return True

    def move(self, client: Client, team: str, squad: str = "0") -> bool:
        """Put a player on a team. Marked as the bot's own move, so the check ignores the event."""
        client.set_var(self, "moved_by_bot", True)
        moved = self.console.apply_verb("move", client, team=team, squad=squad or "0")
        if not moved:
            client.del_var(self, "moved_by_bot")
            return False
        client.team = self._team_name(team)
        client.squad = squad or "0"
        self._stamp(client)
        self.hold_off(ANNOUNCE_SECONDS * 10)
        return True

    def _team_name(self, engine_team: str) -> str:
        for name in ("red", "blue", "green", "yellow"):
            if self.console.team_id(name) == engine_team:
                return name
        return engine_team

    def other_team(self, client: Client) -> str:
        """The team a `!pachangeteam` would put this player on, in the engine's spelling."""
        current = self.console.team_id(client.team or "")
        if not current or not current.isdigit() or current == "0":
            return ""
        return "2" if current == "1" else "1"

    @command("pateams", level=20, alias="teams")
    def cmd_pateams(self, ctx: CommandContext) -> None:
        """pateams - even the teams up now"""
        counts = self.team_counts()
        if not self.balance():
            first, second = (sorted(counts.values(), reverse=True) + [0, 0])[:2]
            ctx.reply(self.message("pabc2_balanced", first=first, second=second))

    @command("pateambalance", level=40)
    def cmd_pateambalance(self, ctx: CommandContext) -> None:
        """pateambalance <on|off> - move players across when the sides drift apart"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pabc2_toggle_usage", command=ctx.command.name))
            return
        self.settings["teambalance"] = wanted == "on"
        ctx.reply(self.message("pabc2_balance_state", state=wanted))

    @command("pachangeteam", level=60, alias="ct")
    def cmd_pachangeteam(self, ctx: CommandContext) -> None:
        """pachangeteam <player> - move a player to the other team"""
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pabc2_changeteam_usage"))
            return
        target = self.resolve_client(ctx, wanted[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        other = self.other_team(target)
        if not other:
            ctx.reply(self.message("pabc2_no_team", name=target.name))
            return
        if not self.move(target, other):
            ctx.reply(self.message("pabc2_unsupported"))
            return
        ctx.reply(self.message("pabc2_changeteam", name=target.name))

    @command("paspectate", level=60, alias="spectate")
    def cmd_paspectate(self, ctx: CommandContext) -> None:
        """paspectate <player> - take a player out of the game

        This title has no spectator team, and the classic did not pretend otherwise: it moved the
        player to team 0, which is the deploy screen — out of the game, watching, until they pick a
        side again. Same here, since it is the only thing the engine offers.
        """
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pabc2_spectate_usage"))
            return
        target = self.resolve_client(ctx, wanted[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        if not self.move(target, "0"):
            ctx.reply(self.message("pabc2_unsupported"))
            return
        ctx.reply(self.message("pabc2_spectated", name=target.name))

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

    @command("pakill", level=60, alias="kill")
    def cmd_pakill(self, ctx: CommandContext) -> None:
        """pakill <player> [<reason>] - kill a player without touching the scoreboard"""
        parts = ctx.args.split(None, 1)
        if not parts:
            ctx.reply(self.message("pabc2_kill_usage"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None or not self.may_act_on(ctx, target):
            return
        reason = parts[1].strip() if len(parts) > 1 else ""
        if not self.console.apply_verb("kill", target):
            ctx.reply(self.message("pabc2_unsupported"))
            return
        # Announced *after* the kill went out, not before it. The classic put its announcement on
        # the screen first and said the reason afterwards, so a kill the server refused was still
        # announced to everybody.
        key = "pabc2_killed_reason" if reason else "pabc2_killed"
        self.console.say_big(self.message(key, name=target.name, reason=reason))

    # -- the map -------------------------------------------------------------

    @command("pamaprestart", level=60, alias="maprestart")
    def cmd_pamaprestart(self, ctx: CommandContext) -> None:
        """pamaprestart - restart the map being played"""
        if not self.console.supports_server_verb("map_restart"):
            ctx.reply(self.message("pabc2_unsupported"))
            return
        self.console.say(self.message("pabc2_map_restart"))
        self._after(ANNOUNCE_SECONDS, lambda: self.console.apply_server_verb("map_restart"))

    @command("pamapreload", level=60, alias="mapreload")
    def cmd_pamapreload(self, ctx: CommandContext) -> None:
        """pamapreload - load the current map again from the start

        Two commands: point the next-level index at the map being played, then cycle. The classic
        cleared the server's map list, put the current level in it, cycled, and then ran
        `mapList.load` — which re-reads the list from the server's disk and so threw away the two
        commands before it.
        """
        if not self.console.supports_server_verb("cyclemap"):
            ctx.reply(self.message("pabc2_unsupported"))
            return
        current = self.console.get_map() or ""
        maps = self.console.get_maps()
        if current and current in maps:
            self.console.rcon_words(f"mapList.nextLevelIndex {maps.index(current)}")
        self.console.say(self.message("pabc2_map_reload"))
        self._after(ANNOUNCE_SECONDS, lambda: self.console.apply_server_verb("cyclemap"))

    @command("pamaplist", level=60, alias="maplist")
    def cmd_pamaplist(self, ctx: CommandContext) -> None:
        """pamaplist - re-read the server's own map list from its disk"""
        if ctx.args.strip():
            # The classic took a filename and never sent it: the command was built as one string
            # rather than a word list, so `mapList.load whatever.txt` went out as a single word. On
            # R9, the oldest build this bot accepts, `mapList.load` reads the server's `mapList.txt`
            # and takes no argument at all.
            ctx.reply(self.message("pabc2_maplist_usage"))
            return
        self.console.rcon_words("mapList.load")
        ctx.reply(self.message("pabc2_maplist", count=len(self.console.get_maps())))

    @command("pasetnextmap", level=60, alias="setnextmap")
    def cmd_pasetnextmap(self, ctx: CommandContext) -> None:
        """pasetnextmap <map> - the map to play after this one"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pabc2_nextmap_usage"))
            return
        maps = self.console.get_maps()
        # Matched against the rotation, by id or by the name a player would call it — and where the
        # rotation cannot be read at all, what the admin typed is the map. That is the rule `!map`
        # follows, and it is all that can be done on a server that will not answer.
        chosen = wanted
        if maps:
            found = match_names(wanted, [(m, self.console.map_display(m)) for m in maps])
            if len(found) != 1:
                listed = ", ".join(self.console.map_display(m) for m in (found or maps)[:5])
                ctx.reply(self.message("pabc2_nextmap_unknown", map=wanted, maps=listed))
                return
            chosen = found[0]
        self.set_next_map(chosen, maps)
        ctx.reply(self.message("pabc2_nextmap", map=self.console.map_display(chosen)))

    def set_next_map(self, map_id: str, maps: list[str]) -> None:
        """Point the rotation at ``map_id`` for the next level.

        Frostbite 1 holds a flat list of level names and the next map is an *index into it*, not a
        setting — which is why this is here rather than behind `Console.set_next_map`. A map already
        in the rotation is pointed at where it is; one that is not is inserted at the next index,
        which is what makes it possible to play a map that is installed and not rotated.
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

    # -- the playlists -------------------------------------------------------

    @command("paconquest", level=60, alias="conq")
    def cmd_paconquest(self, ctx: CommandContext) -> None:
        """paconquest - play Conquest from the next map on"""
        self._set_playlist(ctx)

    @command("parush", level=60, alias="rush")
    def cmd_parush(self, ctx: CommandContext) -> None:
        """parush - play Rush from the next map on"""
        self._set_playlist(ctx)

    @command("pasqdm", level=60, alias="sqdm")
    def cmd_pasqdm(self, ctx: CommandContext) -> None:
        """pasqdm - play Squad Deathmatch from the next map on"""
        self._set_playlist(ctx)

    @command("pasqrush", level=60, alias="sqru")
    def cmd_pasqrush(self, ctx: CommandContext) -> None:
        """pasqrush - play Squad Rush from the next map on"""
        self._set_playlist(ctx)

    def _set_playlist(self, ctx: CommandContext) -> None:
        """The four playlist commands, which differ only in the word they send."""
        playlist = PLAYLISTS[ctx.command.name]
        reply = self.console.rcon_words(f"admin.setPlaylist {playlist}")
        if reply and reply[0] not in ("OK", ""):
            # This engine answers a command it refused, and the classic threw the answer away — so a
            # playlist the server would not take looked exactly like one it had.
            log.warning("poweradminbfbc2: the server refused playlist %s: %s", playlist, reply[0])
            ctx.reply(self.message("pabc2_playlist_failed", playlist=playlist))
            return
        ctx.reply(self.message("pabc2_playlist", playlist=playlist))

    # -- talking to people ---------------------------------------------------

    @command("payell", level=20, alias="yell")
    def cmd_payell(self, ctx: CommandContext) -> None:
        """payell <message> - put a message on everybody's screen"""
        text = ctx.args.strip()
        if not text:
            ctx.reply(self.message("pabc2_yell_usage", command=ctx.command.name))
            return
        if not self.console.apply_server_verb("yell", text=text, ms=self._yell_ms()):
            ctx.reply(self.message("pabc2_unsupported"))

    @command("payellteam", level=20, alias="yt")
    def cmd_payellteam(self, ctx: CommandContext) -> None:
        """payellteam <message> - put a message on your own team's screens"""
        mine = self.console.team_id(ctx.client.team or "")
        self._yell_each(
            ctx,
            [
                c
                for c in self.console.clients.connected()
                if self.console.team_id(c.team or "") == mine
            ],
        )

    @command("payellenemy", level=20, alias="ye")
    def cmd_payellenemy(self, ctx: CommandContext) -> None:
        """payellenemy <message> - put a message on the other team's screens"""
        mine = self.console.team_id(ctx.client.team or "")
        self._yell_each(
            ctx,
            [
                c
                for c in self.console.clients.connected()
                if self.console.team_id(c.team or "") not in ("", mine)
            ],
        )

    @command("payellsquad", level=20, alias="ys")
    def cmd_payellsquad(self, ctx: CommandContext) -> None:
        """payellsquad <message> - put a message on your own squad's screens"""
        mine = self.console.team_id(ctx.client.team or "")
        squad = ctx.client.squad or "0"
        self._yell_each(
            ctx,
            [
                c
                for c in self.console.clients.connected()
                if self.console.team_id(c.team or "") == mine and (c.squad or "0") == squad
            ],
        )

    @command("payellplayer", level=20, alias="yp")
    def cmd_payellplayer(self, ctx: CommandContext) -> None:
        """payellplayer <player> <message> - put a message on one player's screen"""
        parts = ctx.args.split(None, 1)
        if len(parts) < 2:
            ctx.reply(self.message("pabc2_yell_player_usage"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        if not self.yell_to(target, parts[1].strip()):
            ctx.reply(self.message("pabc2_unsupported"))

    def _yell_each(self, ctx: CommandContext, targets: list[Client]) -> None:
        """Yell to a set of players, one at a time.

        One `admin.yell` per player rather than the engine's team and squad subsets: those exist,
        but the classic never sent them from here — its team, enemy and squad commands sent ordinary
        chat — so there is no captured grammar for them on this generation and none is invented.
        The player subset is the one this title is on record as taking.
        """
        text = ctx.args.strip()
        if not text:
            ctx.reply(self.message("pabc2_yell_usage", command=ctx.command.name))
            return
        if not targets:
            ctx.reply(self.message("pabc2_yell_nobody"))
            return
        sent = 0
        for target in targets:
            sent += bool(self.yell_to(target, text))
        if not sent:
            ctx.reply(self.message("pabc2_unsupported"))

    def yell_to(self, client: Client, text: str) -> bool:
        return self.console.apply_verb("yell_player", client, text=text, ms=self._yell_ms())

    def _yell_ms(self) -> str:
        """The yell duration, in the milliseconds this generation counts in."""
        return str(max(1, as_int(self.settings.get("yell_duration"), 10)) * MS)

    # -- the server ----------------------------------------------------------

    @command("paset", level=100)
    def cmd_paset(self, ctx: CommandContext) -> None:
        """paset <setting> <value> - set one of the server's settings"""
        parts = ctx.args.split(None, 1)
        # `data.split(' ', 1)[1]` in the classic, so `!paset friendlyFire` was an IndexError.
        if len(parts) < 2:
            ctx.reply(self.message("pabc2_set_usage"))
            return
        name = self._cvar_name(ctx, parts[0])
        if not name:
            return
        self.console.set_cvar(name, parts[1].strip())
        ctx.reply(self.message("pabc2_set", name=name, value=parts[1].strip()))

    @command("paget", level=100)
    def cmd_paget(self, ctx: CommandContext) -> None:
        """paget <setting> - what one of the server's settings is"""
        wanted = ctx.args.split()
        if not wanted:
            ctx.reply(self.message("pabc2_get_usage"))
            return
        name = self._cvar_name(ctx, wanted[0])
        if not name:
            return
        value = self.console.get_cvar(name)
        if value is None:
            # `getCvar(...).value` in the classic, which is an AttributeError on a server that does
            # not answer — and one that does not know the setting does not answer.
            ctx.reply(self.message("pabc2_get_unknown", name=name))
            return
        ctx.reply(self.message("pabc2_get", name=name, value=value))

    def _cvar_name(self, ctx: CommandContext, wanted: str) -> str:
        """A settings name, with the `vars.` prefix either half of it may be typed with.

        Checked rather than trusted: on this engine the setting's *name* is the command, so a space
        or a quote in it is a second command rather than a bad value.
        """
        name = wanted.strip()
        if not CVAR_NAME_RE.match(name):
            ctx.reply(self.message("pabc2_set_bad_name", name=name))
            return ""
        return name if "." in name else CVAR_PREFIX + name

    @command("paserverinfo", level=40)
    def cmd_paserverinfo(self, ctx: CommandContext) -> None:
        """paserverinfo - what the server says about itself"""
        game = self.console.game
        ctx.reply(
            self.message(
                "pabc2_serverinfo",
                hostname=game.hostname or "this server",
                players=len(self.console.clients.connected()),
                max=game.max_players or "?",
                map=self.console.map_display(game.map_name) if game.map_name else "?",
                gametype=game.gametype or "?",
            )
        )

    @command("paident", level=20, alias="id")
    def cmd_paident(self, ctx: CommandContext) -> None:
        """paident [<player>] - the slot, address and GUID of a player

        Answered privately, always. The classic said it through `sayLoudOrPM`, so `!!paident bob`
        put somebody's IP address in public chat.
        """
        wanted = ctx.args.split()
        target = ctx.client if not wanted else self.resolve_client(ctx, wanted[0])
        if target is None:
            return
        self.console.tell(
            ctx.client,
            self.message(
                "pabc2_ident",
                name=target.name,
                cid=target.cid or "?",
                ip=target.ip or "no address",
                guid=target.guid or "no GUID",
            ),
        )

    @command("runscript", level=100)
    def cmd_runscript(self, ctx: CommandContext) -> None:
        """runscript <file.cfg> - run a file of server commands that is on the game server"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pabc2_script_usage"))
            return
        if not SCRIPT_FILE_RE.match(wanted):
            ctx.reply(self.message("pabc2_script_bad_name", name=wanted))
            return
        if not self.console.apply_server_verb("exec", file=wanted):
            ctx.reply(self.message("pabc2_unsupported"))
            return
        log.info("poweradminbfbc2: %s ran %s on the server", ctx.client.name, wanted)
        ctx.reply(self.message("pabc2_script", name=wanted))

    # -- match mode ----------------------------------------------------------

    @command("pamatch", level=20, alias="match")
    def cmd_pamatch(self, ctx: CommandContext) -> None:
        """pamatch <on|off> - hold a match: everybody readies up, then the map restarts"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("pabc2_match_usage"))
            return
        changed = self._set_match_mode(wanted == "on")
        if wanted == "on":
            self.console.say(self.message("pabc2_match_on"))
            if changed:
                ctx.reply(self.message("pabc2_match_plugins_off", plugins=", ".join(changed)))
            self.ready_up.begin()
            self.console.say(self.message("pabc2_match_ready"))
            self.console.say_big(self.message("pabc2_match_ready"))
            return
        self.ready_up.cancel()
        self.console.say(self.message("pabc2_match_off"))
        if changed:
            ctx.reply(self.message("pabc2_match_plugins_on", plugins=", ".join(changed)))

    def _set_match_mode(self, on: bool) -> list[str]:
        """Turn match mode on or off. Returns the plugins whose state this changed.

        The team balancer is *suspended* rather than switched off — `holding_off()` is true while a
        match is on. The classic set the balancer's own setting to off and never set it back, so a
        match cost the server its balancer until the bot restarted.
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
            ctx.reply(self.message("pabc2_ready_none"))
            return
        state = self.ready_up.toggle(ctx.client)
        if state is None:
            ctx.reply(self.message("pabc2_ready_late"))
            return
        self.yell_to(ctx.client, self.message("pabc2_ready_yes" if state else "pabc2_ready_no"))
        ctx.reply(self.message("pabc2_ready_yes" if state else "pabc2_ready_no"))

    def _run_ready_up(self) -> None:
        """One pass of the ready-up, which is where all of its announcing happens."""
        tick = self.ready_up.tick(self.console.clients.connected())
        if tick is None:
            return
        if tick.step is Step.WAITING:
            for client in tick.waiting:
                self.yell_to(client, self.message("pabc2_match_waiting_you"))
            if 0 < len(tick.waiting) <= NAMES_IN_A_LINE:
                named = ", ".join(client.name for client in tick.waiting)
                self.console.say(self.message("pabc2_match_waiting", players=named))
            return
        if tick.step is Step.READY:
            self.console.say(self.message("pabc2_match_all_ready"))
            return
        if tick.step is Step.COUNT:
            self.console.say_big(self.message("pabc2_match_count", count=tick.count))
            return
        self.console.say_big(self.message("pabc2_match_start"))
        self.console.apply_server_verb("map_restart")

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
    "CVAR_PREFIX",
    "DEFAULTS",
    "MESSAGES",
    "MS",
    "PLAYLISTS",
    "QUIET_SECONDS",
    "Delayed",
    "Poweradminbfbc2Plugin",
]
