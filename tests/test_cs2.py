"""Counter-Strike 2: one account written three ways, and a title with no SourceMod under it.

The grammar needed nothing. Every line captured in the classic `test_csgo.py` was run through
`SourceParser` before any of this was written and all of them parsed, which is what the shared
`source` family was for -- CS:GO and Insurgency shared one parser classically, and the Half-Life log
standard survived into CS2. So the work here is not the log format. It is the two things the captures
cannot tell us, because they were recorded in 2012:

**Identity.** CS2 writes `[U:1:N]` in its log and answers `status` with a 17-digit Steam64, while a
database imported from classic B3 is keyed on `STEAM_1:Y:Z`. Three spellings of one account, and
nothing folded them together, so the same player was up to three records with a level and a ban
history each. `test_the_two_spellings_are_one_player` is the test that matters in this file.

**What the bot can say back.** SourceMod has no CS2 build -- Source 2 broke what it hooks -- so every
verb Insurgency uses is absent, and this title runs on what a stock server answers: `say`, `kickid`,
`changelevel`. There is no private message and no dependable ban, and the tests below pin down what
is done instead rather than leaving it to be discovered on a live server.
"""

from __future__ import annotations

import pytest

from b3.core import steamid
from b3.core.clients import ClientManager
from b3.core.game import PlayerInfo
from b3.parsers.games import PROFILES, parser_for
from b3.parsers.source.parser import SourceParser
from b3.parsers.source.profiles import CS2, INSURGENCY
from b3.parsers.status import parse_status

#: One account, three spellings. Account 2222222 -- so legacy is `STEAM_1:0:1111111`, since
#: 1111111 * 2 + 0 = 2222222.
ACCOUNT = 2222222
MODERN = f"[U:1:{ACCOUNT}]"
LEGACY = "STEAM_1:0:1111111"
STEAM64 = "76561197962487950"  # 76561197960265728 + 2222222, computed rather than eyeballed


def _parser() -> SourceParser:
    return SourceParser(CS2, ClientManager())


# -- the arithmetic ----------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", [MODERN, LEGACY, STEAM64])
def test_every_spelling_resolves_to_the_same_account(spelling):  # noqa: ANN001
    assert steamid.account_id(spelling) == ACCOUNT


def test_an_odd_account_keeps_its_low_bit():
    """The middle digit of the legacy form *is* the account's low bit, so it must survive the trip.

    Dropping it would map two neighbouring accounts onto one id, which is worse than not converting
    at all: two players would share a record, a level and a ban.
    """
    assert steamid.to_legacy(2222223) == "STEAM_1:1:1111111"
    assert steamid.account_id("STEAM_1:1:1111111") == 2222223
    assert steamid.account_id("[U:1:2222223]") == 2222223


@pytest.mark.parametrize(
    "spelling",
    [
        "",
        "BOT",
        "Console",
        "STEAM_ID_PENDING",
        "1234567890abcdef1234567890abcdef",  # a CoD4 guid
        "[G:1:4]",  # a group, not a player
        "[U:1:x]",
        "76561197960265728",  # account 0: the base itself is nobody
        "12345678901234567",  # 17 digits, but below the base
    ],
)
def test_what_is_not_a_steam_id_is_left_alone(spelling):  # noqa: ANN001
    """This sits on the path *every* guid takes, so being a no-op for the rest is load-bearing."""
    assert steamid.account_id(spelling) is None
    assert steamid.canonical(spelling) == spelling


def test_the_canonical_form_is_the_one_classic_b3_stored():
    """Legacy, not the prettiest -- the one already on disk in databases people are importing."""
    assert steamid.canonical(MODERN) == LEGACY
    assert steamid.canonical(STEAM64) == LEGACY
    assert steamid.canonical(LEGACY) == LEGACY


def test_no_other_title_pays_for_it():
    assert INSURGENCY.normalise_steam_ids is False
    assert INSURGENCY.canonical_guid(MODERN) == MODERN  # untouched on a title that reports one form
    assert CS2.canonical_guid(MODERN) == LEGACY


# -- the fold, which is the point -------------------------------------------------------------


def test_the_log_form_is_stored_in_the_canonical_form():
    parser = _parser()
    parser.parse_line(
        f'L 01/15/2026 - 20:11:04: "courgette<194><{MODERN}><CT>" say "!help"'
    )
    client = parser.clients.get_by_cid("194")
    assert client is not None
    assert client.guid == LEGACY


def test_the_two_spellings_are_one_player():
    """The log says `[U:1:N]`, `status` says Steam64, and an imported record says `STEAM_1:Y:Z`.

    Without folding them, the sync reads the status row as a stranger who has taken the slot, drops
    the record built from the log, and creates another -- losing the player's level and ban history
    for the session, every five minutes, on every player.
    """
    parser = _parser()
    parser.parse_line(f'L 01/15/2026 - 20:11:04: "courgette<194><{MODERN}><CT>" say "hi"')
    from_log = parser.clients.get_by_cid("194")
    assert from_log is not None

    status = (
        "hostname: A CS2 server\n"
        "map     : de_dust2\n"
        "# userid name uniqueid connected ping loss state rate adr\n"
        f'#194 2 "courgette" {STEAM64} 33:48 67 0 active 20000 11.222.111.222:27005\n'
    )
    _map, players = parse_status(status, CS2.status_patterns, CS2.identity_field)
    assert len(players) == 1
    assert CS2.canonical_guid(players[0].guid) == from_log.guid


def test_a_bot_is_still_not_given_an_identity():
    """Normalising must not resurrect the shared `BOT` guid as something authenticatable."""
    parser = _parser()
    parser.parse_line('L 01/15/2026 - 20:11:04: "Moe<224><BOT><TERRORIST>" say "hi"')
    client = parser.clients.get_by_cid("224")
    assert client is not None
    assert client.guid == ""


# -- teams -------------------------------------------------------------------------------------


def test_the_team_tokens_are_mapped_as_the_classic_bot_mapped_them():
    """TERRORIST is blue and CT is red, matching `csgo.py`'s `getTeam`.

    Which colour goes with which side is arbitrary; agreeing with the classic bot is not, because a
    plugin's stored per-team figures would otherwise change meaning on import.
    """
    assert CS2.teams["TERRORIST"] == "blue"
    assert CS2.teams["CT"] == "red"
    assert CS2.teams["Spectator"] == "spec"
    assert CS2.teams["Unassigned"] == ""


def test_switching_sides_is_seen():
    parser = _parser()
    parser.parse_line(f'L 01/15/2026 - 20:11:04: "courgette<194><{MODERN}><CT>" say "hi"')
    parser.parse_line(
        f'L 01/15/2026 - 20:11:05: "courgette<194><{MODERN}><CT>" '
        "switched from team <CT> to <TERRORIST>"
    )
    client = parser.clients.get_by_cid("194")
    assert client is not None
    assert client.team == "blue"


def test_a_token_this_bot_does_not_know_leaves_the_team_alone():
    """A bot-stuck line carries a *number* in the team field, and a validated line repeats the id.

    Clearing the team on an unrecognised token would drop half a roster out of its side.
    """
    parser = _parser()
    parser.parse_line(f'L 01/15/2026 - 20:11:04: "courgette<194><{MODERN}><CT>" say "hi"')
    parser.parse_line(f'L 01/15/2026 - 20:11:05: "courgette<194><{MODERN}><193>" say "hi"')
    client = parser.clients.get_by_cid("194")
    assert client is not None
    assert client.team == "red"


# -- what a stock server can actually be told ---------------------------------------------------


def test_the_verbs_are_the_native_ones_not_sourcemod():
    """SourceMod has no CS2 build, so an `sm_` verb here would fail silently on every send."""
    for template in (
        CS2.say_template,
        CS2.tell_template,
        CS2.kick_template,
        CS2.ban_template,
        CS2.tempban_template,
        CS2.map_template,
    ):
        assert "sm_" not in template
    assert CS2.required_mod is None
    assert INSURGENCY.required_mod is not None  # which is the difference between the two titles

    assert CS2.kick_template.startswith("kickid ")
    assert CS2.map_template.startswith("changelevel ")


def test_a_ban_is_a_kick_and_says_so():
    """`banid` survives from Source 1 but is unreliable on CS2, so enforcement is this bot's own.

    `Bot.unban` keys its "there is no server-side ban list" message off exactly this equality, so if
    these two ever drift apart an operator gets sent hunting for a list nothing ever wrote to.
    """
    assert CS2.ban_template == CS2.kick_template
    assert CS2.unban_template is None


def test_a_private_reply_goes_out_public_and_named():
    """There is no private-message verb at all. Naming the player is the honest version of that.

    The alternative -- keeping Insurgency's `sm_psay` -- sends a command the server does not have,
    which produces no error and no reply: the admin sees their command do nothing.
    """
    assert CS2.tell_template == "say [%(name)s] %(text)s"
    rendered = CS2.tell_template % {"name": "courgette", "text": "you are now superadmin"}
    assert rendered == "say [courgette] you are now superadmin"


def test_there_is_no_next_map_cvar_to_read():
    """`sm_nextmap` is SourceMod's. Without it `!nextmap` must answer "unknown", not guess.

    `maps *` lists every map installed, in no order, so stepping through it would state a falsehood
    about the rotation -- which is what `next_map_cvar` exists to prevent.
    """
    assert CS2.next_map_cvar == ""
    assert CS2.rotation_cvar == ""
    assert CS2.maplist_command == "maps *"


# -- registration ------------------------------------------------------------------------------


def test_cs2_is_configurable_and_reads_the_shared_grammar():
    assert PROFILES["cs2"] is CS2
    assert isinstance(parser_for(CS2, ClientManager(), 27015), SourceParser)


def test_the_captured_csgo_combat_lines_still_parse_under_this_profile():
    """Provenance: `test_csgo.py` in the classic tree. Evidence, not specification -- CS:GO 1 is
    dropped, but a 2012 line whose shape survived into CS2 is a line we can trust."""
    parser = _parser()
    for line in (
        'L 08/26/2012 - 03:46:44: "Pheonix<22><BOT><TERRORIST>" killed "Ringo<17><BOT><CT>"'
        ' with "glock" (headshot)',
        'L 08/26/2012 - 03:46:44: "Pheonix<22><BOT><TERRORIST>" [280 -133 -223] killed'
        ' "Ringo<17><BOT><CT>" [-216 397 -159] with "aug"',
        'L 08/26/2012 - 03:38:04: "Pheonix<22><BOT><TERRORIST>" committed suicide with "world"',
        'L 08/26/2012 - 03:22:36: "Pheonix<11><BOT><Unassigned>" joined team "TERRORIST"',
        'L 08/26/2012 - 03:47:40: Team "CT" triggered "SFUI_Notice_Target_Saved" (CT "3") (T "5")',
    ):
        assert parser.parse_line(line) is not None, line


def test_a_status_row_with_a_steam64_still_parses():
    """The captured rows all carry legacy ids; CS2 reports the 64-bit form in the same column."""
    row = f'#194 2 "courgette" {STEAM64} 33:48 67 0 active 20000 11.222.111.222:27005\n'
    _map, players = parse_status(row, CS2.status_patterns, CS2.identity_field)
    assert players == [
        PlayerInfo(
            cid="194",
            name="courgette",
            guid=STEAM64,
            steam_id="",
            ip="11.222.111.222",
            port=27005,
            ping=67,
        )
    ]
