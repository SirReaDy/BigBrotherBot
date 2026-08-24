"""Knowing a newer version exists — and the four promises that make it not a reversal.

This project deleted the classic bot's update check and its self-updater, and says so in the README as
a selling point. Rebuilding a check is only defensible if the differences are real, so they are the
tests: it points at a repository the operator names, it asks at most once per interval, a *check*
never installs anything, and it sends nothing about this server.

The rest is the trap the classic's version comparison would have fallen into and this one must not:
`v2.0.10` is newer than `v2.0.9`, which a string sort gets backwards.
"""

from __future__ import annotations

import pytest

from b3.core.selfupdate import (
    TOKEN_ENV,
    UpdateChecker,
    UpdateInfo,
    check,
    install_command,
    release_tags,
)

REMOTE = "https://github.com/SirReaDy/BigBrotherBot.git"


class FakeGit:
    """The one git call this module makes, and a record of how often it was made."""

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


# -- reading the tags --------------------------------------------------------------------------


def test_ten_is_newer_than_nine():
    """The trap a string sort falls into, and the reason this reuses the plugin installer's key."""
    assert release_tags(["v2.0.9", "v2.0.10", "v2.0.2"])[-1] == "v2.0.10"


def test_only_release_tags_are_compared():
    """`version_key` falls back to reading numbers out of any string, so a branch tag would happily
    outrank a release. Anything that is not a version is ignored rather than ranked."""
    assert release_tags(["latest", "nightly-2026-08-24", "v2.1.0", "release-candidate"]) == [
        "v2.1.0"
    ]


def test_a_pre_release_is_never_offered_as_the_latest():
    """A candidate is a real tag somebody meant to push, and `--to` installs one on purpose. But it
    is not something to *offer*: the line printed after every command has to name the release, and
    read as numbers alone an rc outranks the release it was a candidate for — for ever."""
    assert release_tags(["v2.1.0-rc1", "v2.1.0", "v2.2.0b1", "v2.1.0+build3"]) == ["v2.1.0"]


def test_a_remote_with_only_candidates_has_published_no_release():
    info = check(REMOTE, "2.0.0", git=FakeGit(["v2.1.0-rc1", "v2.1.0-rc2"]))  # type: ignore[arg-type]

    assert info.latest == ""
    assert info.available is False
    assert "no releases published yet" in info.describe()


def test_running_a_pre_release_is_offered_the_release_it_precedes():
    """The half of this that would have hurt: **every install today runs `2.0.0a0`**. Read as numbers
    alone `2.0.0a0` and `2.0.0` compare equal, so the day `v2.0.0` was tagged every one of them would
    have been told it was already on the latest, and never offered the release."""
    info = check(REMOTE, "2.0.0a0", git=FakeGit(["v2.0.0"]))  # type: ignore[arg-type]

    assert info.available is True
    assert "2.0.0 is available (running 2.0.0a0)" in info.describe()


def test_a_candidate_can_still_be_installed_on_purpose(tmp_path, monkeypatch, capsys):
    """Refusing to *offer* one is not refusing to install one. `--to` takes any tag the remote has."""
    import subprocess

    from b3.cli import main
    from b3.core import selfupdate

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])
    # Nothing is on offer — the remote's only release is the one already running. `--to` is how an
    # operator says "install this anyway", and it is also how a rollback is done.
    monkeypatch.setattr(
        selfupdate,
        "check",
        lambda remote, current=None, **kw: UpdateInfo(current="2.0.0", latest="2.0.0"),  # noqa: ARG005
    )
    monkeypatch.setattr(selfupdate, "in_container", lambda: False)
    ran: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        ran.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    code = main(["-c", str(tmp_path / "srv" / "b3.yaml"), "update", "--to", "v2.1.0-rc1", "-y"])

    assert code == 0
    assert ran and ran[0][-1].endswith("@v2.1.0-rc1")


def test_an_update_is_reported_when_the_remote_is_ahead():
    git = FakeGit(["v2.0.0", "v2.0.10", "v2.1.0"])

    info = check(REMOTE, "2.0.0", git=git)  # type: ignore[arg-type]

    assert info.latest == "2.1.0"
    assert info.available is True
    assert "2.1.0 is available" in info.describe()


def test_the_highest_release_is_not_an_update_when_you_are_running_it():
    info = check(REMOTE, "2.1.0", git=FakeGit(["v2.0.0", "v2.1.0"]))  # type: ignore[arg-type]

    assert info.known is True
    assert info.available is False
    assert "is the latest release" in info.describe()


def test_a_version_newer_than_every_release_is_never_told_to_downgrade():
    """Somebody running from a git checkout is ahead of every release by definition."""
    info = check(REMOTE, "2.2.0", git=FakeGit(["v2.1.0"]))  # type: ignore[arg-type]

    assert info.available is False
    assert "newer than the latest release" in info.describe()


def test_a_repository_with_no_releases_says_so_rather_than_failing():
    info = check(REMOTE, "2.0.0", git=FakeGit([]))  # type: ignore[arg-type]

    assert info.available is False
    assert info.error == ""
    assert "no releases published yet" in info.describe()


def test_an_unreachable_remote_is_reported_and_never_raises():
    """An unreachable repository must not stop a bot from moderating a game server."""
    from b3.core.plugininstall import PluginInstallError

    info = check(REMOTE, "2.0.0", git=FakeGit(error=PluginInstallError("host unreachable")))  # type: ignore[arg-type]

    assert info.available is False
    assert "host unreachable" in info.describe()


def test_no_remote_configured_is_not_an_error_anybody_needs_to_see():
    info = check("", "2.0.0")

    assert info.available is False
    assert "no update remote" in info.describe()


# -- the four promises -------------------------------------------------------------------------


def test_nothing_about_this_server_is_sent():
    """`git ls-remote` reads public refs. The only thing that leaves the machine is the URL the
    operator configured — no version, no address, no server name."""
    git = FakeGit(["v2.1.0"])

    check(REMOTE, "2.0.0", git=git)  # type: ignore[arg-type]

    assert git.urls == [REMOTE]


def test_a_check_never_installs_anything(monkeypatch):
    """The classic's updater could rewrite the installation because a *check* found something."""
    import subprocess

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise AssertionError("a check ran a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)

    check(REMOTE, "2.0.0", git=FakeGit(["v2.1.0"]))  # type: ignore[arg-type]


def test_the_answer_is_cached_for_the_interval():
    """The classic asked on every startup. Two calls inside the interval ask git once."""
    git = FakeGit(["v2.1.0"])
    checker = UpdateChecker(REMOTE, interval=3600, git=git)  # type: ignore[arg-type]

    first = checker.check(now=1000.0, current="2.0.0")
    second = checker.check(now=1000.0 + 60, current="2.0.0")

    assert len(git.urls) == 1
    assert first.latest == second.latest == "2.1.0"

    checker.check(now=1000.0 + 3601, current="2.0.0")
    assert len(git.urls) == 2


def test_a_token_never_reaches_a_log_line(monkeypatch):
    """A private repository needs credentials, and they must not travel with a config file — so the
    token is read from the environment, and redacted from anything reported."""
    from b3.core.plugininstall import PluginInstallError

    monkeypatch.setenv(TOKEN_ENV, "s3cret-token")
    git = FakeGit(error=PluginInstallError("fatal: could not read https://s3cret-token@host/x.git"))

    info = check("https://host/x.git", "2.0.0", git=git)  # type: ignore[arg-type]

    assert "s3cret-token" not in info.describe()
    assert "***" in info.describe()
    # ...and it is put into the URL rather than into the config file.
    assert git.urls == ["https://s3cret-token@host/x.git"]


# -- installing --------------------------------------------------------------------------------


def test_the_install_uses_this_interpreters_pip():
    """`sys.executable -m pip`, never a bare `pip`: the difference between updating the bot and
    updating whatever else is first on PATH."""
    import sys

    command = install_command(REMOTE, "2.1.0")

    assert command[:4] == [sys.executable, "-m", "pip", "install"]
    assert command[-1] == f"git+{REMOTE}@v2.1.0"


def test_a_tag_already_carrying_its_v_is_not_given_another():
    assert install_command(REMOTE, "v2.1.0")[-1].endswith("@v2.1.0")


# -- through the CLI ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("latest", "expected"),
    [("2.1.0", 1), ("2.0.0", 0)],
)
def test_check_exits_one_when_an_update_exists(tmp_path, monkeypatch, capsys, latest, expected):  # noqa: ANN001
    """So a cron job can mail you: 1 means "there is an update", 0 means "you are current", and 2 is
    reserved for the check having failed — which a script must be able to tell apart."""
    from b3.cli import main
    from b3.core import selfupdate

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])
    monkeypatch.setattr(
        selfupdate,
        "check",
        lambda remote, current=None, **kw: UpdateInfo(current="2.0.0", latest=latest),  # noqa: ARG005
    )

    code = main(["-c", str(tmp_path / "srv" / "b3.yaml"), "update", "--check"])

    assert code == expected
    assert latest in capsys.readouterr().out


def test_check_exits_two_when_the_remote_cannot_be_read(tmp_path, monkeypatch, capsys):
    from b3.cli import main
    from b3.core import selfupdate

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])
    monkeypatch.setattr(
        selfupdate,
        "check",
        lambda remote, current=None, **kw: UpdateInfo(current="2.0.0", error="unreachable"),  # noqa: ARG005
    )

    code = main(["-c", str(tmp_path / "srv" / "b3.yaml"), "update", "--check"])

    assert code == 2
    assert "unreachable" in capsys.readouterr().out


def test_updating_inside_a_container_says_to_pull_an_image_instead(tmp_path, monkeypatch, capsys):
    """pip in the running environment is the wrong operation there: the image *is* the version."""
    from b3.cli import main
    from b3.core import selfupdate

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])
    monkeypatch.setattr(
        selfupdate,
        "check",
        lambda remote, current=None, **kw: UpdateInfo(current="2.0.0", latest="2.1.0"),  # noqa: ARG005
    )
    monkeypatch.setattr(selfupdate, "in_container", lambda: True)

    code = main(["-c", str(tmp_path / "srv" / "b3.yaml"), "update", "-y"])

    assert code == 2
    assert "pull a new image" in capsys.readouterr().out


def test_an_install_runs_pip_and_says_what_to_do_next(tmp_path, monkeypatch, capsys):
    """Never in place: files change, and the change takes effect on the next start — and a new
    version may add a migration, which the bot now refuses to start without."""
    import subprocess

    from b3.cli import main
    from b3.core import selfupdate

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "pw"])
    monkeypatch.setattr(
        selfupdate,
        "check",
        lambda remote, current=None, **kw: UpdateInfo(current="2.0.0", latest="2.1.0"),  # noqa: ARG005
    )
    monkeypatch.setattr(selfupdate, "in_container", lambda: False)
    ran: list[list[str]] = []

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN003, ANN202, ARG001
        ran.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    code = main(["-c", str(tmp_path / "srv" / "b3.yaml"), "update", "-y"])

    out = capsys.readouterr().out
    assert code == 0
    assert ran and ran[0][-1].endswith("@v2.1.0")
    assert "every bot instance" in out  # one code install serves them all
    assert "db upgrade" in out


def test_doctor_reports_an_update_as_a_warning_and_never_a_failure(tmp_path, monkeypatch):
    """Being a version behind is not a broken install, and reporting it as FAIL would teach an
    operator to ignore the red rows."""
    from b3.config.schema import BotConfig, Config, ServerConfig
    from b3.core import selfupdate
    from b3.core.doctor import Status, run_checks

    monkeypatch.setattr(
        selfupdate,
        "check",
        lambda remote, current=None, **kw: UpdateInfo(current="2.0.0", latest="2.1.0"),  # noqa: ARG005
    )
    log = tmp_path / "games_mp.log"
    log.write_text("  0:00 InitGame:\n", encoding="utf-8")
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4", game_log=str(log), rcon_password="pw"),
    )

    checks = run_checks(config, tmp_path, check_update=True)
    row = next(check for check in checks if check.name == "update")

    assert row.status is Status.WARN
    assert "b3 update" in row.hint


def test_doctor_does_not_reach_the_network_unless_asked(tmp_path, monkeypatch):
    """Every other check here talks to this deployment only. A test suite, an air-gapped install and
    anything embedding these checks should not have to know to switch one off."""
    from b3.config.schema import BotConfig, Config, ServerConfig
    from b3.core import selfupdate
    from b3.core.doctor import run_checks

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202, ARG001
        raise AssertionError("doctor checked for updates without being asked")

    monkeypatch.setattr(selfupdate, "check", explode)
    log = tmp_path / "games_mp.log"
    log.write_text("  0:00 InitGame:\n", encoding="utf-8")
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4", game_log=str(log), rcon_password="pw"),
    )

    checks = run_checks(config, tmp_path)

    assert not any(check.name == "update" for check in checks)


# -- packaging -----------------------------------------------------------------------------------


def test_the_package_version_and_the_module_version_agree():
    """`b3 update` compares the running `b3.__version__` against the highest tag on the remote, and
    the release workflow refuses a tag that disagrees with `pyproject.toml`. If these two can drift
    from each other, the bot either misses an update or offers one that installs the same code."""
    import pathlib
    import re
    import tomllib

    import b3

    root = pathlib.Path(__file__).resolve().parent.parent
    packaged = tomllib.loads(root.joinpath("pyproject.toml").read_text(encoding="utf-8"))
    assert packaged["project"]["version"] == b3.__version__

    # And the workflow reads the module version with a regex rather than by importing it, so the
    # assignment has to stay in a shape that regex can find.
    source = root.joinpath("src", "b3", "__init__.py").read_text(encoding="utf-8")
    found = re.search(r'__version__\s*=\s*"([^"]+)"', source)
    assert found is not None and found.group(1) == b3.__version__
