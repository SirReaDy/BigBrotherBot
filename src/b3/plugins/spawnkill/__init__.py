"""Punishes shooting players who have only just spawned.

A port of the classic `spawnkill` plugin. A player who has been alive for less than a couple of
seconds cannot fight back, and camping the place they appear is the most demoralising thing anybody can
do on a small map. Two windows, each with its own penalty: shooting somebody that recently (`hit`) and
killing them (`kill`).

Kept from the classic: the two windows, the level that is exempt, the configurable penalty and reason
per window, and the rule that a player with no recorded spawn cannot be spawn-killed — which is what
makes a bot restart mid-round safe.

Changed, and the first two are faults:

* **A team spawn-kill was free.** `onClientKillTeam` checked the killer's level and whether the victim
  had ever spawned, applied **no penalty at all**, and did not look at the delay — so it announced a
  spawn-kill for every team kill of anybody who had spawned at any point in the map, while punishing
  none of them. Team kills and team damage are held to the same windows as the rest here, which is the
  point of the plugin: from the victim's side it makes no difference whose bullet it was.
* **Gibs were not kills.** Like every other plugin in this family, it watched `EVT_CLIENT_KILL` alone,
  so on the engines that report a gib as its own event a spawn-kill was not a kill.
* **It is not Urban-Terror-only.** The classic declared `requiresParsers = ['iourt42', 'iourt43']`.
  What it needs is `CLIENT_SPAWN`, which the Quake 3 family and Frostbite report; where an engine never
  reports a spawn, nothing is ever recorded and so nothing is punished — the correct outcome, arrived at
  without a list of game names.
* **The settings are per instance.** The classic held them in a dict on the *class* and mutated it in
  place at load, so the defaults were permanently overwritten and two instances shared one table.

**`slap`, `nuke` and `kill` work where the engine has the verb.** The classic offered them through
`inflictCustomPenalty`, which sent a command into the dark on any title. They are `GameProfile.player_verbs`
here, asked for with `Console.supports_verb` before being offered: configuring one on a title that has
none is refused at load, by name, and falls back to `warn` — because a penalty that silently does
nothing is worse than one that says it cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Penalties every engine can carry out, because they are the bot's own.
PENALTIES = ("warn", "kick", "tempban")

#: Penalties that are *engine verbs* rather than recorded penalties — the classic's
#: `inflictCustomPenalty` set. Available only where the title declares one (`GameProfile.player_verbs`,
#: which today means the Urban Terror family), and refused at load with a reason where it does not.
ENGINE_PENALTIES = ("slap", "nuke", "kill")

#: A hit that counts against the window. Team damage included, which the classic left out: a
#: spawning player shot by their own side is just as dead.
HIT_EVENTS = (EventType.CLIENT_DAMAGE, EventType.CLIENT_DAMAGE_TEAM)

#: A kill that counts against the window — all four spellings, where the classic saw one.
KILL_EVENTS = (
    EventType.CLIENT_KILL,
    EventType.CLIENT_KILL_TEAM,
    EventType.CLIENT_GIB,
    EventType.CLIENT_GIB_TEAM,
)

#: The classic's own defaults, per window.
DEFAULTS: dict[str, dict[str, object]] = {
    "hit": {
        # At or above this level, nobody is checked. 40 is `admin`.
        "max_level": 40,
        # Seconds after spawning during which a hit counts as shooting a spawning player.
        "delay": 2,
        "penalty": "warn",
        # Minutes: how long the warning lives, or how long a tempban lasts.
        "duration": 3,
        "reason": "do not shoot players who have just spawned",
    },
    "kill": {
        "max_level": 40,
        "delay": 3,
        "penalty": "warn",
        "duration": 5,
        "reason": "spawn-killing is not allowed here",
    },
}


@dataclass(frozen=True, slots=True)
class Window:
    """One of the two windows, as it will actually be applied."""

    name: str
    max_level: int
    delay: int
    penalty: str
    duration: int
    reason: str


class SpawnkillPlugin(Plugin):
    """Warns, kicks or bans players who shoot somebody who has only just spawned."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.windows: dict[str, Window] = {}

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.windows = {name: self._window(name, config.get(name)) for name in ("hit", "kill")}

    def _window(self, name: str, section: object) -> Window:
        defaults = DEFAULTS[name]
        values = {**defaults, **(section if isinstance(section, dict) else {})}
        penalty = str(values.get("penalty") or "warn").strip().lower()
        if penalty in ENGINE_PENALTIES and not self.console.supports_verb(penalty):
            log.error(
                "spawnkill: %s.penalty is %r, which this game has no verb for; using %r instead. "
                "Available here: %s",
                name,
                penalty,
                defaults["penalty"],
                ", ".join(
                    (*PENALTIES, *(v for v in ENGINE_PENALTIES if self.console.supports_verb(v)))
                ),
            )
            penalty = str(defaults["penalty"])
        elif penalty not in PENALTIES and penalty not in ENGINE_PENALTIES:
            log.error(
                "spawnkill: %s.penalty is %r, which is not one of %s; using %r",
                name,
                penalty,
                ", ".join(PENALTIES),
                defaults["penalty"],
            )
            penalty = str(defaults["penalty"])
        delay = as_int(values.get("delay"), as_int(defaults["delay"], 2))
        if delay < 0:
            log.warning("spawnkill: %s.delay cannot be negative; using 0", name)
            delay = 0
        return Window(
            name=name,
            max_level=as_level(values.get("max_level"), as_int(defaults["max_level"], 40)),
            delay=delay,
            penalty=penalty,
            duration=max(0, as_int(values.get("duration"), as_int(defaults["duration"], 3))),
            reason=str(values.get("reason") or defaults["reason"]),
        )

    def on_startup(self) -> None:
        self.subscribe(EventType.CLIENT_SPAWN, self.on_spawn)
        for event_type in HIT_EVENTS:
            self.subscribe(event_type, self.on_hit)
        for event_type in KILL_EVENTS:
            self.subscribe(event_type, self.on_kill)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.on_load_config()

    # -- the record ----------------------------------------------------------

    def spawned_at(self, client: Client) -> float | None:
        recorded = client.get_var(self, "spawned_at")
        return recorded if isinstance(recorded, (int, float)) else None

    def on_spawn(self, event: Event) -> None:
        if event.client is not None:
            event.client.set_var(self, "spawned_at", self.console.clock.now())

    def on_disconnect(self, event: Event) -> None:
        if event.client is not None:
            event.client.del_var(self, "spawned_at")

    # -- checking ------------------------------------------------------------

    def on_hit(self, event: Event) -> None:
        self.check("hit", event)

    def on_kill(self, event: Event) -> None:
        self.check("kill", event)

    def check(self, name: str, event: Event) -> bool:
        """Punish the attacker if the victim had only just spawned. True if anything was done."""
        window = self.windows.get(name)
        attacker, victim = event.client, event.target
        if window is None or attacker is None or victim is None or attacker is victim:
            return False
        if attacker.max_level() >= window.max_level:
            return False
        spawned = self.spawned_at(victim)
        if spawned is None:
            # Never seen to spawn, so there is no window to be inside. This is what makes a bot
            # started mid-round safe: it has watched nobody spawn, so it accuses nobody.
            return False
        if self.console.clock.now() - spawned >= window.delay:
            return False
        log.info(
            "spawnkill: %s %s %s %.1fs after they spawned",
            attacker.name,
            "shot" if name == "hit" else "killed",
            victim.name,
            self.console.clock.now() - spawned,
        )
        self.punish(window, attacker)
        return True

    def punish(self, window: Window, client: Client) -> None:
        if window.penalty in ENGINE_PENALTIES:
            # A verb rather than a penalty: nothing is recorded, because nothing happened that a
            # future admin needs to read. The reason still goes to the player where the engine's verb
            # carries one; `slap` and `nuke` do not, so the log line is the record.
            self.console.apply_verb(window.penalty, client)
        elif window.penalty == "kick":
            self.console.kick(client, reason=window.reason)
        elif window.penalty == "tempban":
            self.console.tempban(client, minutes=window.duration, reason=window.reason)
        else:
            self.console.warn(client, reason=window.reason, minutes=window.duration)


__all__ = [
    "DEFAULTS",
    "ENGINE_PENALTIES",
    "HIT_EVENTS",
    "KILL_EVENTS",
    "PENALTIES",
    "SpawnkillPlugin",
    "Window",
]
