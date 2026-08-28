"""Config loading + path-token resolution."""

from __future__ import annotations

import logging
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


# -- levels written as group keywords -----------------------------------------------------------


def test_a_level_setting_accepts_the_group_keyword_fifteen_years_of_configs_use() -> None:
    """`mod_level: senioradmin` is what the classic bot's own shipped `.ini` files wrote.

    Read through `as_int` a word is not a number, so the setting fell back to its default: an
    operator writing `senioradmin` got 20 where they meant 80, with one line in a log to say so.
    `level_for` already knew how to read "a keyword or a number 0-100"; the plugins simply never
    reached it. This is the case that made a config converter dangerous — a value copied across
    verbatim looks right and grants the wrong level.
    """
    from b3.core.util import as_level

    assert as_level("senioradmin", 20) == 80
    assert as_level("mod", 100) == 20
    assert as_level("guest", 100) == 0


def test_a_level_setting_still_takes_a_number_written_either_way() -> None:
    from b3.core.util import as_level

    assert as_level(60, 20) == 60
    assert as_level("60", 20) == 60


def test_an_unreadable_level_falls_back_and_says_which_words_it_knows(caplog) -> None:  # noqa: ANN001
    """A typo must not take the bot down, and must not be silent either."""
    from b3.core.util import as_level

    with caplog.at_level(logging.WARNING):
        assert as_level("moderator", 40) == 40  # `mod` is the word; `moderator` is not

    assert "moderator" in caplog.text
    assert "senioradmin" in caplog.text  # the message lists what it would have accepted


def test_a_level_out_of_range_is_refused_rather_than_clamped() -> None:
    """101 is not "superadmin, roughly": it is a mistake, and a silent clamp hides it."""
    from b3.core.util import as_level

    assert as_level(101, 40) == 40
    assert as_level(-1, 40) == 40


def test_yes_is_not_a_level() -> None:
    """YAML reads a bare `yes` as True, and True is not level 1."""
    from b3.core.util import as_level

    assert as_level(True, 40) == 40
