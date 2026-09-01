"""Team-kill points, forgiving, and an auto-punish for whoever will not stop.

A port of the classic `tk` plugin — the most-wanted one, and the reason the parsers went first: it
needs team damage and team kills from every engine, and until those existed this would have been
written for Call of Duty and then bent once per family.

The model is the original's and it is a good one. Hurting a teammate earns the attacker points
against *that teammate*, scaled by how senior the attacker is, and only the teammate can clear
them: `!forgive`. Collect enough and the server says so publicly; keep them a little longer and the
bot bans you for as long as your level says. Nobody is punished for something a teammate has already
forgiven, which is the difference between this and a plugin that just counts kills.

Kept from the classic, deliberately: the point arithmetic (a kill is 100 damage times the level's
kill multiplier), the ×1.5/×3 penalty for firing at spawn, the grudge (a victim who refuses to
forgive), the 150%-of-the-limit immediate ban, and the halving of everybody's points when a round
ends. The captured tests are what those numbers come from — a team kill by a guest is 200 points and
`!forgiveinfo` says so.

Changed, and each for a reason:

* **No threads.** The classic armed a `threading.Timer` for the 30-second "somebody forgive him"
  window and a `OneTimeCronTab` for the halving. Both are deadlines here, checked by one scheduled
  task, which is how the admin plugin already does its warning kicks.
* **Damage is read defensively.** Four of the nine families report *no damage figure at all* — their
  parsers refuse to invent one — so a kill on Source or Frostbite is scored as the 100 damage a kill
  is, rather than crashing on a field that is absent on purpose.
* **`halflife` is not per title.** The classic hard-coded ``_round_end_games = ['bf3']`` to decide
  whether a round ending or a map ending is the moment to halve. That table would rot; here either
  signal does it, debounced, so an engine that fires both halves once.
* **The levels table is a mapping, not a section per level.** `level_guest`/`level_0` sections in an
  ini are the same statement written twice, and the classic's loader silently kept a half-parsed
  table when one of them was missing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType, damage_points
from b3.core.plugin import Plugin
from b3.domain.client import Client, PenaltyType
from b3.core.util import as_float, as_int, as_level, parse_duration

log = logging.getLogger(__name__)

#: Above this share of `max_points` the player is banned without being given a chance to be
#: forgiven. The classic's `maxPoints + maxPoints / 2`.
IMMEDIATE_BAN_RATIO = 1.5

#: Seconds between warnings for the same attacker: one minute while firing at spawn, three minutes
#: for ordinary team damage. The classic's figures, and they matter — without them a player caught in
#: a firefight with a teammate collects a warning per bullet.
SPAWN_WARN_INTERVAL = 60.0
TK_WARN_INTERVAL = 180.0

#: A round ending and a map ending are the same event on some engines and two on others. Halving
#: twice in a row would wipe points that were meant to survive the round, so a second signal inside
#: this many seconds is ignored.
HALVE_DEBOUNCE = 5.0

DEFAULTS: dict[str, object] = {
    # Points at which the server announces it; 150% of it bans immediately.
    "max_points": 400,
    # Seconds after a round starts during which firing at a teammate counts as spawn-killing.
    # 0 disables the whole idea, including the multipliers below.
    "round_grace": 7,
    # The warning given for firing at spawn — a *keyword*, expanded through the admin plugin's
    # `warn_reasons` table, so it means whatever that table says it does. Empty = do not warn.
    #
    # The classic's default here is `sfire`, which its own `plugin_admin.ini` defines as `/rule9`.
    # This bot's admin config spells the same entry `spawnfire`, and the two defaults shipped
    # disagreeing: `sfire` resolved to nothing, and the fallback warned the player with the bare
    # word "sfire". Named for the table it is actually looked up in. A config imported from the
    # classic still says `sfire`, and `warn_reasons` there carries it, so both spellings work.
    "issue_warning": "spawnfire",
    # Whether `!grudge` exists at all, and from which level. Unlike the classic, a disabled grudge
    # command still answers — it says grudges are off, rather than looking like a typo.
    "grudge_enable": True,
    "grudge_level": 0,
    # Tell the players involved privately, rather than announcing every forgive to the server.
    "private_messages": True,
    # Points in one hit above which the attacker is warned. 100 = a kill's worth.
    "damage_threshold": 100,
    # Only players *below* this level are warned. Admins are still scored and still banned.
    "warn_level": 2,
    # How long a team-damage warning lives, as a duration.
    "warn_duration": "1h",
    # Seconds between halvings of everybody's points. 0 = only when a round or map ends.
    "halflife": 0,
    # How long an over-the-limit player has to be forgiven before the ban lands.
    "forgive_grace": 30,
    # Gametypes with no teams, where "team damage" is the point of the game. The classic's list.
    "ffa_gametypes": ("dm", "ffa", "syc-ffa", "lms"),
}

#: Level -> (kill multiplier, damage multiplier, ban minutes), from the classic's own config file.
#: A player above the highest level named here is not scored at all, which is how the classic let
#: senior admins out of it — so the top entry is a statement about who is exempt, not only about
#: multipliers.
DEFAULT_LEVELS: dict[int, tuple[float, float, int]] = {
    0: (2.0, 1.0, 2),
    1: (2.0, 1.0, 2),
    2: (1.0, 0.5, 1),
    20: (1.0, 0.5, 0),
    40: (0.75, 0.5, 0),
}

MESSAGES = {
    "tk_ban_reason": "team damage over limit",
    "tk_forgiven": "{victim} has forgiven {attacker} [{points}]",
    "tk_forgiven_many": "{victim} has forgiven {attackers}",
    "tk_grudged": "{victim} has a grudge against {attacker} [{points}]",
    "tk_grudges_disabled": "grudges are turned off on this server",
    "tk_nobody_to_forgive": "nobody to forgive",
    "tk_forgive_who": "forgive who? {players}",
    "tk_info": "{name} has {points} TK point(s)",
    "tk_info_attacked": "attacked: {victims}",
    "tk_info_attacked_by": "attacked by: {attackers}",
    "tk_cleared": "{name} cleared of {points} TK point(s)",
    # Names the slot to type, because the victim is being asked to act in the two seconds before
    # they respawn and "forgive him" is not a command.
    "tk_alert": "{name} will be kicked if nobody forgives them - type !forgive {cid} [damage: {points}]",
    "tk_warning_reason": "do not attack teammates - you hit {victim} [{points}]",
    "tk_request_action": "type !fp to forgive {attacker}",
}


@dataclass(slots=True)
class Multipliers:
    """What one level's team damage costs, and how long it bans for."""

    kill: float
    damage: float
    ban_minutes: int


@dataclass
class TeamDamage:
    """One player's standing: who owes them, whom they owe, and what has been forgiven.

    Held against the client object (`Client.set_var`), so it lives exactly as long as the player's
    session does. Points are *not* stored in the database on purpose: they are a fact about this
    round, and a player who reconnects has already paid the price of losing their slot.
    """

    cid: str
    #: Attacker slot -> points they owe *me*. Cleared by me forgiving them.
    attackers: dict[str, int] = field(default_factory=dict)
    #: Slots I have hurt. My own points are the sum of what each of them holds against me, so this
    #: is the index into their records rather than a number of my own.
    attacked: set[str] = field(default_factory=set)
    #: Attackers I refuse to forgive: `!forgiveall` and `!fp` skip them.
    grudged: set[str] = field(default_factory=set)
    #: Victim slot -> the id of the warning I was given for hurting them, so forgiving can lift it.
    warnings: dict[str, int] = field(default_factory=dict)
    #: When I was last warned, and whether a ban is already queued for me.
    last_warned: float = 0.0
    ban_due: float = 0.0

    #: The slot that hurt me most recently — what `!fp` and `!forgive last` act on.
    last_attacker: str | None = None

    def damaged_by(self, cid: str, points: int) -> None:
        self.attackers[cid] = self.attackers.get(cid, 0) + points
        self.last_attacker = cid

    def forgive(self, cid: str) -> int:
        """Clear one attacker's debt to me and return what it was."""
        points = self.attackers.pop(cid, 0)
        if self.last_attacker == cid:
            self.last_attacker = None
        self.grudged.discard(cid)
        return points


class TkPlugin(Plugin):
    """Scores team damage, lets the victim forgive it, and bans whoever collects too much."""

    requires_plugins = ("admin",)
    #: The classic's `loadAfterPlugins`. `spawnkill` punishes the same act from the other end, so
    #: where both are installed it should have had its say first.
    load_after = ("spawnkill",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.levels: dict[int, Multipliers] = {
            level: Multipliers(*values) for level, values in DEFAULT_LEVELS.items()
        }
        #: Nobody above this is scored — see DEFAULT_LEVELS.
        self.max_level = max(DEFAULT_LEVELS)
        #: When everybody's points are next halved, and when they last were.
        self._halve_due = 0.0
        self._halved_at = 0.0
        #: Said once per run, not once per team kill — see `_resolve_reason`.
        self._warned_about_keyword = False

    # -- setup --------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.levels = self._load_levels(config.get("levels"))
        self.max_level = max(self.levels)

    def _load_levels(self, table: object) -> dict[int, Multipliers]:
        """Read the `levels:` mapping, or keep the classic's table and say why.

        Keys are a level number or a group keyword (`guest`, `mod`, …), because an operator thinks
        in whichever of the two their config already uses. A malformed entry is dropped **with the
        reason named**: the classic's loader raised on the first bad section and fell back to
        defaults for the lot, so one typo silently changed every level's multipliers.
        """
        if not isinstance(table, dict) or not table:
            return {level: Multipliers(*values) for level, values in DEFAULT_LEVELS.items()}
        from b3.domain.permissions import DEFAULT_GROUPS

        by_keyword = {group.keyword: group.level for group in DEFAULT_GROUPS}
        loaded: dict[int, Multipliers] = {}
        for key, values in table.items():
            level = by_keyword.get(str(key).strip().lower())
            if level is None:
                # Parsed here rather than with `as_int`, which would log its own complaint about a
                # value that is perfectly valid as a keyword — one message about one mistake.
                try:
                    level = int(str(key).strip())
                except ValueError:
                    level = -1
            if level < 0:
                log.error("tk: %r is not a level or a group keyword; ignoring it", key)
                continue
            if not isinstance(values, dict):
                log.error("tk: levels.%s must name kill/damage/ban_minutes; ignoring it", key)
                continue
            loaded[level] = Multipliers(
                kill=as_float(values.get("kill"), 1.0) or 1.0,
                damage=as_float(values.get("damage"), 0.5) or 0.5,
                ban_minutes=as_int(values.get("ban_minutes"), 0),
            )
        if not loaded:
            log.error("tk: no usable entry in levels; using the built-in table")
            return {level: Multipliers(*values) for level, values in DEFAULT_LEVELS.items()}
        return loaded

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_DAMAGE_TEAM, self.on_team_damage)
        self.subscribe(EventType.CLIENT_KILL_TEAM, self.on_team_kill)
        # Gibbing a teammate is a team kill on the engines that tell the two apart (Enemy
        # Territory), and the classic tk plugin never saw it because that event did not exist there.
        self.subscribe(EventType.CLIENT_GIB_TEAM, self.on_team_kill)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.subscribe(EventType.GAME_ROUND_START, self.on_round_start)
        for over in (EventType.GAME_ROUND_END, EventType.GAME_EXIT, EventType.GAME_MAP_CHANGE):
            self.subscribe(over, self.on_round_over)
        self._apply_grudge_level()
        # One task, two deadlines: the ban a forgive-window has run out on, and the next halving.
        # Both are checked against the clock rather than slept on, so no thread and nothing to
        # cancel when a plugin is disabled mid-window.
        self.schedule(self._tick, second="*", name="TkPlugin.deadlines")

    def _apply_grudge_level(self) -> None:
        """Put `!grudge` at its configured level, or take it away entirely.

        The classic registered the command only when `grudge_enable` was set, so on a server with
        grudges off `!grudge` answered "unknown command" — indistinguishable from a typo. Here the
        command stays and says grudges are off; only the *level* comes from config.
        """
        registered = self.console.command_registry.get("grudge")
        if registered is None:  # pragma: no cover - registration is the framework's job
            return
        registered.min_level = as_level(self.settings.get("grudge_level"), 0)

    # -- the record ----------------------------------------------------------

    def info(self, client: Client) -> TeamDamage:
        """This player's team-damage record, created on first use."""
        record = client.get_var(self, "tk")
        if not isinstance(record, TeamDamage):
            record = TeamDamage(cid=client.cid or "")
            client.set_var(self, "tk", record)
        return record

    def points(self, client: Client) -> int:
        """What this player owes: the sum of what their victims hold against them.

        Kept as the classic computed it — from the victims' records rather than a running total on
        the attacker — because forgiveness happens in the victim's record, and a second copy of the
        number is a second thing to keep in step.
        """
        total = 0
        record = self.info(client)
        for cid in list(record.attacked):
            victim = self.console.clients.get_by_cid(cid)
            if victim is None:
                record.attacked.discard(cid)
                continue
            total += self.info(victim).attackers.get(record.cid, 0)
        return total

    def multipliers(self, client: Client) -> Multipliers:
        """The multipliers for this player's level, tripled while the round is still starting.

        The highest entry at or below their level wins, so a table naming 0/2/40 covers everybody in
        between. Firing at spawn costs ×1.5 on a kill and ×3 on damage, which is the classic's
        weighting and reads the way it should: shooting a teammate who cannot even move yet is worse
        than losing a firefight with one.
        """
        applicable = [level for level in self.levels if level <= client.max_level()]
        if not applicable:
            return Multipliers(0.0, 0.0, 0)
        found = self.levels[max(applicable)]
        if self._in_spawn_grace():
            return Multipliers(found.kill * 1.5, found.damage * 3, found.ban_minutes)
        return found

    def _in_spawn_grace(self) -> bool:
        grace = as_int(self.settings.get("round_grace"), 0)
        if grace <= 0:
            return False
        return self.console.game.round_uptime(self.console.clock.now()) < grace

    # -- handlers ------------------------------------------------------------

    def on_team_damage(self, event: Event) -> None:
        self._record(event, killed=False)

    def on_team_kill(self, event: Event) -> None:
        self._record(event, killed=True)

    def _record(self, event: Event, killed: bool) -> None:
        attacker, victim = event.client, event.target
        if attacker is None or victim is None or attacker.cid is None or victim.cid is None:
            return
        if attacker.cid == victim.cid:
            return  # self-damage is not team damage, whatever the engine calls it
        if self._is_free_for_all():
            return
        if attacker.max_level() > self.max_level:
            return  # above the table: exempt, as the classic's `_maxLevel` had it

        points = damage_points(event.data, killed=killed)
        if points <= 0:
            return
        multipliers = self.multipliers(attacker)
        scaled = int(round(points * (multipliers.kill if killed else multipliers.damage)))
        if scaled <= 0:
            return

        mine, theirs = self.info(attacker), self.info(victim)
        mine.attacked.add(victim.cid)
        theirs.damaged_by(attacker.cid, scaled)

        self._maybe_warn(attacker, victim, scaled)
        self._check_limit(attacker)

    def _is_free_for_all(self) -> bool:
        """Deathmatch has no teams, so "team damage" there is just the game being played.

        Guarded because several engines report a team for everybody regardless of the gametype, so
        without this every kill in a deathmatch is a team kill and the whole server gets banned.
        """
        gametype = (self.console.game.gametype or "").strip().lower()
        if not gametype:
            return False
        return gametype in self._ffa_gametypes()

    def _ffa_gametypes(self) -> set[str]:
        """The configured free-for-all gametypes, however the operator wrote them.

        A single value is accepted as one name rather than iterated: `ffa_gametypes: dm` is a
        reasonable thing to type, and treating a string as a sequence would make it mean "d" and "m".
        """
        configured = self.settings.get("ffa_gametypes") or ()
        if isinstance(configured, str):
            return {configured.strip().lower()}
        if isinstance(configured, (list, tuple, set, frozenset)):
            return {str(name).strip().lower() for name in configured}
        log.error("tk: ffa_gametypes must be a list of gametype names; ignoring it")
        return set()

    def _maybe_warn(self, attacker: Client, victim: Client, points: int) -> None:
        """Warn the attacker, at most once per interval, and tell the victim they can forgive.

        Two different warnings, and the spawn one wins where both apply: a player shooting into the
        spawn is being told about *that*, not about damage in general.
        """
        record = self.info(attacker)
        now = self.console.clock.now()
        keyword = str(self.settings.get("issue_warning") or "")
        if self._in_spawn_grace() and keyword:
            if record.last_warned + SPAWN_WARN_INTERVAL > now:
                return
            record.last_warned = now
            minutes, text = self._resolve_reason(keyword)
            self.console.warn(attacker, reason=text, minutes=minutes)
            return
        if points <= as_int(self.settings.get("damage_threshold"), 100):
            return
        if attacker.max_level() >= as_level(self.settings.get("warn_level"), 2):
            return  # scored and bannable, but not lectured
        if record.last_warned + TK_WARN_INTERVAL > now:
            return
        record.last_warned = now
        reason = self.message("tk_warning_reason", victim=victim.name, points=points)
        minutes = self._warn_minutes()
        self.console.warn(attacker, reason=reason, minutes=minutes)
        # Remembered so that forgiving lifts the warning it was issued for, as the classic did:
        # a warning nobody can withdraw makes forgiveness worth less than it looks.
        warning_id = self._latest_warning_id(attacker)
        if warning_id is not None and victim.cid is not None:
            record.warnings[victim.cid] = warning_id
        self.console.tell(victim, self.message("tk_request_action", attacker=attacker.name))

    def _warn_minutes(self) -> int:
        try:
            return parse_duration(str(self.settings.get("warn_duration", "1h")))
        except ValueError:
            log.warning(
                "tk: invalid warn_duration %r; the warning will not expire",
                self.settings.get("warn_duration"),
            )
            return 0

    def _resolve_reason(self, keyword: str) -> tuple[int, str]:
        """Expand a warning keyword through the admin plugin's `warn_reasons` table.

        `sfire` is a keyword in that table, not a sentence, so without this the player is warned
        with the literal word — which is what the classic bot would have done had it not gone
        through `warnClient`. The admin plugin owns the table; asking it is the one plugin-to-plugin
        call here, and it degrades to the keyword itself when the table has no entry.
        """
        admin = self.console.get_plugin("admin")
        resolve = getattr(admin, "resolve_reason", None)
        if resolve is None:  # pragma: no cover - admin is a declared requirement
            return 0, keyword
        minutes, text = resolve(keyword)
        text = text or keyword
        if text == keyword and keyword not in getattr(admin, "warn_reasons", {}):
            # The keyword is not in the table, so the fallback is about to warn a player with the
            # bare word — "sfire", to somebody who has just shot a teammate. Harmless to the bot and
            # baffling in the game, and silent: exactly the kind of thing an operator sees once,
            # wonders about, and never reports. Said once, at the moment it first happens.
            if not self._warned_about_keyword:
                self._warned_about_keyword = True
                log.warning(
                    "tk: issue_warning is %r, which is not in the admin plugin's warn_reasons "
                    "table - players are being warned with that bare word. Add it to "
                    "warn_reasons, or name one that is there",
                    keyword,
                )
        return minutes, text

    def _latest_warning_id(self, client: Client) -> int | None:
        """The id of the warning just issued, so it can be lifted when the victim forgives."""
        if client.id is None:
            return None
        warnings = self.console.storage.get_active_penalties(client.id, PenaltyType.WARNING)
        return warnings[0].id if warnings else None

    def on_disconnect(self, event: Event) -> None:
        """A player leaving takes their debts and their credit with them.

        Their own points go because the slot is about to belong to somebody else, and what they were
        owed goes because a victim who has left cannot forgive — leaving it behind would hold an
        attacker at the limit with nobody able to clear them.
        """
        client = event.client
        if client is None or client.cid is None:
            return
        self._forgive_everything(client.cid)

    def on_round_start(self, event: Event) -> None:
        self._arm_halving()

    def on_round_over(self, event: Event) -> None:
        """Halve everybody's points when the round or the map ends.

        Debounced, because a round ending and a map change are one event on some engines and two on
        others — the classic picked between them with a per-title list, which is the kind of table
        that is wrong one patch later.
        """
        now = self.console.clock.now()
        if now - self._halved_at < HALVE_DEBOUNCE:
            return
        self.halve_points()

    def _arm_halving(self) -> None:
        halflife = as_int(self.settings.get("halflife"), 0)
        self._halve_due = self.console.clock.now() + halflife if halflife > 0 else 0.0

    def halve_points(self) -> None:
        """Halve every debt, and clear the ones that round down to nothing.

        Forgiveness by decay: a player who stops team-killing works their way back to a clean sheet
        without needing anybody to type anything, which is what keeps this plugin from turning one
        bad round into a ban half an hour later.
        """
        self._halved_at = self.console.clock.now()
        for client in self.console.clients.connected():
            record = self.info(client)
            for cid, points in list(record.attackers.items()):
                halved = int(round(points / 2))
                if halved <= 0:
                    self._forgive(client, cid, silent=True)
                else:
                    record.attackers[cid] = halved
        self._arm_halving()

    def _tick(self) -> None:
        """The two deadlines: a ban whose forgive window has closed, and the next halving."""
        now = self.console.clock.now()
        for client in self.console.clients.connected():
            record = self.info(client)
            if record.ban_due and now >= record.ban_due:
                record.ban_due = 0.0
                self._ban_if_still_over(client)
        if self._halve_due and now >= self._halve_due:
            self.halve_points()

    # -- the limit -----------------------------------------------------------

    def _check_limit(self, attacker: Client) -> None:
        """Announce, or ban outright, once an attacker is over the limit."""
        maximum = as_int(self.settings.get("max_points"), 400)
        points = self.points(attacker)
        if points < maximum:
            return
        record = self.info(attacker)
        if points >= maximum * IMMEDIATE_BAN_RATIO:
            # Past this there is nothing to discuss: they have killed the same team three times over
            # while being told about it. Their points are cleared first so that the victims are not
            # left holding a debt against somebody who has already paid for it.
            self._forgive_everything(attacker.cid or "")
            self._tempban(attacker, self.multipliers(attacker).ban_minutes)
            return
        if record.ban_due:
            return  # already announced; the window is running
        grace = as_int(self.settings.get("forgive_grace"), 30)
        record.ban_due = self.console.clock.now() + grace
        self.console.say(self._alert_text(attacker, points))

    def _alert_text(self, attacker: Client, points: int) -> str:
        """The public alert, naming who they hurt so a victim knows it is their call."""
        text = self.message("tk_alert", name=attacker.name, cid=attacker.cid or "?", points=points)
        victims = self._victim_list(attacker)
        if victims:
            text += " — " + self.message("tk_info_attacked", victims=victims)
        return text

    def _ban_if_still_over(self, client: Client) -> None:
        """Carry out the ban the alert promised, unless somebody forgave them in the meantime.

        The length is the level's ban time multiplied by how many teammates they hurt, so somebody
        who took out four people is gone for four times as long as somebody who took out one.
        """
        maximum = as_int(self.settings.get("max_points"), 400)
        if self.points(client) < maximum:
            return
        record = self.info(client)
        victims = max(1, len(record.attacked))
        minutes = self.multipliers(client).ban_minutes * victims
        self._forgive_everything(client.cid or "")
        self._tempban(client, minutes)

    def _tempban(self, client: Client, minutes: int) -> None:
        """Ban for team damage. A level whose `ban_minutes` is 0 is never banned for it."""
        if minutes <= 0:
            log.info("tk: %s is over the limit but their level is not banned for it", client.name)
            return
        self.console.tempban(client, minutes, reason=self.message("tk_ban_reason"))

    # -- forgiving -----------------------------------------------------------

    def _forgive(self, victim: Client, attacker_cid: str, silent: bool = False) -> int:
        """Clear what one attacker owes this victim, and lift the warning it earned them."""
        record = self.info(victim)
        points = record.forgive(attacker_cid)
        attacker = self.console.clients.get_by_cid(attacker_cid)
        if attacker is not None and victim.cid is not None:
            attacker_record = self.info(attacker)
            attacker_record.attacked.discard(victim.cid)
            warning_id = attacker_record.warnings.pop(victim.cid, None)
            if warning_id is not None:
                # The forgiven warning stops counting towards the admin plugin's escalation too,
                # which is the point: two plugins punishing one act, once forgiven, is once too many.
                self.console.storage.disable_penalty(warning_id)
            if not attacker_record.attacked:
                attacker_record.ban_due = 0.0
        if not silent:
            self._announce(
                "tk_forgiven",
                victim,
                attacker,
                points,
                attacker_name=attacker.name if attacker else attacker_cid,
            )
        return points

    def _forgive_everything(self, attacker_cid: str) -> int:
        """Clear every debt this player owes, wherever it is recorded. Returns the total."""
        if not attacker_cid:
            return 0
        total = 0
        for client in self.console.clients.connected():
            total += self._forgive(client, attacker_cid, silent=True)
        attacker = self.console.clients.get_by_cid(attacker_cid)
        if attacker is not None:
            record = self.info(attacker)
            record.attacked.clear()
            record.warnings.clear()
            record.ban_due = 0.0
        return total

    def _announce(
        self,
        key: str,
        victim: Client,
        attacker: Client | None,
        points: int,
        attacker_name: str,
    ) -> None:
        """Say a forgive or a grudge, to the two involved or to everybody.

        Private is the default and it is the better one: a forgive announced to the server is a
        second public mention of something the victim has just decided to let go.
        """
        text = self.message(key, victim=victim.name, attacker=attacker_name, points=points)
        if not self.settings.get("private_messages"):
            self.console.say(text)
            return
        # `smart_say` is not used here: this is addressed to two specific people, and a dead victim
        # still needs to see the answer to the command they just typed.
        self.console.tell(victim, text)
        if attacker is not None:
            self.console.tell(attacker, text)

    def _victim_list(self, attacker: Client) -> str:
        """ "Bob (200), Mike (50)" — who this attacker has hurt, and by how much."""
        record = self.info(attacker)
        parts = []
        for cid in sorted(record.attacked):
            victim = self.console.clients.get_by_cid(cid)
            if victim is None:
                continue
            parts.append(f"{victim.name} ({self.info(victim).attackers.get(record.cid, 0)})")
        return ", ".join(parts)

    def _attacker_list(self, victim: Client) -> str:
        """ "[3] Bob [200]" — who owes this player, with the slot to type at `!forgive`.

        A grudged attacker is marked, because the reason `!forgiveall` left them behind should be
        visible without having to remember typing `!grudge`.
        """
        record = self.info(victim)
        parts = []
        for cid, points in record.attackers.items():
            attacker = self.console.clients.get_by_cid(cid)
            if attacker is None:
                continue
            marker = " (grudged)" if cid in record.grudged else ""
            parts.append(f"[{cid}] {attacker.name} [{points}]{marker}")
        return ", ".join(parts)

    def _targets(self, victim: Client, handle: str) -> list[str]:
        """Which of my attackers an argument means: all of one, `last`, a slot, or a partial name."""
        record = self.info(victim)
        wanted = handle.strip().lower()
        if not wanted:
            return list(record.attackers) if len(record.attackers) == 1 else []
        if wanted == "last":
            return [record.last_attacker] if record.last_attacker else []
        if wanted in record.attackers:
            return [wanted]
        found = []
        for cid in record.attackers:
            attacker = self.console.clients.get_by_cid(cid)
            if attacker is not None and wanted in attacker.name.lower():
                found.append(cid)
        return found

    # -- commands ------------------------------------------------------------

    @command(level=0, alias="f")
    def cmd_forgive(self, ctx: CommandContext) -> None:
        """forgive [player] - clear the team damage somebody did to you"""
        record = self.info(ctx.client)
        if not record.attackers:
            ctx.reply(self.message("tk_nobody_to_forgive"))
            return
        targets = self._targets(ctx.client, ctx.args)
        if not targets:
            # More than one attacker and no name given: list them rather than guessing which of
            # them the victim meant to let off.
            ctx.reply(self.message("tk_forgive_who", players=self._attacker_list(ctx.client)))
            return
        for cid in targets:
            self._forgive(ctx.client, cid)

    @command(level=0, alias="fp")
    def cmd_forgiveprev(self, ctx: CommandContext) -> None:
        """forgiveprev - forgive whoever hit you last"""
        record = self.info(ctx.client)
        last = record.last_attacker
        if last is None or last in record.grudged:
            ctx.reply(self.message("tk_nobody_to_forgive"))
            return
        self._forgive(ctx.client, last)

    @command(level=0, alias="fa")
    def cmd_forgiveall(self, ctx: CommandContext) -> None:
        """forgiveall - forgive everybody who has hit you, except anyone you hold a grudge against"""
        record = self.info(ctx.client)
        forgiven = []
        for cid in list(record.attackers):
            if cid in record.grudged:
                continue
            attacker = self.console.clients.get_by_cid(cid)
            points = self._forgive(ctx.client, cid, silent=True)
            if attacker is not None and points:
                forgiven.append(f"{attacker.name} [{points}]")
                if self.settings.get("private_messages"):
                    self.console.tell(
                        attacker,
                        self.message(
                            "tk_forgiven",
                            victim=ctx.client.name,
                            attacker=attacker.name,
                            points=points,
                        ),
                    )
        if not forgiven:
            ctx.reply(self.message("tk_nobody_to_forgive"))
            return
        text = self.message(
            "tk_forgiven_many", victim=ctx.client.name, attackers=", ".join(forgiven)
        )
        if self.settings.get("private_messages"):
            ctx.reply(text)
        else:
            self.console.say(text)

    @command(level=0, alias="fl")
    def cmd_forgivelist(self, ctx: CommandContext) -> None:
        """forgivelist - list everybody who has hit you, with the slot to forgive them by"""
        listed = self._attacker_list(ctx.client)
        if not listed:
            ctx.reply(self.message("tk_nobody_to_forgive"))
            return
        ctx.reply(self.message("tk_forgive_who", players=listed))

    @command(level=20, alias="fi")
    def cmd_forgiveinfo(self, ctx: CommandContext) -> None:
        """forgiveinfo <player> - show somebody's team damage, both directions"""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("usage", usage="forgiveinfo <player>"))
            return
        target = self.resolve_client(ctx, handle)
        if target is None:
            return
        text = self.message("tk_info", name=target.name, points=self.points(target))
        victims = self._victim_list(target)
        if victims:
            text += " — " + self.message("tk_info_attacked", victims=victims)
        attackers = self._attacker_list(target)
        if attackers:
            text += " — " + self.message("tk_info_attacked_by", attackers=attackers)
        ctx.reply(text)

    @command(level=60, alias="fc")
    def cmd_forgiveclear(self, ctx: CommandContext) -> None:
        """forgiveclear <player> - wipe somebody's team damage points"""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("usage", usage="forgiveclear <player>"))
            return
        target = self.resolve_client(ctx, handle)
        if target is None:
            return
        points = self._forgive_everything(target.cid or "")
        text = self.message("tk_cleared", name=target.name, points=points)
        if self.settings.get("private_messages"):
            ctx.reply(text)
            if target.cid != ctx.client.cid:
                self.console.tell(target, text)
        else:
            self.console.say(text)

    @command(level=0)
    def cmd_grudge(self, ctx: CommandContext) -> None:
        """grudge [player] - refuse to forgive somebody, so !forgiveall leaves them alone"""
        if not self.settings.get("grudge_enable"):
            ctx.reply(self.message("tk_grudges_disabled"))
            return
        record = self.info(ctx.client)
        if not record.attackers:
            ctx.reply(self.message("tk_nobody_to_forgive"))
            return
        targets = self._targets(ctx.client, ctx.args)
        if not targets:
            ctx.reply(self.message("tk_forgive_who", players=self._attacker_list(ctx.client)))
            return
        for cid in targets:
            record.grudged.add(cid)
            attacker = self.console.clients.get_by_cid(cid)
            self._announce(
                "tk_grudged",
                ctx.client,
                attacker,
                record.attackers.get(cid, 0),
                attacker_name=attacker.name if attacker else cid,
            )


__all__ = ["DEFAULTS", "DEFAULT_LEVELS", "MESSAGES", "Multipliers", "TeamDamage", "TkPlugin"]
