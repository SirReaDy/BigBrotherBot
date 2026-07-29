"""Homefront's message grammar, and the four things about this engine that catch people out.

Identity is a Steam id while the *handle* is the player's name; a kill line may name either; the
chatter channel repeats every message including the bot's own; and nothing can be asked for
synchronously, so the roster is assembled from pushed messages.
"""

from __future__ import annotations

import json

import pytest

from b3.core.events import EventType
from b3.net.homefront import CHANNEL_CHATTER, CHANNEL_GAMEPLAY, CHANNEL_SERVER
from b3.parsers.homefront.parser import HfParser, KillData
from b3.parsers.homefront.profiles import HOMEFRONT

ADMIN_ID = "76561197963239764"
BOB_ID = "76561197963239765"


def _parser() -> HfParser:
    return HfParser(HOMEFRONT)


def _line(data: str, channel: int = CHANNEL_SERVER) -> str:
    return json.dumps([channel, data])


def _join(parser: HfParser, name: str, uid: str, team: str | None = None) -> None:
    parser.parse_line(_line(f"LOGIN: {name} {uid}"))
    if team is not None:
        parser.parse_line(_line(f"TEAM CHANGE: {uid} {team}"))


# -- identity -----------------------------------------------------------------------------------


def test_a_login_is_the_join_because_it_carries_the_identity():
    parser = _parser()
    events = parser.parse_line(_line(f"LOGIN: Courgette {ADMIN_ID}"))

    assert [e.type for e in events] == [EventType.CLIENT_JOIN]
    client = events[0].client
    assert client is not None
    assert (client.cid, client.name, client.guid) == ("Courgette", "Courgette", ADMIN_ID)


def test_a_name_with_spaces_survives_because_the_id_is_at_the_end():
    """Split at the *last* space: `<name> <steam id>`, and plenty of names contain spaces."""
    parser = _parser()
    parser.parse_line(_line(f"LOGIN: Bob the Builder {BOB_ID}"))
    client = parser.clients.get_by_cid("Bob the Builder")
    assert client is not None and client.guid == BOB_ID


@pytest.mark.parametrize("uid", ["0", "00"])
def test_a_connection_with_no_usable_id_is_not_a_player(uid):
    """`00` is a banned player being turned away; `0` is one the server has not resolved yet. Making
    a client from either would put a row in the database that every unidentified player then shares."""
    parser = _parser()
    assert parser.parse_line(_line(f"LOGIN: Somebody {uid}")) == []
    assert parser.clients.connected() == []


def test_a_uid_message_joins_a_player_the_login_was_missed_for():
    """The classic parser created the client here silently, so a player met this way was never
    authenticated — no level, no ban check — until they happened to say something."""
    parser = _parser()
    events = parser.parse_line(_line(f"UID: Courgette {ADMIN_ID}"))

    assert [e.type for e in events] == [EventType.CLIENT_JOIN]


def test_but_a_uid_for_somebody_already_known_is_not_a_second_join():
    parser = _parser()
    parser.parse_line(_line(f"LOGIN: Courgette {ADMIN_ID}"))
    assert parser.parse_line(_line(f"UID: Courgette {ADMIN_ID}")) == []


def test_a_rename_keeps_the_same_player():
    """The id is the identity and the name is only the handle — but the handle is what chat lines
    carry, so the roster key has to move with it."""
    parser = _parser()
    parser.parse_line(_line(f"LOGIN: Courgette {ADMIN_ID}"))
    parser.parse_line(_line(f"UID: Courgette2 {ADMIN_ID}"))

    assert parser.clients.get_by_cid("Courgette") is None
    moved = parser.clients.get_by_cid("Courgette2")
    assert moved is not None and moved.guid == ADMIN_ID
    assert len(parser.clients.connected()) == 1


def test_a_logout_removes_them():
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)
    events = parser.parse_line(_line(f"LOGOUT: {ADMIN_ID}"))

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT]
    assert parser.clients.connected() == []


# -- combat -------------------------------------------------------------------------------------


def test_a_kill_line_may_name_either_party_by_id_or_by_name():
    """Both forms turn up in the same field, which is why each is tested against a 17-digit pattern."""
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID, team="0")
    _join(parser, "Bob", BOB_ID, team="1")

    events = parser.parse_line(_line(f"KILL: {ADMIN_ID} EXP_Frag Bob"))

    assert [e.type for e in events] == [EventType.CLIENT_KILL]
    assert events[0].client is not None and events[0].client.guid == ADMIN_ID
    assert events[0].target is not None and events[0].target.cid == "Bob"
    assert events[0].data == KillData(weapon="EXP_Frag")


def test_a_kill_between_team_mates_is_a_team_kill():
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID, team="1")
    _join(parser, "Bob", BOB_ID, team="1")

    events = parser.parse_line(_line("KILL: Courgette EXP_Frag Bob"))
    assert [e.type for e in events] == [EventType.CLIENT_KILL_TEAM]


def test_players_with_no_team_yet_are_not_team_mates():
    """Team 3 is "none", and reading it as a side makes every early kill a team kill."""
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID, team="3")
    _join(parser, "Bob", BOB_ID, team="3")

    events = parser.parse_line(_line("KILL: Courgette EXP_Frag Bob"))
    assert [e.type for e in events] == [EventType.CLIENT_KILL]


def test_suicided_is_how_leaving_looks_as_well_as_dying():
    """The classic parser's own comment: this fires when a player leaves the server, too. Published as
    a suicide either way; the LOGOUT that follows is what removes them."""
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID, team="1")

    events = parser.parse_line(_line("KILL: Courgette Suicided Courgette"))
    assert [e.type for e in events] == [EventType.CLIENT_SUICIDE]
    assert events[0].client is events[0].target


def test_a_kill_involving_somebody_unknown_is_dropped():
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID, team="1")
    assert parser.parse_line(_line("KILL: Courgette EXP_Frag Stranger")) == []


# -- chat ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("", EventType.CLIENT_SAY),
        (" (team)", EventType.CLIENT_TEAM_SAY),
        (" (squad)", EventType.CLIENT_SQUAD_SAY),
    ],
)
def test_the_three_chat_scopes(marker, expected):
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)

    # The space belongs to the format, not to the marker: "Bob says:" and "Bob (team)says:".
    text = f"BROADCAST: Courgette{marker or ' '}says: hello"
    events = parser.parse_line(_line(text, CHANNEL_CHATTER))

    assert [e.type for e in events] == [expected]
    assert events[0].data == "hello"


def test_the_space_before_says_is_required_so_a_name_is_not_eaten():
    """`Bob says: hi` — the classic pattern requires that space, and a fake server which dropped it
    produced `Bobsays:` and no chat at all. Found by the end-to-end run, so it is pinned here."""
    parser = _parser()
    _join(parser, "Bob", BOB_ID)
    assert parser.parse_line(_line("BROADCAST: Bobsays: hi", CHANNEL_CHATTER)) == []


def test_the_repeat_of_every_chatter_message_is_not_a_player_talking():
    """Each message arrives twice — as a BROADCAST and as a plain repeat — and the bot's own output
    comes back the same way. Read as speech, the bot answers its own announcements forever."""
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)

    events = parser.parse_line(_line("Courgette: !help", CHANNEL_CHATTER))

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "server_say"
    assert events[0].client is None  # so nothing routes it to the command processor


def test_the_bots_own_announcement_coming_back_is_not_obeyed():
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)
    events = parser.parse_line(_line("Server: !permban Courgette", CHANNEL_CHATTER))
    assert [e.type for e in events] == [EventType.CUSTOM]


def test_chat_from_a_player_the_bot_never_saw_connect_is_dropped():
    """The line carries only a name, and a client invented from it would have no identity."""
    parser = _parser()
    assert parser.parse_line(_line("BROADCAST: Stranger says: hi", CHANNEL_CHATTER)) == []


# -- the match ----------------------------------------------------------------------------------


def test_a_level_change_is_reported_the_way_the_runtime_records_a_map():
    parser = _parser()
    events = parser.parse_line(_line("CHANGE LEVEL: FL-Harbor"))

    assert [e.type for e in events] == [EventType.GAME_ROUND_START]
    assert events[0].data == {"mapname": "fl-harbor"}


@pytest.mark.parametrize(
    ("team_id", "name", "tie"),
    [("0", "red", False), ("1", "blue", False), ("2", "tie", True)],
)
def test_round_over_names_the_winning_side(team_id, name, tie):
    parser = _parser()
    events = parser.parse_line(_line(f"ROUND OVER: {team_id}"))

    assert [e.type for e in events] == [EventType.GAME_ROUND_END]
    assert events[0].data == name
    assert events[0].extra["tie"] is tie


def test_teams_and_clans():
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)

    team = parser.parse_line(_line(f"TEAM CHANGE: {ADMIN_ID} 0"))
    assert [e.type for e in team] == [EventType.CLIENT_TEAM_CHANGE]
    assert parser.clients.get_by_cid("Courgette").team == "red"

    clan = parser.parse_line(_line(f"CLAN CHANGE: {ADMIN_ID} B3bot"))
    assert [e.type for e in clan] == [EventType.CUSTOM]
    assert clan[0].extra["kind"] == "clan_change"
    assert clan[0].data == "B3bot"


# -- votes --------------------------------------------------------------------------------------


def test_votes_use_the_shared_vocabulary():
    """The classic bot created its own event names for these at runtime, so nothing could listen for a
    vote across two games. All four already exist here."""
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)

    start = parser.parse_line(_line(f"VOTESTART: {ADMIN_ID} kick {BOB_ID}"))
    assert [e.type for e in start] == [EventType.CLIENT_CALLVOTE]
    assert start[0].data == "kick"

    cast = parser.parse_line(_line(f"VOTE: {ADMIN_ID} 1"))
    assert [e.type for e in cast] == [EventType.CLIENT_VOTE]

    passed = parser.parse_line(_line("VOTEEND: 4 66.6 passed"))
    assert [e.type for e in passed] == [EventType.VOTE_PASSED]
    assert passed[0].extra["yes_votes"] == 4

    failed = parser.parse_line(_line("VOTEEND: 1 12.5 failed"))
    assert [e.type for e in failed] == [EventType.VOTE_FAILED]


# -- the roster nothing can ask for --------------------------------------------------------------


def test_the_player_list_reply_is_newline_separated_inside_one_message():
    parser = _parser()
    row = f"PLAYER: {BOB_ID}\n1\nB3bot\nBob the Builder\n7\n2"
    events = parser.parse_line(_line(row))

    # First sight of this player, so it is a join: it is the only place the bot could learn of them.
    assert [e.type for e in events] == [EventType.CLIENT_JOIN]
    client = parser.clients.get_by_cid("Bob the Builder")
    assert client is not None
    assert (client.guid, client.team) == (BOB_ID, "blue")


def test_a_player_row_for_somebody_known_is_an_update_not_a_join():
    parser = _parser()
    _join(parser, "Bob", BOB_ID)
    assert parser.parse_line(_line(f"PLAYER: {BOB_ID}\n0\nclan\nBob\n7\n2")) == []
    assert parser.clients.get_by_cid("Bob").team == "red"


def test_the_roster_and_pings_come_from_pushed_messages():
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)
    parser.parse_line(_line(f"PLAYER: {ADMIN_ID}\n1\n\nCourgette\n9\n3"))
    parser.parse_line(_line(f"PLAYERPING: {ADMIN_ID} 48"))

    players = parser.get_players()
    assert [p.cid for p in players] == ["Courgette"]
    assert players[0].guid == ADMIN_ID
    assert players[0].ping == 48
    assert players[0].score == 9  # kills, which is the only score this engine reports


def test_the_servers_ban_list_is_remembered():
    """Nothing acts on it yet; it is what an `!unban` that verifies itself would read."""
    parser = _parser()
    parser.parse_line(_line(f"BAN ITEM: Bob {BOB_ID}"))
    assert parser.get_bans() == {BOB_ID: "Bob"}

    added = parser.parse_line(_line(f"BAN ADDED: Courgette {ADMIN_ID}"))
    assert [e.type for e in added] == [EventType.CUSTOM]
    assert added[0].extra["kind"] == "ban_added"
    assert set(parser.get_bans()) == {BOB_ID, ADMIN_ID}

    removed = parser.parse_line(_line(f"BAN REMOVE: {BOB_ID}"))
    assert removed[0].extra["kind"] == "ban_removed"
    assert set(parser.get_bans()) == {ADMIN_ID}


def test_a_ban_the_server_announces_is_not_announced_again():
    """The classic parser shouted about it with `adminbigsay` — from inside the parser, and a second
    time after the admin plugin had already said so. Reported as an event; policy is a plugin's job."""
    parser = _parser()
    events = parser.parse_line(_line(f"BAN ADDED: Bob {BOB_ID}"))
    assert all(e.type is not EventType.CLIENT_SAY for e in events)


# -- robustness ---------------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["", "   ", "not json", '{"a": 1}', "[1]", "[1, 2, 3]"])
def test_a_line_that_is_not_one_of_ours_is_ignored(line):
    assert _parser().parse_line(line) == []


def test_channels_this_bot_does_not_read_are_ignored():
    assert _parser().parse_line(_line("anything at all", CHANNEL_GAMEPLAY)) == []


def test_an_unknown_event_name_is_ignored():
    assert _parser().parse_line(_line("SOMETHING NEW: whatever")) == []


def test_a_server_message_in_no_known_form_is_ignored():
    assert _parser().parse_line(_line("just some text with no colon")) == []


def test_half_a_message_does_not_raise():
    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)
    for data in (
        "LOGIN: nogid",
        "TEAM CHANGE: only-one-field",
        "KILL: incomplete",
        "PLAYER: too\nfew\nfields",
        "VOTEEND: 1",
        "PLAYERPING: nope",
        "BAN ITEM: no-id-here",
    ):
        parser.parse_line(_line(data))  # no exception is the assertion


# -- the roster after an outage ------------------------------------------------------------------


def test_a_reconnect_makes_the_roster_untrustworthy_and_it_is_dropped():
    """Every `LOGOUT` that happened while the socket was down was missed, so anybody who left is still
    on the list. The classic parser emptied its clients on a connection close for exactly this reason —
    but silently; here each of them is reported as a disconnect, so anything counting who is present
    hears about it. The client asks for the player list at once, so whoever is really there returns.
    """
    from b3.net.homefront import CHANNEL_CLIENT_NOTICE, RECONNECTED_NOTICE

    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)
    _join(parser, "Bob", BOB_ID)
    parser.parse_line(_line(f"PLAYERPING: {ADMIN_ID} 48"))
    assert len(parser.clients.connected()) == 2

    events = parser.parse_line(json.dumps([CHANNEL_CLIENT_NOTICE, RECONNECTED_NOTICE]))

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT] * 2
    assert all(e.extra["inferred"] is True for e in events)
    assert parser.clients.connected() == []
    assert parser.get_players() == []  # and no stale pings left behind either


def test_a_reconnect_with_nobody_known_says_nothing():
    from b3.net.homefront import CHANNEL_CLIENT_NOTICE, RECONNECTED_NOTICE

    parser = _parser()
    assert parser.parse_line(json.dumps([CHANNEL_CLIENT_NOTICE, RECONNECTED_NOTICE])) == []


def test_an_unknown_client_notice_is_ignored():
    from b3.net.homefront import CHANNEL_CLIENT_NOTICE

    parser = _parser()
    _join(parser, "Courgette", ADMIN_ID)
    assert parser.parse_line(json.dumps([CHANNEL_CLIENT_NOTICE, "SOMETHING ELSE"])) == []
    assert len(parser.clients.connected()) == 1  # and changes nothing
