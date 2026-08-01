"""The events a Frostbite server pushes, and the six Battlefield / Medal of Honor profiles.

Sample word lists come from the docstrings of the classic ``frostbite/abstractParser.py`` and
``frostbite2/abstractParser.py``, captured from real servers. Not verified against a live Battlefield
server; see TODO.md §2.1.

The identity tests matter most. On this engine a player's *name* is their handle — `admin.kickPlayer`
takes a name, not a slot — while bans are by EA GUID, which arrives in a separate event. Getting that
split wrong means either kicking nobody or banning the wrong person.
"""

from __future__ import annotations

import json

import pytest

from b3.core.events import EventType
from b3.domain.client import Client
from b3.parsers.frostbite.parser import FbParser, KillData
from b3.parsers.frostbite.profiles import BF3, BF4, BFBC2, BFH, MOH, MOHW
from b3.parsers.frostbite.status import parse_ban_list, parse_player_block
from b3.parsers.games import PROFILES, PUSH_FAMILIES, parser_for

GUID = "EA_00000000000000000000000000000001"
GUID2 = "EA_00000000000000000000000000000002"


@pytest.fixture
def parser() -> FbParser:
    return FbParser(BF3)


def feed(p: FbParser, words: list[str]):  # noqa: ANN201
    """Events reach the parser as JSON, which is how a word list survives a line-shaped contract."""
    return p.parse_line(json.dumps(words))


def one(p: FbParser, words: list[str]):  # noqa: ANN201
    events = feed(p, words)
    assert len(events) == 1, f"expected 1 event from {words!r}, got {events!r}"
    return events[0]


def _with_players(profile=BF3) -> FbParser:  # noqa: ANN001
    p = FbParser(profile)
    p.clients.add(Client(cid="Bravo17", name="Bravo17", guid=GUID, team="red"))
    p.clients.add(Client(cid="Bob", name="Bob", guid=GUID2, team="blue"))
    return p


# -- identity --------------------------------------------------------------


def test_authentication_is_the_join(parser):  # noqa: ANN001
    """Before the GUID lands there is no identity to match a ban against."""
    ev = one(parser, ["player.onAuthenticated", "Bravo17", GUID])

    assert ev.type is EventType.CLIENT_JOIN
    client = parser.clients.get_by_cid("Bravo17")
    assert client.guid == GUID
    assert client.cid == "Bravo17"  # the name is the handle: `admin.kickPlayer` takes it


def test_a_join_is_only_a_connection(parser):  # noqa: ANN001
    """The classic parser ignored this event: it arrives before the game client has really connected,
    and one that then fails to connect never produces a matching leave."""
    ev = one(parser, ["player.onJoin", "Bravo17", GUID])
    assert ev.type is EventType.CLIENT_CONNECT


def test_leaving_removes_the_player(parser):  # noqa: ANN001
    feed(parser, ["player.onAuthenticated", "Bravo17", GUID])
    ev = one(parser, ["player.onLeave", "Bravo17", "0", "name", "0"])

    assert ev.type is EventType.CLIENT_DISCONNECT
    assert parser.clients.get_by_cid("Bravo17") is None


def test_a_leave_for_somebody_unknown_is_not_invented(parser):  # noqa: ANN001
    assert feed(parser, ["player.onLeave", "Ghost"]) == []


def test_a_kick_carries_its_reason():
    p = _with_players()
    ev = one(p, ["player.onKicked", "Bob", "idle for too long"])
    assert ev.type is EventType.CLIENT_DISCONNECT
    assert ev.data == "idle for too long"
    assert p.clients.get_by_cid("Bob") is None


def test_the_server_is_never_a_player(parser):  # noqa: ANN001
    """`Server` speaks on the chat event — including the bot's own output coming back."""
    assert feed(parser, ["player.onChat", "Server", "b3: Bob was kicked", "all"]) == []
    assert parser.clients.get_by_cid("Server") is None


# -- chat ------------------------------------------------------------------


def test_public_chat():
    p = _with_players()
    ev = one(p, ["player.onChat", "Bravo17", "hello b3", "all"])
    assert ev.type is EventType.CLIENT_SAY
    assert ev.data == "hello b3"
    assert ev.client.cid == "Bravo17"


@pytest.mark.parametrize(
    "target", ["team 1", "squad 1 2", "player Bob"]
)
def test_restricted_chat_is_team_chat(target):  # noqa: ANN001
    p = _with_players()
    ev = one(p, ["player.onChat", "Bravo17", "on my six", target])
    assert ev.type is EventType.CLIENT_TEAM_SAY
    assert ev.extra["target"] == target


def test_chat_with_no_target_is_public():
    p = _with_players()
    assert one(p, ["player.onChat", "Bravo17", "hi"]).type is EventType.CLIENT_SAY


def test_chat_from_an_unseen_name_creates_the_player():
    """Unlike Quake3, the name *is* the id here, so a first sighting is a usable identity."""
    p = FbParser(BF3)
    ev = one(p, ["player.onChat", "Newcomer", "hello", "all"])
    assert ev.client.cid == "Newcomer"


def test_a_command_typed_in_chat_survives_intact():
    """The classic parser stripped the last word off a command line to undo its own mangling."""
    p = _with_players()
    ev = one(p, ["player.onChat", "Bravo17", "!ban Bob cheating in the tank", "all"])
    assert ev.data == "!ban Bob cheating in the tank"


# -- combat ----------------------------------------------------------------


def test_a_kill():
    p = _with_players()
    ev = one(p, ["player.onKill", "Bravo17", "Bob", "M67", "false"])

    assert ev.type is EventType.CLIENT_KILL
    assert ev.client.cid == "Bravo17" and ev.target.cid == "Bob"
    assert ev.data == KillData(weapon="M67", headshot=False)


def test_a_headshot_is_recorded():
    p = _with_players()
    ev = one(p, ["player.onKill", "Bravo17", "Bob", "M416", "true"])
    assert ev.data.headshot is True


def test_a_team_kill():
    p = FbParser(BF3)
    p.clients.add(Client(cid="A", name="A", guid=GUID, team="red"))
    p.clients.add(Client(cid="B", name="B", guid=GUID2, team="red"))
    assert one(p, ["player.onKill", "A", "B", "M67", "false"]).type is EventType.CLIENT_KILL_TEAM


def test_players_with_no_team_yet_are_not_team_mates():
    """Team 0 means "still at the deploy screen". Treating it as a team makes every early kill a TK."""
    p = FbParser(BF3)
    p.clients.add(Client(cid="A", name="A", guid=GUID, team=""))
    p.clients.add(Client(cid="B", name="B", guid=GUID2, team=""))
    assert one(p, ["player.onKill", "A", "B", "M67", "false"]).type is EventType.CLIENT_KILL


def test_killing_yourself():
    p = _with_players()
    assert one(p, ["player.onKill", "Bob", "Bob", "M67", "false"]).type is EventType.CLIENT_SUICIDE


def test_a_kill_with_no_killer_is_a_suicide():
    """`['', 'Bob', 'DamageArea', 'false']` — a fall, a fire, the server. Not a player called ""."""
    p = _with_players()
    ev = one(p, ["player.onKill", "", "Bob", "DamageArea", "false"])
    assert ev.type is EventType.CLIENT_SUICIDE
    assert ev.client.cid == "Bob"
    assert p.clients.get_by_cid("") is None


def test_a_kill_by_the_server_is_a_suicide_too():
    p = _with_players()
    ev = one(p, ["player.onKill", "Server", "Bob", "Death", "false"])
    assert ev.type is EventType.CLIENT_SUICIDE


# -- teams and spawning ----------------------------------------------------


def test_a_team_change():
    p = _with_players()
    ev = one(p, ["player.onTeamChange", "Bob", "1", "0"])
    assert ev.type is EventType.CLIENT_TEAM_CHANGE
    assert p.clients.get_by_cid("Bob").team == "red"


def test_a_squad_change_also_states_the_team():
    p = _with_players()
    one(p, ["player.onSquadChange", "Bob", "2", "3"])
    assert p.clients.get_by_cid("Bob").team == "blue"


def test_spawning_sets_the_team_on_frostbite_2():
    p = _with_players()
    ev = one(p, ["player.onSpawn", "Bob", "1"])
    assert ev.type is EventType.CLIENT_SPAWN
    assert p.clients.get_by_cid("Bob").team == "red"


def test_spawning_on_frostbite_1_reports_a_kit_not_a_team():
    """BC2 puts the kit here. Reading it as a team would relabel everyone as "assault"."""
    p = _with_players(BFBC2)
    before = p.clients.get_by_cid("Bob").team
    one(p, ["player.onSpawn", "Bob", "assault", "gadget", "pistol"])
    assert p.clients.get_by_cid("Bob").team == before


# -- the round -------------------------------------------------------------


def test_a_new_level_reads_like_a_round_start(parser):  # noqa: ANN001
    """Published with the same payload shape the other families use, so `Game` needs no special case."""
    ev = one(parser, ["server.onLevelLoaded", "MP_001", "ConquestLarge0", "1", "2"])
    assert ev.type is EventType.GAME_ROUND_START
    assert ev.data["mapname"] == "MP_001"
    assert ev.data["g_gametype"] == "ConquestLarge0"


def test_the_end_of_a_round(parser):  # noqa: ANN001
    ev = one(parser, ["server.onRoundOver", "1"])
    assert ev.type is EventType.GAME_ROUND_END


def test_a_punkbuster_message_is_passed_through_not_dropped(parser):  # noqa: ANN001
    ev = one(parser, ["punkBuster.onMessage", "PunkBuster Server: Player GUID Computed abc"])
    assert ev.type is EventType.CUSTOM
    assert ev.extra["kind"] == "punkbuster"


# -- robustness ------------------------------------------------------------


def test_an_unknown_event_is_ignored_quietly(parser):  # noqa: ANN001
    """Battlefield servers emit plenty this bot has no use for."""
    assert feed(parser, ["vars.somethingNobodyPortedYet", "true"]) == []


def test_a_truncated_event_does_not_raise(parser):  # noqa: ANN001
    assert feed(parser, ["player.onKill"]) == []
    assert feed(parser, ["player.onChat", "Bravo17"]) == []
    assert feed(parser, []) == []


def test_a_line_that_is_not_json_is_reported_not_raised(parser):  # noqa: ANN001
    assert parser.parse_line("player.onChat Bravo17 hello") == []
    assert parser.parse_line("") == []


def test_json_that_is_not_a_word_list_is_refused(parser):  # noqa: ANN001
    assert parser.parse_line('{"player.onChat": "Bravo17"}') == []
    assert parser.parse_line("[1, 2, 3]") == []


# -- the player block ------------------------------------------------------


def test_reading_a_player_block():
    players = list(
        parse_player_block(
            ["3", "name", "guid", "score", "2", "Bravo17", GUID, "1500", "Bob", GUID2, "20"]
        )
    )
    assert [(p.name, p.guid, p.score) for p in players] == [
        ("Bravo17", GUID, 1500),
        ("Bob", GUID2, 20),
    ]


def test_an_empty_player_block():
    assert list(parse_player_block(["3", "name", "guid", "score", "0"])) == []
    assert list(parse_player_block([])) == []


def test_a_block_cut_short_yields_what_arrived():
    """It says two players and delivers one and a half; the first must still come through."""
    players = list(
        parse_player_block(["2", "name", "guid", "2", "Bravo17", GUID, "Bob"])
    )
    assert [p.name for p in players] == ["Bravo17"]


def test_a_malformed_header_is_survivable():
    assert list(parse_player_block(["notanumber", "name"])) == []


def test_a_row_with_no_name_identifies_nobody():
    assert list(parse_player_block(["2", "name", "guid", "1", "", GUID])) == []


def test_no_ip_is_reported_and_none_is_invented():
    """Frostbite does not tell an admin tool where a player connects from. PunkBuster does."""
    (player,) = parse_player_block(["2", "name", "guid", "1", "Bravo17", GUID])
    assert player.ip == ""


def test_reading_a_ban_list():
    bans = parse_ban_list(["guid", GUID, "perm", "0", "cheating", "ip", "1.2.3.4", "rounds", "5", "x"])
    assert bans[0] == {
        "id_type": "guid",
        "target": GUID,
        "ban_type": "perm",
        "amount": "0",
        "reason": "cheating",
    }
    assert bans[1]["id_type"] == "ip"


def test_a_ban_list_that_does_not_divide_evenly_is_refused():
    """Misaligning it would attribute every ban after the fault to the wrong player."""
    assert parse_ban_list(["guid", GUID, "perm"]) == []


# -- profiles and wiring ---------------------------------------------------


@pytest.mark.parametrize("game", ["bf3", "bf4", "bfbc2", "bfh", "moh", "mohw"])
def test_every_title_is_configurable(game):  # noqa: ANN001
    profile = PROFILES[game]
    assert profile.family == "frostbite"
    assert isinstance(parser_for(profile), FbParser)
    assert profile.family in PUSH_FAMILIES  # so the CLI builds one object for both halves


def test_the_titles_differ_in_nothing_the_bot_cares_about():
    """Both protocol generations take the same verbs; what differs is data read by name.

    `map_names` is excluded because it is per-title data by definition.
    """
    from dataclasses import replace

    for profile in (BF4, BFBC2, BFH, MOH, MOHW):
        assert replace(profile, name="bf3", map_names=BF3.map_names) == BF3


def test_bans_are_native_and_by_guid():
    """So they hold across a name change, and across the bot not running."""
    assert "banList.add guid" in BF3.ban_template
    assert "seconds" in BF3.tempban_template
    assert BF3.unban_template == 'banList.remove guid "%(guid)s"'


def test_the_reason_cap_is_the_engines_not_ours():
    """Frostbite *rejects* a command whose reason runs past 80 characters — no ban at all."""
    assert BF3.max_reason_length == 80


def test_the_templates_quote_every_value():
    """This engine takes word lists, so an unquoted reason with a space would become two words."""
    for template in (BF3.tell_template, BF3.kick_template, BF3.ban_template, BF3.tempban_template):
        assert template.count('"') >= 2
