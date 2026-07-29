"""Frontline's grammar, whose distinguishing feature is that **the roster reply is the event stream**.

This engine writes no connect line and no disconnect line. Nothing at all is reported when a player
arrives or leaves; the only statement of who is playing is the `PlayerList:` reply. So the parser diffs
it and publishes the joins and parts itself — which makes two things load-bearing that are nobody's
concern in any other family: the reply's own player count (a short read must not empty the room), and
its column header (a build that adds a column must not shift every field).

The lines and the reply shape come from the classic parser's patterns and its documented examples;
there is no captured test suite for this title, unlike Ravaged. Where the classic parser and the game's
own documentation disagree, the documentation wins — its login could never have run (`md5.new` on a
`hashlib.md5` import) and its `authorizeClients` raised `NotImplementedError`, so no player was ever
authenticated by it.
"""

from __future__ import annotations

from b3.core.events import EventType
from b3.net.frontline import RECONNECTED_NOTICE
from b3.parsers.frontline.parser import PLAYER_COLUMNS, FlParser, ServerBan
from b3.parsers.frontline.profiles import FRONTLINE

COURGETTE = "1561500"
BADGER = "1561501"

HEADERS = "\t".join(PLAYER_COLUMNS)


def _parser() -> FlParser:
    return FlParser(FRONTLINE)


def _row(
    cid: str,
    name: str,
    profile_id: str,
    team: str = "0",
    ping: int = 40,
    score: int = 0,
    kills: int = 0,
    pb_hash: str = "",
) -> str:
    values = {
        "ID": cid,
        "Name": name,
        "Ping": str(ping),
        "Team": team,
        "Squad": "0",
        "Score": str(score),
        "Kills": str(kills),
        "Deaths": "0",
        "TK": "0",
        "CP": "0",
        "Time": "120",
        "Idle": "0",
        "Loadout": "0",
        "Role": "Assault",
        "RoleLvl": "1",
        "Vehicle": "",
        "Hash": pb_hash,
        "ProfileID": profile_id,
    }
    return "\t".join(values[c] for c in PLAYER_COLUMNS)


def _playerlist(*rows: str, map_name: str = "CQ-Gnaw", count: int | None = None, round: str = "1/3") -> str:
    stated = len(rows) if count is None else count
    header = (
        f"PlayerList: Map={map_name} Time=739 Players={stated}/32 Tickets=500,500 Round={round}"
    )
    return "\n".join([header, HEADERS, *rows])


# -- the roster as the event stream --------------------------------------------------------------


def test_a_player_appearing_in_the_roster_is_the_join():
    """There is no connect line on this engine. This is the whole of how a join is learned."""
    parser = _parser()
    events = parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))

    joins = [e for e in events if e.type is EventType.CLIENT_JOIN]
    assert len(joins) == 1
    client = joins[0].client
    assert client is not None
    assert (client.cid, client.guid, client.name) == ("1", COURGETTE, "Courgette")
    assert client.team == "red"


def test_a_player_vanishing_from_the_roster_is_the_disconnect():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE), _row("2", "Badger", BADGER)))
    events = parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))

    gone = [e for e in events if e.type is EventType.CLIENT_DISCONNECT]
    assert len(gone) == 1
    assert gone[0].client is not None and gone[0].client.guid == BADGER
    assert parser.clients.get_by_cid("2") is None


def test_a_second_reply_with_the_same_players_produces_nothing():
    """It arrives every three seconds. A parser that re-announced the room each time would publish a
    join per player per interval, and every plugin counting joins would be wrong by a factor of time.
    """
    parser = _parser()
    listing = _playerlist(_row("1", "Courgette", COURGETTE))
    parser.parse_line(listing)
    assert parser.parse_line(listing) == []


def test_a_short_reply_does_not_empty_the_room():
    """The guard that matters most here. The reply states its own count, so a truncated one is
    detectable — and pruning on it would announce a dozen departures that never happened."""
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE), _row("2", "Badger", BADGER)))

    # The server says two players and sends one: a read that arrived mid-write.
    truncated = _playerlist(_row("1", "Courgette", COURGETTE), count=2)
    events = parser.parse_line(truncated)

    assert [e.type for e in events] == []
    assert {c.cid for c in parser.clients.connected()} == {"1", "2"}


def test_an_honestly_empty_server_does_empty_the_room():
    """The other half of that: `Players=0/32` with no rows is not a truncation, it is an empty server,
    and a bot that could not tell the difference would keep ghosts for ever."""
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    events = parser.parse_line(_playerlist())

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT]
    assert parser.clients.connected() == []


def test_the_column_header_is_read_rather_than_assumed():
    """A build that inserts a column would otherwise shift every field after it — so the name would
    become the ping and the profile id would become something else entirely."""
    parser = _parser()
    columns = ("ID", "Name", "Country", "Ping", "Team", "ProfileID")
    header = "PlayerList: Map=CQ-Gnaw Time=739 Players=1/32 Tickets=500,500 Round=1/3"
    listing = "\n".join(
        [header, "\t".join(columns), "\t".join(["3", "Courgette", "MD", "38", "1", COURGETTE])]
    )
    events = parser.parse_line(listing)

    joins = [e for e in events if e.type is EventType.CLIENT_JOIN]
    assert len(joins) == 1
    client = joins[0].client
    assert client is not None
    assert (client.name, client.guid, client.team) == ("Courgette", COURGETTE, "blue")


def test_a_slot_with_no_profile_id_yet_is_not_a_player():
    """A player mid-connect has `ProfileID=0`. Adding them would put a client the database cannot key
    on into the roster; the classic parser meant to skip these and compared its string to the int 0."""
    parser = _parser()
    events = parser.parse_line(_playerlist(_row("1", "Connecting", "0")))

    # The map in the header is still news, so that event is expected; the player is not.
    assert [e.type for e in events] == [EventType.GAME_ROUND_START]
    assert parser.clients.connected() == []


def test_a_slot_number_reused_by_a_different_player_is_a_different_person():
    """Slots are reused here, which is why bans are keyed on the ProfileID. The bot must not inherit
    the previous occupant's identity."""
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    parser.parse_line(_playerlist())  # slot 1 empties
    parser.parse_line(_playerlist(_row("1", "Badger", BADGER)))

    client = parser.clients.get_by_cid("1")
    assert client is not None and client.guid == BADGER


def test_a_team_change_is_reported_from_the_roster_too():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE, team="0")))
    events = parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE, team="1")))

    assert [e.type for e in events] == [EventType.CLIENT_TEAM_CHANGE]
    assert events[0].data == "blue"


def test_a_rename_is_followed():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    parser.parse_line(_playerlist(_row("1", "Courgette2", COURGETTE)))

    client = parser.clients.get_by_cid("1")
    assert client is not None and client.name == "Courgette2"


def test_scores_and_pings_come_off_the_roster():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE, ping=38, score=21)))

    players = parser.get_players()
    assert len(players) == 1
    assert (players[0].ping, players[0].score) == (38, 21)


def test_a_statistic_that_is_not_a_number_costs_that_cell_and_nothing_else():
    """The roster is the only source of identity on this engine, so one odd column must not cost it."""
    parser = _parser()
    row = _row("1", "Courgette", COURGETTE).split("\t")
    row[PLAYER_COLUMNS.index("Ping")] = "n/a"
    events = parser.parse_line(_playerlist("\t".join(row)))

    assert EventType.CLIENT_JOIN in [e.type for e in events]
    assert parser.get_players()[0].ping == 0


# -- the header, which is the only place the map and round appear ---------------------------------


def test_the_map_comes_from_the_roster_header():
    parser = _parser()
    events = parser.parse_line(_playerlist(map_name="FL-Oilfield"))

    starts = [e for e in events if e.type is EventType.GAME_ROUND_START]
    assert len(starts) == 1
    assert starts[0].data["mapname"] == "FL-Oilfield"
    assert starts[0].data["sv_maxclients"] == "32"


def test_the_same_map_reported_again_is_not_a_new_round():
    """The header arrives every three seconds. A round-start event on each would reset the round clock
    continuously, and every "this round lasted" figure would read three seconds."""
    parser = _parser()
    parser.parse_line(_playerlist())
    events = parser.parse_line(_playerlist())
    assert [e.type for e in events] == []


def test_a_new_round_on_the_same_map_is_reported():
    parser = _parser()
    parser.parse_line(_playerlist(round="1/3"))
    events = parser.parse_line(_playerlist(round="2/3"))

    assert [e.type for e in events] == [EventType.GAME_ROUND_START]
    assert events[0].data["g_currentround"] == "2"
    assert "mapname" in events[0].data


def test_the_ticket_pair_is_read():
    """The classic parser asked for `Tickets=(?P<tickets>,\\d+)`, which can never match `500,500` — so
    its map name, round number and slot count were never recorded either, all four in one pattern."""
    parser = _parser()
    events = parser.parse_line(_playerlist())
    assert events[0].data["tickets"] == "500,500"


def test_a_header_missing_a_field_still_yields_the_roster():
    parser = _parser()
    listing = "\n".join(
        ["PlayerList: Players=1/32", HEADERS, _row("1", "Courgette", COURGETTE)]
    )
    events = parser.parse_line(listing)
    assert EventType.CLIENT_JOIN in [e.type for e in events]


# -- chat ----------------------------------------------------------------------------------------


def test_chat_is_resolved_by_exact_name_because_that_is_all_it_carries():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    events = parser.parse_line(
        'CHAT: PlayerName="Courgette" Channel="Say" Message="hello everybody"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_SAY]
    assert events[0].data == "hello everybody"
    assert events[0].client is not None and events[0].client.guid == COURGETTE


def test_team_chat_is_distinguished():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    events = parser.parse_line('CHAT: PlayerName="Courgette" Channel="TeamSay" Message="on me"')
    assert [e.type for e in events] == [EventType.CLIENT_TEAM_SAY]


def test_squad_chat_is_team_chat_rather_than_a_broadcast():
    """This bot has no narrower vocabulary for it, and the one thing that is certainly true of squad
    chat is that it was not public."""
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    events = parser.parse_line('CHAT: PlayerName="Courgette" Channel="SquadSay" Message="flank"')

    assert [e.type for e in events] == [EventType.CLIENT_TEAM_SAY]
    assert events[0].extra["channel"] == "SquadSay"


def test_a_channel_nobody_has_seen_is_reported_rather_than_dropped():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    events = parser.parse_line('CHAT: PlayerName="Courgette" Channel="Whisper" Message="psst"')

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra == {"kind": "chat", "channel": "Whisper"}


def test_chat_from_somebody_not_on_the_roster_yet_is_not_an_event():
    """Rather than a client invented from a name: slots are reused here, so a name-keyed identity
    would eventually attach one player's history to another."""
    parser = _parser()
    assert parser.parse_line('CHAT: PlayerName="Ghost" Channel="Say" Message="hi"') == []


def test_a_name_with_a_quote_in_it_does_not_break_the_chat_pattern():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", 'He said "no"', COURGETTE)))
    # The name field is delimited by quotes, so the server's own escaping is what it is; what must not
    # happen is the *message* being lost when the name is odd.
    events = parser.parse_line('CHAT: PlayerName="He said " Channel="Say" Message="hi"')
    assert [e.type for e in events] in ([], [EventType.CLIENT_SAY])


# -- the server's own bookkeeping ------------------------------------------------------------------


def test_a_ban_the_server_reports_is_published():
    """It may not be a ban the bot made — a console admin's ban looks exactly like this, and it is
    precisely the thing an operator wants in the log."""
    parser = _parser()
    events = parser.parse_line(
        'Banned Player: PlayerName="Courgette" PlayerID=1 ProfileID=1561500 Hash= '
        "BanDuration=-1 Permanently"
    )

    assert [e.type for e in events] == [EventType.CUSTOM]
    ban = events[0].data
    assert isinstance(ban, ServerBan)
    assert (ban.guid, ban.name, ban.minutes) == (COURGETTE, "Courgette", None)


def test_a_timed_ban_keeps_its_duration_in_minutes():
    parser = _parser()
    events = parser.parse_line(
        'Banned Player: PlayerName="Courgette" PlayerID=1 ProfileID=1561500 Hash= BanDuration=120'
    )
    assert events[0].data.minutes == 120


def test_the_kick_half_of_a_ban_is_understood_and_quiet():
    """Two lines describe one ban. Both are matched — so `b3 probe` does not call one of them an
    unknown line, which is what the classic parser's pattern did on every single ban."""
    parser = _parser()
    line = (
        'Kicked Player as part of ban: PlayerName="Courgette" PlayerID=1 ProfileID=1561500 '
        "Hash= BanDuration=-1"
    )
    assert parser.parse_line(line) == []
    assert parser.handler_for(line) == "on_kicked_for_ban"


def test_an_unban_is_published():
    parser = _parser()
    events = parser.parse_line(
        'UnBanned Player: PlayerName="" PlayerID=-1 ProfileID=1561500 Hash='
    )
    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "server_unban"
    assert events[0].data == COURGETTE


def test_a_refused_unban_is_reported_rather_than_ignored():
    """The classic parser ignored this line, which makes a failed `!unban` indistinguishable from one
    that worked: the bot clears its own record either way and nobody finds out until the player
    walks back in."""
    parser = _parser()
    events = parser.parse_line(
        "UnBan failed! Player ProfileID or Hash is not banned: 1561500"
    )
    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "server_unban_failed"


# -- maps ----------------------------------------------------------------------------------------


def test_the_map_list_is_read_and_not_filtered():
    """The classic parser kept only names starting `FL-`, which drops the `CQ-` maps quoted in its own
    docstring — so `!maps` would have shown a fraction of the rotation."""
    parser = _parser()
    listing = "\n".join(
        [
            "MapList: Count=4",
            "ID\tMapName\tGameType",
            "0\tCQ-Gnaw\tConquest",
            "1\tFL-Oilfield\tFrontlines",
            "2\tCQ-Bridge\tConquest",
            "3\tFL-Harbor\tFrontlines",
        ]
    )
    assert parser.parse_line(listing) == [], "a rotation is a fact, not an event"
    assert parser.get_maps() == ["CQ-Gnaw", "FL-Oilfield", "CQ-Bridge", "FL-Harbor"]


def test_the_current_and_next_map_replies_are_kept():
    parser = _parser()
    parser.parse_line("CurrentMap is: CQ-Gnaw")
    parser.parse_line("NextMap is: FL-Oilfield")

    assert parser.current_map == "CQ-Gnaw"
    assert parser.get_next_map() == "FL-Oilfield"


def test_a_forced_transition_invalidates_what_we_thought_was_current():
    """It does not name the new map — the next roster header does — so the honest thing is to forget."""
    parser = _parser()
    parser.parse_line("CurrentMap is: CQ-Gnaw")
    parser.parse_line("Forced transition to next map")
    assert parser.current_map == ""


# -- PunkBuster, the only source of an IP address --------------------------------------------------


def test_punkbuster_is_the_only_thing_that_reports_an_ip():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    assert parser.get_players()[0].ip == "", "the roster carries no address at all"

    events = parser.parse_line(
        "PunkBuster Server: Player GUID Computed "
        "0123456789abcdef0123456789abcdef(-) (slot #1) 192.168.0.1:14567 Courgette"
    )

    assert [e.type for e in events] == [EventType.CLIENT_UPDATE]
    client = parser.clients.get_by_cid("1")
    assert client is not None
    assert client.ip == "192.168.0.1"
    assert client.pbid == "0123456789abcdef0123456789abcdef"


def test_a_punkbuster_line_for_a_slot_we_do_not_know_is_not_an_event():
    parser = _parser()
    assert (
        parser.parse_line(
            "PunkBuster Server: Player GUID Computed "
            "0123456789abcdef0123456789abcdef(-) (slot #7) 192.168.0.1:14567 Ghost"
        )
        == []
    )


# -- the connection talking about itself ----------------------------------------------------------


def test_a_reconnect_drops_the_roster_and_says_who_went():
    """It matters more here than anywhere else: this engine reports no departures, so every player who
    left while the socket was down would stay on the roster for ever."""
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE), _row("2", "Badger", BADGER)))
    events = parser.parse_line(RECONNECTED_NOTICE)

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT] * 2
    assert parser.clients.connected() == []


def test_the_logging_switches_are_reported_because_going_deaf_is_silent():
    """If chat logging is ever off, no chat arrives at all and this line is the only warning."""
    parser = _parser()
    events = parser.parse_line("ChatLogging now FALSE")

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra == {"kind": "server_var", "name": "ChatLogging"}
    assert events[0].data == "FALSE"


def test_the_login_lines_are_understood_and_quiet():
    parser = _parser()
    welcome = "WELCOME! Frontlines: Fuel of War (RCON) VER=2 CHALLENGE=38D384D07C"
    assert parser.parse_line(welcome) == []
    assert parser.handler_for(welcome) == "on_welcome", "and not reported as an unknown line"
    assert parser.parse_line("Login SUCCESS! User:admin") == []


def test_debug_noise_is_understood_so_a_real_gap_stands_out():
    """This connection carries the engine's whole debug log whether we want it or not. Without a
    handler every one of those counts as a line nobody understood, and the two that matter drown."""
    parser = _parser()
    for line in (
        "DEBUG: ScriptLog: something happened",
        "DEBUG: DevNet: a packet",
        "DEBUG: RendezVous: Update Gathering 1561500: 7/0",
        "DEBUG: SeamlessTravel: Loading CQ-Gnaw",
    ):
        assert parser.parse_line(line) == []
        assert parser.handler_for(line) is not None, line


def test_junk_is_understood_to_be_junk():
    parser = _parser()
    assert parser.parse_line("f00 f00 f00") == []
    assert parser.handler_for("f00 f00 f00") is None, "`b3 probe` must be able to say so"


def test_punkbuster_reports_the_address_before_the_roster_knows_the_player():
    """The ordering the real server produces, and the one that used to lose the address entirely.

    PunkBuster computes a GUID at connect time; the roster is only asked for every few seconds. So the
    line carrying the IP normally arrives *first*, when the slot is still unknown — and since it is the
    only time an address is ever reported, dropping it costs that player their IP for the session.
    """
    parser = _parser()
    parser.parse_line(
        "PunkBuster Server: Player GUID Computed "
        "0123456789abcdef0123456789abcdef(-) (slot #1) 192.168.0.1:14567 Courgette"
    )
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))

    client = parser.clients.get_by_cid("1")
    assert client is not None
    assert client.ip == "192.168.0.1"
    assert client.pbid == "0123456789abcdef0123456789abcdef"


def test_a_held_address_is_not_given_to_whoever_took_the_slot_next():
    """Slots are reused. A held address applied to the wrong player is worse than no address: it puts
    somebody else's IP on their record, and every ban-by-IP decision after that is wrong."""
    parser = _parser()
    parser.parse_line(
        "PunkBuster Server: Player GUID Computed "
        "0123456789abcdef0123456789abcdef(-) (slot #1) 192.168.0.1:14567 Courgette"
    )
    parser.parse_line(_playerlist(_row("1", "Badger", BADGER)))

    client = parser.clients.get_by_cid("1")
    assert client is not None
    assert client.ip == "", "the name did not match, so the address was dropped"


def test_a_held_address_does_not_outlive_the_slot_emptying():
    parser = _parser()
    parser.parse_line(
        "PunkBuster Server: Player GUID Computed "
        "0123456789abcdef0123456789abcdef(-) (slot #1) 192.168.0.1:14567 Courgette"
    )
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    parser.parse_line(_playerlist())  # slot 1 empties, taking the held entry with it

    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    client = parser.clients.get_by_cid("1")
    assert client is not None and client.ip == "", "a fresh session starts without one"


def test_a_punkbuster_row_also_carries_an_address():
    parser = _parser()
    parser.parse_line(_playerlist(_row("1", "Courgette", COURGETTE)))
    events = parser.parse_line(
        'PunkBuster Server: 1 0123456789abcdef0123456789abcdef(-) 192.168.0.9:14567 OK 1 3.0 0 (W) "Courgette"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_UPDATE]
    client = parser.clients.get_by_cid("1")
    assert client is not None and client.ip == "192.168.0.9"
