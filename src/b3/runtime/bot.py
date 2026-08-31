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
from dataclasses import fields, replace
from datetime import timezone, tzinfo
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Protocol
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
from b3.core.util import MAX_RCON_CVAR, as_int, format_time, sanitize_rcon_value
from b3.domain.client import Alias, Client, IpAlias, NEVER_EXPIRES, Penalty, PenaltyType
from b3.parsers import games, punkbuster
from b3.parsers.cod import status as status_parser
from b3.parsers.cod.auth import AuthInfo, AuthManager
from b3.core.selfupdate import UpdateChecker, write_cache
from b3.parsers.profile import GameProfile, MapRequest, VersionQuirk
from b3.storage.store import SqlAlchemyStorage

log = logging.getLogger(__name__)

# Chat events that may carry a command.
_SAY_EVENTS = (EventType.CLIENT_SAY, EventType.CLIENT_TEAM_SAY)

# How often the player list is reconciled against the server (legacy ran `sync` on a cron too).
SYNC_MINUTES = 5

# Every title, keyed by `server.game` — see b3.parsers.games. Titles in the same engine family
# share a parser; the differences between them are data.
PROFILES: dict[str, GameProfile] = dict(games.PROFILES)


class MissingServerModError(RuntimeError):
    """The title needs a server-side mod (SourceMod) and the server does not have it.

    Its own class so `cli.main` can report it as one line an operator can act on, the way it already
    does for an unknown `server.game`, rather than as a traceback.
    """


class WrongGameError(RuntimeError):
    """`server.game` names one title and the server is running another.

    Its own class, next to `MissingServerModError`, so `cli.main` reports it as one line an operator
    can act on. It is a configuration mistake of exactly the same kind as an unknown `server.game`,
    and the fix is a one-word edit — which a traceback would bury.
    """


class RconClient(Protocol):
    """What the bot needs of an RCON client.

    ``write`` — send without waiting for a reply — is looked up with ``getattr`` rather than
    required here, so the test fakes and any older client keep working; they simply pay for a
    round trip on chat output the way the bot used to.
    """

    def command(self, cmd: str) -> str: ...
    def close(self) -> None: ...


def _line_length(config: Config, profile: GameProfile) -> int:
    """How long a chat line may be: the configured length, capped by the engine's own limit.

    The smaller wins, because the two mean different things. `bot.line_length` is a preference — an
    operator asking for shorter lines than the game would show. `GameProfile.line_length` is a fact
    about the engine: past it, the game does not display the rest. A config value cannot lift that,
    and letting it try is how most of every reply goes missing on Plutonium.
    """
    if not profile.line_length:
        return config.bot.line_length
    return min(config.bot.line_length, profile.line_length)


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
        # The title is resolved first, before anything is built or opened: it raises on an unknown
        # one (see games.profile_for), and doing it here means a typo no longer creates an empty
        # database on its way to the error.
        self.profile = games.profile_for(config.server.game)
        self.bus = EventBus()
        self.clients = ClientManager(self.clock)
        #: Slot -> when a mute on it runs out. Here rather than in a plugin because two plugins want
        #: to mute (see `Bot.mute`), and two owners of one piece of engine state fight over it.
        self._muted: dict[str, float] = {}
        self.command_registry = CommandRegistry()
        self.storage = storage or SqlAlchemyStorage(config.bot.database, clock=self.clock)
        self.messages = self._build_messages()
        self.tz = _resolve_timezone(config.bot.time_zone)
        self.scheduler = Scheduler(self.clock, tz=self.tz)
        #: Set by `!die`/`!restart`; the live loop watches it and returns this exit code.
        self.exit_code: int | None = None
        #: Set by `!pause`: log lines are ignored (not lost — just not parsed) until this time.
        self.paused_until: float = 0.0
        #: Where the config was loaded from, so `!reconfig` can re-read it. Set by the CLI.
        self.config_path: Path | None = None
        #: Which of the profile's status commands last produced players — see `_read_status`.
        self._status_command = ""
        #: A status reply we could not read is worth one warning, not one per poll.
        self._warned_about_status = False
        #: The PunkBuster service, once the server has confirmed it is running one. None on every
        #: engine that cannot run it, and on every server that does not.
        self.punkbuster: punkbuster.PunkBuster | None = None
        #: Whether a newer release exists, asked at most once per `bot.update_check_interval` and
        #: read by `!b3`. None until `setup_update_check` builds one, and on a bot with the check
        #: switched off it stays None — which is what makes `update_available()` answer "".
        self._update_checker: UpdateChecker | None = None
        #: What is known to be odd about this server's build, once it has been asked. None until
        #: `check_version` runs, and on every title that has no build-specific faults.
        self.version_quirk: VersionQuirk | None = None
        #: Slots already asked about with `userinfo_command`. A server that never sets an id must be
        #: asked once and then left alone, not once per sync for the life of the bot.
        self._asked_userinfo: set[str] = set()
        #: Set when the last status reply held rows no pattern could read. "Unknown", not "empty" --
        #: `sync` refuses to reconcile on it rather than reporting an empty server.
        self._status_unreadable = False

        self.parser = games.parser_for(self.profile, self.clients, config.server.port)
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
        self.bus.subscribe(EventType.CLIENT_UPDATE, self._on_client_update)
        self.bus.subscribe(EventType.CLIENT_DISCONNECT, self._on_disconnect)
        self.bus.subscribe(EventType.GAME_ROUND_START, self._on_round_start)
        # Who is alive, for `say_dead`/`smart_say`. Subscribed here rather than tracked in each
        # parser because the events already say it on every family that reports them, and a parser
        # that does not report spawns simply leaves everyone alive — which is the safe answer.
        for died in (EventType.CLIENT_KILL, EventType.CLIENT_SUICIDE, EventType.CLIENT_GIB):
            self.bus.subscribe(died, self._on_death)
        self.bus.subscribe(EventType.CLIENT_SPAWN, self._on_spawn)
        self.bus.subscribe(EventType.SERVER_INFO, self._on_server_info)
        for evt in _SAY_EVENTS:
            self.bus.subscribe(evt, self._on_say)

    # -- setup -------------------------------------------------------------

    def _build_messages(self) -> Messages:
        """Build the message formatter from the config and the *current* profile.

        One place rather than three, because the profile can change after startup — a server with
        SourceMod's "B3 Say" turns `prefix_messages` off (see `apply_optional_mods`) — and a
        formatter built before that would go on prefixing with a value nothing agrees with.
        """
        return Messages(
            self.config.messages,
            line_length=_line_length(self.config, self.profile),
            color_prefix=self.config.bot.line_color_prefix,
            prefix=self.config.bot.prefix if self.profile.prefix_messages else "",
        )

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
        # Before anything else that could look like success: a title needing a server-side mod is
        # not administerable without it, and the whole point of checking is to say so instead of
        # running as a bot whose `!ban` does nothing.
        self.check_required_mod()
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
            self.check_version()
            self.apply_optional_mods()
            self.setup_punkbuster()
            self.read_server_info()
            # Without this a bot started mid-match sees nobody until the next join line, and a
            # missed disconnect leaves a ghost in the client list forever.
            self.scheduler.add(self._scheduled_sync, minute=f"*/{SYNC_MINUTES}", name="Bot.sync")
            if self.profile.player_verbs.get("mute"):
                # Only where there is something to lift. See `Bot.mute` for why the deadline is here
                # and not in whichever plugin happened to set it.
                self.scheduler.add(self._lift_expired_mutes, second="*/5", name="Bot.mutes")
        self._report_command_conflicts()
        self.setup_update_check()

    def _report_command_conflicts(self) -> None:
        """Say, once, which plugins wanted the same word — the way a missing dependency is reported.

        Each collision was already logged as it happened, in the middle of a startup nobody reads
        line by line. This is the summary an operator can act on, and it names the losing plugin
        because that is the one whose config they wrote and whose command is not there.
        """
        conflicts = self.command_registry.conflicts
        if not conflicts:
            return
        log.warning(
            "%d command name(s) wanted by two plugins; the first to load kept each one:",
            len(conflicts),
        )
        for clash in conflicts:
            log.warning("  %s", clash.describe())
        log.warning("  -> `!cmdalias` can rename one, or drop a plugin from `plugins:`")

    def setup_update_check(self) -> None:
        """Ask, at most once a day, whether a newer release exists — and only say so if one does.

        The classic checked on every startup and said something every time. This is a scheduled task
        so it never blocks the run loop, `asyncio.to_thread`'d because `git ls-remote` is a process,
        and silent when there is nothing to report: a bot that nags an operator who is already
        up to date teaches them to ignore it.
        """
        from b3.core.util import parse_duration

        if not self.config.bot.update_check or not self.config.bot.update_remote.strip():
            return
        try:
            minutes = parse_duration(self.config.bot.update_check_interval)
        except ValueError:
            log.warning(
                "bot.update_check_interval is %r, which is not a duration; using a day",
                self.config.bot.update_check_interval,
            )
            minutes = 24 * 60
        self._update_checker = UpdateChecker(
            self.config.bot.update_remote, interval=max(60.0, minutes * 60.0)
        )
        self.scheduler.add(self._check_for_update, hour="*", name="Bot.update_check")

    async def _check_for_update(self) -> None:
        """One pass of the update check, off the run loop's thread."""
        import asyncio

        checker = self._update_checker
        if checker is None or not checker.due(self.clock.now()):
            return
        info = await asyncio.to_thread(checker.check, self.clock.now())
        # Written where the CLI reads it, so on a machine that runs a bot no command ever has to ask
        # for itself: the answer somebody sees after `b3 plugins` is the one this already has.
        await asyncio.to_thread(write_cache, self.config.bot.update_remote, info)
        if info.available:
            log.warning(
                "b3 %s is available (running %s) — install it with `b3 update`",
                info.latest,
                info.current,
            )
        elif info.error:
            # A warning, never a failure: an unreachable repository must not stop a bot from
            # moderating a game server.
            log.info("update check: %s", info.error)

    def update_available(self) -> str:
        """The newer version this bot knows about, or "". Never asks: `!b3` must not wait on git."""
        checker = self._update_checker
        if checker is None or checker.last is None or not checker.last.available:
            return ""
        return str(checker.last.latest)

    def check_version(self) -> None:
        """Read which build this server is, and say so when the build is one with a known fault.

        The classic bot's `setVersionExceptions`. Call of Duty 2 is what it was for: build 1.0
        cannot authenticate players, and build 1.2 reports PunkBuster ids one character shorter than
        every other build. We read no version at all before this, so a 1.0 server merely looked
        broken and a 1.2 PunkBuster id failed validation without a word.

        Never fatal. A server that will not answer the question is not a reason to refuse to run —
        unlike a missing SourceMod, which makes the bot unable to act at all.
        """
        self._check_is_the_right_game()
        if not self.profile.version_cvar:
            return
        try:
            version = self.get_cvar(self.profile.version_cvar) or ""
        except Exception as exc:  # noqa: BLE001 - a version we cannot read is not a failure
            log.debug("could not read %s: %s", self.profile.version_cvar, exc)
            return
        if not version:
            return
        self.game.version = version
        log.info("%s: server reports version %s", self.profile.name, version)
        quirk = self.profile.quirk_for(version)
        if quirk is None:
            return
        self.version_quirk = quirk
        if quirk.warning and not (quirk.only_when_using_ids and self.profile.ips_only):
            log.warning("%s %s: %s", self.profile.name, version, quirk.warning)

    def setup_punkbuster(self) -> None:
        """Build the PunkBuster service, if this title can run it and this server does.

        **Asked rather than configured**, which is the difference from the classic. There, Call of
        Duty enabled it unless told otherwise and Quake 3 disabled it unless told to, so half the
        operators running PunkBuster got no benefit from it and the other half saw a stream of
        unknown-command replies. `PB_SV_Ver` answers the question outright, so the default is to ask.

        `server.punkbuster: true` insists instead, and says so when the server has none — an
        operator relying on PunkBuster for player identity needs to hear that it is not there, which
        is exactly the case where a silent fallback is worst. `false` skips the question.
        """
        wanted = self.config.server.punkbuster
        if not self.profile.punkbuster or wanted is False or self._rcon is None:
            return
        # Asked the way this title carries a PunkBuster verb, which is the whole of the Frostbite
        # story: the probe went out bare there, was answered `UnknownCommand`, and that answer reads
        # as "not installed" — so no Battlefield server ever got the service.
        probe = self._punkbuster_line(punkbuster.VERSION_COMMAND)
        try:
            reply = self._rcon.command(probe)
        except Exception as exc:  # noqa: BLE001 - a question we could not ask is not an answer
            log.warning("could not ask whether PunkBuster is running: %s", exc)
            return
        if not punkbuster.PunkBuster.is_installed(reply):
            if wanted:
                log.warning(
                    "server.punkbuster is set, but %r answered %r — this server is not running "
                    "PunkBuster, so no PunkBuster id will ever be recorded and `!pbss` cannot work",
                    probe,
                    (reply or "").strip()[:80],
                )
            return
        self.punkbuster = punkbuster.PunkBuster(self._send_and_read)
        log.info("punkbuster active: %s", reply.strip()[:120])
        quirk = self.version_quirk
        if quirk is not None and quirk.pb_id_length:
            # Recorded rather than enforced: the row pattern already accepts 30-32 characters,
            # because the captured data from real servers contains 31-character ids — the same fact
            # this quirk states about Call of Duty 2 build 1.2, arriving from the other direction.
            log.info(
                "%s %s reports %d-character PunkBuster ids",
                self.profile.name,
                self.game.version,
                quirk.pb_id_length,
            )

    def request_screenshot(self, client: Client) -> bool:
        """Ask PunkBuster for a picture of what this player is seeing. False if it cannot be asked.

        On the port because `!pbss` needs it and the admin plugin is deliberately not given the
        PunkBuster object — the same reasoning as `map_display` and `parse_map_request`. The picture
        lands in the *server's* PunkBuster folder, so all this can report is whether it was asked.
        """
        if self.punkbuster is None:
            return False
        try:
            return self.punkbuster.screenshot(client)
        except Exception as exc:  # noqa: BLE001 - the admin gets told, rather than a traceback
            log.warning("punkbuster screenshot request failed: %s", exc)
            return False

    def _punkbuster_line(self, cmd: str) -> str:
        """A PunkBuster verb as this engine carries it.

        `GameProfile.punkbuster_template` is the carrier, and on every family but one it is `%s` — a
        `PB_SV_*` verb *is* an RCON command there. Frostbite hands it to `punkBuster.pb_sv_command`
        instead, and the inner quotes are escaped because that engine's commands are word lists: a
        bare quote inside the argument would end the word and turn the rest of a ban command into
        arguments of its own.
        """
        template = self.profile.punkbuster_template or "%s"
        if template == "%s":
            return cmd
        return template % cmd.replace('"', '\\"')

    def _send_and_read(self, cmd: str) -> str:
        """Send a PunkBuster verb and return the reply — what :class:`punkbuster.PunkBuster` uses."""
        return self._ask(self._punkbuster_line(cmd))

    def send_punkbuster(self, command: str) -> str | None:
        """See `Console.send_punkbuster`. None where this server is not running PunkBuster.

        Deliberately *not* on :class:`punkbuster.PunkBuster` as another method: this is a line an
        operator typed, so there is nothing to model. It exists because three plugins had grown their
        own copy of it — `poweradminbf3`, `poweradminbfbc2` and the classic `poweradminmoh` — each
        with the verb's spelling written into the plugin, which is how a Battlefield-only spelling
        ends up in a plugin that is not about Battlefield.
        """
        if self.punkbuster is None:
            return None
        try:
            return self._send_and_read(command)
        except Exception as exc:  # noqa: BLE001 - the admin gets told, rather than a traceback
            log.warning("punkbuster command failed: %s", exc)
            return None

    def apply_optional_mods(self) -> None:
        """Ask what is installed, and take the better verbs when a mod offers them.

        SourceMod's "B3 Say" is the case: with it there, `b3_say`/`b3_hsay`/`b3_psay` display much
        better on screen than the stock `sm_` verbs. The classic bot switched templates the same way,
        off the same `sm plugins list` reply.

        The profile is frozen — deliberately, so nothing loaded at runtime can edit a title — so this
        builds a **new** profile and swaps it in whole, rather than writing over a template in place.
        The parser is handed the same object, because two profiles that disagree would be worse than
        either of them being wrong.

        Silent when a mod is absent: that is the ordinary case, and a line about every mod the server
        has not got is noise. Never fatal — an optional mod is optional.
        """
        mods = self.profile.optional_mods
        reader = getattr(self.parser, "read_installed_mods", None)
        if not mods or reader is None or self._rcon is None:
            return
        replies: dict[str, frozenset[str]] = {}
        overrides: dict[str, object] = {}
        found: list[str] = []
        for mod in mods:
            if mod.command not in replies:
                try:
                    replies[mod.command] = frozenset(reader(self._rcon.command(mod.command)))
                except Exception as exc:  # noqa: BLE001 - an optional mod is optional
                    log.debug("could not list mods with %r: %s", mod.command, exc)
                    replies[mod.command] = frozenset()
            if mod.name not in replies[mod.command]:
                continue
            found.append(mod.name)
            overrides.update(mod.overrides)
        if not overrides:
            return
        unknown = set(overrides) - {f.name for f in fields(GameProfile)}
        if unknown:
            # A typo'd field name would otherwise be a no-op that looks like a working feature.
            raise ValueError(
                f"{self.profile.name}: optional mod overrides name fields a GameProfile "
                f"does not have: {', '.join(sorted(unknown))}"
            )
        # `Any` values, because the field names are only known at runtime: an override table is data
        # on the profile, so the type checker cannot pair a key with the field it names. The check
        # above is what stands in for it, and it is stricter in the way that matters — it runs
        # against the actual dataclass rather than against what a template was declared as.
        applied: dict[str, Any] = dict(overrides)
        self.profile = replace(self.profile, **applied)
        # One profile object, not two. The parser reads its own for guid rules and team names, and a
        # copy that had missed this swap would be a second, quietly different, answer to every
        # question the profile answers.
        self.parser.profile = self.profile
        # Rebuilt because one of the things a mod can change is whether the bot prefixes at all, and
        # the formatter was built at startup with the answer from before this question was asked.
        self.messages = self._build_messages()
        log.info(
            "%s: %s installed, using its verbs (%s)",
            self.profile.name,
            " and ".join(found),
            ", ".join(sorted(overrides)),
        )

    def read_server_info(self) -> None:
        """Ask the server to describe itself, for the engines that will not answer it as cvars.

        The classic bot's `getServerVars`. Only Frostbite declares a `server_info_command`: it has no
        cvars, so without this the bot does not know the server's name, its player limit, its
        gametype or how many rounds a map runs for — all of which `!status` and any scheduled
        announcement want, and every other family here gets from a cvar dump for free.

        The reply is *positional*, so the parser reads it. Never fatal: a server that will not
        describe itself is still a server the bot can moderate.
        """
        command = self.profile.server_info_command
        reader = getattr(self.parser, "read_server_info", None)
        if not command or reader is None or self._rcon is None:
            return
        try:
            info = reader(self._rcon.command(command))
        except Exception as exc:  # noqa: BLE001 - a server that will not describe itself still works
            log.warning("could not read server info (%r failed: %s)", command, exc)
            return
        if not info:
            return
        # Returned as cvar names so it goes through the same merge every other engine's values do —
        # `sv_hostname` and `sv_maxclients` are what `Game.update_cvars` keeps its named fields in
        # step with, and a second way of setting them is a second way for them to disagree.
        self.game.update_cvars(info)
        if not self.game.map_name and info.get("mapname"):
            self.game.map_name = info["mapname"]
        log.info(
            "%s: %s, %s players max, playing %s",
            self.profile.name,
            self.game.hostname or "(unnamed server)",
            self.game.max_players or "?",
            self.map_display(self.game.map_name) if self.game.map_name else "no map yet",
        )

    def _check_is_the_right_game(self) -> None:
        """Refuse to run against a server that is not the game this title parses.

        The classic bot's per-title `checkVersion`, and a refusal for the same reason it was one
        there: the six Frostbite titles share a parser and a set of verbs, so a `bf3` bot pointed at
        a Battlefield 4 server connects, logs in and then reads a grammar that is *almost* right.
        Half the events come out wrong and the rest do not come out at all, which is indistinguishable
        from a quiet server. Asking costs one command and the server answers with its own name.

        A server that will not answer is not treated as the wrong game — the same distinction
        `check_required_mod` draws, and for the same reason: "could not ask" and "answered wrongly"
        want different responses from whoever is reading the log.
        """
        check = self.profile.version_check
        if check is None or self._rcon is None:
            return
        try:
            reply = self._rcon.command(check.command)
        except Exception as exc:  # noqa: BLE001 - any transport failure means "could not ask"
            log.warning(
                "%s: could not check what game this server is (%r failed: %s); continuing",
                self.profile.name,
                check.command,
                exc,
            )
            return
        words = (reply or "").split()
        if not words:
            log.warning("%s: the server did not say what game it is; continuing", self.profile.name)
            return
        self.game.version = " ".join(words)
        log.info("%s: server version %s", self.profile.name, self.game.version)
        if words[0] != check.game_name:
            raise WrongGameError(
                f"server.game is {self.profile.name!r}, but this server answered "
                f"{check.command!r} with {words[0]!r} — it is not the game that title parses. "
                f"Set server.game to the title this server actually runs. Connecting anyway would "
                f"read the wrong grammar, which looks like a server where nothing ever happens."
            )
        if not check.min_build or len(words) < 2:
            return
        try:
            build = int(words[1])
        except ValueError:
            # A build number we cannot read is not evidence that it is too old.
            log.debug("%s: unreadable build number %r", self.profile.name, words[1])
            return
        if build < check.min_build:
            raise WrongGameError(
                f"this server is {check.game_name} build {build}, and the {self.profile.name} "
                f"parser was written against build {check.min_build} and later. Update the game "
                f"server: the events this bot reads were not all in that build."
            )

    def check_required_mod(self) -> None:
        """Refuse to run when the title needs a server-side mod that is not installed.

        Only asked when there is an RCON connection to ask over: an offline `b3 replay` has no
        server, and a mod check there would fail on every run for no reason.

        A server that does not answer at all is *not* treated as a missing mod. The distinction
        matters because the responses differ: a missing mod is an install job, and a silent server is
        a wrong host, port or password — and reporting the second as the first sends the operator to
        install something they already have.
        """
        required = self.profile.required_mod
        if required is None or self._rcon is None:
            return
        try:
            reply = self._rcon.command(required.command)
        except Exception as exc:  # noqa: BLE001 - any transport failure means "could not ask"
            log.warning(
                "%s: could not check for %s (%r failed: %s); continuing, but %s commands will not "
                "work if it is absent",
                self.profile.name,
                required.name,
                required.command,
                exc,
                required.name,
            )
            return
        if required.expect.lower() in (reply or "").lower():
            log.info("%s: %s is installed", self.profile.name, required.name)
            return
        raise MissingServerModError(
            f"{self.profile.name} requires {required.name} on the game server, and "
            f"{required.command!r} answered {(reply or '').strip()[:120]!r}. Every command reply, "
            f"every private message and the whole of !ban go through {required.name} on this "
            "engine, so the bot would start and silently do nothing. Install it and try again."
        )

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
        if self._reject_long_name(event.client):
            return
        if self._authenticate(event.client) and not event.client.ip:
            # The join line has no IP; it only exists in the server's status table, which lags the
            # join by a second or two. Poll for it in the background (legacy Parser.authorizeClients).
            self._schedule_auth(event.client)

    def _on_client_update(self, event: Event) -> None:
        """A player changed their userinfo; enforce the engine's name-length limit.

        Checked on update as well as on join because a rename mid-game overflows the same string,
        and on Urban Terror a userinfo line is how a rename arrives.
        """
        if event.client is not None:
            self._reject_long_name(event.client)

    def _reject_long_name(self, client: Client) -> bool:
        """Kick a player whose name overflowed the protocol. True if they were thrown out.

        The parser has already truncated the name (:attr:`GameProfile.name_max_length`). Kicking
        needs the config and an RCON socket, so it happens here; `server.allow_long_names: true`
        keeps the truncation and skips the kick.
        """
        if not client.name_overflow or self.config.server.allow_long_names:
            return False
        client.name_overflow = False  # dealt with; a rejoin gets a fresh verdict
        client.rejected = True
        limit = self.profile.name_max_length
        self.kick(client, reason=self.messages.get("name_too_long", limit=limit))
        return True

    def _on_disconnect(self, event: Event) -> None:
        if event.client is not None and event.client.cid is not None:
            self.auth.cancel(event.client.cid)

    def _on_round_start(self, event: Event) -> None:
        """Track the match from the ``InitGame`` cvar dump (the legacy ``Game`` object)."""
        cvars = event.data if isinstance(event.data, dict) else {}
        now = self.clock.now()
        # A new round puts everybody back in play. Without this a player killed in the last seconds
        # of a round stays "dead" for the whole of the next one on any engine that does not report
        # spawns, and `say_dead` would keep talking to them while `smart_say` answered them privately
        # in front of nobody.
        for client in self.clients.connected():
            client.alive = True
        new_map = cvars.get("mapname", "")
        if new_map and new_map != self.game.map_name:
            previous = self.game.map_name
            self.game.start_map(cvars, now)
            if previous:
                self.bus.publish_soon(Event(EventType.GAME_MAP_CHANGE, data=new_map))
        else:
            self.game.update_cvars(cvars)
            self.game.start_round(now)

    def _on_death(self, event: Event) -> None:
        """The victim of a kill is the `target`; on a suicide it is the `client`.

        Both spellings are handled because both occur: a kill names two people and a suicide names
        one, and reading only the first would leave every self-inflicted death marked alive.
        """
        victim = event.target if event.target is not None else event.client
        if victim is not None:
            victim.alive = False

    def _on_spawn(self, event: Event) -> None:
        if event.client is not None:
            event.client.alive = True

    def _on_server_info(self, event: Event) -> None:
        """The server said what it is called and how many players it holds.

        Altitude announces this on its own line; every other family here reports the same two values
        as cvars, which :meth:`Game.update_cvars` picks up. Before this the event was published and
        nothing listened, so the values were parsed and thrown away.
        """
        name = str(event.data or "")
        if name:
            self.game.hostname = name
        limit = as_int(event.extra.get("max_players"), 0)
        if limit:
            self.game.max_players = limit

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
                return AuthInfo(cid=cid, guid=info.guid, ip=info.ip, alt_guid=info.alt_guid)
        return None

    def _on_resolved(self, info: AuthInfo, attempt: int) -> None:
        """Fill in what the log could not tell us, once the status table catches up."""
        client = self.clients.get_by_cid(info.cid)
        if client is None:
            return
        if client.guid and info.guid:
            spelled = PlayerInfo(cid=info.cid, guid=info.guid, alt_guid=info.alt_guid)
            if not self._names_the_same_player(client.guid, spelled):
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
                # Read before the save below, which sets `time_edit` to now: this is the only moment
                # "when were they last here" still exists. See `Client.last_visit`.
                client.last_visit = record.time_edit
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
            self._send_penalty(
                self.profile.ban_template, client, self._penalty_values(client, penalty.reason)
            )
            log.info("rejected banned client %s (%s)", client.name, client.guid)
            self.bus.publish_soon(Event(EventType.CLIENT_BAN, client=client, data=penalty.reason))
            return

        remaining = int(max(1, (penalty.time_expire - self.clock.epoch()) // 60))
        self._send_tempban(client, remaining, penalty.reason)
        log.info(
            "rejected tempbanned client %s (%s), %d min left", client.name, client.guid, remaining
        )
        self.bus.publish_soon(Event(EventType.CLIENT_BAN_TEMP, client=client, data=penalty.reason))

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

        Falls back to a round trip on an RCON object with no `write`, which covers the fakes in the
        test suite and any transport that does not implement one.
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
        """Message one player, split across as many lines as the game's chat limit needs.

        Marked with `bot.pm_prefix` on top of the bot's own prefix — the classic's `pmPrefix` — so a
        player can tell a reply meant for them from a broadcast that happens to name them.

        The name is offered as well as the slot because engines disagree about which addresses a
        player: Altitude's `serverWhisper` takes the name, and it has no verb that takes a slot.
        """
        for line in self.messages.wrap(text, self.config.bot.pm_prefix):
            self._tell_line(client, line)

    def _tell_line(self, client: Client, line: str) -> None:
        """Send one already-wrapped line to one player.

        Shared by `tell` and `say_dead`, which differ only in which prefix the text carries — the
        addressing is identical, and two copies of it would be two places for an engine's spelling
        of "this player" to be wrong.
        """
        self._write(
            self.profile.tell_template
            % {
                "cid": client.cid,
                "name": sanitize_rcon_value(client.name),
                # Homefront's `adminpm` takes the player's Steam id, not a slot or a name.
                "guid": sanitize_rcon_value(client.guid),
                "text": sanitize_rcon_value(line, None),
            }
        )

    def say_dead(self, text: str) -> None:
        """Say something only the players waiting to respawn will see.

        There is no verb for this on any engine here. The classic bot did it by sending the same
        private message to every dead client in turn, and so does this — which means it works on any
        title with a `tell`, not only on the two the classic implemented it for.

        The prefix matters as much as the routing: without it a dead player cannot tell a message
        aimed at them from one everybody got, which is the whole point of sending it separately.
        """
        dead = [c for c in self.clients.connected() if not c.alive and c.cid is not None]
        if not dead:
            return
        # Wrapped once and sent to each of them, rather than going through `tell`: this is dead
        # *chat*, not a private message, so it takes `dead_prefix` and not `[pm]` — and wrapping it
        # per recipient would be the same work repeated once per corpse.
        lines = self.messages.wrap(text, self.config.bot.dead_prefix)
        for client in dead:
            for line in lines:
                self._tell_line(client, line)

    def smart_say(self, client: Client, text: str) -> None:
        """Answer where the player who asked will actually see it — the legacy ``smartSay``.

        A dead or spectating player on these engines is shown only the dead/spectator chat, so a
        reply sent with `say` is a reply they never read while everybody else does. Alive: say it to
        the server. Dead or spectating: say it to the dead.
        """
        if client.alive and client.team != "spec":
            self.say(text)
        else:
            self.say_dead(text)

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

    def send_rcon(self, command: str) -> str:
        """Send an operator-written rcon line and return the reply — see `Console.send_rcon`.

        Logged at info, unlike the verbs above: a command whose text came out of a config file is one
        nobody here can validate, so the log is the only record of what was actually sent when it
        turns out to have done something surprising.
        """
        text = command.strip()
        if not text:
            return ""
        log.info("%s: sending operator-defined command %r", self.profile.name, text)
        return self._ask(text)

    def get_players(self) -> list[PlayerInfo]:
        if self._rcon is None:
            return []
        # Some protocols do not answer this as *text*: Frostbite replies with a structured block
        # carrying its own field names. A client that can read its own reply is asked to, rather than
        # having it flattened to a string and parsed back out again. Same duck-typed seam as `write`.
        own_reader = getattr(self._rcon, "get_players", None)
        if own_reader is not None:
            return list(own_reader())
        if not self.profile.status_commands:
            # This engine cannot be asked at all — Altitude's "rcon" is a file the server reads, and
            # nothing ever answers. Sending the query anyway would append a bogus command to that
            # file; answering "nobody is connected" would be worse, because `_reconcile` would then
            # drop every player the log told us about. The parser keeps the roster instead.
            parser_reader = getattr(self.parser, "get_players", None)
            if parser_reader is not None:
                return list(parser_reader())
            return [
                PlayerInfo(cid=c.cid, name=c.name, guid=c.guid, ip=c.ip)
                for c in self.clients.connected()
                if c.cid is not None
            ]
        return self._identify(self._read_status()[1])

    def _identify_from_punkbuster(self, players: list[PlayerInfo]) -> list[PlayerInfo]:
        """Fill in what PunkBuster knows: a PunkBuster id for everyone, and a guid for those with none.

        This is what PunkBuster is *for*, on the engines that most need it. A plain Quake 3 or an
        older Call of Duty server may hand out no usable `cl_guid` at all, so without this a player
        on one of those titles cannot hold a level or be matched against a ban — the bot sees a name
        and an address and nothing that survives either being changed.

        The id is recorded on `Client.pbid` in every case, because it is a *second* identity rather
        than a replacement: a player's guid and their PunkBuster id are different things, and a
        plugin or an admin looking one up wants the one they asked for.

        Skipped when the roster is empty, so an idle server is not asked about nobody.
        """
        if self.punkbuster is None or not players:
            return players
        try:
            seen = self.punkbuster.player_list()
        except Exception as exc:  # noqa: BLE001 - PunkBuster being unavailable is not fatal
            log.debug("punkbuster player list failed: %s", exc)
            return players
        if not seen:
            return players
        filled: list[PlayerInfo] = []
        for info in players:
            found = seen.get(info.cid)
            if found is None:
                filled.append(info)
                continue
            client = self.clients.get_by_cid(info.cid)
            if client is not None and client.pbid != found.pbid:
                client.pbid = found.pbid
                if client.id is not None:
                    self.storage.save_client(client)
            # Only as a *fallback* identity. Where the engine states a guid that is the one to key
            # on, because it is what an imported classic database and every existing ban already
            # use; overwriting it with a PunkBuster id would orphan a player's whole history.
            if info.guid:
                filled.append(info)
                continue
            log.info("punkbuster identified slot %s, which the engine did not", info.cid)
            filled.append(replace(info, guid=found.pbid, ip=info.ip or found.ip))
        return filled

    def _identify(self, players: list[PlayerInfo]) -> list[PlayerInfo]:
        """Ask the server directly about any player whose identity the status table does not carry.

        Only the Quake 3 family declares a `userinfo_command`, and it is the family that needs one:
        its status table reports a slot, a name and a ping, but the persistent id lives in the
        `ClientUserinfo` line — so a bot started mid-match, or one that missed that line, has a player
        it cannot authenticate, hold a level for, or match a ban against. The classic bot's
        `queryClientUserInfoByCid`.

        Runs wherever `get_players` runs, which is on a worker thread for both the auth poll and the
        five-minute sync, so the round trips are off the event loop.

        **Asked once per slot, not once per poll.** A server that sets no `cl_guid` at all — a plain
        Enemy Territory server is exactly that — would otherwise be asked about every player every
        five minutes, forever, for an answer that is never going to arrive.
        """
        players = self._identify_from_punkbuster(players)
        command = self.profile.userinfo_command
        reader = getattr(self.parser, "read_userinfo", None)
        if not command or reader is None or self._rcon is None:
            return players
        for info in players:
            if info.guid or info.cid in self._asked_userinfo:
                continue
            self._asked_userinfo.add(info.cid)
            try:
                reply = self._ask(command % {"cid": sanitize_rcon_value(info.cid)})
            except Exception as exc:  # noqa: BLE001 - one slot failing must not lose the roster
                log.debug("userinfo query for slot %s failed: %s", info.cid, exc)
                continue
            line = reader(info.cid, reply)
            if line:
                # Fed back as a log line so it goes through the same handler an ordinary
                # `ClientUserinfo` would — see `Q3Parser.read_userinfo` for why that matters.
                self.parser.parse_line(line)
                log.info("identified slot %s from the server's own userinfo", info.cid)
        # Hand back what the parser learned, so `_reconcile` sees the identity on this pass rather
        # than on the next one — otherwise the player is adopted with no guid and authenticated as a
        # stranger before the answer we just fetched is ever looked at.
        enriched: list[PlayerInfo] = []
        for info in players:
            client = self.clients.get_by_cid(info.cid)
            if not info.guid and client is not None and client.guid:
                enriched.append(replace(info, guid=client.guid))
            else:
                enriched.append(info)
        return enriched

    def _read_status(self) -> tuple[str, list[PlayerInfo]]:
        """Ask the server for its status table, and parse it. Returns ``(map name, players)``.

        A title may have more than one way to ask — see
        :attr:`b3.parsers.profile.GameProfile.status_commands`. CoD4X is why: a server running the
        `b3hide` mod answers `b3status` with the full table and leaves hidden players out of `status`
        entirely, while a server without the mod does not know `b3status` at all. They are tried in
        order until one yields players, and the winner is remembered so an ordinary server pays for
        the first attempt once rather than on every five-minute sync. If the winner later stops
        answering the search starts again, which is what makes loading the mod mid-life work.

        The map name is taken from the first reply that carries one, independently of the players: an
        empty server has a map but no rows, and `!map` still has to work on it.
        """
        if self._rcon is None:
            return "", []
        commands = self.profile.status_commands
        if self._status_command in commands:
            commands = (
                self._status_command,
                *(c for c in commands if c != self._status_command),
            )
        map_name = ""
        unreadable: list[str] = []
        for command in commands:
            # Cached briefly by Rcon.get_status, so an auth poll and a `!status` in the same second
            # cost one round trip. Keyed by command, so a fallback is not served the first reply.
            get_status = getattr(self._rcon, "get_status", None)
            raw = get_status(command) if get_status else self._rcon.command(command)
            found_map, players = status_parser.parse_status(
                raw, self.profile.status_patterns, self.profile.identity_field
            )
            map_name = map_name or found_map
            if players:
                self._status_command = command
                self._status_unreadable = False
                return map_name, players
            unreadable.extend(status_parser.unparsed_rows(raw, self.profile.status_patterns))
        self._status_command = ""
        # Rows we could not read mean the roster is **unknown**, which is a different answer from
        # "empty" and must not be allowed to become one: `_reconcile` drops every player missing from
        # the list it is given, so reporting nobody would disconnect the whole server every sync,
        # losing each player's session for as long as the mismatch lasts. `sync` reads this and
        # declines to reconcile. It is the same reasoning `get_players` already applies to an engine
        # that cannot be asked at all.
        self._status_unreadable = bool(unreadable)
        if unreadable and not self._warned_about_status:
            # An empty server and a table shape we cannot read look identical from here, and they
            # want opposite responses. Saying which it is beats guessing at a looser pattern: a
            # pattern that is *almost* right reads the id column as part of the player's name.
            self._warned_about_status = True
            log.warning(
                "%s: the server answered with %d row(s) this bot cannot read, so it cannot tell who "
                "is playing; the roster is being left alone. The first is: %s",
                self.profile.name,
                len(unreadable),
                unreadable[0],
            )
        return map_name, []

    def get_cvar(self, name: str) -> str | None:
        if self._rcon is None or not name:
            # An empty name is not a question. Sending one to a BattlEye server would frame an empty
            # command — its *keepalive* — which never gets a reply, so the caller would stall for the
            # whole rcon timeout and then be told the server is broken.
            return None
        asked = self.profile.get_cvar_template % {"name": sanitize_rcon_value(name)}
        value = status_parser.parse_cvar(name, self._ask(asked))
        if value is not None:
            self.game.update_cvars({name: value})
        return value

    def set_cvar(self, name: str, value: str) -> None:
        # The template quotes the value, so a quote inside it would close the quoting early — hence
        # the sanitising, which strips them. The verb comes from the profile because Black Ops does
        # not accept a plain `set` over rcon (see GameProfile.set_template).
        #
        # A cvar value gets its own, much larger cap. The 128-character one that suits a ban reason
        # cut a real value off mid-word: a Quake 3 `sv_mapRotation` and Black Ops's
        # `playlist_excludeMap` both run to several hundred characters, and a rotation truncated at
        # `mp_russia` is one the engine rejects with nothing said anywhere. And when even this cap
        # has to bite, it says so — that is the whole difference from what was here before.
        cleaned = sanitize_rcon_value(value, None)
        if len(cleaned) > MAX_RCON_CVAR:
            log.warning(
                "set_cvar: %s is %d characters, which will not fit in one rcon datagram; "
                "sending the first %d",
                name,
                len(cleaned),
                MAX_RCON_CVAR,
            )
            cleaned = cleaned[:MAX_RCON_CVAR].rstrip()
        self._send(
            self.profile.set_template % {"name": sanitize_rcon_value(name), "value": cleaned}
        )
        self.game.update_cvars({name: value})

    def team_id(self, team: str) -> str:
        """See `Console.team_id`."""
        return self.profile.team_id(team)

    def rcon_words(self, command: str) -> list[str]:
        """See `Console.rcon_words`. Falls back to the flattened reply on the eight text families.

        **A refusal is an answer here, not an exception.** The Frostbite client raises on a reply that
        does not begin `OK`, because at that level a refused command really is an error — but the
        engine's word for what was wrong (`PlayerAlreadyInList`, `InvalidArguments`,
        `CommandDisallowedOnRanked`) is the most useful thing a plugin can tell an admin, and a plugin
        that had to catch a *net-layer* exception to read it would be a plugin tied to one engine.
        The words come back instead, and nothing here raises: a caller checks the first word or
        ignores it, exactly as it would on the text families where a refusal has always been text.
        """
        words = getattr(self._rcon, "command_words", None)
        if words is None:
            reply = self.send_rcon(command)
            return [reply] if reply else []
        try:
            return list(words(command))
        except Exception as exc:  # noqa: BLE001 - a refusal is the server's answer, not our failure
            refusal = [str(word) for word in getattr(exc, "words", []) if str(word)]
            log.info("%s: %r was refused: %s", self.profile.name, command, refusal or exc)
            return refusal

    def get_map(self) -> str | None:
        if self._rcon is None or not self.profile.status_commands:
            # Nothing to ask (see `get_players`), so the answer is what the log last said — which on
            # Altitude is accurate: it announces every map change.
            return self.game.map_name or None
        map_name, _ = self._read_status()
        if map_name:
            self.game.map_name = map_name
        return map_name or None

    def get_maps(self) -> list[str]:
        # A client that holds the rotation itself is asked first. Frostbite is the case: its map list
        # is neither a cvar nor a text reply but a self-describing block that has to be paged
        # through, so the client reads it for the same reason it reads its own player block.
        own_rotation = getattr(self._rcon, "get_maps", None)
        if own_rotation is not None:
            return [str(m) for m in own_rotation()]
        if not self.profile.rotation_cvar:
            # No cvars -- but some of those engines will still answer a question. Ravaged's
            # `getmaplist false` returns the rotation in order, and its parser knows the row shape;
            # asking is this method's job and reading the answer is the parser's, exactly as with the
            # roster. Without this, `!maprotate` and `!nextmap` would quietly report nothing on a
            # game that can in fact answer both.
            reader = getattr(self.parser, "read_maps", None)
            if self.profile.maplist_command and reader is not None:
                reply = self._ask(self.profile.maplist_command)
                return list(reader(reply)) if reply.strip() else []
            # Or the parser may simply have it. Frontline answers `MapList` with a pushed message
            # naming no command, so nothing can correlate it to a question -- the client asks on an
            # interval and the parser keeps the answer. Same seam as `get_players`.
            own = getattr(self.parser, "get_maps", None)
            if own is not None:
                return list(own())
            return []  # nothing to read and nothing to ask (Arma changes mission by command)
        rotation = self.get_cvar(self.profile.rotation_cvar)
        return status_parser.parse_rotation(rotation) if rotation else []

    def set_next_map(self, name: str) -> bool:
        """Write the map that comes next into the cvar this title holds it in."""
        if not self.profile.next_map_cvar:
            return False
        self.set_cvar(self.profile.next_map_cvar, name)
        return True

    def get_next_map(self) -> str | None:
        """The map after the current one in the rotation — wrapping around at the end.

        Unless the engine simply says: Frontline answers `GetNextMap` outright, and a server
        whose rotation is randomised or voted on knows better than any arithmetic done here.
        """
        stated = getattr(self.parser, "get_next_map", None)
        if stated is not None:
            answer = stated()
            if answer:
                return str(answer)
        # Or a cvar may hold it outright. Asked before the arithmetic below rather than after,
        # because on the engines that have such a cvar the arithmetic is not a worse answer, it is a
        # wrong one: their map list is everything installed, not a rotation (see next_map_cvar).
        if self.profile.next_map_cvar:
            named = self.get_cvar(self.profile.next_map_cvar)
            # And on those engines an unanswered cvar means "nobody knows", **not** "work it out from
            # the map list": falling through would print a map picked out of an alphabetical listing
            # of everything installed, which reads to an admin as a fact about the rotation.
            return named.strip() if named and named.strip() else None
        maps = self.get_maps()
        if not maps:
            return None
        current = (self.get_map() or self.game.map_name).strip().lower()
        for index, name in enumerate(maps):
            if name.strip().lower() == current:
                return maps[(index + 1) % len(maps)]
        return maps[0]  # current map is not in the rotation: the next one played is its first

    def map_display(self, map_id: str) -> str:
        """The name to show a player for a map id — "Operation Metro" rather than `MP_Subway`.

        One helper rather than a call at each site, so `!map`, `!maps`, `!nextmap` and `!status`
        cannot disagree about what a map is called.
        """
        return self.profile.map_display(map_id)

    def parse_map_request(self, text: str) -> MapRequest:
        """Split a `!map` argument the way this engine splits it — see `GameProfile.map_arguments`.

        On the port rather than left to the plugin for the same reason `map_display` is: the answer
        is a fact about the title, and the admin plugin is deliberately not given the profile.
        """
        return self.profile.parse_map_request(text)

    def map_usage(self) -> str:
        """The `!map` arguments past the map name, in this engine's separator — "" on most titles."""
        return self.profile.map_usage()

    def change_map(self, name: str, extras: Mapping[str, str] | None = None) -> None:
        """Load a named map. Some engines need two commands for it, so the template may hold both.

        ``extras`` carries the engine-specific arguments an admin may give after the map —
        :attr:`GameProfile.map_arguments` names them, and `cmd_map` parses them off the command line.
        A title that declares none never sees this and is rendered exactly as before, positionally.

        Some engines cannot express a map change as a template at all. Frostbite is the case: loading
        a named map means reading the rotation, adding the map at a computed index, pointing the
        server at that index and then ending the round — four commands where one of the arguments is
        arithmetic over a reply. A client that can do it is asked to, the same duck-typed seam
        `get_players` and `remove_ban` already use.
        """
        values = dict(extras or {})
        own = getattr(self._rcon, "change_map", None)
        if own is not None:
            # Unsanitised on purpose, and only safe because of what is on the other side: a client
            # implementing this seam speaks a framed protocol where each argument is its own
            # length-prefixed word, so there is no command line for a quote or a semicolon to break
            # out of. A template, which is a string, is sanitised below.
            own(name, values)
            return
        if not self.profile.map_template:
            log.warning(
                "%s has no verb for loading a named map; only !maprotate can move it on",
                self.profile.name,
            )
            return
        if self.profile.map_arguments:
            # Named substitution, and every declared argument is given a value even when the admin
            # left it out: a template naming a placeholder the caller omitted would raise KeyError
            # here rather than at the point the profile was written, which is the worst place to
            # find out. An unfilled extra renders empty and the whitespace is tidied below.
            rendered = self.profile.map_template % {
                "map": sanitize_rcon_value(name),
                **{
                    key: sanitize_rcon_value(str(values.get(key, "")))
                    for key in self.profile.map_arguments
                },
            }
        else:
            rendered = self.profile.map_template % sanitize_rcon_value(name)
        for command in rendered.splitlines():
            # `split()`/`join` rather than `strip`: an omitted middle argument leaves a double space
            # inside the command, and some of these servers read that as an empty positional rather
            # than as spacing.
            collapsed = " ".join(command.split())
            if collapsed:
                self._send(collapsed)

    def rotate_map(self) -> None:
        if not self.profile.rotate_command:
            # No single verb for it. Some of those engines can still be rotated in two steps: ask
            # what comes next, then load it — which is exactly what the classic Source parser did,
            # there being no `map_rotate` on that engine either. Only attempted when the next map is
            # actually *known*, so this never degenerates into loading a guess.
            following = self.get_next_map()
            if following:
                log.info("%s: rotating to %s", self.profile.name, following)
                self.change_map(following)
                return
            # Said out loud, because the alternative is an admin typing `!maprotate`, being told
            # nothing, and assuming it worked.
            log.warning(
                "%s has no map-rotation command; skip to a named map with !map instead",
                self.profile.name,
            )
            return
        self._send(self.profile.rotate_command)

    def mute(self, client: Client, minutes: float) -> bool:
        """Silence a player for `minutes`, and remember when to let them speak again.

        The engine's verb takes seconds and the server does its own counting, so the deadline here is
        a *safety net* rather than the mechanism: a bot that restarts, or an engine build whose mute
        does not expire on its own, would otherwise leave somebody silenced for good. It is also the
        single place that knows a mute is running, which is what stops two plugins with two ladders
        from lifting each other's.

        A longer mute replaces a shorter one; a shorter one does not cut a longer one short.
        """
        if not self.supports_verb("mute"):
            return False
        seconds = max(1, int(minutes * 60))
        until = self.clock.now() + seconds
        if until <= self._muted.get(client.cid or "", 0.0):
            log.debug("%s is already muted for longer than %ds", client.name, seconds)
            return True
        self._muted[client.cid or ""] = until
        return self.apply_verb("mute", client, seconds=str(seconds))

    def unmute(self, client: Client) -> bool:
        """Let a player talk again. `mute <cid> 0` is how these engines lift one."""
        self._muted.pop(client.cid or "", None)
        if not self.supports_verb("mute"):
            return False
        return self.apply_verb("mute", client, seconds="0")

    def muted_until(self, client: Client) -> float:
        """When this player's mute runs out, or 0.0. Read by the plugins that set one."""
        return self._muted.get(client.cid or "", 0.0)

    def _lift_expired_mutes(self) -> None:
        """Lift the mutes whose time is up. One pass for the server, on the scheduler."""
        now = self.clock.now()
        for cid, until in list(self._muted.items()):
            if now < until:
                continue
            del self._muted[cid]
            client = self.clients.get_by_cid(cid)
            if client is not None:
                self.apply_verb("mute", client, seconds="0")
                log.info("mute on %s has run out", client.name)

    def supports_verb(self, name: str) -> bool:
        """Whether this title declares a verb for doing ``name`` to a player."""
        return name in self.profile.player_verbs

    def apply_verb(self, name: str, client: Client, **values: str) -> bool:
        """Do ``name`` to a player — `slap`, `nuke`, `mute`. False if this engine cannot.

        Everything substituted is sanitised, because `%(name)s` is a string the *player* chose. A
        template whose arguments were not all supplied is refused and said out loud rather than sent
        half-rendered: §1.10 is the entry about a penalty verb that could not be filled in being sent
        anyway, and this is the same rule.
        """
        lines = self._render_verb(name, client, **values)
        if lines is None:
            return False
        for line in lines:
            self._send(line)
        return True

    def ask_verb(self, name: str, client: Client, **values: str) -> str | None:
        """See `Console.ask_verb`: do it, and hand back what the server said.

        The same rendering and the same refusals as `apply_verb`; only the sending differs, because
        for a handful of verbs the reply *is* the result — Urban Terror's `startserverdemo` answers
        with the filename it has started writing. A template holding more than one command answers
        with the last reply, which is the only thing a caller could use.
        """
        lines = self._render_verb(name, client, **values)
        if lines is None:
            return None
        reply = ""
        for line in lines:
            reply = self._ask(line)
        return reply

    def _render_verb(self, name: str, client: Client, **values: str) -> list[str] | None:
        """A player verb as the lines to send, or None when it cannot be filled in."""
        template = self.profile.player_verbs.get(name)
        if not template:
            log.warning("%s has no verb for %r", self.profile.name, name)
            return None
        fields = {
            "cid": sanitize_rcon_value(client.cid or ""),
            "name": sanitize_rcon_value(client.name),
            # The player's persistent id, because on some engines that **is** the handle a verb takes:
            # Homefront's `admin kill`, `admin forceteamswitch` and `admin makespectate` all name the
            # Steam id, and its `cid` is the player's name. Offered here for the same reason
            # `_penalty_fields` and `tell` offer it — a profile names the field its own engine wants,
            # and a verb template was the one place that could not.
            "guid": sanitize_rcon_value(client.guid),
            **{key: sanitize_rcon_value(str(value)) for key, value in values.items()},
        }
        try:
            rendered = template % fields
        except KeyError as missing:
            log.error(
                "%s: the %r verb is %r and nothing was passed for %s, so it was not sent",
                self.profile.name,
                name,
                template,
                missing,
            )
            return None
        return [" ".join(line.split()) for line in rendered.splitlines() if line.split()]

    def supports_server_verb(self, name: str) -> bool:
        """Whether this title declares a verb for ``name`` that names no player."""
        return name in self.profile.server_verbs

    def apply_server_verb(self, name: str, **values: str) -> bool:
        """Run a verb that names no player. Same rules as `apply_verb`, minus the client."""
        lines = self._render_server_verb(name, **values)
        if lines is None:
            return False
        for line in lines:
            self._send(line)
        return True

    def ask_server_verb(self, name: str, **values: str) -> str | None:
        """See `Console.ask_server_verb` — `ask_verb` for the verbs that name no player."""
        lines = self._render_server_verb(name, **values)
        if lines is None:
            return None
        reply = ""
        for line in lines:
            reply = self._ask(line)
        return reply

    def _render_server_verb(self, name: str, **values: str) -> list[str] | None:
        """A server verb as the lines to send, or None when it cannot be filled in."""
        template = self.profile.server_verbs.get(name)
        if not template:
            log.warning("%s has no verb for %r", self.profile.name, name)
            return None
        try:
            rendered = template % {
                key: sanitize_rcon_value(str(value)) for key, value in values.items()
            }
        except KeyError as missing:
            log.error(
                "%s: the %r verb is %r and nothing was passed for %s, so it was not sent",
                self.profile.name,
                name,
                template,
                missing,
            )
            return None
        return [" ".join(line.split()) for line in rendered.splitlines() if line.split()]

    def can_cancel_vote(self) -> bool:
        """Whether this title has a verb for stopping a vote — see `GameProfile.veto_command`."""
        return bool(self.profile.veto_command)

    def cancel_vote(self) -> bool:
        """Cancel the vote in progress. False where this engine has no verb for it."""
        if not self.profile.veto_command:
            return False
        self._send(self.profile.veto_command)
        return True

    def sync(self) -> list[Client]:
        """Reconcile the in-memory client list against the server's — the legacy ``sync``.

        Log lines are the primary source of who is connected, but they are not sufficient: a bot
        started mid-match never saw anyone join, and a dropped log line leaves a ghost in the slot
        list forever. This adopts players we do not know about, drops those the server no longer
        lists, and fills in IPs along the way.

        Skipped entirely when the server's answer could not be read (see `_read_status`): the
        departures half of reconciling is destructive, and an unreadable table is no evidence that
        anybody left.
        """
        players = self.get_players()
        if self._status_unreadable:
            return self.clients.connected()
        return self._reconcile(players)

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
            # The other half of `GameProfile.canonical_guid`, the log path being the first: a title
            # that spells one account two ways usually does it once per source, and CS2 does exactly
            # that -- `[U:1:N]` in the log, a Steam64 in this table. Both have to arrive here in the
            # same spelling or the comparison below reads the player as a stranger in their slot.
            info = replace(info, guid=self.profile.canonical_guid(info.guid))
            client = self.clients.get_by_cid(info.cid)
            if client is not None and client.guid and info.guid:
                if not self._names_the_same_player(client.guid, info):
                    # Someone else is in that slot now; forget the stale record first.
                    self._drop_client(client)
                    client = None
                elif not client.guid.lower().startswith(info.guid.lower()):
                    # The same person under the other spelling this engine uses. Converge, or they
                    # keep two records and whichever spoke last decides who they are.
                    self._adopt_identity(client, info)
            if client is None:
                # An AI player takes a slot and appears in this table like anyone else. It must not
                # be given an identity: every bot on the server reports the same guid, so they would
                # all end up sharing one database row — with one level and one ban history between
                # them. The log path drops it too (CodParser._valid_guid), and this is the other way
                # a client can be created.
                is_bot = self.profile.is_bot_guid(info.guid)
                guid = "" if is_bot else info.guid
                client = Client(cid=info.cid, name=info.name, guid=guid, ip=info.ip, is_bot=is_bot)
                self.clients.add(client)
                log.info("sync: adopting player %s in slot %s", info.name, info.cid)
                self._authenticate(client)
                continue
            self._apply_team(client, info)
            if info.ip and client.ip != info.ip:
                client.ip = info.ip
                if client.id is not None:
                    self.storage.save_client(client)
                    self._record_history(client)
                # Said out loud, because on these engines this is where an address comes from: the
                # log line that authenticates a player carries no IP at all. A plugin that acts on
                # one — `banlist` is the case — would otherwise only ever see the addresses that
                # arrive through the auth-resolution poll, and miss every player already on the
                # server when the bot started.
                self.bus.publish_soon(Event(EventType.CLIENT_UPDATE, client=client))

        for client in self.clients.connected():
            if client.cid is not None and client.cid not in live:
                log.info("sync: %s is no longer on the server", client.name)
                self._drop_client(client)
        return self.clients.connected()

    @staticmethod
    def _names_the_same_player(guid: str, info: PlayerInfo) -> bool:
        """Whether a client's guid and a status row are one person, in either of the engine's spellings.

        A title that spells one account two ways usually does it once per source. CS2's two spellings
        convert into each other, which is what `GameProfile.canonical_guid` is for; CoD4X's do not —
        its log names a player by a per-session number and its status table by their Steam64 id, two
        unrelated numbers printed side by side in the same row. So the row itself is the only thing
        that can say they are the same person, and this asks it.
        """
        wanted = guid.lower()
        return any(
            spelling and wanted.startswith(spelling.lower())
            for spelling in (info.guid, info.alt_guid)
        )

    def _adopt_identity(self, client: Client, info: PlayerInfo) -> None:
        """Re-key a client onto the identity the server states, and authenticate them again.

        A client built from a log line is spelled the way the log spells it — on CoD4X, a per-session
        number — and has already been authenticated as whoever that number is: a stranger, with no
        level, no history and none of their bans. Left alone the same person holds two records that
        take turns, so an admin who has just been made superadmin is a nobody again a second later.
        `id` and `authed` are cleared so the record for the real identity is the one loaded.
        """
        log.info(
            "sync: slot %s is %s, which the log spells %s; taking the identity the server states",
            info.cid,
            info.guid,
            client.guid,
        )
        client.guid = info.guid
        client.id = None
        client.authed = False
        self._authenticate(client)

    def _apply_team(self, client: Client, info: PlayerInfo) -> None:
        """Take the team and squad off a roster row, where the engine states them.

        Only Frostbite does, and it is the only source for a player who has not changed team since
        the bot started — a plugin that swaps two players had nothing to go on for them. The change
        is published, because a team change read from the roster is the same fact as one read from a
        log line, and `afk` and `poweradminurt`'s team lock both listen for it.
        """
        if info.squad and client.squad != info.squad:
            client.squad = info.squad
        if not info.team:
            return
        team = self.profile.teams.get(info.team, info.team)
        if team == client.team:
            return
        client.team = team
        self.bus.publish_soon(Event(EventType.CLIENT_TEAM_CHANGE, data=team, client=client))

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
        self.messages = self._build_messages()
        self.tz = _resolve_timezone(config.bot.time_zone)
        self.scheduler.tz = self.tz
        log.info("configuration reloaded from %s", self.config_path)
        return config

    def _drop_client(self, client: Client) -> None:
        if client.cid is not None:
            self.clients.remove(client.cid)
            self.auth.cancel(client.cid)
            # The slot is free, so the next player in it is a fresh question. Without this, a server
            # that gave one player no id would never be asked about their replacement either.
            self._asked_userinfo.discard(client.cid)
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
        self,
        client: Client,
        reason: str = "",
        minutes: int = 0,
        admin: Client | None = None,
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
            # The same duration in seconds, because Frostbite's ban verb counts in those, and in
            # days, because Ravaged's does. Offered rather than converted at the call site, so a
            # profile picks the unit its own engine takes and nothing has to know which that is.
            "seconds": minutes * 60,
            "days": round(minutes / 1440, 4) if minutes else 0,
            # Who issued it. Homefront's ban verb names the admin — `admin kickban "<uid>" "<admin>"
            # "<reason>"` — and the classic parser read that name off the admin object without
            # checking there was one, so a ban with no admin behind it (a plugin's, or the console's)
            # raised AttributeError instead of banning anybody. The bot's own name stands in.
            "admin": sanitize_rcon_value(admin.name if admin is not None else self.config.bot.name),
        }

    def _send_penalty(self, template: str, client: Client, values: dict[str, object]) -> None:
        """Send a penalty verb, which may be **more than one command — one per line**.

        The same rule `change_map` already follows, and for the same reason: some engines need two
        commands to do one thing. On the Source titles a ban is ``sm_addban``, which writes the ban
        list, *and* ``sm_kick``, which removes the player who is on the server right now —
        ``sm_addban`` alone bans them and leaves them playing to the end of the map. The classic
        parser sent both, and a single-command version looks like it worked.

        A command naming a **slot** is skipped when the target has not got one, which is what lets the
        same template serve an offline ban: there is nobody in a slot to kick, and sending the verb
        anyway would name the literal string "None".
        """
        for raw in template.splitlines():
            command = raw.strip()
            if not command:
                continue
            if "%(cid)s" in command and client.cid is None:
                log.debug(
                    "%s: skipping %r — %s is not on the server, so there is no slot to name",
                    self.profile.name,
                    command.split(" ", 1)[0],
                    client.name or client.guid,
                )
                continue
            self._send(command % values)

    def _expressible(self, template: str, client: Client) -> bool:
        """Whether ``template`` can actually be filled in for this client.

        The case that matters: a ban verb keyed on the player's **guid** — CoD4X's `unban <guid>`,
        Frostbite's `banList.add guid <id>`, Altitude's `addBan <vaporId>` — and a client the engine
        has not identified. Substituting the empty string sends a syntactically valid command with a
        missing argument, which the server either rejects or, worse, applies to nothing while
        answering as though it worked. Removing them now with the kick verb is honest: the penalty is
        still recorded, so it reads back in `!lastbans`, and the log says why it cannot be enforced
        when they return.
        """
        if "%(guid)s" not in template or client.guid:
            return True
        log.warning(
            "%s: %s has no guid the server recognises, so %r cannot be sent — kicking instead. "
            "The penalty is recorded, but it cannot be re-applied when they reconnect, because "
            "there is no id to match them by",
            self.profile.name,
            client.name or client.cid,
            template.split(" ", 1)[0],
        )
        return False

    def kick(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        self._record_penalty(PenaltyType.KICK, client, admin, reason, 0, NEVER_EXPIRES)
        self.bus.publish_soon(Event(EventType.CLIENT_KICK, client=client, data=reason))
        self._send_penalty(self.profile.kick_template, client, self._penalty_values(client, reason))

    def ban(self, client: Client, reason: str = "", admin: Client | None = None) -> None:
        self._record_penalty(PenaltyType.BAN, client, admin, reason, 0, NEVER_EXPIRES)
        self.bus.publish_soon(Event(EventType.CLIENT_BAN, client=client, data=reason))
        self._send_ban(client, reason, admin)

    def _send_ban(self, client: Client, reason: str, admin: Client | None = None) -> None:
        """Send the ban verb — or the kick verb, when the ban cannot be expressed for this client."""
        template = self.profile.ban_template
        if not self._expressible(template, client):
            template = self.profile.kick_template
        self._send_penalty(template, client, self._penalty_values(client, reason, admin=admin))

    def tempban(
        self, client: Client, minutes: int, reason: str = "", admin: Client | None = None
    ) -> None:
        expire = self.clock.epoch() + minutes * 60
        self._record_penalty(PenaltyType.TEMPBAN, client, admin, reason, minutes, expire)
        self.bus.publish_soon(Event(EventType.CLIENT_BAN_TEMP, client=client, data=reason))
        self._send_tempban(client, minutes, reason, admin)

    def _send_tempban(
        self, client: Client, minutes: int, reason: str, admin: Client | None = None
    ) -> None:
        """Use the engine's own timed ban when it has one, else ban and re-apply on reconnect.

        Either way the penalty row is what the bot enforces: `enforce_ban` re-applies a tempban
        with its remaining time, so a server that forgets its banlist does not let anyone back in.
        """
        template = self.profile.tempban_template
        if template is None:
            self._send_ban(client, reason, admin)
            return
        if not self._expressible(template, client):
            self._send_penalty(
                self.profile.kick_template, client, self._penalty_values(client, reason)
            )
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
        self._send_penalty(template, client, self._penalty_values(client, reason, capped, admin))

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
            if self.profile.ban_template == self.profile.kick_template:
                # This engine has no ban verb at all — a "ban" here was only ever a kick, and the
                # record just lifted was the entire enforcement. Nothing is left half-done, so there
                # is nothing to warn about; the warning below would send an operator looking for a
                # server-side ban list that does not exist.
                log.info(
                    "%s has no server-side ban list; %s is unbanned and will not be kicked again",
                    self.profile.name,
                    client.name,
                )
                return
            # Our record is lifted either way, so the bot will not re-apply the ban, but the server
            # keeps its own entry. Reporting that is what stops a player staying banned by a server
            # ban nobody remembers making.
            log.warning(
                "%s cannot lift a ban over rcon; %s is unbanned in b3 but may still be on the "
                "server's own ban list — remove it there with the game's own tools",
                self.profile.name,
                client.name,
            )
            return
        if not self._expressible(self.profile.unban_template, client):
            # Nothing to send: this engine lifts a ban by id and we have none. Our own record is
            # already lifted above, which is what stops the bot re-applying it.
            return
        self._send_penalty(
            self.profile.unban_template, client, self._penalty_values(client, reason)
        )

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

    def find_clients(self, handle: str) -> list[Client]:
        """Every connected player a handle could mean, most specific match first.

        Callers with someone to ask should prefer this to :meth:`find_client`: with "bob" and
        "bobby" both in the server, taking the first substring match acts on whichever the roster
        happens to list first.
        """
        # @<dbid> -> database lookup. An id names one player or none.
        if handle.startswith("@"):
            try:
                found = self.storage.get_client_by_id(int(handle[1:]))
            except ValueError:
                return []
            return [found] if found is not None else []
        # exact slot id
        by_cid = self.clients.get_by_cid(handle)
        if by_cid is not None:
            return [by_cid]
        needle = handle.lower()
        connected = self.clients.connected()
        # An exact name wins outright, or "bob" could not be named while "bobby" is connected:
        # every handle for bob is also a handle for bobby.
        exact = [c for c in connected if c.name.lower() == needle]
        if exact:
            return exact
        return [c for c in connected if needle in c.name.lower()]

    def find_client(self, handle: str) -> Client | None:
        """The best guess for a handle, for callers that cannot ask for a more specific one.

        A command typed by an admin should use :meth:`find_clients` and refuse when the handle is
        ambiguous.
        """
        matches = self.find_clients(handle)
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
        # Prefer connected players' database records, then fall back to a stored-name/alias search.
        # Every match rather than the best one, so the caller can refuse an ambiguous handle for a
        # connected pair as it already does for two stored names.
        connected = [c for c in self.find_clients(term) if c.id is not None]
        if connected:
            return connected
        return self.storage.search_clients(term)
