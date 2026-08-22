"""What Enemy Territory and Soldier of Fortune 2 add to the shared Quake3 grammar.

Sample lines come from the comments in the classic ``et.py``/``sof2.py`` — captured from real
servers. Not verified against a live server of either game; see TODO.md §1.4.

The ET identity test is the one that matters. A plain ET server sets no ``cl_guid``, so before
``ConnectInfo:`` was parsed nobody on such a server could hold a level or a ban.
"""

from __future__ import annotations

import pytest

from b3.core.events import EventType
from b3.domain.client import Client
from b3.parsers.games import PROFILES, parser_for
from b3.parsers.q3.et import EtParser
from b3.parsers.q3.profiles import ET, SOF2
from b3.parsers.q3.sof2 import Sof2Parser

PBID = "E24F9B2702B9E4A1223E905BF597FA92"  # 32 hex, as PunkBuster reports it


def one(p, line: str):  # noqa: ANN001, ANN201
    events = p.parse_line(line)
    assert len(events) == 1, f"expected 1 event from {line!r}, got {events!r}"
    return events[0]


# -- Enemy Territory -------------------------------------------------------


def test_connectinfo_is_where_an_et_player_gets_an_identity():
    p = EtParser(ET)
    ev = one(p, f"ConnectInfo: 0: {PBID}: ^w[^2AS^w]^2Lead: 3: 3: 24.153.180.106:2794")

    assert ev.type is EventType.CLIENT_UPDATE
    client = p.clients.get_by_cid("0")
    assert client.guid == PBID  # the same id the classic bot stored, so imported bans still match
    assert client.ip == "24.153.180.106"
    assert client.name == "^w[^2AS^w]^2Lead"


def test_a_connectinfo_line_survives_the_log_timestamp():
    """ET timestamps run into the line with no space: `1579:03ConnectInfo: 0: ...`."""
    p = EtParser(ET)
    events = p.parse_line(f"1579:03ConnectInfo: 0: {PBID}: Lead: 3: 3: 24.153.180.106:2794")
    assert [e.type for e in events] == [EventType.CLIENT_UPDATE]


def test_chat_identified_by_slot():
    """ET's own chat lines carry the slot, which beats matching a colour-coded name."""
    p = EtParser(ET)
    p.clients.add(Client(cid="17", name="^1[^7DP^1]^4Timekiller", team="red"))

    said = one(p, "sayc: 17: ^1[^7DP^1]^4Timekiller: ^4hello ^2there")
    assert said.type is EventType.CLIENT_SAY
    assert said.data == "^4hello ^2there"
    assert said.client.cid == "17"

    team = one(p, "sayteamc: 17: ^1[^7DP^1]^4Timekiller: ^4ammo ^2here !!!!!")
    assert team.type is EventType.CLIENT_TEAM_SAY


def test_chat_from_an_unknown_slot_is_dropped():
    assert EtParser(ET).parse_line("sayc: 9: Ghost: hello") == []


def test_gibbing():
    p = EtParser(ET)
    p.clients.add(Client(cid="1", name="klaus", team="red"))
    p.clients.add(Client(cid="18", name="fox", team="blue"))

    ev = one(p, "Gib: 1 18 9: ^1klaus gibbed ^1[pura]fox.nl by MOD_MP40")

    assert ev.type is EventType.CLIENT_GIB
    assert ev.client.cid == "1"  # attacker first, as id Tech 3 logs it
    assert ev.target.cid == "18"


def test_gibbing_a_team_mate():
    p = EtParser(ET)
    p.clients.add(Client(cid="1", name="klaus", team="red"))
    p.clients.add(Client(cid="2", name="hans", team="red"))
    assert one(p, "Gib: 1 2 9: klaus gibbed hans by MOD_MP40").type is EventType.CLIENT_GIB_TEAM


def test_gibbing_yourself():
    p = EtParser(ET)
    p.clients.add(Client(cid="1", name="klaus", team="red"))
    assert one(p, "Gib: 1 1 9: klaus gibbed klaus by MOD_GRENADE").type is EventType.CLIENT_GIB_SELF


def test_the_round_boundary_lines():
    p = EtParser(ET)
    assert one(p, "warmup:").type is EventType.GAME_WARMUP
    assert one(p, "restartgame:").type is EventType.GAME_ROUND_END


def test_the_shared_kill_line_still_reads_attacker_first():
    """Kept deliberately: the classic et.py read it victim-first, inverting every ET kill.

    id Tech 3 logs `Kill: %i %i %i` as killer, victim, means-of-death — the same order Urban
    Terror's parser used, in the same classic tree, which is what makes the ET one a transcription
    bug rather than a per-game difference.
    """
    p = EtParser(ET)
    p.clients.add(Client(cid="1", name="klaus", team="red"))
    p.clients.add(Client(cid="18", name="fox", team="blue"))

    ev = one(p, "Kill: 1 18 9: ^1klaus killed ^1[pura]fox.nl by MOD_MP40")

    assert ev.client.cid == "1" and ev.target.cid == "18"


# -- Soldier of Fortune 2 --------------------------------------------------


def test_sof2_states_the_damage_on_its_hit_line():
    p = Sof2Parser(SOF2)
    p.clients.add(Client(cid="0", name="victim", team="red"))
    p.clients.add(Client(cid="4", name="xlr8or", team="blue"))

    ev = one(p, "Hit: 0 4 520 368 0: xlr8or hit victim at location 520 for 368")

    assert ev.type is EventType.CLIENT_DAMAGE
    assert ev.client.cid == "4"  # victim first on a Hit line, attacker second
    assert ev.target.cid == "0"
    assert ev.data.damage == 368  # from the line itself: no weapon/location table needed
    assert ev.data.hit_location == "520"


def test_sof2_self_damage():
    p = Sof2Parser(SOF2)
    p.clients.add(Client(cid="0", name="xlr8or", team="red"))
    ev = one(p, "Hit: 0 0 520 368 0: xlr8or hit xlr8or at location 520 for 368")
    assert ev.type is EventType.CLIENT_DAMAGE_SELF


def test_sof2_team_damage():
    p = Sof2Parser(SOF2)
    p.clients.add(Client(cid="0", name="mate", team="red"))
    p.clients.add(Client(cid="4", name="xlr8or", team="red"))
    ev = one(p, "Hit: 0 4 520 100 0: xlr8or hit mate at location 520 for 100")
    assert ev.type is EventType.CLIENT_DAMAGE_TEAM


# -- wiring ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("game", "expected"),
    [
        ("et", "EtParser"),
        ("etpro", "EtParser"),
        ("sof2", "Sof2Parser"),
        ("sof2pm", "Sof2Parser"),
        ("iourt42", "UrtParser"),
        ("q3", "Q3Parser"),
        ("wop", "Q3Parser"),
    ],
)
def test_each_title_gets_its_parser(game, expected):  # noqa: ANN001
    assert type(parser_for(PROFILES[game])).__name__ == expected


def test_a_subclassed_family_keeps_the_whole_shared_grammar():
    """A title must never lose a line by gaining a family of its own."""
    for parser in (EtParser(ET), Sof2Parser(SOF2)):
        events = parser.parse_line(r"ClientUserinfoChanged: 3 n\Bob\t\1")
        # The team change rides along: the line carries a team, and on this family it is the only
        # line that ever says a player moved.
        assert [e.type for e in events] == [
            EventType.CLIENT_UPDATE,
            EventType.CLIENT_TEAM_CHANGE,
        ]
        assert parser.clients.get_by_cid("3").name == "Bob"


def test_et_lines_are_not_read_on_a_plain_quake3_server():
    from b3.parsers.q3.parser import Q3Parser
    from b3.parsers.q3.profiles import Q3

    p = Q3Parser(Q3)
    p.clients.add(Client(cid="17", name="Bob", team="red"))
    assert p.parse_line(f"ConnectInfo: 0: {PBID}: Bob: 3: 3: 24.153.180.106:2794") == []
    assert p.parse_line("sayc: 17: Bob: hello") == []
