"""Insurgency's game log — every line below is a **captured** line, not an invented one.

Provenance: `tests/core/parsers/test_insurgency.py` in the classic tree (493 lines), which recorded
real lines from a real server, plus the CS:GO-era combat lines from `test_csgo.py` that share this
parser. TODO §4b: the portable thing in those files is the pair (input line -> expected event), not
the test code, so the pairs are re-expressed here against `SourceParser`.

Running the captures through the grammar *before* writing assertions found two gaps and one fault in
the classic parser, all recorded as tests below:

* ``switched from team`` carries a **four**-field identity, and the classic's pattern allowed three
  with a greedy id — so the id it captured had the team glued onto it.
* the ignore pattern for a ``//`` comment matched only a bare ``//``.
* the classic's own ignore list has ``(Disconnected\\.)`` with the parentheses unescaped, so the
  ``Dropped … from server`` line it meant to ignore could never match.
"""

from __future__ import annotations

from b3.core.events import EventType
from b3.parsers.source.parser import SourceParser
from b3.parsers.source.profiles import INSURGENCY, SOURCE_STATUS_ROW_RE
from b3.parsers.status import parse_cvar, parse_status, unparsed_rows

COURGETTE = "STEAM_1:0:1111111"

#: The captured ``status`` reply, verbatim from the classic test module. Note the padded header
#: labels, the two row shapes, and that ten of the eleven players are bots.
STATUS_REPLY = """\
hostname: Courgette's Server
version : 1.17.5.1/11751 5038 secure
udp/ip  : 11.23.32.44:27015  (public ip: 11.23.32.44)
os      :  Linux
type    :  community dedicated
map     : cs_foobar
players : 1 humans, 10 bots (20/20 max) (not hibernating)

# userid name uniqueid connected ping loss state rate adr
#224 "Moe" BOT active
#194 2 "courgette" STEAM_1:0:1111111 33:48 67 0 active 20000 11.222.111.222:27005
#225 "Quintin" BOT active
#226 "Kurt" BOT active
#227 "Arnold" BOT active
#228 "Rip" BOT active
#229 "Zach" BOT active
#230 "Wolf" BOT active
#231 "Minh" BOT active
#232 "Ringo" BOT active
#233 "Quade" BOT active
#end
L 08/28/2012 - 01:28:40: rcon from "11.222.111.222:4181": command "status"
"""


def _parser() -> SourceParser:
    return SourceParser(INSURGENCY)


def _joined(parser: SourceParser, cid: str = "194", name: str = "courgette", guid: str = COURGETTE):
    parser.parse_line(
        f'L 08/26/2012 - 03:22:36: "{name}<{cid}><{guid}><>" connected, address "11.222.111.222:27005"'
    )
    parser.parse_line(f'L 08/26/2012 - 03:22:40: "{name}<{cid}><{guid}><>" entered the game')
    client = parser.clients.get_by_cid(cid)
    assert client is not None
    return client


# -- the identity quad, and the fourth field that is not always a team --------------------------


def test_an_unknown_team_token_leaves_the_players_team_alone():
    """The captured tests state this outright, and it is not the obvious behaviour.

    After ``#Team_Security`` a token of ``f00`` must leave them on Security. It has to work this way
    because the fourth field of the quad is **not always a team**: a bot-stuck line puts a number
    there and a validation line repeats the Steam id. Treating "not in the table" as "no team" would
    keep wiping the team of everyone those lines mention.
    """
    parser = _parser()
    client = _joined(parser)

    parser.parse_line(
        f'L 04/01/2014 - 12:56:51: "courgette<194><{COURGETTE}><#Team_Security>" say "hi"'
    )
    assert client.team == "red"

    parser.parse_line(f'L 04/01/2014 - 12:56:51: "courgette<194><{COURGETTE}><f00>" say "hi"')
    assert client.team == "red", "an unrecognised token must not clear a known team"


def test_every_captured_team_token_maps_to_a_canonical_team():
    parser = _parser()
    client = _joined(parser)

    for token, expected in (
        ("#Team_Security", "red"),
        ("#Team_Insurgent", "blue"),
        ("Spectator", "spec"),
        ("#Team_Unassigned", ""),
    ):
        parser.parse_line(
            f'L 04/01/2014 - 12:56:51: "courgette<194><{COURGETTE}><{token}>" say "!pb @531 rule1"'
        )
        assert client.team == expected, token


def test_a_bot_stuck_line_puts_a_number_where_the_team_goes_and_is_understood():
    """Captured with **and** without the ``path_goal`` the engine sometimes appends to the same line.

    Matched rather than left to fall through: these arrive in bulk on a coop server, and an unmatched
    line is what `b3 probe` reports as a hole in the grammar.
    """
    parser = _parser()
    for line in (
        'L 02/07/2015 - 12:30:01: "Minh<338><BOT><193>" stuck (position "4355.51 -2602.87 143.98") '
        '(duration "19.97") L 02/07/2015 - 12:30:01:    path_goal ( "4370.00 -2703.29 148.56" )',
        'L 02/07/2015 - 14:53:25: "Abento<616><BOT><193>" stuck (position "-165.03 -705.59 222.38") '
        '(duration "1.50")',
    ):
        assert parser.handler_for(line) == "on_stuck"
        assert parser.parse_line(line) == []


# -- identity ----------------------------------------------------------------------------------


def test_a_bot_gets_no_identity_so_they_cannot_share_one_database_row():
    """``"Moe<3><BOT><>"`` — every AI player on the server reports the same ``BOT``."""
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:22:36: "Moe<3><BOT><>" connected, address "none"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_CONNECT]
    client = events[0].client
    assert client is not None
    assert client.guid == "", "a bot's shared id must never become an identity"
    assert client.ip == "", 'address "none" is not an address'


def test_a_human_connect_line_is_the_only_place_the_address_appears():
    parser = _parser()
    events = parser.parse_line(
        f'L 08/26/2012 - 03:22:36: "courgette<2><{COURGETTE}><>" connected, '
        'address "11.222.111.222:27005"'
    )

    client = events[0].client
    assert client is not None
    assert (client.cid, client.guid, client.ip) == ("2", COURGETTE, "11.222.111.222")


def test_a_name_holding_brackets_still_resolves():
    """Captured: ``"Ein 1337er M!L[H<106><STEAM_1:0:5555555><>" entered the game``."""
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 05:43:29: "Ein 1337er M!L[H<106><STEAM_1:0:5555555><>" entered the game'
    )

    assert [e.type for e in events] == [EventType.CLIENT_JOIN]
    client = events[0].client
    assert client is not None
    assert client.name == "Ein 1337er M!L[H"


def test_a_slot_reused_by_someone_else_does_not_inherit_the_previous_player():
    """Slots are recycled. Inheriting the record would hand over their level and their bans."""
    parser = _parser()
    first = _joined(parser, cid="194", name="courgette", guid=COURGETTE)
    assert first.guid == COURGETTE

    second = _joined(parser, cid="194", name="somebody", guid="STEAM_1:0:9999999")
    assert second is not first
    assert second.guid == "STEAM_1:0:9999999"


# -- chat --------------------------------------------------------------------------------------


def test_public_chat_carries_the_text_a_command_is_read_from():
    parser = _parser()
    client = _joined(parser)
    events = parser.parse_line(
        f'L 04/01/2014 - 12:56:51: "courgette<194><{COURGETTE}><#Team_Security>" say "!pb @531 rule1"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_SAY]
    assert events[0].data == "!pb @531 rule1"
    assert events[0].client is client


def test_team_chat_is_its_own_event():
    parser = _parser()
    _joined(parser, cid="2", guid="STEAM_1:0:1487018")
    events = parser.parse_line(
        'L 08/26/2012 - 05:04:44: "courgette<2><STEAM_1:0:1487018><#Team_Security>" '
        'say_team "team say"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_TEAM_SAY]
    assert events[0].data == "team say"


def test_the_console_talking_is_not_a_player_saying_something():
    """A bot that read its own console output as player speech would answer itself."""
    parser = _parser()
    assert parser.parse_line('L 08/26/2012 - 05:09:55: "Console<0><Console><Console>" say "hi"') == []


# -- leaving -----------------------------------------------------------------------------------


def test_a_disconnect_removes_the_player_and_reports_it():
    parser = _parser()
    client = _joined(parser)
    events = parser.parse_line(
        f'L 04/01/2014 - 12:56:51: "courgette<194><{COURGETTE}><#Team_Security>" '
        'disconnected (reason "Disconnected.")'
    )

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT]
    assert events[0].client is client
    assert parser.clients.get_by_cid("194") is None


def test_a_console_kick_reports_the_kick_as_well_as_the_disconnect():
    """``Kicked by Console`` is the stock console verb, so it is a genuine third-party penalty.

    The kick comes first because it is *why* the disconnect happened, and both carry a resolvable
    player — which is why the client is removed only after the events are built.
    """
    parser = _parser()
    client = _joined(parser)
    events = parser.parse_line(
        f'L 04/01/2014 - 12:56:51: "courgette<194><{COURGETTE}><#Team_Security>" '
        'disconnected (reason "Kicked by Console")'
    )

    assert [e.type for e in events] == [EventType.CLIENT_KICK, EventType.CLIENT_DISCONNECT]
    assert events[0].data == "Kicked by Console"
    assert all(e.client is client for e in events)


def test_an_ordinary_kick_reason_is_not_reported_as_a_kick():
    """Captured: ``disconnected (reason "to make room for a clan member")`` — not a penalty."""
    parser = _parser()
    _joined(parser, cid="479", name="JACKASS NINJA", guid="STEAM_1:0:87123453")
    events = parser.parse_line(
        'L 02/13/2015 - 18:08:14: "JACKASS NINJA<479><STEAM_1:0:87123453><#Team_Security>" '
        'disconnected (reason "to make room for a clan member")'
    )

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT]


# -- names and teams ---------------------------------------------------------------------------


def test_a_name_change_is_applied_and_published():
    parser = _parser()
    client = _joined(parser)
    events = parser.parse_line(
        f'L 02/07/2015 - 19:36:25: "courgette<194><{COURGETTE}><#Team_Security>" '
        'changed name to "fooobar"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_NAME_CHANGE]
    assert events[0].data == "fooobar"
    assert client.name == "fooobar"


def test_a_name_change_for_a_player_never_seen_join_still_reports_it():
    """A bot started mid-match has seen nobody connect, and the rename is still a fact."""
    parser = _parser()
    events = parser.parse_line(
        'L 02/07/2015 - 19:36:25: "courgette2<4><STEAM_1:0:1111112><#Team_Security>" '
        'changed name to "fooobar"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_NAME_CHANGE]
    assert events[0].data == "fooobar"


def test_joining_a_team_takes_the_team_from_the_message_not_the_quad():
    """The quad still holds the *old* team on this line."""
    parser = _parser()
    client = _joined(parser)
    events = parser.parse_line(
        f'L 08/26/2012 - 03:22:36: "courgette<194><{COURGETTE}><#Team_Unassigned>" '
        'joined team "#Team_Insurgent"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_TEAM_CHANGE]
    assert client.team == "blue"
    assert events[0].data == "blue"


def test_switched_from_team_carries_four_identity_fields_not_three():
    """The gap the captures found, and the classic parser's own fault.

    Captured: ``"courgette<194><STEAM_1:0:1111111><CT>" switched from team <TERRORIST> to
    <Unassigned>`` — four fields. The classic pattern allowed three and used a greedy id, so what it
    captured was ``STEAM_1:0:1111111><CT``: the Steam id with the team glued on. That id matches no
    database record, so a player switching teams could be created again under a corrupt one and lose
    their level and bans for the session.
    """
    parser = _parser()
    client = _joined(parser)
    line = (
        f'L 07/19/2013 - 17:18:44: "courgette<194><{COURGETTE}><#Team_Security>" '
        "switched from team <#Team_Security> to <#Team_Insurgent>"
    )

    assert parser.handler_for(line) == "on_switched_team"
    events = parser.parse_line(line)
    assert [e.type for e in events] == [EventType.CLIENT_TEAM_CHANGE]
    assert client.team == "blue"
    assert client.guid == COURGETTE, "the id must not absorb the team field"


def test_moving_to_no_team_does_not_resurrect_a_player_who_has_left():
    """The server writes this as somebody leaves, so it must not be the line that creates a client."""
    parser = _parser()
    line = (
        f'L 07/19/2013 - 17:18:44: "courgette<194><{COURGETTE}><#Team_Security>" '
        "switched from team <#Team_Security> to <#Team_Unassigned>"
    )

    assert parser.parse_line(line) == []
    assert parser.clients.get_by_cid("194") is None


def test_a_team_token_this_title_does_not_have_is_ignored_rather_than_reported():
    """``Unassigned`` is a CS:GO token; Insurgency says ``#Team_Unassigned``.

    No event, because nothing changed — a team-change event carrying the team they were already on
    would state that something happened when it did not.
    """
    parser = _parser()
    client = _joined(parser)
    parser.parse_line(
        f'L 08/26/2012 - 03:22:36: "courgette<194><{COURGETTE}><#Team_Security>" '
        'joined team "#Team_Security"'
    )
    assert client.team == "red"

    assert (
        parser.parse_line(
            f'L 08/26/2012 - 03:22:36: "courgette<194><{COURGETTE}><#Team_Security>" '
            'joined team "TERRORIST"'
        )
        == []
    )
    assert client.team == "red"


# -- combat ------------------------------------------------------------------------------------


def test_a_kill_reports_the_weapon_and_whether_it_was_a_headshot():
    parser = _parser()
    parser.parse_line('L 08/26/2012 - 03:00:00: "Pheonix<22><BOT><#Team_Insurgent>" entered the game')
    parser.parse_line('L 08/26/2012 - 03:00:00: "Ringo<17><BOT><#Team_Security>" entered the game')
    events = parser.parse_line(
        'L 08/26/2012 - 03:46:44: "Pheonix<22><BOT><#Team_Insurgent>" killed '
        '"Ringo<17><BOT><#Team_Security>" with "glock" (headshot)'
    )

    assert [e.type for e in events] == [EventType.CLIENT_KILL]
    assert events[0].data.weapon == "glock"
    assert events[0].data.hit_location == "head"


def test_a_kill_without_the_headshot_property_is_a_body_hit():
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:46:46: "Shark<19><BOT><#Team_Security>" killed '
        '"Pheonix<22><BOT><#Team_Insurgent>" with "hkp2000"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_KILL]
    assert events[0].data.hit_location == "body"


def test_a_kill_line_may_carry_coordinates_after_either_player():
    """Some builds append world coordinates. They are allowed for, and discarded."""
    parser = _parser()
    line = (
        'L 08/26/2012 - 03:46:44: "Pheonix<22><BOT><#Team_Insurgent>" [100 200 -30] killed '
        '"Ringo<17><BOT><#Team_Security>" [110 210 -30] with "glock" (headshot)'
    )

    events = parser.parse_line(line)
    assert [e.type for e in events] == [EventType.CLIENT_KILL]
    assert events[0].data.weapon == "glock"


def test_killing_a_team_mate_is_a_team_kill_only_when_both_teams_are_known():
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:46:44: "A<1><STEAM_1:0:1111><#Team_Security>" killed '
        '"B<2><STEAM_1:0:2222><#Team_Security>" with "glock"'
    )
    assert [e.type for e in events] == [EventType.CLIENT_KILL_TEAM]

    # Two unassigned players have the *same* empty team, and reporting that as a team kill would
    # punish half of every opening round.
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:46:44: "A<1><STEAM_1:0:1111><#Team_Unassigned>" killed '
        '"B<2><STEAM_1:0:2222><#Team_Unassigned>" with "glock"'
    )
    assert [e.type for e in events] == [EventType.CLIENT_KILL]


def test_killing_yourself_on_a_kill_line_is_a_suicide():
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:46:44: "A<1><STEAM_1:0:1111><#Team_Security>" killed '
        '"A<1><STEAM_1:0:1111><#Team_Security>" with "hegrenade"'
    )
    assert [e.type for e in events] == [EventType.CLIENT_SUICIDE]


def test_a_death_caused_by_the_map_is_a_suicide_with_the_world_as_the_weapon():
    """There is no world slot on this engine to attribute it to — the line names one player."""
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:38:04: "Pheonix<22><BOT><#Team_Insurgent>" committed suicide with "world"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_SUICIDE]
    assert events[0].data.weapon == "world"
    assert events[0].client is events[0].target


def test_an_assist_is_reported_against_both_players():
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:46:44: "Greg<3946><BOT><#Team_Security>" assisted killing '
        '"Dennis<3948><BOT><#Team_Insurgent>"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_ASSIST]
    assert events[0].client is not None and events[0].client.name == "Greg"
    assert events[0].target is not None and events[0].target.name == "Dennis"


# -- things players do -------------------------------------------------------------------------


def test_an_objective_action_is_published_with_its_name():
    parser = _parser()
    events = parser.parse_line(
        f'L 02/07/2015 - 12:31:34: "courgette<195><{COURGETTE}><#Team_Security>" '
        'triggered "obj_captured" (name "#unknown_controlpoint")'
    )

    assert [e.type for e in events] == [EventType.CLIENT_ACTION]
    assert events[0].data == "obj_captured"
    assert events[0].extra["name"] == "#unknown_controlpoint"


def test_an_action_the_classic_had_never_seen_is_still_published():
    """The classic filtered against a fixed list and warned about the rest, so a title with an
    objective it did not know produced nothing at all. ``CLIENT_ACTION`` carries the name, so passing
    an unrecognised one through is harmless and strictly more useful."""
    parser = _parser()
    events = parser.parse_line(
        f'L 02/07/2015 - 12:31:34: "courgette<195><{COURGETTE}><#Team_Security>" '
        'triggered "some_new_objective"'
    )

    assert [e.type for e in events] == [EventType.CLIENT_ACTION]
    assert events[0].data == "some_new_objective"


def test_a_purchase_and_a_throw_name_the_item():
    parser = _parser()
    bought = parser.parse_line(
        'L 08/26/2012 - 03:22:37: "Calvin<3942><BOT><#Team_Security>" purchased "p90"'
    )
    assert [e.type for e in bought] == [EventType.CLIENT_ACTION]
    assert (bought[0].data, bought[0].extra["item"]) == ("purchased", "p90")

    thrown = parser.parse_line(
        f'L 08/26/2012 - 03:22:37: "courgette<2><{COURGETTE}><#Team_Security>" '
        "threw molotov [59 386 -225]"
    )
    assert [e.type for e in thrown] == [EventType.CLIENT_ACTION]
    assert (thrown[0].data, thrown[0].extra["item"]) == ("threw", "molotov")


def test_a_clantag_is_published_rather_than_dropped():
    """There is no field on Client for it, and it is the only place the engine states a clan."""
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 03:22:37: "Pheonix<11><BOT><#Team_Insurgent>" triggered "clantag" '
        '(value "TAG")'
    )

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "clantag"
    assert events[0].data == "TAG"


# -- the match ---------------------------------------------------------------------------------


def test_the_gamerules_states_map_to_the_events_they_mean():
    """``GR_STATE_PREROUND`` maps to nothing on purpose: it is the countdown, and publishing a round
    start for it as well as for ``GR_STATE_RND_RUNNING`` would double every round."""
    parser = _parser()
    for state, expected in (
        ("GR_STATE_PREGAME", [EventType.GAME_WARMUP]),
        ("GR_STATE_STARTGAME", [EventType.GAME_ROUND_START]),
        ("GR_STATE_PREROUND", []),
        ("GR_STATE_RND_RUNNING", [EventType.GAME_ROUND_START]),
        ("GR_STATE_POSTROUND", [EventType.GAME_ROUND_END]),
        ("GR_STATE_GAME_OVER", [EventType.GAME_EXIT]),
    ):
        events = parser.parse_line(f"Gamerules: entering state '{state}'")
        assert [e.type for e in events] == expected, state


def test_a_round_start_from_gamerules_does_not_claim_a_map_change():
    """An empty payload means "a round started". A ``mapname`` in it would be read as a new map."""
    parser = _parser()
    events = parser.parse_line("Gamerules: entering state 'GR_STATE_RND_RUNNING'")
    assert events[0].data == {}


def test_the_three_ways_the_server_announces_a_map_all_report_it():
    parser = _parser()
    for line, expected in (
        ('L 08/26/2012 - 03:49:56: Loading map "de_nuke"', "de_nuke"),
        ("L 08/26/2012 - 03:49:56: -------- Mapchange to de_dust --------", "de_dust"),
        ('L 08/26/2012 - 03:22:35: Started map "de_dust" (CRC "1592693790")', "de_dust"),
    ):
        events = parser.parse_line(line)
        assert [e.type for e in events] == [EventType.GAME_ROUND_START], line
        assert events[0].data["mapname"] == expected


def test_a_team_trigger_that_ends_the_round_is_published_with_the_team():
    parser = _parser()
    events = parser.parse_line(
        'L 02/07/2015 - 12:53:23: Team "#Team_Insurgent" triggered "Round_Win"'
    )

    assert [e.type for e in events] == [EventType.GAME_ROUND_END]
    assert events[0].data["team"] == "blue"
    assert events[0].data["event_name"] == "Round_Win"


def test_an_unrecognised_team_trigger_does_not_end_the_round():
    """Gated where the *player* triggers are not, because this one drives logic: a spurious round end
    runs every end-of-round handler in the bot at a moment when the round did not end."""
    parser = _parser()
    assert parser.parse_line('L 02/07/2015 - 12:53:23: Team "#Team_Insurgent" triggered "Nonsense"') == []


def test_a_team_score_line_is_understood_and_recorded_nowhere():
    parser = _parser()
    line = 'L 08/26/2012 - 03:48:09: Team "#Team_Security" scored "3" with "5" players'
    assert parser.handler_for(line) == "on_team_score"
    assert parser.parse_line(line) == []


# -- cvars -------------------------------------------------------------------------------------


def test_a_cvar_line_is_read_including_spaces_and_empty_values():
    """All three shapes are captured, and the empty one is the awkward case."""
    parser = _parser()
    parser.parse_line('L 02/07/2015 - 14:48:24: "mp_lobbytime" = "10"')
    parser.parse_line('L 02/07/2015 - 14:48:24: "nextlevel" = "heights_coop checkpoint"')
    parser.parse_line('L 02/07/2015 - 14:48:24: "tv_password" = ""')

    assert parser.cvars["mp_lobbytime"] == "10"
    assert parser.cvars["nextlevel"] == "heights_coop checkpoint"
    assert parser.cvars["tv_password"] == ""


def test_a_server_cvar_line_is_read_the_same_way():
    parser = _parser()
    parser.parse_line('L 02/07/2015 - 14:48:24: server_cvar: "nextlevel" ""')
    assert parser.cvars["nextlevel"] == ""

    parser.parse_line(
        'L 02/07/2015 - 14:48:24: server_cvar: "sv_tags" "cid brutal coop bots hunt unforgiving"'
    )
    assert parser.cvars["sv_tags"] == "cid brutal coop bots hunt unforgiving"


def test_a_cvar_read_over_rcon_uses_an_equals_sign_on_this_engine():
    """The shared pattern insisted on the word ``is``, which every Source title spells ``=`` — so
    every cvar read here returned None, indistinguishable from a cvar the server does not have."""
    assert parse_cvar("sv_gravity", '"sv_gravity" = "800" ( def. "800" )') == "800"
    assert parse_cvar("tv_password", '"tv_password" = ""') == ""


# -- the server talking about itself -----------------------------------------------------------


def test_a_refused_rcon_password_is_reported():
    parser = _parser()
    events = parser.parse_line(
        'L 08/26/2012 - 05:21:23: rcon from "78.207.134.100:15073": Bad Password'
    )

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "bad_rcon_password"


def test_the_echo_of_our_own_rcon_command_is_understood_and_ignored():
    parser = _parser()
    line = 'L 08/26/2012 - 05:37:56: rcon from "11.222.111.122:15349": command "say test"'
    assert parser.handler_for(line) == "on_rcon"
    assert parser.parse_line(line) == []


def test_a_server_side_ban_is_custom_rather_than_the_bots_own_ban_event():
    """The server echoes the bot's own ``sm_addban`` down this log, so reusing ``CLIENT_BAN_TEMP``
    — which the bot publishes when it bans somebody — would record every penalty twice."""
    parser = _parser()
    events = parser.parse_line(
        f'L 08/28/2012 - 00:03:01: Banid: "courgette<91><{COURGETTE}><>" was banned '
        '"for 1.00 minutes" by "Console"'
    )

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "server_ban"
    assert events[0].extra["duration"] == "for 1.00 minutes"
    assert events[0].extra["admin"] == "Console"


def test_a_sourcemod_kick_announcement_is_custom_for_the_same_reason():
    parser = _parser()
    events = parser.parse_line(
        f'L 08/28/2012 - 00:03:01: [basecommands.smx] "admin<1><{COURGETTE}><>" kicked '
        f'"bob<91><STEAM_1:0:2222222><>" (reason "spam")'
    )

    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "server_kick"
    assert events[0].data == "spam"


def test_the_server_asking_to_be_restarted_is_reported():
    parser = _parser()
    for line in (
        "L 09/17/2012 - 23:26:45: Your server needs to be restarted in order to receive the latest "
        "update.",
        "L 09/17/2012 - 23:26:45: Your server is out of date.  Please update and restart.",
    ):
        events = parser.parse_line(line)
        assert [e.type for e in events] == [EventType.CUSTOM], line
        assert events[0].extra["kind"] == "restart_required"


# -- lines that are understood and uninteresting -----------------------------------------------


def test_every_captured_noise_line_is_matched_so_probe_does_not_call_it_a_gap():
    """An unmatched line is indistinguishable from a quiet server, which is the failure mode this
    project keeps finding. So the noise is matched deliberately rather than left to fall through."""
    parser = _parser()
    for line in (
        "L 02/07/2015 - 15:15:22: Log file closed",
        'L 02/07/2015 - 15:15:24: Log file started (file "logs\\L023.log") '
        '(game "C:\\servers\\insurgency") (version "5885")',
        'L 02/07/2015 - 14:53:26:    path_goal ( "-162.50 -750.00 182.68" )',
        'L 02/07/2015 - 19:13:07: Vote succeeded "Kick Based God Allah [U:1:120000090]"',
        'L 02/13/2015 - 18:08:52: "courgette<18><STEAM_1:1:1111111><STEAM_1:1:1111111>" '
        "STEAM USERID validated",
        "L 08/26/2012 - 05:29:47: server cvars start",
        "L 08/26/2012 - 05:29:47: server cvars end",
        "L 09/24/2001 - 18:44:50: // This is a comment in the log file.",
        'L 08/30/2012 - 00:43:10: server_message: "quit"',
        "L 08/26/2012 - 03:22:37: [META] Loaded 3 plugins (1 already loaded)",
        "L 08/26/2012 - 03:22:37: [basechat.smx] something happened",
        "L 02/07/2015 - 12:00:00: Dropped courgette from server (Disconnected.)",
        "L 08/26/2012 - 03:22:37: Molotov projectile spawned at 1.0 2.0 3.0, velocity 4.0 5.0 6.0",
    ):
        assert parser.handler_for(line) is not None, line
        assert parser.parse_line(line) == [], line


def test_the_dropped_from_server_line_the_classic_could_never_have_ignored():
    """Its ignore pattern was ``^Dropped .+ from server (Disconnected\\.)$`` with the parentheses
    **unescaped**, making them a group — so it could only match ``…from serverDisconnected.``, a line
    no server writes. That line was reported as unhandled for the whole life of the parser."""
    parser = _parser()
    assert parser.handler_for("L 02/07/2015 - 12:00:00: Dropped courgette from server (Disconnected.)")


# -- the status table --------------------------------------------------------------------------


def test_the_status_reply_yields_every_player_including_the_bots():
    """The reason one pattern has to match both row shapes.

    Candidate patterns are tried until one yields *any* row, so a pattern for human rows alone would
    win on this reply and hand back a roster of one — which `Bot._reconcile` reads as "the other ten
    left" and disconnects every bot, every sync.
    """
    map_name, players = parse_status(STATUS_REPLY, (SOURCE_STATUS_ROW_RE,), "guid")

    assert map_name == "cs_foobar"
    assert len(players) == 11, "one human and ten bots"
    assert [p.name for p in players][:3] == ["Moe", "courgette", "Quintin"]


def test_the_human_row_yields_the_slot_the_log_lines_use():
    _, players = parse_status(STATUS_REPLY, (SOURCE_STATUS_ROW_RE,), "guid")
    human = next(p for p in players if p.name == "courgette")

    # `#194` is the id the log writes as `<194>`; the second number on the row is not in the table's
    # own header and the classic parser threw it away too.
    assert human.cid == "194"
    assert human.guid == COURGETTE
    assert human.ip == "11.222.111.222"
    assert human.port == 27005
    assert human.ping == 67


def test_a_bot_row_has_no_ping_rate_or_address_and_still_parses():
    _, players = parse_status(STATUS_REPLY, (SOURCE_STATUS_ROW_RE,), "guid")
    bot = next(p for p in players if p.name == "Moe")

    assert (bot.cid, bot.guid, bot.ip, bot.ping) == ("224", "BOT", "", 0)


def test_the_padded_header_label_is_why_the_map_pattern_stopped_insisting_on_a_bare_colon():
    """``map     : cs_foobar``. Anchored on ``map:``, the shared pattern found no map at all here and
    `!map` would report nothing while the reply plainly said otherwise."""
    map_name, _ = parse_status(STATUS_REPLY, (SOURCE_STATUS_ROW_RE,), "guid")
    assert map_name == "cs_foobar"


def test_the_spawn_position_current_builds_append_is_not_taken_as_the_map_name():
    map_name, _ = parse_status(
        "map     : de_dust2 at: 0 x, 0 y, 0 z\n", (SOURCE_STATUS_ROW_RE,), "guid"
    )
    assert map_name == "de_dust2"


def test_a_source_table_this_bot_cannot_read_is_reported_as_rows_not_as_an_empty_server():
    """The two answers want opposite responses from an operator, and the ``#``-led rows are a shape
    the Tech 3 data-row pattern recognises none of."""
    unreadable = "#194 3 4 5 nonsense with no quoted name\n"
    assert unparsed_rows(unreadable, (SOURCE_STATUS_ROW_RE,)) == [unreadable.strip()]


# -- answers to questions ----------------------------------------------------------------------


def test_the_maps_reply_is_parsed_into_the_maps_installed():
    parser = _parser()
    reply = (
        "PENDING:  (fs) buhriz.bsp\n"
        "PENDING:  (fs) district.bsp\n"
        "PENDING:  (fs) sinjar.bsp\n"
        "some other line\n"
    )

    assert parser.read_maps(reply) == ["buhriz", "district", "sinjar"]


def test_the_profile_names_a_next_map_cvar_because_the_map_list_is_not_a_rotation():
    """``maps *`` is everything installed, in filesystem order. Deriving "the next map" from that
    order would state a falsehood about the rotation rather than decline to answer."""
    assert INSURGENCY.rotation_cvar == ""
    assert INSURGENCY.maplist_command == "maps *"
    assert INSURGENCY.next_map_cvar == "sm_nextmap"
