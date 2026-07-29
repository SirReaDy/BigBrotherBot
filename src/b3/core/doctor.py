"""Pre-flight checks — `b3 doctor`.

A first B3 install fails in a handful of predictable ways: the RCON password is wrong, `game_log`
points at a path that does not exist (or that the bot's user cannot read), the database is not
writable, a plugin will not import. The classic bot surfaced all of these the same way — start it
and read a traceback, often after it had already partly worked — which is why "B3 won't connect"
was the single most common support question for fifteen years.

This asks each question directly, in the order a deployment actually fails, and says what to do
about it. Every check is independent: one failure never hides the next.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from b3.config.schema import Config

if TYPE_CHECKING:
    from collections.abc import Callable

    from b3.runtime.bot import RconClient

log = logging.getLogger(__name__)

#: Replies a Quake3/IW server sends when the RCON password is wrong.
BAD_PASSWORD_MARKERS = ("invalid password", "bad rcon", "badrcon", "invalid rcon")


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str
    hint: str = ""  # what to do about it, when it is not obvious

    @property
    def failed(self) -> bool:
        return self.status is Status.FAIL


def run_checks(
    config: Config,
    conf_dir: Path | None = None,
    *,
    rcon_factory: "Callable[[], RconClient] | None" = None,
) -> list[Check]:
    """Run every pre-flight check and return the results, in the order they matter."""
    return [
        _check_server(config),
        _check_timezone(config),
        _check_database(config),
        _check_game_log(config),
        _check_rcon(config, rcon_factory),
        *_check_plugins(config, conf_dir),
    ]


def _check_server(config: Config) -> Check:
    from b3.parsers.games import suggest
    from b3.runtime.bot import PROFILES

    game = config.server.game
    if game not in PROFILES:
        near = suggest(game)
        hint = f"known games: {', '.join(sorted(PROFILES))}"
        if near:
            hint = f"did you mean {' or '.join(repr(n) for n in near)}? ({hint})"
        return Check("game", Status.FAIL, f"no parser for {game!r}", hint)
    return Check("game", Status.OK, f"{game}, server {config.server.host}:{config.server.port}")


def _check_timezone(config: Config) -> Check:
    from b3.runtime.bot import _resolve_timezone

    name = config.bot.time_zone
    if _resolve_timezone(name) is None and name:
        return Check(
            "time zone",
            Status.WARN,
            f"{name!r} is unknown; schedules will use local time",
            "install the 'tzdata' package, or use an IANA name like Europe/Chisinau",
        )
    return Check("time zone", Status.OK, name or "local time")


def _check_database(config: Config) -> Check:
    """Connect, create the schema if needed, and prove we can actually write."""
    from b3.domain.client import Client
    from b3.storage.store import SqlAlchemyStorage

    url = config.bot.database
    storage = None
    try:
        storage = SqlAlchemyStorage(url)
        storage.connect()
        count = storage.count_clients()
        # A read can succeed on a database the bot may not write to (permissions, read-only mount).
        probe = storage.save_client(Client(guid="__b3_doctor__", name="doctor"))
        if probe.id is not None:
            storage.disable_penalties(probe.id)  # harmless no-op; proves a write round trip
        return Check("database", Status.OK, f"{_safe_url(url)}: {count} client(s), writable")
    except Exception as exc:  # noqa: BLE001 - reporting the failure is the whole job
        return Check(
            "database",
            Status.FAIL,
            f"{_safe_url(url)}: {exc}",
            "check the path is writable by the bot's user, or the MySQL/Postgres credentials",
        )
    finally:
        if storage is not None:
            try:
                storage.close()
            except Exception:  # pragma: no cover - nothing useful to do
                pass


def _check_game_log(config: Config) -> Check:
    """The log is the bot's only source of events: if this is wrong, nothing else matters.

    Unless the game has no log. A BattlEye server pushes its events down the RCON socket, so there is
    nothing to point `server.game_log` at and the rcon check below covers the whole input path.
    """
    from b3.net.logsource import URL_SCHEMES, create_log_source
    from b3.parsers.games import PROFILES, PUSH_FAMILIES

    profile = PROFILES.get(config.server.game)
    if profile is not None and profile.family in PUSH_FAMILIES:
        return Check(
            "game log",
            Status.OK,
            f"not used: a {config.server.game} server sends its events over rcon",
        )

    spec = config.server.game_log
    scheme = spec.split("://", 1)[0].lower() if "://" in spec else ""
    if scheme in URL_SCHEMES and scheme != "file":
        source = create_log_source(spec, timeout=config.server.log_timeout)
        try:
            source.open()
            source.close()
            return Check("game log", Status.OK, f"{getattr(source, 'safe_url', spec)} reachable")
        except Exception as exc:  # noqa: BLE001
            return Check(
                "game log",
                Status.FAIL,
                f"{getattr(source, 'safe_url', spec)}: {exc}",
                "check the host, credentials and path; percent-encode any @ or : in the password",
            )

    path = Path(spec)
    if not path.exists():
        return Check(
            "game log",
            Status.FAIL,
            f"{path} does not exist",
            "point server.game_log at the game's games_mp.log "
            "(check g_log / fs_game on the server)",
        )
    try:
        with path.open("rb") as fh:
            fh.read(1)
    except OSError as exc:
        return Check(
            "game log",
            Status.FAIL,
            f"{path} is not readable: {exc}",
            "the bot's user needs read access to the game log",
        )
    size = path.stat().st_size
    if size == 0:
        return Check(
            "game log",
            Status.WARN,
            f"{path} is empty",
            "normal on a fresh server; the bot sees nothing until players do something",
        )
    return Check("game log", Status.OK, f"{path} ({size} bytes)")


def _check_rcon(config: Config, rcon_factory: "Callable[[], RconClient] | None" = None) -> Check:
    """Ask the server for its status. Distinguishes 'not answering' from 'wrong password'."""
    from b3.net.rcon import Rcon, RconError, UdpRconTransport
    from b3.parsers.games import FILE_RCON_FAMILIES, PROFILES, PUSH_FAMILIES

    profile = PROFILES.get(config.server.game)
    if rcon_factory is None and profile is not None and profile.family in FILE_RCON_FAMILIES:
        # Checked before the password, because this family has no password: write access to the
        # command file *is* the authorisation, and warning about an empty rcon_password on a game
        # that has no rcon port would be advice to go and set a setting that does nothing.
        return _check_command_file(config)

    if not config.server.rcon_password:
        return Check(
            "rcon",
            Status.WARN,
            "no rcon_password set",
            "the bot can read the log but cannot kick, ban or reply in game",
        )

    if rcon_factory is None and profile is not None and profile.family in PUSH_FAMILIES:
        if profile.family == "frostbite":
            return _check_frostbite_rcon(config)
        if profile.family == "homefront":
            return _check_homefront_rcon(config)
        return _check_battleye_rcon(config)

    if rcon_factory is None:

        def rcon_factory() -> "RconClient":
            transport = UdpRconTransport(
                config.server.host, config.server.port, timeout=config.server.rcon_timeout
            )
            return Rcon(
                transport,
                password=config.server.rcon_password,
                encoding=config.server.encoding,
            )

    rcon = rcon_factory()
    try:
        reply = rcon.command("status")
    except RconError as exc:
        return Check(
            "rcon",
            Status.FAIL,
            f"no reply from {config.server.host}:{config.server.port} ({exc})",
            "check the port is the game's, that the server is running, "
            "and that a firewall is not dropping UDP",
        )
    except Exception as exc:  # noqa: BLE001
        return Check("rcon", Status.FAIL, f"{exc}")
    finally:
        try:
            rcon.close()
        except Exception:  # pragma: no cover
            pass

    lowered = reply.lower()
    if any(marker in lowered for marker in BAD_PASSWORD_MARKERS):
        return Check(
            "rcon",
            Status.FAIL,
            "the server rejected the password",
            "server.rcon_password must match rcon_password on the game server",
        )
    if "map:" not in lowered and "num score" not in lowered:
        return Check(
            "rcon",
            Status.WARN,
            f"unexpected reply to 'status': {reply.strip()[:60]!r}",
            "the server answered, but not with a status table — is this a CoD server?",
        )
    players = max(0, len([ln for ln in reply.splitlines() if ln.strip()]) - 3)
    return Check("rcon", Status.OK, f"answered; roughly {players} player(s) connected")


def _check_command_file(config: Config) -> Check:
    """Altitude: can we write to the file the game server reads its commands from?

    Reported under the name "rcon" because that is the job it does, and because an operator reading
    the report wants one row that answers "can the bot act on this server?".

    Writability is tested by actually appending and truncating, not by looking at permission bits: on
    Windows an ACL can refuse a write that `os.access` says is fine, and this check exists precisely
    so nobody discovers that from a kick that did nothing.
    """
    spec = config.server.command_file
    if not spec:
        return Check(
            "rcon",
            Status.FAIL,
            f"a {config.server.game} server is commanded through a file, and none is configured",
            "set server.command_file to the command.txt the game server reads "
            "(next to its log), or the bot can watch but never act",
        )
    path = Path(spec)
    if not path.parent.is_dir():
        return Check(
            "rcon",
            Status.FAIL,
            f"{path.parent} does not exist",
            "server.command_file must be inside the game server's own directory",
        )
    try:
        with path.open("a", encoding="utf-8"):
            pass
        existing = path.stat().st_size
    except OSError as exc:
        return Check(
            "rcon",
            Status.FAIL,
            f"{path} is not writable: {exc}",
            "the bot's user needs write access; this file is how it kicks, bans and talks",
        )
    if existing:
        # Not a failure: the bot empties it on startup precisely so these never run again. Saying so
        # is still worth a line, because it means the last run did not stop cleanly.
        return Check(
            "rcon",
            Status.WARN,
            f"{path} is writable, and holds {existing} byte(s) from a previous run",
            "the bot empties it on startup so the game server cannot replay old commands",
        )
    return Check("rcon", Status.OK, f"{path} is writable (this game has no rcon port)")


def _check_homefront_rcon(config: Config) -> Check:
    """Homefront: connect, log in, and say what came back.

    Worth its own check for the same reason BattlEye has one: the port is not the game's, and a wrong
    port looks exactly like a wrong password from the outside. This one can also tell an operator
    something no other family needs to know — the server hangs up after ten seconds of silence, so a
    connection that opens and then dies is a keepalive problem rather than a credentials one.
    """
    from b3.net.homefront import HomefrontAuthError, HomefrontClient, HomefrontError

    client = HomefrontClient(
        config.server.host,
        config.server.port,
        config.server.rcon_password,
        timeout=max(config.server.rcon_timeout, 2.0),
    )
    try:
        client.open()
    except HomefrontAuthError:
        return Check(
            "rcon",
            Status.FAIL,
            f"{config.server.host}:{config.server.port} rejected the password",
            "server.rcon_password must be the admin password from the server's own configuration",
        )
    except HomefrontError as exc:
        return Check(
            "rcon",
            Status.FAIL,
            f"cannot reach {config.server.host}:{config.server.port} ({exc})",
            "server.port is the *admin* port, not the game port -- and check a firewall is not "
            "blocking it",
        )
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - nothing useful to do
            pass

    version = client.server_version or "version not stated"
    return Check("rcon", Status.OK, f"logged in; server says {version}")


def _check_battleye_rcon(config: Config) -> Check:
    """The BattlEye version of the same check, and the one that matters most on that engine.

    Here rcon is not a side channel: it carries the events too, so a failure means the bot is deaf,
    not merely mute. BattlEye states outright whether a password was accepted, which is why this can
    tell "wrong password" from "nothing is listening" without guessing at reply text.
    """
    from b3.net.battleye import BattleyeAuthError, BattleyeClient, BattleyeError

    client = BattleyeClient(
        config.server.host,
        config.server.port,
        config.server.rcon_password,
        timeout=config.server.rcon_timeout,
    )
    try:
        client.open()
        reply = client.command("players")
    except BattleyeAuthError:
        return Check(
            "rcon",
            Status.FAIL,
            "the server rejected the password",
            "server.rcon_password must match BePath's RConPassword in beserver.cfg",
        )
    except BattleyeError as exc:
        return Check(
            "rcon",
            Status.FAIL,
            f"no reply from {config.server.host}:{config.server.port} ({exc})",
            "BattlEye listens on its own port from beserver.cfg (RConPort), which is usually "
            "*not* the game port — and check a firewall is not dropping UDP",
        )
    except Exception as exc:  # noqa: BLE001
        return Check("rcon", Status.FAIL, f"{exc}")
    finally:
        client.close()

    players = max(0, len([ln for ln in reply.splitlines() if ln.strip()]) - 3)
    return Check("rcon", Status.OK, f"logged in; roughly {players} player(s) connected")


def _check_frostbite_rcon(config: Config) -> Check:
    """The Frostbite version, where rcon carries the events too — so a failure means the bot is deaf.

    Logging in also asks the server to *send* events, which is the step an operator is most likely to
    have blocked: BF3's rcon port is separate from the game port and often unforwarded.
    """
    from b3.net.frostbite import FrostbiteAuthError, FrostbiteClient, FrostbiteError

    client = FrostbiteClient(
        config.server.host,
        config.server.port,
        config.server.rcon_password,
        timeout=config.server.rcon_timeout,
    )
    try:
        client.open()
        players = client.get_players()
    except FrostbiteAuthError:
        return Check(
            "rcon",
            Status.FAIL,
            "the server rejected the rcon password",
            "server.rcon_password must match admin.password in the server's startup arguments",
        )
    except FrostbiteError as exc:
        return Check(
            "rcon",
            Status.FAIL,
            f"no usable connection to {config.server.host}:{config.server.port} ({exc})",
            "Frostbite rcon listens on its own TCP port (the -remoteAdmin/admin.port setting), "
            "which is not the game port and is often not forwarded",
        )
    except Exception as exc:  # noqa: BLE001
        return Check("rcon", Status.FAIL, f"{exc}")
    finally:
        client.close()

    return Check("rcon", Status.OK, f"logged in; {len(players)} player(s) connected")


def _check_plugins(config: Config, conf_dir: Path | None) -> list[Check]:
    """Import every configured plugin and confirm its config file is there — without starting any."""
    from b3.core.pluginmgr import PluginLoadError, resolve_plugin_class
    from b3.core.plugininstall import installed_plugins_dir, register_installed_plugins

    for setting in (config.bot.shared_plugins_dir, config.bot.plugins_dir):
        if setting:
            try:
                register_installed_plugins(installed_plugins_dir(setting, conf_dir))
            except Exception as exc:  # noqa: BLE001
                log.debug("could not register %s: %s", setting, exc)

    if not config.plugins:
        return [Check("plugins", Status.WARN, "none configured", "at least `admin` is usual")]

    checks: list[Check] = []
    for entry in config.plugins:
        label = f"plugin {entry.name}"
        try:
            resolve_plugin_class(entry)
        except PluginLoadError as exc:
            checks.append(
                Check(
                    label,
                    Status.FAIL,
                    str(exc),
                    "run `b3 plugin list`; the plugin may not be installed for this server",
                )
            )
            continue
        if entry.config:
            from b3.config.loader import resolve_path_token

            path = Path(resolve_path_token(entry.config, conf_dir))
            if not path.is_file():
                checks.append(
                    Check(label, Status.FAIL, f"config file not found: {path}", "")
                )
                continue
        state = " (disabled)" if entry.disabled else ""
        checks.append(Check(label, Status.OK, f"imports cleanly{state}"))
    return checks


def _safe_url(url: str) -> str:
    """Hide the password in a database URL before printing it."""
    if "://" not in url or "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    credentials, _, host = rest.rpartition("@")
    if ":" in credentials:
        user, _, _password = credentials.partition(":")
        credentials = f"{user}:***"
    return f"{scheme}://{credentials}@{host}"
