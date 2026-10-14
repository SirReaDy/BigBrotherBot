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
    default_plugins,
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


def test_a_question_mark_lists_the_games_and_asks_again():
    """A double-clicked `b3.exe` asks this in a window with no shell to type `b3 games` into."""
    script = Script(["?", "urt43", "iourt43"])

    game = Prompter(reader=script.read, writer=script.write).ask(
        "Which game", "cod4", validate=valid_game
    )

    assert game == "iourt43"
    assert len(script.asked) == 3  # asked again after the list, and after the typo
    listed = "\n".join(script.said)
    assert "cod4x" in listed and "iourt43" in listed and "bf3" in listed
    assert "Did you mean iourt43" in listed and "Type ? to see them all" in listed


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


def test_a_relative_sqlite_path_is_checked_in_the_servers_folder_and_not_created(
    tmp_path, monkeypatch
):
    """Opened as written it resolved against wherever `b3 init` ran - a file the bot never reads,
    left behind empty (beside the exe, on a double-click). The bot creates it on first use."""
    started_in = tmp_path / "started-here"
    started_in.mkdir()
    monkeypatch.chdir(started_in)
    server = tmp_path / "b3" / "cod4_1"  # not created yet: the questions come first

    assert valid_database("sqlite:///b3.sqlite", server) is None

    assert list(tmp_path.rglob("*.sqlite")) == []  # nothing made, anywhere
    assert not server.exists()


def test_an_existing_file_that_is_not_a_database_is_refused(tmp_path):
    (tmp_path / "b3.sqlite").write_text("this is a text file, not a database", encoding="utf-8")

    complaint = valid_database("sqlite:///b3.sqlite", tmp_path)

    assert complaint is not None and "could not open it" in complaint


def test_an_existing_database_in_the_servers_folder_is_opened(tmp_path):
    import sqlite3

    sqlite3.connect(tmp_path / "b3.sqlite").close()
    assert valid_database("sqlite:///b3.sqlite", tmp_path) is None


def test_a_sqlite_file_where_nothing_can_be_written_is_refused(tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(os, "access", lambda path, mode: False)  # noqa: ARG005
    complaint = valid_database("sqlite:///b3.sqlite", tmp_path / "server")

    assert complaint is not None and "cannot be written to" in complaint


def test_a_question_mark_lists_database_urls_and_asks_again(tmp_path):
    """Nobody types `mysql+pymysql://` from memory, and the default hints at nothing but SQLite."""
    script = Script(["?", "sqlite:///b3.sqlite"])

    database = Prompter(reader=script.read, writer=script.write).ask(
        "Database URL", "sqlite:///b3.sqlite", validate=lambda a: valid_database(a, tmp_path)
    )

    assert database == "sqlite:///b3.sqlite"
    listed = "\n".join(script.said)
    assert "mysql+pymysql://" in listed and "postgresql+psycopg://" in listed
    assert "%40" in listed  # how to write an @ in a password, the usual way a URL goes wrong


def test_every_database_example_is_a_url_sqlalchemy_reads():
    """An example that does not parse is worse than none: it is copied exactly as shown."""
    from sqlalchemy.engine import make_url

    from b3.core.wizard import DATABASE_EXAMPLES

    urls = [line.split()[0] for line in DATABASE_EXAMPLES if "://" in line]
    assert len(urls) == 7
    for url in urls:
        assert make_url(url).get_backend_name() in ("sqlite", "mysql", "postgresql")


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
            "n",  # do not run doctor
        ]
    )

    answers = ask(tmp_path, script.prompter())

    asked = [question.split(" [")[0].rstrip(": ") for question in script.asked]
    assert asked == [
        "A name for this instance (appears in logs)",
        "Which game (type ? to list them)",
        "Game server address",
        "Game server RCON port",
        "RCON password",
        "Game log (a path here, or an ftp/sftp/http URL)",
        "Database URL (type ? for examples)",
        "Check the server answers when this is written?",
    ]
    spec = answers.spec
    assert (spec.name, spec.game, spec.host, spec.port) == ("cod4_1", "cod4", "10.0.0.5", 28960)
    assert spec.rcon_password == "hunter2"
    assert spec.plugins == default_plugins("cod4")  # not asked: all that work with the game
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
            "n",
        ]
    )

    answers = ask(tmp_path, script.prompter())

    assert answers.spec.game == "cod4"
    assert answers.spec.port == 28960
    assert any("no such game" in line for line in script.said)
    assert any("1 to 65535" in line for line in script.said)


def test_a_new_server_starts_with_every_plugin_that_works_with_its_game():
    """Asked, the question had one answer anybody gave - Enter - and it took six of the 36."""
    from b3.core.wizard import bundled_plugins

    cod = default_plugins("cod4x")
    urt = default_plugins("iourt43")

    assert cod[0] == "admin" and urt[0] == "admin"  # every other plugin's dependency, first
    assert "codam" in cod and "codam" not in urt  # each game's own plugins, and only there
    assert "jumper" in urt and "poweradminurt" in urt and "poweradminurt" not in cod
    assert {"censor", "spamcontrol", "tk", "stats", "welcome", "discord"} <= set(cod)
    assert set(cod) <= set(bundled_plugins())


def test_login_is_never_switched_on_for_somebody():
    """It demotes every admin until they log in with a password, and a new install has none: the
    owner would connect to their own server with no powers."""
    assert all("login" not in default_plugins(game) for game in ("cod4x", "iourt43", "bf3"))


def test_the_wizard_says_what_it_switched_on_and_what_it_left_off(tmp_path):
    log = tmp_path / "games_mp.log"
    log.write_text("", encoding="utf-8")
    script = Script(
        ["srv", "cod4x", "", "", "pw", str(log), f"sqlite:///{tmp_path / 'b3.sqlite'}", "n"]
    )

    ask(tmp_path, script.prompter())

    said = "\n".join(script.said)
    assert f"all {len(default_plugins('cod4x'))} plugins that work with cod4x" in said
    assert "Left off: login" in said and "!setpassword" in said


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
    script = Script([""] * 7 + ["n"])

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
