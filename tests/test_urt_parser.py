"""Urban Terror's own log lines.

Every sample line here is one the classic ``iourt41.py``/``iourt42.py`` carries in its comments —
captured from real servers by the people who wrote it — rather than a shape invented to match the
regex under test. What is *not* verified is a live UrT server end to end; see TODO.md §1.3.

The hit line is the one that matters: it is what makes a team-kill plugin possible on this game.
"""

from __future__ import annotations

import pytest

from b3.core.events import EventType
from b3.domain.client import Client
from b3.parsers.cod.parser import KillData
from b3.parsers.q3.profiles import IOURT42
from b3.parsers.q3.urt import UrtParser


@pytest.fixture
def parser() -> UrtParser:
    p = UrtParser(IOURT42)
    # Two players already known: a Hit line names nobody, only slots.
    p.clients.add(Client(cid="12", name="BSTHanzo[FR]", team="red"))
    p.clients.add(Client(cid="7", name="ercan", team="blue"))
    return p


def one(p: UrtParser, line: str):  # noqa: ANN201
    events = p.parse_line(line)
    assert len(events) == 1, f"expected 1 event from {line!r}, got {events!r}"
    return events[0]


# -- hits ------------------------------------------------------------------


def test_a_hit_on_an_enemy():
    """`Hit: 12 7 1 19: BSTHanzo[FR] hit ercan in the Helmet` — victim 12, attacker 7."""
    p = UrtParser(IOURT42)
    p.clients.add(Client(cid="12", name="BSTHanzo[FR]", team="red"))
    p.clients.add(Client(cid="7", name="ercan", team="blue"))

    ev = one(p, "Hit: 12 7 1 19: BSTHanzo[FR] hit ercan in the Helmet")

    assert ev.type is EventType.CLIENT_DAMAGE
    assert ev.client.cid == "7"  # the attacker is the SECOND slot on a Hit line
    assert ev.target.cid == "12"
    assert isinstance(ev.data, KillData)


def test_the_hit_weapon_id_is_translated_to_the_kill_weapon(parser):  # noqa: ANN001
    """Weapon 19 on a Hit line is the M4; on a Kill line 19 is the LR300. Same number, not same gun."""
    ev = one(parser, "Hit: 12 7 1 19: BSTHanzo[FR] hit ercan in the Helmet")
    assert ev.data.weapon == "38"  # UT_MOD_M4


def test_the_damage_comes_from_the_weapon_and_hit_location(parser):  # noqa: ANN001
    """A Hit line carries no damage figure at all, so the table *is* the damage."""
    helmet = one(parser, "Hit: 12 7 1 19: BSTHanzo[FR] hit ercan in the Helmet")
    head = one(parser, "Hit: 12 7 0 19: BSTHanzo[FR] hit ercan in the Head")

    assert helmet.data.damage == 51  # M4, helmet
    assert head.data.damage == 100  # M4, head
    assert (helmet.data.hit_location, head.data.hit_location) == ("helmet", "head")


def test_an_unknown_weapon_still_produces_a_hit(parser):  # noqa: ANN001
    """A mod or a new gun must not silence hit detection — a team-kill plugin needs the event."""
    ev = one(parser, "Hit: 12 7 2 99: BSTHanzo[FR] hit ercan in the Torso")
    assert ev.type is EventType.CLIENT_DAMAGE
    assert ev.data.damage == 15  # the classic fallback


def test_a_hit_on_a_team_mate():
    p = UrtParser(IOURT42)
    p.clients.add(Client(cid="1", name="Victim", team="red"))
    p.clients.add(Client(cid="2", name="Attacker", team="red"))

    ev = one(p, "Hit: 1 2 2 8: Attacker hit Victim in the Torso")

    assert ev.type is EventType.CLIENT_DAMAGE_TEAM
    assert ev.client.cid == "2" and ev.target.cid == "1"


def test_a_hit_on_yourself():
    p = UrtParser(IOURT42)
    p.clients.add(Client(cid="3", name="Clumsy", team="blue"))
    ev = one(p, "Hit: 3 3 5 21: Clumsy hit Clumsy in the Legs")
    assert ev.type is EventType.CLIENT_DAMAGE_SELF


def test_a_hit_naming_a_slot_the_bot_does_not_know_is_dropped(parser):  # noqa: ANN001
    """A hit is a poor moment to invent a player, and the classic parser dropped it too."""
    assert parser.parse_line("Hit: 12 99 1 19: BSTHanzo[FR] hit nobody in the Helmet") == []


# -- radio and voting ------------------------------------------------------


def test_a_radio_call(parser):  # noqa: ANN001
    ev = one(parser, 'Radio: 7 - 7 - 2 - "New Alley" - "I\'m going for the flag"')
    assert ev.type is EventType.CLIENT_RADIO
    assert ev.client.cid == "7"
    assert ev.data == {
        "msg_group": "7",
        "msg_id": "2",
        "location": "New Alley",
        "text": "I'm going for the flag",
    }


def test_a_callvote(parser):  # noqa: ANN001
    ev = one(parser, 'Callvote: 7 - "map dressingroom"')
    assert ev.type is EventType.CLIENT_CALLVOTE
    assert ev.data == "map dressingroom"


def test_a_vote_cast(parser):  # noqa: ANN001
    ev = one(parser, "Vote: 7 - 2")
    assert ev.type is EventType.CLIENT_VOTE
    assert ev.data == "2"


def test_a_vote_that_passed(parser):  # noqa: ANN001
    ev = one(parser, 'VotePassed: 1 - 0 - "reload"')
    assert ev.type is EventType.VOTE_PASSED
    assert ev.data == {"yes": 1, "no": 0, "what": "reload"}


def test_a_vote_that_failed(parser):  # noqa: ANN001
    ev = one(parser, 'VoteFailed: 1 - 1 - "restart"')
    assert ev.type is EventType.VOTE_FAILED


# -- flags and the bomb ----------------------------------------------------


@pytest.mark.parametrize(
    ("subtype", "action"),
    [("0", "flag_dropped"), ("1", "flag_returned"), ("2", "flag_captured")],
)
def test_flag_actions(parser, subtype, action):  # noqa: ANN001
    ev = one(parser, f"Flag: 7 {subtype}: team_CTF_blueflag")
    assert ev.type is EventType.CLIENT_ACTION
    assert ev.data == action
    assert ev.client.cid == "7"


def test_a_flag_returned_by_the_game_has_no_player(parser):  # noqa: ANN001
    ev = one(parser, "Flag Return: RED")
    assert ev.type is EventType.GAME_FLAG_RETURNED
    assert ev.data == "RED"
    assert ev.client is None


@pytest.mark.parametrize(
    ("line", "action"),
    [
        ("Bomb was planted by 7", "bomb_planted"),
        ("Bomb was defused by 7!", "bomb_defused"),
        ("Bomb was tossed by 7", "bomb_tossed"),
        ("Bomb has been collected by 7", "bomb_collected"),
    ],
)
def test_bomb_actions(parser, line, action):  # noqa: ANN001
    ev = one(parser, line)
    assert ev.type is EventType.CLIENT_ACTION
    assert ev.data == action


def test_the_bomb_holder_spawning(parser):  # noqa: ANN001
    ev = one(parser, "Bombholder is 7")
    assert (ev.type, ev.data) == (EventType.CLIENT_ACTION, "bomb_holder_spawn")


def test_the_bomb_going_off(parser):  # noqa: ANN001
    ev = one(parser, "Pop!")
    assert ev.type is EventType.GAME_BOMB_EXPLODED


def test_a_survivor_round_winner(parser):  # noqa: ANN001
    assert one(parser, "SurvivorWinner: Red").type is EventType.GAME_SURVIVOR_WINNER


# -- the shared grammar still works ----------------------------------------


def test_the_quake3_lines_are_inherited():
    """UrtParser must be a strictly larger grammar, not a replacement for it."""
    p = UrtParser(IOURT42)
    events = p.parse_line(
        r"ClientUserinfoChanged: 3 n\Bob\t\1\cl_guid\A337702493AF67BB0B0F8565CE8BC6C"
    )
    assert [e.type for e in events] == [EventType.CLIENT_UPDATE, EventType.CLIENT_TEAM_CHANGE]
    assert p.clients.get_by_cid("3").name == "Bob"

    kill = one(p, "Kill: 3 3 16: Bob killed Bob by UT_MOD_SPAS")
    assert kill.type is EventType.CLIENT_SUICIDE


def test_a_team_change_is_published_when_the_infostring_says_a_different_team():
    """There is no team-change line on this engine: the team is a field, and a change to it is the
    only evidence anybody switched. Nothing published this event, so every subscriber to it — the
    `!paforce` lock, the team balancer, `afk`'s idea of activity — sat dead on the whole family."""
    p = UrtParser(IOURT42)
    p.parse_line(r"ClientUserinfoChanged: 3 n\Bob\t\1")

    events = p.parse_line(r"ClientUserinfoChanged: 3 n\Bob\t\2")

    assert [e.type for e in events] == [EventType.CLIENT_UPDATE, EventType.CLIENT_TEAM_CHANGE]
    assert events[-1].data == "blue"


def test_a_line_that_does_not_move_anybody_is_only_an_update():
    p = UrtParser(IOURT42)
    p.parse_line(r"ClientUserinfoChanged: 3 n\Bob\t\1")

    events = p.parse_line(r"ClientUserinfoChanged: 3 n\Bobby\t\1")

    assert [e.type for e in events] == [EventType.CLIENT_UPDATE]


def test_the_spectators_are_spelt_the_way_everything_else_spells_them():
    """`spec`. This family said `spectator`, which matched nothing that reads a team — so a Quake3
    spectator was counted as a player by `callvote`, asked whether they were away by `afk`, and
    answered by `smart_say` in a channel they cannot read."""
    p = UrtParser(IOURT42)
    p.parse_line(r"ClientUserinfoChanged: 3 n\Bob\t\3")

    assert p.clients.get_by_cid("3").team == "spec"


def test_a_plain_quake3_server_does_not_get_the_urt_lines():
    """The extra lines are UrT's; a q3 or ET profile must not start interpreting them."""
    from b3.parsers.q3.parser import Q3Parser
    from b3.parsers.q3.profiles import Q3

    p = Q3Parser(Q3)
    p.clients.add(Client(cid="7", name="ercan", team="blue"))
    assert p.parse_line("Hit: 12 7 1 19: BSTHanzo[FR] hit ercan in the Helmet") == []
    assert p.parse_line('Callvote: 7 - "map dressingroom"') == []


def test_the_timestamp_prefix_is_stripped_like_every_other_line(parser):  # noqa: ANN001
    """Real UrT logs carry `6:37 ` in front of every line."""
    ev = one(parser, "  6:37 Hit: 12 7 1 19: BSTHanzo[FR] hit ercan in the Helmet")
    assert ev.type is EventType.CLIENT_DAMAGE


@pytest.mark.parametrize(
    "mod",
    [
        "UT_MOD_SPAS",
        "UT_MOD_M4",
        "UT_MOD_AK103",
        "UT_MOD_LR300",
        "UT_MOD_G36",
        "UT_MOD_PSG1",
        "UT_MOD_SR8",
        "UT_MOD_MP5K",
        "UT_MOD_HK69",
        "MOD_MP40",
    ],
)
def test_a_kill_by_any_weapon_is_seen(parser, mod):  # noqa: ANN001
    """Regression: the shared kill pattern rejected any means-of-death containing a digit.

    That is most of Urban Terror's arsenal, and the symptom is silence — an unmatched log line looks
    exactly like a line the server never wrote, so kills simply went missing.
    """
    ev = one(parser, f"Kill: 7 12 16: ercan killed BSTHanzo[FR] by {mod}")
    assert ev.type is EventType.CLIENT_KILL
    assert ev.client.cid == "7" and ev.target.cid == "12"
    assert ev.data.means_of_death == mod


# -- the hit location a kill line does not carry ---------------------------


def test_a_kill_carries_the_hit_location_of_the_shot_that_did_it(parser):  # noqa: ANN001
    """`Kill:` states the weapon and never the part of the body.

    The classic bot threaded it through the victim as `lastDamageTaken`, and it has to be threaded
    somehow: the kill is the event plugins listen to, so without this the fact that a shot was a
    headshot is in the log and nowhere else. `firstkill` is what wants it.
    """
    one(parser, "Hit: 12 7 0 19: BSTHanzo[FR] hit ercan in the Head")
    kill = one(parser, "Kill: 7 12 19: ercan killed BSTHanzo[FR] by UT_MOD_M4")

    assert kill.type is EventType.CLIENT_KILL
    assert kill.data.hit_location == "head"


def test_a_kill_with_no_hit_before_it_claims_no_hit_location(parser):  # noqa: ANN001
    kill = one(parser, "Kill: 7 12 19: ercan killed BSTHanzo[FR] by UT_MOD_M4")

    assert kill.data.hit_location == ""


def test_a_hit_location_is_spent_on_one_kill(parser):  # noqa: ANN001
    """The next death is a different life, and must not inherit the last one's wound."""
    one(parser, "Hit: 12 7 0 19: BSTHanzo[FR] hit ercan in the Head")
    first = one(parser, "Kill: 7 12 19: ercan killed BSTHanzo[FR] by UT_MOD_M4")
    second = one(parser, "Kill: 7 12 19: ercan killed BSTHanzo[FR] by UT_MOD_M4")

    assert first.data.hit_location == "head"
    assert second.data.hit_location == ""


def test_a_round_starting_forgets_every_wound(parser):  # noqa: ANN001
    one(parser, "Hit: 12 7 0 19: BSTHanzo[FR] hit ercan in the Head")
    parser.parse_line(r"InitGame: \sv_hostname\test\mapname\ut4_casa")
    kill = one(parser, "Kill: 7 12 19: ercan killed BSTHanzo[FR] by UT_MOD_M4")

    assert kill.data.hit_location == ""


def test_a_departing_player_takes_their_wound_with_them(parser):  # noqa: ANN001
    """The slot is somebody else's in a moment, and their hit locations are not inherited."""
    one(parser, "Hit: 12 7 0 19: BSTHanzo[FR] hit ercan in the Head")
    parser.parse_line("ClientDisconnect: 12")
    parser.clients.add(Client(cid="12", name="Somebody", team="red"))
    kill = one(parser, "Kill: 7 12 19: ercan killed Somebody by UT_MOD_M4")

    assert kill.data.hit_location == ""
