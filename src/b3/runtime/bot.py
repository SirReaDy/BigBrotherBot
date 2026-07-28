"""The Bot runtime — concrete implementation of the Console port.

Wires the pieces built in phases A-D into a working bot:

    log line -> parser.parse_line -> Event -> bus.publish -> plugin handlers / command processor
    admin command -> Console.kick/ban/... -> RCON out + penalty recorded + event published

It owns the shared services (bus, clients, storage, command registry, clock), authenticates players
against the database on join, and routes chat that starts with a command prefix to the core command
processor. RCON is any object exposing ``command(str) -> str`` (real UDP client, or a fake in tests).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timezone, tzinfo
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from b3.config.schema import Config
from b3.core.bus import EventBus
from b3.core.clients import ClientManager
from b3.core.clock import Clock, SystemClock
from b3.core.commands import CommandContext, CommandProcessor, CommandRegistry
from b3.core.events import Event, EventType
from b3.core.game import Game, PlayerInfo
from b3.core.messages import Messages
from b3.core.plugin import Plugin
from b3.core.scheduler import Scheduler
from b3.core.util import format_time, sanitize_rcon_value
from b3.domain.client import Alias, Client, IpAlias, NEVER_EXPIRES, Penalty, PenaltyType
from b3.parsers import games
from b3.parsers.cod import status as status_parser
from b3.parsers.cod.auth import AuthInfo, AuthManager
from b3.parsers.profile import GameProfile
from b3.storage.store import SqlAlchemyStorage

log = logging.getLogger(__name__)

# Chat events that may carry a command.
_SAY_EVENTS = (EventType.CLIENT_SAY, EventType.CLIENT_TEAM_SAY)

# How often the player list is reconciled against the server (legacy ran `sync` on a cron too).
SYNC_MINUTES = 5

# Every title, keyed by `server.game` — see b3.parsers.games. Titles in the same engine family
# share a parser; the differences between them are data.
PROFILES: dict[str, GameProfile] = dict(games.PROFILES)


class RconClient(Protocol):
    """What the bot needs of an RCON client.

    ``write`` — send without waiting for a reply — is looked up with ``getattr`` rather than
    required here, so the test fakes and any older client keep working; they simply pay for a
    round trip on chat output the way the bot used to.
    """

    def command(self, cmd: str) -> str: ...
    def close(self) -> None: ...


def _resolve_timezone(name: str) -> tzinfo | None:
    """Resolve an IANA zone name; fall back to local time with a warning if it is unknown.

    UTC is handled without touching the IANA database so the default config always works, even on a
    system with no zoneinfo data (Windows ships none — hence the ``tzdata`` dependency).
    """
    if not name:
        return None
    if name.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "unknown time_zone %r; falling back to local time (is the 'tzdata' package installed?)",
            name,
        )
        return None


class Bot:
    def __init__(
        self,
        config: Config,
        *,
        storage: SqlAlchemyStorage | None = None,
        rcon: RconClient | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.config = config
        self.clock: Clock = clock or SystemClock()
        self.bus = EventBus()
        self.clients = ClientManager()
        self.command_registry = CommandRegistry()
        self.storage = storage or SqlAlchemyStorage(config.bot.database, clock=self.clock)
        self.messages = Messages(
            config.messages,
            line_length=config.bot.line_length,
            color_prefix=config.bot.line_color_prefix,
        )
        self.tz = _resolve_timezone(config.bot.time_zone)
        self.scheduler = Scheduler(self.clock, tz=self.tz)
        #: Set by `!die`/`!restart`; the live loop watches it and returns this exit code.
        self.exit_code: int | None = None
        #: Set by `!pause`: log lines are ignored (not lost — just not parsed) until this time.
        self.paused_until: float = 0.0
        #: Where the config was loaded from, so `!reconfig` can re-read it. Set by the CLI.
        self.config_path: Path | None = None

        # Raises on an unknown title rather than guessing one — see games.profile_for.
        self.profile = games.profile_for(config.server.game)
        self.parser = games.parser_for(self.profile, self.clients)
        self.game = Game()
        self._rcon = rcon
        self._processor = CommandProcessor(
            self.command_registry,
            self,
            loud_level=config.bot.loud_level,
            silent_level=config.bot.silent_level,
        )
        self._plugins: dict[str, Plugin] = {}
        # Name/IP already recorded for a client this session, so history recording is idempotent
        # and `num_used` counts times a name was *adopted*, not chat lines sent.
        self._recorded: dict[int, tuple[str, str]] = {}
        # Second-phase authentication: a join line carries no IP, so poll the server for it.
        self.auth = AuthManager(self._resolve_auth, self._on_resolved)

        # Authenticate players as they join; route chat to the command processor.
        self.bus.subscribe(EventType.CLIENT_JOIN, self._on_join)
        self.bus.subscribe(EventType.CLIENT_DISCONNECT, self._on_disconnect)
        self.bus.subscribe(EventType.GAME_ROUND_START, self._on_round_start)
        for evt in _SAY_EVENTS:
            self.bus.subscribe(evt, self._on_say)

    # -- setup -------------------------------------------------------------

    def add_plugin(self, plugin: Plugin, name: str | None = None) -> None:
        """Register a plugin. ``name`` defaults to the class name minus a 'Plugin' suffix."""
        if name is None:
            name = type(plugin).__name__.removesuffix("Plugin").lower()
        self._plugins[name] = plugin

    @property
    def plugins(self) -> dict[str, Plugin]:
        """The registered plugins, in load order."""
        return dict(self._plugins)

    def get_plugin(self, name: str) -> Plugin | None:
        return self._plugins.get(name)

    def start(self) -> None:
        """Connect storage and start the enabled plugins. Call before feeding any lines."""
        self.storage.connect()
        for name, plugin in self._plugins.items():
            if plugin.is_enabled():
                plugin.start()
            else:
                log.info("plugin %r not started: %s", name, plugin.disabled_reason)
        if self._rcon is not None:
            for command in self.profile.startup_commands:
                try:
                    self._send(command)
                    log.info("%s: sent startup command %r", self.profile.name, command)
                except Exception as exc:  # noqa: BLE001 - the bot still works without it
                    log.warning("startup command %r failed: %s", command, exc)
            # Without this a bot started mid-match sees nobody until the next join line, and a
            # missed disconnect leaves a ghost in the client list forever.
            self.scheduler.add(self._scheduled_sync, minute=f"*/{SYNC_MINUTES}", name="Bot.sync")

    # -- main loop ---------------------------------------------------------

    async def feed_line(self, line: str) -> list[Event]:
        """Parse one log line and publish the resulting events. Returns them (for tests)."""
        if self.paused_until and self.clock.now() < self.paused_until:
            return []  # `!pause`: keep tailing, but do not act on anything
        events = self.parser.parse_line(line)
        for event in events:
            if not event.time:
                event.time = self.clock.now()
            await self.bus.publish(event)
        # Deliver any follow-up events emitted by handlers (e.g. CLIENT_BAN from an admin command).
        await self.bus.drain()
        return events

    async def replay(self, lines: list[str]) -> None:
        """Feed a batch of pre-recorded log lines (replay harness / tests)."""
        for line in lines:
            await self.feed_line(line)

    # -- authentication ----------------------------------------------------

    def _on_join(self, event: Event) -> None:
        if event.client is None:
            return
        if self._authenticate(event.client) and not event.client.ip:
            # The join line has no IP; it only exists in the server's status table, which lags the
            # join by a second or two. Poll for it in the background (legacy Parser.authorizeClients).
            self._schedule_auth(event.client)

    def _on_disconnect(self, event: Event) -> None:
        if event.client is not None and event.client.cid is not None:
            self.auth.cancel(event.client.cid)

    def _on_round_start(self, event: Event) -> None:
        """Track the match from the ``InitGame`` cvar dump (the legacy ``Game`` object)."""
        cvars = event.data if isinstance(event.data, dict) else {}
        now = self.clock.now()
        new_map = cvars.get("mapname", "")
        if new_map and new_map != self.game.map_name:
            previous = self.game.map_name
            self.game.start_map(cvars, now)
            if previous:
                self.bus.publish_soon(Event(EventType.GAME_MAP_CHANGE, data=new_map))
        else:
            self.game.cvars.update(cvars)
            self.game.start_round(now)

    def _schedule_auth(self, client: Client) -> None:
        """Start the status-poll auth for a client, if we can (needs RCON and a running loop)."""
        if self._rcon is None or client.cid is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # no loop: replay harness or a synchronous test
            return
        self.auth.schedule(client.cid)

    async def _resolve_auth(self, cid: str) -> AuthInfo | None:
        """Look for ``cid`` in the server's status table. Returns None until it appears.

        Runs the RCON round trip on a worker thread: this is called from the auth task, on the
        event loop, and a UDP timeout would otherwise stall every other handler.
        """
        try:
            players = await asyncio.to_thread(self.get_players)
        except Exception as exc:  # noqa: BLE001 - a status hiccup just means "not yet"
            log.debug("auth status poll failed for cid %s: %s", cid, exc)
            return None
        for info in players:
            if info.cid == cid and info.ip:
                return AuthInfo(cid=cid, guid=info.guid, ip=info.ip)
        return None

    def _on_resolved(self, info: AuthInfo, attempt: int) -> None:
        """Fill in what the log could not tell us, once the status table catches up."""
        client = self.clients.get_by_cid(info.cid)
        if client is None:
            return
        if client.guid and info.guid and not client.guid.lower().startswith(info.guid.lower()):
            # The slot was recycled by someone else while we were polling.
            log.info("auth resolve ignored: slot %s is now a different player", info.cid)
            return
        client.ip = info.ip
        if client.id is not None:
            self.storage.save_client(client)
            self._record_history(client)
        log.debug("resolved ip %s for %s on attempt %d", info.ip, client.name, attempt)
        self.bus.publish_soon(Event(EventType.CLIENT_UPDATE, client=client))

    def _authenticate(self, client: Client) -> bool:
        """Back the in-memory client with its database record, record history, enforce bans.

        Mirrors the legacy ``Client.auth()``: load-or-create the record, bump the connection count,
        remember the name/IP we saw, then — crucially — re-apply any ban still in force before the
        player is allowed to do anything. Returns False when the client was rejected.
        """
        if client.rejected:
            # Already thrown out; re-issuing the ban for every buffered line would spam RCON.
            return False
        if client.authed:
            # Already authenticated this session; a rejoin under a new name still gets recorded.
            self._record_history(client)
            return True
        if client.guid:
            record = self.storage.get_client_by_guid(client.guid)
            if record is not None:
                client.id = record.id
                client.group_bits = record.group_bits
                client.password = record.password
                client.login = record.login
                client.connections = record.connections + 1
            else:
                client.connections = 1
            self.storage.save_client(client)

        self._record_history(client)

        ban = self._ban_in_force(client)
        if ban is not None:
            client.rejected = True
            self.enforce_ban(client, ban)
            return False

        client.authed = True
        self.bus.publish_soon(Event(EventType.CLIENT_AUTH, client=client))
        return True

    def _record_history(self, client: Client) -> None:
        """Record the name and IP this client is using (legacy ``makeAlias``/``makeIpAlias``).

        Storage upserts on (value, client_id) and bumps ``num_used``. Calling this repeatedly for the
        same name is a no-op within a session, so the counter tracks how often a name was *adopted*
        — matching the legacy semantics, where an alias was only touched on a name change.
        """
        if client.id is None:
            return
        seen_name, seen_ip = self._recorded.get(client.id, ("", ""))
        if client.name and client.name != seen_name:
            self.storage.add_alias(Alias(value=client.name, client_id=client.id))
            seen_name = client.name
        if client.ip and client.ip != seen_ip:
            self.storage.add_ip_alias(IpAlias(value=client.ip, client_id=client.id))
            seen_ip = client.ip
        self._recorded[client.id] = (seen_name, seen_ip)

    def _ban_in_force(self, client: Client) -> Penalty | None:
        """The active ban to re-apply, if any. Penalties come back most-recent-first."""
        if client.id is None:
            return None
        for type_ in (PenaltyType.BAN, PenaltyType.TEMPBAN):
            penalties = self.storage.get_active_penalties(client.id, type_)
            if penalties:
                return penalties[0]
        return None

    def enforce_ban(self, client: Client, penalty: Penalty) -> None:
        """Re-apply an existing ban to a returning player — the legacy ``Client.reBan``.

        Deliberately does *not* record a new penalty: the ban already exists, and counting it again
        would inflate the player's ban history every time they knocked on the door.
        """
        if penalty.time_expire == NEVER_EXPIRES:
            self._send(self.profile.ban_template % self._penalty_values(client, penalty.reason))
            log.info("rejected banned client %s (%s)", client.name, client.guid)
            self.bus.publish_soon(Event(EventType.CLIENT_BAN, client=client, data=penalty.reason))
            return

        remaining = int(max(1, (penalty.time_expire - self.clock.epoch()) // 60))
        self._send_tempban(client, remaining, penalty.reason)
        log.info(
            "rejected tempbanned client %s (%s), %d min left", client.name, client.guid, remaining
        )
        self.bus.publish_soon(
            Event(EventType.CLIENT_BAN_TEMP, client=client, data=penalty.reason)
        )

    async def run_command(self, issuer: Client, text: str) -> bool:
        """Run a chat line as if ``issuer`` had typed it — what `!runas` is built on."""
        return await self._processor.handle(issuer, text)

    async def _on_say(self, event: Event) -> None:
        if event.client is None:
            return
        if not self._authenticate(event.client):
            return  # banned client: do not serve commands
        await self._processor.handle(event.client, str(event.data))

    # -- Console: in-game output ------------------------------------------

    def _send(self, cmd: str) -> None:
        """Send and wait for the reply. For anything whose failure an admin must hear about."""
        if self._rcon is not None:
            self._rcon.command(cmd)
        else:
            log.debug("rcon (no transport): %s", cmd)

    def _write(self, cmd: str) -> None:
        """Send without waiting for a reply — chat output only.

        This is what keeps RCON off the critical path. A reply-waiting send costs the socket's
        settle window *every time*, and these handlers run on the event-loop thread, so a `!rules`
        (twenty lines) used to stall the loop for seconds: the scheduler missed ticks and log lines
        queued up behind it. Nothing reads a `say` reply, so nothing needs to wait for one.

        Falls back to a round trip on an RCON object with no `write` — the fakes in the test suite,
        and any transport predating this. See TODO.md §1.7 for what is deliberately still blocking.
        """
        if self._rcon is None:
            log.debug("rcon (no transport): %s", cmd)
            return
        writer = getattr(self._rcon, "write", None)
        if writer is None:
            self._rcon.command(cmd)
            return
        writer(cmd)

    # Chat output is sanitised for the same reason penalty values are: almost every announcement
    # interpolates a player-chosen name ("Bob was kicked"), so the text on the command line is
    # partly untrusted. No length cap here — `wrap` has already bounded each line.

    def say(self, text: str) -> None:
        """Broadcast, split across as many lines as the game's chat limit needs."""
        for line in self.messages.wrap(text):
            self._write(self.profile.say_template % sanitize_rcon_value(line, None))

    def tell(self, client: Client, text: str) -> None:
        """Message one player, split across as many lines as the game's chat limit needs."""
        for line in self.messages.wrap(text):
            self._write(
                self.profile.tell_template
                % {"cid": client.cid, "text": sanitize_rcon_value(line, None)}
            )

    def say_big(self, text: str) -> None:
        """Centre-screen announcement, or a plain `say` on engines without one."""
        template = self.profile.saybig_template or self.profile.say_template
        for line in self.messages.wrap(text):
            self._write(template % sanitize_rcon_value(line, None))

    def command_reply(self, ctx: CommandContext, text: str) -> None:
        if ctx.loud:
            self.say(text)
        else:
            self.tell(ctx.client, text)

    # -- Console: server queries + control ---------------------------------
    #
    # These are the verbs that need RCON *replies* rather than fire-and-forget sends. With no
    # transport (replay, tests) they answer "nothing known" instead of failing: replay must not
    # depend on a server. A real RCON failure propagates, so the admin who typed the command is
    # told, rather than being shown a confidently empty answer.

    def _ask(self, cmd: str) -> str:
        return "" if self._rcon is None else self._rcon.command(cmd)

    def get_players(self) -> list[PlayerInfo]:
        if self._rcon is None:
            return []
        # Some protocols do not answer this as *text*: Frostbite replies with a structured block
        # carrying its own field names. A client that can read its own reply is asked to, rather than
        # having it flattened to a string and parsed back out again. Same duck-typed seam as `write`.
        own_reader = getattr(self._rcon, "get_players", None)
        if own_reader is not None:
            return list(own_reader())
        # Cached briefly by Rcon.get_status, so an auth poll and a `!status` in the same second
        # cost one round trip.
        get_status = getattr(self._rcon, "get_status", None)
        raw = get_status() if get_status else self._rcon.command(self.profile.status_command)
        _, players = status_parser.parse_status(
            raw, self.profile.status_patterns, self.profile.identity_field
        )
        return players

    def get_cvar(self, name: str) -> str | None:
        if self._rcon is None or not name:
            # An empty name is not a question. Sending one to a BattlEye server would frame an empty
            # command — its *keepalive* — which never gets a reply, so the caller would stall for the
            # whole rcon timeout and then be told the server is broken.
            return None
        value = status_parser.parse_cvar(name, self._ask(name))
        if value is not None:
            self.game.cvars[name] = value
        return value

    def set_cvar(self, name: str, value: str) -> None:
        # The value is wrapped in quotes here, so a quote inside it would close them early.
        self._send(f'set {sanitize_rcon_value(name)} "{sanitize_rcon_value(value)}"')
        self.game.cvars[name] = value

    def get_map(self) -> str | None:
        if self._rcon is None:
            return self.game.map_name or None
        get_status = getattr(self._rcon, "get_status", None)
        raw = get_status() if get_status else self._ask(self.profile.status_command)
        map_name, _ = status_parser.parse_status(
            raw, self.profile.status_patterns, self.profile.identity_field
        )
        if map_name:
            self.game.map_name = map_name
        return map_name or None

    def get_maps(self) -> list[str]:
        if not self.profile.rotation_cvar:
            return []  # this engine has no rotation to read (Arma changes mission by command)
        rotation = self.get_cvar(self.profile.rotation_cvar)
        return status_parser.parse_rotation(rotation) if rotation else []

    def get_next_map(self) -> str | None:
        """The map after the current one in the rotation — wrapping around at the end."""
        maps = self.get_maps()
        if not maps:
            return None
        current = (self.get_map() or self.game.map_name).strip().lower()
        for index, name in enumerate(maps):
            if name.strip().lower() == current:
                return maps[(index + 1) % len(maps)]
        return maps[0]  # current map is not in the rotation: the next one played is its first

    def change_map(self, name: str) -> None:
        self._send(self.profile.map_template % sanitize_rcon_value(name))

    def rotate_map(self) -> None:
        self._send(self.profile.rotate_command)

    def sync(self) -> list[Client]:
        """Reconcile the in-memory client list against the server's — the legacy ``sync``.

        Log lines are the primary source of who is connected, but they are not sufficient: a bot
        started mid-match never saw anyone join, and a dropped log line leaves a ghost in the slot
        list forever. This adopts players we do not know about, drops those the server no longer
        lists, and fills in IPs along the way.
        """
        return self._reconcile(self.get_players())

    def _reconcile(self, players: list[PlayerInfo]) -> list[Client]:
        """Apply a player list to the client manager. No I/O of its own — see :meth:`sync`.

        Split out from the RCON round trip on purpose: the round trip can be moved to a worker
        thread, but this half publishes events, and the bus belongs to the event loop.
        """
        if not players and self._rcon is None:
            return self.clients.connected()

        live: set[str] = set()
        for info in players:
            live.add(info.cid)
            client = self.clients.get_by_cid(info.cid)
            if client is not None and client.guid and info.guid:
                if not client.guid.lower().startswith(info.guid.lower()):
                    # Someone else is in that slot now; forget the stale record first.
                    self._drop_client(client)
                    client = None
            if client is None:
                client = Client(cid=info.cid, name=info.name, guid=info.guid, ip=info.ip)
                self.clients.add(client)
                log.info("sync: adopting player %s in slot %s", info.name, info.cid)
                self._authenticate(client)
                continue
            if info.ip and client.ip != info.ip:
                client.ip = info.ip
                if client.id is not None:
                    self.storage.save_client(client)
                    self._record_history(client)

        for client in self.clients.connected():
            if client.cid is not None and client.cid not in live:
                log.info("sync: %s is no longer on the server", client.name)
                self._drop_client(client)
        return self.clients.connected()

    # -- Console: bot lifecycle --------------------------------------------

    def format_time(self, epoch: float | None = None) -> str:
        """Render a timestamp in the bot's configured zone — the legacy ``formatTime``."""
        return format_time(self.clock.now() if epoch is None else epoch, self.tz)

    def pause(self, minutes: float) -> None:
        """Stop acting on log lines for a while (the legacy `!pause`).

        The tail keeps running, so nothing is lost from the file's point of view; the bot simply
        ignores what happens while it is asleep. An admin who paused for an hour and changed their
        mind just calls it again with 0.
        """
        self.paused_until = self.clock.now() + minutes * 60 if minutes > 0 else 0.0

    def is_paused(self) -> bool:
        return bool(self.paused_until) and self.clock.now() < self.paused_until

    def shutdown(self, restart: bool = False) -> None:
        """Ask the run loop to stop. ``restart`` exits with the legacy restart code (221).

        The bot does not re-exec itself: under systemd, Docker or a supervisor script the exit code
        is the restart signal, and re-execing in-process is how the classic bot leaked file handles.
        """
        self.exit_code = 221 if restart else 0
        self.bus.publish_soon(Event(EventType.STOP, data="restart" if restart else "shutdown"))

    def reload_config(self) -> Config:
        """Re-read the main config file — the legacy `!reconfig`.

        Refreshes what can safely change while running: message templates, line wrapping and the
        time zone. Plugins, storage and the log source keep their startup settings, because
        swapping those under a running bot is how the classic version corrupted its own state.
        """
        if self.config_path is None:
            raise RuntimeError("no config path recorded; reload is unavailable")
        from b3.config.loader import load_config

        config = load_config(str(self.config_path))
        self.config = config
        self.messages = Messages(
            config.messages,
            line_length=config.bot.line_length,
            color_prefix=config.bot.line_color_prefix,
        )
        self.tz = _resolve_timezone(config.bot.time_zone)
        self.scheduler.tz = self.tz
        log.info("configuration reloaded from %s", self.config_path)
        return config

    def _drop_client(self, client: Client) -> None:
        if client.cid is not None:
            self.clients.remove(client.cid)
            self.auth.cancel(client.cid)
        self.bus.publish_soon(Event(EventType.CLIENT_DISCONNECT, client=client))

    async def _scheduled_sync(self) -> None:
        """Periodic reconciliation. Queries off the loop, mutates on it.

        The status round trip is UDP with a timeout, so it goes to a worker thread; applying the
        result stays here, because adopting or dropping a player publishes events and the bus is
        not thread-safe. A failed round trip must never take the scheduler down — the next tick
        tries again.
        """
        try:
            players = await asyncio.to_thread(self.get_players)
        except Exception as exc:  # noqa: BLE001 - a status hiccup is not fatal
            log.warning("player-list sync failed: %s", exc)
            return
        self._reconcile(players)

    # -- Console: moderation ----------------------------------------------

    # Record first, tell the game server second — deliberately in that order. RCON is a lossy UDP
    # hop to another machine (always so when the log is tailed remotely), and if it is down the
    # penalty must still exist: our own record is the source of truth, and `enforce_ban` re-applies
    # it on the player's next connect. The old order dropped the ban entirely on an RCON timeout.

    def _penalty_values(
        self, client: Client, reason: str = "", minutes: int = 0
    ) -> dict[str, object]:
        """Everything a penalty template might name, so each profile picks what its engine takes.

        Stock CoD only understands a slot id; CoD4X's verbs take a reason and a duration, and its
        `unban` takes a guid so it reaches someone who is not connected.

        Every text value is sanitised on the way out. The reason is typed by an admin and the name
        is chosen by the *player*, so both are untrusted as far as the command line is concerned —
        see :func:`b3.core.util.sanitize_rcon_value`. The slot and the duration are the bot's own
        integers and are passed through.
        """
        return {
            "cid": client.cid,
            "guid": sanitize_rcon_value(client.guid),
            "name": sanitize_rcon_value(client.name),
            "target": sanitize_rcon_value(client.name),
            "reason": (
                sanitize_rcon_value(reason, self.profile.max_reason_length) or "no reason given"
            ),
            "minutes": minutes,
            # The same duration in seconds, because Frostbite's ban verb counts in those. Offered
            # rather than converted at the call site so a profile picks the unit its engine takes.
            "seconds": minutes * 60,
        }

    def kick(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        self._record_penalty(PenaltyType.KICK, client, admin, reason, 0, NEVER_EXPIRES)
        self.bus.publish_soon(Event(EventType.CLIENT_KICK, client=client, data=reason))
        self._send(self.profile.kick_template % self._penalty_values(client, reason))

    def ban(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        self._record_penalty(PenaltyType.BAN, client, admin, reason, 0, NEVER_EXPIRES)
        self.bus.publish_soon(Event(EventType.CLIENT_BAN, client=client, data=reason))
        self._send(self.profile.ban_template % self._penalty_values(client, reason))

    def tempban(
        self, client: Client, minutes: int, reason: str = "", admin: Client | None = None
    ) -> None:
        expire = self.clock.epoch() + minutes * 60
        self._record_penalty(PenaltyType.TEMPBAN, client, admin, reason, minutes, expire)
        self.bus.publish_soon(Event(EventType.CLIENT_BAN_TEMP, client=client, data=reason))
        self._send_tempban(client, minutes, reason)

    def _send_tempban(self, client: Client, minutes: int, reason: str) -> None:
        """Use the engine's own timed ban when it has one, else ban and re-apply on reconnect.

        Either way the penalty row is what the bot enforces: `enforce_ban` re-applies a tempban
        with its remaining time, so a server that forgets its banlist does not let anyone back in.
        """
        template = self.profile.tempban_template
        if template is None:
            self._send(self.profile.ban_template % self._penalty_values(client, reason))
            return
        capped = minutes
        if self.profile.tempban_max_minutes and minutes > self.profile.tempban_max_minutes:
            capped = self.profile.tempban_max_minutes
            log.info(
                "%s caps a native tempban at %d minutes; the %d-minute penalty is still recorded "
                "in full and re-applied on reconnect",
                self.profile.name,
                capped,
                minutes,
            )
        self._send(template % self._penalty_values(client, reason, capped))

    def warn(
        self,
        client: Client,
        reason: str = "",
        admin: Client | None = None,
        minutes: int = 0,
    ) -> None:
        """Record a warning. ``minutes`` gives it a lifetime; 0 means it stands until cleared."""
        expire = self.clock.epoch() + minutes * 60 if minutes > 0 else NEVER_EXPIRES
        self._record_penalty(PenaltyType.WARNING, client, admin, reason, minutes, expire)
        self.bus.publish_soon(Event(EventType.CLIENT_WARN, client=client, data=reason))

    def notice(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        """Record a note about a player — no in-game effect, it is admin memory."""
        self._record_penalty(PenaltyType.NOTICE, client, admin, reason, 0, NEVER_EXPIRES)
        self.bus.publish_soon(Event(EventType.CLIENT_NOTICE, client=client, data=reason))

    def unban(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        """Lift every active ban (soft-delete, never a physical delete) and un-ban server-side."""
        if client.id is not None:
            for type_ in (PenaltyType.BAN, PenaltyType.TEMPBAN):
                self.storage.disable_penalties(client.id, type_)
        self.bus.publish_soon(Event(EventType.CLIENT_UNBAN, client=client, data=reason))
        if self.profile.unban_template is None:
            # No single command can express it on this engine (see GameProfile.unban_template). Some
            # clients can still do it as a sequence — BattlEye has to read its ban list to find the
            # row number — so ask before giving up. Same duck-typed seam as `write`/`get_status`.
            remover = getattr(self._rcon, "remove_ban", None)
            if remover is not None and client.guid:
                if remover(client.guid):
                    return
                # It said so itself, and in more detail than this could; do not repeat it.
                return
            # Our record is lifted either way, so the bot will not re-apply the ban — but the server
            # keeps its own entry, and saying so is the whole point: silently doing half the job is
            # how a player stays banned by a server nobody remembers banning them on.
            log.warning(
                "%s cannot lift a ban over rcon; %s is unbanned in b3 but may still be on the "
                "server's own ban list — remove it there with the game's own tools",
                self.profile.name,
                client.name,
            )
            return
        self._send(self.profile.unban_template % self._penalty_values(client, reason))

    def _record_penalty(
        self,
        type_: PenaltyType,
        client: Client,
        admin: Client | None,
        reason: str,
        duration: int,
        time_expire: int,
    ) -> None:
        if client.id is None:
            self.storage.save_client(client)
        self.storage.add_penalty(
            Penalty(
                type=type_,
                client_id=client.id,  # type: ignore[arg-type]
                admin_id=admin.id if admin else None,
                duration=duration,
                reason=reason,
                # Which server this is, for the operators who point several bots at one database.
                # Blank unless they say so, and never used to *filter* enforcement: a shared ban
                # list that only applied on the server it was typed on would be pointless.
                server_id=self.config.bot.server_id,
                time_expire=time_expire,
            )
        )

    # -- Console: lookup ---------------------------------------------------

    def find_client(self, handle: str) -> Client | None:
        # @<dbid> -> database lookup
        if handle.startswith("@"):
            try:
                return self.storage.get_client_by_id(int(handle[1:]))
            except ValueError:
                return None
        # exact slot id
        by_cid = self.clients.get_by_cid(handle)
        if by_cid is not None:
            return by_cid
        # case-insensitive name match among connected players
        needle = handle.lower()
        matches = [c for c in self.clients.connected() if needle in c.name.lower()]
        return matches[0] if matches else None

    def lookup_clients(self, term: str) -> list[Client]:
        """Resolve a handle against the database so offline players can be acted on.

        ``!unban``/``!aliases``/``!baninfo`` are almost always used on someone who has already left,
        which :meth:`find_client` (connected players) cannot reach.
        """
        term = term.strip()
        if not term:
            return []
        if term.startswith("@"):
            try:
                found = self.storage.get_client_by_id(int(term[1:]))
            except ValueError:
                return []
            return [found] if found is not None else []
        # Prefer a connected player's database record, then fall back to a stored-name/alias search.
        connected = self.find_client(term)
        if connected is not None and connected.id is not None:
            return [connected]
        return self.storage.search_clients(term)
