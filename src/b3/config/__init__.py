"""Typed configuration: Pydantic schema + YAML loader."""

from b3.config.loader import load_config, resolve_path_token
from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig

__all__ = [
    "BotConfig",
    "Config",
    "PluginEntry",
    "ServerConfig",
    "load_config",
    "resolve_path_token",
]
