"""Choosing a game: an id the bot does not know must be refused, never guessed.

The bug this file exists for: `PROFILES.get(game, DEFAULT)` meant `server.game: cod4X` — one
capital letter — silently ran the CoD4 parser. On a Battlefield or Arma server that is worse than
an error, because every log line simply fails to match and the symptom is a bot that reports
nothing at all. There was no warning anywhere.
"""

from __future__ import annotations

import pytest

from b3.cli import main
from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.parsers.games import PROFILES, UnknownGameError, by_family, profile_for, suggest
from b3.runtime.bot import Bot


def _config(tmp_path, game: str) -> Config:
    return Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game=game),
    )


# -- the resolver -------------------------------------------------------------------------------


def test_a_known_title_resolves_to_its_own_profile():
    assert profile_for("cod4").name == "cod4"
    assert profile_for("cod4x").name == "cod4x"  # and not to cod4, which is the near miss
    assert profile_for("bf3").family == "frostbite"


@pytest.mark.parametrize("game", ["cod4X", "bf3_typo", "arma_3", ""])
def test_an_unknown_title_raises_instead_of_falling_back(game):
    with pytest.raises(UnknownGameError):
        profile_for(game)


def test_the_error_names_the_near_miss_and_where_to_find_the_list():
    with pytest.raises(UnknownGameError) as exc:
        profile_for("cod4X")
    message = str(exc.value)
    assert "'cod4X'" in message  # what they wrote, verbatim
    assert "cod4x" in message  # what they almost certainly meant
    assert "b3 games" in message


def test_a_name_with_nothing_close_still_gets_a_usable_error():
    with pytest.raises(UnknownGameError) as exc:
        profile_for("zzzzzzzz")
    assert suggest("zzzzzzzz") == []
    assert "b3 games" in str(exc.value)


def test_suggestions_are_case_insensitive_because_that_is_the_common_typo():
    assert "cod4x" in suggest("COD4X")


def test_a_suffixed_name_is_matched_by_its_prefix_where_the_ratio_gives_up():
    """`bf3_typo` scores 0.55 against `bf3` — below difflib's cutoff, yet obviously meant it."""
    assert suggest("bf3_typo")[0] == "bf3"
    assert suggest("cod4-1")[0] == "cod4"


def test_a_single_character_suggests_nothing_because_it_would_match_half_the_list():
    assert suggest("c") == []


def test_every_family_is_accounted_for_and_nothing_is_listed_twice():
    grouped = by_family()
    listed = [title for titles in grouped.values() for title in titles]
    assert sorted(listed) == sorted(PROFILES)
    assert len(listed) == len(set(listed))


# -- the bot ------------------------------------------------------------------------------------


def test_the_bot_refuses_to_start_on_an_unknown_game(tmp_path):
    """The regression. Before the fix this built a bot running CodParser, quietly."""
    with pytest.raises(UnknownGameError):
        Bot(_config(tmp_path, "bf3_typo"), clock=FakeClock())


def test_the_bot_still_builds_for_every_title_it_claims_to_support(tmp_path):
    for game in PROFILES:
        bot = Bot(_config(tmp_path, game), clock=FakeClock())
        assert bot.profile.name == game
        bot.storage.close()


# -- the CLI ------------------------------------------------------------------------------------


def test_init_refuses_an_unknown_game_and_prints_the_valid_set(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["init", "srv", "--game", "cod4X"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "cod4x" in err and "bf3" in err  # the whole list, not just the near miss
    assert not (tmp_path / "srv").exists()  # and nothing was written


def test_init_accepts_a_title_that_is_not_the_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "srv", "--game", "bf3"]) == 0
    assert "game: bf3" in (tmp_path / "srv" / "b3.yaml").read_text(encoding="utf-8")


def test_a_typo_in_the_config_is_reported_rather_than_raised(tmp_path, monkeypatch, caplog):
    """It reaches the bot only by hand-editing the config, so that path needs the message too."""
    monkeypatch.chdir(tmp_path)
    main(["init", "srv"])
    config_path = tmp_path / "srv" / "b3.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("game: cod4", "game: cod4X"),
        encoding="utf-8",
    )
    log = tmp_path / "srv" / "games_mp.log"
    log.write_text("", encoding="utf-8")

    with caplog.at_level("ERROR"):
        assert main(["-c", str(config_path), "replay", str(log)]) == 1
    assert "unknown game 'cod4X'" in caplog.text
    assert "did you mean 'cod4x'" in caplog.text
    assert "Traceback" not in caplog.text


def test_b3_games_lists_every_title_and_needs_no_config(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no b3.yaml here at all

    assert main(["games"]) == 0

    out = capsys.readouterr().out
    printed = {word for line in out.splitlines() for word in line.split()}
    for game in PROFILES:
        assert game in printed, f"{game} missing from `b3 games`"
    assert f"{len(PROFILES)} titles" in out
    assert "events over rcon" in out  # a push family is marked as needing no game log
    assert "reads a game log" in out


def test_b3_plugins_distinguishes_where_a_plugin_comes_from(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "srv"])
    capsys.readouterr()  # discard init's output

    config = _config(tmp_path, "cod4")
    config.plugins = [
        PluginEntry(name="admin"),
        PluginEntry(name="censor", disabled=True),
    ]
    from b3.cli import _run_plugins

    assert _run_plugins(config, tmp_path) == 0
    out = capsys.readouterr().out
    assert "bundled" in out and "admin" in out and "spamcontrol" in out
    assert "installed, this server" in out
    assert "enabled in this config" in out
    assert "[disabled" in out  # and says which of them will not actually run
