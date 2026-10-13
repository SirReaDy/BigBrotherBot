"""A double-clicked `b3.exe` - the first thing a Windows operator without Python sees the bot do.

Before this it printed its usage line into a console that closed before anybody could read it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from b3.core import doubleclick
from b3.core.wizard import Prompter


def scripted(answers: list[str]) -> tuple[Prompter, list[str]]:
    """A prompter that answers from a list and records what it was shown."""
    shown: list[str] = []
    remaining = list(answers)

    def read(question: str) -> str:
        shown.append(question)
        if not remaining:
            raise AssertionError(f"asked more than was scripted: {question!r}")
        return remaining.pop(0)

    return Prompter(reader=read, writer=shown.append), shown


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):  # noqa: ANN001, ANN202
    """`init` refreshes the geolocation database; a test has no business downloading it."""
    import b3.core.geoipdata

    monkeypatch.setattr(b3.core.geoipdata, "refresh_all", lambda **kw: [])  # noqa: ARG005


# -- telling a double-click from a command -------------------------------------------------


@pytest.fixture(autouse=True)
def _a_console(monkeypatch):  # noqa: ANN001, ANN202
    """A double-click hands the program a real console for input and output; pytest does not."""
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


def test_a_console_of_its_own_with_no_arguments_is_a_double_click(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert doubleclick.started_by_double_click([], console_processes=lambda: 1)


def test_started_from_a_shell_it_is_the_ordinary_command(monkeypatch):
    """cmd or PowerShell shares the console, so there is one process more than this program."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert not doubleclick.started_by_double_click([], console_processes=lambda: 2)


def test_any_argument_is_a_command_however_it_was_started():
    assert not doubleclick.started_by_double_click(["--version"], console_processes=lambda: 1)


def test_not_on_windows_there_is_no_double_click():
    """0 is what the console count reads as where there is no Windows console to ask."""
    assert not doubleclick.started_by_double_click([], console_processes=lambda: 0)


def test_git_bash_hands_it_a_hidden_console_but_no_terminal(monkeypatch):
    """mintty is not a Windows console: the program gets one of its own, with pipes for its I/O."""
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert not doubleclick.started_by_double_click([], console_processes=lambda: 1)


def test_the_one_file_build_counts_its_own_bootloader(monkeypatch, tmp_path):
    """One file is two processes - the bootloader waits while the Python it unpacked runs."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "unpacked" / "_MEI1234"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "download" / "b3.exe"))

    assert doubleclick.started_by_double_click([], console_processes=lambda: 2)
    assert not doubleclick.started_by_double_click([], console_processes=lambda: 3)


def test_the_folder_build_runs_in_place(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "b3" / "_internal"), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "b3" / "b3.exe"))

    assert doubleclick.started_by_double_click([], console_processes=lambda: 1)
    assert not doubleclick.started_by_double_click([], console_processes=lambda: 2)


def test_main_hands_a_double_click_to_the_session(monkeypatch):
    from b3 import cli

    monkeypatch.setattr(sys, "argv", ["b3.exe"])
    monkeypatch.setattr(doubleclick, "started_by_double_click", lambda argv: argv == [])
    monkeypatch.setattr(doubleclick, "run", lambda: 42)

    assert cli.main() == 42


def test_main_given_arguments_never_asks(monkeypatch):
    """`main(argv)` is how tests and embedders call the CLI; it must never turn interactive."""
    from b3 import cli

    monkeypatch.setattr(doubleclick, "started_by_double_click", lambda argv: True)  # noqa: ARG005
    monkeypatch.setattr(doubleclick, "run", lambda: pytest.fail("went interactive"))

    assert cli.main(["games"]) == 0


# -- the session ---------------------------------------------------------------------------


def test_the_bot_is_started_as_the_exe_in_a_frozen_build(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/b3/b3.exe")
    assert doubleclick.bot_command() == ["C:/b3/b3.exe"]


def test_the_bot_is_started_through_python_otherwise(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert doubleclick.bot_command() == [sys.executable, "-m", "b3.cli"]


def test_a_first_run_asks_the_init_questions_and_writes_the_server(tmp_path):
    """Nothing set up yet: the `b3 init` questions, then a server under `~/b3`, then an offer."""
    home = tmp_path / "b3"
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")
    ask, shown = scripted(
        [
            "cod4_1",  # name, which picks the folder
            "cod4x",  # game
            "",  # address: the default
            "",  # port: the default
            "secret",  # rcon password
            str(log),
            f"sqlite:///{(tmp_path / 'b3.sqlite').as_posix()}",
            "",  # plugins: the suggested ones
            "n",  # no doctor - there is no game server to ask
            "n",  # do not start it
            "",  # press Enter to close
        ]
    )

    assert doubleclick.run(ask, home=home) == 0

    config = home / "cod4_1" / "b3.yaml"
    assert config.is_file()
    text = config.read_text(encoding="utf-8")
    assert "cod4x" in text
    assert "secret" in text
    assert shown[-1] == "Press Enter to close this window: "
    # The wizard's own name question is skipped: the name was asked once, first.
    assert sum("name" in line.lower() for line in shown if line.endswith(": ")) == 1


def test_a_name_already_set_up_is_refused(tmp_path):
    home = tmp_path / "b3"
    (home / "cod4_1").mkdir(parents=True)
    (home / "cod4_1" / "b3.yaml").write_text("{}", encoding="utf-8")

    check = doubleclick._valid_name(home)
    assert check("cod4_1") is not None
    assert check("cod4_2") is None
    assert check("has space") is not None
    assert check("../escape") is not None


def test_the_default_name_is_the_first_one_free(tmp_path):
    (tmp_path / "server1").mkdir()
    assert doubleclick._free_name(tmp_path) == "server2"


def test_a_server_already_set_up_is_offered_and_started(tmp_path, monkeypatch):
    from b3 import cli

    home = tmp_path / "b3"
    (home / "cod4_1").mkdir(parents=True)
    config = home / "cod4_1" / "b3.yaml"
    config.write_text("{}", encoding="utf-8")
    started: list[list[str]] = []
    monkeypatch.setattr(cli, "_supervise", lambda child: started.append(child) or 0)
    ask, shown = scripted(["", ""])  # yes, start it; then Enter

    assert doubleclick.run(ask, home=home) == 0

    assert started == [[*doubleclick.bot_command(), "-c", str(config), "run"]]
    assert any("cod4_1" in line for line in shown)


def test_with_several_servers_one_is_picked_by_number(tmp_path):
    home = tmp_path / "b3"
    for name in ("cod4_1", "urt_1"):
        (home / name).mkdir(parents=True)
        (home / name / "b3.yaml").write_text("{}", encoding="utf-8")
    found = doubleclick.servers(home)

    ask, _shown = scripted(["5", "2"])  # out of range, then a good answer
    assert doubleclick.choose(ask, found) == home / "urt_1" / "b3.yaml"

    ask, _shown = scripted(["n"])
    assert doubleclick.choose(ask, found) is None


def test_an_error_is_shown_and_the_window_still_waits(tmp_path, monkeypatch, capsys):
    """A traceback in a window that closes at once is the original bug in a different shape."""

    def broken(home: Path | None = None) -> list[Path]:  # noqa: ARG001
        raise RuntimeError("the disk is on fire")

    monkeypatch.setattr(doubleclick, "servers", broken)
    ask, shown = scripted([""])

    assert doubleclick.run(ask, home=tmp_path) == 1
    assert "the disk is on fire" in capsys.readouterr().err
    assert shown[-1] == "Press Enter to close this window: "


def test_ctrl_c_part_way_through_still_waits(tmp_path):
    answers = iter([KeyboardInterrupt, ""])

    def read(question: str) -> str:  # noqa: ARG001
        answer = next(answers)
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answer

    shown: list[str] = []
    assert doubleclick.run(Prompter(reader=read, writer=shown.append), home=tmp_path) == 0
    assert "stopped" in shown


def test_supervise_runs_the_child_it_is_given(monkeypatch):
    from b3 import cli

    ran: list[list[str]] = []
    monkeypatch.setattr(cli.subprocess, "call", lambda child: ran.append(child) or 0)

    assert cli._supervise(["b3.exe", "-c", "b3.yaml", "run"]) == 0
    assert ran == [["b3.exe", "-c", "b3.yaml", "run"]]
