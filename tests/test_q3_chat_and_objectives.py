"""Quake 3 chat, capture-the-flag and the per-title combat lines.

Every sample line here is one the classic B3 tree records as coming off a real server: the comments
in `iourt41.py`/`iourt42.py`/`iourt43.py`/`oa081.py`/`wop15.py`, and `test_iourt42.py` where it has
one. The source is named per test so the grammar can be checked against it.
"""

from __future__ import annotations

import pytest

from b3.core.events import EventType
from b3.domain.client import Client
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.q3.profiles import IOURT42, IOURT43, Q3, WOP15
from b3.parsers.q3.urt import UrtParser
from b3.parsers.q3.wop import Wop15Parser


def one(p, line):  # noqa: ANN001, ANN201
    events = p.parse_line(line)
    assert len(events) == 1, f"expected 1 event from {line!r}, got {events!r}"
    return events[0]


def none(p, line) -> None:  # noqa: ANN001
    assert p.parse_line(line) == []


# -- chat: the slot-prefixed form, which is UrT's --------------------------------------------


@pytest.fixture
def urt() -> UrtParser:
    p = UrtParser(IOURT42)
    p.clients.add(Client(cid="6", name="^5Marcel^2[^6CZARMY^2]", team="red"))
    p.clients.add(Client(cid="8", name="denzel", team="blue"))
    p.clients.add(Client(cid="15", name="repelSteeltje", team="red"))
    p.clients.add(Client(cid="16", name="woekele", team="blue"))
    return p


def test_chat_with_a_slot_in_front_of_the_name(urt):
    """`say: 6 ^5Marcel^2[^6CZARMY^2]: !help` — test_iourt42.py::test_say.

    Urban Terror writes the speaker's slot before their name. A pattern that does not allow for it
    reads the whole of "6 ^5Marcel…" as the name, matches no player, and publishes nothing — which
    means commands typed in chat never run.
    """
    ev = one(urt, "say: 6 ^5Marcel^2[^6CZARMY^2]: !help")

    assert ev.type is EventType.CLIENT_SAY
    assert ev.client.cid == "6"
    assert ev.data == "!help"


def test_team_chat_with_a_slot(urt):
    """`sayteam: 12 New_UrT_Player_v4.1: wokele` — iourt41.py::OnSayteam."""
    urt.clients.add(Client(cid="12", name="New_UrT_Player_v4.1"))

    ev = one(urt, "sayteam: 12 New_UrT_Player_v4.1: wokele")

    assert ev.type is EventType.CLIENT_TEAM_SAY
    assert ev.client.cid == "12"
    assert ev.data == "wokele"


def test_plain_quake3_chat_without_a_slot():
    """Making the slot optional must not break the shape every other title writes."""
    p = Q3Parser(Q3)
    p.clients.add(Client(cid="1", name="Bob"))

    ev = one(p, "say: Bob: hello")

    assert ev.type is EventType.CLIENT_SAY
    assert ev.client.cid == "1"
    assert ev.data == "hello"


def test_a_name_that_starts_with_digits_is_not_read_as_a_slot():
    p = Q3Parser(Q3)
    p.clients.add(Client(cid="1", name="007Bond"))

    ev = one(p, "say: 007Bond: shaken")

    assert ev.client.cid == "1"


def test_a_wrong_slot_falls_back_to_the_name(urt):
    """Urban Terror sometimes reports a chat line against the wrong slot.

    Commands run with the speaker's permission level, so the line has to be attributed to the named
    player rather than to whoever occupies the slot the server gave.
    """
    ev = one(urt, "say: 8 ^5Marcel^2[^6CZARMY^2]: hello")

    assert ev.client.cid == "6"  # the name wins, not slot 8


def test_a_leading_control_character_is_stripped(urt):
    """Some clients prefix chat with 0x15; left in place, `\\x15!help` is not read as a command."""
    ev = one(urt, "say: 8 denzel: \x15!help")

    assert ev.data == "!help"


def test_chat_from_an_unknown_player_publishes_nothing(urt):
    none(urt, "say: 3 Nobody: hello")


# -- saytell ---------------------------------------------------------------------------------


def test_saytell_is_a_private_message(urt):
    """`saytell: 15 16 repelSteeltje: nno` — iourt41.py::OnSaytell.

    This is how a command whispered to the bot arrives, rather than said out loud.
    """
    ev = one(urt, "saytell: 15 16 repelSteeltje: nno")

    assert ev.type is EventType.CLIENT_PRIVATE_SAY
    assert ev.client.cid == "15"
    assert ev.target.cid == "16"
    assert ev.data == "nno"


def test_saytell_with_matching_slots(urt):
    """`saytell: 15 15 …` is a shape the server writes, so the two slots must not be required to
    differ."""
    ev = one(urt, "saytell: 15 15 repelSteeltje: nno")

    assert ev.type is EventType.CLIENT_PRIVATE_SAY
    assert ev.client.cid == ev.target.cid == "15"


# -- ClientSpawn, Assist, FlagCaptureTime ----------------------------------------------------


def test_client_spawn(urt):
    """`ClientSpawn: 0` — test_iourt42.py::test_ClientSpawn."""
    urt.clients.add(Client(cid="0", name="Patate"))

    ev = one(urt, "ClientSpawn: 0")

    assert ev.type is EventType.CLIENT_SPAWN
    assert ev.client.cid == "0"


def test_flag_capture_time(urt):
    """`FlagCaptureTime: 0: 1234567890` — test_iourt42.py::test_Flagcapturetime.

    The slot is followed by a colon here, unlike the space-separated fields on the other lines.
    """
    urt.clients.add(Client(cid="0", name="Patate"))

    ev = one(urt, "FlagCaptureTime: 0: 1234567890")

    assert ev.type is EventType.CLIENT_FLAG_CAPTURE_TIME
    assert ev.client.cid == "0"
    assert ev.data == 1234567890


def test_flag_capture_time_without_the_colon_is_not_this_line(urt):
    urt.clients.add(Client(cid="0", name="Patate"))

    none(urt, "FlagCaptureTime: 0 1234567890")


def test_assist_credits_the_assister():
    """`Assist: 0 14 15: -[TPF]-PtitBigorneau assisted Bot1 to kill Bot2` — iourt43.py::OnAssist."""
    p = UrtParser(IOURT43)
    p.clients.add(Client(cid="0", name="-[TPF]-PtitBigorneau"))
    p.clients.add(Client(cid="14", name="Bot1"))
    p.clients.add(Client(cid="15", name="Bot2"))

    ev = one(p, "Assist: 0 14 15: -[TPF]-PtitBigorneau assisted Bot1 to kill Bot2")

    assert ev.type is EventType.CLIENT_ASSIST
    assert ev.client.cid == "0"  # the assister is who the event is about
    assert ev.target.cid == "15"  # the player who died
    assert ev.extra["killer"].cid == "14"


# -- CTF, on plain Quake 3 and OpenArena -----------------------------------------------------


@pytest.fixture
def q3() -> Q3Parser:
    p = Q3Parser(Q3)
    p.clients.add(Client(cid="1", name="Sarge", team="red"))
    p.clients.add(Client(cid="2", name="Burpman", team="blue"))
    p.clients.add(Client(cid="3", name="Tanisha", team="red"))
    return p


@pytest.mark.parametrize(
    ("line", "action"),
    [
        # Sample lines from oa081.py::OnCtf.
        ("CTF: 3 1 0: Tanisha got the RED flag!", "flag_taken"),
        ("CTF: 2 2 1: Burpman captured the BLUE flag!", "flag_captured"),
        ("CTF: 1 1 3: Sarge fragged RED's flag carrier!", "flag_carrier_kill"),
    ],
)
def test_a_ctf_action_is_credited_to_the_player(q3, line, action):  # noqa: ANN001
    ev = one(q3, line)

    assert ev.type is EventType.CLIENT_ACTION
    assert ev.data == action


def test_a_returned_flag_is_a_game_event(q3):
    """`CTF: 1 2 2: Sarge returned the BLUE flag!` — reported the way `Flag Return:` is."""
    ev = one(q3, "CTF: 1 2 2: Sarge returned the BLUE flag!")

    assert ev.type is EventType.GAME_FLAG_RETURNED
    assert ev.data == "BLUE"


def test_the_flag_colour_comes_from_the_column_not_the_sentence(q3):
    """The sentence is the server's English announcement and cannot be relied on."""
    ev = one(q3, "CTF: 2 1 2: le drapeau a ete rendu")

    assert ev.type is EventType.GAME_FLAG_RETURNED
    assert ev.data == "RED"


def test_an_unknown_ctf_action_still_names_itself(q3):
    ev = one(q3, "CTF: 1 1 9: something new")

    assert ev.data == "flag_action_9"


# -- World of Padman 1.5 ---------------------------------------------------------------------


@pytest.fixture
def wop() -> Wop15Parser:
    p = Wop15Parser(WOP15)
    p.clients.add(Client(cid="0", name="PadPlayer", team="red"))
    p.clients.add(Client(cid="1", name="Padder", team="blue"))
    p.clients.add(Client(cid="2", name="Spray", team="red"))
    p.clients.add(Client(cid="1022", name="<world>"))
    return p


def test_the_kill_line_shape_this_title_uses(wop):
    """`Kill: 2 MOD_INJECTOR 0` — a sample line from wop15.py. Attacker, means of death, victim.

    The shared Quake3 kill pattern requires a trailing `: <text> by <MOD>` and does not match this.
    """
    ev = one(wop, "Kill: 2 MOD_INJECTOR 0")

    assert ev.type is EventType.CLIENT_KILL_TEAM  # 2 and 0 are both red
    assert ev.client.cid == "2"
    assert ev.target.cid == "0"
    assert ev.data.means_of_death == "MOD_INJECTOR"


def test_a_kill_across_teams(wop):
    ev = one(wop, "Kill: 0 MOD_INJECTOR 1")

    assert ev.type is EventType.CLIENT_KILL


def test_non_fatal_damage(wop):
    """`Damage: 2 1022 2 50 7` — a sample line from wop15.py: victim, weapon, attacker, damage, mod."""
    ev = one(wop, "Damage: 1 5 0 50 7")

    assert ev.type is EventType.CLIENT_DAMAGE
    assert ev.client.cid == "0"
    assert ev.target.cid == "1"
    assert ev.data.damage == 50
    assert ev.extra["means_of_death_id"] == "7"


def test_damage_with_the_same_slot_on_both_sides_is_self_damage(wop):
    ev = one(wop, "Damage: 2 1022 2 50 7")

    assert ev.type is EventType.CLIENT_DAMAGE_SELF
    assert ev.client.cid == "2"


def test_a_slot_outside_the_valid_range_is_read_as_the_world(wop):
    """This engine reports slots over 64 on these lines; they cannot be players."""
    ev = one(wop, "Damage: 1 5 812 30 7")

    assert ev.client.cid == "1022"


def test_the_world_hurting_the_world_is_nothing(wop):
    none(wop, "Damage: 1022 5 1022 30 7")


def test_world_of_padman_still_reads_the_shared_grammar(wop):
    """A subclass adds lines; it must not lose the ones every Quake3 server writes."""
    ev = one(wop, "ClientConnect: 4")

    assert ev.type is EventType.CLIENT_CONNECT
