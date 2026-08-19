"""The `welcome` plugin — greeting arrivals with what the database already knows.

The classic plugin's own tests cover its config parsing and almost nothing of its behaviour, so these
are written from its code and from what the messages promise. The behaviour worth pinning is the set
of moments it must *not* speak: within five minutes of the bot starting (when a whole server
authenticates at once), for a player greeted within `min_gap`, and for one who has already left
before the delay came round — which the classic could not do at all, because its timer had already
been handed the client.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.welcome import MAX_GREETING, WelcomePlugin

#: Long enough that the plugin is past its startup silence in every test that wants to be greeted.
PAST_SILENCE = 400


def _welcome(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = WelcomePlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    console.clock.advance(PAST_SILENCE)
    return plugin


def _client(name="Bob", cid="2", id_=7, bits=0, connections=1, last_visit=0):  # noqa: ANN001, ANN202
    return Client(
        guid=name[0].upper() * 4,
        name=name,
        cid=cid,
        id=id_,
        group_bits=bits,
        connections=connections,
        last_visit=last_visit,
    )


async def _arrive(console, plugin, client, delay=30):  # noqa: ANN001, ANN202
    """Authenticate a player, then let the delay pass and the plugin's task run."""
    console.clients.add(client)
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))
    console.clock.advance(delay + 1)
    plugin._greet_due()


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- who gets which message ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_first_visit_is_told_about_help_and_announced(console):
    plugin = _welcome(console)
    bob = _client(connections=1)

    await _arrive(console, plugin, bob)

    assert any("first visit" in text and "!help" in text for text in _told(console, bob))
    assert any("everyone welcome Bob" in text for text in console.said)


@pytest.mark.asyncio
async def test_a_returning_unregistered_player_is_told_about_register(console):
    """The reason there are two returning messages: this one advertises `!register`, and sending it
    to somebody who has already registered would be nagging them about it forever."""
    plugin = _welcome(console)
    bob = _client(connections=4, last_visit=1_000)

    await _arrive(console, plugin, bob)

    assert any("!register" in text for text in _told(console, bob))


@pytest.mark.asyncio
async def test_a_returning_registered_player_is_told_their_group_instead(console):
    plugin = _welcome(console)
    bob = _client(connections=4, bits=8, last_visit=1_000)  # mod

    await _arrive(console, plugin, bob)

    told = " ".join(_told(console, bob))
    assert "Moderator" in told
    assert "!register" not in told


@pytest.mark.asyncio
async def test_a_regular_is_not_announced_to_the_whole_server_again(console):
    """`newb_connections`: a player who has been here twenty times does not need announcing."""
    plugin = _welcome(console, newb_connections=15)
    bob = _client(connections=20, last_visit=1_000)

    await _arrive(console, plugin, bob)

    assert _told(console, bob)  # privately greeted
    assert console.said == []  # but not announced


@pytest.mark.asyncio
async def test_the_last_visit_is_a_date_and_not_a_number(console):
    plugin = _welcome(console)
    bob = _client(connections=4, last_visit=1_000_000)

    await _arrive(console, plugin, bob)

    assert console.format_time(1_000_000) in " ".join(_told(console, bob))


@pytest.mark.asyncio
async def test_a_player_the_database_has_no_date_for_is_told_so(console):
    """`connections` says they have been here before but the row carried no timestamp — an imported
    database is exactly that. "Unknown" is truthful; a formatted epoch zero would not be."""
    plugin = _welcome(console)
    bob = _client(connections=4, last_visit=0)

    await _arrive(console, plugin, bob)

    assert "unknown" in " ".join(_told(console, bob))


@pytest.mark.asyncio
async def test_each_message_can_be_switched_off(console):
    plugin = _welcome(console, welcome_first=False, announce_first=False)
    bob = _client(connections=1)

    await _arrive(console, plugin, bob)

    assert _told(console, bob) == []
    assert console.said == []


# -- when it must not speak ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nobody_is_greeted_in_the_first_five_minutes(console):
    """A bot restarted mid-match authenticates the whole server at once. Greeting all of them is a
    wall of text that teaches nobody anything, and it is how the classic's own five-minute rule came
    to exist."""
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = WelcomePlugin(console, None)
    plugin.start()  # no clock advance: the bot has just started
    bob = _client()

    await _arrive(console, plugin, bob)

    assert _told(console, bob) == []
    assert console.said == []


@pytest.mark.asyncio
async def test_a_player_greeted_recently_is_not_greeted_again(console):
    plugin = _welcome(console, min_gap=3600)
    bob = _client(connections=4, last_visit=console.clock.epoch() - 60)

    await _arrive(console, plugin, bob)

    assert _told(console, bob) == []


@pytest.mark.asyncio
async def test_min_gap_can_be_turned_off(console):
    plugin = _welcome(console, min_gap=0)
    bob = _client(connections=4, last_visit=console.clock.epoch() - 60)

    await _arrive(console, plugin, bob)

    assert _told(console, bob)


@pytest.mark.asyncio
async def test_somebody_who_leaves_before_the_delay_is_not_greeted(console):
    """The classic had handed the client to a timer, so it greeted players who had gone — a private
    message to a slot that by then may hold somebody else."""
    plugin = _welcome(console, delay=30)
    bob = _client(connections=4, last_visit=1_000)
    console.clients.add(bob)

    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=bob))
    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bob, data="left"))
    console.clock.advance(31)
    plugin._greet_due()

    assert _told(console, bob) == []


@pytest.mark.asyncio
async def test_a_slot_taken_over_by_somebody_else_is_not_greeted_either(console):
    """Several engines never report a departure at all, so the roster poll is what removes a player —
    and that can land inside the delay."""
    plugin = _welcome(console, delay=30)
    bob = _client(name="Bob", cid="2", connections=4, last_visit=1_000)
    console.clients.add(bob)
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=bob))

    console.clients.remove("2")
    console.clients.add(_client(name="Ann", cid="2", id_=9, connections=4, last_visit=1_000))
    console.clock.advance(31)
    plugin._greet_due()

    assert console.told == []


@pytest.mark.asyncio
async def test_the_greeting_waits_for_the_delay(console):
    plugin = _welcome(console, delay=30)
    bob = _client(connections=1)
    console.clients.add(bob)

    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=bob))
    console.clock.advance(10)
    plugin._greet_due()
    assert _told(console, bob) == []

    console.clock.advance(25)
    plugin._greet_due()
    assert _told(console, bob)


def test_a_delay_outside_the_sensible_range_is_refused(console, caplog):
    """Under fifteen seconds the player is still loading the map; past ninety they have stopped
    reading chat. The classic clamped it to 30 silently."""
    with caplog.at_level("WARNING"):
        plugin = _welcome(console, delay=5)

    assert plugin.settings["delay"] == 30
    assert "outside" in caplog.text


# -- a player's own greeting ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_greeting_is_announced_when_its_owner_arrives(console):
    plugin = _welcome(console)
    bob = _client(connections=4, bits=8, last_visit=1_000)
    bob.greeting = "hello from {name}"

    await _arrive(console, plugin, bob)

    assert any("Bob joined: hello from Bob" in text for text in console.said)


@pytest.mark.asyncio
async def test_setting_and_clearing_a_greeting(console):
    _welcome(console)
    bob = _client(bits=8)  # mod: the level `!greeting` sits at
    console.clients.add(bob)

    await _run(console, bob, "!greeting hi all, {name} here")
    assert bob.greeting == "hi all, {name} here"
    assert "hi all, Bob here" in _told(console, bob)[-1]

    await _run(console, bob, "!greeting none")
    assert bob.greeting == ""
    assert "cleared" in _told(console, bob)[-1]


@pytest.mark.asyncio
async def test_asking_with_no_argument_reports_what_you_have(console):
    _welcome(console)
    bob = _client(bits=8)
    console.clients.add(bob)

    await _run(console, bob, "!greeting")
    assert "no greeting" in _told(console, bob)[-1]

    bob.greeting = "hello"
    await _run(console, bob, "!greeting")
    assert "your greeting is: hello" in _told(console, bob)[-1]


@pytest.mark.asyncio
async def test_a_placeholder_that_does_not_exist_is_refused_by_name(console):
    """The classic validated by rendering at greeting time, so a typo failed on somebody else's
    arrival — in front of everybody, and never in front of the player who wrote it."""
    _welcome(console)
    bob = _client(bits=8)
    console.clients.add(bob)

    await _run(console, bob, "!greeting hello from {nickname}")

    assert "{nickname}" in _told(console, bob)[-1]
    assert bob.greeting == ""


@pytest.mark.asyncio
async def test_a_greeting_longer_than_the_column_is_refused(console):
    """The classic checked 255 characters against a `varchar(128)`, so anything in between was
    accepted and then silently truncated by the database."""
    _welcome(console)
    bob = _client(bits=8)
    console.clients.add(bob)

    await _run(console, bob, "!greeting " + "x" * (MAX_GREETING + 1))

    assert f"limit is {MAX_GREETING}" in _told(console, bob)[-1]
    assert bob.greeting == ""


@pytest.mark.asyncio
async def test_a_greeting_from_a_classic_database_is_said_as_it_is(console):
    """An imported greeting uses `%(name)s`, which means nothing to this bot's formatter. Saying it
    verbatim costs that player a placeholder; raising would cost somebody else their arrival."""
    plugin = _welcome(console)
    bob = _client(connections=4, last_visit=1_000)
    bob.greeting = "hi from %(name)s"

    await _arrive(console, plugin, bob)

    assert any("hi from %(name)s" in text for text in console.said)


def test_the_greeting_level_comes_from_the_config(console):
    _welcome(console, greeting_level=40)

    assert console.command_registry.get("greeting").min_level == 40


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_player_joining_is_greeted_over_rcon(tmp_path):
    """A player who has been here before, from real log lines: the bot has to have read their record
    to know that, and `last_visit` only exists because it is captured before the session is saved."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            return ""

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="welcome")],
    )
    clock = FakeClock()
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=clock)
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = WelcomePlugin(bot, {"settings": {"min_gap": 0}})
    bot.add_plugin(plugin, "welcome")
    bot.start()
    admin.start()
    plugin.start()
    clock.advance(PAST_SILENCE)

    guid = "b" * 32
    await bot.replay([f"J;{guid};2;Bob"])
    await bot.bus.drain()
    clock.advance(31)
    plugin._greet_due()

    assert any("first visit" in c for c in rcon.commands)

    # Again, as a returning player: the database now has a row, so the *other* message goes out.
    rcon.commands.clear()
    await bot.replay([f"Q;{guid};2;Bob", f"J;{guid};2;Bob"])
    await bot.bus.drain()
    clock.advance(31)
    plugin._greet_due()

    assert any("welcome back" in c for c in rcon.commands)
    bot.storage.close()
