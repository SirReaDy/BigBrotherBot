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
async def test_a_radio_call_is_scored_on_the_gap_and_not_on_the_words(console):
    """A radio message is chosen from a fixed menu, so repeating one is ordinary and the content
    says almost nothing. What marks out somebody abusing it is the rate — which is why this is not
    scored the way chat is, and why the classic gave it a scorer of its own."""
    plugin = SpamcontrolPlugin(console)
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)

    event = _radio()
    event.client = bob
    await console.bus.publish(event)

    assert plugin.score(bob) == 0  # nothing to compare the first one against


@pytest.mark.asyncio
async def test_a_burst_of_radio_calls_scores(console):
    plugin = SpamcontrolPlugin(console)
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)

    for _ in range(2):
        event = _radio()
        event.client = bob
        await console.bus.publish(event)

    # No gap at all: within 20s (+1), within 2s (+1) and the same call again (+3), within 1s (+3).
    assert plugin.score(bob, falloff=2.0) == 8


@pytest.mark.asyncio
async def test_radio_calls_a_minute_apart_are_not_spam(console):
    plugin = SpamcontrolPlugin(console)
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)

    for _ in range(4):
        event = _radio()
        event.client = bob
        await console.bus.publish(event)
        console.clock.advance(60)

    assert console.muted == {}
    assert console.warned == []


@pytest.mark.asyncio
async def test_the_same_radio_call_from_a_new_location_still_counts_as_a_repeat(console):
    """The location changes as the player moves, so it must not be part of what is scored."""
    plugin = SpamcontrolPlugin(console, {"radio": {"max_spamins": 100}})
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)

    for location in ("New Alley", "Back Alley", "The Bridge"):
        event = _radio(location=location)
        event.client = bob
        await console.bus.publish(event)

    assert plugin.score(bob, falloff=2.0) == 16  # nothing for the first, then 8 and 8


@pytest.mark.asyncio
async def test_enough_radio_spam_is_answered_with_a_mute(console):
    """Not a warning: the radio is a menu of buttons, so telling somebody to stop does not stop
    them, and they are not reading the chat a warning arrives in."""
    plugin = SpamcontrolPlugin(console, {"radio": {"max_spamins": 5, "mute_seconds": 30}})
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)
    console.clients.add(bob)

    for _ in range(2):
        event = _radio()
        event.client = bob
        await console.bus.publish(event)

    assert console.muted_until(bob) > console.clock.now()
    assert console.warned == []
    assert any("radio is off" in text for _who, text in console.told)


@pytest.mark.asyncio
async def test_a_muted_player_is_not_scored_further(console):
    """The classic kept a second deadline of its own for this, computed from the mute length rather
    than read from it, so the two could drift apart."""
    plugin = SpamcontrolPlugin(console, {"radio": {"max_spamins": 5, "mute_seconds": 30}})
    plugin.start()
    bob = Client(guid="B", name="Bob", cid="2", id=7)
    console.clients.add(bob)
    for _ in range(2):
        event = _radio()
        event.client = bob
        await console.bus.publish(event)
    before = plugin.score(bob, falloff=2.0)

    for _ in range(3):
        event = _radio()
        event.client = bob
        await console.bus.publish(event)

    assert plugin.score(bob, falloff=2.0) == before


@pytest.mark.asyncio
async def test_a_game_with_no_mute_verb_warns_instead(console, caplog):
    console.player_verbs = set()
    with caplog.at_level("WARNING"):
        plugin = SpamcontrolPlugin(console, {"radio": {"max_spamins": 5, "mute_seconds": 30}})
        plugin.start()
    assert "no `mute` verb" in caplog.text
    bob = Client(guid="B", name="Bob", cid="2", id=7)
    console.clients.add(bob)

    for _ in range(2):
        event = _radio()
        event.client = bob
        await console.bus.publish(event)

    assert console.muted == {}
    assert console.warned


@pytest.mark.asyncio
async def test_an_admin_may_use_the_radio(console):
    plugin = SpamcontrolPlugin(console, {"radio": {"max_spamins": 1}})
    plugin.start()
    boss = Client(guid="B", name="Boss", cid="2", id=7, group_bits=64)
    console.clients.add(boss)

    for _ in range(3):
        event = _radio()
        event.client = boss
        await console.bus.publish(event)

    assert console.muted == {}
    assert console.warned == []
