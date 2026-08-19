"""The `afk` plugin — asking players who look absent whether they are.

The classic plugin has the most thorough test suite of any in that tree (nine files), and what it
pins is mostly the *refusals*: a bot is never asked, a spectator is never asked, an immune player is
never asked, a player with no recorded activity is never kicked, and nobody is kicked if that would
take the server below `min_ingame_humans`. Those are the tests worth keeping, because every one of
them is a case where an AFK kicker misfiring costs the server a real player.

The shape of the original is kept too, deliberately: nothing is swept on a timer. A check happens
only when there is a reason — several deaths in a row without activity, or another player saying
"afk" in chat.
"""

from __future__ import annotations

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.cod.parser import KillData
from b3.plugins.admin import AdminPlugin
from b3.plugins.afk import AfkPlugin


def _afk(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = AfkPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


def _players(console, *names, team="red", bits=0):  # noqa: ANN001, ANN202
    made = []
    for index, name in enumerate(names, start=1):
        client = Client(
            guid=name[0].upper() * 4, name=name, cid=str(index), id=index, group_bits=bits
        )
        client.team = team
        console.clients.add(client)
        made.append(client)
    return made


async def _act(console, client):  # noqa: ANN001, ANN202
    """Something the bot can see, so the player has a last-seen time."""
    await console.bus.publish(Event(EventType.CLIENT_SPAWN, client=client))


async def _die(console, victim, killer):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(
            EventType.CLIENT_KILL,
            client=killer,
            target=victim,
            data=KillData(
                weapon="ak47", damage=100, hit_location="chest", means_of_death="MOD_RIFLE"
            ),
        )
    )


async def _say(console, client, text):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_SAY, client=client, data=text))


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- the two triggers ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dying_repeatedly_without_doing_anything_gets_you_asked(console):
    _afk(console, consecutive_deaths=3, inactivity_threshold=30, min_ingame_humans=1)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)  # idle for longer than the threshold

    for _ in range(3):
        await _die(console, bob, ann)

    assert any("are you AFK" in text for text in _told(console, bob))
    assert any("Bob may be AFK" in text for text in console.said)
    assert console.kicked == []  # asked, not kicked


@pytest.mark.asyncio
async def test_two_deaths_are_not_enough(console):
    plugin = _afk(console, consecutive_deaths=3, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)

    await _die(console, bob, ann)
    await _die(console, bob, ann)

    assert _told(console, bob) == []
    assert plugin.activity(bob).deaths == 2


@pytest.mark.asyncio
async def test_doing_something_resets_the_death_count(console):
    plugin = _afk(console, consecutive_deaths=3, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)

    await _die(console, bob, ann)
    await _die(console, bob, ann)
    await _act(console, bob)  # they were there after all
    await _die(console, bob, ann)

    assert plugin.activity(bob).deaths == 1
    assert _told(console, bob) == []


@pytest.mark.asyncio
async def test_a_suicide_counts_as_being_there(console):
    """It is something the player did. The classic drew the same line: repeatedly killing yourself is
    annoying rather than absent."""
    plugin = _afk(console, consecutive_deaths=2, inactivity_threshold=30)
    bob, _ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)

    for _ in range(3):
        await console.bus.publish(
            Event(EventType.CLIENT_SUICIDE, client=bob, target=bob, data=None)
        )

    assert plugin.activity(bob).deaths == 0
    assert _told(console, bob) == []


@pytest.mark.asyncio
async def test_somebody_saying_afk_in_chat_checks_everybody(console):
    """What players type is "bob is afk", not a command. The word is the trigger, as classically."""
    _afk(console, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    await _act(console, ann)
    console.clock.advance(60)

    await _say(console, ann, "bob is afk I think")

    # Ann has just spoken, which is activity, so only Bob is suspected.
    assert any("are you AFK" in text for text in _told(console, bob))
    assert _told(console, ann) == []


@pytest.mark.asyncio
async def test_the_chat_trigger_is_throttled(console):
    """An argument about who is AFK must not sweep the server on every line of it."""
    plugin = _afk(console, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)

    await _say(console, ann, "afk?")
    first = len(console.said)
    plugin.activity(bob).answer_by = 0.0  # pretend the question expired, to see a second one
    await _say(console, ann, "afk!")

    assert len(console.said) == first  # the second line swept nothing


# -- who is never asked --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bot_is_never_asked(console):
    """It cannot answer. `Client.is_bot` is set where the AI's guid is recognised and dropped, which
    is the only moment the fact exists — afterwards a bot looks like an unidentified player."""
    _afk(console, inactivity_threshold=30)
    bob, _ann, _sue = _players(console, "Bob", "Ann", "Sue")
    bob.is_bot = True
    await _act(console, bob)
    console.clock.advance(60)

    await _say(console, _ann, "afk")

    assert _told(console, bob) == []


@pytest.mark.asyncio
async def test_a_spectator_is_never_asked(console):
    """Doing nothing is what spectating is."""
    _afk(console, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    bob.team = "spec"
    await _act(console, bob)
    console.clock.advance(60)

    await _say(console, ann, "afk")

    assert _told(console, bob) == []


@pytest.mark.asyncio
async def test_an_immune_player_is_never_asked(console):
    _afk(console, inactivity_threshold=30, immunity_level=40)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    bob.group_bits = 16  # admin, level 40
    await _act(console, bob)
    console.clock.advance(60)

    await _say(console, ann, "afk")

    assert _told(console, bob) == []


@pytest.mark.asyncio
async def test_a_player_nobody_has_seen_do_anything_is_left_alone(console):
    """Not "assumed absent": *unknown*. It is what makes a fresh join, a map change and a bot restart
    all safe without a special case for each of them."""
    _afk(console, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    console.clock.advance(300)

    await _say(console, ann, "afk")

    assert _told(console, bob) == []


# -- the last chance -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silence_gets_you_kicked_when_the_clock_runs_out(console):
    plugin = _afk(console, inactivity_threshold=30, last_chance_delay=20)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)
    await _say(console, ann, "afk")

    console.clock.advance(21)
    plugin._check_deadlines()

    assert [c.name for c, _reason, _admin in console.kicked] == ["Bob"]


@pytest.mark.asyncio
async def test_answering_calls_the_kick_off_and_says_so(console):
    plugin = _afk(console, inactivity_threshold=30, last_chance_delay=20)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)
    await _say(console, ann, "afk")

    await _say(console, bob, "sorry, back")
    console.clock.advance(21)
    plugin._check_deadlines()

    assert console.kicked == []
    assert any("not AFK" in text for text in _told(console, bob))


@pytest.mark.asyncio
async def test_a_suspect_is_not_asked_twice(console):
    _afk(console, inactivity_threshold=30, consecutive_deaths=1)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)

    await _die(console, bob, ann)
    await _die(console, bob, ann)

    assert len([t for t in _told(console, bob) if "are you AFK" in t]) == 1


@pytest.mark.asyncio
async def test_the_server_is_never_emptied_by_this(console):
    """`min_ingame_humans`. An idle player in a server is a better outcome than an empty one, and the
    population is re-counted when the kick is due — people leave inside those twenty seconds."""
    plugin = _afk(console, inactivity_threshold=30, min_ingame_humans=2, consecutive_deaths=0)
    bob, ann, sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)
    await _say(console, ann, "afk")
    assert any("are you AFK" in text for text in _told(console, bob))

    console.clients.remove(sue.cid or "")  # now only Bob and Ann are here
    console.clock.advance(21)
    plugin._check_deadlines()

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_round_change_forgets_everything(console):
    """A player loading a new map has no recent activity, and the slowest computer on the server must
    not be the first thing this plugin kicks."""
    plugin = _afk(console, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)
    await _say(console, ann, "afk")
    assert plugin.activity(bob).answer_by

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))

    assert plugin.activity(bob).answer_by == 0.0
    assert plugin.activity(bob).last_seen is None
    console.clock.advance(60)
    plugin._check_deadlines()
    assert console.kicked == []


@pytest.mark.asyncio
async def test_disabling_the_plugin_drops_the_questions_it_asked(console):
    plugin = _afk(console, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)
    await _say(console, ann, "afk")

    plugin.disable()
    console.clock.advance(30)
    plugin._check_deadlines()

    assert console.kicked == []


@pytest.mark.asyncio
async def test_leaving_clears_the_record_so_the_next_player_in_the_slot_starts_fresh(console):
    plugin = _afk(console, inactivity_threshold=30)
    bob, _ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bob, data="left"))

    assert plugin.activity(bob).last_seen is None


# -- configuration -------------------------------------------------------------------------------


def test_an_inactivity_threshold_below_thirty_seconds_is_raised(console, caplog):
    """Under thirty seconds of quiet is an ordinary firefight, not an absence."""
    with caplog.at_level("WARNING"):
        plugin = _afk(console, inactivity_threshold=5)

    assert plugin.settings["inactivity_threshold"] == 30
    assert "inactivity_threshold" in caplog.text


def test_a_last_chance_outside_the_range_is_refused(console, caplog):
    with caplog.at_level("WARNING"):
        plugin = _afk(console, last_chance_delay=200)

    assert plugin.settings["last_chance_delay"] == 20
    assert "last_chance_delay" in caplog.text


def test_the_death_trigger_can_be_switched_off(console):
    plugin = _afk(console, consecutive_deaths=0)

    assert plugin.settings["consecutive_deaths"] == 0


@pytest.mark.asyncio
async def test_with_the_death_trigger_off_only_chat_checks_anybody(console):
    _afk(console, consecutive_deaths=0, inactivity_threshold=30)
    bob, ann, _sue = _players(console, "Bob", "Ann", "Sue")
    await _act(console, bob)
    console.clock.advance(60)

    for _ in range(5):
        await _die(console, bob, ann)
    assert _told(console, bob) == []

    await _say(console, ann, "afk")
    assert any("are you AFK" in text for text in _told(console, bob))


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_idle_player_is_asked_and_then_kicked(tmp_path):
    """Real Call of Duty log lines, a real bot, and the kick going out over rcon."""
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="afk")],
    )
    clock = FakeClock()
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=clock)
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = AfkPlugin(bot, {"settings": {"inactivity_threshold": 30, "last_chance_delay": 20}})
    bot.add_plugin(plugin, "afk")
    bot.start()
    admin.start()
    plugin.start()

    bob, ann, sue = "b" * 32, "a" * 32, "s" * 32
    await bot.replay([f"J;{bob};1;Bob", f"J;{ann};2;Ann", f"J;{sue};3;Sue"])
    await bot.bus.drain()
    clock.advance(60)

    rcon.commands.clear()
    await bot.replay([f"say;{ann};2;Ann;is Bob afk?"])
    await bot.bus.drain()
    assert any("are you AFK" in c for c in rcon.commands)

    clock.advance(21)
    plugin._check_deadlines()

    assert any("clientkick" in c or "kick" in c for c in rcon.commands)
    bot.storage.close()
