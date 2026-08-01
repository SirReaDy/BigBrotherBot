"""Ravaged's game log — every case below is a **captured** line, not an invented one.

Provenance: `tests/core/parsers/test_ravaged.py` in the classic tree (717 lines), which recorded real
lines from a real server. That is what makes this family worth more than its player count: it is the
first one here built from measured data rather than from a protocol description, and four of the
quirks in that data would defeat a reasonable-looking pattern.

* `disconnected` has **no space** before it.
* The connect line carries an **empty name** — the server does not know it yet.
* The weapon on a kill line is **sometimes quoted and sometimes not**.
* A kill can name **no victim at all** (`killed  with UTDmgType_VehicleCollision`, two spaces), which
  is the world killing somebody rather than a player called "".
"""

from __future__ import annotations

import pytest

from b3.core.events import EventType
from b3.parsers.ravaged.parser import RavParser
from b3.parsers.ravaged.profiles import RAVAGED

COURGETTE = "12312312312312312"


def _parser() -> RavParser:
    return RavParser(RAVAGED)


def _joined(parser: RavParser, guid: str = COURGETTE, name: str = "courgette", team: str = "1"):
    parser.parse_line(f'"<{guid}><>" connected, address "192.168.0.1"')
    parser.parse_line(f'"{name}<{guid}><{team}>" entered the game')
    client = parser.clients.get_by_cid(guid)
    assert client is not None
    return client


# -- identity -----------------------------------------------------------------------------------


def test_the_connect_line_has_no_name_yet_and_that_is_not_a_bug():
    """Captured: `"<12312312312312312><>" connected, address "192.168.0.1"`.

    Both the name and the team are empty. A pattern using `.+?` for the name — which every other
    line here wants — silently misses this one, and with it the only line carrying the IP address.
    """
    parser = _parser()
    events = parser.parse_line(f'"<{COURGETTE}><>" connected, address "192.168.0.1"')

    assert [e.type for e in events] == [EventType.CLIENT_CONNECT]
    client = events[0].client
    assert client is not None
    assert (client.cid, client.guid, client.name, client.ip) == (
        COURGETTE,
        COURGETTE,
        "",
        "192.168.0.1",
    )


def test_the_empty_team_on_a_connect_line_does_not_overwrite_a_known_one():
    """`<>` means "not said", not "no team" — so a reconnect must not blank what we know."""
    parser = _parser()
    client = _joined(parser, team="0")
    assert client.team == "red"

    parser.parse_line(f'"<{COURGETTE}><>" connected, address "192.168.0.1"')
    assert client.team == "red"


def test_entering_the_game_is_the_join_and_it_carries_the_name():
    parser = _parser()
    parser.parse_line(f'"<{COURGETTE}><>" connected, address "192.168.0.1"')
    events = parser.parse_line(f'"courgette<{COURGETTE}><0>" entered the game')

    assert [e.type for e in events] == [EventType.CLIENT_JOIN]
    client = events[0].client
    assert client is not None
    assert (client.name, client.team) == ("courgette", "red")
    assert not client.authed, "the runtime has to authenticate them against the database"


def test_the_cid_is_the_steam_id_because_that_is_what_the_commands_take():
    parser = _parser()
    client = _joined(parser)
    assert client.cid == client.guid == COURGETTE


def test_a_name_with_a_space_in_it_survives():
    """Captured: `"Killer Badger<70000000000000005><0>"`. Names contain spaces on this engine."""
    parser = _parser()
    client = _joined(parser, guid="70000000000000005", name="Killer Badger", team="0")
    assert client.name == "Killer Badger"


def test_joining_a_team_reads_the_team_from_the_message_not_the_identity():
    """Captured: `"courgette<…><1>" joined team "1"` — and the triple holds the *old* team, so a
    parser reading the team out of it would report the change it already had."""
    parser = _parser()
    _joined(parser, team="0")
    events = parser.parse_line(f'"courgette<{COURGETTE}><0>" joined team "1"')

    assert [e.type for e in events] == [EventType.CLIENT_TEAM_CHANGE]
    assert events[0].data == "blue"
    client = events[0].client
    assert client is not None and client.team == "blue"


def test_disconnected_has_no_space_before_it():
    """Captured verbatim: `"courgette<12312312312312312><0>"disconnected`.

    The single most likely line in this family to be missed, and missing it means the roster grows
    forever — every player who ever joined staying "connected" until the bot restarts.
    """
    parser = _parser()
    _joined(parser, team="0")
    events = parser.parse_line(f'"courgette<{COURGETTE}><0>"disconnected')

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT]
    assert parser.clients.get_by_cid(COURGETTE) is None


def test_a_disconnect_for_somebody_never_seen_is_not_an_event():
    parser = _parser()
    assert parser.parse_line(f'"ghost<{COURGETTE}><0>"disconnected') == []


# -- chat ---------------------------------------------------------------------------------------


def test_chat_arrives_wrapped_in_an_html_colour_tag_which_is_stripped():
    """Captured: `say "<FONT COLOR='#FF0000'> hi"`. A plugin matching `!help` cannot be expected to
    know that this engine dresses chat in HTML."""
    parser = _parser()
    _joined(parser)
    events = parser.parse_line(f'"courgette<{COURGETTE}><1>" say "<FONT COLOR=\'#FF0000\'> hi"')

    assert [e.type for e in events] == [EventType.CLIENT_SAY]
    assert events[0].data == "hi"


def test_team_chat_is_prefixed_as_well_as_coloured():
    """Captured: `say_team "(Team) <FONT COLOR='#66CCFF'> hi team"` — two layers of decoration."""
    parser = _parser()
    _joined(parser)
    events = parser.parse_line(
        f'"courgette<{COURGETTE}><1>" say_team "(Team) <FONT COLOR=\'#66CCFF\'> hi team"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_TEAM_SAY]
    assert events[0].data == "hi team"


def test_the_server_talking_is_not_chat():
    """So nothing routes the bot's own announcements back into the command processor."""
    parser = _parser()
    events = parser.parse_line("Server say \"<FONT COLOR='#F2C880'> welcome\"")

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "server_say"
    assert events[0].data == "welcome"
    assert events[0].client is None


# -- combat -------------------------------------------------------------------------------------


def test_a_kill_with_a_quoted_weapon():
    """Captured: `"Name1<11111111111111><0>" killed "Name2<2222222222222><1>" with "the_weapon"`.

    Note both guids are shorter than a Steam id in the captured data — the classic test used made-up
    ids. Nothing here requires them to be 17 digits; only the profile's `guid_min_length` does, and
    that is the runtime's business rather than the grammar's.
    """
    parser = _parser()
    events = parser.parse_line(
        '"Name1<11111111111111><0>" killed "Name2<2222222222222><1>" with "the_weapon"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_KILL]
    attacker, victim = events[0].client, events[0].target
    assert attacker is not None and victim is not None
    assert (attacker.name, victim.name) == ("Name1", "Name2")
    assert events[0].data.weapon == "the_weapon"


def test_a_kill_with_an_unquoted_weapon():
    """Captured, from the same file, with no quotes at all:
    `"txsniper<76561000000000000><1>" killed "Killer Badger<70000000000000005><0>" with
    R_DmgType_SniperPrimary`. Both forms are real, which is why the pattern makes the quotes
    optional — and why the victim's name having a space in it matters here too."""
    parser = _parser()
    events = parser.parse_line(
        '"txsniper<76561000000000000><1>" killed '
        '"Killer Badger<70000000000000005><0>" with R_DmgType_SniperPrimary'
    )

    assert [e.type for e in events] == [EventType.CLIENT_KILL]
    victim = events[0].target
    assert victim is not None and victim.name == "Killer Badger"
    assert events[0].data.weapon == "R_DmgType_SniperPrimary"


def test_same_team_makes_it_a_team_kill():
    parser = _parser()
    events = parser.parse_line(
        '"Name1<11111111111111><0>" killed "Name2<2222222222222><0>" with "the_weapon"'
    )
    assert [e.type for e in events] == [EventType.CLIENT_KILL_TEAM]


def test_killing_yourself_through_the_kill_line_is_a_suicide():
    parser = _parser()
    events = parser.parse_line(
        '"Name1<11111111111111><0>" killed "Name1<11111111111111><0>" with "the_weapon"'
    )
    assert [e.type for e in events] == [EventType.CLIENT_SUICIDE]


def test_the_dedicated_suicide_line():
    """Captured: `committed suicide with "R_DmgType_M26Grenade"`."""
    parser = _parser()
    _joined(parser)
    events = parser.parse_line(
        f'"courgette<{COURGETTE}><1>" committed suicide with "R_DmgType_M26Grenade"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_SUICIDE]
    assert events[0].data.weapon == "R_DmgType_M26Grenade"
    assert events[0].client is events[0].target


def test_a_kill_with_no_victim_at_all_is_the_world_killing_them():
    """Captured: `"Name1<11111111111111><0>" killed  with UTDmgType_VehicleCollision` — **two**
    spaces, and nothing between them.

    Reported as a suicide, as the classic parser did. The alternative is worse than it looks: the
    kill pattern would have to accept an empty victim name, and that would put a client with no name
    and no guid into the roster and then into the database.
    """
    parser = _parser()
    events = parser.parse_line('"Name1<11111111111111><0>" killed  with UTDmgType_VehicleCollision')

    assert [e.type for e in events] == [EventType.CLIENT_SUICIDE]
    assert events[0].data.weapon == "UTDmgType_VehicleCollision"
    client = events[0].client
    assert client is not None and client.name == "Name1"
    assert parser.clients.get_by_cid("") is None, "no nameless client was invented"


def test_the_double_space_line_is_not_read_as_an_ordinary_kill():
    """Guarding the ordering of two patterns that can both plausibly claim this line."""
    parser = _parser()
    assert (
        parser.handler_for('"Name1<11111111111111><0>" killed  with UTDmgType_VehicleCollision')
        == "on_killed_by_the_world"
    )


# -- the match ----------------------------------------------------------------------------------


def test_the_gametype_comes_out_of_the_map_name_because_there_is_nowhere_else():
    """`CTR_Canyon` is Capture the Resource on Canyon; `Thrust_Oilrig` is Thrust on Oilrig. This
    engine has no cvars to ask, so the prefix is the only statement of mode there is."""
    parser = _parser()
    events = parser.parse_line('Loading map "Thrust_Oilrig"')

    assert [e.type for e in events] == [EventType.GAME_ROUND_START]
    assert events[0].data == {"mapname": "Thrust_Oilrig", "g_gametype": "Thrust"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0", "red"), ("1", "blue")],
)
def test_the_winning_team_is_translated_but_the_raw_id_is_kept(raw, expected):
    parser = _parser()
    events = parser.parse_line(f'Round finished, winning team is "{raw}"')

    assert [e.type for e in events] == [EventType.GAME_ROUND_END]
    assert events[0].data == expected
    assert events[0].extra["team_id"] == raw


def test_an_unrecognised_winning_team_is_reported_as_itself_rather_than_dropped():
    parser = _parser()
    events = parser.parse_line('Round finished, winning team is "7"')
    assert events[0].data == "7"


def test_round_started():
    parser = _parser()
    assert [e.type for e in parser.parse_line("Round started")] == [EventType.GAME_ROUND_START]


# -- the admin connection talking about itself ---------------------------------------------------


def test_a_remote_admin_connecting_is_reported_rather_than_ignored():
    """Captured: `(127.0.0.1:3508 has connected remotely)`. The classic parser dropped it; on a
    server where this bot is the only admin tool, a second one appearing is worth knowing about."""
    parser = _parser()
    events = parser.parse_line("(127.0.0.1:3508 has connected remotely)")

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra == {"kind": "rcon_connected", "ip": "127.0.0.1", "port": 3508}


def test_a_remote_admin_leaving_runs_the_login_into_the_address():
    """Captured: `RCon:(Admin127.0.0.1:3508 has disconnected from RCon)` — no separator between the
    login and the IP, which is the game's own doing and not a transcription slip."""
    parser = _parser()
    events = parser.parse_line("RCon:(Admin127.0.0.1:3508 has disconnected from RCon)")

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["login"] == "Admin"
    assert events[0].extra["ip"] == "127.0.0.1"


def test_junk_is_understood_to_be_junk():
    """Captured, as the classic suite's own unknown-line case: `f00 f00 f00`."""
    parser = _parser()
    assert parser.parse_line("f00 f00 f00") == []
    assert parser.handler_for("f00 f00 f00") is None, "`b3 probe` must be able to say so"


# -- the player list, which is a reply the parser reads -------------------------------------------


PLAYER_LIST = f"1 players:\ncourgette 21 pts 4:8 38ms steamid: {COURGETTE}\n"


def test_the_player_list_reply_is_routed_like_any_other_line():
    """Captured reply to `getplayerlist`. It arrives as a *reply*, but the client hands it to the
    parser as a line so that all the parsing lives in one place."""
    parser = _parser()
    assert parser.handler_for(PLAYER_LIST) == "on_player_list"


def test_reading_the_player_list_produces_no_event():
    """It is an answer, not something that happened."""
    parser = _parser()
    assert parser.parse_line(PLAYER_LIST) == []


def test_the_player_list_carries_the_only_scores_and_pings_this_engine_reports():
    parser = _parser()
    parser.parse_line(PLAYER_LIST)

    players = parser.get_players()
    assert len(players) == 1
    assert (players[0].cid, players[0].name) == (COURGETTE, "courgette")
    assert (players[0].ping, players[0].score) == (38, 21)


def test_a_player_seen_only_in_the_list_still_joins_the_roster():
    """A bot started mid-round learns who is playing from the first refresh, not from the log."""
    parser = _parser()
    parser.parse_line(PLAYER_LIST)
    client = parser.clients.get_by_cid(COURGETTE)
    assert client is not None and client.name == "courgette"


def test_negative_scores_parse_because_team_killing_earns_them():
    parser = _parser()
    parser.parse_line(f"1 players:\ncourgette -3 pts 0:4 210ms steamid: {COURGETTE}\n")
    assert parser.get_players()[0].score == -3


def test_a_name_with_spaces_in_a_player_row():
    parser = _parser()
    parser.parse_line("1 players:\nKiller Badger 7 pts 2:1 55ms steamid: 70000000000000005\n")
    players = parser.get_players()
    assert (players[0].name, players[0].guid) == ("Killer Badger", "70000000000000005")


def test_an_empty_player_list_does_not_empty_the_roster():
    """The log is the authority on who left — it reports every disconnect. A refresh that arrives
    truncated, or during a map change, must not evict everybody: `sync` would then announce a room
    full of departures that never happened.
    """
    parser = _parser()
    parser.parse_line(PLAYER_LIST)
    parser.parse_line("0 players:\n")
    assert [p.cid for p in parser.get_players()] == [COURGETTE]


def test_the_roster_keeps_what_the_log_knows_and_the_list_confirms():
    parser = _parser()
    _joined(parser, team="0")
    parser.parse_line(PLAYER_LIST)

    players = parser.get_players()
    assert len(players) == 1
    assert players[0].ip == "192.168.0.1", "the IP only ever appears on the connect line"
    assert players[0].ping == 38, "and the ping only ever in the list"


def test_stats_are_forgotten_when_the_player_leaves():
    parser = _parser()
    _joined(parser)
    parser.parse_line(PLAYER_LIST)
    parser.parse_line(f'"courgette<{COURGETTE}><1>"disconnected')

    _joined(parser)
    assert parser.get_players()[0].score == 0, "a new session starts from nothing"


# -- the map list -------------------------------------------------------------------------------


MAP_LIST = "0 CTR_Canyon\n1 CTR_Derelict\n2 CTR_IceBreaker\n3 CTR_Liberty\n"


def test_the_map_list_is_read_in_rotation_order_with_the_current_map_first():
    """Captured reply to `getmaplist false`. **Index 0 is the map being played now** — which is how
    the classic parser answered both `!map` and `!nextmap` from a single call."""
    assert RavParser.read_maps(MAP_LIST) == [
        "CTR_Canyon",
        "CTR_Derelict",
        "CTR_IceBreaker",
        "CTR_Liberty",
    ]


def test_a_thirteen_map_rotation_reads_double_digit_indices():
    """Captured from the classic `changeMap` test, whose rotation runs to index 12."""
    listing = "\n".join(f"{i} Map{i}" for i in range(13)) + "\n"
    assert RavParser.read_maps(listing)[12] == "Map12"
