"""Plugin-facing services: scheduled work and message templates, wired through a real Bot."""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.plugin import Plugin
from b3.core.scheduler import CronError
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot, _resolve_timezone

GADMIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
GBOB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class FakeRcon:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


class TickerPlugin(Plugin):
    """A plugin that does timed work, like the legacy scheduler/pingwatch plugins."""

    def __init__(self, console, config=None) -> None:  # noqa: ANN001
        super().__init__(console, config)
        self.ticks = 0

    def on_startup(self) -> None:
        self.schedule(self.every_second, second="*")

    def every_second(self) -> None:
        self.ticks += 1


def _bot(tmp_path, *, messages=None, clock=None, line_length=90, tz="UTC"):
    config = Config(
        bot=BotConfig(
            database=f"sqlite:///{tmp_path / 'b3.sqlite'}",
            line_length=line_length,
            time_zone=tz,
        ),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin")],
        messages=messages or {},
    )
    rcon = FakeRcon()
    bot = Bot(config, rcon=rcon, clock=clock or FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    rcon.commands.clear()  # the profile's startup cvars are not what these tests are about
    return bot, rcon


def _plugin_tasks(bot, plugin=None) -> list[str]:  # noqa: ANN001
    """Schedule names owned by plugins. The bot schedules a player-list sync and the admin
    plugin a warning-kick check, so tests name the plugin they mean."""
    return [
        t.name
        for t in bot.scheduler.tasks
        if t.plugin is not None and (plugin is None or t.plugin is plugin)
    ]


# -- Plugin.schedule -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plugin_can_schedule_timed_work(tmp_path):
    clock = FakeClock()
    bot, _ = _bot(tmp_path, clock=clock)
    plugin = TickerPlugin(bot)
    bot.add_plugin(plugin, "ticker")
    plugin.start()

    await bot.scheduler.tick()
    clock.advance(1)
    await bot.scheduler.tick()

    assert plugin.ticks == 2


@pytest.mark.asyncio
async def test_a_disabled_plugin_does_not_run_its_schedule(tmp_path):
    clock = FakeClock()
    bot, _ = _bot(tmp_path, clock=clock)
    plugin = TickerPlugin(bot)
    plugin.start()

    plugin.disable()
    await bot.scheduler.tick()
    assert plugin.ticks == 0

    plugin.enable()
    clock.advance(1)
    await bot.scheduler.tick()
    assert plugin.ticks == 1


@pytest.mark.asyncio
async def test_unloading_a_plugin_removes_its_schedules(tmp_path):
    bot, _ = _bot(tmp_path)
    plugin = TickerPlugin(bot)
    plugin.start()
    assert _plugin_tasks(bot, plugin) == ["TickerPlugin.every_second"]

    plugin.unload()
    assert _plugin_tasks(bot, plugin) == []  # other owners' schedules stay


def test_a_plugin_schedule_is_named_after_the_plugin(tmp_path):
    bot, _ = _bot(tmp_path)
    plugin = TickerPlugin(bot)
    plugin.start()
    assert _plugin_tasks(bot, plugin) == ["TickerPlugin.every_second"]


def test_a_bad_schedule_raises_when_the_plugin_registers_it(tmp_path):
    bot, _ = _bot(tmp_path)

    class BrokenPlugin(Plugin):
        def on_startup(self) -> None:
            self.schedule(lambda: None, hour="99")

    with pytest.raises(CronError):
        BrokenPlugin(bot).start()


# -- time zone resolution --------------------------------------------------


def test_utc_resolves_without_the_iana_database():
    assert _resolve_timezone("UTC").utcoffset(None).total_seconds() == 0
    assert _resolve_timezone("gmt") is not None


def test_a_named_zone_resolves():
    assert _resolve_timezone("Europe/Chisinau") is not None


def test_an_unknown_zone_falls_back_to_local_time(caplog):
    with caplog.at_level("WARNING"):
        assert _resolve_timezone("Mars/Olympus_Mons") is None
    assert "unknown time_zone" in caplog.text


def test_an_empty_zone_means_local_time():
    assert _resolve_timezone("") is None


# -- message templates end to end ------------------------------------------


@pytest.mark.asyncio
async def test_an_operator_can_customise_a_command_reply(tmp_path):
    bot, rcon = _bot(tmp_path, messages={"kicked": "^1{name}^7 removed! reason: {reason}"})
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!kick Bob spam",
        ]
    )
    # The wording is what this test is about; the bot's prefix rides in front of every reply and is
    # tested on its own (test_message_prefix.py).
    assert any("^1Bob^7 removed! reason: spam" in c for c in rcon.commands)


@pytest.mark.asyncio
async def test_default_wording_is_unchanged_without_config(tmp_path):
    bot, rcon = _bot(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!kick Bob spam",
        ]
    )
    assert any("Bob was kicked (spam)" in c for c in rcon.commands)


@pytest.mark.asyncio
async def test_the_framework_messages_are_customisable_too(tmp_path):
    bot, rcon = _bot(tmp_path, messages={"unknown_command": "eh? '{command}' is not a thing"})
    await bot.replay([f"J;{GBOB};2;Bob", "say;x;2;Bob;!nonsense"])
    assert any("eh? 'nonsense' is not a thing" in c for c in rcon.commands)


# -- output wrapping -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_long_reply_is_split_across_several_lines(tmp_path):
    """The classic bot's getWrap: without this the game truncates the line mid-word.

    **The prefixes are inside the budget**, which is the point of them living in `Messages` rather
    than being added at the send site: every line here is within the engine's limit *including* the
    `(b3):` and `[pm]` markers. Added afterwards they would push the first line past it, and on Call
    of Duty the engine drops the overflow rather than wrapping it.
    """
    bot, rcon = _bot(tmp_path, line_length=30)
    await bot.feed_line(f"J;{GBOB};2;Bob")
    bob = bot.clients.get_by_cid("2")

    bot.tell(bob, "the quick brown fox jumps over the lazy dog and keeps on running")

    told = [c for c in rcon.commands if c.startswith("tell 2 ")]
    assert len(told) > 1
    assert all(len(c) - len("tell 2 ") <= 30 for c in told)
    rejoined = " ".join(c[len("tell 2 ") :] for c in told)
    assert rejoined.startswith("^2(b3)^7: ^8[pm]^7 the quick brown fox")
    assert rejoined.endswith("keeps on running")


@pytest.mark.asyncio
async def test_say_is_wrapped_too(tmp_path):
    bot, rcon = _bot(tmp_path, line_length=25)
    bot.say("a much longer broadcast message than one line can hold")
    said = [c for c in rcon.commands if c.startswith("say ")]
    assert len(said) > 1


@pytest.mark.asyncio
async def test_empty_output_sends_nothing(tmp_path):
    bot, rcon = _bot(tmp_path)
    bot.say("")
    assert rcon.commands == []
