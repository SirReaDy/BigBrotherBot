"""The Quake3 engine family: one parser, thirteen titles.

The structural difference from CoD is worth stating: a Quake3 log gives a player's identity **once**,
in an infostring, and every later line refers to a bare slot number. So most of what matters here is
whether the parser remembers.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clients import ClientManager
from b3.core.clock import FakeClock
from b3.core.events import EventType
from b3.parsers.games import PROFILES, parser_for
from b3.parsers.q3.parser import Q3Parser, parse_infostring
from b3.parsers.q3.profiles import IOURT42, Q3
from b3.parsers.q3.status import Q3_PLAYER_LINE_RE
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot

GUID = "A337702493AF67BB0B0F8565CE8BC6C1"


def q3(profile=Q3):  # noqa: ANN001, ANN202
    return Q3Parser(profile, ClientManager())


def one(p, line):  # noqa: ANN001, ANN202
    events = p.parse_line(line)
    assert len(events) == 1, f"expected 1 event from {line!r}, got {events!r}"
    return events[0]


# -- infostrings ------------------------------------------------------------------------------


def test_an_infostring_becomes_a_dict():
    info = parse_infostring(r"\name\Bob\t\2\cl_guid\ABC123")
    assert info == {"name": "Bob", "t": "2", "cl_guid": "ABC123"}


def test_a_missing_leading_backslash_is_tolerated():
    assert parse_infostring(r"n\Bob\t\2")["n"] == "Bob"


def test_an_empty_value_does_not_swallow_the_next_key():
    info = parse_infostring(r"\name\Bob\model\\rate\25000")
    assert info["model"] == ""
    assert info["rate"] == "25000"


# -- identity ---------------------------------------------------------------------------------


def test_userinfo_is_where_a_player_gets_a_name_and_a_guid():
    p = q3()
    p.parse_line(rf"ClientUserinfoChanged: 3 \n\Bob\t\2\cl_guid\{GUID}")

    client = p.clients.get_by_cid("3")
    assert client.name == "Bob"
    assert client.guid == GUID
    assert client.team == "blue"  # team 2


def test_short_keys_are_understood_too():
    """ClientUserinfoChanged uses `n` and `t`; ClientUserinfo uses `name` and `team`."""
    p = q3()
    p.parse_line(r"ClientUserinfo: 3 \name\Alice\team\1")
    assert p.clients.get_by_cid("3").name == "Alice"
    assert p.clients.get_by_cid("3").team == "red"


def test_et_sends_the_infostring_on_its_own_line():
    """ClientConnect, then a bare Userinfo: — the slot has to be carried between the two."""
    p = q3()
    p.parse_line("ClientConnect: 4")
    p.parse_line(rf"Userinfo: \name\Thorn\cl_guid\{GUID}")

    client = p.clients.get_by_cid("4")
    assert client.name == "Thorn"
    assert client.guid == GUID


def test_a_bare_userinfo_with_no_pending_slot_is_ignored():
    p = q3()
    assert p.parse_line(r"Userinfo: \name\Nobody") == []


def test_a_guid_too_short_for_the_title_is_refused_once(caplog):
    """Urban Terror ids are 32 characters; a shorter one is spoofable."""
    p = q3(IOURT42)
    with caplog.at_level("WARNING"):
        p.parse_line(r"ClientUserinfoChanged: 3 \n\Bob\cl_guid\abc")
        p.parse_line(r"ClientUserinfoChanged: 4 \n\Sue\cl_guid\def")

    assert p.clients.get_by_cid("3").guid == ""
    assert caplog.text.count("nobody will be authenticated") == 1


def test_joining_and_leaving():
    p = q3()
    assert one(p, "ClientConnect: 2").type is EventType.CLIENT_CONNECT
    assert one(p, "ClientBegin: 2").type is EventType.CLIENT_JOIN

    event = one(p, "ClientDisconnect: 2")
    assert event.type is EventType.CLIENT_DISCONNECT
    assert p.clients.get_by_cid("2") is None


def test_disconnecting_an_unknown_slot_is_quiet():
    assert q3().parse_line("ClientDisconnect: 9") == []


# -- combat -----------------------------------------------------------------------------------


def _two_players(p):  # noqa: ANN001, ANN202
    p.parse_line(r"ClientUserinfoChanged: 1 \n\Killer\t\1")
    p.parse_line(r"ClientUserinfoChanged: 2 \n\Victim\t\2")


def test_a_kill_is_read_from_the_slots_not_the_names():
    """The names in a kill line carry colour codes and can be duplicated; the slots cannot."""
    p = q3()
    _two_players(p)

    event = one(p, "Kill: 1 2 10: ^1Killer killed ^2Victim by MOD_ROCKET")

    assert event.type is EventType.CLIENT_KILL
    assert event.client.name == "Killer"
    assert event.target.name == "Victim"
    assert event.data.means_of_death == "MOD_ROCKET"


def test_killing_a_teammate():
    p = q3()
    p.parse_line(r"ClientUserinfoChanged: 1 \n\Killer\t\1")
    p.parse_line(r"ClientUserinfoChanged: 2 \n\Mate\t\1")  # same team

    assert one(p, "Kill: 1 2 10: Killer killed Mate by MOD_ROCKET").type is (
        EventType.CLIENT_KILL_TEAM
    )


def test_the_world_killing_you_is_a_suicide():
    p = q3()
    _two_players(p)
    event = one(p, "Kill: 1022 2 19: <world> killed Victim by MOD_FALLING")
    assert event.type is EventType.CLIENT_SUICIDE
    assert event.client.cid == "2"  # the victim, not the world


def test_a_self_inflicted_death_is_a_suicide_whoever_the_engine_blames():
    p = q3()
    _two_players(p)
    assert one(p, "Kill: 2 2 7: Victim killed Victim by MOD_SUICIDE").type is (
        EventType.CLIENT_SUICIDE
    )


# -- chat -------------------------------------------------------------------------------------


def test_chat_is_matched_to_the_player_by_name():
    """Quake3 chat lines carry a name, not a slot — the bot has to have seen the infostring."""
    p = q3()
    _two_players(p)

    event = one(p, "say: Killer: nice shot")
    assert event.type is EventType.CLIENT_SAY
    assert event.client.cid == "1"
    assert event.data == "nice shot"


def test_team_chat():
    p = q3()
    _two_players(p)
    assert one(p, "sayteam: Victim: enemy at base").type is EventType.CLIENT_TEAM_SAY


def test_chat_from_a_name_we_have_never_seen_is_dropped():
    """Better nothing than an event about a client with no identity."""
    assert q3().parse_line("say: Ghost: hello") == []


def test_a_private_message_names_both_ends():
    p = q3()
    _two_players(p)
    event = one(p, "tell: Killer to Victim: behind you")
    assert event.type is EventType.CLIENT_PRIVATE_SAY
    assert event.client.cid == "1"
    assert event.target.cid == "2"


# -- the world ---------------------------------------------------------------------------------


def test_round_boundaries():
    p = q3()
    start = one(p, r"InitGame: \sv_maxclients\16\mapname\q3dm17\g_gametype\0")
    assert start.type is EventType.GAME_ROUND_START
    assert start.data["mapname"] == "q3dm17"

    assert one(p, "Exit: Timelimit hit.").type is EventType.GAME_EXIT
    assert one(p, "ShutdownGame:").type is EventType.GAME_ROUND_END


def test_items_and_awards():
    p = q3()
    _two_players(p)
    assert one(p, "Item: 1 weapon_rocketlauncher").data == "weapon_rocketlauncher"
    assert one(p, "Award: 1 2").type is EventType.CLIENT_ACTION


def test_an_unknown_line_is_ignored():
    assert q3().parse_line("something the engine invented: 42") == []


# -- the status table ----------------------------------------------------------------------------


def test_the_status_table_parses_with_and_without_a_guid_column():
    with_guid = "  0    12   47 abc123def456 Bob      50 192.0.2.44:27960 12345 25000"
    without = "  1    -3   61 Alice      40 198.51.100.9:27961 6789 25000"

    first = Q3_PLAYER_LINE_RE.match(with_guid)
    second = Q3_PLAYER_LINE_RE.match(without)

    assert first["name"] == "Bob"
    assert first["guid"] == "abc123def456"
    assert second["name"] == "Alice"
    assert second["guid"] is None
    assert second["score"] == "-3"


# -- wiring ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "game",
    [
        "q3",
        "oa081",
        "sof2",
        "sof2pm",
        "smg",
        "smg11",
        "wop",
        "wop15",
        "et",
        "etpro",
        "iourt41",
        "iourt42",
        "iourt43",
    ],
)
def test_every_quake3_title_is_configurable(game):
    # Urban Terror, ET, SoF2 and World of Padman 1.5 have extra lines of their own, so each names a
    # *subclass* family; the `isinstance` below is the part that matters — every one of these reads
    # the shared grammar.
    assert PROFILES[game].family in ("q3", "urt", "et", "sof2", "wop15")
    assert isinstance(parser_for(PROFILES[game]), Q3Parser)


def test_the_two_families_get_different_parsers():
    from b3.parsers.cod.parser import CodParser

    assert isinstance(parser_for(PROFILES["cod4x"]), CodParser)
    assert isinstance(parser_for(PROFILES["iourt42"]), Q3Parser)


@pytest.mark.asyncio
async def test_a_bot_runs_a_quake3_server_end_to_end(tmp_path):
    """The rest of the bot does not care which engine it is: same events, same admin plugin."""
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="iourt42"),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()

    await bot.replay(
        [
            r"InitGame: \mapname\ut4_casa\g_gametype\4",
            rf"ClientUserinfoChanged: 0 \n\Admin\t\1\cl_guid\{GUID}",
            "ClientBegin: 0",
            "say: Admin: !iamgod",
        ]
    )

    assert bot.game.map_name == "ut4_casa"
    assert bot.storage.has_superadmin() is True  # a command ran, through the same admin plugin


def test_a_server_with_no_guid_column_yields_an_empty_id_not_none():
    """A plain Quake3 server has no persistent id at all. None here would crash sync and auth."""
    from b3.parsers.status import parse_status

    text = "map: q3dm17\n  1    -3   61 Alice      40 198.51.100.9:27961 6789 25000\n"
    _map, players = parse_status(text, Q3.status_patterns, Q3.identity_field)

    assert players[0].guid == ""
    assert players[0].ip == "198.51.100.9"


@pytest.mark.asyncio
async def test_sync_survives_a_guidless_server(tmp_path):
    """The whole reconciliation path runs over players the engine cannot identify."""

    class Rcon:
        def command(self, cmd):  # noqa: ANN001, ANN202
            return "map: q3dm17\n  1   -3   61 Alice   40 198.51.100.9:27961 6789 25000\n"

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="q3"),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, rcon=Rcon(), clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()

    bot.sync()

    assert [c.name for c in bot.clients.connected()] == ["Alice"]
