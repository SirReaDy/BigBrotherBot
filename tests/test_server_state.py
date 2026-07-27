"""Live server state: `status`/cvar reply parsing, the Game object, the RCON query verbs,
player-list reconciliation, and the status-poll auth resolver that finally supplies player IPs.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.core.game import Game
from b3.domain.client import Client
from b3.parsers.cod import status as sp
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot

GADMIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
GBOB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

STATUS = """map: mp_crash
num score ping guid                             name            lastmsg address               qport rate
--- ----- ---- -------------------------------- --------------- ------- --------------------- ----- -----
  0    12   47 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Admin                50 192.0.2.44:28960     12345 25000
  2    -1  102 bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb Bob the Builder      70 198.51.100.88:28961      6789 25000
"""


class ScriptedRcon:
    """An RCON whose reply to each command is looked up in a dict (regex-free, exact match)."""

    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.replies = replies or {}
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return self.replies.get(cmd, "")


def _bot(tmp_path, rcon=None, clock=None) -> Bot:
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, rcon=rcon, clock=clock or FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    # Forget the profile's startup cvars; these tests assert on what happens afterwards.
    getattr(rcon, "commands", []).clear()
    return bot


# -- reply parsing ---------------------------------------------------------------------------


def test_parse_status_reads_the_map_and_every_player():
    map_name, players = sp.parse_status(STATUS)

    assert map_name == "mp_crash"
    assert [p.cid for p in players] == ["0", "2"]
    admin, bob = players
    assert admin.name == "Admin"
    assert admin.guid == GADMIN
    assert admin.ip == "192.0.2.44"  # the whole point: the join line never carries this
    assert admin.port == 28960
    assert admin.ping == 47
    assert admin.score == 12
    assert bob.name == "Bob the Builder"  # a name with spaces still parses
    assert bob.score == -1  # negative scores are normal


def test_parse_status_survives_a_truncated_reply():
    """A status reply arrives over UDP and can be cut off mid-row."""
    truncated = STATUS[: STATUS.index("  2 ") + 40]
    map_name, players = sp.parse_status(truncated)
    assert map_name == "mp_crash"
    assert [p.cid for p in players] == ["0"]


def test_parse_status_of_an_empty_server():
    map_name, players = sp.parse_status("map: mp_vacant\nnum score ping guid\n--- ---\n")
    assert map_name == "mp_vacant"
    assert players == []


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('"sv_maxclients" is: "16^7" default: "8^7"', "16"),
        ('"sv_maxclients" is:"16^7", the default', "16"),
        ('"sv_maxclients" is: "24"', "24"),
    ],
)
def test_parse_cvar_handles_the_reply_shapes(reply, expected):
    assert sp.parse_cvar("sv_maxclients", reply) == expected


def test_parse_cvar_rejects_a_reply_about_another_cvar():
    assert sp.parse_cvar("mapname", '"sv_maxclients" is: "16^7" default: "8^7"') is None
    assert sp.parse_cvar("mapname", "unknown command") is None


def test_parse_rotation():
    value = "gametype dm map mp_crash gametype war map mp_vacant map mp_backlot"
    assert sp.parse_rotation(value) == ["mp_crash", "mp_vacant", "mp_backlot"]


# -- the Game object -------------------------------------------------------------------------


def test_game_tracks_map_and_round_timings():
    game = Game()
    game.start_map({"mapname": "mp_crash", "g_gametype": "dm"}, now=1000.0)

    assert (game.map_name, game.gametype, game.rounds) == ("mp_crash", "dm", 1)
    assert game.map_uptime(1060.0) == 60.0

    game.start_round(now=1100.0)
    assert game.rounds == 2
    assert game.round_uptime(1130.0) == 30.0
    assert game.map_uptime(1130.0) == 130.0  # the map clock keeps running across rounds


def test_game_uptime_is_zero_before_the_first_map():
    assert Game().map_uptime(1000.0) == 0.0


@pytest.mark.asyncio
async def test_initgame_updates_the_bots_game_state(tmp_path):
    bot = _bot(tmp_path)
    await bot.feed_line(r"InitGame: \g_gametype\dm\mapname\mp_crash\sv_maxclients\32")

    assert bot.game.map_name == "mp_crash"
    assert bot.game.gametype == "dm"
    assert bot.game.cvars["sv_maxclients"] == "32"
    assert bot.game.rounds == 1


@pytest.mark.asyncio
async def test_a_second_initgame_on_the_same_map_is_a_new_round(tmp_path):
    bot = _bot(tmp_path)
    line = r"InitGame: \g_gametype\dm\mapname\mp_crash"
    await bot.feed_line(line)
    await bot.feed_line(line)

    assert bot.game.rounds == 2
    assert bot.game.map_name == "mp_crash"


@pytest.mark.asyncio
async def test_changing_map_publishes_a_map_change_event(tmp_path):
    bot = _bot(tmp_path)
    seen: list[Event] = []
    bot.bus.subscribe(EventType.GAME_MAP_CHANGE, lambda e: seen.append(e))

    await bot.feed_line(r"InitGame: \mapname\mp_crash")
    await bot.feed_line(r"InitGame: \mapname\mp_vacant")

    assert bot.game.map_name == "mp_vacant"
    assert bot.game.rounds == 1  # a new map restarts the round count
    assert [e.data for e in seen] == ["mp_vacant"]  # and no event for the very first map


# -- the query verbs -------------------------------------------------------------------------


def test_get_players_parses_the_status_table(tmp_path):
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    players = bot.get_players()
    assert [(p.cid, p.ip) for p in players] == [("0", "192.0.2.44"), ("2", "198.51.100.88")]


def test_get_cvar_caches_into_the_game_state(tmp_path):
    rcon = ScriptedRcon({"sv_maxclients": '"sv_maxclients" is: "16^7" default: "8^7"'})
    bot = _bot(tmp_path, rcon=rcon)

    assert bot.get_cvar("sv_maxclients") == "16"
    assert bot.game.cvars["sv_maxclients"] == "16"


def test_get_maps_and_next_map_walk_the_rotation(tmp_path):
    rotation = '"sv_maprotation" is: "map mp_crash map mp_vacant map mp_backlot^7" default: ""'
    bot = _bot(tmp_path, rcon=ScriptedRcon({"sv_mapRotation": rotation, "status": STATUS}))

    assert bot.get_maps() == ["mp_crash", "mp_vacant", "mp_backlot"]
    assert bot.get_next_map() == "mp_vacant"  # status says we are on mp_crash


def test_next_map_wraps_at_the_end_of_the_rotation(tmp_path):
    rotation = '"sv_maprotation" is: "map mp_crash map mp_vacant^7" default: ""'
    status = STATUS.replace("map: mp_crash", "map: mp_vacant")
    bot = _bot(tmp_path, rcon=ScriptedRcon({"sv_mapRotation": rotation, "status": status}))

    assert bot.get_next_map() == "mp_crash"


def test_next_map_when_the_current_map_is_not_in_the_rotation(tmp_path):
    rotation = '"sv_maprotation" is: "map mp_vacant map mp_backlot^7" default: ""'
    bot = _bot(tmp_path, rcon=ScriptedRcon({"sv_mapRotation": rotation, "status": STATUS}))

    assert bot.get_next_map() == "mp_vacant"  # whatever the rotation starts with


def test_map_control_sends_the_profile_verbs(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)

    bot.change_map("mp_vacant")
    bot.rotate_map()
    bot.set_cvar("g_gametype", "dm")

    assert rcon.commands == ["map mp_vacant", "map_rotate", 'set g_gametype "dm"']
    assert bot.game.cvars["g_gametype"] == "dm"


def test_query_verbs_are_inert_without_an_rcon(tmp_path):
    """Replay mode has no server to ask; it must answer 'nothing known', not explode."""
    bot = _bot(tmp_path)
    assert bot.get_players() == []
    assert bot.get_cvar("sv_maxclients") is None
    assert bot.get_maps() == []
    assert bot.get_next_map() is None


def test_say_big_falls_back_to_say_when_the_engine_has_none(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    bot.say_big("round starting")
    assert rcon.commands == ["say round starting"]


# -- player-list reconciliation ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_adopts_players_the_bot_never_saw_join(tmp_path):
    """The bot was started mid-match: nobody produced a join line, so nobody is known."""
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    assert bot.clients.connected() == []

    bot.sync()

    assert sorted(c.cid for c in bot.clients.connected()) == ["0", "2"]
    admin = bot.clients.get_by_cid("0")
    assert admin.guid == GADMIN
    assert admin.ip == "192.0.2.44"
    assert bot.storage.get_client_by_guid(GADMIN) is not None  # and they are in the database


@pytest.mark.asyncio
async def test_sync_drops_a_player_the_server_no_longer_lists(tmp_path):
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    ghost = Client(guid="cccccccccccccccccccccccccccccccc", name="Ghost", cid="5")
    bot.clients.add(ghost)
    gone: list[Event] = []
    bot.bus.subscribe(EventType.CLIENT_DISCONNECT, lambda e: gone.append(e))

    bot.sync()
    await bot.bus.drain()

    assert bot.clients.get_by_cid("5") is None
    assert [e.client.name for e in gone] == ["Ghost"]


@pytest.mark.asyncio
async def test_sync_fills_in_a_missing_ip(tmp_path):
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    await bot.feed_line(f"J;{GADMIN};0;Admin")
    admin = bot.clients.get_by_cid("0")
    assert admin.ip == ""  # the join line has no IP

    bot.sync()

    assert admin.ip == "192.0.2.44"
    assert [a.value for a in bot.storage.get_ip_aliases(admin.id)] == ["192.0.2.44"]


@pytest.mark.asyncio
async def test_sync_replaces_a_recycled_slot(tmp_path):
    """Someone else is in slot 0 now — the stale client must not keep the slot."""
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    stale = Client(guid="dddddddddddddddddddddddddddddddd", name="Old", cid="0")
    bot.clients.add(stale)

    bot.sync()

    assert bot.clients.get_by_cid("0").guid == GADMIN


@pytest.mark.asyncio
async def test_scheduled_sync_publishes_its_events(tmp_path):
    """The round trip runs on a worker thread; the bus does not. Found in a live run: adopting a
    player from a worker thread dropped the event and aborted the sync half-way."""
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    ghost = Client(guid="cccccccccccccccccccccccccccccccc", name="Ghost", cid="5")
    bot.clients.add(ghost)
    gone: list[Event] = []
    bot.bus.subscribe(EventType.CLIENT_DISCONNECT, lambda e: gone.append(e))

    await bot._scheduled_sync()
    await bot.bus.drain()

    assert sorted(c.cid for c in bot.clients.connected()) == ["0", "2"]
    assert [e.client.name for e in gone] == ["Ghost"]


@pytest.mark.asyncio
async def test_a_failing_status_poll_does_not_break_the_schedule(tmp_path):
    class DeadRcon:
        def command(self, cmd: str) -> str:
            raise OSError("rcon unreachable")

    bot = _bot(tmp_path, rcon=DeadRcon())
    await bot.feed_line(f"J;{GADMIN};0;Admin")

    await bot._scheduled_sync()  # must not raise

    assert bot.clients.get_by_cid("0") is not None  # and must not drop everyone either


# -- the status-poll auth resolver ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_resolver_supplies_the_ip_a_join_line_lacks(tmp_path):
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    # No real waiting: drive the auth state machine with an instant sleep.
    bot.auth._initial_delay = 0
    bot.auth._retry_delay = 0

    await bot.feed_line(f"J;{GBOB};2;Bob")
    bob = bot.clients.get_by_cid("2")
    await bot.auth.wait_all()

    # The join line carried no IP; the status poll supplied it and it was persisted.
    assert bob.ip == "198.51.100.88"
    assert bot.storage.get_client_by_guid(GBOB).ip == "198.51.100.88"


@pytest.mark.asyncio
async def test_auth_resolver_gives_up_quietly_when_the_player_never_appears(tmp_path):
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": "map: mp_crash\n"}))
    bot.auth._initial_delay = 0
    bot.auth._retry_delay = 0
    bot.auth._max_attempts = 2

    await bot.feed_line(f"J;{GBOB};2;Bob")
    await bot.auth.wait_all()

    assert bot.clients.get_by_cid("2").ip == ""  # no IP, but the bot is fine


@pytest.mark.asyncio
async def test_auth_poll_is_cancelled_when_the_player_leaves(tmp_path):
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": STATUS}))
    bot.auth._initial_delay = 10  # long enough that the quit lands first

    await bot.feed_line(f"J;{GBOB};2;Bob")
    assert bot.auth.pending == {"2"}

    await bot.feed_line(f"Q;{GBOB};2;Bob")
    assert bot.auth.pending == set()


@pytest.mark.asyncio
async def test_no_auth_poll_without_an_rcon(tmp_path):
    """Replay mode: there is no server to poll, so nothing is scheduled."""
    bot = _bot(tmp_path)
    await bot.feed_line(f"J;{GBOB};2;Bob")
    assert bot.auth.pending == set()


# -- the commands ----------------------------------------------------------------------------


def _proc(bot) -> CommandProcessor:
    return CommandProcessor(bot.command_registry, bot)


def _admin(level_bits: int = 128) -> Client:
    return Client(guid=GADMIN, name="Admin", cid="0", id=1, group_bits=level_bits)


@pytest.mark.asyncio
async def test_map_command_changes_the_map(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)

    await _proc(bot).handle(_admin(), "!map mp_vacant")

    assert "map mp_vacant" in rcon.commands
    assert "say changing map to mp_vacant" in rcon.commands


@pytest.mark.asyncio
async def test_map_command_needs_a_map_name(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)

    await _proc(bot).handle(_admin(), "!map")

    assert not any(c.startswith("map ") for c in rcon.commands)
    assert "usage: map <name>" in rcon.commands[-1]


@pytest.mark.asyncio
async def test_maprotate_command(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    await _proc(bot).handle(_admin(), "!maprotate")
    assert "map_rotate" in rcon.commands


@pytest.mark.asyncio
async def test_maps_and_nextmap_commands(tmp_path):
    rotation = '"sv_maprotation" is: "map mp_crash map mp_vacant^7" default: ""'
    rcon = ScriptedRcon({"sv_mapRotation": rotation, "status": STATUS})
    bot = _bot(tmp_path, rcon=rcon)

    await _proc(bot).handle(_admin(), "!maps")
    assert "map rotation: mp_crash, mp_vacant" in rcon.commands[-1]

    await _proc(bot).handle(_admin(), "!nextmap")
    assert "next map: mp_vacant" in rcon.commands[-1]


@pytest.mark.asyncio
async def test_maps_command_with_no_rotation_configured(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    await _proc(bot).handle(_admin(), "!maps")
    assert "no map rotation" in rcon.commands[-1]


@pytest.mark.asyncio
async def test_status_command_reports_the_bot_and_the_match(tmp_path):
    rcon = ScriptedRcon({"status": STATUS})
    bot = _bot(tmp_path, rcon=rcon)
    await bot.feed_line(r"InitGame: \mapname\mp_crash")
    bot.sync()

    await _proc(bot).handle(_admin(), "!status")

    assert "database UP, 2 player(s) on mp_crash" in rcon.commands[-1]


@pytest.mark.asyncio
async def test_nextmap_is_open_to_ordinary_players_but_map_is_not(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    user = Client(guid=GBOB, name="Bob", cid="2", id=2, group_bits=1)  # level 1

    await _proc(bot).handle(user, "!nextmap")
    assert "sufficient access" not in rcon.commands[-1]

    await _proc(bot).handle(user, "!map mp_vacant")
    assert "sufficient access" in rcon.commands[-1]


# -- bot lifecycle ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_makes_the_bot_ignore_the_log_then_resume_by_itself(tmp_path):
    clock = FakeClock()
    bot = _bot(tmp_path, clock=clock)
    await bot.feed_line(f"J;{GADMIN};0;Admin")
    assert bot.clients.get_by_cid("0") is not None

    bot.pause(30)
    assert bot.is_paused() is True
    await bot.feed_line(f"J;{GBOB};2;Bob")
    assert bot.clients.get_by_cid("2") is None  # ignored while asleep

    # No timer thread: the pause is a deadline, so it lapses on its own.
    clock.advance(31 * 60)
    assert bot.is_paused() is False
    await bot.feed_line(f"J;{GBOB};2;Bob")
    assert bot.clients.get_by_cid("2") is not None


@pytest.mark.asyncio
async def test_pause_zero_resumes_immediately(tmp_path):
    bot = _bot(tmp_path)
    bot.pause(30)
    bot.pause(0)
    assert bot.is_paused() is False


def test_shutdown_sets_the_exit_code_the_run_loop_watches(tmp_path):
    bot = _bot(tmp_path)
    assert bot.exit_code is None

    bot.shutdown()
    assert bot.exit_code == 0

    bot.shutdown(restart=True)
    assert bot.exit_code == 221  # what a supervisor script looks for


def test_reload_config_picks_up_edited_messages(tmp_path):
    path = tmp_path / "b3.yaml"
    path.write_text(
        f"bot:\n  database: 'sqlite:///{tmp_path / 'b3.sqlite'}'\n"
        "  time_zone: UTC\nserver:\n  game: cod4\n"
        "messages:\n  iamgod_done: 'first'\n",
        encoding="utf-8",
    )
    bot = _bot(tmp_path)
    bot.config_path = path
    assert bot.messages.get("iamgod_done") == "you are now superadmin"

    path.write_text(path.read_text(encoding="utf-8").replace("first", "second"), encoding="utf-8")
    bot.reload_config()

    assert bot.messages.get("iamgod_done") == "second"


def test_reload_config_without_a_known_path_says_so(tmp_path):
    bot = _bot(tmp_path)
    with pytest.raises(RuntimeError, match="no config path"):
        bot.reload_config()


@pytest.mark.asyncio
async def test_warn_with_a_lifetime_expires_on_its_own(tmp_path):
    clock = FakeClock()
    bot = _bot(tmp_path, clock=clock)
    await bot.feed_line(f"J;{GBOB};2;Bob")
    bob = bot.clients.get_by_cid("2")

    bot.warn(bob, "language", minutes=60)
    from b3.domain.client import PenaltyType

    assert len(bot.storage.get_active_penalties(bob.id, PenaltyType.WARNING)) == 1

    clock.advance(61 * 60)
    assert bot.storage.get_active_penalties(bob.id, PenaltyType.WARNING) == []


@pytest.mark.asyncio
async def test_notice_is_recorded_without_touching_the_server(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    await bot.feed_line(f"J;{GBOB};2;Bob")
    bob = bot.clients.get_by_cid("2")

    bot.notice(bob, "helpful player")

    from b3.domain.client import PenaltyType

    notices = bot.storage.get_active_penalties(bob.id, PenaltyType.NOTICE)
    assert [n.reason for n in notices] == ["helpful player"]
    assert rcon.commands == []  # a note is between admins; the player learns nothing


@pytest.mark.asyncio
async def test_warning_escalation_end_to_end_through_the_scheduler(tmp_path):
    """The grace period is driven by the bot's own scheduler — no thread, no real sleeping."""
    from b3.domain.client import PenaltyType

    clock = FakeClock()
    rcon = ScriptedRcon()
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, rcon=rcon, clock=clock)
    bot.add_plugin(
        AdminPlugin(
            bot,
            {
                "warn_reasons": {"lang": "3h, watch your language"},
                "warn": {"delay": 0, "alert_at": 2, "grace": 25, "kick_at": 9},
            },
        ),
        "admin",
    )
    bot.start()

    await bot.replay([f"J;{GADMIN};1;Admin", "say;x;1;Admin;!iamgod", f"J;{GBOB};2;Bob"])
    bob = bot.clients.get_by_cid("2")
    await bot.feed_line("say;x;1;Admin;!warn Bob lang")
    await bot.feed_line("say;x;1;Admin;!warn Bob lang")

    assert any("ALERT" in c for c in rcon.commands)
    assert bot.storage.get_active_penalties(bob.id, PenaltyType.TEMPBAN) == []

    # Nothing happens until the grace period lapses and the scheduler runs.
    clock.advance(10)
    await bot.scheduler.tick()
    assert bot.storage.get_active_penalties(bob.id, PenaltyType.TEMPBAN) == []

    clock.advance(20)
    await bot.scheduler.tick()
    bans = bot.storage.get_active_penalties(bob.id, PenaltyType.TEMPBAN)
    assert len(bans) == 1
    assert "too many warnings" in bans[0].reason
    assert "banclient 2" in rcon.commands  # and the server was told
