"""Announces the first kill of the map, and the first team kill.

A port of the classic `firstkill` plugin. Three announcements, once each per map: first blood, first
blood by headshot, and the first person to shoot their own side. It is a small thing that makes the
start of a map feel like the start of something.

Kept from the classic: the three announcements, the three switches with a command each, the headshot
exclusions (a bleed-out and a grenade are not headshots, whatever the last hit recorded was), and the
reset at the start of a new map.

Changed, and the first two are faults:

* **The commands reported their own state wrongly.** All three were written
  `'^7Firstkill: %s' % '^2ON' if self._firstkill else '^1OFF'`, and `%` binds tighter than the
  conditional — so the label belonged to the ON branch only, and `!firstkill` on a switched-off
  plugin answered with the bare word "OFF" and no idea what it referred to.
* **Headshots are found from what the engine reported, not from the game's name.** The classic gated
  the whole feature on `gameName in ('iourt41', 'iourt42', 'iourt43')` and read `event.data[2]` — a
  positional index into one parser's payload tuple. Call of Duty has stated its hit location on every
  kill line for twenty years and Source states a headshot flag; both work here, and an engine that
  reports neither simply never announces one instead of the plugin having to know which engines those
  are. (Urban Terror needed a parser change to keep working at all: its `Kill:` line names the weapon
  and not the part of the body, so the hit location is threaded from the `Hit:` line before it — see
  `Q3Parser._last_hit`.)
* **`say_big` instead of three branches on the game's name.** The classic sent `bigtext` on Urban
  Terror, `say` on Call of Duty and `saybig` elsewhere. Which verb a title has is a fact about the
  title: `GameProfile.saybig_template` holds it, and falls back to `say` where there is none.
* **The reset is not tied to one event.** It hung off `EVT_GAME_MAP_CHANGE` alone; `reset_on` reads
  the end of a map as well, and takes `round` for the titles where players count first blood per
  round.
* **A command that cannot work is not deleted.** `!firsths` was unregistered from the admin plugin's
  table on any non-UrT game — a plugin reaching into another plugin's registry to remove something.
  There is nothing engine-specific about it now.
"""

from __future__ import annotations

import logging
from typing import Any

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Where a shot has to land to be a headshot. Urban Terror names both (its helmet is a head hit with
#: armour on), Call of Duty says `head`, and Source's parser writes `head` when the engine sets its
#: headshot flag.
HEADSHOT_LOCATIONS = ("head", "helmet")

#: In a means-of-death, this says headshot on its own — Call of Duty's `MOD_HEAD_SHOT`.
HEADSHOT_MOD = "HEAD_SHOT"

#: Deaths that are not headshots however the last hit landed, and the classic's own list. Bleeding to
#: death is not a shot to the head, and a grenade does not have a hit location worth the name.
NOT_HEADSHOTS = ("UT_MOD_BLED", "UT_MOD_HEGRENADE")

DEFAULTS: dict[str, object] = {
    "announce_first_kill": True,
    "announce_first_teamkill": True,
    # On wherever the engine says enough to tell. The classic shipped it on and then refused to read
    # the setting at all unless the game was Urban Terror.
    "announce_first_headshot": True,
    # When the counters go back to zero: `map` (a map change or the end of one), `round`, or `never`.
    "reset_on": "map",
    # Level for the three switches. superadmin, as the classic's config had it: they change what the
    # whole server sees.
    "min_level": 100,
}

MESSAGES = {
    "firstkill_kill": "first kill: {player} killed {victim}",
    "firstkill_headshot": "first kill, by headshot: {player} killed {victim}",
    "firstkill_teamkill": "first team kill: {player} shot {victim}",
    "firstkill_state": "{what}: {state}",
    "firstkill_usage": "expecting on or off",
}

#: The three switches, by the command that toggles them: setting key and what to call it out loud.
SWITCHES = {
    "firstkill": ("announce_first_kill", "first kill"),
    "firsttk": ("announce_first_teamkill", "first team kill"),
    "firsths": ("announce_first_headshot", "first kill by headshot"),
}


def is_headshot(data: Any) -> bool:
    """Whether a kill payload describes a shot to the head.

    Read with ``getattr`` rather than by position, which is the whole point: the classic indexed
    ``event.data[2]`` into one parser's tuple, so the feature could only ever work on the one game
    whose payload had that shape, and would have raised on any other.
    """
    mod = str(getattr(data, "means_of_death", "") or "").upper()
    if mod in NOT_HEADSHOTS:
        return False
    if HEADSHOT_MOD in mod:
        return True
    location = str(getattr(data, "hit_location", "") or "").strip().lower()
    return location in HEADSHOT_LOCATIONS


class FirstkillPlugin(Plugin):
    """Announces first blood, first headshot and first team kill, once each per map."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: Whether each announcement is still owed on this map.
        self._kill_pending = True
        self._teamkill_pending = True

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        reset = str(self.settings.get("reset_on") or "map").strip().lower()
        if reset not in ("map", "round", "never"):
            log.warning("firstkill: reset_on %r is not map, round or never; using map", reset)
            reset = "map"
        self.settings["reset_on"] = reset
        level = as_level(self.settings.get("min_level"), 100)
        for name in SWITCHES:
            registered = self.console.command_registry.get(name)
            if registered is not None:
                registered.min_level = level

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_KILL, self.on_kill)
        self.subscribe(EventType.CLIENT_GIB, self.on_kill)
        self.subscribe(EventType.CLIENT_KILL_TEAM, self.on_team_kill)
        self.subscribe(EventType.CLIENT_GIB_TEAM, self.on_team_kill)
        for event_type in (EventType.GAME_EXIT, EventType.GAME_MAP_CHANGE):
            self.subscribe(event_type, self.on_map_over)
        self.subscribe(EventType.GAME_ROUND_END, self.on_round_over)
        self.on_load_config()

    # -- handlers ------------------------------------------------------------

    def on_kill(self, event: Event) -> None:
        """First blood. A headshot gets its own line, if this engine said enough to know."""
        if not self._kill_pending:
            return
        killer, victim = event.client, event.target
        if killer is None or victim is None or killer is victim:
            return  # a death with nobody to credit is not first blood
        self._kill_pending = False
        if not self.settings.get("announce_first_kill"):
            return
        headshot = self.settings.get("announce_first_headshot") and is_headshot(event.data)
        self.announce("firstkill_headshot" if headshot else "firstkill_kill", killer, victim)

    def on_team_kill(self, event: Event) -> None:
        if not self._teamkill_pending:
            return
        killer, victim = event.client, event.target
        if killer is None or victim is None:
            return
        self._teamkill_pending = False
        if not self.settings.get("announce_first_teamkill"):
            return
        self.announce("firstkill_teamkill", killer, victim)

    def on_map_over(self, event: Event) -> None:
        if self.settings.get("reset_on") in ("map", "round"):
            self.reset()

    def on_round_over(self, event: Event) -> None:
        if self.settings.get("reset_on") == "round":
            self.reset()

    def reset(self) -> None:
        self._kill_pending = True
        self._teamkill_pending = True

    def announce(self, key: str, killer: Client, victim: Client) -> None:
        """Centre-screen where the engine has a verb for it, and plain chat where it has not."""
        self.console.say_big(self.message(key, player=killer.name, victim=victim.name))

    # -- the switches --------------------------------------------------------

    @command("firstkill", level=100)
    def cmd_firstkill(self, ctx: CommandContext) -> None:
        """firstkill [on|off] - announce the first kill of a map"""
        self._switch(ctx, "firstkill")

    @command("firsttk", level=100)
    def cmd_firsttk(self, ctx: CommandContext) -> None:
        """firsttk [on|off] - announce the first team kill of a map"""
        self._switch(ctx, "firsttk")

    @command("firsths", level=100)
    def cmd_firsths(self, ctx: CommandContext) -> None:
        """firsths [on|off] - announce the first kill of a map when it is a headshot"""
        self._switch(ctx, "firsths")

    def _switch(self, ctx: CommandContext, name: str) -> None:
        key, label = SWITCHES[name]
        wanted = ctx.args.strip().lower()
        if wanted and wanted not in ("on", "off"):
            ctx.reply(self.message("firstkill_usage"))
            return
        if wanted:
            self.settings[key] = wanted == "on"
        # One message either way, and the label is in it — which is the fault this replaces: the
        # classic's `'label: %s' % 'ON' if x else 'OFF'` answered a switched-off plugin with the bare
        # word "OFF".
        state = "on" if self.settings.get(key) else "off"
        ctx.reply(self.message("firstkill_state", what=label, state=state))


__all__ = [
    "DEFAULTS",
    "HEADSHOT_LOCATIONS",
    "HEADSHOT_MOD",
    "MESSAGES",
    "NOT_HEADSHOTS",
    "SWITCHES",
    "FirstkillPlugin",
    "is_headshot",
]
