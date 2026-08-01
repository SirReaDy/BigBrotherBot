"""Resolving the handle an admin typed to exactly one player.

Two halves: `Bot.find_clients` ranks the candidates, and the commands refuse to act when more than
one player matches rather than taking whichever the roster lists first.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, ServerConfig
from b3.core.clock import FakeClock
from b3.core.commands import CommandProcessor
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.pingwatch import PingwatchPlugin
from b3.plugins.spamcontrol import SpamcontrolPlugin
from b3.runtime.bot import Bot


def _bot(tmp_path) -> Bot:  # noqa: ANN001
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
    )
    return Bot(config, clock=FakeClock())


def _connect(bot: Bot, *names_and_cids: tuple[str, str]) -> list[Client]:
    added = []
    for name, cid in names_and_cids:
        client = Client(guid=name.upper(), name=name, cid=cid)
        bot.clients.add(client)
        added.append(client)
    return added


# -- Bot.find_clients ------------------------------------------------------------------------


def test_a_substring_matching_two_players_returns_both(tmp_path):
    bot = _bot(tmp_path)
    bobby, bobcat = _connect(bot, ("bobby", "1"), ("bobcat", "2"))

    assert bot.find_clients("bob") == [bobby, bobcat]


def test_an_exact_name_wins_outright_over_a_longer_one(tmp_path):
    """Without this "bob" could not be named at all while "bobby" is connected."""
    bot = _bot(tmp_path)
    bob, _bobby = _connect(bot, ("bob", "1"), ("bobby", "2"))

    assert bot.find_clients("bob") == [bob]
    assert bot.find_client("bob") is bob


def test_the_exact_match_is_case_insensitive(tmp_path):
    bot = _bot(tmp_path)
    bob, _bobby = _connect(bot, ("Bob", "1"), ("BobbyTables", "2"))

    assert bot.find_clients("bOB") == [bob]


def test_two_players_with_the_same_name_are_both_returned(tmp_path):
    """Names are not unique on most of these engines, so an exact match can be ambiguous."""
    bot = _bot(tmp_path)
    first, second = _connect(bot, ("Bob", "1"), ("Bob", "2"))

    assert bot.find_clients("Bob") == [first, second]


def test_a_slot_id_beats_every_name(tmp_path):
    bot = _bot(tmp_path)
    _bobby, bobcat = _connect(bot, ("bobby", "1"), ("bobcat", "2"))

    assert bot.find_clients("2") == [bobcat]


def test_a_single_substring_match_still_works(tmp_path):
    bot = _bot(tmp_path)
    bobby, _joe = _connect(bot, ("bobby", "1"), ("joe", "2"))

    assert bot.find_clients("obb") == [bobby]
    assert bot.find_client("obb") is bobby


def test_nobody_matching_is_an_empty_list(tmp_path):
    bot = _bot(tmp_path)
    _connect(bot, ("bobby", "1"))

    assert bot.find_clients("ghost") == []
    assert bot.find_client("ghost") is None


def test_a_malformed_dbid_matches_nobody_rather_than_falling_through_to_names(tmp_path):
    bot = _bot(tmp_path)
    _connect(bot, ("@home", "1"))

    assert bot.find_clients("@home") == []


# -- the commands refuse ---------------------------------------------------------------------


def _admin() -> Client:
    return Client(guid="A", name="Admin", group_bits=128, cid="0", id=1)


def _ambiguous(console) -> tuple[Client, Client]:  # noqa: ANN001
    """Make "bob" a handle that matches two connected players."""
    bobby = Client(guid="B", name="bobby", cid="1", id=2)
    bobcat = Client(guid="C", name="bobcat", cid="2", id=3)
    console.register_clients("bob", [bobby, bobcat])
    return bobby, bobcat


@pytest.mark.asyncio
async def test_ban_refuses_an_ambiguous_name_and_lists_the_candidates(console):
    plugin = AdminPlugin(console)
    plugin.register_commands()
    proc = CommandProcessor(console.command_registry, console)
    _ambiguous(console)

    await proc.handle(_admin(), "!ban bob cheating")

    assert console.banned == []
    said = console.told[-1][1]
    assert "2 players match" in said
    assert "bobby [1]" in said and "bobcat [2]" in said


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        "!kick bob spam",
        "!ban bob cheating",
        "!permban bob cheating",
        "!tempban bob 2h aimbot",
        "!warn bob lang",
        "!runas bob !help",
        "!unmask bob",
    ],
)
async def test_no_command_acts_on_an_ambiguous_name(console, line):  # noqa: ANN001
    plugin = AdminPlugin(console)
    plugin.register_commands()
    proc = CommandProcessor(console.command_registry, console)
    _ambiguous(console)

    await proc.handle(_admin(), line)

    assert console.kicked == []
    assert console.banned == []
    assert console.tempbanned == []
    assert console.warned == []
    assert "players match" in console.told[-1][1]


@pytest.mark.asyncio
async def test_a_single_match_still_acts(console):
    """The refusal must not affect an unambiguous handle."""
    plugin = AdminPlugin(console)
    plugin.register_commands()
    proc = CommandProcessor(console.command_registry, console)
    bob = Client(guid="B", name="bob", cid="4", id=2)
    console.register_client("bob", bob)

    await proc.handle(_admin(), "!kick bob spam")

    assert console.kicked[-1][0] is bob


@pytest.mark.asyncio
async def test_spamins_refuses_an_ambiguous_name(console):
    """Read-only, but it would otherwise report one player's score under another's name."""
    SpamcontrolPlugin(console).start()
    proc = CommandProcessor(console.command_registry, console)
    _ambiguous(console)

    await proc.handle(_admin(), "!spamins bob")

    assert "players match" in console.told[-1][1]


@pytest.mark.asyncio
async def test_ci_refuses_an_ambiguous_name(console):
    PingwatchPlugin(console, {"settings": {"settle_time": 0}}).start()
    proc = CommandProcessor(console.command_registry, console)
    _ambiguous(console)

    await proc.handle(_admin(), "!ci bob")

    assert console.kicked == []
    assert "players match" in console.told[-1][1]
