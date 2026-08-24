"""Telling an operator a new version exists, without ever being the thing that waits on the network.

`b3 update` could already answer the question; what it could not do was *raise* it. Somebody who
never types `b3 update` never hears that there is one, and telling them is the whole point of having
a check at all.

Two rules make that affordable, and nearly every test here is about one of them:

* **The line is read, not asked.** A file holds the last answer — written by the running bot's own
  scheduled check, by `b3 doctor`, by `b3 version` — and only when it is a week old does a command
  pay for a `git ls-remote`, with a few seconds' timeout, *after* it has done its work.
* **It is said only when it is news.** An update, once, on stderr, to a terminal. Being current is
  not news; a check that failed is not the business of whoever ran `b3 plugins`.

`b3 version` is the same answer asked for on purpose: this version, and the newest published one.
"""

from __future__ import annotations

import json
import time

import pytest

from b3.core.selfupdate import (
    CACHE_ENV,
    NOTICE_INTERVAL,
    NOTICE_TIMEOUT,
    QUIET_ENV,
    UpdateInfo,
    cached_check,
    notice,
    read_cache,
    write_cache,
)

REMOTE = "https://example.invalid/b3.git"


class FakeGit:
    """The one git call this makes, and a record of how often — and how patiently — it was made."""

    def __init__(self, tags: list[str] | None = None, error: Exception | None = None) -> None:
        self.tags = tags or []
        self.error = error
        self.urls: list[str] = []
        self.timeouts: list[int | None] = []

    def remote_tags(self, url: str, timeout: int | None = None) -> list[str]:
        self.urls.append(url)
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return list(self.tags)


@pytest.fixture
def cache(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    """A cache file of this test's own, and updates unmuted."""
    path = tmp_path / "update.json"
    monkeypatch.setenv(CACHE_ENV, str(path))
    monkeypatch.delenv(QUIET_ENV, raising=False)
    return path


def _answers(monkeypatch, latest: str = "9.9.9", error: str = "") -> None:
    """Make the remote say something, without a remote."""
    from b3.core import selfupdate

    monkeypatch.setattr(
        selfupdate,
        "check",
        lambda remote, current="2.0.0", **kw: UpdateInfo(  # noqa: ARG005
            current=current, latest=latest, error=error
        ),
    )


def _tty(monkeypatch, yes: bool = True) -> None:
    monkeypatch.setattr("sys.stderr.isatty", lambda: yes, raising=False)


# -- the remembered answer -----------------------------------------------------------------------


def test_an_answer_is_written_down_and_read_back(cache):
    git = FakeGit(["v2.1.0"])

    first = cached_check(REMOTE, current="2.0.0", now=1_000.0, git=git)
    second = cached_check(REMOTE, current="2.0.0", now=1_100.0, git=git)

    assert (first.latest, second.latest) == ("2.1.0", "2.1.0")
    assert len(git.urls) == 1  # the second command asked nobody


def test_the_answer_goes_stale_after_a_week(cache):
    """A week, not the bot's day: this is the path for a machine where no bot is running just now —
    somebody at a terminal, who does not need to pay for a round trip more often than that."""
    git = FakeGit(["v2.1.0"])

    cached_check(REMOTE, current="2.0.0", now=1_000.0, git=git)
    cached_check(REMOTE, current="2.0.0", now=1_000.0 + NOTICE_INTERVAL + 1, git=git)

    assert len(git.urls) == 2
    assert NOTICE_INTERVAL == 7 * 24 * 60 * 60


def test_a_failed_check_is_remembered_too(cache):
    """Otherwise an offline machine pays the timeout on every single command it runs."""
    from b3.core.plugininstall import PluginInstallError

    git = FakeGit(error=PluginInstallError("host unreachable"))

    first = cached_check(REMOTE, current="2.0.0", now=1_000.0, git=git)
    cached_check(REMOTE, current="2.0.0", now=1_100.0, git=git)

    assert "unreachable" in first.error
    assert len(git.urls) == 1


def test_it_is_asked_with_a_short_timeout(cache):
    """A command somebody is waiting on cannot have the three minutes a clone is allowed."""
    git = FakeGit(["v2.1.0"])

    cached_check(REMOTE, current="2.0.0", now=1_000.0, git=git)

    assert git.timeouts == [NOTICE_TIMEOUT]


def test_an_answer_about_another_remote_is_not_used(cache):
    """An operator who points `bot.update_remote` at their own fork must not be told about tags on
    the one they left."""
    git = FakeGit(["v2.1.0"])

    cached_check(REMOTE, current="2.0.0", now=1_000.0, git=git)
    cached_check("https://example.invalid/fork.git", current="2.0.0", now=1_050.0, git=git)

    assert len(git.urls) == 2


def test_the_version_running_now_decides_and_not_the_one_that_was(cache):
    """Somebody who has just upgraded stops being told to upgrade, without waiting a week for the
    remembered answer to expire."""
    git = FakeGit(["v2.1.0"])

    before = cached_check(REMOTE, current="2.0.0", now=1_000.0, git=git)
    after = cached_check(REMOTE, current="2.1.0", now=1_100.0, git=git)

    assert notice(before) and not notice(after)


def test_a_cache_that_cannot_be_read_is_not_an_error(cache):
    """Truncated by a full disk, or written by a version that stored different keys. Every way this
    goes wrong means the same thing to a caller: ask, or say nothing."""
    cache.write_text("{not json", encoding="utf-8")

    assert read_cache(REMOTE) is None


def test_a_cache_that_cannot_be_written_is_not_an_error(tmp_path, monkeypatch):
    """A read-only home is not a reason for the command somebody actually ran to fail."""
    blocked = tmp_path / "a-file"
    blocked.write_text("in the way", encoding="utf-8")
    monkeypatch.setenv(CACHE_ENV, str(blocked / "update.json"))

    write_cache(REMOTE, UpdateInfo(current="2.0.0", latest="2.1.0"))  # must not raise


def test_only_an_update_is_worth_a_line():
    """Being current is not news, and a failed check is not the business of somebody who ran
    `b3 plugins`. Either would train an operator to stop reading this line."""
    assert "2.1.0" in notice(UpdateInfo(current="2.0.0", latest="2.1.0"))
    assert notice(UpdateInfo(current="2.0.0", latest="2.0.0")) == ""
    assert notice(UpdateInfo(current="2.0.0", error="unreachable")) == ""
    assert notice(None) == ""


# -- `b3 version` --------------------------------------------------------------------------------


def test_version_prints_the_running_one_and_the_latest(cache, monkeypatch, capsys):
    from b3 import __version__
    from b3.cli import main

    _answers(monkeypatch)

    code = main(["version"])

    out = capsys.readouterr().out
    assert (
        code == 0
    )  # never non-zero: a script reading a version must not fail because of a release
    assert __version__ in out
    assert "9.9.9" in out
    assert "b3 update" in out  # and what to do about it


def test_version_needs_no_config_at_all(cache, monkeypatch, capsys, tmp_path):
    """ "Which version am I running" is asked *about* an install that is not working, which is the
    moment its config is least likely to load."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "b3.yaml").write_text("this: [is not a config\n", encoding="utf-8")
    _answers(monkeypatch, latest="")

    assert main(["version"]) == 0
    assert "none published yet" in capsys.readouterr().out


def test_version_says_so_when_checking_is_switched_off(cache, monkeypatch, capsys, tmp_path):
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])
    config = tmp_path / "srv" / "b3.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("update_check: true", "update_check: false"),
        encoding="utf-8",
    )
    capsys.readouterr()

    assert main(["-c", str(config), "version"]) == 0
    assert "not checked" in capsys.readouterr().out


def test_version_refresh_asks_now(cache, monkeypatch):
    """For the operator who has just published a release and wants to see it."""
    from b3.cli import main
    from b3.core import selfupdate

    asked: list[float] = []

    def record(remote, current="2.0.0", **kw):  # noqa: ANN001, ANN003, ANN202, ARG001
        asked.append(1.0)
        return UpdateInfo(current=current, latest="9.9.9")

    monkeypatch.setattr(selfupdate, "check", record)

    main(["version"])
    main(["version", "--refresh"])

    assert len(asked) == 2  # the second did not take the answer the first wrote


def test_the_version_flag_answers_without_a_subcommand(capsys):
    """A tool that answers `--version` with a usage error looks broken."""
    from b3 import __version__
    from b3.cli import main

    with pytest.raises(SystemExit) as exit_code:
        main(["--version"])

    assert exit_code.value.code == 0
    assert __version__ in capsys.readouterr().out


# -- the line after a command --------------------------------------------------------------------


def test_a_command_mentions_an_update_when_it_is_done(cache, monkeypatch, capsys, tmp_path):
    """On stderr, so `b3 games | grep cod` is unaffected by it."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    _answers(monkeypatch)
    _tty(monkeypatch)

    assert main(["games"]) == 0

    captured = capsys.readouterr()
    assert "9.9.9 is available" in captured.err
    assert "9.9.9" not in captured.out  # the command's own output is untouched


def test_the_line_is_read_from_the_cache_and_asks_nobody(cache, monkeypatch, capsys, tmp_path):
    """The whole point: a command that mentions a release is not a command that waits on git."""
    from b3.cli import main
    from b3.config.schema import BotConfig
    from b3.core import selfupdate

    monkeypatch.chdir(tmp_path)
    cache.write_text(
        json.dumps(
            {
                "remote": BotConfig().update_remote,
                "latest": "9.9.9",
                "error": "",
                "checked_at": time.time(),
            }
        ),
        encoding="utf-8",
    )

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("a command asked the network")

    monkeypatch.setattr(selfupdate, "check", refuse)
    _tty(monkeypatch)

    assert main(["games"]) == 0
    assert "9.9.9 is available" in capsys.readouterr().err


def test_a_pipe_hears_nothing_about_updates(cache, monkeypatch, capsys, tmp_path):
    """A cron job that wants this asks for it with `b3 update --check`, whose exit code is designed
    for exactly that, and does not want a surprise line in its mail."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    _answers(monkeypatch)
    _tty(monkeypatch, yes=False)

    main(["games"])

    assert "9.9.9" not in capsys.readouterr().err


def test_the_shell_can_switch_the_line_off(cache, monkeypatch, capsys, tmp_path):
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(QUIET_ENV, "1")
    _answers(monkeypatch)
    _tty(monkeypatch)

    main(["games"])

    assert "9.9.9" not in capsys.readouterr().err


def test_a_config_that_switched_checking_off_is_obeyed(cache, monkeypatch, capsys, tmp_path):
    """`bot.update_check: false` means this bot's operator has said no, and a line after every
    command is exactly what they said no to."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])
    config = tmp_path / "srv" / "b3.yaml"
    config.write_text(
        config.read_text(encoding="utf-8").replace("update_check: true", "update_check: false"),
        encoding="utf-8",
    )
    _answers(monkeypatch)
    _tty(monkeypatch)
    capsys.readouterr()

    main(["-c", str(config), "plugins"])

    assert "9.9.9" not in capsys.readouterr().err


def test_the_commands_that_report_it_themselves_do_not_say_it_twice(
    cache, monkeypatch, capsys, tmp_path
):
    """`b3 completion` above all: a stray line in the snippet it prints is a syntax error in
    somebody's shell rc."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    _answers(monkeypatch)
    _tty(monkeypatch)

    main(["version"])
    assert "is available" not in capsys.readouterr().err

    pytest.importorskip("argcomplete")
    main(["completion", "bash"])
    assert "is available" not in capsys.readouterr().err
