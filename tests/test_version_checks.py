"""Asking the server what it is, before trusting what it says — §1.15 and §1.19.

Two questions, both of which the classic bot asked and this one did not.

**"Are you even the right game?"** (`checkVersion`). The six Frostbite titles share one parser and
one set of verbs, so a `bf3` bot pointed at a Battlefield 4 server connects, logs in, and then reads
a grammar that is *almost* right — half the events come out wrong and the rest not at all, which is
indistinguishable from a server where nothing happens. The classic refused to start, and so does
this.

**"Which build are you?"** (`setVersionExceptions`). Call of Duty 2 has two builds that are not like
the others: 1.0 cannot authenticate players at all, and 1.2 reports PunkBuster ids one character
shorter than every other build. We read no version at all before this, so a 1.0 server looked merely
broken and a 1.2 PunkBuster id failed a length check without a word.
"""

from __future__ import annotations

import pytest

from b3.config.schema import Config
from b3.parsers.cod.profiles import COD2, COD4, VERSION_CVAR
from b3.parsers.frostbite.profiles import (
    BF3,
    BF3_REQUIRED_BUILD,
    BF4,
    BFBC2,
    BFH,
    MOH,
    MOHW,
)
from b3.parsers.frostbite.parser import FbParser
from b3.runtime.bot import Bot, WrongGameError


class ScriptedRcon:
    """Answers whatever the test said to, per command."""

    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.replies = replies or {}
        self.sent: list[str] = []

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        for prefix, reply in self.replies.items():
            if cmd.startswith(prefix):
                return reply
        return ""

    def close(self) -> None:
        pass


def _bot(game: str, rcon: ScriptedRcon) -> Bot:
    config = Config.model_validate(
        {"bot": {"database": "sqlite://"}, "server": {"game": game}},
    )
    return Bot(config, rcon=rcon)


# -- is this the game the title parses? -----------------------------------------


def test_the_right_game_is_accepted() -> None:
    bot = _bot("bf3", ScriptedRcon({"version": f"BF3 {BF3_REQUIRED_BUILD}"}))
    bot.check_version()
    assert bot.game.version == f"BF3 {BF3_REQUIRED_BUILD}"


def test_a_bf4_server_refuses_a_bf3_bot() -> None:
    """The whole reason this check exists: the two share a parser, so it would otherwise connect."""
    bot = _bot("bf3", ScriptedRcon({"version": "BF4 155011"}))

    with pytest.raises(WrongGameError) as caught:
        bot.check_version()

    # The message has to name the fix, since it is one word in the config file.
    assert "server.game" in str(caught.value)
    assert "BF4" in str(caught.value)


def test_a_build_older_than_the_parser_was_written_for_refuses() -> None:
    bot = _bot("bf3", ScriptedRcon({"version": f"BF3 {BF3_REQUIRED_BUILD - 1}"}))

    with pytest.raises(WrongGameError) as caught:
        bot.check_version()

    assert str(BF3_REQUIRED_BUILD) in str(caught.value)


def test_a_server_that_will_not_answer_is_not_the_wrong_game() -> None:
    """ "Could not ask" and "answered wrongly" want different responses from whoever reads the log.

    The same distinction `check_required_mod` draws: reporting a silent server as a misconfigured
    one sends the operator to edit a setting that was right all along.
    """
    bot = _bot("bf3", ScriptedRcon({}))
    bot.check_version()  # does not raise


def test_a_transport_failure_is_not_the_wrong_game_either() -> None:
    class Broken(ScriptedRcon):
        def command(self, cmd: str) -> str:
            raise OSError("connection reset")

    _bot("bf3", Broken()).check_version()  # does not raise


def test_hardline_calls_itself_something_other_than_its_title_id() -> None:
    """`bfh` in the config, `BFHL` on the wire — which is why this is data, not derived from `name`."""
    assert BFH.version_check is not None
    assert BFH.version_check.game_name == "BFHL"


def test_every_frostbite_title_states_what_it_expects_to_be_talking_to() -> None:
    expected = [
        (BF3, "BF3"),
        (BF4, "BF4"),
        (BFBC2, "BFBC2"),
        (BFH, "BFHL"),
        (MOH, "MOH"),
        (MOHW, "MOHW"),
    ]
    for profile, game_name in expected:
        assert profile.version_check is not None, profile.name
        assert profile.version_check.game_name == game_name
        assert profile.version_check.command == "version"


def test_medal_of_honor_2010_has_no_build_floor() -> None:
    """Because the classic checked only its name. A floor invented here would be a guess."""
    assert MOH.version_check is not None
    assert MOH.version_check.min_build == 0


# -- which build is it? ---------------------------------------------------------


def test_cod2_1_0_is_warned_about(caplog) -> None:  # noqa: ANN001
    bot = _bot("cod2", ScriptedRcon({VERSION_CVAR: 'shortversion is "1.0"'}))

    with caplog.at_level("WARNING"):
        bot.check_version()

    assert bot.game.version == "1.0"
    assert "cannot authenticate players properly" in caplog.text


def test_cod2_1_2_carries_the_shorter_punkbuster_id_length() -> None:
    """31, not 32. A validator written to the documented length rejects every id this build makes —
    and a rejected id looks exactly like a player who has none."""
    bot = _bot("cod2", ScriptedRcon({VERSION_CVAR: 'shortversion is "1.2"'}))

    bot.check_version()

    assert bot.version_quirk is not None
    assert bot.version_quirk.pb_id_length == 31


def test_a_build_suffix_still_matches_its_quirk() -> None:
    """Servers report things like `1.2 build 4`, so the match is on the leading part."""
    quirk = COD2.quirk_for("1.2 build 4")
    assert quirk is not None
    assert quirk.pb_id_length == 31


def test_the_more_specific_version_wins_regardless_of_table_order() -> None:
    assert COD2.quirk_for("1.0") is not None
    assert COD2.quirk_for("1.5") is None  # an ordinary build is not a quirk


def test_an_ordinary_build_says_nothing(caplog) -> None:  # noqa: ANN001
    bot = _bot("cod2", ScriptedRcon({VERSION_CVAR: 'shortversion is "1.3"'}))

    with caplog.at_level("WARNING"):
        bot.check_version()

    assert bot.version_quirk is None
    assert caplog.text == ""


def test_only_the_title_with_something_to_learn_asks() -> None:
    """CoD2 is the one Call of Duty title whose builds differ in anything this bot depends on, and
    it is the one the classic overrode `setVersionExceptions` for. A startup round trip on the other
    ten would buy a log line and nothing else, so they do not make it."""
    rcon = ScriptedRcon({VERSION_CVAR: 'shortversion is "1.7"'})
    bot = _bot("cod4", rcon)

    bot.check_version()

    assert COD4.version_cvar == ""
    assert COD4.version_quirks == {}
    assert rcon.sent == []


# -- the server describing itself -----------------------------------------------


def test_a_frostbite_server_info_reply_is_read_by_position() -> None:
    parser = FbParser(BF3)
    info = parser.read_server_info('"my server" 2 16 ConquestLarge0 MP_001 1 2')
    assert info["sv_hostname"] == "my server"
    assert info["sv_maxclients"] == "16"
    assert info["g_gametype"] == "ConquestLarge0"
    assert info["mapname"] == "MP_001"
    assert info["roundsTotal"] == "2"


def test_a_short_server_info_reply_gives_up_what_it_has() -> None:
    """The two protocol generations carry different numbers of trailing fields."""
    parser = FbParser(BF3)
    info = parser.read_server_info('"my server" 2 16')
    assert info["sv_hostname"] == "my server"
    assert "mapname" not in info


def test_an_empty_server_info_reply_is_not_an_error() -> None:
    assert FbParser(BF3).read_server_info("") == {}


def test_the_server_info_lands_on_the_game_object() -> None:
    rcon = ScriptedRcon(
        {
            "version": f"BF3 {BF3_REQUIRED_BUILD}",
            "serverInfo": '"Dan\'s server" 2 24 ConquestLarge0 MP_001 1 2',
        }
    )
    bot = _bot("bf3", rcon)

    bot.read_server_info()

    assert bot.game.hostname == "Dan's server"
    assert bot.game.max_players == 24
    assert bot.game.map_name == "MP_001"


def test_only_frostbite_asks_because_only_frostbite_has_no_cvars() -> None:
    assert BF3.server_info_command == "serverInfo"
    assert COD4.server_info_command == ""
