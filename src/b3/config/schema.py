"""Typed configuration schema.

Replaces the legacy dual XML/INI parsers + the ``MainConfig`` proxy + the stringly-typed,
late ``analyze()`` validation. Config is a Pydantic model: parsed from YAML, validated up front,
so a bad value fails at load with a clear message instead of raising deep inside startup.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BotConfig(BaseModel):
    name: str = "b3"
    # Goes in front of everything the bot says, so its messages can be told apart from player chat
    # on a busy server — which is the entire reason the classic bot had it (`msgPrefix`). Game
    # colour codes allowed. Empty means the bot says exactly what it was given.
    #
    # It is part of the **line-wrapping budget**, not something added afterwards: see
    # `b3.core.messages.Messages.prefix`.
    prefix: str = "^2(b3)^7:"
    # IANA name (e.g. Europe/Chisinau). Schedules are evaluated in this zone.
    time_zone: str = "UTC"
    log_level: str = "INFO"
    # Game chat line limit; longer replies are word-wrapped across several lines.
    line_length: int = 90
    # Prepended to each continuation line of a wrapped message (engines reset colour per line).
    line_color_prefix: str = ""
    # Marks a message sent only to the players waiting to respawn (`Console.say_dead`). Without it a
    # dead player cannot tell a message aimed at them from one the whole server got, which is the
    # only reason to send it separately. The classic bot's `deadPrefix`, with its value.
    dead_prefix: str = "[DEAD]^7"
    # Marks a private reply, so a player can tell one meant for them from a broadcast that happens
    # to name them. The classic bot's `pmPrefix`. Stacks on top of `prefix`.
    pm_prefix: str = "^8[pm]^7"
    # SQLAlchemy URL. sqlite:///path, mysql+pymysql://..., postgresql+psycopg://...
    database: str = "sqlite:///b3.sqlite"
    # Where `b3 plugin install` puts git-installed plugins (@b3/@conf/@home tokens allowed).
    # Defaults *next to this config*, so plugins installed for this server stay this server's.
    plugins_dir: str = "@conf/plugins"
    # Optional pool shared by every bot on the machine — install a plugin once with
    # `b3 plugin install --shared`, then enable it in whichever servers' `plugins:` list want it.
    # The classic bot's `external_dir`, which defaulted to one shared `@b3/extplugins`.
    shared_plugins_dir: str | None = None
    # Which server this bot speaks for, recorded on every penalty it issues. Only meaningful when
    # several bots share one database: without it nothing in the table says *where* a ban came from,
    # so a multi-server dashboard cannot attribute one and an operator cannot tell two servers'
    # history apart. Empty means "not stated", which is what every pre-existing row reads as.
    # NOTE it is attribution, not scoping: a shared database still enforces every ban on every
    # server, which is the reason for sharing one in the first place.
    server_id: str = ""
    # -- knowing there is a newer version ----------------------------------------------------
    #
    # The classic bot polled a domain the project no longer controlled, on every startup, and could
    # rewrite the installation from what it found. This is deliberately narrower on all three counts:
    # a repository **you** name, at most once per `update_check_interval`, and a check never installs
    # anything — `b3 update` is a command somebody types. Nothing about this server is sent: it reads
    # public tags with `git ls-remote`.
    #
    # Empty `update_remote` switches the whole feature off, which is also the default for anyone who
    # has not asked for it.
    update_remote: str = "https://github.com/SirReaDy/BigBrotherBot.git"
    update_check: bool = True
    # How long an answer is kept before asking again. A day: releases are not hourly, and the answer
    # is what `!b3` and `b3 doctor` read, so nothing should ever wait on the network to print it.
    update_check_interval: str = "24h"
    # Minimum level to broadcast a reply with the @ / & prefixes (classic default: 9).
    loud_level: int = 9
    # Minimum level for the silent `/` prefix — the classic `hidecmd_level`.
    silent_level: int = 80


class ConfigError(Exception):
    """The config is valid YAML, and still unusable for the game it names.

    Pydantic catches what is wrong with a *value*; this is for what is wrong with a *combination* —
    an Altitude server with no `command_file`, for instance, where every field validates and the bot
    still could not kick anyone. Reported by the CLI as one line, like an unknown game, because it is
    the same kind of mistake: something for the operator to fix in the file, not a crash.
    """


class ServerConfig(BaseModel):
    game: str = "cod4"  # parser id
    rcon_password: str = ""
    host: str = "127.0.0.1"
    port: int = 28960
    # Local path, or a URL to tail a hosted server's log remotely:
    #   ftp://user:pass@host/games_mp.log   ftps://…   sftp://…   http(s)://…
    game_log: str = "games_mp.log"
    # Altitude only: the file the game server reads console commands out of. That engine has no
    # admin socket, so without this the bot can watch the game but not act on it — see
    # b3.net.altitude. Ignored by every other family.
    command_file: str = ""
    # Text encoding of the game log / RCON (CoD engines are typically latin-1; Altitude's JSON log
    # is utf-8).
    encoding: str = "latin-1"
    rcon_timeout: float = 0.8
    # The admin *account* name, for the two engines whose login has one: Frontline hashes
    # `RESPONSE <user> md5(challenge+password)` and cannot log in without it, and Ravaged's
    # `LOGIN=` step names it. Ignored by every other family, all of which authenticate with a
    # password alone.
    rcon_user: str = "admin"
    # Urban Terror 4.2 lets a client connect with a name longer than the 32 characters the userinfo
    # string holds, which overflows it. The name is always truncated to fit; this decides whether
    # the player is kicked as well. Ignored on engines that declare no limit
    # (GameProfile.name_max_length).
    allow_long_names: bool = False
    # PunkBuster: None asks the server (`PB_SV_Ver`), which is the right default because the answer
    # is reliable and the operator should not have to know. True insists — the bot says so loudly if
    # the server has no PunkBuster, which is what an operator who is relying on it for identity
    # wants to hear. False never asks. Ignored on the engines that cannot run it at all
    # (GameProfile.punkbuster).
    punkbuster: bool | None = None
    # -- remote game_log only (ignored for a local path) --
    # Seconds between polls. Each poll is a round trip, so don't set this too low.
    log_poll_interval: float = 2.0
    # Network timeout for a single FTP/SFTP/HTTP operation.
    log_timeout: float = 10.0
    # If the log grew by more than this since the last poll (first poll, or a rotation), skip
    # ahead to the last N bytes instead of replaying the gap. 0 reads every byte.
    log_max_gap: int = 20480


class PluginEntry(BaseModel):
    # Identity: what requires_plugins / load_after refer to. Resolved to b3.plugins.<name> unless
    # `module` says otherwise ("pkg.mod" or "pkg.mod:ClassName" — used by installed plugins).
    name: str = Field(min_length=1)
    module: str | None = None
    config: str | None = None  # path to the plugin's own config file (optional)
    # Loaded but left inert (no commands, no handlers); can be enabled at runtime.
    disabled: bool = False


class Config(BaseModel):
    bot: BotConfig = Field(default_factory=BotConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    plugins: list[PluginEntry] = Field(default_factory=list)
    # Overrides for any of b3.core.messages.DEFAULT_MESSAGES — the modern [messages] section.
    messages: dict[str, str] = Field(default_factory=dict)
