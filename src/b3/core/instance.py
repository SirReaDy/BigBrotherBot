"""Creating a new bot instance — one directory per game server.

B3 is deployed as *one code install, many instances*: the package is installed once (a venv on the
box), and each game server gets its own directory holding a config, its plugin configs, its plugins
and its database. This is the classic layout — a config file per server, passed with ``-c`` — with
the loose ends tied down: the classic bot's own ``--setup`` wizard was abandoned and users were sent
to a website to hand-craft XML, which is why so many installs ran on copied-and-edited configs
nobody understood.

:func:`create_instance` writes that directory: a commented config with the operator's answers
already in it, the admin plugin's config, and (optionally) a systemd unit so the bot comes back
after a reboot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Exit code `!restart` uses. A supervisor should treat it as "start me again", not as a failure.
RESTART_EXIT_CODE = 221


class InstanceError(Exception):
    """The instance directory could not be created."""


@dataclass(frozen=True, slots=True)
class InstanceSpec:
    """What the operator told us about this game server."""

    directory: Path
    name: str = "b3"
    game: str = "cod4"
    host: str = "127.0.0.1"
    port: int = 28960
    rcon_password: str = ""
    game_log: str = "games_mp.log"
    database: str = "sqlite:///b3.sqlite"
    shared_plugins_dir: str | None = None


CONFIG_TEMPLATE = """\
# B3 configuration for {name}.
#
# One B3 install per game server: this file, the plugin configs beside it, the plugins/ directory
# and the database all belong to this server alone. Run it with:
#
#     b3 -c {config_path} run
#
bot:
  name: {name}
  prefix: "^2({name})^7:"
  time_zone: UTC
  log_level: INFO
  # Any SQLAlchemy URL. Point several servers at one MySQL/Postgres URL to share bans, admin
  # levels and player history between them; keep them separate to keep the servers independent.
  database: "{database}"
  # Where `b3 plugin install` puts plugins for THIS server.
  plugins_dir: "@conf/plugins"
{shared_line}
server:
  game: {game}
  rcon_password: "{rcon_password}"
  host: {host}
  port: {port}
  # A local path, or a URL if the bot does not run on the game server's own box:
  #   ftp://user:pass@host/games_mp.log   ftps://…   sftp://…   http(s)://…
  game_log: "{game_log}"
  encoding: latin-1

# Which plugins this server runs. `b3 plugin install` appends to this list for you.
plugins:
  - name: admin
    config: "@conf/plugin_admin.yaml"
"""

SERVICE_TEMPLATE = """\
# systemd unit for {name}. Install with:
#
#     sudo cp {service_name} /etc/systemd/system/
#     sudo systemctl enable --now {service_name}
#
[Unit]
Description=Big Brother Bot ({name})
After=network.target

[Service]
Type=simple
User={user}
WorkingDirectory={directory}
ExecStart={python} -m b3.cli -c {config_path} run
Restart=always
RestartSec=5
# `!restart` exits {restart_code}; treat that as "start me again", not as a crash.
RestartForceExitStatus={restart_code}
SuccessExitStatus={restart_code}

[Install]
WantedBy=multi-user.target
"""


def create_instance(
    spec: InstanceSpec,
    *,
    admin_config_source: Path | None = None,
    service: bool = False,
    python: str = "python3",
    user: str = "b3",
    force: bool = False,
) -> list[Path]:
    """Create a bot instance directory. Returns the files written.

    Refuses to touch an existing config unless ``force``: an instance directory holds a live
    database and a working configuration, and silently overwriting either is not a thing a setup
    command should ever do.
    """
    directory = spec.directory
    config_path = directory / "b3.yaml"
    if config_path.exists() and not force:
        raise InstanceError(f"{config_path} already exists (use --force to overwrite)")

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugins").mkdir(exist_ok=True)

    shared_line = (
        f'  shared_plugins_dir: "{spec.shared_plugins_dir}"\n' if spec.shared_plugins_dir else ""
    )
    config_path.write_text(
        CONFIG_TEMPLATE.format(
            name=spec.name,
            game=spec.game,
            host=spec.host,
            port=spec.port,
            rcon_password=spec.rcon_password,
            game_log=spec.game_log,
            database=spec.database,
            config_path=config_path,
            shared_line=shared_line,
        ),
        encoding="utf-8",
    )
    written = [config_path]

    admin_config = directory / "plugin_admin.yaml"
    if admin_config_source is not None and admin_config_source.is_file():
        admin_config.write_text(admin_config_source.read_text(encoding="utf-8"), encoding="utf-8")
        written.append(admin_config)

    if service:
        service_name = f"b3-{spec.name}.service"
        service_path = directory / service_name
        service_path.write_text(
            SERVICE_TEMPLATE.format(
                name=spec.name,
                directory=directory,
                config_path=config_path,
                python=python,
                user=user,
                service_name=service_name,
                restart_code=RESTART_EXIT_CODE,
            ),
            encoding="utf-8",
        )
        written.append(service_path)

    return written
