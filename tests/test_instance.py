"""`b3 init` — scaffolding one bot instance per game server.

The classic bot abandoned its own setup wizard and sent people to a website to hand-craft XML,
which is how so many installs ended up running configs nobody understood. The bar here is that
what `init` writes must actually load and run.
"""

from __future__ import annotations


import pytest

from b3.cli import main
from b3.config.loader import load_config
from b3.core.instance import RESTART_EXIT_CODE, InstanceError, InstanceSpec, create_instance


def test_init_writes_a_config_that_actually_loads(tmp_path):
    written = create_instance(
        InstanceSpec(
            directory=tmp_path / "cod4_1" / "b3",
            name="cod4_1",
            port=28961,
            rcon_password="secret",
            game_log="/srv/cod4_1/main/games_mp.log",
        )
    )

    config_path = tmp_path / "cod4_1" / "b3" / "b3.yaml"
    assert written == [config_path]

    config = load_config(str(config_path))
    assert config.bot.name == "cod4_1"
    assert config.server.port == 28961
    assert config.server.rcon_password == "secret"
    assert config.server.game_log == "/srv/cod4_1/main/games_mp.log"
    assert config.bot.plugins_dir == "@conf/plugins"  # this server's own plugins
    assert [p.name for p in config.plugins] == ["admin"]


def test_init_writes_a_config_that_loads_on_windows_too(tmp_path):
    """The bug this pins: paths went into *double*-quoted YAML, where a backslash starts an escape.

    `--game-log C:\\Users\\b3\\log.txt` produced a config whose next line PyYAML refused to read — `\\U`
    is the start of a unicode escape — so an operator's first command after a successful setup was a
    traceback. Found by running `b3 init` on Windows with an absolute path, which no test did.
    """
    create_instance(
        InstanceSpec(
            directory=tmp_path / "b3",
            game_log=r"C:\Users\b3\Altitude\servers\log.txt",
            database=r"sqlite:///C:\Users\b3\b3.sqlite",
            shared_plugins_dir=r"C:\Users\b3\shared",
        )
    )

    config = load_config(str(tmp_path / "b3" / "b3.yaml"))
    assert config.server.game_log == r"C:\Users\b3\Altitude\servers\log.txt"
    assert config.bot.shared_plugins_dir == r"C:\Users\b3\shared"
    assert config.bot.database.endswith(r"C:\Users\b3\b3.sqlite")


@pytest.mark.parametrize(
    "password",
    [r"back\slash", 'has"a"quote', "has'a'apostrophe", r"^1colour\tab", "trailing\\"],
)
def test_an_awkward_rcon_password_survives_the_round_trip(tmp_path, password):
    """A password is whatever the server's config says, including characters YAML cares about. One
    that broke the file used to mean a bot that would not start and no clue why."""
    create_instance(InstanceSpec(directory=tmp_path / "b3", rcon_password=password))
    assert load_config(str(tmp_path / "b3" / "b3.yaml")).server.rcon_password == password


def test_init_creates_the_plugins_directory(tmp_path):
    create_instance(InstanceSpec(directory=tmp_path / "b3"))
    assert (tmp_path / "b3" / "plugins").is_dir()


def test_init_records_a_shared_pool_when_asked(tmp_path):
    create_instance(
        InstanceSpec(directory=tmp_path / "b3", shared_plugins_dir="/opt/b3/plugins")
    )
    config = load_config(str(tmp_path / "b3" / "b3.yaml"))
    assert config.bot.shared_plugins_dir == "/opt/b3/plugins"


def test_init_leaves_the_shared_pool_unset_by_default(tmp_path):
    create_instance(InstanceSpec(directory=tmp_path / "b3"))
    assert load_config(str(tmp_path / "b3" / "b3.yaml")).bot.shared_plugins_dir is None


def test_init_refuses_to_overwrite_a_live_instance(tmp_path):
    create_instance(InstanceSpec(directory=tmp_path / "b3", name="original"))
    with pytest.raises(InstanceError, match="already exists"):
        create_instance(InstanceSpec(directory=tmp_path / "b3", name="replacement"))

    # ...and the operator's config is untouched.
    assert "original" in (tmp_path / "b3" / "b3.yaml").read_text(encoding="utf-8")


def test_force_overwrites(tmp_path):
    create_instance(InstanceSpec(directory=tmp_path / "b3", name="original"))
    create_instance(InstanceSpec(directory=tmp_path / "b3", name="replacement"), force=True)
    assert "replacement" in (tmp_path / "b3" / "b3.yaml").read_text(encoding="utf-8")


def test_the_admin_config_is_copied_in(tmp_path):
    source = tmp_path / "plugin_admin.yaml"
    source.write_text("settings:\n  ban_duration: 14d\n", encoding="utf-8")

    written = create_instance(
        InstanceSpec(directory=tmp_path / "b3"), admin_config_source=source
    )

    copied = tmp_path / "b3" / "plugin_admin.yaml"
    assert copied in written
    assert "ban_duration" in copied.read_text(encoding="utf-8")


def test_the_systemd_unit_treats_the_restart_code_as_a_restart(tmp_path):
    """`!restart` exits 221; a unit that called that a crash would back off and give up."""
    create_instance(
        InstanceSpec(directory=tmp_path / "b3", name="cod4_1"),
        service=True,
        python="/opt/b3/venv/bin/python",
        user="b3",
    )

    unit = (tmp_path / "b3" / "b3-cod4_1.service").read_text(encoding="utf-8")
    assert f"RestartForceExitStatus={RESTART_EXIT_CODE}" in unit
    assert f"SuccessExitStatus={RESTART_EXIT_CODE}" in unit
    assert "Restart=always" in unit
    assert "/opt/b3/venv/bin/python -m b3.cli -c" in unit
    assert "User=b3" in unit


def test_no_unit_file_unless_asked(tmp_path):
    create_instance(InstanceSpec(directory=tmp_path / "b3", name="x"))
    assert not (tmp_path / "b3" / "b3-x.service").exists()


# -- through the CLI, which is how anyone will actually meet it ---------------------------------


def test_init_through_the_cli_needs_no_existing_config(tmp_path, capsys, monkeypatch):
    """`b3 init` is what you run when there is no b3.yaml yet — it must not try to load one."""
    monkeypatch.chdir(tmp_path)  # no b3.yaml here at all

    exit_code = main(["init", "cod4_1/b3", "--name", "cod4_1", "--port", "28961"])

    assert exit_code == 0
    assert (tmp_path / "cod4_1" / "b3" / "b3.yaml").is_file()
    assert "wrote" in capsys.readouterr().out


def test_the_cli_warns_about_an_empty_rcon_password(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["init", "srv"])
    assert "rcon_password" in capsys.readouterr().out


def test_the_cli_reports_a_refusal_instead_of_raising(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    main(["init", "srv"])
    with caplog.at_level("ERROR"):
        assert main(["init", "srv"]) == 1
    assert "already exists" in caplog.text


def test_a_missing_game_log_is_reported_rather_than_raised(tmp_path, monkeypatch, caplog):
    """The commonest first-run failure there is. `b3 run` used to answer it with a traceback out of
    `source.open()` — which also sat outside the try/finally, so nothing built by `_connect` was
    closed on the way out."""
    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--rcon-password", "x"])  # the game log it names does not exist
    config_path = tmp_path / "srv" / "b3.yaml"

    with caplog.at_level("ERROR"):
        assert main(["-c", str(config_path), "run"]) == 1

    assert "cannot read" in caplog.text
    assert "doctor" in caplog.text  # and it says where to get the whole picture
    assert "Traceback" not in caplog.text


def test_a_scaffolded_instance_runs(tmp_path, monkeypatch):
    """The whole point: what init writes boots a bot and processes a log line."""
    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--name", "cod4_1", "--rcon-password", "x"])

    log = tmp_path / "srv" / "games_mp.log"
    log.write_text("  0:12 J;aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa;1;Admin\n", encoding="utf-8")
    config_path = tmp_path / "srv" / "b3.yaml"

    assert main(["-c", str(config_path), "replay", str(log)]) == 0
    assert (tmp_path / "srv" / "b3.sqlite").is_file()  # database created next to the config
