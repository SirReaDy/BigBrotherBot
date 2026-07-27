"""`b3 doctor` — the pre-flight checks.

What matters here is that each failure is reported *distinctly*: "the server rejected the password"
and "nothing answered on that port" send an operator to completely different places, and the
classic bot showed both as the same silence.
"""

from __future__ import annotations

import pytest

from b3.cli import main
from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.doctor import Status, run_checks

STATUS_REPLY = """map: mp_crash
num score ping guid                             name            lastmsg address        qport rate
  0    12   47 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Admin                50 192.0.2.44:28960 12345 25000
"""


class FakeRcon:
    def __init__(self, reply="", error=None):  # noqa: ANN001
        self._reply = reply
        self._error = error

    def command(self, cmd: str) -> str:
        if self._error is not None:
            raise self._error
        return self._reply

    def close(self) -> None:
        pass


def _config(tmp_path, **server):  # noqa: ANN001, ANN202
    log = tmp_path / "games_mp.log"
    if not log.exists():
        log.write_text("  0:00 InitGame:\n", encoding="utf-8")
    settings = {"game": "cod4", "game_log": str(log), "rcon_password": "pw", **server}
    return Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(**settings),
        plugins=[PluginEntry(name="admin")],
    )


def _named(checks, name):  # noqa: ANN001, ANN202
    return next(c for c in checks if c.name == name)


def _rcon(reply="", error=None):  # noqa: ANN001, ANN202
    return lambda: FakeRcon(reply, error)


# -- the happy path ------------------------------------------------------------------------------


def test_a_healthy_install_passes_everything(tmp_path):
    checks = run_checks(_config(tmp_path), tmp_path, rcon_factory=_rcon(STATUS_REPLY))
    assert [c.name for c in checks if c.failed] == []
    assert _named(checks, "rcon").status is Status.OK
    assert _named(checks, "database").status is Status.OK
    assert _named(checks, "plugin admin").status is Status.OK


# -- rcon: the failure everybody hits --------------------------------------------------------


def test_a_rejected_password_is_named_as_such(tmp_path):
    checks = run_checks(_config(tmp_path), tmp_path, rcon_factory=_rcon("Invalid password.\n"))
    rcon = _named(checks, "rcon")
    assert rcon.status is Status.FAIL
    assert "rejected the password" in rcon.detail
    assert "must match" in rcon.hint


def test_no_reply_points_at_the_port_and_firewall_instead(tmp_path):
    from b3.net.rcon import RconError

    checks = run_checks(
        _config(tmp_path), tmp_path, rcon_factory=_rcon(error=RconError("timed out"))
    )
    rcon = _named(checks, "rcon")
    assert rcon.status is Status.FAIL
    assert "no reply" in rcon.detail
    assert "firewall" in rcon.hint


def test_an_answer_that_is_not_a_status_table_is_flagged(tmp_path):
    checks = run_checks(_config(tmp_path), tmp_path, rcon_factory=_rcon("unknown command\n"))
    assert _named(checks, "rcon").status is Status.WARN


def test_no_password_configured_is_a_warning_not_a_failure(tmp_path):
    """A bot with no RCON still reads the log — it just cannot act."""
    config = _config(tmp_path)
    config.server.rcon_password = ""
    checks = run_checks(config, tmp_path)
    rcon = _named(checks, "rcon")
    assert rcon.status is Status.WARN
    assert "cannot kick" in rcon.hint


# -- game log -------------------------------------------------------------------------------


def test_a_missing_game_log_fails_with_the_cvar_to_check(tmp_path):
    config = _config(tmp_path, game_log=str(tmp_path / "nope.log"))
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "game log")
    assert check.status is Status.FAIL
    assert "g_log" in check.hint


def test_an_empty_game_log_is_only_a_warning(tmp_path):
    empty = tmp_path / "empty.log"
    empty.write_text("", encoding="utf-8")
    config = _config(tmp_path, game_log=str(empty))
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "game log")
    assert check.status is Status.WARN


def test_an_unreachable_remote_log_is_reported_without_the_password(tmp_path):
    config = _config(tmp_path, game_log="ftp://bob:hunter2@127.0.0.1:1/games_mp.log")
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "game log")
    assert check.status is Status.FAIL
    assert "hunter2" not in check.detail  # a diagnostic must not leak the password


# -- database, plugins, config ------------------------------------------------------------------


def test_an_unwritable_database_fails(tmp_path):
    config = _config(tmp_path)
    config.bot.database = "sqlite:////nonexistent-directory/b3.sqlite"
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "database")
    assert check.status is Status.FAIL
    assert "writable" in check.hint


def test_database_passwords_are_not_printed(tmp_path):
    config = _config(tmp_path)
    config.bot.database = "mysql+pymysql://b3user:hunter2@localhost/b3"
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "database")
    assert "hunter2" not in check.detail
    assert "b3user:***" in check.detail


def test_an_unknown_game_is_caught_before_anything_connects(tmp_path):
    config = _config(tmp_path)
    config.server.game = "quake9"
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "game")
    assert check.status is Status.FAIL
    assert "cod4" in check.hint


def test_a_plugin_that_will_not_import_is_reported(tmp_path):
    config = _config(tmp_path)
    config.plugins = [PluginEntry(name="ghost", module="nosuchmodule:Ghost")]
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "plugin ghost")
    assert check.status is Status.FAIL


def test_a_missing_plugin_config_file_is_reported(tmp_path):
    config = _config(tmp_path)
    config.plugins = [PluginEntry(name="admin", config=str(tmp_path / "missing.yaml"))]
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "plugin admin")
    assert check.status is Status.FAIL
    assert "not found" in check.detail


def test_no_plugins_configured_is_a_warning(tmp_path):
    config = _config(tmp_path)
    config.plugins = []
    assert _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "plugins").status \
        is Status.WARN


def test_an_unknown_time_zone_warns_rather_than_stopping(tmp_path):
    config = _config(tmp_path)
    config.bot.time_zone = "Mars/Olympus_Mons"
    check = _named(run_checks(config, tmp_path, rcon_factory=_rcon(STATUS_REPLY)), "time zone")
    assert check.status is Status.WARN
    assert "tzdata" in check.hint


def test_one_failure_never_hides_the_next(tmp_path):
    """Every check runs independently — an operator should see the whole list, once."""
    config = _config(tmp_path, game_log=str(tmp_path / "nope.log"))
    config.server.game = "quake9"
    config.bot.database = "sqlite:////nonexistent-directory/b3.sqlite"

    from b3.net.rcon import RconError

    failed = {
        c.name for c in run_checks(config, tmp_path, rcon_factory=_rcon(error=RconError("x")))
        if c.failed
    }
    assert {"game", "database", "game log", "rcon"} <= failed


# -- through the CLI ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_cli_exits_nonzero_when_something_is_broken(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])  # game log will not exist yet

    exit_code = main(["-c", str(tmp_path / "srv" / "b3.yaml"), "doctor"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "FAIL" in out
    assert "problem(s) to fix" in out
