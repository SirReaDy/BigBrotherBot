"""Altitude's transport: a file the game server reads commands out of.

There is no socket, so the awkward parts are all in the file — framing, the shared file a
neighbouring server also reads, and the queue the game server never truncates and therefore replays
on its next start. The fake server in `tools/fakeservers/altitude.py` models all three.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, ConfigError, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.domain.client import Client
from b3.net.altitude import AltitudeCommandFile
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.altitude import FakeAltitudeServer

PORT = 27276
BOB_ID = "d6545616-17a3-4044-a74b-121231321321"


def _client_file(tmp_path, port: int = PORT) -> AltitudeCommandFile:  # noqa: ANN001
    return AltitudeCommandFile(tmp_path / "command.txt", port=port)


def _lines(client: AltitudeCommandFile) -> list[str]:
    return client.path.read_text(encoding="utf-8").splitlines()


# -- framing -------------------------------------------------------------------------------------


def test_a_command_is_framed_for_this_servers_port(tmp_path):
    """The prefix is what says which server a command is for; one installation shares this file."""
    client = _client_file(tmp_path)
    client.open()
    client.command("kick Bob")
    assert _lines(client) == [f"{PORT},console,kick Bob"]


def test_a_command_never_waits_for_a_reply_because_there_is_none(tmp_path):
    client = _client_file(tmp_path)
    client.open()
    assert client.command("kick Bob") == ""


def test_a_two_verb_command_is_written_as_two_commands(tmp_path):
    """How a profile expresses a penalty this engine has no single verb for: `addBan` does not
    remove the player who is already connected, so a ban is a ban *and* a kick."""
    client = _client_file(tmp_path)
    client.open()
    client.command("addBan abc 1 forever cheating\nkick Bob")
    assert _lines(client) == [
        f"{PORT},console,addBan abc 1 forever cheating",
        f"{PORT},console,kick Bob",
    ]


def test_an_empty_command_is_not_written_at_all(tmp_path):
    """A bare "27276,console," is a line the server has to parse and can do nothing with. Reachable:
    a profile with no rotation verb used to send one."""
    client = _client_file(tmp_path)
    client.open()
    client.command("")
    client.command("   \n  ")
    assert _lines(client) == []


# -- the queue the server replays -----------------------------------------------------------------


def test_the_file_is_emptied_on_open_so_a_crash_cannot_replay_old_penalties(tmp_path, caplog):
    """The classic parser only cleared this on a clean shutdown, so a killed bot left its commands
    for the game server to find and re-run against whoever holds those names now."""
    path = tmp_path / "command.txt"
    path.write_text(
        f"{PORT},console,kick Bob\n{PORT},console,addBan {BOB_ID} 1 forever old\n", encoding="utf-8"
    )

    client = AltitudeCommandFile(path, port=PORT)
    with caplog.at_level("WARNING"):
        client.open()

    assert path.read_text(encoding="utf-8") == ""
    assert "previous run" in caplog.text  # and it says so rather than swallowing it


def test_the_file_is_emptied_on_close_too(tmp_path):
    client = _client_file(tmp_path)
    client.open()
    client.command("kick Bob")
    client.close()
    assert _lines(client) == []


def test_the_directory_is_created_if_it_does_not_exist(tmp_path):
    client = AltitudeCommandFile(tmp_path / "nested" / "deeper" / "command.txt", port=PORT)
    client.open()
    client.command("kick Bob")
    assert client.path.is_file()


# -- against the fake server ----------------------------------------------------------------------


def test_the_fake_server_executes_what_the_bot_writes(tmp_path):
    server = FakeAltitudeServer(tmp_path / "srv", port=PORT).start()
    server.add_player(1, "Bob", BOB_ID)
    client = AltitudeCommandFile(server.command_path, port=PORT)
    client.open()

    client.command(f"addBan {BOB_ID} 1 forever cheating\nkick Bob")
    server.poll()

    assert server.received == [f"addBan {BOB_ID} 1 forever cheating", "kick Bob"]
    assert server.bans == {BOB_ID: ("1", "forever", "cheating")}
    assert 1 not in server.players  # the kick landed
    assert server.malformed == []


def test_a_command_for_another_server_sharing_the_file_is_not_ours(tmp_path):
    """Which is exactly what happens if the port prefix is ever wrong."""
    server = FakeAltitudeServer(tmp_path / "srv", port=PORT).start()
    neighbour = AltitudeCommandFile(server.command_path, port=27999)
    neighbour.command("kick Bob")
    server.poll()

    assert server.received == []
    assert server.for_other_ports == [(27999, "kick Bob")]


def test_a_bare_command_would_be_rejected_by_the_server(tmp_path):
    """The fake is strict on purpose: this is what the framing protects against."""
    server = FakeAltitudeServer(tmp_path / "srv", port=PORT).start()
    server.command_path.write_text("kick Bob\n", encoding="utf-8")
    server.poll()

    assert server.received == []
    assert server.malformed == ["kick Bob"]


def test_a_restarting_server_replays_a_file_that_was_not_emptied(tmp_path):
    """The failure mode the clear-on-open/close exists for, proven from the server's side."""
    server = FakeAltitudeServer(tmp_path / "srv", port=PORT).start()
    server.add_player(1, "Bob", BOB_ID)
    raw = AltitudeCommandFile(server.command_path, port=PORT)
    raw.command("kick Bob")
    server.poll()
    assert server.received == ["kick Bob"]

    # Nobody cleared it: the server rewinds and does it all again.
    assert server.restart() == ["kick Bob"]

    # Cleared, as the bot does on both open and close: nothing to replay.
    raw.close()
    assert server.restart() == []


# -- through the bot ------------------------------------------------------------------------------


def _bot(tmp_path):  # noqa: ANN001, ANN202
    server = FakeAltitudeServer(tmp_path / "srv", port=PORT).start()
    client = AltitudeCommandFile(server.command_path, port=PORT)
    client.open()
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="altitude",
            port=PORT,
            game_log=str(server.log_path),
            command_file=str(server.command_path),
        ),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, rcon=client, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    return bot, server


@pytest.mark.asyncio
async def test_the_penalties_use_the_handle_each_verb_actually_takes(tmp_path):
    """`kick` and `serverWhisper` take the name; `addBan` and `removeBan` take the vapor id. Neither
    takes the slot the log lines are keyed on."""
    bot, server = _bot(tmp_path)
    bob = Client(cid="1", guid=BOB_ID, name="Bob")
    bot.clients.add(bob)
    server.add_player(1, "Bob", BOB_ID)

    bot.kick(bob, "language")
    bot.ban(bob, "cheating")
    bot.unban(bob, "appeal granted")
    server.poll()

    assert server.received == [
        "kick Bob",
        f"addBan {BOB_ID} 1 forever cheating",
        "kick Bob",
        f"removeBan {BOB_ID}",
    ]
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_tempban_uses_the_games_own_minutes(tmp_path):
    bot, server = _bot(tmp_path)
    bob = Client(cid="1", guid=BOB_ID, name="Bob")
    bot.clients.add(bob)

    bot.tempban(bob, minutes=30, reason="spam")
    server.poll()

    assert server.received[0] == f"addBan {BOB_ID} 30 minute spam"
    assert server.received[1] == "kick Bob"
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_chat_goes_out_as_the_two_verbs_this_game_has(tmp_path):
    bot, server = _bot(tmp_path)
    bob = Client(cid="1", guid=BOB_ID, name="Bob")
    bot.clients.add(bob)

    bot.say("hello everyone")
    bot.tell(bob, "watch your language")
    bot.say_big("round over")  # no centre-screen verb: falls back to a plain message
    server.poll()

    # The bot's prefix rides in front of everything it says, and `[pm]` on top of it for a private
    # message — the classic's `msgPrefix`/`pmPrefix`. What this test is about is the two *verbs*.
    assert server.received == [
        "serverMessage ^2(b3)^7: hello everyone",
        "serverWhisper Bob ^2(b3)^7: ^8[pm]^7 watch your language",
        "serverMessage ^2(b3)^7: round over",
    ]
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_hostile_name_cannot_smuggle_a_second_command_into_the_file(tmp_path):
    """The newline is this transport's command separator, so a name containing one would be a free
    command. `sanitize_rcon_value` strips it before it reaches the template."""
    bot, server = _bot(tmp_path)
    bob = Client(cid="1", guid=BOB_ID, name="Bob\nchangeMap ball_cave")
    bot.clients.add(bob)

    bot.kick(bob, "cheating")
    server.poll()

    assert server.received == ["kick Bob changeMap ball_cave"]  # one command, not two
    assert "mapChange" not in server.log_path.read_text(encoding="utf-8")
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_syncing_does_not_empty_the_roster_on_an_engine_that_cannot_be_asked(tmp_path):
    """The trap: `status_command` is empty here, so a runtime that asked anyway would get "" back,
    parse it as an empty player list, and drop every player the log told it about — every five
    minutes, on the scheduled sync."""
    bot, server = _bot(tmp_path)
    bot.clients.add(Client(cid="0", guid="a" * 36, name="Courgette"))
    bot.clients.add(Client(cid="1", guid=BOB_ID, name="Bob"))

    remaining = bot.sync()

    assert {c.cid for c in remaining} == {"0", "1"}
    server.poll()
    assert server.received == []  # and nothing was written to the file in the attempt
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_servers_own_name_and_player_limit_are_recorded(tmp_path):
    """They were parsed and thrown away: the event was published and nothing listened."""
    bot, server = _bot(tmp_path)

    await bot.feed_line(
        '{"port": %d, "time": 1, "type": "serverInit", "name": "Ball Fans Only", '
        '"maxPlayerCount": 14}' % PORT
    )

    assert bot.game.hostname == "Ball Fans Only"
    assert bot.game.max_players == 14
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_map_is_answered_from_the_log_since_there_is_nothing_to_ask(tmp_path):
    bot, server = _bot(tmp_path)
    bot.game.map_name = "ball_grotto"

    assert bot.get_map() == "ball_grotto"
    server.poll()
    assert server.received == []
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_map_rotation_says_it_cannot_rather_than_doing_nothing(tmp_path, caplog):
    """This engine has no rotation verb — the classic parser's `rotateMap` was an empty method, so
    `!maprotate` reported success and did nothing at all."""
    bot, server = _bot(tmp_path)

    with caplog.at_level("WARNING"):
        bot.rotate_map()

    assert "no map-rotation command" in caplog.text
    server.poll()
    assert server.received == []
    await bot.bus.drain()
    bot.storage.close()


@pytest.mark.asyncio
async def test_changing_to_a_named_map_does_work(tmp_path):
    bot, server = _bot(tmp_path)
    bot.change_map("ball_cave")
    server.poll()
    assert server.received == ["changeMap ball_cave"]
    await bot.bus.drain()
    bot.storage.close()


# -- the configuration this family cannot work without --------------------------------------------


def test_running_without_a_command_file_is_refused_in_one_line(tmp_path):
    """Not a crash and not a warning: with nowhere to write, every kick, ban and reply would go
    nowhere and report success."""
    from b3.cli import _command_file_client

    config = Config(server=ServerConfig(game="altitude", port=PORT))
    try:
        _command_file_client(config)
    except ConfigError as exc:
        assert "command_file" in str(exc)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a missing command_file must be refused")


def test_the_cli_reports_that_refusal_instead_of_raising(tmp_path, monkeypatch, caplog):
    """A hand-written config — which is how this arrives, since `b3 init` fills the path in."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    log = tmp_path / "log.txt"
    log.write_text(
        "", encoding="utf-8"
    )  # so the refusal is about the command file and nothing else
    config_path = tmp_path / "b3.yaml"
    config_path.write_text(
        f'bot:\n  database: "sqlite:///{(tmp_path / "b3.sqlite").as_posix()}"\n'
        f'server:\n  game: altitude\n  port: {PORT}\n  game_log: "{log.as_posix()}"\n',
        encoding="utf-8",
    )

    with caplog.at_level("ERROR"):
        assert main(["-c", str(config_path), "run"]) == 1
    assert "command_file" in caplog.text
    assert "Traceback" not in caplog.text


def test_connect_pairs_a_log_source_with_the_command_file(tmp_path):
    """The one path the end-to-end driver skips, because it builds the client itself."""
    from b3.cli import _connect
    from b3.net.logsource import FileLogSource

    log = tmp_path / "log.txt"
    log.write_text("", encoding="utf-8")
    config = Config(
        server=ServerConfig(
            game="altitude",
            port=PORT,
            game_log=str(log),
            command_file=str(tmp_path / "command.txt"),
            encoding="latin-1",  # the CoD default, which must NOT be used for a JSON log
        )
    )

    connection = _connect(config)

    assert isinstance(connection.rcon, AltitudeCommandFile)
    assert isinstance(connection.source, FileLogSource)
    assert connection.shared is False  # two separate things here, unlike BattlEye
    assert connection.rcon.port == PORT
    assert "command.txt" in connection.description
    # JSON is utf-8 by definition; latin-1 would not fail, it would quietly mangle every non-ASCII
    # player name, which is the worse outcome.
    assert connection.source._encoding == "utf-8"


def test_doctor_reports_the_command_file_as_the_rcon_row(tmp_path):
    from b3.core.doctor import Status, run_checks

    log = tmp_path / "log.txt"
    log.write_text("{}\n", encoding="utf-8")
    command = tmp_path / "command.txt"
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="altitude", port=PORT, game_log=str(log), command_file=str(command)
        ),
        plugins=[PluginEntry(name="admin")],
    )

    def _rcon_row(cfg: Config):  # noqa: ANN202
        return next(c for c in run_checks(cfg, tmp_path) if c.name == "rcon")

    # Writable and empty: fine, and it says the quiet part — there is no rcon port to check.
    check = _rcon_row(config)
    assert check.status is Status.OK
    assert "no rcon port" in check.detail

    # Holding commands from a previous run: worth a word, not a failure.
    command.write_text(f"{PORT},console,kick Bob\n", encoding="utf-8")
    check = _rcon_row(config)
    assert check.status is Status.WARN
    assert "previous run" in check.detail

    # Not configured at all: the bot could watch the game and never act on it.
    config.server.command_file = ""
    check = _rcon_row(config)
    assert check.status is Status.FAIL
    assert "command_file" in (check.hint or "")


def test_doctor_does_not_ask_for_an_rcon_password_this_game_has_no_use_for(tmp_path):
    """Every other family warns when it is empty. Here it would be advice to set a dead setting."""
    from b3.core.doctor import Status, run_checks

    log = tmp_path / "log.txt"
    log.write_text("{}\n", encoding="utf-8")
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="altitude",
            port=PORT,
            game_log=str(log),
            command_file=str(tmp_path / "command.txt"),
            rcon_password="",
        ),
    )

    check = next(c for c in run_checks(config, tmp_path) if c.name == "rcon")
    assert check.status is Status.OK
    assert "password" not in check.detail


def test_init_writes_what_this_family_needs(tmp_path, monkeypatch):
    """A scaffolded Altitude instance has to be runnable: the command file, and utf-8 for its JSON
    log rather than the CoD engines' latin-1."""
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["init", "srv", "--game", "altitude", "--game-log", "/srv/alt/log.txt"]) == 0

    written = (tmp_path / "srv" / "b3.yaml").read_text(encoding="utf-8")
    assert "game: altitude" in written
    assert "command_file:" in written
    assert "command.txt" in written  # guessed next to the game log
    assert "encoding: utf-8" in written


def test_init_leaves_every_other_game_alone(tmp_path, monkeypatch):
    from b3.cli import main

    monkeypatch.chdir(tmp_path)
    main(["init", "srv", "--game", "cod4"])
    written = (tmp_path / "srv" / "b3.yaml").read_text(encoding="utf-8")
    assert "command_file" not in written
    assert "encoding: latin-1" in written
