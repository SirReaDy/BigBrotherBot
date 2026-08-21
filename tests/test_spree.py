"""The `spree` plugin — the server saying a player's name.

The classic's three test files pin the wording and, more usefully, the *order* two announcements come
out in when one kill both ends a losing streak and ends somebody else's spree. Those are kept.

What they could not show is the fault: they drove the plugin with `EVT_CLIENT_KILL` only, which is the
one event it listened for. A gib, a suicide and a team kill were all invisible to it — so a spree
survived walking off a ledge, and on a title that reports kills as gibs no spree ever started.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.spree import SpreePlugin, parse_spree_messages

#: The classic's own thresholds and wording, translated to `{}` placeholders. The 12-death losing
#: spree is the number its captured tests used.
KILLING = {
    5: "{player} is on a killing spree (5 kills in a row) # {player} stopped the spree of {victim}",
    10: "{player} is on fire! (10 kills in a row) # {player} iced {victim}",
}
LOSING = {12: "Keep it up {player}, it will come eventually # {player} is back in business"}


def _plugin(console, *, killing=None, losing=None, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    config: dict[str, object] = {"settings": settings}
    if killing is not None:
        config["killing_sprees"] = killing
    if losing is not None:
        config["losing_sprees"] = losing
    plugin = SpreePlugin(console, config)
    plugin.start()
    return plugin


def _join(console, name, cid=None, bits=1):  # noqa: ANN001, ANN202
    """A registered player — level 1, as the classic's own fixtures were, since `!spree` needs it."""
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), group_bits=bits
    )
    client.team = "red"
    console.clients.add(client)
    console.register_client(name, client)
    return client


async def _kill(console, killer, victim, times=1):  # noqa: ANN001, ANN202
    for _ in range(times):
        await console.bus.publish(
            Event(EventType.CLIENT_KILL, client=killer, target=victim, data=None)
        )


async def _publish(console, event_type, **fields):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(event_type, **fields))


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- the announcements ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_five_kills_in_a_row_is_announced(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")

    await _kill(console, bill, mike, times=5)

    assert console.said == ["Bill is on a killing spree (5 kills in a row)"]


@pytest.mark.asyncio
async def test_the_player_who_ends_a_spree_is_named(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")

    await _kill(console, bill, mike, times=5)
    await _kill(console, mike, bill)

    assert console.said == [
        "Bill is on a killing spree (5 kills in a row)",
        "Mike stopped the spree of Bill",
    ]


@pytest.mark.asyncio
async def test_the_next_threshold_replaces_the_end_message(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")

    await _kill(console, bill, mike, times=10)
    await _kill(console, mike, bill)

    assert console.said == [
        "Bill is on a killing spree (5 kills in a row)",
        "Bill is on fire! (10 kills in a row)",
        "Mike iced Bill",
    ]


@pytest.mark.asyncio
async def test_a_losing_streak_is_announced_and_then_ended(console):
    """The order is the classic's, and it is the readable one: the two lines are about two people."""
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")

    await _kill(console, bill, mike, times=12)
    await _kill(console, mike, bill)

    assert console.said == [
        "Bill is on a killing spree (5 kills in a row)",
        "Bill is on fire! (10 kills in a row)",
        "Keep it up Mike, it will come eventually",
        "Mike is back in business",
        "Mike iced Bill",
    ]


@pytest.mark.asyncio
async def test_a_spree_needs_the_kills_to_be_consecutive(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")

    await _kill(console, bill, mike, times=4)
    await _kill(console, mike, bill)  # Bill dies, and starts again from nothing
    await _kill(console, bill, mike, times=4)

    assert console.said == []


# -- the deaths the classic could not see --------------------------------------------------------


@pytest.mark.asyncio
async def test_walking_off_a_ledge_ends_a_spree(console):
    """The classic listened for kills only, so a suicide kept a twenty-kill spree alive.

    Nothing is announced — the end message names whoever did it, and nobody did.
    """
    plugin = _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    await _kill(console, bill, mike, times=5)
    console.said.clear()

    await _publish(console, EventType.CLIENT_SUICIDE, client=bill)

    assert console.said == []
    assert plugin.spree(bill).kills == 0
    assert plugin.spree(bill).deaths == 1


@pytest.mark.asyncio
async def test_being_team_killed_ends_a_spree_and_names_who_did_it(console):
    plugin = _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    sam = _join(console, "Sam")
    await _kill(console, bill, mike, times=5)
    console.said.clear()

    await _publish(console, EventType.CLIENT_KILL_TEAM, client=sam, target=bill)

    assert console.said == ["Sam stopped the spree of Bill"]
    assert plugin.spree(bill).kills == 0


@pytest.mark.asyncio
async def test_team_killing_does_not_build_a_spree(console):
    """A spree is something to be proud of, and shooting your own side is not."""
    plugin = _plugin(console, killing=KILLING, losing=LOSING)
    sam, bill = _join(console, "Sam"), _join(console, "Bill")

    for _ in range(5):
        await _publish(console, EventType.CLIENT_KILL_TEAM, client=sam, target=bill)

    assert console.said == []
    assert plugin.spree(sam).kills == 0


@pytest.mark.asyncio
async def test_a_gib_is_a_kill(console):
    """Enemy Territory reports most kills as gibs. The classic announced no sprees at all there."""
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")

    for _ in range(5):
        await _publish(console, EventType.CLIENT_GIB, client=bill, target=mike)

    assert console.said == ["Bill is on a killing spree (5 kills in a row)"]


@pytest.mark.asyncio
async def test_a_kill_reported_against_oneself_is_a_death(console):
    plugin = _plugin(console, killing=KILLING, losing=LOSING)
    bill = _join(console, "Bill")

    await _kill(console, bill, bill)

    assert plugin.spree(bill).kills == 0
    assert plugin.spree(bill).deaths == 1


# -- resetting -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_map_change_clears_everybody(console):
    """`EVT_GAME_EXIT` is emitted by three of the eleven parsers, so the classic's reset was inert
    on Frostbite, Homefront, Altitude, Ravaged and Frontline."""
    plugin = _plugin(console, killing=KILLING, losing=LOSING, reset_on="map")
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    await _kill(console, bill, mike, times=3)

    await _publish(console, EventType.GAME_MAP_CHANGE, data="mp_carentan")

    assert plugin.spree(bill).kills == 0
    assert plugin.spree(mike).deaths == 0


@pytest.mark.asyncio
async def test_the_end_of_a_map_clears_everybody(console):
    plugin = _plugin(console, killing=KILLING, losing=LOSING, reset_on="map")
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    await _kill(console, bill, mike, times=3)

    await _publish(console, EventType.GAME_EXIT, data="")

    assert plugin.spree(bill).kills == 0


@pytest.mark.asyncio
async def test_a_round_ending_only_clears_when_asked_to(console):
    plugin = _plugin(console, killing=KILLING, losing=LOSING, reset_on="map")
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    await _kill(console, bill, mike, times=3)

    await _publish(console, EventType.GAME_ROUND_END, data="")
    assert plugin.spree(bill).kills == 3

    plugin.settings["reset_on"] = "round"
    await _publish(console, EventType.GAME_ROUND_END, data="")
    assert plugin.spree(bill).kills == 0


@pytest.mark.asyncio
async def test_never_means_never(console):
    plugin = _plugin(console, killing=KILLING, losing=LOSING, reset_on="never")
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    await _kill(console, bill, mike, times=3)

    await _publish(console, EventType.GAME_MAP_CHANGE, data="mp_carentan")
    await _publish(console, EventType.GAME_EXIT, data="")

    assert plugin.spree(bill).kills == 3


def test_a_junk_reset_setting_falls_back_to_the_classic_behaviour(console):
    assert _plugin(console, reset_on="tuesday").settings["reset_on"] == "map"


# -- the message tables --------------------------------------------------------------------------


def test_the_compact_form_the_classic_shipped_is_understood():
    table = parse_spree_messages({5: "on a spree, {player} # {player} stopped {victim}"}, "killing")

    assert table == {5: ("on a spree, {player}", "{player} stopped {victim}")}


def test_the_explicit_form_is_understood():
    table = parse_spree_messages(
        {5: {"start": "{player} is going", "end": "{player} got {victim}"}}, "killing"
    )

    assert table == {5: ("{player} is going", "{player} got {victim}")}


def test_an_entry_with_no_end_message_keeps_its_start_message():
    """The classic dropped the whole entry when the `#` was missing, start message included."""
    table = parse_spree_messages({5: "{player} is going"}, "killing")

    assert table == {5: ("{player} is going", "")}


@pytest.mark.asyncio
async def test_a_spree_with_no_end_message_says_nothing_when_it_ends(console):
    _plugin(console, killing={5: "{player} is going"}, losing={})
    bill, mike = _join(console, "Bill"), _join(console, "Mike")

    await _kill(console, bill, mike, times=5)
    console.said.clear()
    await _kill(console, mike, bill)

    assert console.said == []


def test_one_bad_entry_does_not_take_the_others_with_it():
    """A non-numeric key raised `ValueError` at load in the classic, before any entry was read."""
    table = parse_spree_messages(
        {"five": "{player} is going", 10: "{player} is going # done", 0: "nope"}, "killing"
    )

    assert table == {10: ("{player} is going", "done")}


def test_a_placeholder_this_plugin_cannot_fill_is_refused():
    """The classic sent it to the server as literal text."""
    table = parse_spree_messages(
        {5: "{killer} is going # {player} stopped {victim}", 10: "{player} is going"}, "killing"
    )

    assert 5 not in table
    assert 10 in table


def test_an_unclosed_placeholder_is_refused():
    assert parse_spree_messages({5: "{player is going"}, "killing") == {}


def test_an_empty_section_means_no_announcements(console):
    plugin = _plugin(console, killing={}, losing={})

    assert plugin.killing_sprees == {}
    assert plugin.losing_sprees == {}


def test_no_section_at_all_means_the_defaults(console):
    """A plugin loaded with no config of its own should still do something."""
    plugin = _plugin(console)

    assert 5 in plugin.killing_sprees
    assert 7 in plugin.losing_sprees


def test_the_tables_are_rebuilt_on_a_reload(console):
    """The classic held them on the class and only ever inserted, so a removed threshold stayed."""
    plugin = _plugin(console, killing=KILLING, losing=LOSING)
    assert 10 in plugin.killing_sprees

    plugin.config = {"settings": {}, "killing_sprees": {5: KILLING[5]}, "losing_sprees": {}}
    plugin.on_load_config()

    assert 10 not in plugin.killing_sprees


# -- the command ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spree_reports_your_own_streak(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    await _kill(console, bill, mike, times=5)

    await _run(console, bill, "!spree")
    await _run(console, mike, "!spree")

    assert _told(console, bill) == ["you have 5 kills in a row"]
    assert _told(console, mike) == ["you have 5 deaths in a row"]


@pytest.mark.asyncio
async def test_spree_reports_somebody_elses(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill, mike = _join(console, "Bill"), _join(console, "Mike")
    await _kill(console, mike, bill, times=5)

    await _run(console, bill, "!spree Mike")
    await _run(console, mike, "!spree Bill")

    assert _told(console, bill) == ["Mike has 5 kills in a row"]
    assert _told(console, mike) == ["Bill has 5 deaths in a row"]


@pytest.mark.asyncio
async def test_spree_on_nobody_in_particular(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill = _join(console, "Bill")
    _join(console, "Mike")

    await _run(console, bill, "!spree")
    await _run(console, bill, "!spree Mike")

    assert _told(console, bill) == [
        "you are not having a spree right now",
        "Mike is not having a spree right now",
    ]


@pytest.mark.asyncio
async def test_spree_on_a_name_nobody_has(console):
    _plugin(console, killing=KILLING, losing=LOSING)
    bill = _join(console, "Bill")

    await _run(console, bill, "!spree bob")

    assert _told(console, bill) != []
    assert "bob" in _told(console, bill)[0]


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_call_of_duty_kills_build_a_real_spree(tmp_path):
    """Log lines from a real server, through a real bot, out to the server as chat.

    The spree is built from `K;` lines and ended by a `D;`-shaped death — a suicide, which is one of
    the three kinds of death the classic never saw.
    """
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="spree")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = SpreePlugin(bot, {"settings": {}, "killing_sprees": {3: KILLING[5]}})
    bot.add_plugin(plugin, "spree")
    bot.start()
    admin.start()
    plugin.start()

    bill, mike = "b" * 32, "m" * 32
    await bot.replay([f"J;{bill};1;Bill", f"J;{mike};2;Mike"])
    await bot.bus.drain()
    rcon.commands.clear()

    for _ in range(3):
        await bot.replay([f"K;{mike};2;axis;Mike;{bill};1;allies;Bill;ak47;100;MOD_RIFLE;chest"])
    await bot.bus.drain()

    assert any("killing spree" in c for c in rcon.commands), rcon.commands
    killer = bot.clients.get_by_cid("1")
    assert killer is not None
    assert plugin.spree(killer).kills == 3

    rcon.commands.clear()
    await bot.replay([f"K;{bill};1;allies;Bill;{bill};1;allies;Bill;world;100;MOD_FALLING;none"])
    await bot.bus.drain()

    assert plugin.spree(killer).kills == 0
    assert not any("spree" in c for c in rcon.commands)  # nobody to credit
    bot.storage.close()
