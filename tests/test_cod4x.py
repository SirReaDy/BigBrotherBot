"""CoD4X 1.8 — the mod most live CoD4 servers run.

The differences from stock CoD4 are entirely `GameProfile` data, which is the claim the profile
split was built on; these tests are what hold that claim up. Spec transplanted from
`leiizko/b3_cod4x`.

The identity test is the important one: CoD4X reports both a per-session guid and a Steam64 id,
and keying on the wrong one would create a fresh player record — losing their level and their bans
— every time somebody reconnected.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.commands import CommandProcessor
from b3.domain.client import Client, PenaltyType
from b3.parsers.cod import status as sp
from b3.parsers.cod.profiles import COD4, COD4X
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot

STEAM_ADMIN = "76561198000000001"
STEAM_BOB = "76561198000000002"

# CoD4X with `sv_usesteam64id 1`: an extra Steam64 column, and no `lastmsg`.
COD4X_STATUS = f"""map: mp_crash
num score ping guid              steamid           name            address
--- ----- ---- ----------------- ----------------- --------------- --------------------
  0    12   47 1234567           {STEAM_ADMIN} Admin           192.0.2.44:28960
  2    -1  102 7654321           {STEAM_BOB} Bob the Builder 198.51.100.88:28961
"""


class ScriptedRcon:
    def __init__(self, replies=None):  # noqa: ANN001
        self.replies = replies or {}
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return self.replies.get(cmd, "")


def _bot(tmp_path, game="cod4x", rcon=None, clock=None, forget_startup=False):  # noqa: ANN001, ANN202
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game=game),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, rcon=rcon, clock=clock or FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    if forget_startup and rcon is not None:
        rcon.commands.clear()
    return bot


# -- the status table ----------------------------------------------------------------------


def test_the_steam64_id_is_the_identity_not_the_session_guid():
    _map, players = sp.parse_status(COD4X_STATUS, COD4X.status_patterns, COD4X.identity_field)

    admin = players[0]
    assert admin.guid == STEAM_ADMIN  # what we store and match a returning player on
    assert admin.steam_id == STEAM_ADMIN
    assert admin.cid == "0"
    assert admin.ip == "192.0.2.44"
    assert admin.ping == 47


def test_names_with_spaces_and_negative_scores_still_parse():
    _map, players = sp.parse_status(COD4X_STATUS, COD4X.status_patterns, COD4X.identity_field)
    bob = players[1]
    assert bob.name == "Bob the Builder"
    assert bob.score == -1
    assert bob.guid == STEAM_BOB


def test_a_player_with_no_steam_id_falls_back_to_the_guid():
    """CoD4X reports 0 until a client is identified; every such player must not become one row."""
    text = "map: mp_crash\n  3    0   50 999888 0 Nobody 203.0.113.9:28960\n"
    _map, players = sp.parse_status(text, COD4X.status_patterns, COD4X.identity_field)

    assert players[0].guid == "999888"
    assert players[0].steam_id == ""  # and we do not claim a Steam id it never gave us


def test_the_optional_lastmsg_column_is_tolerated():
    """CoD4X builds disagree about this column; one extra field should not lose the player."""
    text = f"map: mp_crash\n  0 12 47 1234567 {STEAM_ADMIN} Admin 50 192.0.2.44:28960 12345 25000\n"
    _map, players = sp.parse_status(text, COD4X.status_patterns, COD4X.identity_field)

    assert len(players) == 1
    assert players[0].name == "Admin"
    assert players[0].guid == STEAM_ADMIN
    assert players[0].ip == "192.0.2.44"


def test_a_table_with_no_id_columns_still_yields_its_players(tmp_path):
    """`b3hide` strips the id columns; the profile's fallback shape catches that server.

    The point of a *tuple* of patterns: an empty player list from a server with 20 people on it is
    the worst possible failure, because nothing gets authenticated and no penalty can be applied,
    and the symptom ("the bot ignores everyone") points nowhere near the status table.
    """
    hidden = (
        "map: mp_crash\n"
        "num score ping name            address\n"
        "--- ----- ---- --------------- --------------------\n"
        "  0    12   47 Admin           192.0.2.44:28960\n"
        "  2    -1  102 Bob the Builder 198.51.100.88:28961\n"
    )
    _map, players = sp.parse_status(hidden, COD4X.status_patterns, COD4X.identity_field)

    assert [p.cid for p in players] == ["0", "2"]
    assert [p.name for p in players] == ["Admin", "Bob the Builder"]
    assert [p.ip for p in players] == ["192.0.2.44", "198.51.100.88"]
    assert players[0].guid == ""  # no id column to read: exactly like a guidless Quake3 server


def test_the_strict_shape_wins_while_it_still_matches():
    """Ordering is the whole contract: the loose fallback must not touch a normal server."""
    _map, players = sp.parse_status(COD4X_STATUS, COD4X.status_patterns, COD4X.identity_field)

    assert [p.guid for p in players] == [STEAM_ADMIN, STEAM_BOB]  # not "" from the fallback


def test_the_stock_table_is_unaffected():
    stock = (
        "map: mp_crash\n"
        "  0    12   47 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa Admin  50 192.0.2.44:28960 1 25000\n"
    )
    _map, players = sp.parse_status(stock, COD4.status_patterns, COD4.identity_field)
    assert players[0].guid == "a" * 32
    assert players[0].steam_id == ""


# -- the profile ---------------------------------------------------------------------------


def test_the_profile_declares_the_cod4x_verbs():
    assert COD4X.ban_template.startswith("permban")
    assert COD4X.tempban_template == "tempban %(cid)s %(minutes)sm %(reason)s"
    assert COD4X.unban_template == "unban %(guid)s"
    assert COD4X.startup_commands == ("g_logsync 3", "sv_usesteam64id 1")
    assert COD4X.guid_min_length == 17  # a Steam64 id; 32 would reject every CoD4X player


def test_a_bot_can_be_configured_for_cod4x(tmp_path):
    assert _bot(tmp_path).profile.name == "cod4x"


# -- the RCON verbs ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_steam64_reporting_is_switched_on_at_startup(tmp_path):
    rcon = ScriptedRcon()
    _bot(tmp_path, rcon=rcon)
    assert "sv_usesteam64id 1" in rcon.commands


@pytest.mark.asyncio
async def test_stock_cod4_asks_for_unbuffered_logging_but_not_steam_ids(tmp_path):
    """Every CoD server needs g_logsync or the bot reads a buffered log; only CoD4X has Steam ids."""
    rcon = ScriptedRcon()
    _bot(tmp_path, game="cod4", rcon=rcon)
    assert rcon.commands == ["g_logsync 3"]


@pytest.mark.asyncio
async def test_a_native_tempban_carries_the_duration_and_reason(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    bob = Client(guid=STEAM_BOB, name="Bob", cid="2")
    bot.clients.add(bob)

    bot.tempban(bob, 120, reason="aimbot")

    assert "tempban 2 120m aimbot" in rcon.commands


@pytest.mark.asyncio
async def test_a_tempban_longer_than_the_engine_allows_is_capped_but_recorded_in_full(tmp_path):
    """CoD4X refuses more than 30 days. The bot still knows about the longer ban and enforces it."""
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    bob = Client(guid=STEAM_BOB, name="Bob", cid="2")
    bot.clients.add(bob)
    bot.storage.save_client(bob)

    bot.tempban(bob, 60 * 24 * 60, reason="cheating")  # 60 days

    assert "tempban 2 43200m cheating" in rcon.commands  # engine gets its maximum
    penalty = bot.storage.get_active_penalties(bob.id, PenaltyType.TEMPBAN)[0]
    assert penalty.duration == 60 * 24 * 60  # ...and we keep the real one


@pytest.mark.asyncio
async def test_stock_cod4_falls_back_to_banclient_for_a_tempban(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, game="cod4", rcon=rcon, forget_startup=True)
    bob = Client(guid="b" * 32, name="Bob", cid="2")
    bot.clients.add(bob)

    bot.tempban(bob, 120, reason="aimbot")

    assert rcon.commands == ["banclient 2"]


@pytest.mark.asyncio
async def test_ban_and_kick_pass_the_reason_to_the_engine(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    bob = Client(guid=STEAM_BOB, name="Bob", cid="2")
    bot.clients.add(bob)

    bot.kick(bob, reason="spawn camping")
    bot.ban(bob, reason="wallhack")

    assert "kick 2 spawn camping" in rcon.commands
    assert "permban 2 wallhack" in rcon.commands


@pytest.mark.asyncio
async def test_a_penalty_with_no_reason_still_produces_a_valid_command(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    bob = Client(guid=STEAM_BOB, name="Bob", cid="2")
    bot.clients.add(bob)

    bot.kick(bob)

    assert "kick 2 no reason given" in rcon.commands


@pytest.mark.asyncio
async def test_unban_uses_the_guid_so_it_reaches_someone_who_has_left(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, rcon=rcon)
    gone = bot.storage.save_client(Client(guid=STEAM_BOB, name="Bob"))  # no cid: not connected

    bot.unban(gone, reason="appealed")

    assert f"unban {STEAM_BOB}" in rcon.commands


# -- identity end to end ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_returning_player_is_the_same_person(tmp_path):
    """The whole point of keying on Steam64: one record, and the ban follows them back."""
    rcon = ScriptedRcon({"status": COD4X_STATUS})
    bot = _bot(tmp_path, rcon=rcon)

    await bot.replay([f"J;{STEAM_ADMIN};0;Admin", "say;x;0;Admin;!iamgod"])
    await bot.feed_line(f"J;{STEAM_BOB};2;Bob")
    proc = CommandProcessor(bot.command_registry, bot)
    await proc.handle(bot.clients.get_by_cid("0"), "!permban Bob cheating")
    await bot.bus.drain()

    # Bob comes back in a different slot after a reconnect.
    bot.clients.remove("2")
    await bot.feed_line(f"J;{STEAM_BOB};5;Bob")

    assert bot.storage.count_clients() == 2  # not three: he is the same person
    assert bot.clients.get_by_cid("5").rejected is True
    assert "permban 5" in " ".join(rcon.commands)  # re-banned in his new slot


@pytest.mark.asyncio
async def test_the_auth_poll_matches_on_the_steam_id(tmp_path):
    """The status table's `guid` column is not what the join line carried — `steam` is."""
    rcon = ScriptedRcon({"status": COD4X_STATUS})
    bot = _bot(tmp_path, rcon=rcon)
    bot.auth._initial_delay = 0
    bot.auth._retry_delay = 0

    await bot.feed_line(f"J;{STEAM_BOB};2;Bob")
    await bot.auth.wait_all()

    assert bot.clients.get_by_cid("2").ip == "198.51.100.88"


@pytest.mark.asyncio
async def test_sync_adopts_cod4x_players_under_their_steam_ids(tmp_path):
    bot = _bot(tmp_path, rcon=ScriptedRcon({"status": COD4X_STATUS}))

    bot.sync()

    assert sorted(c.guid for c in bot.clients.connected()) == sorted([STEAM_ADMIN, STEAM_BOB])


# -- diagnosing a mis-configured server ---------------------------------------------------------


def test_a_guid_of_the_wrong_shape_says_so_once(caplog):
    """Configure cod4x against a server that is not reporting Steam64 ids and nobody authenticates.

    Silently. Every symptom of that points somewhere else, so the parser complains — once.
    """
    from b3.core.clients import ClientManager
    from b3.parsers.cod.parser import CodParser

    parser = CodParser(COD4X, ClientManager())
    with caplog.at_level("WARNING"):
        parser.parse_line("J;1234567;0;Admin")
        parser.parse_line("J;7654321;1;Bob")

    assert parser.clients.get_by_cid("0").guid == ""  # rejected, as it must be
    assert caplog.text.count("Nobody will be authenticated") == 1  # said once, not per join
    assert "sv_usesteam64id" in caplog.text


def test_a_valid_steam_id_passes_without_complaint(caplog):
    from b3.core.clients import ClientManager
    from b3.parsers.cod.parser import CodParser

    parser = CodParser(COD4X, ClientManager())
    with caplog.at_level("WARNING"):
        parser.parse_line(f"J;{STEAM_ADMIN};0;Admin")

    assert parser.clients.get_by_cid("0").guid == STEAM_ADMIN
    assert caplog.text == ""


# -- the rest of the CoD family ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("game", "guid_len"),
    [("cod2", 6), ("cod4", 32), ("cod4gr", 10), ("cod4x", 17), ("cod5", 8), ("cod6", 16),
     ("cod7", 5), ("cod8", 16)],
)
def test_every_cod_title_is_configurable_and_keeps_its_guid_length(tmp_path, game, guid_len):
    """One parser, eight titles: the differences are data, which is the claim to hold up."""
    bot = _bot(tmp_path, game=game)
    assert bot.profile.name == game
    assert bot.profile.guid_min_length == guid_len


@pytest.mark.parametrize("game", ["cod2", "cod4", "cod4gr", "cod4x", "cod5", "cod6", "cod7", "cod8"])
@pytest.mark.asyncio
async def test_every_title_forces_unbuffered_logging(tmp_path, game):
    """Without g_logsync the engine buffers its log and the bot reacts late, or not at all."""
    rcon = ScriptedRcon()
    _bot(tmp_path, game=game, rcon=rcon)
    assert any(c.startswith("g_logsync") for c in rcon.commands)


@pytest.mark.parametrize("game", ["cod2", "cod4", "cod5", "cod6", "cod7", "cod8"])
@pytest.mark.asyncio
async def test_every_title_parses_the_shared_log_grammar(tmp_path, game):
    """The CoD log format is the same across the family — only identity and verbs differ."""
    bot = _bot(tmp_path, game=game)
    guid = "a" * 32  # long enough for every profile's minimum

    await bot.replay([r"InitGame: \mapname\mp_crash", f"J;{guid};3;Player", "say;x;3;Player;hi"])

    assert bot.game.map_name == "mp_crash"
    assert bot.clients.get_by_cid("3").name == "Player"


@pytest.mark.asyncio
async def test_modern_warfare_titles_ban_by_kicking(tmp_path):
    """cod6/cod8 accept `banclient` and ignore it, so the ban is a kick the bot re-applies."""
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, game="cod6", rcon=rcon, forget_startup=True)
    bob = Client(guid="b" * 16, name="Bob", cid="2")
    bot.clients.add(bob)

    bot.ban(bob, reason="cheating")

    assert rcon.commands == ["clientkick 2"]


def test_the_gameranger_status_table_yields_the_account_id():
    from b3.parsers.cod.profiles import COD4GR

    text = (
        "map: mp_crash\n"
        "  0    12   47 GameRanger-Account-ID_1234567890 Bob   50 192.0.2.44:28960 12345 25000\n"
    )
    _map, players = sp.parse_status(text, COD4GR.status_patterns, COD4GR.identity_field)

    assert players[0].guid == "1234567890"
    assert players[0].name == "Bob"


def test_the_modern_warfare_status_table_stops_at_the_port():
    from b3.parsers.cod.profiles import COD6

    text = "map: mp_rust\n  4    30   61 abc123def4567890 Sniper   40 203.0.113.7:28960\n"
    _map, players = sp.parse_status(text, COD6.status_patterns, COD6.identity_field)

    assert players[0].guid == "abc123def4567890"
    assert players[0].ip == "203.0.113.7"


# -- the Black Ops RCON framing -----------------------------------------------------------------


def test_black_ops_frames_its_rcon_differently():
    """Not encrypted — a claim this project carried in a comment until it was checked."""
    from b3.net.rcon import Cod7Dialect

    dialect = Cod7Dialect()

    sent = dialect.encode_command("secret", "status", "latin-1")
    assert sent == b"\xff\xff\xff\xff\x00secret status\x00"  # NUL where others write "rcon"
    assert dialect.strip_reply(b"\xff\xff\xff\xff\x01print\nmap: mp_crash", "latin-1") == (
        "map: mp_crash"
    )


def test_the_cod7_profile_asks_for_that_dialect():
    from b3.parsers.cod.profiles import COD4, COD7

    assert COD7.rcon_dialect == "cod7"
    assert COD4.rcon_dialect == "quake3"  # every other title


def test_an_unknown_dialect_falls_back_loudly(caplog):
    from b3.net.rcon import Quake3Dialect, dialect_for

    with caplog.at_level("WARNING"):
        assert isinstance(dialect_for("nonsense"), Quake3Dialect)
    assert "unknown rcon dialect" in caplog.text
