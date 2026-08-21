"""The `firstkill` plugin — first blood, first headshot, first team kill.

The classic's captured tests drive the plugin with a payload tuple, `(100, UT_MOD_DEAGLE, HL_HEAD)`,
and that tuple is the fault: the plugin read `event.data[2]` out of it, so the headshot half only
worked on the one parser whose kill payload had that shape, and the whole feature was gated on the
game's *name*. Here a headshot is whatever the engine actually reported, and the tests cover Urban
Terror's threaded hit location, Call of Duty's `MOD_HEAD_SHOT` and an engine that reports neither.

Also pinned: the three commands' own replies, which were written `'label: %s' % 'ON' if flag else
'OFF'` — so a switched-off plugin answered with the bare word "OFF".
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.cod.parser import KillData
from b3.plugins.admin import AdminPlugin
from b3.plugins.firstkill import FirstkillPlugin, is_headshot


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    plugin = FirstkillPlugin(console, {"settings": settings})
    plugin.start()
    return plugin


def _join(console, name, cid=None, bits=128, team="red"):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), group_bits=bits
    )
    client.team = team
    console.clients.add(client)
    console.register_client(name, client)
    return client


def _kill_data(hit_location="chest", mod="MOD_RIFLE"):  # noqa: ANN001, ANN202
    return KillData(weapon="ak47", damage=100, hit_location=hit_location, means_of_death=mod)


async def _kill(console, killer, victim, **kwargs):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(EventType.CLIENT_KILL, client=killer, target=victim, data=_kill_data(**kwargs))
    )


async def _team_kill(console, killer, victim, **kwargs):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(EventType.CLIENT_KILL_TEAM, client=killer, target=victim, data=_kill_data(**kwargs))
    )


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- first blood ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_kill_is_announced_once(console):
    _plugin(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")

    await _kill(console, mike, bill)
    await _kill(console, mike, bill)

    assert console.said_big == ["first kill: Mike killed Bill"]


@pytest.mark.asyncio
async def test_it_goes_out_centre_screen_where_the_engine_has_a_verb_for_it(console):
    """`say_big`, not a branch on the game's name — the classic had three."""
    _plugin(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")

    await _kill(console, mike, bill)

    assert console.said == []
    assert console.said_big != []


@pytest.mark.asyncio
async def test_first_blood_can_be_switched_off(console):
    _plugin(console, announce_first_kill=False)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")

    await _kill(console, mike, bill)

    assert console.said_big == []


@pytest.mark.asyncio
async def test_a_kill_with_nobody_to_credit_is_not_first_blood(console):
    plugin = _plugin(console)
    mike = _join(console, "Mike")

    await console.bus.publish(
        Event(EventType.CLIENT_KILL, client=mike, target=mike, data=_kill_data())
    )

    assert console.said_big == []
    assert plugin._kill_pending is True  # still owed


# -- headshots -----------------------------------------------------------------------------------


def test_a_headshot_is_read_from_what_the_engine_reported():
    """Three engines, three ways of saying it, and one that does not say it at all."""
    assert is_headshot(_kill_data(hit_location="head")) is True  # Urban Terror, Source
    assert is_headshot(_kill_data(hit_location="helmet")) is True  # Urban Terror with armour
    assert is_headshot(_kill_data(hit_location="", mod="MOD_HEAD_SHOT")) is True  # Call of Duty
    assert is_headshot(_kill_data(hit_location="chest")) is False
    assert is_headshot(_kill_data(hit_location="")) is False  # nothing stated, nothing claimed


def test_bleeding_out_and_grenades_are_not_headshots():
    """The classic's own exclusions, and they matter on Urban Terror.

    A hit to the head that leaves somebody bleeding, who then dies of it, arrives as a kill by
    `UT_MOD_BLED` with `head` still recorded against them.
    """
    assert is_headshot(_kill_data(hit_location="head", mod="UT_MOD_BLED")) is False
    assert is_headshot(_kill_data(hit_location="head", mod="UT_MOD_HEGRENADE")) is False


def test_a_payload_with_no_hit_location_at_all_is_not_a_headshot():
    """Frostbite, Homefront and Ravaged report a weapon and nothing else.

    The classic indexed `event.data[2]`, which on those engines is not a hit location and may not
    exist at all.
    """
    assert is_headshot(None) is False
    assert is_headshot(object()) is False


@pytest.mark.asyncio
async def test_a_first_kill_by_headshot_gets_its_own_line(console):
    _plugin(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")

    await _kill(console, mike, bill, hit_location="head")

    assert console.said_big == ["first kill, by headshot: Mike killed Bill"]


@pytest.mark.asyncio
async def test_with_headshots_off_it_is_just_a_first_kill(console):
    _plugin(console, announce_first_headshot=False)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")

    await _kill(console, mike, bill, hit_location="head")

    assert console.said_big == ["first kill: Mike killed Bill"]


@pytest.mark.asyncio
async def test_the_second_kill_being_a_headshot_is_not_announced(console):
    """Once per map means once: the classic counted the same way."""
    _plugin(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")

    await _kill(console, mike, bill)
    await _kill(console, mike, bill, hit_location="head")

    assert console.said_big == ["first kill: Mike killed Bill"]


# -- team kills ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_first_team_kill_is_announced_once(console):
    _plugin(console)
    mike, mark = _join(console, "Mike"), _join(console, "Mark")

    await _team_kill(console, mike, mark)
    await _team_kill(console, mike, mark)

    assert console.said_big == ["first team kill: Mike shot Mark"]


@pytest.mark.asyncio
async def test_first_blood_and_the_first_team_kill_are_counted_apart(console):
    _plugin(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")
    mark = _join(console, "Mark")

    await _kill(console, mike, bill)
    await _team_kill(console, mike, mark)

    assert console.said_big == [
        "first kill: Mike killed Bill",
        "first team kill: Mike shot Mark",
    ]


@pytest.mark.asyncio
async def test_team_kills_can_be_switched_off(console):
    _plugin(console, announce_first_teamkill=False)
    mike, mark = _join(console, "Mike"), _join(console, "Mark")

    await _team_kill(console, mike, mark)

    assert console.said_big == []


# -- resetting -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_map_owes_a_first_kill_again(console):
    _plugin(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")
    await _kill(console, mike, bill)

    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE, data="mp_carentan"))
    await _kill(console, mike, bill)

    assert console.said_big == ["first kill: Mike killed Bill"] * 2


@pytest.mark.asyncio
async def test_the_end_of_a_map_resets_it_too(console):
    """`GAME_MAP_CHANGE` is derived from a round start naming a different map, so a title that
    reports the end of a map and nothing else would never have reset at all."""
    _plugin(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")
    await _kill(console, mike, bill)

    await console.bus.publish(Event(EventType.GAME_EXIT, data=""))
    await _kill(console, mike, bill)

    assert len(console.said_big) == 2


@pytest.mark.asyncio
async def test_rounds_only_reset_it_when_asked(console):
    _plugin(console, reset_on="map")
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")
    await _kill(console, mike, bill)

    await console.bus.publish(Event(EventType.GAME_ROUND_END, data=""))
    await _kill(console, mike, bill)
    assert len(console.said_big) == 1

    _plugin(console, reset_on="round")
    console.said_big.clear()
    await console.bus.publish(Event(EventType.GAME_ROUND_END, data=""))
    await _kill(console, mike, bill)
    assert len(console.said_big) == 1


@pytest.mark.asyncio
async def test_never_means_never(console):
    _plugin(console, reset_on="never")
    mike, bill = _join(console, "Mike"), _join(console, "Bill", team="blue")
    await _kill(console, mike, bill)

    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE, data="mp_carentan"))
    await _kill(console, mike, bill)

    assert len(console.said_big) == 1


# -- the switches --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_switch_reports_its_own_state_with_a_label(console):
    """The classic's reply for a switched-off feature was the bare word "OFF"."""
    _plugin(console, announce_first_kill=False)
    admin = _join(console, "Admin")

    await _run(console, admin, "!firstkill")

    assert _told(console, admin) == ["first kill: off"]


@pytest.mark.asyncio
async def test_a_switch_can_be_turned_on_and_off(console):
    plugin = _plugin(console)
    admin = _join(console, "Admin")

    await _run(console, admin, "!firsttk off")
    assert plugin.settings["announce_first_teamkill"] is False
    await _run(console, admin, "!firsttk on")
    assert plugin.settings["announce_first_teamkill"] is True

    assert _told(console, admin) == ["first team kill: off", "first team kill: on"]


@pytest.mark.asyncio
async def test_junk_is_refused(console):
    plugin = _plugin(console)
    admin = _join(console, "Admin")

    await _run(console, admin, "!firsths f00")

    assert plugin.settings["announce_first_headshot"] is True
    assert _told(console, admin) == ["expecting on or off"]


@pytest.mark.asyncio
async def test_the_headshot_switch_exists_on_every_game(console):
    """The classic deleted it from the admin plugin's command table on any game but Urban Terror."""
    _plugin(console)

    assert console.command_registry.get("firsths") is not None


# -- through a real bot --------------------------------------------------------------------------


def _bot(tmp_path, game="cod4", **settings):  # noqa: ANN001, ANN202
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
        server=ServerConfig(game=game),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="firstkill")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = FirstkillPlugin(bot, {"settings": settings})
    bot.add_plugin(plugin, "firstkill")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()
    return bot, rcon, plugin


@pytest.mark.asyncio
async def test_a_real_call_of_duty_headshot_is_announced_as_one(tmp_path):
    """`MOD_HEAD_SHOT` on a real `K;` line — a title the classic refused to look at."""
    bot, rcon, _plugin = _bot(tmp_path)
    mike, bill = "m" * 32, "b" * 32
    await bot.replay([f"J;{mike};1;Mike", f"J;{bill};2;Bill"])
    await bot.bus.drain()
    rcon.commands.clear()

    await bot.replay([f"K;{bill};2;axis;Bill;{mike};1;allies;Mike;ak47;100;MOD_HEAD_SHOT;head"])
    await bot.bus.drain()

    assert any("by headshot" in c for c in rcon.commands), rcon.commands
    bot.storage.close()


@pytest.mark.asyncio
async def test_urban_terror_needs_the_hit_line_before_the_kill(tmp_path):
    """End to end on the engine the classic wrote this for, and the reason the parser changed.

    A UrT `Kill:` line names the weapon and not the part of the body. The hit location comes from the
    `Hit:` line before it, threaded through `Q3Parser._last_hit` — without that this announcement
    could not exist on Urban Terror at all, which is the one game the classic allowed it on.
    """
    bot, rcon, _plugin = _bot(tmp_path, game="iourt42")
    mike, bill = "m" * 32, "b" * 32
    await bot.replay(
        [
            f"ClientUserinfo: 1 \\name\\Mike\\cl_guid\\{mike}",
            f"ClientUserinfo: 2 \\name\\Bill\\cl_guid\\{bill}",
        ]
    )
    await bot.bus.drain()
    for cid, team in (("1", "red"), ("2", "blue")):
        client = bot.clients.get_by_cid(cid)
        assert client is not None
        client.team = team
    rcon.commands.clear()

    await bot.replay(
        [
            "Hit: 2 1 0 19: Mike hit Bill in the Head",
            "Kill: 1 2 19: Mike killed Bill by UT_MOD_M4",
        ]
    )
    await bot.bus.drain()

    assert any("by headshot" in c for c in rcon.commands), rcon.commands
    # Urban Terror's own verb, which the profile now declares rather than the plugin knowing it.
    assert any(c.startswith("bigtext") for c in rcon.commands), rcon.commands
    bot.storage.close()
