"""Per-session statistics — kills, deaths, damage, a skill score and an XP figure.

A port of the classic `stats` plugin. Its good idea is that the skill score is **relative**: killing
somebody who is doing well is worth more than killing somebody who is not, so a player who farms the
weakest opponent on the server climbs slowly and one who takes on the best climbs fast. Everything is
per session and nothing is written to the database, which is what keeps this plugin cheap — the
classic bot's persistent statistics were `xlrstats`, a project of its own.

The arithmetic is the original's, down to the rounding, because the classic bot's captured tests pin
it: two players on equal points are worth 12.5 to each other, a victim on double the killer's score
is worth 20, and half is worth 8.75. `!mapstats`, `!testscore`, `!topstats` and `!topxp` are its
commands at its own levels.

Changed, and each for a reason:

* **Assists are not gated on a game name.** The classic checked `gameName == "iourt43"` in three
  places to decide whether to subscribe to the assist event and whether to print an "A" column.
  Here the event is always subscribed and the column appears once the engine has actually reported
  one — which is the same question asked of the server rather than of a table of game ids.
* **An award with nobody to award crashes nothing.** `show_awards` called the command with no client,
  and the "no top players" branch then called `client.message` on None. Here an empty board is
  announced as such, or not at all.
* **One damage rule, shared.** How much a kill or a hit was worth lives in `b3.core.events`
  (`damage_points`), because four families report no figure and both this plugin and `tk` have to
  answer that question the same way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType, damage_points
from b3.core.plugin import Plugin
from b3.domain.client import Client
from b3.core.util import as_float, as_int

log = logging.getLogger(__name__)

#: How many players a top-five board holds. The classic's figure, and it is about a chat line's
#: width rather than about statistics.
TOP_COUNT = 5

DEFAULTS: dict[str, object] = {
    # The score everybody starts on. Points are added to and taken from it, so it is also the number
    # a player's skill is judged against — 100 in, 87.5 means they are down on the day.
    "start_points": 100,
    # Wipe the skill score / the XP when a round starts. Both off, as the classic had them: a score
    # that resets every round cannot say much about a session.
    "reset_score": False,
    "reset_xp": False,
    # Announce the boards when the map ends, without anybody asking.
    "show_awards": False,
    "show_awards_xp": False,
    # Levels for the four commands. The classic's `plugin_stats.ini` defaults: anybody may look at
    # their own stats, regulars and up may see the boards.
    "mapstats_level": 0,
    "testscore_level": 0,
    "topstats_level": 2,
    "topxp_level": 2,
}

MESSAGES = {
    "stats_line": "stats [{name}] K {kills} D {deaths} TK {team_kills} dmg {damage} "
    "skill {skill} XP {xp}",
    # Used instead of the above on an engine that reports assists — see `StatsPlugin.on_assist`.
    "stats_line_with_assists": "stats [{name}] K {kills} D {deaths} A {assists} TK {team_kills} "
    "dmg {damage} skill {skill} XP {xp}",
    "stats_testscore": "{name} will get {points} skill points for killing {target}",
    "stats_testscore_self": "you get no points for killing yourself",
    "stats_testscore_teammate": "you get no points for killing a teammate",
    "stats_top": "top stats: {players}",
    "stats_top_xp": "most experienced: {players}",
    "stats_top_empty": "nobody has scored yet",
}


@dataclass
class PlayerStats:
    """One player's session so far.

    Held against the client (`Client.set_var`), so it lasts exactly as long as they are connected
    and costs nothing when they leave. The classic kept the same figures as a dozen separate client
    variables, which is why `onRoundStart` there is a wall of `setvar` calls that has to be kept in
    step with every read site.
    """

    kills: int = 0
    deaths: int = 0
    assists: int = 0
    team_kills: int = 0
    #: Hits landed and damage dealt to opponents, and the same suffered.
    shots_hit: int = 0
    damage_hit: int = 0
    shots_got: int = 0
    damage_got: int = 0
    #: Hits landed on teammates. Kept apart from `damage_hit` on purpose — the captured tests show a
    #: team kill adding nothing to the damage column, because damage to your own side is not a
    #: contribution to anything.
    shots_team_hit: int = 0
    damage_team_hit: int = 0
    #: The relative skill score, and how it got there.
    points: float = 0.0
    points_won: float = 0.0
    points_lost: float = 0.0
    #: This round's XP, and the sum of the rounds before it. Two fields because a round start files
    #: the current figure away rather than dropping it, so `!mapstats` can show a session total while
    #: `!topxp` ranks on the round.
    experience: float = 0.0
    previous_experience: float = 0.0
    #: Set once the player has done anything at all, so the boards can leave out the people who
    #: joined and stood still. The classic used "does this client have a points variable?", which was
    #: true of anybody who had merely been *read* — including the victim of a team kill.
    active: bool = False


class StatsPlugin(Plugin):
    """Keeps a session's worth of statistics and answers questions about them."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: Whether this engine has been seen to report an assist. Only Urban Terror 4.3 does, and
        #: this is how the "A" column appears there without a table of game ids deciding it.
        self.reports_assists = False

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_KILL, self.on_kill)
        self.subscribe(EventType.CLIENT_KILL_TEAM, self.on_team_kill)
        self.subscribe(EventType.CLIENT_GIB, self.on_kill)
        self.subscribe(EventType.CLIENT_GIB_TEAM, self.on_team_kill)
        self.subscribe(EventType.CLIENT_DAMAGE, self.on_damage)
        self.subscribe(EventType.CLIENT_DAMAGE_TEAM, self.on_team_damage)
        self.subscribe(EventType.CLIENT_ASSIST, self.on_assist)
        self.subscribe(EventType.GAME_ROUND_START, self.on_round_start)
        for over in (EventType.GAME_EXIT, EventType.GAME_MAP_CHANGE):
            self.subscribe(over, self.on_map_end)
        self._apply_command_levels()

    def _apply_command_levels(self) -> None:
        """Put the four commands at their configured levels.

        The classic read them from a `[commands]` section whose keys were `mapstats-stats` — the
        command and its alias glued together — and matched them by prefix. They are plain settings
        here; the alias is a property of the command, not of the operator's config.
        """
        for name, key in (
            ("mapstats", "mapstats_level"),
            ("testscore", "testscore_level"),
            ("topstats", "topstats_level"),
            ("topxp", "topxp_level"),
        ):
            registered = self.console.command_registry.get(name)
            if registered is None:  # pragma: no cover - registration is the framework's job
                continue
            registered.min_level = as_int(self.settings.get(key), 0)

    # -- the record ----------------------------------------------------------

    def stats(self, client: Client) -> PlayerStats:
        """This player's figures, created on first use and starting on `start_points`."""
        record = client.get_var(self, "stats")
        if not isinstance(record, PlayerStats):
            record = PlayerStats(points=self._start_points())
            client.set_var(self, "stats", record)
        return record

    def _start_points(self) -> float:
        return as_float(self.settings.get("start_points"), 100.0)

    def score(self, killer: Client, victim: Client) -> float:
        """What killing ``victim`` is worth to ``killer`` right now.

        The classic's formula, kept exactly: the victim's score over the killer's, halved, times
        fifteen, plus five — so an even match is 12.5 points, a victim on twice your score is 20, and
        one on half is 8.75. Clamped to 1..100 and rounded to two places, which is what makes the
        captured figures reproducible.

        A score below 1 counts as 1 so the ratio stays finite: without it, killing somebody who has
        been beaten down to zero would be worth either nothing or everything, depending on which side
        of the division they landed.
        """
        mine = max(1.0, self.stats(killer).points)
        theirs = max(1.0, self.stats(victim).points)
        points = 15.0 * ((theirs / mine) / 2) + 5
        return round(min(100.0, max(1.0, points)), 2)

    def update_xp(self, client: Client) -> None:
        """Recompute XP: what the player has actually gained, weighted by how often they die.

        The classic's arithmetic — kills times net points, divided by deaths — which is a
        kill-to-death ratio scaled by how much those kills were worth. Somebody who never dies is not
        divided by zero; they simply keep the whole figure.
        """
        record = self.stats(client)
        net = record.points_won - record.points_lost
        if record.deaths:
            record.experience = (record.kills * net) / record.deaths
        else:
            record.experience = record.kills * net

    # -- handlers ------------------------------------------------------------

    def on_kill(self, event: Event) -> None:
        killer, victim = event.client, event.target
        if killer is None or victim is None:
            return
        points = damage_points(event.data, killed=True)
        mine, theirs = self.stats(killer), self.stats(victim)
        mine.active = theirs.active = True

        mine.shots_hit += 1
        mine.damage_hit += points
        theirs.shots_got += 1
        theirs.damage_got += points
        mine.kills += 1
        theirs.deaths += 1

        value = self.score(killer, victim)
        mine.points += value
        mine.points_won += value
        theirs.points -= value
        theirs.points_lost += value

        self.update_xp(killer)
        self.update_xp(victim)

    def on_team_kill(self, event: Event) -> None:
        """A team kill costs the killer and does nothing for their damage column."""
        killer, victim = event.client, event.target
        if killer is None or victim is None:
            return
        points = damage_points(event.data, killed=True)
        mine = self.stats(killer)
        mine.active = True
        mine.shots_team_hit += 1
        mine.damage_team_hit += points
        mine.team_kills += 1

        value = self.score(killer, victim)
        mine.points -= value
        mine.points_lost += value
        self.update_xp(killer)

    def on_damage(self, event: Event) -> None:
        killer, victim = event.client, event.target
        if killer is None or victim is None:
            return
        points = damage_points(event.data, killed=False)
        mine, theirs = self.stats(killer), self.stats(victim)
        mine.active = True
        mine.shots_hit += 1
        mine.damage_hit += points
        theirs.shots_got += 1
        theirs.damage_got += points

    def on_team_damage(self, event: Event) -> None:
        killer = event.client
        if killer is None:
            return
        record = self.stats(killer)
        record.active = True
        record.shots_team_hit += 1
        record.damage_team_hit += damage_points(event.data, killed=False)

    def on_assist(self, event: Event) -> None:
        """Somebody else finished a kill this player set up. Urban Terror 4.3 is the only engine
        here that reports it, and seeing one is what makes the "A" column appear."""
        client = event.client
        if client is None:
            return
        self.reports_assists = True
        self.stats(client).assists += 1

    def on_round_start(self, event: Event) -> None:
        """Start the round's counters again, keeping whatever the config says to keep."""
        reset_score = bool(self.settings.get("reset_score"))
        reset_xp = bool(self.settings.get("reset_xp"))
        for client in self.console.clients.connected():
            record = self.stats(client)
            record.kills = record.deaths = record.assists = record.team_kills = 0
            record.shots_hit = record.damage_hit = 0
            record.shots_got = record.damage_got = 0
            record.shots_team_hit = record.damage_team_hit = 0
            record.active = False
            if reset_score:
                record.points = self._start_points()
                record.points_won = record.points_lost = 0.0
            if reset_xp:
                record.experience = record.previous_experience = 0.0
            else:
                # Filed away rather than dropped, so `!mapstats` can still show a session total
                # while `!topxp` ranks on the round being played.
                record.previous_experience += record.experience
                record.experience = 0.0

    def on_map_end(self, event: Event) -> None:
        """Announce the boards at the end of a map, for operators who asked for them.

        Nothing is said when nobody scored: the classic called its own command with no client here,
        and its empty branch then messaged that None — so a quiet map ended in a traceback.
        """
        if self.settings.get("show_awards"):
            board = self._board(lambda s: s.points)
            if board:
                self.console.say(self.message("stats_top", players=board))
        if self.settings.get("show_awards_xp"):
            board = self._board(lambda s: s.experience)
            if board:
                self.console.say(self.message("stats_top_xp", players=board))

    # -- boards --------------------------------------------------------------

    def _board(self, value: Callable[[PlayerStats], float]) -> str:
        """The top five by whatever ``value`` reads off a record, as one chat line.

        Only players who have actually done something appear. The classic asked "has this client got
        a points variable?", which was true of anyone the scoring code had merely *read* — so the
        victim of a team kill appeared on the board above the person who killed them, having done
        nothing at all.
        """
        scored = [
            (client, float(value(self.stats(client))))
            for client in self.console.clients.connected()
            if self.stats(client).active
        ]
        scored.sort(key=lambda row: row[1], reverse=True)
        return ", ".join(
            f"#{place} {client.name} [{round(points, 2)}]"
            for place, (client, points) in enumerate(scored[:TOP_COUNT], start=1)
        )

    # -- commands ------------------------------------------------------------

    @command(level=0, alias="stats")
    def cmd_mapstats(self, ctx: CommandContext) -> None:
        """mapstats [player] - kills, deaths, damage, skill and XP for this session"""
        target = ctx.client
        handle = ctx.args.strip()
        if handle:
            found = self.resolve_client(ctx, handle)
            if found is None:
                return
            target = found
        record = self.stats(target)
        key = "stats_line_with_assists" if self.reports_assists else "stats_line"
        ctx.reply(
            self.message(
                key,
                name=target.name,
                kills=record.kills,
                deaths=record.deaths,
                assists=record.assists,
                team_kills=record.team_kills,
                damage=record.damage_hit,
                skill=f"{record.points:.2f}",
                xp=round(record.previous_experience + record.experience, 2),
            )
        )

    @command(level=0, alias="ts")
    def cmd_testscore(self, ctx: CommandContext) -> None:
        """testscore <player> - how many skill points killing them would be worth"""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("usage", usage="testscore <player>"))
            return
        target = self.resolve_client(ctx, handle)
        if target is None:
            return
        if target.cid == ctx.client.cid:
            ctx.reply(self.message("stats_testscore_self"))
            return
        # A team is only a team when the engine says which one: several report an empty team for
        # everybody in a free-for-all, and calling that "the same team" would refuse to answer a
        # question that has a perfectly good answer.
        if ctx.client.team and target.team and ctx.client.team == target.team:
            ctx.reply(self.message("stats_testscore_teammate"))
            return
        ctx.reply(
            self.message(
                "stats_testscore",
                name=ctx.client.name,
                target=target.name,
                points=self.score(ctx.client, target),
            )
        )

    @command(level=2, alias="top")
    def cmd_topstats(self, ctx: CommandContext) -> None:
        """topstats - the five best skill scores this session"""
        board = self._board(lambda s: s.points)
        if not board:
            ctx.reply(self.message("stats_top_empty"))
            return
        ctx.reply(self.message("stats_top", players=board))

    @command(level=2)
    def cmd_topxp(self, ctx: CommandContext) -> None:
        """topxp - the five most experienced players this session"""
        board = self._board(lambda s: s.experience)
        if not board:
            ctx.reply(self.message("stats_top_empty"))
            return
        ctx.reply(self.message("stats_top_xp", players=board))


__all__ = ["DEFAULTS", "MESSAGES", "TOP_COUNT", "PlayerStats", "StatsPlugin"]
