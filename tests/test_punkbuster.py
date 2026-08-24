"""PunkBuster — §4.1, written against the captured data of §4b.

Every row below is **captured**, from the classic tree's ``tests/core/parsers/test_punkbuster.py``
(300 lines, real servers, 2010-era Call of Duty). Provenance is recorded per §4b's rule: a fixture
whose origin is written down can be trusted later, and one that is merely present looks exactly like
something invented to make a parser pass.

Reading it first was worth it, as it has been every time. Four things in the data are load-bearing
and none was guessable from the classic *code*:

* PunkBuster numbers slots from **1** and the game numbers them from **0**.
* Ids run **30 to 32** hex characters, not the documented 32.
* Rows lose characters at random — a whole captured file of them does.
* The line prefix varies per game and per mod, and is sometimes absent.
"""

from __future__ import annotations

import pytest

from b3.domain.client import Client
from b3.parsers.punkbuster import PbPlayer, PunkBuster, parse_player_list

# Captured: test_punkbuster.py::test_getPlayerList_nominal. Note the last two rows — a 31-character
# id, and a row with no space between the slot and the id.
NOMINAL = """\
: Player List: [Slot #] [GUID] [Address] [Status] [Power] [Auth Rate] [Recent SS] [O/S] [Name]
: 4  27b26543216546163546513465135135(-) 111.11.1.11:28960 OK   1 3.0 0 (W) "ShyRat"
: 5 387852749658574858598854913cdf11(-) 222.222.222.222:28960 OK   1 10.0 0 (W) "shatgun"
: 6 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"
^3PunkBuster Server: 7 290d4ad01d240000000026f304572ea(VALID) 11.43.50.163:28960 OK   1 3.0 0 (W) "RascalJr>XI<"
^3PunkBuster Server: 8290d4ad01d240000000026f304572eaf(VALID) 11.43.50.163:28960 OK   1 3.0 0 (W) "RascalJr>XI<"
"""

# Captured: test_getPlayerList_cod5. A different prefix again, and a two-digit slot.
COD5_ROW = (
    "whatever: 19 c0356dc89ddb0000000d4f9509db46d1(-) 11.111.111.11:28960 OK 0 2.9 0 (W) "
    '"FATTYBMBLATY"'
)

# Captured: test_getPlayerList_missing_chars_randomly — a *reported* PunkBuster defect, where the
# output drops a character in no consistent place. Every one of these must still yield the row.
MANGLED = [
    '^3PunkBuster Server:  1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '3PunkBuster Server:  1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^PunkBuster Server:  1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3unkBuster Server:  1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBusterServer:  1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster erver:  1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server  1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server:1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 19732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1() 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(- 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-)33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-2960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960OK   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 K   1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK1 5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   5.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   15.0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 .0 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 50 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5. 0 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.00 (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0  (W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0(W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 W) "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 () "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W "FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W)"FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) FATTYBMBLATY"',
    '^3PunkBuster Server: 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY',
    ': 1 9732d328485274156125252141252ba1(-) 33.133.3.133:-28960 OK   1 5.0 0 (W) "FATTYBMBLATY"',
]


# -- reading the player list ----------------------------------------------------


def test_the_slot_is_one_higher_than_the_game_s():
    """**The mistake that would silently ban the wrong player.** PunkBuster counts from 1."""
    players = parse_player_list(NOMINAL)

    assert players["3"].pbid == "27b26543216546163546513465135135"
    assert players["3"].pb_slot == "4"
    assert players["3"].cid == "3"


def test_every_captured_row_is_read():
    players = parse_player_list(NOMINAL)

    assert set(players) == {"3", "4", "5", "6", "7"}
    assert players["4"].name == "shatgun"
    assert players["4"].ip == "222.222.222.222"


def test_a_thirty_one_character_id_is_accepted():
    """Captured from a real server. The documented length is 32, and a validator written to it
    rejects every id this row carries — which is the same fault §1.19 records for Call of Duty 2
    build 1.2, arriving here from a completely different direction."""
    players = parse_player_list(NOMINAL)

    assert players["6"].pbid == "290d4ad01d240000000026f304572ea"
    assert len(players["6"].pbid) == 31


def test_a_missing_space_between_slot_and_id_still_splits_correctly():
    """`8290d4ad…` is slot 8 and a 32-character id, not slot 82 and a short one."""
    players = parse_player_list(NOMINAL)

    assert players["7"].pb_slot == "8"
    assert players["7"].pbid == "290d4ad01d240000000026f304572eaf"


def test_a_negative_port_does_not_lose_the_row():
    """`33.133.3.133:-28960` is in the captures. Insisting on a well-formed port drops the player."""
    players = parse_player_list(NOMINAL)

    assert players["5"].ip == "33.133.3.133"


def test_the_header_is_not_reported_as_an_unreadable_row():
    """It is not a row. Warning about it would train an operator to ignore the warning."""
    assert "Player List:" in NOMINAL
    assert len(parse_player_list(NOMINAL)) == 5


def test_a_different_prefix_is_read_the_same():
    """`whatever: ` — the prefix varies by game and by mod, and is sometimes absent entirely."""
    players = parse_player_list(COD5_ROW)

    assert players["18"].pbid == "c0356dc89ddb0000000d4f9509db46d1"
    assert players["18"].name == "FATTYBMBLATY"


@pytest.mark.parametrize("line", MANGLED)
def test_a_row_missing_a_character_still_yields_its_player(line: str):
    """PunkBuster is *reported* to drop a character from its output in no consistent place. The
    classic captured a file of them, and the loose pattern is the answer to it."""
    players = parse_player_list(line)

    assert "0" in players, line
    assert players["0"].pbid == "9732d328485274156125252141252ba1"
    assert players["0"].ip == "33.133.3.133"
    assert players["0"].name == "FATTYBMBLATY"


def test_a_row_whose_slot_does_not_advance_is_dropped():
    """The classic's rule, and worth keeping: a mangled row can parse into a plausible one carrying
    a slot already seen, and filing a stranger's id against a connected player is how the wrong
    person gets banned. A missing id is a player the bot cannot act on; a wrong one is worse."""
    doubled = COD5_ROW + "\n" + COD5_ROW

    assert len(parse_player_list(doubled)) == 1


def test_nothing_readable_is_no_players_rather_than_an_error():
    assert parse_player_list("") == {}
    assert parse_player_list('Unknown command "PB_SV_PList"') == {}


# -- detection ------------------------------------------------------------------


def test_a_server_running_punkbuster_is_recognised():
    assert PunkBuster.is_installed("PunkBuster Server v1.700 (a1000) Enabled")


def test_a_server_without_it_is_not():
    """It answers nothing, or an unknown-command message. Both mean "do not build this"."""
    assert not PunkBuster.is_installed("")
    assert not PunkBuster.is_installed('Unknown command "PB_SV_Ver"')


# -- the verbs ------------------------------------------------------------------


class Recorder:
    def __init__(self, reply: str = "") -> None:
        self.sent: list[str] = []
        self.reply = reply

    def __call__(self, cmd: str) -> str:
        self.sent.append(cmd)
        return self.reply


def _client(**kwargs) -> Client:  # noqa: ANN003
    base = {"guid": "G", "name": "Bob", "cid": "3", "pbid": "9732d328485274156125252141252ba1"}
    return Client(**{**base, **kwargs})


def test_a_kick_names_punkbuster_s_slot_not_the_game_s():
    send = Recorder()

    PunkBuster(send).kick(_client(), minutes=5, reason="cheating")

    assert send.sent == ['PB_SV_Kick "4" "5" "cheating" ""']


def test_a_screenshot_is_asked_of_the_right_slot():
    send = Recorder()

    PunkBuster(send).screenshot(_client())

    assert send.sent == ['PB_SV_GetSs "4"']


def test_a_connected_player_is_banned_by_slot():
    send = Recorder()

    PunkBuster(send).ban(_client(), reason="aimbot")

    assert send.sent == ['PB_SV_Ban "4" "aimbot" ""']


def test_a_player_who_has_left_is_banned_by_id_instead():
    """The slot form reaches nobody once they are gone, and the id form is the only one that does."""
    send = Recorder()

    PunkBuster(send).ban(_client(cid=None), reason="aimbot")

    assert send.sent == ['PB_SV_BanGuid "9732d328485274156125252141252ba1" "Bob" "???" "aimbot"']


def test_an_unknown_address_is_sent_as_question_marks_not_as_nothing():
    """PunkBuster's own documentation says to. An empty argument shifts every following one along,
    so the reason would land in the address field."""
    send = Recorder()

    PunkBuster(send).ban_id(_client(cid=None, name="", ip=""), reason="aimbot")

    assert '"???" "???"' in send.sent[0]


def test_a_player_with_no_punkbuster_id_cannot_be_banned_by_one():
    send = Recorder()

    assert not PunkBuster(send).ban_id(_client(pbid=""))
    assert send.sent == []


def test_lifting_a_ban_writes_the_ban_file():
    """Without the second command the unban only changes the list in memory, so the ban returns at
    the next restart — looking like a ban nobody remembers making."""
    send = Recorder()

    assert PunkBuster(send).unban_id(_client())

    assert send.sent == [
        'PB_SV_UnBanGuid "9732d328485274156125252141252ba1"',
        "pb_sv_updbanfile",
    ]


def test_an_unban_by_list_position_writes_the_file_too():
    send = Recorder()

    PunkBuster(send).unban_slot("7")

    assert send.sent == ['PB_SV_UnBan "7"', "pb_sv_updbanfile"]


def test_a_setting_is_asked_for_explicitly():
    """The classic turned *any* attribute assignment into a command, so a typo sent nonsense to the
    anti-cheat and reported nothing."""
    send = Recorder()

    PunkBuster(send).setting("cvarwalk", "1")

    assert send.sent == ["PB_SV_Cvarwalk 1"]


def test_a_reason_cannot_break_out_of_its_quotes():
    """Reasons are typed by admins and names are chosen by players, so both reach this command line
    untrusted — the same rule every other verb in this bot follows."""
    send = Recorder()

    PunkBuster(send).kick(_client(), reason='hax"; PB_SV_UnBanGuid "x')

    assert send.sent[0].count('"') == 8  # four arguments, and no more quoting than that


def test_a_name_shaped_slot_is_passed_through():
    """Frostbite's "slot" is the player's name, and PunkBuster's verbs accept a name in that
    position — so adding one to it is neither possible nor wanted."""
    send = Recorder()

    PunkBuster(send).kick(_client(cid="Bravo17"))

    assert 'PB_SV_Kick "Bravo17"' in send.sent[0]


def test_the_player_list_round_trips_through_the_verb():
    send = Recorder(reply=NOMINAL)

    players = PunkBuster(send).player_list()

    assert send.sent == ["PB_SV_PList"]
    assert isinstance(players["3"], PbPlayer)


# -- the runtime, and the reason PunkBuster is worth having at all ---------------
#
# Everything above is the protocol. This is what an operator gets: on the older titles, a player
# whose identity the *game* cannot state. A plain Quake 3 server sets no `cl_guid`, and `cod`/`cod2`
# hand out a six-character CD-key hash; without a PunkBuster id those players cannot hold a level or
# be matched against a ban at all.

PB_VERSION = "PunkBuster Server v1.700 (a1000) Enabled"


class PbRcon:
    """A server that runs PunkBuster and answers its player list."""

    def __init__(self, *, installed: bool = True, plist: str = NOMINAL) -> None:
        self.installed = installed
        self.plist = plist
        self.sent: list[str] = []

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        if cmd == "PB_SV_Ver":
            return PB_VERSION if self.installed else 'Unknown command "PB_SV_Ver"'
        if cmd == "PB_SV_PList":
            return self.plist
        return ""

    def close(self) -> None:
        pass


def _pb_bot(rcon, game: str = "cod2"):  # noqa: ANN001, ANN202
    from b3.config.schema import Config
    from b3.runtime.bot import Bot

    config = Config.model_validate(
        {"bot": {"database": "sqlite://"}, "server": {"game": game}},
    )
    return Bot(config, rcon=rcon)


def test_a_server_running_punkbuster_gets_the_service():
    bot = _pb_bot(PbRcon())
    bot.setup_punkbuster()
    assert bot.punkbuster is not None


def test_a_server_without_it_gets_none_and_no_complaint(caplog):  # noqa: ANN001
    bot = _pb_bot(PbRcon(installed=False))

    with caplog.at_level("WARNING"):
        bot.setup_punkbuster()

    assert bot.punkbuster is None
    assert caplog.text == ""  # the ordinary case; a line about it would be noise


def test_asking_for_it_and_not_getting_it_is_said_out_loud(caplog):  # noqa: ANN001
    """An operator relying on PunkBuster for identity has to hear that it is not there — that is
    exactly the case where quietly carrying on is worst."""
    from b3.config.schema import Config
    from b3.runtime.bot import Bot

    rcon = PbRcon(installed=False)
    config = Config.model_validate(
        {"bot": {"database": "sqlite://"}, "server": {"game": "cod2", "punkbuster": True}},
    )
    bot = Bot(config, rcon=rcon)

    with caplog.at_level("WARNING"):
        bot.setup_punkbuster()

    assert bot.punkbuster is None
    assert "not running PunkBuster" in caplog.text


def test_turning_it_off_means_the_question_is_never_asked():
    from b3.config.schema import Config
    from b3.runtime.bot import Bot

    rcon = PbRcon()
    config = Config.model_validate(
        {"bot": {"database": "sqlite://"}, "server": {"game": "cod2", "punkbuster": False}},
    )
    Bot(config, rcon=rcon).setup_punkbuster()

    assert rcon.sent == []


def test_an_engine_that_cannot_run_it_is_never_asked():
    """Plutonium postdates PunkBuster in these games, so asking earns an unknown command forever."""
    rcon = PbRcon()
    _pb_bot(rcon, game="plutoiw5").setup_punkbuster()
    assert rcon.sent == []


def test_the_id_is_recorded_against_the_connected_player():
    from b3.core.game import PlayerInfo

    rcon = PbRcon()
    bot = _pb_bot(rcon)
    bot.setup_punkbuster()
    bob = Client(guid="ENGINE_GUID", name="shatgun", cid="4")
    bot.clients.add(bob)

    bot._identify_from_punkbuster([PlayerInfo(cid="4", name="shatgun", guid="ENGINE_GUID")])

    assert bob.pbid == "387852749658574858598854913cdf11"


def test_an_engine_guid_is_never_replaced_by_a_punkbuster_id():
    """It is a *second* identity, not a substitute: every existing ban and every row of an imported
    classic database is keyed on the engine's guid, so overwriting it orphans a player's history."""
    from b3.core.game import PlayerInfo

    rcon = PbRcon()
    bot = _pb_bot(rcon)
    bot.setup_punkbuster()

    filled = bot._identify_from_punkbuster(
        [PlayerInfo(cid="4", name="shatgun", guid="ENGINE_GUID")]
    )

    assert filled[0].guid == "ENGINE_GUID"


def test_a_player_the_engine_cannot_identify_is_identified_by_punkbuster():
    """The whole point on the older titles: without this the player has no persistent id at all."""
    from b3.core.game import PlayerInfo

    rcon = PbRcon()
    bot = _pb_bot(rcon)
    bot.setup_punkbuster()

    filled = bot._identify_from_punkbuster([PlayerInfo(cid="4", name="shatgun", guid="")])

    assert filled[0].guid == "387852749658574858598854913cdf11"
    assert filled[0].ip == "222.222.222.222"


def test_an_empty_roster_is_not_asked_about():
    rcon = PbRcon()
    bot = _pb_bot(rcon)
    bot.setup_punkbuster()
    rcon.sent.clear()

    bot._identify_from_punkbuster([])

    assert rcon.sent == []


def test_a_screenshot_can_be_asked_for_through_the_port():
    rcon = PbRcon()
    bot = _pb_bot(rcon)
    bot.setup_punkbuster()

    assert bot.request_screenshot(Client(guid="G", name="Bob", cid="3"))
    assert 'PB_SV_GetSs "4"' in rcon.sent


def test_a_screenshot_on_a_server_without_punkbuster_reports_that():
    bot = _pb_bot(PbRcon(installed=False))
    bot.setup_punkbuster()

    assert not bot.request_screenshot(Client(guid="G", name="Bob", cid="3"))


# -- how the verb is carried, which is the title's business ----------------------


class FrostbitePbRcon:
    """A Battlefield server: PunkBuster answers only through `punkBuster.pb_sv_command`.

    Everything else is `UnknownCommand`, which is what a real one says — and which the startup probe
    reads as "no PunkBuster here".
    """

    def __init__(self) -> None:
        self.sent: list[str] = []

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        if cmd == "version":
            return "BFBC2 527791"
        if not cmd.startswith("punkBuster.pb_sv_command"):
            return f'UnknownCommand: "{cmd.split()[0]}"'
        return PB_VERSION if "PB_SV_Ver" in cmd else ""

    def close(self) -> None:
        pass


def test_punkbuster_on_a_battlefield_server_was_never_built_at_all():
    """`PB_SV_Ver` is not a command on this engine, it is an *argument* — so the probe was answered
    `UnknownCommand` and the service was skipped on all six Frostbite titles, in silence, while the
    profile said `punkbuster=True`."""
    rcon = FrostbitePbRcon()
    bot = _pb_bot(rcon, game="bfbc2")
    bot.setup_punkbuster()

    assert bot.punkbuster is not None
    assert rcon.sent[-1] == 'punkBuster.pb_sv_command "PB_SV_Ver"'


def test_the_quake_families_send_the_verb_as_it_stands():
    """The other side of the same fact: `%s` is the default carrier, and a `PB_SV_*` verb really is
    an RCON command there."""
    rcon = PbRcon()
    bot = _pb_bot(rcon)
    bot.setup_punkbuster()

    assert rcon.sent == ["PB_SV_Ver"]


def test_a_quote_inside_a_wrapped_verb_does_not_end_the_word():
    """Frostbite commands are word lists, so an unescaped quote inside the argument would turn the
    rest of a ban command into arguments of its own."""
    rcon = FrostbitePbRcon()
    bot = _pb_bot(rcon, game="bfbc2")
    bot.setup_punkbuster()

    bot.send_punkbuster('PB_SV_BanGuid "abc" "Bob"')

    assert rcon.sent[-1] == 'punkBuster.pb_sv_command "PB_SV_BanGuid \\"abc\\" \\"Bob\\""'


def test_a_line_for_punkbuster_on_a_server_without_it_answers_none():
    """So `!punkbuster` can tell "there is no PunkBuster here" from "PunkBuster said nothing"."""
    bot = _pb_bot(PbRcon(installed=False))
    bot.setup_punkbuster()

    assert bot.send_punkbuster("PB_SV_PList") is None
