"""The messages a BattlEye server pushes, and the Arma profiles.

Sample messages come from the docstrings of the classic ``battleye/abstractParser.py``, captured from
real Arma servers. Not verified against a live Arma server; see TODO.md §2.4.

The identity tests are the ones that matter. BattlEye announces a connection *without* an id and then
sends the id separately — first as the client claimed it, then again once BattlEye's own backend has
checked it — so which of those the bot acts on decides whether a ban can be dodged by claiming
somebody else's GUID.
"""

from __future__ import annotations

import pytest

from b3.core.events import EventType
from b3.domain.client import Client
from b3.parsers.battleye.parser import BeParser
from b3.parsers.battleye.profiles import ARMA2, ARMA3
from b3.parsers.battleye.status import PATTERNS
from b3.parsers.games import PROFILES, PUSH_FAMILIES, parser_for
from b3.parsers.status import parse_status

GUID = "80a5885ebe2420bab5e1581234567890"
GUID2 = "04b81a0bd914e7ba610ef31234567890"

PLAYERS_REPLY = (
    "Players on server:\n"
    "[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n"
    "--------------------------------------------------\n"
    f"0   76.108.91.78:2304    31   {GUID}(OK) Bravo17\n"
    f"2   198.51.100.7:2304    64   {GUID2}(?) NZ\n"
    "3   203.0.113.9:2304     -1   -                                    Joining (Lobby)\n"
    "(3 players in total)"
)


@pytest.fixture
def parser() -> BeParser:
    return BeParser(ARMA2)


def one(p: BeParser, line: str):  # noqa: ANN201
    events = p.parse_line(line)
    assert len(events) == 1, f"expected 1 event from {line!r}, got {events!r}"
    return events[0]


# -- identity --------------------------------------------------------------


def test_a_connection_arrives_before_any_id(parser):  # noqa: ANN001
    ev = one(parser, "Player #0 Bravo17 (76.108.91.78:2304) connected")

    assert ev.type is EventType.CLIENT_CONNECT
    client = parser.clients.get_by_cid("0")
    assert (client.name, client.ip) == ("Bravo17", "76.108.91.78")
    assert client.guid == ""  # the connect message carries none


def test_the_verified_id_is_what_joins_a_player(parser):  # noqa: ANN001
    """CLIENT_JOIN is published here, not on connect: before this there is no identity to match."""
    parser.parse_line("Player #0 Bravo17 (76.108.91.78:2304) connected")
    ev = one(parser, f"Verified GUID ({GUID}) of player #0 Bravo17")

    assert ev.type is EventType.CLIENT_JOIN
    assert parser.clients.get_by_cid("0").guid == GUID
    assert parser.clients.get_by_cid("0").ip == "76.108.91.78"  # kept from the connect message


def test_an_unverified_id_does_not_authenticate_anybody(parser):  # noqa: ANN001
    """It is what the *client* claimed. Trusting it is how somebody else's ban gets applied — or dodged."""
    ev = one(parser, f"Player #0 Bravo17 - GUID: {GUID} (unverified)")

    assert ev.type is EventType.CLIENT_UPDATE  # recorded, not a join
    assert parser.clients.get_by_cid("0").guid == GUID


def test_an_operator_can_choose_to_trust_the_unverified_id():
    """Faster, and their call — but it must be a decision, not the default."""
    from dataclasses import replace

    parser = BeParser(replace(ARMA2, trust_unverified_guid=True))
    ev = one(parser, f"Player #0 Bravo17 - GUID: {GUID} (unverified)")
    assert ev.type is EventType.CLIENT_JOIN


def test_a_verified_id_does_not_overwrite_one_already_held(parser):  # noqa: ANN001
    parser.parse_line(f"Verified GUID ({GUID}) of player #0 Bravo17")
    parser.parse_line(f"Verified GUID ({GUID2}) of player #0 Bravo17")
    assert parser.clients.get_by_cid("0").guid == GUID


def test_a_disconnect_removes_the_player(parser):  # noqa: ANN001
    parser.parse_line("Player #4 Kauldron (76.108.91.78:2304) connected")
    ev = one(parser, "Player #4 Kauldron disconnected")

    assert ev.type is EventType.CLIENT_DISCONNECT
    assert parser.clients.get_by_cid("4") is None


def test_a_disconnect_for_somebody_unknown_is_not_invented(parser):  # noqa: ANN001
    assert parser.parse_line("Player #9 Ghost disconnected") == []


def test_battleyes_own_kick_is_reported_as_a_disconnect(parser):  # noqa: ANN001
    """The anti-cheat's verdict, not ours — so it is not written into our penalty history."""
    parser.parse_line("Player #2 NZ (198.51.100.7:2304) connected")
    ev = one(
        parser,
        f"Player #2 NZ ({GUID2}) has been kicked by BattlEye: Script Restriction #107",
    )

    assert ev.type is EventType.CLIENT_DISCONNECT
    assert ev.data == "BattlEye: Script Restriction #107"
    assert parser.clients.get_by_cid("2") is None


# -- chat ------------------------------------------------------------------


def _with_players(profile=ARMA2) -> BeParser:  # noqa: ANN001
    p = BeParser(profile)
    p.clients.add(Client(cid="0", name="Bravo17", guid=GUID))
    p.clients.add(Client(cid="2", name="NZ", guid=GUID2))
    return p


@pytest.mark.parametrize("channel", ["Global", "Direct", "Lobby", "Unknown"])
def test_public_chat_channels(channel):  # noqa: ANN001
    p = _with_players()
    ev = one(p, f"({channel}) Bravo17: hello b3")
    assert ev.type is EventType.CLIENT_SAY
    assert ev.data == "hello b3"
    assert ev.client.cid == "0"
    assert ev.extra["channel"] == channel


@pytest.mark.parametrize("channel", ["Group", "Side", "Vehicle", "Command"])
def test_restricted_channels_are_team_chat(channel):  # noqa: ANN001
    """The classic parser reported every channel as a plain say, so no plugin could tell them apart."""
    p = _with_players()
    ev = one(p, f"({channel}) Bravo17: on my six")
    assert ev.type is EventType.CLIENT_TEAM_SAY


def test_chat_from_a_name_the_bot_has_never_seen_is_dropped():
    """Chat names the speaker rather than their slot — the Quake3 problem, handled the same way."""
    assert _with_players().parse_line("(Global) Stranger: hello") == []


def test_a_message_containing_a_colon_survives_intact():
    p = _with_players()
    ev = one(p, "(Global) Bravo17: meet at 12:30 by the hangar")
    assert ev.data == "meet at 12:30 by the hangar"


def test_a_player_whose_name_contains_a_colon_can_still_be_heard():
    """Splitting on the first colon would make them unidentifiable — and so immune to every plugin
    that reads chat. The speaker is matched against known names instead."""
    p = _with_players()
    p.clients.add(Client(cid="5", name="TF2: Sniper", guid="c" * 32))
    ev = one(p, "(Global) TF2: Sniper: hello")
    assert ev.client.cid == "5"
    assert ev.data == "hello"


def test_the_longest_matching_name_wins():
    p = _with_players()
    p.clients.add(Client(cid="6", name="Bravo17: the builder", guid="d" * 32))
    ev = one(p, "(Global) Bravo17: the builder: hi")
    assert ev.client.cid == "6"


def test_nothing_is_stripped_from_the_front_of_a_message(parser):  # noqa: ANN001
    """The shared parser eats a leading `12:30 `; a BattlEye message has no timestamp to eat."""
    assert parser._timestamp_re.sub("", "12:30 (Global) x") == "12:30 (Global) x"


def test_the_bots_own_output_coming_back_is_ignored():
    """Our `say` echoes on the RCON admin channel; reading it back would feed the bot itself."""
    p = _with_players()
    assert p.parse_line("RCon admin #0: (Global) b3: Bob was kicked") == []


def test_an_anti_cheat_script_log_reaches_plugins():
    p = _with_players()
    ev = one(
        p, f'Script Log: #0 Bravo17 ({GUID}) - #2 "bp_id") == -9999) then {{player setVariable'
    )
    assert ev.type is EventType.CLIENT_ACTION
    assert ev.data == "battleye_script_log"
    assert ev.client.cid == "0"


def test_an_unrecognised_message_yields_nothing(parser):  # noqa: ANN001
    assert parser.parse_line("Something the game grew after this was written") == []


# -- the players table -----------------------------------------------------


def test_the_players_table_parses_every_row():
    _map, players = parse_status(PLAYERS_REPLY, PATTERNS, ARMA2.identity_field)

    assert [p.cid for p in players] == ["0", "2", "3"]
    assert [p.name for p in players] == ["Bravo17", "NZ", "Joining"]
    assert [p.ip for p in players] == ["76.108.91.78", "198.51.100.7", "203.0.113.9"]
    assert players[0].guid == GUID
    assert players[1].guid == GUID2  # an unverified id still identifies the row


def test_a_player_still_in_the_lobby_is_not_lost():
    """No GUID, ping -1 — but connected, and kickable, so dropping the row would hide them.

    This is why the table is one pattern with an optional id rather than two candidates: both shapes
    appear in the same reply, and `parse_status` keeps only the first pattern that matched.
    """
    _map, players = parse_status(PLAYERS_REPLY, PATTERNS, ARMA2.identity_field)
    lobby = players[2]
    assert (lobby.cid, lobby.guid, lobby.ping) == ("3", "", -1)


def test_a_player_listed_twice_keeps_their_real_address():
    """Captured: `tests/core/parsers/test_arma2.py:322` (`test_getPlayerList`).

    An Arma server prints a player who is also in the lobby **twice on the same slot**, and the
    second row carries the lobby connection's address and port, not the player's. Both rows were
    kept, so the last one won everywhere a roster is read by slot: the address this bot recorded,
    saved to the database, wrote alias history for and matched shared ban lists against was
    `192.168.0.100` — a private address — while the player's real one was in the same reply.
    """
    reply = (
        "Players on server:\n"
        "[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n"
        "--------------------------------------------------\n"
        f"0   76.108.91.78:2304     63   {GUID}(OK) Bravo17\n"
        f"0   192.168.0.100:2316    0    {GUID}(OK) Bravo17 (Lobby)\n"
        "(1 players in total)"
    )

    _map, players = parse_status(reply, PATTERNS, ARMA2.identity_field)

    assert len(players) == 1  # one slot, one player
    assert (players[0].ip, players[0].port) == ("76.108.91.78", 2304)
    assert players[0].ping == 63  # the lobby row's 0 is not a measurement of anything
    assert players[0].lobby is False


def test_the_in_game_row_wins_whichever_order_the_server_prints_them():
    """The capture happens to print the game row first; nothing promises it always will."""
    rows = (
        f"0   192.168.0.100:2316    0    {GUID}(OK) Bravo17 (Lobby)\n",
        f"0   76.108.91.78:2304     63   {GUID}(OK) Bravo17\n",
    )
    reply = "Players on server:\n[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n" + "".join(rows)

    _map, players = parse_status(reply, PATTERNS, ARMA2.identity_field)

    assert len(players) == 1
    assert players[0].ip == "76.108.91.78"


def test_a_lobby_only_player_is_still_reported_as_being_in_the_lobby():
    """Captured: `tests/core/parsers/test_arma3.py:176`. Their only row is the lobby one.

    They are connected and kickable, so the row must survive — and the flag has to be readable
    rather than stripped off the name, which is what hid the duplicate-row fault above.
    """
    reply = (
        "Players on server:\n"
        "[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n"
        f"8   111.222.200.50:2304   -1   {GUID}(?)  Max (Lobby)\n"
        "(14 players in total)"
    )

    _map, players = parse_status(reply, PATTERNS, ARMA2.identity_field)

    assert len(players) == 1
    assert players[0].name == "Max"  # the marker is not part of it
    assert players[0].lobby is True
    assert players[0].ip == "111.222.200.50"


def test_a_table_with_no_score_column_does_not_break_the_shared_reader():
    _map, players = parse_status(PLAYERS_REPLY, PATTERNS, ARMA2.identity_field)
    assert all(p.score == 0 for p in players)


def test_an_empty_server_is_not_an_error():
    empty = "Players on server:\n[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n(0 players in total)"
    _map, players = parse_status(empty, PATTERNS, ARMA2.identity_field)
    assert players == []


# -- profile and wiring ----------------------------------------------------


@pytest.mark.parametrize("game", ["arma2", "arma3"])
def test_both_titles_are_configurable(game):  # noqa: ANN001
    profile = PROFILES[game]
    assert profile.family == "battleye"
    assert isinstance(parser_for(profile), BeParser)


def test_battleye_is_marked_as_a_push_family():
    """Which is what tells the CLI to use one object as both rcon client and log source."""
    assert PROFILES["arma3"].family in PUSH_FAMILIES


def test_the_two_titles_differ_in_nothing_the_bot_cares_about():
    from dataclasses import replace

    assert replace(ARMA2, name="arma3") == ARMA3


def test_the_ban_verbs_use_a_native_duration():
    """BattlEye takes minutes, 0 being permanent — so a tempban is enforced by the server."""
    assert ARMA2.ban_template.startswith("addBan %(guid)s 0 %(reason)s")
    assert ARMA2.tempban_template.startswith("addBan %(guid)s %(minutes)s %(reason)s")
    assert ARMA2.tempban_max_minutes == 0  # no engine ceiling to work around


def test_a_ban_keys_on_the_guid_so_an_offline_player_can_be_banned():
    """Captured: `tests/core/parsers/test_arma2.py:377` (`test_ban__by_guid`).

    The classic bot had two ban verbs and picked between them on whether the player held a slot:
    `ban <slot>` for someone standing there, `addBan <guid>` for someone who is not. We had only
    the first, and `Bot._send_penalty` skips any line naming a slot the player has not got — so
    banning an offline Arma player sent the server **nothing**, silently.
    """
    lines = ARMA2.ban_template.splitlines()

    assert lines[0] == "addBan %(guid)s 0 %(reason)s"  # works with no slot
    assert "%(cid)s" not in lines[0]
    # `addBan` writes the list without removing the player, so the kick is a separate line — which
    # is also what lets it be dropped for a player who is not connected.
    assert lines[1] == "kick %(cid)s %(reason)s"


def test_every_ban_saves_the_ban_list_to_disk():
    """Captured: `tests/core/parsers/test_arma2.py:370-408` — every ban asserts a `writeBans`.

    BattlEye keeps its ban list in memory. Without this verb the list on disk never changes, so a
    ban does not survive a server restart — and neither does an *unban*, which is the worse half:
    the player comes back banned by a list this bot's database says nothing about, so nothing will
    ever look at it again.
    """
    assert ARMA2.ban_template.splitlines()[-1] == "writeBans"
    assert ARMA2.tempban_template.splitlines()[-1] == "writeBans"


def test_unban_is_declared_inexpressible():
    """`removeBan` takes a ban-list index, which the bot does not know. See TODO.md §2.4."""
    assert ARMA2.unban_template is None
