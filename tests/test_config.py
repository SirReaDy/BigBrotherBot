"""Config loading + path-token resolution."""

from __future__ import annotations

import os

import pytest

from b3.config.loader import (
    load_config,
    load_config_from_string,
    resolve_path_token,
)
from b3.config.schema import Config


def test_defaults():
    cfg = Config()
    assert cfg.bot.name == "b3"
    assert cfg.server.game == "cod4"
    assert cfg.server.encoding == "latin-1"


def test_load_from_yaml_string():
    cfg = load_config_from_string(
        """
        bot:
          name: MyBot
          database: sqlite:///data/b3.sqlite
        server:
          game: cod4
          rcon_password: secret
          host: 10.0.0.5
          port: 28960
          game_log: games_mp.log
        plugins:
          - name: admin
            config: "@conf/plugin_admin.yaml"
          - name: welcome
            disabled: true
        """
    )
    assert cfg.bot.name == "MyBot"
    assert cfg.server.rcon_password == "secret"
    assert cfg.server.host == "10.0.0.5"
    assert len(cfg.plugins) == 2
    assert cfg.plugins[0].name == "admin"
    assert cfg.plugins[1].disabled is True


def test_resolve_at_b3_token():
    resolved = resolve_path_token("@b3/sql/b3.sql")
    assert resolved.endswith(os.path.normpath("b3/sql/b3.sql"))
    assert "@b3" not in resolved


def test_resolve_at_conf_token(tmp_path):
    resolved = resolve_path_token("@conf/plugin_admin.yaml", conf_dir=tmp_path)
    assert resolved == os.path.normpath(str(tmp_path / "plugin_admin.yaml"))


def test_resolve_plain_path():
    resolved = resolve_path_token("games_mp.log")
    assert resolved == os.path.normpath("games_mp.log")


# -- a relative sqlite path belongs to the config, not to the shell -----------------------------


def test_a_relative_sqlite_path_is_anchored_to_the_config(tmp_path, monkeypatch):
    """Starting the bot from another directory must not silently create a second, empty database."""
    instance = tmp_path / "cod4_1" / "b3"
    instance.mkdir(parents=True)
    (instance / "b3.yaml").write_text(
        'bot:\n  database: "sqlite:///b3.sqlite"\nserver:\n  game: cod4\n', encoding="utf-8"
    )
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    config = load_config(str(instance / "b3.yaml"))

    assert config.bot.database.endswith("cod4_1/b3/b3.sqlite")


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///:memory:",
        "mysql+pymysql://user:pass@dbhost/b3",
        "postgresql+psycopg://user:pass@dbhost/b3",
    ],
)
def test_other_urls_are_left_alone(tmp_path, url):
    (tmp_path / "b3.yaml").write_text(
        f'bot:\n  database: "{url}"\nserver:\n  game: cod4\n', encoding="utf-8"
    )
    assert load_config(str(tmp_path / "b3.yaml")).bot.database == url


def test_an_absolute_sqlite_path_is_left_alone(tmp_path):
    absolute = "sqlite:////var/lib/b3/b3.sqlite"
    (tmp_path / "b3.yaml").write_text(
        f'bot:\n  database: "{absolute}"\nserver:\n  game: cod4\n', encoding="utf-8"
    )
    assert load_config(str(tmp_path / "b3.yaml")).bot.database == absolute


def test_a_conf_token_in_the_database_url_resolves(tmp_path):
    (tmp_path / "b3.yaml").write_text(
        'bot:\n  database: "sqlite:///@conf/data/b3.sqlite"\nserver:\n  game: cod4\n',
        encoding="utf-8",
    )
    resolved = load_config(str(tmp_path / "b3.yaml")).bot.database
    assert resolved.endswith("data/b3.sqlite")
    assert "@conf" not in resolved
