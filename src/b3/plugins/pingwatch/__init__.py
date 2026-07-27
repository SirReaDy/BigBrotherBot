"""Ping watch — removes players whose connection is ruining the game for everyone else.

A port of the classic `pingwatch`. The rule that makes it usable is hysteresis: a player is only
removed after staying over the limit for a while, so a momentary spike — which every connection
has — is ignored. A ping of 999 means the engine has lost them entirely ("connection interrupted"),
which is announced differently because it is not the player's fault.

Kept from the original, and easy to forget: nothing is checked for a couple of minutes after
startup or a map change. Every player's ping spikes while a map loads, and a bot that kicks half
the server after every rotation is worse than one that misses a lagger.

Changed from the original: the poll is a scheduled coroutine rather than a thread, and it reads the
player list the bot already keeps warm (`Console.get_players`, whose status reply is cached), so a
short interval does not hammer the server.
"""

from __future__ import annotations

import logging

from b3.core.commands import CommandContext, command
from b3.core.events import EventType
from b3.core.console import Console
from b3.core.plugin import Plugin
from b3.core.util import as_int

log = logging.getLogger(__name__)

#: The engine reports this when it has stopped hearing from a client altogether.
CONNECTION_INTERRUPTED = 999

DEFAULTS: dict[str, object] = {
    "interval": 30,  # seconds between checks
    "max_ping": 500,
    "max_ping_duration": 90,  # how long they must stay over the limit before being removed
    "kick_level": 20,  # at or above this level, nobody is kicked for ping
    "settle_time": 120,  # seconds of quiet after startup or a map change
}

MESSAGES = {
    "ping_kick": "kicked for a ping of {ping}",
    "ping_ci": "{name}'s connection was interrupted",
    "ping_report": "{name} has a ping of {ping}",
    "ping_unknown": "the server did not report a ping for {name}",
}


class PingwatchPlugin(Plugin):
    """Kicks players who stay above a ping limit."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: Nothing is checked before this time. Everyone's ping spikes while a map loads, and
        #: kicking half the server after every map change is worse than missing a lagger.
        self.ignore_until = 0.0

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self._settle()
        # GAME_EXIT only started firing once the parser learned the `ExitLevel:` line; before that
        # this grace period would have applied at startup and never again.
        self.subscribe(EventType.GAME_EXIT, lambda _event: self._settle())
        self.subscribe(EventType.GAME_MAP_CHANGE, lambda _event: self._settle())
        interval = max(1, as_int(self.settings.get("interval"), 30))
        # Cron granularity is a second, so an interval under a minute is a second-field step.
        if interval < 60:
            self.schedule(self.check, second=f"*/{interval}", name="PingwatchPlugin.check")
        else:
            self.schedule(self.check, minute=f"*/{interval // 60}", name="PingwatchPlugin.check")

    # -- the check ----------------------------------------------------------

    def _settle(self) -> None:
        """Give the server time to settle before judging anyone's connection."""
        self.ignore_until = self.console.clock.now() + as_int(self.settings.get("settle_time"), 120)

    def check(self) -> None:
        """Look at every connected player's ping; remove whoever has been over the limit too long."""
        if self.console.clock.now() < self.ignore_until:
            return
        try:
            players = self.console.get_players()
        except Exception as exc:  # noqa: BLE001 - a missed poll is not worth a crash
            log.debug("pingwatch: could not read the player list: %s", exc)
            return

        limit = as_int(self.settings.get("max_ping"), 500)
        endurance = as_int(self.settings.get("max_ping_duration"), 90)
        kick_level = as_int(self.settings.get("kick_level"), 20)
        now = self.console.clock.now()

        for info in players:
            client = self.console.clients.get_by_cid(info.cid)
            if client is None or client.max_level() >= kick_level:
                continue
            if info.ping <= limit:
                client.del_var(self, "high_ping_since")
                continue

            since = client.get_var(self, "high_ping_since")
            if since is None:
                # First time over: note it and let them recover. Every connection spikes.
                client.set_var(self, "high_ping_since", now)
                continue
            if now - float(since) <= endurance:
                continue

            client.del_var(self, "high_ping_since")
            if info.ping >= CONNECTION_INTERRUPTED:
                self.console.say(self.message("ping_ci", name=client.name))
            self.console.kick(client, reason=self.message("ping_kick", ping=info.ping))

    # -- commands -----------------------------------------------------------

    @command(level=20)
    def cmd_ci(self, ctx: CommandContext) -> None:
        """ci <player> - remove a player whose connection has been interrupted"""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("usage", usage="ci <player>"))
            return
        target = self.console.find_client(handle)
        if target is None:
            ctx.reply(self.message("player_not_found", handle=handle))
            return

        ping = self._ping_of(target.cid)
        if ping is None:
            ctx.reply(self.message("ping_unknown", name=target.name))
            return
        if ping < CONNECTION_INTERRUPTED:
            ctx.reply(self.message("ping_report", name=target.name, ping=ping))
            return
        self.console.say(self.message("ping_ci", name=target.name))
        self.console.kick(target, reason=self.message("ping_kick", ping=ping), admin=ctx.client)

    def _ping_of(self, cid: str | None) -> int | None:
        try:
            players = self.console.get_players()
        except Exception as exc:  # noqa: BLE001
            log.debug("pingwatch: could not read the player list: %s", exc)
            return None
        return next((p.ping for p in players if p.cid == cid), None)
