"""Black Ops's own admin commands — playlists, the map exclusion list, and the DLC switches.

A port of the classic `poweradmincod7` plugin, and the title is the whole point: Call of Duty: Black
Ops is the one engine here that an admin cannot simply tell to load a map. A **ranked** server takes
its map and gametype from a *playlist*, and the only lever over it is `playlist_excludeMap` — a list
of maps the playlist may not pick. So "set the next map" on a ranked server means "exclude the other
twenty-five", and the operator's own exclusion list has to be put back afterwards. That mechanism is
what this plugin exists for.

Kept from the classic: every command and its config-declared level and alias, the playlist numbering,
Treyarch's off-by-one DLC numbering (`!pasetdlc 1` writes `playlist_excludeDlc2`), and the split
between what a ranked server will obey and what an unranked one will.

Changed, and the first is why the gametype command never worked:

* **A dvar was written with a bare assignment.** `!pagametype` sent `g_gametype tdm` as an rcon
  command. Black Ops does not take a plain assignment — it wants `setadmindvar`, which is exactly the
  fault this repository already records against this title's `g_logsync` — so the gametype was never
  changed and the map restarted anyway. Every cvar written here goes through `Console.set_cvar`, which
  is what knows the title's spelling.
* **Nothing sleeps, and nothing is a thread.** The classic called `time.sleep` inside four command
  handlers — two seconds for a restart, five for a gametype change, and **one second per playlist**
  in `!pagetplaylists`, which froze the whole bot for twenty-five seconds and sent the admin
  twenty-five separate chat lines. The playlists are one message now; the restarts are a deadline on
  the plugin's one scheduled pass.
* **`!paload` did not run on the thread it made.** `threading.Thread(target=self._configloader(data))`
  *calls* the loader and hands the thread its return value, so the whole file was sent from the
  command handler with a one-second sleep per line — a bot frozen for as long as the file is long —
  and the admin was told "successfully loaded" before a single line had been acknowledged. Here the
  lines go out one per second from the scheduled pass, the admin is told up front how long that will
  take, and told again when it is done.
* **The exclusion list was restored as the word "None".** `_admin_excluded_maps` was only assigned if
  `playlist_excludeMap` could be read at startup, and the round-start restore interpolated it
  regardless — so on a server that did not answer, `!pasetmap` left `playlist_excludeMap "None"`
  behind. Nothing is read at startup here: the operator's list is read at the moment `!pasetmap`
  takes it away, which also means it is current rather than however it looked when the bot booted.
* **Two crashes on ordinary typing.** `!paset g_gametype` with no value raised `IndexError` inside the
  handler, and `!pasetplaylist 2.7` passed a `float()` check and then raised `ValueError` on
  `int('2.7')`. Both are answered now.
* **`!paget` replied with the cvar object, not its value**, so an admin saw a repr rather than a
  number.
* **`!pasetdlc` with anything but `on`/`off` printed to the bot's stdout** and said nothing at all to
  the admin who typed it.
* **A map is resolved rather than exact-matched.** The classic accepted the console id, the id without
  `mp_`, or the friendly name spelled exactly — so `!pasetmap firing` failed where `!pasetmap
  firing range` worked. Maps are matched the way every other map argument in this bot is matched, and
  two candidates are a question rather than a coin toss. `!paexcludemaps` accepts friendly names too,
  where the classic took console ids only.
* **`!paexcludemaps` pointed the admin at the wrong help.** Its "missing parameter" message named
  `!help pasetplaylist`.
* **`sv_ranked` and `playlist_enabled` are read where they are needed**, not once in startup. A cvar
  read at startup with no error handling takes the whole plugin down on a server that does not answer
  for it — the fault that cost `poweradminurt` all forty-nine of its commands — and reading these two
  once also meant a bot restart was needed after an operator changed either.
* **The stock map table lives on the title, not in here.** `GameProfile.map_names` is what
  `!maps`, `!nextmap` and `callvote`'s announcements consult, and Black Ops is the one Call of Duty
  title whose ids and names diverge; declaring the table here would have left every one of those
  printing `mp_nuked`.

**Not ported.** `!paversion` printed the plugin's version string — `!plugin info` answers that for
every plugin. `!paident` showed a player's IP and guid at level 40, and `@paident` broadcast them to
the whole server; `!clientinfo` is that command, at level 80, and it is private.

**One thing this cannot fix.** `!pasetmap` leaves twenty-five maps excluded until the next round
starts. If the bot stops in that window the server keeps them excluded, and only an operator can put
the list back. The list is restored when the plugin is disabled as well as at the round start, and the
reply says the window exists, which is as far as a bot with no memory across restarts can go.

`!paset` and `!paget` are the same two commands `poweradminurt` offers. Neither plugin is restricted
by title, so an operator who loads both on a Black Ops server will find the second one **refused**
those two names, with both plugins named in the log. Both do the same thing, so whichever keeps them
works.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import match_names
from b3.domain.client import Client
from b3.parsers.cod.maps import COD7_MAPS

log = logging.getLogger(__name__)

#: The cvar that decides which maps a playlist may pick. The only lever over the map on a ranked
#: Black Ops server, and the reason this plugin exists.
EXCLUDE_CVAR = "playlist_excludeMap"

#: How long the bot waits after announcing a restart, so the players reading it get a moment. The
#: classic's own figures — two seconds for a restart, five for a gametype change, which also has to
#: leave the dvar time to take before the map reloads.
RESTART_SECONDS = 2.0
GAMETYPE_SECONDS = 5.0

#: Lines of a loaded server config sent per pass, and the pass runs once a second. A Black Ops rcon
#: socket drops a flood, which is why the classic paced this at all; what it got wrong was doing the
#: pacing with `time.sleep` in the command handler.
CONFIG_LINES_PER_PASS = 1

#: Black Ops names its game modes in a playlist rather than a rotation, and the playlist numbering is
#: the engine's. Ranked servers pick from these; `!pasetplaylist` writes the number.
PLAYLISTS: dict[int, str] = {
    1: "Team Deathmatch",
    2: "Free For All",
    3: "Capture The Flag",
    4: "Search & Destroy",
    5: "Headquarters",
    6: "Domination",
    7: "Sabotage",
    8: "Demolition",
    9: "Hardcore Team Deathmatch",
    10: "Hardcore Free For All",
    11: "Hardcore Capture The Flag",
    12: "Hardcore Search & Destroy",
    13: "Hardcore Headquarters",
    14: "Hardcore Domination",
    15: "Hardcore Sabotage",
    16: "Hardcore Demolition",
    17: "Barebones Team Deathmatch",
    18: "Barebones Free For All",
    19: "Barebones Capture The Flag",
    20: "Barebones Search & Destroy",
    21: "Barebones Headquarters",
    22: "Barebones Domination",
    23: "Barebones Sabotage",
    24: "Barebones Demolition",
    25: "Team Tactical",
}

#: `g_gametype` on an unranked server, where a gametype can be set directly instead of chosen by a
#: playlist. The four at the end are Black Ops's party modes.
GAMETYPES: dict[str, str] = {
    "dm": "Free-For-All",
    "tdm": "Team Deathmatch",
    "sd": "Search and Destroy",
    "dom": "Domination",
    "sab": "Sabotage",
    "ctf": "Capture the Flag",
    "koth": "Headquarters",
    "dem": "Demolition",
    "oic": "One in the Chamber",
    "hlnd": "Sticks and Stones",
    "gun": "Gun Game",
    "shrp": "Sharpshooter",
}

DEFAULTS: dict[str, object] = {
    # Where this server's `.cfg` files are, for `!palistcfg` and `!paload`. No default: the classic
    # used the bot's own config folder, which a plugin here cannot know and should not assume — and
    # naming it explicitly lets an operator keep server configs somewhere other than beside `b3.yaml`.
    "config_dir": "",
}

MESSAGES = {
    "pac7_playlist": "playlist {number}: {name}",
    "pac7_playlist_unknown": "the server reports playlist {number}, which is not one this bot knows",
    "pac7_playlist_unset": "the server did not answer for its playlist",
    "pac7_playlists": "playlists: {playlists}",
    "pac7_playlists_off": "playlists are switched off on this server",
    "pac7_playlist_usage": "!pasetplaylist <1-{highest}>",
    "pac7_playlist_range": "{number} is not a playlist - pick a number from 1 to {highest}",
    "pac7_playlist_set": "playlist is now {number}: {name}",
    "pac7_ranked_only": "this only works on a ranked server; try !map or !maprotate",
    "pac7_unranked_only": "this does not work on a ranked server",
    "pac7_ranked_unknown": "the server did not answer for sv_ranked, so I cannot tell whether it is "
    "ranked; nothing was changed",
    "pac7_setmap_usage": "!pasetmap <map>",
    "pac7_no_such_map": "{map} is not a stock Black Ops map",
    "pac7_ambiguous_map": "{map} could be {candidates} - say which",
    "pac7_setmap": "{map} is next; the other maps are excluded until this round ends",
    "pac7_excludemaps_usage": "!paexcludemaps <map> [<map> ...]",
    "pac7_excluded": "excluded from the playlist: {maps}",
    "pac7_set_usage": "!paset <cvar> <value>",
    "pac7_set": "{name} is now {value}",
    "pac7_get_usage": "!paget <cvar>",
    "pac7_get": "{name} is {value}",
    "pac7_get_unset": "{name} is not set, or the server did not answer",
    "pac7_restarting": "restarting the map in {seconds} seconds",
    "pac7_fast_restarting": "restarting the round in {seconds} seconds",
    "pac7_restart_unsupported": "this server has no verb for that",
    "pac7_gametype_usage": "!pagametype <{gametypes}>",
    "pac7_gametype_unknown": "{gametype} is not a gametype - try one of {gametypes}",
    "pac7_gametype": "gametype is now {name}; the map restarts in {seconds} seconds",
    "pac7_dlc_usage": "!pasetdlc <number> <on|off>",
    "pac7_dlc": "DLC{number} map pack is {state}",
    "pac7_cfg_dir_unset": "no config directory is configured for this plugin",
    "pac7_cfg_dir_missing": "the configured config directory does not exist",
    "pac7_cfg_none": "no .cfg files there",
    "pac7_cfg_list": "config files: {files}",
    "pac7_load_usage": "!paload <file.cfg>",
    "pac7_load_not_cfg": "{file} is not a .cfg file",
    "pac7_load_missing": "there is no {file} in the config directory",
    "pac7_load_busy": "{file} is still being sent - wait for it to finish",
    "pac7_load_started": "sending {file}: {lines} lines, about {minutes} minutes",
    "pac7_load_done": "{file} has been sent in full",
    "pac7_load_failed": "{file} could not be read ({error})",
}


@dataclass(slots=True)
class Delayed:
    """Something announced now and done a few seconds later, on the scheduled pass."""

    due: float
    action: Callable[[], object]


@dataclass(slots=True)
class Loading:
    """A server config file going out one line at a time."""

    name: str
    lines: deque[str] = field(default_factory=deque)
    client: Client | None = None


class Poweradmincod7Plugin(Plugin):
    """Black Ops's playlist, map-exclusion and DLC controls, and its config-file loader."""

    requires_plugins = ("admin",)
    requires_parsers = ("cod7",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self._delayed: list[Delayed] = []
        self._loading: Loading | None = None
        #: The operator's own exclusion list, held while a `!pasetmap` is waiting for a round to end.
        #: None means nothing is pending, which is also what stops a second `!pasetmap` from
        #: recording the twenty-five maps the first one excluded as though the operator had asked
        #: for them.
        self._restore: str | None = None

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        # One pass, once a second: the delayed restarts and the config file being sent. Nothing here
        # sleeps, which is the difference between this and the classic's four `time.sleep` calls.
        self.schedule(self._run_pending, second="*", name="Poweradmincod7Plugin.pending")
        self.subscribe(EventType.GAME_ROUND_START, self._on_round_start)
        directory = self.config_dir()
        if directory is not None and not directory.is_dir():
            log.warning(
                "poweradmincod7: config_dir %s does not exist, so !palistcfg and !paload have "
                "nothing to offer",
                directory,
            )

    def on_disable(self) -> None:
        """Put the operator's exclusion list back rather than leaving the server with 25 maps off."""
        self._restore_exclusions()

    def config_dir(self) -> Path | None:
        """Where this server's `.cfg` files are, or None when the operator has not said."""
        configured = str(self.settings.get("config_dir") or "").strip()
        return Path(configured).expanduser() if configured else None

    # -- what the server will obey -------------------------------------------

    def is_ranked(self) -> bool | None:
        """Whether this is a ranked server. None when the server did not answer.

        Read here rather than at startup: a read that raises in `on_startup` takes the whole plugin
        down, and reading it once meant an operator who changed `sv_ranked` had to restart the bot.
        """
        value = self.console.get_cvar("sv_ranked")
        if value is None or not str(value).strip():
            return None
        try:
            return int(str(value).strip()) == 2
        except ValueError:
            return None

    def playlists_enabled(self) -> bool:
        """Whether the playlist machinery is on. A ranked server always uses it."""
        if self.is_ranked():
            return True
        value = self.console.get_cvar("playlist_enabled")
        if value is None:
            return False
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _needs_ranked(self, ctx: CommandContext) -> bool:
        ranked = self.is_ranked()
        if ranked is None:
            ctx.reply(self.message("pac7_ranked_unknown"))
            return False
        if not ranked:
            ctx.reply(self.message("pac7_ranked_only"))
            return False
        return True

    def _needs_unranked(self, ctx: CommandContext) -> bool:
        ranked = self.is_ranked()
        if ranked is None:
            ctx.reply(self.message("pac7_ranked_unknown"))
            return False
        if ranked:
            ctx.reply(self.message("pac7_unranked_only"))
            return False
        return True

    # -- playlists -----------------------------------------------------------

    @command("paplaylist", level=40, alias="playlist")
    def cmd_paplaylist(self, ctx: CommandContext) -> None:
        """paplaylist - which playlist the server is running"""
        value = self.console.get_cvar("playlist")
        number = _as_playlist(value)
        if number is None:
            ctx.reply(self.message("pac7_playlist_unset"))
            return
        name = PLAYLISTS.get(number)
        if name is None:
            # The classic indexed its table directly, so a server reporting anything outside 1-25
            # raised inside the command rather than answering.
            ctx.reply(self.message("pac7_playlist_unknown", number=number))
            return
        ctx.reply(self.message("pac7_playlist", number=number, name=name))

    @command("pagetplaylists", level=100, alias="getplaylists")
    def cmd_pagetplaylists(self, ctx: CommandContext) -> None:
        """pagetplaylists - the playlists this engine has"""
        listed = ", ".join(f"{number} {name}" for number, name in sorted(PLAYLISTS.items()))
        ctx.reply(self.message("pac7_playlists", playlists=listed))

    @command("pasetplaylist", level=100, alias="setplaylist")
    def cmd_pasetplaylist(self, ctx: CommandContext) -> None:
        """pasetplaylist <number> - run a different playlist"""
        highest = max(PLAYLISTS)
        if not self.playlists_enabled():
            ctx.reply(self.message("pac7_playlists_off"))
            return
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pac7_playlist_usage", highest=highest))
            return
        # The classic validated with `float()` and then converted with `int()`, so `2.7` passed the
        # check and raised on the conversion.
        if not wanted.isdigit() or not 1 <= int(wanted) <= highest:
            ctx.reply(self.message("pac7_playlist_range", number=wanted, highest=highest))
            return
        number = int(wanted)
        self.console.set_cvar("playlist", str(number))
        log.info("poweradmincod7: %s set playlist %d", ctx.client.name, number)
        ctx.reply(self.message("pac7_playlist_set", number=number, name=PLAYLISTS[number]))

    # -- the map exclusion list ----------------------------------------------

    @command("pasetmap", level=40, alias="setmap")
    def cmd_pasetmap(self, ctx: CommandContext) -> None:
        """pasetmap <map> - the map the playlist plays next"""
        if not self._needs_ranked(ctx):
            return
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pac7_setmap_usage"))
            return
        chosen = self._resolve_map(ctx, wanted)
        if chosen is None:
            return
        if self._restore is None:
            # Read now, not at startup: this is what the operator's list actually is, and reading it
            # only when nothing is pending stops a second `!pasetmap` recording the first one's
            # twenty-five exclusions as though somebody had asked for them.
            self._restore = self.console.get_cvar(EXCLUDE_CVAR) or ""
        others = [map_id for map_id in COD7_MAPS if map_id != chosen]
        self.console.set_cvar(EXCLUDE_CVAR, " ".join(others))
        log.info(
            "poweradmincod7: %s set %s next; %d maps excluded until the round ends",
            ctx.client.name,
            chosen,
            len(others),
        )
        ctx.reply(self.message("pac7_setmap", map=self.console.map_display(chosen)))

    @command("paexcludemaps", level=100, alias="excludemaps")
    def cmd_paexcludemaps(self, ctx: CommandContext) -> None:
        """paexcludemaps <map> [<map> ...] - keep maps out of the playlist"""
        if not self.playlists_enabled():
            ctx.reply(self.message("pac7_playlists_off"))
            return
        wanted = ctx.arg_list()
        if not wanted:
            ctx.reply(self.message("pac7_excludemaps_usage"))
            return
        chosen: list[str] = []
        for name in wanted:
            found = self._resolve_map(ctx, name)
            if found is None:
                return  # `_resolve_map` has already said why
            if found not in chosen:
                chosen.append(found)
        self.console.set_cvar(EXCLUDE_CVAR, " ".join(chosen))
        # This *is* the operator's list now, so there is nothing held over to put back.
        self._restore = None
        log.info("poweradmincod7: %s excluded %s", ctx.client.name, " ".join(chosen))
        ctx.reply(
            self.message(
                "pac7_excluded", maps=", ".join(self.console.map_display(m) for m in chosen)
            )
        )

    def _resolve_map(self, ctx: CommandContext, wanted: str) -> str | None:
        """One stock Black Ops map from what an admin typed, or None with the reason given."""
        found = match_names(wanted, [(map_id, name) for map_id, name in COD7_MAPS.items()])
        if not found:
            ctx.reply(self.message("pac7_no_such_map", map=wanted))
            return None
        if len(found) > 1:
            listed = ", ".join(self.console.map_display(m) for m in found[:5])
            ctx.reply(self.message("pac7_ambiguous_map", map=wanted, candidates=listed))
            return None
        return found[0]

    def _on_round_start(self, event: Event) -> None:
        self._restore_exclusions()

    def _restore_exclusions(self) -> None:
        """Put the operator's exclusion list back, exactly as it was — including empty."""
        if self._restore is None:
            return
        held, self._restore = self._restore, None
        self.console.set_cvar(EXCLUDE_CVAR, held)
        log.info("poweradmincod7: the operator's map exclusion list is back (%r)", held)

    # -- cvars ---------------------------------------------------------------

    @command("paset", level=100)
    def cmd_paset(self, ctx: CommandContext) -> None:
        """paset <cvar> <value> - change a server setting"""
        parts = ctx.args.split(None, 1)
        if len(parts) < 2:
            # `data.split(' ', 1)[1]` in the classic, which raised IndexError on `!paset name`.
            ctx.reply(self.message("pac7_set_usage"))
            return
        name, value = parts[0].lower(), parts[1].strip()
        self.console.set_cvar(name, value)
        if name == EXCLUDE_CVAR.lower():
            self._restore = None  # the operator has just said what the list should be
        log.info("poweradmincod7: %s set %s to %r", ctx.client.name, name, value)
        ctx.reply(self.message("pac7_set", name=name, value=value))

    @command("paget", level=100)
    def cmd_paget(self, ctx: CommandContext) -> None:
        """paget <cvar> - read a server setting"""
        name = ctx.args.split()[0].lower() if ctx.args.split() else ""
        if not name:
            ctx.reply(self.message("pac7_get_usage"))
            return
        value = self.console.get_cvar(name)
        if value is None or not str(value).strip():
            ctx.reply(self.message("pac7_get_unset", name=name))
            return
        ctx.reply(self.message("pac7_get", name=name, value=value))

    @command("pasetdlc", level=100, alias="setdlc")
    def cmd_pasetdlc(self, ctx: CommandContext) -> None:
        """pasetdlc <number> <on|off> - switch a map pack on or off"""
        parts = ctx.arg_list()
        if len(parts) < 2 or not parts[0].isdigit() or parts[1].lower() not in ("on", "off"):
            # The classic printed the "expecting on or off" complaint to the bot's stdout, so the
            # admin who typed it was told nothing at all.
            ctx.reply(self.message("pac7_dlc_usage"))
            return
        number, state = int(parts[0]), parts[1].lower()
        # Treyarch counts its map packs from 2, so DLC1 is `playlist_excludeDlc2`. The command takes
        # the number an operator says out loud.
        self.console.set_cvar(f"playlist_excludeDlc{number + 1}", "0" if state == "on" else "1")
        log.info("poweradmincod7: %s switched DLC%d %s", ctx.client.name, number, state)
        ctx.reply(self.message("pac7_dlc", number=number, state=state))

    # -- restarts and the gametype -------------------------------------------

    @command("pamaprestart", level=40, alias="maprestart")
    def cmd_pamaprestart(self, ctx: CommandContext) -> None:
        """pamaprestart - reload the map"""
        self._restart(ctx, "map_restart", "pac7_restarting", RESTART_SECONDS)

    @command("pafastrestart", level=40, alias="fastrestart")
    def cmd_pafastrestart(self, ctx: CommandContext) -> None:
        """pafastrestart - restart the round without reloading the map"""
        self._restart(ctx, "fast_restart", "pac7_fast_restarting", RESTART_SECONDS)

    def _restart(self, ctx: CommandContext, verb: str, key: str, seconds: float) -> None:
        if not self._needs_unranked(ctx):
            return
        if not self.console.supports_server_verb(verb):
            ctx.reply(self.message("pac7_restart_unsupported"))
            return
        self.console.say(self.message(key, seconds=int(seconds)))
        self._after(seconds, lambda: self.console.apply_server_verb(verb))

    @command("pagametype", level=40, alias="gametype")
    def cmd_pagametype(self, ctx: CommandContext) -> None:
        """pagametype <gametype> - change the gametype and restart the map"""
        if not self._needs_unranked(ctx):
            return
        listed = ", ".join(sorted(GAMETYPES))
        parts = ctx.arg_list()
        if not parts:
            ctx.reply(self.message("pac7_gametype_usage", gametypes=listed))
            return
        wanted = parts[0].lower()
        if wanted not in GAMETYPES:
            ctx.reply(self.message("pac7_gametype_unknown", gametype=wanted, gametypes=listed))
            return
        # `setadmindvar g_gametype "tdm"`, which is what the title takes. The classic sent the bare
        # assignment `g_gametype tdm` and Black Ops ignored it, so the gametype never changed and
        # the map restarted regardless — the same fault this repository records against `g_logsync`.
        self.console.set_cvar("g_gametype", wanted)
        log.info("poweradmincod7: %s set gametype %s", ctx.client.name, wanted)
        self.console.say(
            self.message("pac7_gametype", name=GAMETYPES[wanted], seconds=int(GAMETYPE_SECONDS))
        )
        if self.console.supports_server_verb("map_restart"):
            self._after(GAMETYPE_SECONDS, lambda: self.console.apply_server_verb("map_restart"))

    def _after(self, seconds: float, action: Callable[[], object]) -> None:
        self._delayed.append(Delayed(due=self.console.clock.now() + seconds, action=action))

    # -- server config files -------------------------------------------------

    @command("palistcfg", level=100, alias="listcfg")
    def cmd_palistcfg(self, ctx: CommandContext) -> None:
        """palistcfg - the server config files this bot can send"""
        files = self._config_files(ctx)
        if files is None:
            return
        if not files:
            ctx.reply(self.message("pac7_cfg_none"))
            return
        ctx.reply(self.message("pac7_cfg_list", files=", ".join(sorted(files))))

    def _config_files(self, ctx: CommandContext) -> list[str] | None:
        directory = self.config_dir()
        if directory is None:
            ctx.reply(self.message("pac7_cfg_dir_unset"))
            return None
        if not directory.is_dir():
            ctx.reply(self.message("pac7_cfg_dir_missing"))
            return None
        return [item.name for item in directory.iterdir() if item.suffix.lower() == ".cfg"]

    @command("paload", level=100, alias="load")
    def cmd_paload(self, ctx: CommandContext) -> None:
        """paload <file.cfg> - send a server config file, a line at a time"""
        wanted = ctx.args.strip()
        if not wanted:
            ctx.reply(self.message("pac7_load_usage"))
            return
        if not wanted.lower().endswith(".cfg"):
            ctx.reply(self.message("pac7_load_not_cfg", file=wanted))
            return
        if self._loading is not None:
            ctx.reply(self.message("pac7_load_busy", file=self._loading.name))
            return
        files = self._config_files(ctx)
        if files is None:
            return
        # Only a name from that directory, never a path: `!paload ../../etc/passwd` is not a config
        # file this bot offers, and the classic joined whatever was typed onto the conf path.
        chosen = next((name for name in files if name.lower() == wanted.lower()), None)
        directory = self.config_dir()
        if chosen is None or directory is None:
            ctx.reply(self.message("pac7_load_missing", file=wanted))
            return
        try:
            lines = _config_lines(directory / chosen)
        except OSError as exc:
            ctx.reply(self.message("pac7_load_failed", file=chosen, error=exc.strerror or "unread"))
            return
        self._loading = Loading(name=chosen, lines=deque(lines), client=ctx.client)
        minutes = max(1, round(len(lines) / (60 * CONFIG_LINES_PER_PASS)))
        log.info("poweradmincod7: %s is sending %s (%d lines)", ctx.client.name, chosen, len(lines))
        ctx.reply(self.message("pac7_load_started", file=chosen, lines=len(lines), minutes=minutes))

    # -- the one scheduled pass ----------------------------------------------

    def _run_pending(self) -> None:
        """Everything this plugin owes on a deadline. Nothing sleeps and nothing is a thread."""
        now = self.console.clock.now()
        for delayed in list(self._delayed):
            if now >= delayed.due:
                self._delayed.remove(delayed)
                delayed.action()
        self._send_config_lines()

    def _send_config_lines(self) -> None:
        loading = self._loading
        if loading is None:
            return
        for _ in range(CONFIG_LINES_PER_PASS):
            if not loading.lines:
                break
            self.console.send_rcon(loading.lines.popleft())
        if loading.lines:
            return
        self._loading = None
        log.info("poweradmincod7: %s has been sent in full", loading.name)
        if loading.client is not None:
            self.console.tell(loading.client, self.message("pac7_load_done", file=loading.name))


def _as_playlist(value: object) -> int | None:
    """The playlist number a server reported, or None when it reported nothing usable."""
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    return int(text)


def _config_lines(path: Path) -> list[str]:
    """The lines of a server config worth sending: no comments, no blanks.

    The classic skipped `//` and a literal `\\r\\n` and sent everything else, so a file with blank
    lines spent a second per blank sending nothing.
    """
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("//"):
            lines.append(line)
    return lines


__all__ = [
    "CONFIG_LINES_PER_PASS",
    "DEFAULTS",
    "EXCLUDE_CVAR",
    "GAMETYPES",
    "GAMETYPE_SECONDS",
    "MESSAGES",
    "PLAYLISTS",
    "RESTART_SECONDS",
    "Delayed",
    "Loading",
    "Poweradmincod7Plugin",
]
