"""Urban Terror's two abuse channels: the over-long-name exploit and radio spam.

A name longer than the userinfo string holds overflows it, and radio is a chat channel that can be
flooded like any other.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.q3.profiles import IOURT42, Q3
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.q3.urt import UrtParser
from b3.plugins.admin import AdminPlugin
from b3.plugins.spamcontrol import SpamcontrolPlugin
from b3.runtime.bot import Bot

GUID = "A337702493AF67BB0B0F8565CE8BC6C7"
LONG = "x" * 40


class FakeRcon:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


# -- the parser truncates -----------------------------------------------------------------------


def test_a_name_over_the_limit_is_cut_and_flagged():
    p = UrtParser(IOURT42)

    p.parse_line(f"ClientUserinfoChanged: 3 n\\{LONG}\\t\\2\\cl_guid\\{GUID}")

    client = p.clients.get_by_cid("3")
    assert client.name == "x" * 32  # the protocol's limit, not the 40 the client claimed
    assert client.name_overflow is True


def test_a_name_at_the_limit_is_left_alone():
    p = UrtParser(IOURT42)

    p.parse_line(f"ClientUserinfoChanged: 3 n\\{'x' * 32}\\cl_guid\\{GUID}")

    client = p.clients.get_by_cid("3")
    assert client.name == "x" * 32
    assert client.name_overflow is False


def test_an_engine_that_declares_no_limit_keeps_the_name():
    """Only titles that declare `name_max_length` are policed."""
    p = Q3Parser(Q3)

    p.parse_line(f"ClientUserinfoChanged: 3 n\\{LONG}\\cl_guid\\{GUID}")

    client = p.clients.get_by_cid("3")
    assert client.name == LONG
    assert client.name_overflow is False


# -- the runtime kicks --------------------------------------------------------------------------


def _bot(tmp_path, *, allow_long_names: bool = False) -> tuple[Bot, FakeRcon]:  # noqa: ANN001
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="iourt42", allow_long_names=allow_long_names),
        plugins=[PluginEntry(name="admin")],
    )
    rcon = FakeRcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    rcon.commands.clear()  # the profile's startup cvars
    return bot, rcon


JOIN = [
    "ClientConnect: 3",
    f"ClientUserinfoChanged: 3 n\\{LONG}\\t\\2\\cl_guid\\{GUID}",
    "ClientBegin: 3",
]


@pytest.mark.asyncio
async def test_an_overflowing_name_is_kicked_by_default(tmp_path):
    bot, rcon = _bot(tmp_path)

    await bot.replay(JOIN)

    assert rcon.commands == ["clientkick 3"]
    assert bot.clients.get_by_cid("3").rejected is True


@pytest.mark.asyncio
async def test_the_kick_can_be_turned_off_and_the_name_is_still_truncated(tmp_path):
    bot, rcon = _bot(tmp_path, allow_long_names=True)

    await bot.replay(JOIN)

    assert rcon.commands == []
    client = bot.clients.get_by_cid("3")
    assert client.rejected is False
    assert client.name == "x" * 32  # the setting governs the kick, not the truncation


@pytest.mark.asyncio
async def test_an_ordinary_name_is_not_kicked(tmp_path):
    bot, rcon = _bot(tmp_path)

    await bot.replay(
        ["ClientConnect: 3", f"ClientUserinfoChanged: 3 n\\Bob\\cl_guid\\{GUID}", "ClientBegin: 3"]
    )

    assert rcon.commands == []
    assert bot.clients.get_by_cid("3").rejected is False


@pytest.mark.asyncio
async def test_a_rename_mid_game_is_caught_too(tmp_path):
    """A userinfo change overflows the same string, so a reconnect is not needed to exploit it."""
    bot, rcon = _bot(tmp_path)
    await bot.replay(
        ["ClientConnect: 3", f"ClientUserinfoChanged: 3 n\\Bob\\cl_guid\\{GUID}", "ClientBegin: 3"]
    )
    assert rcon.commands == []

    await bot.feed_line(f"ClientUserinfoChanged: 3 n\\{LONG}\\cl_guid\\{GUID}")

    assert rcon.commands  # kicked without ever rejoining
    assert bot.clients.get_by_cid("3").rejected is True


# -- radio spam ---------------------------------------------------------------------------------


def _radio(group: str = "7", msg: str = "2", location: str = "New Alley", text: str = "Enemy!"):  # noqa: ANN202
    return Event(
        EventType.CLIENT_RADIO,
        data={"msg_group": group, "msg_id": msg, "location": location, "text": text},
        client=None,
    )


@pytest.mark.asyncio
async def test_radio_spam_is_scored(console):
    plugin = SpamcontrolPlugin(console)
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)

    event = _radio()
    event.client = bob
    await console.bus.publish(event)

    assert plugin.score(bob) == 1  # the plugin subscribes to CLIENT_RADIO, not only to chat


@pytest.mark.asyncio
async def test_the_same_radio_call_from_a_new_location_still_counts_as_a_repeat(console):
    """The location changes as the player moves, so it must not be part of what is scored."""
    plugin = SpamcontrolPlugin(console)
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)

    for location in ("New Alley", "Back Alley", "The Bridge"):
        event = _radio(location=location)
        event.client = bob
        await console.bus.publish(event)

    assert plugin.score(bob) == 7  # 1 for the first, then 3 + 3 for two repeats


@pytest.mark.asyncio
async def test_enough_radio_spam_earns_a_warning(console):
    plugin = SpamcontrolPlugin(console, {"settings": {"max_spamins": 5}})
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)

    for _ in range(3):
        event = _radio()
        event.client = bob
        await console.bus.publish(event)

    assert console.warned  # 1 + 3 + 3 = 7, past the 5-point threshold
