"""Altitude's JSON log, and the five things the classic parser got wrong in it.

Each of those is marked in the test that pins it: the join fired on the wrong line, the map change
was published as a warmup, a vote raised `KeyError`, the round-end awards were overwritten by the
stats, and a structure name was put where every other event carries a Client.
"""

from __future__ import annotations

import json

import pytest

from b3.core.events import EventType
from b3.parsers.altitude.parser import NOBODY, AltParser, KillData
from b3.parsers.altitude.profiles import ALTITUDE

PORT = 27276
ADMIN_ID = "a8654321-123a-414e-c71a-123123123131"
BOB_ID = "d6545616-17a3-4044-a74b-121231321321"


def _parser(port: int = PORT) -> AltParser:
    parser = AltParser(ALTITUDE)
    parser.port = port
    return parser


def _line(**fields: object) -> str:
    return json.dumps({"port": PORT, "time": 12345, **fields})


def _join(parser: AltParser, slot: int, name: str, vapor_id: str, ip: str = "10.0.0.5") -> None:
    parser.parse_line(
        _line(
            type="clientAdd",
            player=slot,
            nickname=name,
            vaporId=vapor_id,
            ip=f"{ip}:27272",
            level=9,
            aceRank=0,
        )
    )


# -- identity -----------------------------------------------------------------------------------


def test_a_connection_is_the_join_because_the_identity_arrives_with_it():
    """The classic parser fired the join on *spawn*, so a banned player sitting in spectator was
    never checked against the database at all."""
    parser = _parser()
    events = parser.parse_line(
        _line(
            type="clientAdd",
            player=2,
            nickname="Courgette",
            vaporId=ADMIN_ID,
            ip="192.168.10.1:27272",
            level=9,
            aceRank=0,
        )
    )
    assert [e.type for e in events] == [EventType.CLIENT_JOIN]
    client = events[0].client
    assert client is not None
    assert (client.cid, client.name, client.guid) == ("2", "Courgette", ADMIN_ID)
    assert client.ip == "192.168.10.1"  # the port the log appends is not part of the address
    assert parser.clients.get_by_cid("2") is client


def test_a_bot_player_occupies_a_slot_but_gets_no_identity():
    """Every AI shares the all-zero vapor id, so authenticating on it would merge them all into one
    database row — with one shared level and one shared ban history."""
    parser = _parser()
    _join(parser, 3, "Rookie AI", NOBODY)
    client = parser.clients.get_by_cid("3")
    assert client is not None
    assert client.name == "Rookie AI"
    assert client.guid == ""


def test_a_spawn_is_a_spawn_and_never_invents_a_player():
    parser = _parser()
    events = parser.parse_line(
        _line(
            type="spawn",
            player=1,
            team=3,
            plane="Biplane",
            skin="No Skin",
            perkRed="Heavy Cannon",
            perkGreen="Heavy Armor",
            perkBlue="Reverse Thrust",
        )
    )
    # Nobody joined, so there is nothing to report and nothing to fabricate.
    assert events == []
    assert parser.clients.get_by_cid("1") is None

    _join(parser, 1, "Bob", BOB_ID)
    events = parser.parse_line(
        _line(
            type="spawn",
            player=1,
            team=3,
            plane="Biplane",
            skin="",
            perkRed="",
            perkGreen="",
            perkBlue="",
        )
    )
    assert [e.type for e in events] == [EventType.CLIENT_SPAWN]
    assert events[0].extra["plane"] == "Biplane"


def test_a_late_departure_line_does_not_disconnect_whoever_is_in_the_slot_now():
    """Keyed on the slot, so a `clientRemove` that arrives after the slot was retaken would drop the
    wrong player — and with no roster query on this engine, nothing would put them back. The classic
    parser looked players up *by* vapor id, which made it immune; the identity is verified instead."""
    parser = _parser()
    _join(parser, 1, "Bob", BOB_ID)
    parser.parse_line(
        _line(type="clientRemove", player=1, nickname="Bob", vaporId=BOB_ID, reason="Client left.")
    )
    _join(parser, 1, "Courgette", ADMIN_ID)  # same slot, different player

    events = parser.parse_line(
        _line(type="clientRemove", player=1, nickname="Bob", vaporId=BOB_ID, reason="Client left.")
    )

    assert events == []
    client = parser.clients.get_by_cid("1")
    assert client is not None and client.guid == ADMIN_ID  # still there


def test_a_slot_taken_over_without_a_departure_line_reports_the_departure_too():
    """A dropped `clientRemove` used to make the previous occupant vanish from the client store with
    no event at all — and on this engine the five-minute sync cannot notice, because there is no
    roster to compare against."""
    parser = _parser()
    _join(parser, 1, "Bob", BOB_ID)

    events = parser.parse_line(
        _line(
            type="clientAdd",
            player=1,
            nickname="Courgette",
            vaporId=ADMIN_ID,
            ip="10.0.0.6:27272",
            level=9,
            aceRank=0,
        )
    )

    assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT, EventType.CLIENT_JOIN]
    assert events[0].client is not None and events[0].client.guid == BOB_ID
    assert events[0].extra["inferred"] is True  # nothing said so; we worked it out
    assert events[1].client is not None and events[1].client.guid == ADMIN_ID
    assert parser.clients.get_by_cid("1") is events[1].client


def test_the_same_player_reconnecting_into_their_own_slot_is_not_a_departure():
    parser = _parser()
    _join(parser, 1, "Bob", BOB_ID)
    events = parser.parse_line(
        _line(
            type="clientAdd",
            player=1,
            nickname="Bob",
            vaporId=BOB_ID,
            ip="10.0.0.5:27272",
            level=9,
            aceRank=0,
        )
    )
    assert [e.type for e in events] == [EventType.CLIENT_JOIN]


def test_a_departure_is_always_a_departure_whatever_the_reason_says():
    """The two known reasons are named, and an unrecognised one still empties the slot — a roster
    that only forgets players for reasons it recognises keeps ghosts forever."""
    parser = _parser()
    for slot, reason, vote_kick in (
        (1, "Client left.", False),
        (2, "Kicked by vote.", True),
        (3, "Something nobody has ever logged.", False),
    ):
        _join(parser, slot, f"P{slot}", f"{slot}{BOB_ID[1:]}")
        events = parser.parse_line(
            _line(
                type="clientRemove", player=slot, nickname=f"P{slot}", reason=reason, message="left"
            )
        )
        assert [e.type for e in events] == [EventType.CLIENT_DISCONNECT]
        assert events[0].extra["vote_kick"] is vote_kick
        assert parser.clients.get_by_cid(str(slot)) is None


# -- one log file, several servers ---------------------------------------------------------------


def test_another_servers_lines_are_not_ours():
    """An Altitude installation writes every server it runs into one log file."""
    parser = _parser(port=PORT)
    neighbour = json.dumps(
        {
            "port": 27999,
            "time": 1,
            "type": "clientAdd",
            "player": 1,
            "nickname": "Stranger",
            "vaporId": BOB_ID,
            "ip": "10.0.0.9:27272",
            "level": 1,
            "aceRank": 0,
        }
    )
    assert parser.parse_line(neighbour) == []
    assert parser.clients.get_by_cid("1") is None


def test_with_no_port_configured_nothing_is_filtered():
    """Refusing every line would be the silent "the bot sees nothing" failure, so an unset port
    means "read it all" — which is also correct for a single-server box."""
    parser = _parser(port=0)
    line = json.dumps(
        {
            "port": 27999,
            "time": 1,
            "type": "clientAdd",
            "player": 1,
            "nickname": "Stranger",
            "vaporId": BOB_ID,
            "ip": "10.0.0.9:27272",
            "level": 1,
            "aceRank": 0,
        }
    )
    assert [e.type for e in parser.parse_line(line)] == [EventType.CLIENT_JOIN]


# -- chat ---------------------------------------------------------------------------------------


def test_a_player_talking_is_a_say():
    parser = _parser()
    _join(parser, 2, "Courgette", ADMIN_ID)
    events = parser.parse_line(_line(type="chat", player=2, message="!help", server=False))
    assert [e.type for e in events] == [EventType.CLIENT_SAY]
    assert events[0].data == "!help"


def test_the_servers_own_message_is_never_read_as_a_player_talking():
    """Everything the bot says is echoed back into this log. Read as chat, the bot would obey its
    own announcements — this is the flag that stops it."""
    parser = _parser()
    _join(parser, 2, "Courgette", ADMIN_ID)
    events = parser.parse_line(
        _line(type="chat", player=-1, message="!permban Courgette", server=True)
    )
    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra["kind"] == "server_message"
    assert events[0].client is None


# -- combat -------------------------------------------------------------------------------------


def test_a_kill_carries_what_this_engine_actually_reports():
    """No damage figure and no hit location exist on this engine; the classic parser passed a
    hard-coded 100 damage, which was invented."""
    parser = _parser()
    _join(parser, 0, "Courgette", ADMIN_ID)
    _join(parser, 1, "Bob", BOB_ID)
    events = parser.parse_line(
        _line(type="kill", player=1, victim=0, source="missile", xp=10, currentStreak=3, multi=2)
    )
    assert [e.type for e in events] == [EventType.CLIENT_KILL]
    assert events[0].data == KillData(weapon="missile", xp=10, streak=3, multi=2)
    assert events[0].client is not None and events[0].client.cid == "1"
    assert events[0].target is not None and events[0].target.cid == "0"


@pytest.mark.parametrize("attacker", [-1, 0])
def test_a_kill_with_no_real_killer_is_a_suicide(attacker):
    """`player: -1` is a plane flown into the ground. The classic parser had a hidden "WORLD" client
    in its store to receive the credit for those."""
    parser = _parser()
    _join(parser, 0, "Courgette", ADMIN_ID)
    events = parser.parse_line(_line(type="kill", player=attacker, victim=0, source="plane", xp=0))
    assert [e.type for e in events] == [EventType.CLIENT_SUICIDE]
    assert events[0].client is events[0].target


def test_no_team_kill_is_ever_reported_even_between_teammates():
    """Friendly fire is off in this game, so the engine does not report one — and the parser does not
    infer one from the team ids either."""
    parser = _parser()
    _join(parser, 0, "Courgette", ADMIN_ID)
    _join(parser, 1, "Bob", BOB_ID)
    for slot in (0, 1):
        parser.parse_line(_line(type="teamChange", player=slot, team=4))
    events = parser.parse_line(_line(type="kill", player=1, victim=0, source="missile", xp=10))
    assert [e.type for e in events] == [EventType.CLIENT_KILL]


def test_an_assist_names_both_players():
    parser = _parser()
    _join(parser, 0, "Courgette", ADMIN_ID)
    _join(parser, 1, "Bob", BOB_ID)
    events = parser.parse_line(_line(type="assist", player=1, victim=0, xp=8))
    assert [e.type for e in events] == [EventType.CLIENT_ACTION]
    assert events[0].data == "assist"
    assert events[0].target is not None and events[0].target.cid == "0"


# -- teams --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(2, "spec"), (3, "red"), (4, "blue"), (8, "purple"), (0, ""), (1, ""), (99, "")],
)
def test_team_ids_become_names_and_placeholders_become_no_team(raw, expected):
    """The classic parser flattened every colour to "no team", so nothing could tell red from blue
    in a game with a two-team ball mode. 0 and 1 stay empty for Frostbite's reason: a placeholder
    treated as a team makes everyone who has not picked one look like teammates."""
    parser = _parser()
    _join(parser, 1, "Bob", BOB_ID)
    events = parser.parse_line(_line(type="teamChange", player=1, team=raw))
    assert [e.type for e in events] == [EventType.CLIENT_TEAM_CHANGE]
    client = parser.clients.get_by_cid("1")
    assert client is not None and client.team == expected


# -- objectives ---------------------------------------------------------------------------------


def test_powerups_and_objectives_report_what_happened():
    parser = _parser()
    _join(parser, 1, "Bob", BOB_ID)
    pickup = parser.parse_line(
        _line(
            type="powerupPickup",
            player=1,
            powerup="Homing Missile",
            positionX=3572.41,
            positionY=639.57,
        )
    )
    assert [e.type for e in pickup] == [EventType.CLIENT_ITEM_PICKUP]
    assert pickup[0].data == "Homing Missile"
    assert pickup[0].extra["position"] == (3572.41, 639.57)

    auto = parser.parse_line(
        _line(type="powerupAutoUse", player=1, powerup="Health", positionX=1, positionY=2)
    )
    assert [e.type for e in auto] == [EventType.CLIENT_ITEM_PICKUP]

    used = parser.parse_line(
        _line(type="powerupUse", player=1, powerup="Homing Missile", positionX=1, positionY=2)
    )
    assert [e.type for e in used] == [EventType.CUSTOM]
    assert used[0].extra["kind"] == "powerup_use"

    defused = parser.parse_line(
        _line(type="powerupDefuse", player=1, powerup="Bomb", positionX=1, positionY=2, xp=10)
    )
    assert [e.type for e in defused] == [EventType.CLIENT_ACTION]
    assert defused[0].data == "defuse"

    goal = parser.parse_line(_line(type="goal", player=1, xp=50, assister=-1))
    assert [e.type for e in goal] == [EventType.CLIENT_ACTION]
    assert goal[0].data == "goal"
    assert goal[0].target is None  # -1 is "nobody assisted", not a player


def test_a_structure_is_not_a_client():
    """The classic parser put "base"/"turret" in the event's `target`, which every other event uses
    for a Client — so any plugin reading `event.target.name` raised on it."""
    parser = _parser()
    _join(parser, 3, "Bob", BOB_ID)
    for etype, action in (
        ("structureDamage", "structure_damage"),
        ("structureDestroy", "structure_destroy"),
    ):
        events = parser.parse_line(_line(type=etype, player=3, target="turret", xp=31))
        assert [e.type for e in events] == [EventType.CLIENT_ACTION]
        assert events[0].data == action
        assert events[0].target is None
        assert events[0].extra["structure"] == "turret"


# -- the match ----------------------------------------------------------------------------------


def test_a_map_change_is_a_round_start_so_the_map_is_actually_recorded():
    """The classic parser published a *warmup* here, so nothing ever recorded the map: `!map` and
    every plugin asking what was being played got the previous answer forever."""
    parser = _parser()
    events = parser.parse_line(_line(type="mapChange", map="ball_grotto", mode="ball"))
    assert [e.type for e in events] == [EventType.GAME_ROUND_START]
    assert events[0].data == {"mapname": "ball_grotto", "g_gametype": "ball"}


def test_map_loading_is_the_warmup():
    parser = _parser()
    events = parser.parse_line(_line(type="mapLoading", map="ball_cave"))
    assert [e.type for e in events] == [EventType.GAME_WARMUP]
    assert events[0].data == "ball_cave"


def test_round_end_stats_are_matched_up_to_their_players_and_the_awards_survive():
    """The classic parser stored `participantStatsByName` under the `winnerByAward` key as well, so
    the awards were lost to a copy-paste."""
    parser = _parser()
    events = parser.parse_line(
        _line(
            type="roundEnd",
            participants=[0, 1],
            participantStatsByName={"Kills": [22, 12], "Deaths": [8, 35]},
            winnerByAward={"Most Deadly": 0},
        )
    )
    assert [e.type for e in events] == [EventType.GAME_ROUND_END]
    assert events[0].data == {"0": {"Kills": 22, "Deaths": 8}, "1": {"Kills": 12, "Deaths": 35}}
    assert events[0].extra["awards"] == {"Most Deadly": 0}


def test_server_init_reports_the_name_and_the_player_limit():
    parser = _parser()
    events = parser.parse_line(_line(type="serverInit", name="Test Server", maxPlayerCount=14))
    assert [e.type for e in events] == [EventType.SERVER_INFO]
    assert events[0].data == "Test Server"
    assert events[0].extra["max_players"] == 14


# -- console commands ---------------------------------------------------------------------------


def test_a_kick_vote_is_reported_with_its_arguments():
    """The classic handler read `vaporId` from an event whose field is called `source`, so every
    vote raised a KeyError instead of firing — the anti-admin-kick-vote feature never ran once."""
    parser = _parser()
    _join(parser, 2, "Courgette", ADMIN_ID)
    events = parser.parse_line(
        _line(
            type="consoleCommandExecute",
            command="vote",
            source=ADMIN_ID,
            arguments=["kick", "Bob"],
            group="Anonymous",
        )
    )
    assert [e.type for e in events] == [EventType.CLIENT_CALLVOTE]
    assert events[0].extra["arguments"] == ["kick", "Bob"]
    assert events[0].client is not None and events[0].client.cid == "2"


def test_a_ballot_is_a_vote():
    parser = _parser()
    _join(parser, 2, "Courgette", ADMIN_ID)
    events = parser.parse_line(
        _line(type="consoleCommandExecute", command="castBallot", source=ADMIN_ID, arguments=["1"])
    )
    assert [e.type for e in events] == [EventType.CLIENT_VOTE]
    assert events[0].data == "1"


def test_commands_the_server_ran_itself_are_ignored():
    """Which includes every command this bot sends: the bot must not react to its own kicks."""
    parser = _parser()
    _join(parser, 2, "Courgette", ADMIN_ID)
    assert (
        parser.parse_line(
            _line(type="consoleCommandExecute", command="kick", source=NOBODY, arguments=["Bob"])
        )
        == []
    )


def test_an_unrecognised_console_command_is_still_reported():
    parser = _parser()
    _join(parser, 2, "Courgette", ADMIN_ID)
    events = parser.parse_line(
        _line(
            type="consoleCommandExecute",
            command="changeMap",
            source=ADMIN_ID,
            arguments=["ball_cave"],
        )
    )
    assert [e.type for e in events] == [EventType.CUSTOM]
    assert events[0].extra == {"kind": "console_command", "arguments": ["ball_cave"]}


# -- the roster this engine cannot be asked about -----------------------------------------------


def test_the_roster_is_never_smaller_than_what_the_log_said():
    """The classic parser answered `getPlayerList` from the last ping report, and its `sync` then
    disconnected anyone missing from it — so a player who joined between two reports was silently
    forgotten, losing their level and ban state."""
    parser = _parser()
    _join(parser, 0, "Courgette", ADMIN_ID)
    parser.parse_line(_line(type="pingSummary", pingByPlayer={"0": 42}))
    _join(parser, 1, "Bob", BOB_ID)  # joined *after* the last report

    players = parser.get_players()
    assert {p.cid for p in players} == {"0", "1"}
    assert next(p.ping for p in players if p.cid == "0") == 42
    assert next(p.ping for p in players if p.cid == "1") == 0  # not reported yet, not a claim


def test_a_ping_for_an_unknown_slot_does_not_invent_a_player():
    """There is no roster query on this engine, so a player who was already flying when the bot
    started cannot be given a name or an identity — and a guid-less client would be a database row
    that could never be matched to the same person again."""
    parser = _parser()
    _join(parser, 0, "Courgette", ADMIN_ID)
    parser.parse_line(_line(type="pingSummary", pingByPlayer={"0": 42, "7": 150}))
    assert {p.cid for p in parser.get_players()} == {"0"}


# -- robustness ---------------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["", "   ", "not json at all", "[1, 2, 3]", "null"])
def test_a_line_that_is_not_one_of_ours_is_ignored_without_raising(line):
    assert _parser().parse_line(line) == []


def test_an_event_with_no_type_is_ignored():
    assert _parser().parse_line(json.dumps({"port": PORT, "time": 1})) == []


def test_an_unknown_event_type_is_ignored():
    assert _parser().parse_line(_line(type="somethingNew", player=1)) == []


def test_fields_missing_from_an_event_do_not_raise():
    """A real server has emitted more shapes than any capture shows, and half a line is not worth a
    crash on the event loop."""
    parser = _parser()
    _join(parser, 1, "Bob", BOB_ID)
    for etype in (
        "kill",
        "assist",
        "spawn",
        "teamChange",
        "chat",
        "goal",
        "roundEnd",
        "pingSummary",
    ):
        parser.parse_line(_line(type=etype))  # no player, no victim, no anything
