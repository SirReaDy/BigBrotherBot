"""Call of Duty 1 (`cod`), and the chat-line limit shared by the whole Call of Duty family."""

from __future__ import annotations

import pytest

from b3.core.clock import FakeClock
from b3.config.schema import BotConfig, Config, ServerConfig
from b3.parsers.cod.parser import CodParser
from b3.parsers.cod.profiles import COD, COD2, COD4, COD4X, COD5, COD6, COD7, COD8, PLUTOIW5
from b3.parsers.games import PROFILES, parser_for, profile_for, suggest
from b3.runtime.bot import Bot

GBOB = "abcdef"


def test_cod1_is_a_title_the_bot_can_be_configured_for():
    assert "cod" in PROFILES
    assert profile_for("cod") is COD


def test_cod1_reads_the_shared_call_of_duty_grammar():
    assert isinstance(parser_for(COD), CodParser)


def test_identity_is_the_short_cd_key_hash_not_an_ip():
    """CoD1 reports a six-character CD-key hash, the same identity CoD2 uses."""
    assert COD.guid_min_length == 6 == COD2.guid_min_length
    assert COD.ips_only is False


def test_the_verbs_are_the_family_defaults():
    assert COD.kick_template == "clientkick %(cid)s"
    assert COD.ban_template == "banclient %(cid)s"
    assert COD.unban_template == "unbanuser %(target)s"


def test_the_log_is_unbuffered_at_startup():
    assert COD.startup_commands == ("g_logsync 3",)


@pytest.mark.asyncio
async def test_a_cod1_server_runs_end_to_end(tmp_path):
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod"),
    )
    bot = Bot(config, clock=FakeClock())
    bot.start()

    await bot.replay([f"J;{GBOB};2;Bob", "say;;2;Bob;hello"])

    assert bot.clients.get_by_cid("2").name == "Bob"
    bot.storage.close()


# -- the line limit --------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [COD, COD2, COD4, COD4X, COD5, COD6, COD7, COD8])
def test_every_call_of_duty_title_declares_the_engine_line_limit(profile):
    """A Call of Duty console displays 65 characters of a chat line and drops the rest."""
    assert profile.line_length == 65


def test_plutonium_keeps_its_own_smaller_limit():
    """IW5 stops displaying at 43, and the smaller of the two limits applies."""
    assert PLUTOIW5.line_length == 43


# -- the classic id for Quake 3 --------------------------------------------------------------


def test_the_classic_quake3_title_id_is_accepted():
    """The classic bot calls Quake 3 Arena `q3a`, so imported configs use that spelling."""
    assert profile_for("q3a").name == "q3"


def test_the_alias_is_offered_as_a_suggestion():
    assert suggest("q3a") == ["q3"]
