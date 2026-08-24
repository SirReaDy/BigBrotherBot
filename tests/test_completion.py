"""What the shell offers when an operator presses tab.

Completion itself is a shell mechanism and not usefully unit-testable; the *completers* are, and that
is exactly why the logic lives in `b3.core.completion` and the shell layer is a shim. Given a prefix,
each returns the right candidates — and the values they return are read from the profile table and the
plugin package rather than restated, so a title added tomorrow completes without anybody remembering.

The last two tests go through argcomplete's own machinery, which is the only way to know the
completers are actually *reachable* from the parser: a `.completer` attribute attached to the wrong
object is silent, and would leave tab pressing do nothing with everything still passing.
"""

from __future__ import annotations

import os

import pytest

from b3.core import completion


# -- the values -----------------------------------------------------------------------------------


def test_every_game_the_bot_can_read_is_offered():
    """Read from `PROFILES`, so a title added tomorrow completes without anybody remembering."""
    from b3.parsers.games import PROFILES

    assert completion.games() == sorted(PROFILES)
    assert completion.games("bf") == ["bf3", "bf4", "bfbc2", "bfh"]
    assert completion.games("nothing-like-this") == []


def test_the_bundled_plugins_are_read_from_the_package():
    names = completion.bundled()

    assert "admin" in names and "jumper" in names
    assert len(names) >= 30
    assert completion.bundled("power") == [
        "poweradminbf3",
        "poweradminbfbc2",
        "poweradmincod7",
        "poweradminhf",
        "poweradminmoh",
        "poweradminurt",
    ]


def test_an_installed_plugin_is_offered_beside_the_bundled_ones(tmp_path, monkeypatch):
    """`b3 plugin enable` takes either, and an operator who installed a plugin for one server wants
    to see it when enabling it on another."""
    pool = tmp_path / "conf" / "plugins" / "b3_chatlogger"
    pool.mkdir(parents=True)
    (pool / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    names = completion.plugins("b3_")

    assert names == ["b3_chatlogger"]
    # ...and the bundled ones are still there, from the package rather than the directory.
    assert "admin" in completion.plugins()


def test_removal_offers_only_what_was_installed(tmp_path, monkeypatch):
    """`b3 plugin remove` works on the pool and refuses a name that was never installed, so offering
    a bundled one would be offering what the command exists to reject."""
    pool = tmp_path / "plugins" / "b3_chatlogger"
    pool.mkdir(parents=True)
    (pool / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    assert completion.installed() == ["b3_chatlogger"]
    assert "admin" not in completion.installed()


def test_a_pool_directory_that_is_not_there_is_not_an_error(tmp_path, monkeypatch):
    """A completer runs on every tab press, in whatever directory somebody happens to be in."""
    monkeypatch.chdir(tmp_path)

    assert "admin" in completion.plugins()


def test_config_completion_offers_yaml_files_and_directories(tmp_path):
    (tmp_path / "b3.yaml").write_text("", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    (tmp_path / "cod4_1").mkdir()
    (tmp_path / "__pycache__").mkdir()

    offered = completion.configs(str(tmp_path) + os.sep)

    names = {os.path.basename(path.rstrip(os.sep)) for path in offered}
    assert "b3.yaml" in names
    assert "cod4_1" in names  # a directory, so a second tab walks into it
    assert "notes.txt" not in names  # offering every file would bury the one being looked for
    assert "__pycache__" not in names
    assert any(path.endswith(os.sep) for path in offered)


def test_config_completion_filters_by_what_has_been_typed(tmp_path):
    (tmp_path / "b3.yaml").write_text("", encoding="utf-8")
    (tmp_path / "other.yaml").write_text("", encoding="utf-8")

    offered = completion.configs(str(tmp_path / "b3"))

    assert [os.path.basename(path) for path in offered] == ["b3.yaml"]


def test_config_completion_of_somewhere_that_does_not_exist_is_empty():
    assert completion.configs("/no/such/directory/at/all/x") == []


def test_migration_revisions_come_from_the_scripts_not_the_database():
    """Which is the point: this completes on an install whose database is unreachable, and that is
    exactly when somebody is typing a revision by hand."""
    offered = completion.revisions()

    assert "head" in offered and "base" in offered
    assert "0001" in offered
    assert completion.revisions("00") == ["0001", "0002", "0003"]


def test_a_completer_takes_argcompletes_keyword_arguments():
    """They are called with `action`, `parser` and `parsed_args`, which these ignore — so each one
    stays an ordinary function anything else can call."""
    assert completion.games("bf3", action=None, parser=None, parsed_args=None) == ["bf3"]


# -- `b3 completion` ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shell", "expected"),
    [
        ("bash", "complete"),
        ("zsh", "compdef"),
        ("fish", "complete"),
        ("powershell", "Register-ArgumentCompleter"),
    ],
)
def test_completion_prints_a_registration_for_each_shell(capsys, shell, expected):  # noqa: ANN001
    """PowerShell included, which this project's notes said Windows had no answer for — argcomplete
    3.x registers a native ArgumentCompleter for it."""
    pytest.importorskip("argcomplete")
    from b3.cli import main

    assert main(["completion", shell]) == 0

    out = capsys.readouterr().out
    assert expected in out
    assert "b3" in out


def test_completion_names_the_file_to_put_it_in(capsys):
    pytest.importorskip("argcomplete")
    from b3.cli import main

    main(["completion", "bash"])

    assert "~/.bashrc" in capsys.readouterr().out


def test_completion_guesses_the_shell_from_the_environment(capsys, monkeypatch):
    """And says which it guessed: an operator pasting a bash snippet into zsh would otherwise be
    left wondering why nothing completes."""
    pytest.importorskip("argcomplete")
    from b3.cli import main

    monkeypatch.setenv("SHELL", "/usr/bin/zsh")

    assert main(["completion"]) == 0
    assert "~/.zshrc" in capsys.readouterr().out


def test_a_windows_shell_named_with_exe_is_still_recognised(capsys, monkeypatch):
    """Git Bash reports `/bin/bash.exe`, and a guess that fails there fails on the platform least
    likely to have a shell rc set up already."""
    pytest.importorskip("argcomplete")
    from b3.cli import main

    monkeypatch.setenv("SHELL", "/bin/bash.exe")

    assert main(["completion"]) == 0
    assert "~/.bashrc" in capsys.readouterr().out


def test_powershell_is_the_guess_on_windows_with_no_shell_variable(monkeypatch):
    from b3.cli import _guess_shell

    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr("platform.system", lambda: "Windows")

    assert _guess_shell() == "powershell"


def test_completion_with_no_shell_and_nothing_to_guess_from_asks(capsys, monkeypatch):
    pytest.importorskip("argcomplete")
    from b3.cli import main

    monkeypatch.setenv("SHELL", "")
    monkeypatch.setattr("platform.system", lambda: "Linux")

    assert main(["completion"]) == 1
    assert "name a shell" in capsys.readouterr().out


def test_a_shell_nobody_supports_is_refused_by_argparse(capsys):
    from b3.cli import main

    with pytest.raises(SystemExit) as exit_code:
        main(["completion", "nushell"])

    assert exit_code.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_without_the_extra_it_says_what_to_install(capsys, monkeypatch):
    """The library is optional, and a bot that would not start without a completion package is a poor
    trade. So the failure is a sentence naming the extra, not a traceback."""
    import builtins

    from b3.cli import main

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if name.startswith("argcomplete"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    assert main(["completion", "bash"]) == 1
    assert "b3ng[completion]" in capsys.readouterr().out


def test_the_cli_runs_normally_with_no_completion_library(tmp_path, monkeypatch):
    """The hook is a no-op when the package is absent, silently: that is what makes it an extra."""
    import builtins

    from b3.cli import main

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if name.startswith("argcomplete"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    monkeypatch.chdir(tmp_path)

    assert main(["init", "srv", "--game", "cod4", "--rcon-password", "pw"]) == 0


# -- through argcomplete itself -------------------------------------------------------------------


def _complete(line: str) -> list[str]:
    """What the shell would be offered for a command line, via argcomplete's own finder.

    This is the only way to know a completer is *reachable*: one attached to the wrong object is
    silent, and tab would do nothing at all with every other test in this file still passing.
    """
    from argcomplete.finders import CompletionFinder

    from b3.cli import build_parser

    finder = CompletionFinder(build_parser())
    words = line.split(" ")
    return list(finder._get_completions(words[:-1], words[-1], "", None))


def test_a_game_is_completed_from_the_parser_the_cli_builds():
    pytest.importorskip("argcomplete")

    offered = _complete("b3 init srv --game bf")

    assert {"bf3", "bf4", "bfbc2", "bfh"} <= set(offered)


def test_a_plugin_name_is_completed_where_a_command_takes_one():
    pytest.importorskip("argcomplete")

    offered = _complete("b3 plugin enable power")

    assert "poweradminurt" in offered


def test_the_wizard_and_the_shell_agree_on_what_is_bundled():
    """One reading, not two: two answers to "what is bundled?" would eventually differ."""
    from b3.core.wizard import bundled_plugins

    assert bundled_plugins() == completion.bundled()


def test_a_migration_revision_is_completed():
    pytest.importorskip("argcomplete")

    offered = _complete("b3 db upgrade --revision 000")

    assert {"0001", "0002", "0003"} <= set(offered)
