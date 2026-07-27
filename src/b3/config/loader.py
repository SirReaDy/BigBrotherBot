"""YAML config loading and path-token resolution.

Keeps the one genuinely useful bit of the legacy config UX — the ``@b3`` / ``@conf`` / ``@home``
path tokens — as an explicit resolver, and drops everything else in favour of a single typed
YAML file validated by :mod:`b3.config.schema`.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from b3.config.schema import Config

# Package install dir (…/src/b3) and the user home data dir (~/.b3).
_B3_DIR = Path(__file__).resolve().parent.parent
_HOME_DIR = Path(os.path.expanduser("~/.b3"))


def resolve_path_token(value: str, conf_dir: Path | None = None) -> str:
    """Expand ``@b3`` / ``@conf`` / ``@home`` prefixes and ``~`` in a path string.

    * ``@b3/...``   -> the installed b3 package directory
    * ``@conf/...`` -> the directory containing the loaded main config file
    * ``@home/...`` -> the ~/.b3 user data directory
    """
    if value.startswith("@b3"):
        return os.path.normpath(str(_B3_DIR) + value[len("@b3"):])
    if value.startswith("@conf"):
        base = conf_dir if conf_dir is not None else Path.cwd()
        return os.path.normpath(str(base) + value[len("@conf"):])
    if value.startswith("@home"):
        return os.path.normpath(str(_HOME_DIR) + value[len("@home"):])
    return os.path.normpath(os.path.expanduser(value))


def resolve_sqlite_url(url: str, conf_dir: Path) -> str:
    """Anchor a *relative* sqlite path to the config's directory rather than the working directory.

    B3 runs one instance per game server, and `sqlite:///b3.sqlite` in that instance's config means
    "this server's database", not "a database wherever the operator happened to `cd` first".
    Without this, starting the same bot from a different directory silently creates a second, empty
    database — no error, no players, no bans. Absolute URLs and other backends are left alone.
    """
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return url
    rest = url[len(prefix):]
    if not rest or rest.startswith(":"):  # :memory:
        return url
    if rest.startswith("@"):  # explicit @conf/@home/@b3 token
        return prefix + resolve_path_token(rest, conf_dir).replace("\\", "/")
    if Path(rest).is_absolute() or rest.startswith("/"):
        return url
    return prefix + str((conf_dir / rest).resolve()).replace("\\", "/")


def load_config(path: str | os.PathLike[str]) -> Config:
    """Load, validate and return the main config from a YAML file."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    config = Config.model_validate(raw)
    config.bot.database = resolve_sqlite_url(config.bot.database, p.resolve().parent)
    return config


def load_config_from_string(text: str) -> Config:
    """Load config from a YAML string (used by tests)."""
    return Config.model_validate(yaml.safe_load(text) or {})
