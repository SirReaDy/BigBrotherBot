"""`b3 init --interactive` — the questions, and the answers they refuse.

`b3 init` could already write a valid config; what it could not do was *ask*. The tests that matter
are the refusals: an answer is validated as it is given, because an error at question three is worth
ten times the same error after the file is written, when it arrives as a traceback from a command the
operator did not connect to their typo.

The wizard takes its input and output as arguments, which is what makes this a test file rather than
a terminal session.
"""

from __future__ import annotations

import pytest

from b3.core.instance import InstanceSpec, create_instance, plugin_lines
from b3.core.wizard import (
    Prompter,
    ask,
    bundled_plugins,
    unknown_plugins,
    valid_database,
    valid_game,
    valid_log,
    valid_plugins,
    valid_port,
)


class Script:
    """Somebody typing. Records what they were shown, so the questions can be asserted."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []
        self.said: list[str] = []

    def read(self, prompt: str) -> str:
        self.asked.append(prompt)
        if not self.answers:
            raise EOFError(f"nothing left to answer {prompt!r}")
        return self.answers.pop(0)

    def write(self, text: str = "") -> None:
        self.said.append(text)

    def prompter(self) -> Prompter:
        return Prompter(reader=self.read, writer=self.write)


# -- the answers it refuses ----------------------------------------------------------------------


def test_a_game_that_does_not_exist_is_refused_with_the_nearest_one():
    assert valid_game("cod4") is None
    complaint = valid_game("cod44")
    assert complaint is not None and "cod4" in complaint


def test_a_port_is_a_port():
    assert valid_port("28960") is None
    assert valid_port("0") is not None
    assert valid_port("70000") is not None
    assert valid_port("twenty") is not None


def test_a_game_log_that_is_not_there_is_caught_now(tmp_path):
    """The commonest first-run failure of all, and the cheapest moment to catch it."""
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")

    assert valid_log(str(log)) is None
    complaint = valid_log(str(tmp_path / "nope.log"))
    assert complaint is not None and "does not exist" in complaint


def test_a_remote_log_is_accepted_without_being_opened():
    """The credentials in it are the operator's, and this is not the moment to make them wait on a
    network round trip — nor to fail because a game host is briefly down."""
    assert valid_log("ftp://user:pass@host/games_mp.log") is None
    assert valid_log("sftp://host/games_mp.log") is None
    assert valid_log("https://host/logs/games_mp.log") is None


def test_a_database_is_checked_by_opening_it(tmp_path):
    """Which is the only way to tell a URL from a working database — a missing driver is the usual
    answer, and much better heard now than at the first start."""
    assert valid_database(f"sqlite:///{tmp_path / 'b3.sqlite'}") is None

    complaint = valid_database("nonsense://host/db")
    assert complaint is not None and "could not open it" in complaint


def test_an_empty_answer_where_one_is_needed_says_what_it_is_for():
    assert "no events" in (valid_log("") or "")
    assert "SQLAlchemy" in (valid_database("") or "")


def test_a_plugin_name_that_is_not_one_is_refused():
    assert valid_plugins("admin censor tk") is None
    complaint = valid_plugins("admin ../../etc/passwd")
    assert complaint is not None and "not a plugin name" in complaint


def test_the_plugin_list_is_read_from_the_package_rather_than_restated():
    """A generator with its own copy of the list is a lie waiting to happen."""
    names = bundled_plugins()

    assert "admin" in names and "jumper" in names
    assert len(names) >= 30
    # ...and a plugin nobody bundles is not an error: a git-installed one is named the same way.
    assert unknown_plugins(["admin", "somebodys_plugin"]) == ["somebodys_plugin"]


# -- the walk ------------------------------------------------------------------------------------


def test_the_questions_are_asked_in_the_operators_order(tmp_path):
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")
    script = Script(
        [
            "cod4_1",  # name
            "cod4",  # game
            "10.0.0.5",  # host
            "28960",  # port
            "hunter2",  # rcon password
            str(log),  # game log
            f"sqlite:///{tmp_path / 'b3.sqlite'}",  # database
            "admin censor",  # plugins
            "n",  # do not run doctor
        ]
    )

    answers = ask(tmp_path, script.prompter())

    asked = [question.split(" [")[0].rstrip(": ") for question in script.asked]
    assert asked == [
        "A name for this instance (appears in logs)",
        "Which game (`b3 games` lists them all)",
        "Game server address",
        "Game server RCON port",
        "RCON password",
        "Game log (a path here, or an ftp/sftp/http URL)",
        "Database URL",
        "Which ones (space separated)",
        "Check the server answers when this is written?",
    ]
    spec = answers.spec
    assert (spec.name, spec.game, spec.host, spec.port) == ("cod4_1", "cod4", "10.0.0.5", 28960)
    assert spec.rcon_password == "hunter2"
    assert spec.plugins == ("admin", "censor")
    assert answers.run_doctor is False


def test_a_bad_answer_is_asked_again_rather_than_written(tmp_path):
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")
    script = Script(
        [
            "srv",
            "cod44",  # a typo...
            "cod4",  # ...corrected
            "127.0.0.1",
            "not-a-port",  # again
            "28960",
            "pw",
            str(log),
            f"sqlite:///{tmp_path / 'b3.sqlite'}",
            "admin",
            "n",
        ]
    )

    answers = ask(tmp_path, script.prompter())

    assert answers.spec.game == "cod4"
    assert answers.spec.port == 28960
    assert any("no such game" in line for line in script.said)
    assert any("1 to 65535" in line for line in script.said)


def test_admin_is_always_first_however_the_answer_was_typed(tmp_path):
    """It is the command framework's only consumer and every other plugin's dependency: an operator
    who leaves it out has not asked for a bot with no commands."""
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")
    script = Script(
        [
            "srv",
            "cod4",
            "127.0.0.1",
            "28960",
            "pw",
            str(log),
            f"sqlite:///{tmp_path / 'b3.sqlite'}",
            "tk, censor",  # commas, and no admin
            "y",
        ]
    )

    answers = ask(tmp_path, script.prompter())

    assert answers.spec.plugins == ("admin", "tk", "censor")
    assert answers.run_doctor is True


def test_a_title_with_no_rcon_password_is_not_asked_for_one(tmp_path):
    """Altitude is driven by writing to a file the server reads. Asking for a password there is
    asking for something that does not exist."""
    log = tmp_path / "log.txt"
    log.write_text("", encoding="utf-8")
    script = Script(
        [
            "alt",
            "altitude",
            "127.0.0.1",
            "27275",
            str(tmp_path / "commands.txt"),  # command file, in place of the password
            str(log),
            f"sqlite:///{tmp_path / 'b3.sqlite'}",
            "admin",
            "n",
        ]
    )

    answers = ask(tmp_path, script.prompter())

    assert not any("RCON password" in question for question in script.asked)
    assert answers.spec.command_file.endswith("commands.txt")


def test_an_empty_answer_takes_the_default(tmp_path):
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")
    defaults = InstanceSpec(
        directory=tmp_path,
        name="preset",
        game="cod4",
        host="1.2.3.4",
        port=1234,
        rcon_password="pw",
        game_log=str(log),
        database=f"sqlite:///{tmp_path / 'b3.sqlite'}",
    )
    script = Script([""] * 8 + ["n"])

    answers = ask(tmp_path, script.prompter(), defaults=defaults)

    assert (answers.spec.name, answers.spec.host, answers.spec.port) == ("preset", "1.2.3.4", 1234)
    assert "[preset]" in script.asked[0]  # the default is shown, not guessed at


# -- what gets written ---------------------------------------------------------------------------


def test_the_chosen_plugins_reach_the_config_and_their_configs_are_copied(tmp_path):
    examples = tmp_path / "examples"
    examples.mkdir()
    (examples / "plugin_admin.yaml").write_text("settings: {}\n", encoding="utf-8")
    (examples / "plugin_censor.yaml").write_text("badwords: []\n", encoding="utf-8")

    written = create_instance(
        InstanceSpec(directory=tmp_path / "b3", plugins=("admin", "censor", "spree")),
        examples_dir=examples,
    )

    config = (tmp_path / "b3" / "b3.yaml").read_text(encoding="utf-8")
    assert "  - name: admin" in config
    assert '    config: "@conf/plugin_censor.yaml"' in config
    # `spree` has no example here, so it gets a name and no `config:` line — a config pointing at a
    # file that is not there is worse than none, because the loader reports it as a missing config.
    assert "  - name: spree" in config
    assert "plugin_spree.yaml" not in config
    assert {path.name for path in written} >= {"b3.yaml", "plugin_admin.yaml", "plugin_censor.yaml"}


def test_a_plugin_list_with_no_examples_directory_still_names_them():
    lines = plugin_lines(["admin", "tk"], None)

    assert lines == "  - name: admin\n  - name: tk\n"


# -- through the CLI -----------------------------------------------------------------------------


def test_init_with_a_game_does_not_stop_to_ask(tmp_path, monkeypatch):
    """A scripted `b3 init` with its flags — in a Dockerfile, in a provisioning script — must never
    stop and wait for somebody who is not there."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)

    def explode(prompt):  # noqa: ANN001, ANN202
        raise AssertionError(f"asked {prompt!r} in a non-interactive run")

    monkeypatch.setattr("builtins.input", explode)

    assert main(["init", "srv", "--game", "cod4", "--rcon-password", "pw"]) == 0
    assert (tmp_path / "srv" / "b3.yaml").is_file()


@pytest.mark.parametrize("flag", ["--no-interactive"])
def test_init_can_be_told_never_to_ask(tmp_path, monkeypatch, flag):
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: (_ for _ in ()).throw(AssertionError("asked anyway")),  # noqa: ARG005
    )

    assert main(["init", "srv", flag]) == 0


def test_interactive_init_writes_what_was_answered(tmp_path, monkeypatch, capsys):
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")
    answers = iter(
        [
            "srv",
            "cod4",
            "10.0.0.9",
            "28960",
            "hunter2",
            str(log),
            f"sqlite:///{tmp_path / 'b3.sqlite'}",
            "admin",
            "n",  # do not run doctor: it would try to reach a server
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))  # noqa: ARG005

    code = main(["init", "srv", "--interactive"])

    config = (tmp_path / "srv" / "b3.yaml").read_text(encoding="utf-8")
    assert code == 0
    assert "10.0.0.9" in config and "hunter2" in config
    assert "wrote" in capsys.readouterr().out


def test_giving_up_part_way_through_writes_nothing(tmp_path, monkeypatch, capsys):
    """Ctrl-D at question four. The questions come before the file precisely so this is possible."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    answers = iter(["srv", "cod4"])

    def read(prompt):  # noqa: ANN001, ANN202, ARG001
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", read)

    code = main(["init", "srv", "--interactive"])

    assert code == 1
    assert not (tmp_path / "srv" / "b3.yaml").exists()
    assert "nothing written" in capsys.readouterr().out
