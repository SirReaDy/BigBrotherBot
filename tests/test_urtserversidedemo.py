"""The `urtserversidedemo` plugin — Urban Terror's server-side demo recording.

The classic has captured tests, and what they capture is the thing that matters: the server's four
replies, verbatim. `startserverdemo: recording laCourge to serverdemos/2012_04_22_20-16-38_laCourge_642817.dm_68`
is where the filename pattern comes from, and reading that filename is the whole point of the plugin —
without it nothing can say a demo started, and `jumper` cannot find the file again to delete it.

What the captured tests could not show is that **nothing on shutdown ever ran**: `onExit` and `onStop`
were defined and never registered, and the method they would have called iterated a dict as if it held
pairs. So a bot that stopped left the server recording, and `!startserverdemo all` outlived the bot
that asked for it.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.q3.profiles import IOURT41, IOURT42
from b3.plugins.admin import AdminPlugin
from b3.plugins.urtserversidedemo import (
    RETRY_SECONDS,
    STARTED_RE,
    WAIT_SECONDS,
    UrtserversidedemoPlugin,
)

#: Captured from the classic's own tests, both filename shapes.
RECORDING = (
    "startserverdemo: recording {name} to serverdemos/2012_04_22_20-16-38_{name}_642817.dm_68"
)
RECORDING_URTDEMO = (
    "startserverdemo: recording {name} to serverdemos/2016_01_01_10-00-00_{name}.urtdemo"
)
ALREADY = "startserverdemo: {name} is already being recorded"
NOT_ACTIVE = "startserverdemo: {name} is not active"
NO_PLAYER = "No player has that name or slot number"
STOPPED = "stopserverdemo: stopped recording {name}"

DEMO_PLAYER_VERBS = {"record_demo", "record_stop"}
DEMO_SERVER_VERBS = {"record_all", "record_stop_all"}


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.player_verbs = set(DEMO_PLAYER_VERBS)
    console.server_verbs = set(DEMO_SERVER_VERBS)
    plugin = UrtserversidedemoPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


def _client(console, name, bits=0, cid=None, id_=None):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{name}".ljust(32, "0"),
        name=name,
        cid=slot,
        id=id_ if id_ is not None else abs(hash(name)) % 10000,
        group_bits=bits,
        team="red",
    )
    console.clients.add(client)
    console.register_client(name, client)
    console.register_client(name.lower(), client)
    return client


def _boss(console):  # noqa: ANN001, ANN202
    return _client(console, "Boss", bits=128, cid="0", id_=1)


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    told = [text for who, text in console.told if who is client]
    return told[-1] if told else ""


def _asked(console, verb):  # noqa: ANN001, ANN202
    return [c.name for name, c, _ in console.verbs_applied if name == verb]


# -- the reply, which is the point -----------------------------------------------------------------


def test_the_filename_is_read_out_of_the_servers_own_answer():
    """Both extensions: `dm_68` on 4.2 and `urtdemo` on the later builds. The classic's own pattern,
    and the reason it exists — a demo nothing can name is a demo nothing can delete."""
    match = STARTED_RE.match(RECORDING.format(name="laCourge"))
    assert match is not None
    assert match["file"] == "serverdemos/2012_04_22_20-16-38_laCourge_642817.dm_68"
    assert match["name"] == "laCourge"

    later = STARTED_RE.match(RECORDING_URTDEMO.format(name="Joe"))
    assert later is not None
    assert later["file"].endswith(".urtdemo")

    # A name with spaces in it, which is the case a greedier pattern loses.
    spaced = STARTED_RE.match(RECORDING.format(name="la Courge"))
    assert spaced is not None and spaced["name"] == "la Courge"


@pytest.mark.asyncio
async def test_a_demo_reports_the_file_it_is_writing(console):
    plugin = _plugin(console)
    boss = _boss(console)
    joe = _client(console, "Joe")
    console.verb_replies["record_demo"] = RECORDING.format(name="Joe")

    await _run(console, boss, "!startserverdemo joe")

    assert _asked(console, "record_demo") == ["Joe"]
    assert "2012_04_22_20-16-38_Joe_642817.dm_68" in _last(console, boss)
    # And the plugin knows the filename afterwards, which is what `jumper` asks it for.
    assert plugin.filename_for(joe).endswith(".dm_68")


@pytest.mark.asyncio
async def test_a_player_already_being_recorded_is_said_plainly(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")
    console.verb_replies["record_demo"] = ALREADY.format(name="Joe")

    await _run(console, boss, "!startserverdemo joe")

    assert "already being recorded" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_reply_nobody_has_seen_before_reaches_the_admin(console):
    """`raise AssertionError("unexpected response: %r")` in the classic, inside a thread — so the
    attempt vanished, the player stayed in the waiting table for good, and nobody was told."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")
    console.verb_replies["record_demo"] = "startserverdemo: demo recording is disabled"

    await _run(console, boss, "!startserverdemo joe")

    assert "demo recording is disabled" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_slot_with_nobody_in_it_is_not_waited_for(console):
    plugin = _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")
    console.verb_replies["record_demo"] = NO_PLAYER

    await _run(console, boss, "!startserverdemo joe")

    assert plugin.demos.waiting == []
    assert "No player" in _last(console, boss)


# -- a player who has not joined a team yet --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_player_not_on_a_team_is_recorded_when_they_join(console):
    """The classic started a thread per request whose entire job was to wait on this."""
    plugin = _plugin(console)
    boss = _boss(console)
    joe = _client(console, "Joe")
    console.verb_replies["record_demo"] = NOT_ACTIVE.format(name="Joe")

    await _run(console, boss, "!startserverdemo joe")
    assert "has not joined a team yet" in _last(console, boss)
    assert len(plugin.demos.waiting) == 1

    console.verb_replies["record_demo"] = RECORDING.format(name="Joe")
    await console.bus.publish(Event(EventType.CLIENT_JOIN, client=joe))

    assert plugin.demos.waiting == []
    assert plugin.filename_for(joe) != ""
    assert "recording Joe" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_waiting_attempt_is_retried_by_the_scheduled_pass(console):
    plugin = _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")
    console.verb_replies["record_demo"] = NOT_ACTIVE.format(name="Joe")

    await _run(console, boss, "!startserverdemo joe")
    console.verb_replies["record_demo"] = RECORDING.format(name="Joe")

    plugin._run_pending()
    assert len(plugin.demos.waiting) == 1  # not due yet

    console.clock.advance(RETRY_SECONDS)
    plugin._run_pending()

    assert plugin.demos.waiting == []
    assert len(plugin.demos.recording) == 1


@pytest.mark.asyncio
async def test_a_player_who_never_joins_is_given_up_on(console):
    """The classic's thread waited for ever; a table that is never cleaned up is a leak."""
    plugin = _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")
    console.verb_replies["record_demo"] = NOT_ACTIVE.format(name="Joe")

    await _run(console, boss, "!startserverdemo joe")
    console.clock.advance(WAIT_SECONDS + 1)
    plugin._run_pending()

    assert plugin.demos.waiting == []
    assert plugin.demos.recording == []


@pytest.mark.asyncio
async def test_a_player_who_leaves_is_forgotten_from_both_tables(console):
    plugin = _plugin(console)
    boss = _boss(console)
    joe = _client(console, "Joe")
    console.verb_replies["record_demo"] = RECORDING.format(name="Joe")
    await _run(console, boss, "!startserverdemo joe")
    bill = _client(console, "Bill")
    console.verb_replies["record_demo"] = NOT_ACTIVE.format(name="Bill")
    await _run(console, boss, "!startserverdemo bill")

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=joe))
    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bill))

    assert plugin.demos.recording == []
    assert plugin.demos.waiting == []


# -- a demo with a length --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_demo_can_be_asked_for_a_number_of_minutes(console):
    """The classic's `demo_duration` was reachable only from two third-party plugins, so for everybody
    else the machinery existed and nothing could ask for it."""
    plugin = _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")
    console.verb_replies["record_demo"] = RECORDING.format(name="Joe")
    console.verb_replies["record_stop"] = STOPPED.format(name="Joe")

    await _run(console, boss, "!startserverdemo joe 2")
    assert "stopping in 2 minute(s)" in _last(console, boss)

    console.clock.advance(60)
    plugin._run_pending()
    assert _asked(console, "record_stop") == []

    console.clock.advance(61)
    plugin._run_pending()

    assert _asked(console, "record_stop") == ["Joe"]
    assert plugin.demos.recording == []


@pytest.mark.asyncio
async def test_a_timed_demo_of_a_player_who_left_does_not_raise(console):
    """`del self._auto_stop_timers[guid]` in the classic's timer callback, on an entry the disconnect
    handler had already deleted — `KeyError`, in a thread, and the demo never stopped."""
    plugin = _plugin(console)
    boss = _boss(console)
    joe = _client(console, "Joe")
    console.verb_replies["record_demo"] = RECORDING.format(name="Joe")

    await _run(console, boss, "!startserverdemo joe 1")
    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=joe))
    console.clock.advance(61)
    plugin._run_pending()

    assert plugin.demos.recording == []


@pytest.mark.asyncio
async def test_a_length_that_is_not_a_number_is_refused(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")

    await _run(console, boss, "!startserverdemo joe soon")

    assert _asked(console, "record_demo") == []
    assert "<minutes>" in _last(console, boss)


# -- all of them -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recording_everybody_keeps_recording_whoever_arrives(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!startserverdemo all")
    assert plugin.recording_all is True
    assert console.server_verbs_applied == [("record_all", {})]

    joe = _client(console, "Joe")
    console.verb_replies["record_demo"] = RECORDING.format(name="Joe")
    await console.bus.publish(Event(EventType.CLIENT_JOIN, client=joe))

    assert _asked(console, "record_demo") == ["Joe"]


@pytest.mark.asyncio
async def test_all_means_the_same_thing_to_both_commands(console):
    """`data == 'all'` in one and `data.split(' ')[0] == 'all'` in the other, so `!startserverdemo ALL`
    looked for a player called "ALL" while `!stopserverdemo all please` stopped everything."""
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!startserverdemo ALL")
    assert plugin.recording_all is True

    await _run(console, boss, "!stopserverdemo All")
    assert plugin.recording_all is False
    assert ("record_stop_all", {}) in console.server_verbs_applied


@pytest.mark.asyncio
async def test_switching_the_plugin_off_stops_the_recording_it_asked_for(console):
    """The classic's `onExit`/`onStop` were never registered, and the method they called iterated a
    dict as if it held pairs — so a bot that stopped left the server recording."""
    plugin = _plugin(console)
    boss = _boss(console)
    await _run(console, boss, "!startserverdemo all")
    console.server_verbs_applied.clear()

    plugin.disable()

    assert console.server_verbs_applied == [("record_stop_all", {})]
    # ...and it starts again when the plugin comes back, which is the classic's own intent.
    console.server_verbs_applied.clear()
    plugin.enable()
    assert console.server_verbs_applied == [("record_all", {})]


@pytest.mark.asyncio
async def test_stopping_one_player_says_so(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Joe")
    console.verb_replies["record_stop"] = STOPPED.format(name="Joe")

    await _run(console, boss, "!stopserverdemo joe")

    assert _asked(console, "record_stop") == ["Joe"]
    assert "stopped recording Joe" in _last(console, boss)


@pytest.mark.asyncio
async def test_either_command_with_no_argument_says_how_to_use_it(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!startserverdemo")
    assert "<player|all>" in _last(console, boss)

    await _run(console, boss, "!stopserverdemo")
    assert "<player|all>" in _last(console, boss)


# -- it is a 4.2 feature ---------------------------------------------------------------------------


def test_the_verbs_belong_to_the_title_and_not_to_the_plugin():
    """4.1 predates server-side demos, so it declares neither — which is how the plugin knows without
    asking the server, and how an operator on the older build is told by the bot."""
    assert DEMO_PLAYER_VERBS <= set(IOURT42.player_verbs)
    assert DEMO_SERVER_VERBS <= set(IOURT42.server_verbs)
    assert not DEMO_PLAYER_VERBS & set(IOURT41.player_verbs)
    assert not DEMO_SERVER_VERBS & set(IOURT41.server_verbs)
    assert IOURT42.player_verbs["record_demo"] == "startserverdemo %(cid)s"
    assert IOURT42.server_verbs["record_all"] == "startserverdemo all"


def test_the_plugin_switches_itself_off_on_a_title_without_the_feature(console):
    """Rather than offering two commands that could only ever be answered "unknown command"."""
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.player_verbs = set()
    console.server_verbs = set()

    plugin = UrtserversidedemoPlugin(console, None)
    plugin.start()

    assert plugin.is_enabled() is False
    assert "no server-side demo recording" in plugin.disabled_reason


# -- through a real bot ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_demo_is_started_on_a_real_urban_terror_server(tmp_path):
    """The whole way through: a real `Bot` on the iourt42 profile, a chat line from the log, the verb
    that goes out and the reply that comes back."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            if cmd.startswith("startserverdemo"):
                return RECORDING.format(name="Joe")
            return ""

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="iourt42"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="urtserversidedemo")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = UrtserversidedemoPlugin(bot, None)
    bot.add_plugin(plugin, "urtserversidedemo")
    bot.start()
    admin.start()
    plugin.start()

    await bot.replay(
        [
            r"ClientUserinfo: 0 \name\Boss\cl_guid\BOSSGUID000000000000000000000000",
            r"ClientUserinfo: 1 \name\Joe\cl_guid\JOEGUID00000000000000000000000000",
            "ClientBegin: 0",
            "ClientBegin: 1",
        ]
    )
    await bot.bus.drain()
    bot.clients.get_by_cid("0").group_bits = 128
    rcon.commands.clear()

    await bot.replay(["say: 0 Boss: !startserverdemo joe"])
    await bot.bus.drain()

    assert "startserverdemo 1" in rcon.commands
    assert plugin.filename_for(bot.clients.get_by_cid("1")).endswith(".dm_68")
    bot.storage.close()
