"""Command-line entrypoint: ``b3 run`` (live) and ``b3 replay`` (offline)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from typing import Protocol, runtime_checkable

from b3.config.loader import load_config
from b3.config.schema import Config
from b3.net.logsource import LogSource
from b3.runtime.bot import Bot, RconClient


@runtime_checkable
class PushClient(RconClient, LogSource, Protocol):
    """A client for an engine that *pushes* its events down the RCON connection.

    Both halves of the bot's input in one object, stated in the type system rather than left as a
    comment: BattlEye and Frostbite have no game log, so their RCON client is also their log source.
    """


def build_bot(
    config: Config, *, rcon: RconClient | None = None, conf_dir: Path | None = None
) -> Bot:
    """Build a bot with the plugins listed in the config, loaded in dependency order."""
    from b3.core.plugininstall import installed_plugins_dir, register_installed_plugins
    from b3.core.pluginmgr import load_plugins

    # Git-installed plugins live outside the package tree; make them importable before resolving.
    # This server's own directory is registered last so it lands first on sys.path: if the same
    # plugin exists in both, the copy this server installed for itself wins over the shared pool.
    if config.bot.shared_plugins_dir:
        register_installed_plugins(
            installed_plugins_dir(config.bot.shared_plugins_dir, conf_dir)
        )
    register_installed_plugins(installed_plugins_dir(config.bot.plugins_dir, conf_dir))

    bot = Bot(config, rcon=rcon)
    for loaded in load_plugins(bot, config, conf_dir=conf_dir):
        bot.add_plugin(loaded.plugin, loaded.name)
    bot.start()
    return bot


#: How often the live loop comes round: fast enough for a local tail and for scheduler ticks.
LIVE_TICK = 0.2


@dataclass(slots=True)
class Connection:
    """How the bot reaches one game server: a way to command it, and a way to hear from it.

    ``shared`` says the two are the same object, which is the case for an engine that pushes its
    events down the RCON socket (BattlEye). Stated as a flag rather than left to an identity check,
    because "is this rcon client also the log source?" is a fact about the *engine*, and reading it
    off `is` invites the reader to think it might be an accident.
    """

    rcon: RconClient
    source: LogSource
    description: str
    shared: bool = False


def _connect(config: Config) -> Connection:
    """Build this game's RCON client and event source.

    For most games these are two independent things: a UDP RCON socket, and a log file (local or
    remote) to tail. A BattlEye game has no log at all — its RCON socket *is* the event stream — so
    there the two are one object, and `server.game_log` is ignored. Deciding that here keeps
    `_run_live` below identical for both, which is the point: the loop should not know.
    """
    from b3.net.logsource import create_log_source
    from b3.net.rcon import Rcon, UdpRconTransport, dialect_for
    from b3.parsers.games import PUSH_FAMILIES, profile_for

    # Raises UnknownGameError on a typo, which main() reports; nothing has connected yet.
    profile = profile_for(config.server.game)

    if profile.family in PUSH_FAMILIES:
        # An engine that pushes its events: one object is both halves. Which one it is remains the
        # profile's business, not the loop's.
        pushers: dict[str, Callable[[Config], PushClient]] = {
            "battleye": _battleye_client,
            "frostbite": _frostbite_client,
        }
        client = pushers[profile.family](config)
        return Connection(
            rcon=client,
            source=client,
            description=(
                f"{config.server.host}:{config.server.port} ({profile.family} rcon, events pushed)"
            ),
            shared=True,
        )

    transport = UdpRconTransport(
        config.server.host, config.server.port, timeout=config.server.rcon_timeout
    )
    rcon = Rcon(
        transport,
        password=config.server.rcon_password,
        encoding=config.server.encoding,
        # Black Ops frames its packets differently; every other title speaks plain Quake3.
        dialect=dialect_for(profile.rcon_dialect),
    )
    source = create_log_source(
        config.server.game_log,
        encoding=config.server.encoding,
        poll_interval=config.server.log_poll_interval,
        timeout=config.server.log_timeout,
        max_gap=config.server.log_max_gap,
    )
    return Connection(rcon=rcon, source=source, description=config.server.game_log)


def _battleye_client(config: Config) -> PushClient:
    from b3.net.battleye import BattleyeClient

    return BattleyeClient(
        config.server.host,
        config.server.port,
        config.server.rcon_password,
        timeout=config.server.rcon_timeout,
    )


def _frostbite_client(config: Config) -> PushClient:
    from b3.net.frostbite import FrostbiteClient

    return FrostbiteClient(
        config.server.host,
        config.server.port,
        config.server.rcon_password,
        timeout=config.server.rcon_timeout,
    )


async def _run_live(config: Config, conf_dir: Path | None = None, config_path: str = "") -> int:
    connection = _connect(config)
    rcon, source = connection.rcon, connection.source
    bot = build_bot(config, rcon=rcon, conf_dir=conf_dir)
    bot.config_path = Path(config_path) if config_path else None  # so `!reconfig` can re-read it

    source.open()
    logging.info("b3 running; reading %s", connection.description)
    next_poll = 0.0
    try:
        while bot.exit_code is None:
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + source.poll_interval
                # Remote sources block on network I/O; keep them off the event loop thread.
                lines = (
                    await asyncio.to_thread(source.read_lines)
                    if source.blocking
                    else source.read_lines()
                )
                for line in lines:
                    await bot.feed_line(line)
            # Timed plugin work (b3.core.scheduler); ticks are deduped to once per second.
            await bot.scheduler.tick()
            await asyncio.sleep(LIVE_TICK)
        # `!die` (0) or `!restart` (221 — the classic code a supervisor script watches for).
        logging.info("stopping with exit code %d", bot.exit_code)
        return bot.exit_code
    finally:
        source.close()
        if not connection.shared:  # on a BattlEye game those were the same socket
            rcon.close()


async def _run_replay(
    config: Config, logfile: str, conf_dir: Path | None = None, config_path: str = ""
) -> None:
    from b3.net.logsource import FileLogSource

    bot = build_bot(config, conf_dir=conf_dir)  # no rcon in replay mode
    bot.config_path = Path(config_path) if config_path else None
    source = FileLogSource(logfile, encoding=config.server.encoding, from_start=True)
    source.open()
    lines = source.read_lines()
    source.close()
    await bot.replay(lines)
    logging.info("replayed %d lines from %s", len(lines), logfile)


def main(argv: list[str] | None = None) -> int:
    from b3.parsers.games import PROFILES, UnknownGameError

    parser = argparse.ArgumentParser(prog="b3", description="Big Brother Bot 2.0")
    parser.add_argument("-c", "--config", default="b3.yaml", help="path to the YAML config")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="connect to the server and run")

    init = sub.add_parser("init", help="create a bot instance directory for one game server")
    init.add_argument("directory", help="where the instance lives, e.g. /srv/cod4_1/b3")
    init.add_argument("--name", default="b3", help="instance name, used in logs and the unit file")
    # `choices` so a typo is refused here, with the valid set printed, instead of reaching a config
    # file. `metavar` keeps 29 ids out of the usage line; argparse still lists them on an error.
    init.add_argument(
        "--game",
        default="cod4",
        choices=sorted(PROFILES),
        metavar="GAME",
        help="parser id (default: cod4); see `b3 games`",
    )
    init.add_argument("--host", default="127.0.0.1", help="game server address")
    init.add_argument("--port", type=int, default=28960, help="game server RCON port")
    init.add_argument("--rcon-password", default="", help="RCON password")
    init.add_argument("--game-log", default="games_mp.log", help="path or ftp/sftp/http URL")
    init.add_argument("--database", default="sqlite:///b3.sqlite", help="SQLAlchemy URL")
    init.add_argument("--shared-plugins-dir", help='plugin pool shared with other instances')
    init.add_argument("--service", action="store_true", help="also write a systemd unit file")
    init.add_argument("--service-user", default="b3", help="user the systemd unit runs as")
    init.add_argument("--force", action="store_true", help="overwrite an existing config")
    sub.add_parser("doctor", help="check this install before starting it for the first time")
    sub.add_parser("games", help="list the game titles this bot can read")
    sub.add_parser("plugins", help="list every plugin available here, and which this server runs")
    replay = sub.add_parser("replay", help="replay a recorded log file offline")
    replay.add_argument("logfile", help="path to a game log to replay")

    db = sub.add_parser("db", help="database migrations")
    db.add_argument("action", choices=["upgrade", "current", "stamp", "head"])
    db.add_argument("--revision", default="head")

    imp = sub.add_parser("import-db", help="import a legacy B3 database into this one")
    imp.add_argument("source_url", help="SQLAlchemy URL of the legacy DB (e.g. sqlite:///old.db)")

    plug = sub.add_parser("plugin", help="install and manage plugins from git")
    plug_sub = plug.add_subparsers(dest="plugin_cmd", required=True)

    p_install = plug_sub.add_parser("install", help="install a plugin from a git repository")
    p_install.add_argument("spec", help="repo, optionally pinned: owner/repo@v1.2.0")
    p_install.add_argument("--ref", help="tag or branch to install (overrides any @ref in spec)")
    p_install.add_argument(
        "--disabled", action="store_true", help="add to the config, but do not run it"
    )
    p_install.add_argument(
        "--no-enable", action="store_true", help="install the files only; do not touch the config"
    )
    p_install.add_argument("--force", action="store_true", help="reinstall if already present")
    p_install.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    p_install.add_argument(
        "--shared",
        action="store_true",
        help="install into bot.shared_plugins_dir, the pool every bot on this machine can use",
    )

    plug_sub.add_parser("list", help="list installed plugins")

    p_enable = plug_sub.add_parser(
        "enable", help="run an already-installed plugin on this server (no download)"
    )
    p_enable.add_argument("name")
    p_enable.add_argument(
        "--disabled", action="store_true", help="add it to the config but leave it inert"
    )

    p_disable = plug_sub.add_parser(
        "disable", help="stop running a plugin here; its files stay for other servers"
    )
    p_disable.add_argument("name")

    p_update = plug_sub.add_parser("update", help="move an installed plugin to a newer ref")
    p_update.add_argument("name")
    p_update.add_argument("--ref", help="tag or branch (default: highest semver tag)")
    p_update.add_argument("--shared", action="store_true", help="act on the shared pool")

    p_remove = plug_sub.add_parser("remove", help="uninstall a plugin")
    p_remove.add_argument("name")
    p_remove.add_argument(
        "--keep-files", action="store_true", help="deactivate but leave the files on disk"
    )
    p_remove.add_argument("--shared", action="store_true", help="act on the shared pool")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # `init` is the command you run when there is no config yet, and `games` is pure reference —
    # neither must need a config to work.
    if args.cmd == "init":
        return _run_init(args)
    if args.cmd == "games":
        return _run_games()

    config = load_config(args.config)
    conf_dir = Path(args.config).resolve().parent  # base for @conf tokens in plugin config paths

    try:
        if args.cmd == "doctor":
            return _run_doctor(config, conf_dir)
        if args.cmd == "run":
            return asyncio.run(_run_live(config, conf_dir, args.config))
        elif args.cmd == "replay":
            asyncio.run(_run_replay(config, args.logfile, conf_dir, args.config))
        elif args.cmd == "plugins":
            return _run_plugins(config, conf_dir)
        elif args.cmd == "db":
            _run_db(config, args.action, args.revision)
        elif args.cmd == "import-db":
            _run_import(config, args.source_url)
        elif args.cmd == "plugin":
            return _run_plugin(config, args, Path(args.config), conf_dir)
    except UnknownGameError as exc:
        # A one-line refusal beats a traceback: this is a typo in their config, not a crash.
        logging.error("%s", exc)
        return 1
    return 0


def _run_init(args: argparse.Namespace) -> int:
    """`b3 init <dir>` — scaffold one game server's instance directory."""
    import sys

    from b3.core.instance import InstanceError, InstanceSpec, create_instance

    spec = InstanceSpec(
        directory=Path(args.directory).resolve(),
        name=args.name,
        game=args.game,
        host=args.host,
        port=args.port,
        rcon_password=args.rcon_password,
        game_log=args.game_log,
        database=args.database,
        shared_plugins_dir=args.shared_plugins_dir,
    )
    template = Path(__file__).resolve().parent.parent.parent / "examples" / "plugin_admin.yaml"
    try:
        written = create_instance(
            spec,
            admin_config_source=template if template.is_file() else None,
            service=args.service,
            python=sys.executable,
            user=args.service_user,
            force=args.force,
        )
    except InstanceError as exc:
        logging.error("%s", exc)
        return 1

    for path in written:
        print(f"wrote {path}")
    print(f"\nnext:\n  b3 -c {spec.directory / 'b3.yaml'} run")
    if not args.rcon_password:
        print("  (set server.rcon_password first — it is empty)")
    return 0


def _run_games() -> int:
    """`b3 games` — every valid `server.game`, grouped by the engine that reads it.

    Needs no config on purpose: it answers "what may I write in the config?", which is a question
    asked before there is one.
    """
    from b3.parsers.games import PROFILES, PUSH_FAMILIES, by_family

    grouped = by_family()
    width = max(len(family) for family in grouped)
    for family, titles in grouped.items():
        # Whether the operator has to point us at a game log is the one thing a family decides for
        # them, so it belongs in the listing rather than only in the README.
        source = "events over rcon" if family in PUSH_FAMILIES else "reads a game log"
        print(f"{family:<{width}}  {source:<16}  {'  '.join(titles)}")
    print(f"\n{len(PROFILES)} titles. Set one as `server.game` in the config, or `b3 init --game`.")
    return 0


def _run_plugins(config: Config, conf_dir: Path | None) -> int:
    """`b3 plugins` — the three places a plugin can come from, and what this server runs.

    `b3 plugin list` covers the installed pools only; this is the whole set, which is what an
    operator needs to know what a name in the config may refer to.
    """
    import pkgutil

    import b3.plugins

    from b3.core import plugininstall as pi
    from b3.core.pluginmgr import BUILTIN_PACKAGE

    bundled = sorted(m.name for m in pkgutil.iter_modules(b3.plugins.__path__))
    print(f"bundled ({BUILTIN_PACKAGE}, no install needed):")
    print(f"  {'  '.join(bundled) if bundled else '(none)'}")

    pools = [("this server", pi.installed_plugins_dir(config.bot.plugins_dir, conf_dir))]
    if config.bot.shared_plugins_dir:
        shared = pi.installed_plugins_dir(config.bot.shared_plugins_dir, conf_dir)
        if shared != pools[0][1]:
            pools.append(("shared pool", shared))
    for label, directory in pools:
        records = pi.read_lockfile(directory)
        print(f"\ninstalled, {label} ({directory}):")
        if not records:
            print("  (none)")
        for rec in sorted(records.values(), key=lambda r: r.name):
            print(f"  {rec.name:<20} {rec.version:<10} {rec.ref}")

    print("\nenabled in this config:")
    if not config.plugins:
        print("  (none)")
    for entry in config.plugins:
        note = "  [disabled: loaded but inert]" if entry.disabled else ""
        print(f"  {entry.name:<20} {entry.module or f'{BUILTIN_PACKAGE}.{entry.name}'}{note}")
    return 0


def _run_doctor(config: Config, conf_dir: Path | None) -> int:
    """`b3 doctor` — say what is wrong with this install, before it is anyone's evening."""
    from b3.core.doctor import Status, run_checks

    # The checks connect to things, which is chatty. The report is the output here, not the log.
    for noisy in ("alembic", "b3.core.plugininstall", "b3.net.logsource"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    marks = {Status.OK: "  ok  ", Status.WARN: " warn ", Status.FAIL: " FAIL "}
    checks = run_checks(config, conf_dir)
    width = max(len(c.name) for c in checks)
    for check in checks:
        print(f"[{marks[check.status]}] {check.name:<{width}}  {check.detail}")
        if check.hint and check.status is not Status.OK:
            print(f"{'':>{width + 11}}-> {check.hint}")

    failed = [c for c in checks if c.failed]
    warned = [c for c in checks if c.status is Status.WARN]
    print()
    if failed:
        print(f"{len(failed)} problem(s) to fix before this bot will work properly.")
        return 1
    if warned:
        print(f"ready to run, with {len(warned)} thing(s) worth a look.")
        return 0
    print("everything checks out. Start it with `b3 run`.")
    return 0


def _run_db(config: Config, action: str, revision: str) -> None:
    from b3.storage import migrate

    url = config.bot.database
    if action == "upgrade":
        migrate.upgrade(url, revision)
        logging.info("database upgraded to %s", migrate.current_revision(url))
    elif action == "stamp":
        migrate.stamp(url, revision)
        logging.info("database stamped at %s", migrate.current_revision(url))
    elif action == "current":
        logging.info("current revision: %s", migrate.current_revision(url))
    elif action == "head":
        logging.info("head revision: %s", migrate.head_revision(url))


def _run_plugin(
    config: Config, args: argparse.Namespace, config_path: Path, conf_dir: Path | None
) -> int:
    """`b3 plugin install|list|update|remove` — see b3.core.plugininstall for the policies."""
    from b3.core import plugininstall as pi

    plugins_dir = pi.installed_plugins_dir(config.bot.plugins_dir, conf_dir)
    shared_dir = (
        pi.installed_plugins_dir(config.bot.shared_plugins_dir, conf_dir)
        if config.bot.shared_plugins_dir
        else None
    )
    if getattr(args, "shared", False):
        if shared_dir is None:
            logging.error(
                "--shared needs bot.shared_plugins_dir set in %s (e.g. \"@home/plugins\")",
                config_path,
            )
            return 1
        plugins_dir = shared_dir
    try:
        if args.plugin_cmd == "install":
            plan = pi.plan_install(args.spec, ref=args.ref)
            print(f"install {plan.url} at {plan.ref} into {plugins_dir}")
            print(
                "note: a plugin runs in-process with full access to your database and server; "
                "only install repositories you trust."
            )
            if not args.yes and not _confirm("proceed?"):
                print("aborted")
                return 1
            record = pi.install(
                args.spec,
                plugins_dir=plugins_dir,
                config_path=config_path,
                conf_dir=conf_dir,
                ref=args.ref,
                enable=not args.no_enable,
                disabled=args.disabled,
                force=args.force,
            )
            print(f"installed {record.name} {record.version} ({record.ref} @ {record.commit[:8]})")
            if args.no_enable:
                print(f"not activated; add it yourself:\n  - name: {record.name}")
                print(f"    module: {record.entry_point}")
            else:
                print(f"activated in {config_path}; restart b3 to load it")
        elif args.plugin_cmd == "enable":
            search = [plugins_dir] if shared_dir is None else [plugins_dir, shared_dir]
            record = pi.enable(
                args.name,
                plugins_dirs=search,
                config_path=config_path,
                conf_dir=conf_dir,
                disabled=args.disabled,
            )
            print(f"enabled {record.name} {record.version} in {config_path}; restart b3 to load it")
        elif args.plugin_cmd == "disable":
            pi.disable(args.name, config_path=config_path)
            print(f"disabled {args.name} in {config_path}; its files were left in place")
        elif args.plugin_cmd == "list":
            pools = [("this server", plugins_dir)]
            if shared_dir is not None and shared_dir != plugins_dir:
                pools.insert(0, ("shared", shared_dir))
            for label, directory in pools:
                records = pi.read_lockfile(directory)
                print(f"[{label}] {directory}")
                if not records:
                    print("  (none)")
                for rec in sorted(records.values(), key=lambda r: r.name):
                    print(
                        f"  {rec.name:<20} {rec.version:<10} {rec.ref:<15} "
                        f"{rec.commit[:8]}  {rec.url}"
                    )
        elif args.plugin_cmd == "update":
            record = pi.update(
                args.name, plugins_dir=plugins_dir, ref=args.ref, conf_dir=conf_dir
            )
            print(f"updated {record.name} to {record.version} ({record.ref} @ {record.commit[:8]})")
        elif args.plugin_cmd == "remove":
            pi.remove(
                args.name,
                plugins_dir=plugins_dir,
                config_path=config_path,
                keep_files=args.keep_files,
            )
            print(f"removed {args.name}")
    except pi.PluginInstallError as exc:
        logging.error("%s", exc)
        return 1
    return 0


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:  # non-interactive shell
        return False


def _run_import(config: Config, source_url: str) -> None:
    from b3.legacy import import_legacy_database
    from b3.storage.store import SqlAlchemyStorage

    target = SqlAlchemyStorage(config.bot.database)
    report = import_legacy_database(source_url, target)
    target.close()
    logging.info("import complete: %s", report.summary())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
