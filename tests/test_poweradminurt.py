"""`poweradminurt`, first slice — the commands that act on a player or a server setting.

The classic plugin is 3,846 lines across three per-version subclasses of itself, so it is ported in
parts; this covers `!paslap`, `!panuke`, `!pakill`, `!pamute`, `!paunmute`, `!pabigtext`, `!paset`,
`!paget` and `!pavote`.

Two of its faults are pinned here. Its three player commands had **three different immunity rules** —
`slap_safe_level` on `!paslap`, a strictly-higher-level comparison on `!pamute`, and *nothing at all*
on `!panuke`, so an admin who could not slap a fellow admin could nuke one. And a multi-slap was a raw
thread sleeping a second between twenty-five writes, which nothing could stop once it had started.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.poweradminurt import MAX_REPEATS, PoweradminurtPlugin


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    plugin = PoweradminurtPlugin(console, {"settings": settings})
    plugin.start()
    return plugin


def _join(console, name, bits=64, cid=None):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), group_bits=bits
    )
    console.clients.add(client)
    console.register_client(name, client)
    return client


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


def _verbs(console):  # noqa: ANN001, ANN202
    return [(name, who.name) for name, who, _values in console.verbs_applied]


# -- slap, nuke, kill ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_slap_reaches_the_player(console):
    _plugin(console)
    admin, _bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!paslap Bob")

    assert _verbs(console) == [("slap", "Bob")]
    assert _told(console, admin) == ["Bob has been slapped"]


@pytest.mark.asyncio
async def test_the_command_works_without_the_pa(console):
    """The classic's own promise: "you can safely use the command without the 'pa'"."""
    _plugin(console)
    admin, _bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!slap Bob")

    assert _verbs(console) == [("slap", "Bob")]


@pytest.mark.asyncio
async def test_a_nuke_and_a_kill_use_the_engines_own_verbs(console):
    _plugin(console)
    admin, _bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!panuke Bob")
    await _run(console, admin, "!pakill Bob")

    assert _verbs(console) == [("nuke", "Bob"), ("kill", "Bob")]


@pytest.mark.asyncio
async def test_a_multi_slap_is_spread_out_over_time(console):
    """A deadline per repeat, where the classic slept inside a raw thread."""
    plugin = _plugin(console)
    admin, _bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!paslap Bob 3")
    assert len(_verbs(console)) == 1  # the first one lands at once

    for _ in range(3):
        console.clock.advance(1)
        plugin._run_repeats()

    assert _verbs(console) == [("slap", "Bob")] * 3
    assert plugin.pending == []


@pytest.mark.asyncio
async def test_a_player_who_leaves_stops_being_slapped(console):
    """The classic's thread carried on writing to a slot that had been recycled."""
    plugin = _plugin(console)
    admin, bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)
    await _run(console, admin, "!paslap Bob 10")

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bob))
    console.clock.advance(5)
    plugin._run_repeats()

    assert len(_verbs(console)) == 1
    assert plugin.pending == []


@pytest.mark.asyncio
async def test_disabling_the_plugin_stops_the_slapping(console):
    plugin = _plugin(console)
    admin, _bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)
    await _run(console, admin, "!paslap Bob 10")

    plugin.disable()

    assert plugin.pending == []


@pytest.mark.asyncio
async def test_the_repeat_count_is_bounded(console):
    _plugin(console)
    admin, _bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, f"!paslap Bob {MAX_REPEATS + 1}")
    await _run(console, admin, "!paslap Bob 0")
    await _run(console, admin, "!paslap Bob three")

    assert console.verbs_applied == []
    assert _told(console, admin) == [f"that has to be a number from 1 to {MAX_REPEATS}"] * 3


# -- the immunity rule the classic had three of --------------------------------------------------


@pytest.mark.asyncio
async def test_you_cannot_slap_a_peer(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _join(console, "Other", bits=64)

    await _run(console, admin, "!paslap Other")

    assert console.verbs_applied == []
    assert _told(console, admin) == ["Other is not somebody you can do that to"]


@pytest.mark.asyncio
async def test_you_cannot_nuke_one_either(console):
    """The fault: `!panuke` had no rank check at all, so it was the way round `!paslap`'s."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _join(console, "Other", bits=64)

    await _run(console, admin, "!panuke Other")

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_nor_mute_one(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _join(console, "Other", bits=64)

    await _run(console, admin, "!pamute Other")

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_senior_admin_may_act_on_a_junior_one(console):
    _plugin(console)
    senior = _join(console, "Senior", bits=128)
    _join(console, "Junior", bits=16)

    await _run(console, senior, "!paslap Junior")

    assert _verbs(console) == [("slap", "Junior")]


@pytest.mark.asyncio
async def test_you_may_always_slap_yourself(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paslap Admin")

    assert _verbs(console) == [("slap", "Admin")]


@pytest.mark.asyncio
async def test_the_protection_can_be_switched_off(console):
    _plugin(console, protect_peers=False)
    admin = _join(console, "Admin", bits=64)
    _join(console, "Other", bits=64)

    await _run(console, admin, "!paslap Other")

    assert _verbs(console) == [("slap", "Other")]


# -- muting --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_mute_has_a_duration_and_an_end(console):
    """The classic sent `mute <cid>` with an empty duration, which *toggles* on these builds — so a
    mute could outlive the bot that set it with nothing recording that it was on."""
    _plugin(console, default_mute_minutes=5)
    admin, bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!pamute Bob")

    assert console.verbs_applied[-1] == ("mute", bob, {"seconds": "300"})
    assert _told(console, admin) == ["Bob is muted for 5 minutes"]

    console.clock.advance(301)
    console.lift_expired_mutes()
    assert console.verbs_applied[-1] == ("mute", bob, {"seconds": "0"})


@pytest.mark.asyncio
async def test_a_mute_takes_the_minutes_it_is_given(console):
    _plugin(console)
    admin, bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!pamute Bob 30")

    assert console.verbs_applied[-1] == ("mute", bob, {"seconds": "1800"})


@pytest.mark.asyncio
async def test_a_mute_can_be_lifted_by_hand(console):
    _plugin(console)
    admin, bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)
    await _run(console, admin, "!pamute Bob 30")

    await _run(console, admin, "!paunmute Bob")

    assert console.verbs_applied[-1] == ("mute", bob, {"seconds": "0"})
    assert console.muted_until(bob) == 0.0


@pytest.mark.asyncio
async def test_a_shorter_mute_does_not_cut_a_longer_one_short(console):
    """Which is the whole reason muting lives in the runtime: `censor` mutes too, on its own ladder."""
    _plugin(console)
    admin, bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!pamute Bob 30")
    console.verbs_applied.clear()
    await _run(console, admin, "!pamute Bob 1")

    assert console.verbs_applied == []  # nothing re-sent, the longer deadline stands
    assert console.muted_until(bob) == pytest.approx(console.clock.now() + 1800)


# -- an engine without the verbs -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_a_game_with_no_such_verbs_the_commands_say_so(console):
    """Registered, and honest about it — rather than the plugin refusing to load by game name."""
    console.player_verbs = set()
    _plugin(console)
    admin, _bob = _join(console, "Admin", bits=64), _join(console, "Bob", bits=0)

    await _run(console, admin, "!paslap Bob")
    await _run(console, admin, "!pamute Bob")

    assert console.verbs_applied == []
    assert _told(console, admin) == [
        "this game has no slap command",
        "this game has no mute command",
    ]


# -- server settings -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bigtext_goes_out_in_the_biggest_text_the_game_has(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pabigtext round starts in 10")

    assert console.said_big == ["round starts in 10"]
    assert console.said == []


@pytest.mark.asyncio
async def test_a_cvar_can_be_read_and_written(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_gravity"] = "800"

    await _run(console, admin, "!paget g_gravity")
    await _run(console, admin, "!paset g_gravity 400")
    await _run(console, admin, "!paget g_gravity")

    assert _told(console, admin) == [
        "g_gravity is 800",
        "g_gravity is now 400",
        "g_gravity is 400",
    ]


@pytest.mark.asyncio
async def test_a_cvar_the_server_does_not_have(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paget g_nonsense")

    assert _told(console, admin) == ["g_nonsense is not set"]


@pytest.mark.asyncio
async def test_a_value_with_spaces_survives(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paset sv_hostname my great server")

    assert console.cvars["sv_hostname"] == "my great server"


# -- voting --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voting_off_and_on_again_restores_what_was_there(console):
    """The classic remembered the value in an attribute set at *bot start*, so `!pavote on` after a
    restart re-enabled voting with whatever the plugin's default happened to be."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_allowvote"] = "60"

    await _run(console, admin, "!pavote off")
    assert console.cvars["g_allowvote"] == "0"

    await _run(console, admin, "!pavote on")
    assert console.cvars["g_allowvote"] == "60"


@pytest.mark.asyncio
async def test_voting_on_with_nothing_remembered_uses_the_engines_default(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pavote on")

    assert console.cvars["g_allowvote"] == "536870908"


@pytest.mark.asyncio
async def test_switching_it_off_twice_does_not_forget_the_original(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_allowvote"] = "60"

    await _run(console, admin, "!pavote off")
    await _run(console, admin, "!pavote off")
    await _run(console, admin, "!pavote on")

    assert console.cvars["g_allowvote"] == "60"


@pytest.mark.asyncio
async def test_junk_is_refused(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pavote sideways")

    assert _told(console, admin) == ["!pavote on|off"]


# -- levels --------------------------------------------------------------------------------------


def test_the_two_command_levels_are_configurable(console):
    _plugin(console, player_command_level=80, server_command_level=100)

    slap = console.command_registry.get("paslap")
    paset = console.command_registry.get("paset")
    assert slap is not None and slap.min_level == 80
    assert paset is not None and paset.min_level == 100
