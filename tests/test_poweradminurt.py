"""`poweradminurt` — Urban Terror's admin commands, and the team balancer behind them.

Several of the classic's faults are pinned here. Its three player commands had **three different
immunity rules** — `slap_safe_level` on `!paslap`, a strictly-higher-level comparison on `!pamute`, and
*nothing at all* on `!panuke`, so an admin who could not slap a fellow admin could nuke one. A
multi-slap was a raw thread sleeping a second between twenty-five writes, which nothing could stop once
it had started. And its team balancer moved one player, asked the server to count the teams again, and
believed the answer — which could not yet include the move it had just made.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.cod.parser import KillData
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


# -- the match settings -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gametype_switch_writes_the_engines_own_number(console):
    """The numbers are Urban Terror's and are not guessable — 7 is capture the flag."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pactf")

    assert console.cvars["g_gametype"] == "7"
    assert _told(console, admin) == ["the game is now capture the flag"]


@pytest.mark.asyncio
async def test_every_gametype_is_registered_and_distinct(console):
    from b3.plugins.poweradminurt import GAMETYPES

    _plugin(console)

    for name in GAMETYPES:
        assert console.command_registry.get(name) is not None
    numbers = [number for number, _what in GAMETYPES.values()]
    assert len(numbers) == len(set(numbers))


@pytest.mark.asyncio
async def test_a_toggle_takes_on_and_off(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!painstagib on")
    assert console.cvars["g_instagib"] == "1"

    await _run(console, admin, "!painstagib off")
    assert console.cvars["g_instagib"] == "0"


@pytest.mark.asyncio
async def test_a_toggle_refuses_anything_else(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pahardcore sideways")

    assert "g_hardcore" not in console.cvars
    assert _told(console, admin) == ["!pahardcore on|off"]


@pytest.mark.asyncio
async def test_a_number_command_checks_its_number(console):
    """The classic passed the text straight to `setCvar`, so `!pasetgravity banana` set the gravity
    to `banana` — which the server ignored while the admin was told it had worked."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pasetgravity banana")
    assert "g_gravity" not in console.cvars

    await _run(console, admin, "!pasetgravity 400")
    assert console.cvars["g_gravity"] == "400"


@pytest.mark.asyncio
async def test_a_number_out_of_range_is_refused(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!patimelimit -5")
    await _run(console, admin, "!patimelimit 99999")

    assert "timelimit" not in console.cvars
    assert len(_told(console, admin)) == 2


@pytest.mark.asyncio
async def test_stamina_is_three_named_values_not_a_toggle(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pastamina infinite")
    assert console.cvars["g_stamina"] == "2"

    await _run(console, admin, "!pastamina default")
    assert console.cvars["g_stamina"] == "0"

    await _run(console, admin, "!pastamina on")
    assert _told(console, admin)[-1] == "!pastamina default|regain|infinite"


@pytest.mark.asyncio
async def test_moon_mode_puts_back_the_gravity_the_server_had(console):
    """The classic restored a number out of its own config, not the server's."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_gravity"] = "600"

    await _run(console, admin, "!pamoon on")
    assert console.cvars["g_gravity"] == "100"

    await _run(console, admin, "!pamoon off")
    assert console.cvars["g_gravity"] == "600"


@pytest.mark.asyncio
async def test_moon_mode_off_with_nothing_recorded_uses_a_normal_gravity(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamoon off")

    assert console.cvars["g_gravity"] == "800"


# -- !pagear ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagear_with_no_argument_answers_the_admin(console):
    """The classic answered with `console.write(...)` — an **rcon command**. So the admin was told
    nothing and the server was sent a line of colour-coded chat as a command."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_gear"] = "0"

    await _run(console, admin, "!pagear")

    assert _told(console, admin) == ["allowed: nade, snipe, spas, pistol, auto, negev"]
    assert console.rcon_sent == []


@pytest.mark.asyncio
async def test_pagear_all_and_none_are_the_engines_way_round(console):
    """A set bit *forbids* a weapon, so `all` is 0 and `none` is 63."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pagear none")
    assert console.cvars["g_gear"] == "63"
    assert _told(console, admin)[-1] == "no weapons are allowed"

    await _run(console, admin, "!pagear all")
    assert console.cvars["g_gear"] == "0"


@pytest.mark.asyncio
async def test_a_single_weapon_can_be_allowed_or_forbidden(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_gear"] = "0"

    await _run(console, admin, "!pagear -negev")
    assert console.cvars["g_gear"] == "32"

    await _run(console, admin, "!pagear +negev")
    assert console.cvars["g_gear"] == "0"


@pytest.mark.asyncio
async def test_gear_reset_puts_back_what_the_server_started_with(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_gear"] = "3"

    await _run(console, admin, "!pagear none")
    await _run(console, admin, "!pagear reset")

    assert console.cvars["g_gear"] == "3"


@pytest.mark.asyncio
async def test_a_weapon_nobody_has_heard_of(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pagear +banana")

    assert "g_gear" not in console.cvars
    assert "weapons:" in _told(console, admin)[-1]


# -- !pasetnextmap ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_next_map_is_written_to_the_cvar_the_profile_names(console):
    """`g_nextmap` on Urban Terror, and the profile is what knows that — `!nextmap` and `callvote`
    read the same field."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.next_map_cvar = "g_nextmap"  # as the Urban Terror profile declares

    await _run(console, admin, "!pasetnextmap ut4_casa")

    assert console.cvars["g_nextmap"] == "ut4_casa"
    assert _told(console, admin) == ["the next map will be ut4_casa"]


@pytest.mark.asyncio
async def test_a_game_with_no_next_map_cvar_says_so(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    assert console.next_map_cvar == ""  # most titles have none

    await _run(console, admin, "!pasetnextmap ut4_casa")

    assert console.cvars == {}
    assert _told(console, admin) == ["this game has no next-map setting"]


# -- moving players between teams ---------------------------------------------------------------


def _teamed(console, name, team, bits=0, cid=None):  # noqa: ANN001, ANN202
    client = _join(console, name, bits=bits, cid=cid)
    client.team = team
    return client


@pytest.mark.asyncio
async def test_a_player_can_be_forced_to_a_team(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Bob", "blue")

    await _run(console, admin, "!paforce Bob red")

    assert console.verbs_applied[-1][0] == "forceteam"
    assert console.verbs_applied[-1][2] == {"team": "red"}
    assert _told(console, admin) == ["Bob moved to red"]


@pytest.mark.asyncio
async def test_the_engines_own_spelling_for_the_spectators(console):
    """`s`, not `spec` — and an admin may type either."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Bob", "blue")

    await _run(console, admin, "!paforce Bob spec")

    assert console.verbs_applied[-1][2] == {"team": "s"}
    assert _told(console, admin) == ["Bob moved to spectator"]


@pytest.mark.asyncio
async def test_a_team_nobody_has_heard_of(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Bob", "blue")

    await _run(console, admin, "!paforce Bob sideways")

    assert console.verbs_applied == []
    assert "!paforce" in _told(console, admin)[-1]


@pytest.mark.asyncio
async def test_a_locked_player_is_put_back_when_they_switch(console):
    """The only thing that makes the lock mean anything: `forceteam` moves somebody once."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue")

    await _run(console, admin, "!paforce Bob red lock")
    console.verbs_applied.clear()

    bob.team = "blue"  # they switched back
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=bob, data="blue"))

    assert console.verbs_applied[-1][0] == "forceteam"
    assert console.verbs_applied[-1][2] == {"team": "red"}
    assert any("locked to red" in text for _who, text in console.told)


@pytest.mark.asyncio
async def test_an_unlocked_player_may_switch_freely(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue")

    await _run(console, admin, "!paforce Bob red")
    console.verbs_applied.clear()

    bob.team = "blue"
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=bob, data="blue"))

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_team_change_to_where_they_are_already_locked_is_left_alone(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue")
    await _run(console, admin, "!paforce Bob red lock")
    console.verbs_applied.clear()

    bob.team = "red"
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=bob, data="red"))

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_lock_can_be_released(console):
    plugin = _plugin(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue")
    await _run(console, admin, "!paforce Bob red lock")

    await _run(console, admin, "!paforce Bob free")

    assert plugin.locked_to(bob) is None
    assert any("choose their own team" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_releasing_a_lock_nobody_had(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Bob", "blue")

    await _run(console, admin, "!paforce Bob free")

    assert _told(console, admin) == ["Bob was not locked to anything"]


@pytest.mark.asyncio
async def test_a_player_who_leaves_takes_their_lock_with_them(console):
    plugin = _plugin(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue")
    await _run(console, admin, "!paforce Bob red lock")

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bob))

    assert plugin.locked_to(bob) is None


@pytest.mark.asyncio
async def test_you_cannot_force_a_peer(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Other", "blue", bits=64)

    await _run(console, admin, "!paforce Other red")

    assert console.verbs_applied == []
    assert _told(console, admin) == ["Other is not somebody you can do that to"]


# -- !paswap ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_players_can_change_places(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "red", cid="2")
    _teamed(console, "Ann", "blue", cid="3")

    await _run(console, admin, "!paswap Bob Ann")

    assert console.verbs_applied[-1][0] == "swap"
    assert console.verbs_applied[-1][1] is bob
    assert console.verbs_applied[-1][2] == {"other": "3"}


@pytest.mark.asyncio
async def test_with_one_name_you_swap_with_them_yourself(console):
    _plugin(console)
    admin = _teamed(console, "Admin", "red", bits=64, cid="1")
    _teamed(console, "Bob", "blue", cid="2")

    await _run(console, admin, "!paswap Bob")

    assert console.verbs_applied[-1][2] == {"other": "1"}


@pytest.mark.asyncio
async def test_two_players_on_one_team_cannot_swap(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Bob", "red", cid="2")
    _teamed(console, "Ann", "red", cid="3")

    await _run(console, admin, "!paswap Bob Ann")

    assert console.verbs_applied == []
    assert _told(console, admin) == ["Bob and Ann are on the same team"]


@pytest.mark.asyncio
async def test_a_spectator_has_nothing_to_swap(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Bob", "spec", cid="2")
    _teamed(console, "Ann", "red", cid="3")

    await _run(console, admin, "!paswap Bob Ann")

    assert console.verbs_applied == []
    assert _told(console, admin) == ["Bob is a spectator, so there is nothing to swap"]


# -- the whole-team commands --------------------------------------------------


@pytest.mark.asyncio
async def test_the_teams_can_be_swapped_and_shuffled(console):
    """Neither names a player, which is what `server_verbs` is for."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paswapteams")
    await _run(console, admin, "!pashuffleteams")

    assert [name for name, _values in console.server_verbs_applied] == [
        "swapteams",
        "shuffleteams",
    ]
    assert _told(console, admin) == [
        "the teams have been swapped",
        "the teams have been shuffled",
    ]


@pytest.mark.asyncio
async def test_a_game_with_no_such_verb_says_so(console):
    console.server_verbs = set()
    console.player_verbs = set()
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    _teamed(console, "Bob", "blue")

    await _run(console, admin, "!pashuffleteams")
    await _run(console, admin, "!paforce Bob red")
    await _run(console, admin, "!paswap Bob")

    assert console.server_verbs_applied == []
    assert console.verbs_applied == []
    assert _told(console, admin) == [
        "this game has no shuffleteams command",
        "this game has no forceteam command",
        "this game has no swap command",
    ]


# -- the server commands ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_map_can_be_restarted_reloaded_and_cycled(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamaprestart")
    await _run(console, admin, "!pamapreload")
    await _run(console, admin, "!pacyclemap")

    assert [name for name, _values in console.server_verbs_applied] == [
        "map_restart",
        "reload",
        "cyclemap",
    ]


@pytest.mark.asyncio
async def test_a_config_file_can_be_run(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=100)

    await _run(console, admin, "!paexec match.cfg")

    assert console.server_verbs_applied == [("exec", {"file": "match.cfg"})]


@pytest.mark.asyncio
async def test_a_filename_with_a_command_in_it_is_refused(console):
    """`exec` takes a filename; a "filename" with a `;` is somebody running a second command.

    Refused rather than sanitised, because there is no reading of `match.cfg; quit` that is a file.
    """
    _plugin(console)
    admin = _join(console, "Admin", bits=100)

    await _run(console, admin, "!paexec match.cfg; quit")
    await _run(console, admin, "!paexec ../../etc/passwd")

    assert console.server_verbs_applied == []
    assert len(_told(console, admin)) == 2


@pytest.mark.asyncio
async def test_running_a_config_file_needs_a_higher_level(console):
    """What a config file contains is anything, so it sits above the other server commands."""
    _plugin(console)
    registered = console.command_registry.get("paexec")

    assert registered is not None
    assert registered.min_level == 80


# -- !papublic ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_can_be_made_public(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_password"] = "secret22"

    await _run(console, admin, "!papublic on")

    assert console.cvars["g_password"] == ""
    assert console.said == ["the server is public again"]


@pytest.mark.asyncio
async def test_going_private_builds_a_password_from_the_configured_word(console):
    _plugin(console, private_password="clanwar", password_digits=2)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!papublic off")

    password = console.cvars["g_password"]
    assert password.startswith("clanwar")
    assert len(password) == len("clanwar") + 2
    assert password[len("clanwar") :].isdigit()
    # Told to the admin privately, and to nobody else.
    assert any(password in text for text in _told(console, admin))
    assert not any(password in text for text in console.said)


@pytest.mark.asyncio
async def test_the_digits_change_between_uses(console):
    """Which is the only reason to add them: the same password twice is not a new password."""
    _plugin(console, private_password="clanwar", password_digits=6)
    admin = _join(console, "Admin", bits=64)

    seen = set()
    for _ in range(5):
        await _run(console, admin, "!papublic off")
        seen.add(console.cvars["g_password"])

    assert len(seen) > 1


@pytest.mark.asyncio
async def test_going_private_with_no_password_configured_refuses(console):
    """The classic checked for this *after* building the password, so the branch never ran: it set a
    password of two random digits and told the admin that was the password."""
    _plugin(console, private_password="")
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!papublic off")

    assert "g_password" not in console.cvars
    assert _told(console, admin) == ["set private_password in the plugin config first"]


@pytest.mark.asyncio
async def test_papublic_accepts_either_case(console):
    """The classic compared without lower-casing, so `!papublic ON` was refused."""
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!papublic ON")

    assert console.cvars["g_password"] == ""


# -- the team balancer --------------------------------------------------------------------------

#: `g_gametype` as the engine writes it: 3 is team deathmatch, 4 team survivor, 0 free-for-all.
TDM, TS, FFA = "3", "4", "0"


def _balancer(console, **settings):  # noqa: ANN001, ANN202
    """A plugin with the quiet window already elapsed, on a gametype it balances."""
    console.game.gametype = TDM
    plugin = _plugin(console, **settings)
    console.clock.advance(plugin.settings["teambalance_quiet_seconds"] + 1)
    return plugin


def _sides(console, plugin, red, blue):  # noqa: ANN001, ANN202
    """`red` players on red and `blue` on blue, each having joined a second after the last."""
    made = []
    for team, count in (("red", red), ("blue", blue)):
        for _ in range(count):
            console.clock.advance(1)
            client = _teamed(console, f"{team}{len(made)}", team, bits=0)
            client.set_var(plugin, "team_time", console.clock.now())
            made.append(client)
    return made


@pytest.mark.asyncio
async def test_the_smallest_number_of_players_moves(console):
    """Six against two, tolerating one: two players move, which is what the arithmetic says.

    The classic moved one, asked the server to count the teams again, and went round up to
    twenty-five times.
    """
    plugin = _balancer(console)
    _sides(console, plugin, red=6, blue=2)

    moved, uneven = plugin.balance()

    assert uneven
    assert len(moved) == 2
    assert [name for name, _who, _values in console.verbs_applied] == ["forceteam", "forceteam"]
    assert all(values == {"team": "blue"} for _n, _w, values in console.verbs_applied)


@pytest.mark.asyncio
async def test_whoever_joined_last_is_the_one_who_moves(console):
    plugin = _balancer(console)
    made = _sides(console, plugin, red=4, blue=1)

    moved, _uneven = plugin.balance()

    assert [c.name for c in moved] == [made[3].name]  # the last red to arrive


@pytest.mark.asyncio
async def test_teams_within_tolerance_are_left_alone(console):
    plugin = _balancer(console)
    _sides(console, plugin, red=4, blue=3)

    moved, uneven = plugin.balance()

    assert (moved, uneven) == ([], False)
    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_wider_tolerance_is_honoured(console):
    plugin = _balancer(console, teambalance_difference=3)
    _sides(console, plugin, red=5, blue=2)

    moved, uneven = plugin.balance()

    assert (moved, uneven) == ([], False)


@pytest.mark.asyncio
async def test_an_admin_is_left_where_they_are(console):
    """So that an admin can go and help the weaker team without being sent back."""
    plugin = _balancer(console)
    _sides(console, plugin, red=1, blue=3)
    console.clock.advance(1)
    _teamed(console, "Boss", "blue", bits=64)

    moved, _uneven = plugin.balance()

    assert [c.name for c in moved] == ["blue3"]


@pytest.mark.asyncio
async def test_a_locked_player_is_never_the_one_moved(console):
    plugin = _balancer(console)
    made = _sides(console, plugin, red=1, blue=3)
    made[-1].set_var(plugin, "locked_to", "blue")

    moved, _uneven = plugin.balance()

    assert [c.name for c in moved] == [made[-2].name]


@pytest.mark.asyncio
async def test_uneven_teams_nobody_may_move_announce_nothing(console):
    """The classic announced "Autobalancing Teams!" *before* looking for somebody to move, so a
    server whose bigger team was all admins was told it was being balanced every interval, forever,
    while nothing happened."""
    plugin = _balancer(console)
    for index in range(4):
        console.clock.advance(1)
        _teamed(console, f"boss{index}", "red", bits=64)
    _teamed(console, "Lonely", "blue", bits=0)

    moved, uneven = plugin.balance()

    assert (moved, uneven) == ([], True)
    assert console.said_big == []
    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_spectators_are_not_a_side(console):
    plugin = _balancer(console)
    _sides(console, plugin, red=3, blue=2)
    for index in range(6):
        _teamed(console, f"watcher{index}", "spec", bits=0)

    moved, uneven = plugin.balance()

    assert (moved, uneven) == ([], False)


@pytest.mark.asyncio
async def test_the_balance_is_announced_the_way_the_operator_asked(console):
    plugin = _balancer(console, teambalance_announce="say")
    _sides(console, plugin, red=4, blue=1)

    plugin.balance()

    assert console.said == ["balancing the teams"]
    assert console.said_big == []


@pytest.mark.asyncio
async def test_the_announcement_can_be_switched_off(console):
    plugin = _balancer(console, teambalance_announce="off")
    _sides(console, plugin, red=4, blue=1)

    plugin.balance()

    assert (console.said, console.said_big) == ([], [])


# -- the quiet window (the classic's ignoreSet) ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_scheduled_check_stands_down_after_a_shuffle(console):
    """Without this the balancer spends its next pass undoing the shuffle an admin just asked for."""
    plugin = _balancer(console)
    admin = _join(console, "Admin", bits=64)
    _sides(console, plugin, red=4, blue=1)

    await _run(console, admin, "!pashuffleteams")
    console.verbs_applied.clear()
    plugin._teamcheck()

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_the_quiet_window_runs_out(console):
    plugin = _balancer(console)
    admin = _join(console, "Admin", bits=64)
    _sides(console, plugin, red=4, blue=1)
    await _run(console, admin, "!pashuffleteams")
    console.verbs_applied.clear()

    console.clock.advance(plugin.settings["teambalance_quiet_seconds"] + 1)
    plugin._teamcheck()

    assert [name for name, _w, _v in console.verbs_applied] == ["forceteam"]


@pytest.mark.asyncio
async def test_a_round_starting_quiets_the_checks(console):
    plugin = _balancer(console)
    _sides(console, plugin, red=4, blue=1)

    await console.bus.publish(Event(EventType.GAME_ROUND_START))
    plugin._teamcheck()

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_bot_that_has_just_started_balances_nothing(console):
    """It is about to adopt a server full of players, every one of whom looks like a fresh joiner."""
    console.game.gametype = TDM
    plugin = _plugin(console)
    _sides(console, plugin, red=5, blue=1)

    plugin._teamcheck()

    assert console.verbs_applied == []


# -- gametypes ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_gametype_with_no_teams_is_not_balanced(console):
    plugin = _balancer(console)
    console.game.gametype = FFA
    _sides(console, plugin, red=5, blue=1)

    plugin._teamcheck()

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_round_based_gametype_waits_for_the_round_to_end(console):
    """Moving somebody mid-round takes them out of a round they are playing."""
    plugin = _balancer(console, teambalance_gametypes="tdm, ts")
    console.game.gametype = TS
    _sides(console, plugin, red=5, blue=1)

    plugin._teamcheck()
    assert console.verbs_applied == []
    assert plugin._balance_pending

    await console.bus.publish(Event(EventType.GAME_ROUND_END))

    assert [name for name, _w, _v in console.verbs_applied] == ["forceteam", "forceteam"]
    assert not plugin._balance_pending


@pytest.mark.asyncio
async def test_the_classics_spelling_for_bomb_mode_still_means_bomb_mode(console):
    """It wrote `bm`; the command here is `!pabomb`, so the gametype is named `bomb`."""
    plugin = _balancer(console, teambalance_gametypes="tdm, bm")

    assert "bomb" in plugin._gametypes("teambalance_gametypes")


# -- !pateams -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pateams_evens_the_teams_up(console):
    plugin = _balancer(console)
    admin = _join(console, "Admin", bits=64)
    _sides(console, plugin, red=4, blue=1)

    await _run(console, admin, "!pateams")

    assert [name for name, _w, _v in console.verbs_applied] == ["forceteam"]
    assert _told(console, admin) == ["the teams are even now"]


@pytest.mark.asyncio
async def test_pateams_works_without_the_pa(console):
    plugin = _balancer(console)
    admin = _join(console, "Admin", bits=64)
    _sides(console, plugin, red=4, blue=1)

    await _run(console, admin, "!teams")

    assert [name for name, _w, _v in console.verbs_applied] == ["forceteam"]


@pytest.mark.asyncio
async def test_pateams_on_even_teams_says_so(console):
    plugin = _balancer(console)
    admin = _join(console, "Admin", bits=64)
    _sides(console, plugin, red=3, blue=3)

    await _run(console, admin, "!pateams")

    assert console.verbs_applied == []
    assert _told(console, admin) == ["the teams are already even"]


@pytest.mark.asyncio
async def test_pateams_says_when_it_could_not_move_anybody(console):
    """The classic answered "Teams are now balanced" here, having moved nobody at all."""
    _balancer(console)
    admin = _join(console, "Admin", bits=64)
    for index in range(4):
        _teamed(console, f"boss{index}", "red", bits=64)
    _teamed(console, "Lonely", "blue", bits=0)

    await _run(console, admin, "!pateams")

    assert console.verbs_applied == []
    assert _told(console, admin) == [
        "the teams are uneven, but there is nobody I am allowed to move"
    ]


@pytest.mark.asyncio
async def test_pateams_asked_mid_round_waits(console):
    plugin = _balancer(console, teambalance_gametypes="tdm, ts")
    console.game.gametype = TS
    admin = _join(console, "Admin", bits=64)
    _sides(console, plugin, red=4, blue=1)

    await _run(console, admin, "!pateams")

    assert console.verbs_applied == []
    assert _told(console, admin) == ["the teams will be evened up at the end of this round"]
    assert plugin._balance_pending


@pytest.mark.asyncio
async def test_pateams_is_a_regulars_command(console):
    """The classic let a regular ask, and asking only ever evens the teams."""
    _balancer(console)

    registered = console.command_registry.get("pateams")
    assert registered is not None
    assert registered.min_level == 2


@pytest.mark.asyncio
async def test_pateams_on_a_game_with_no_forceteam(console):
    _balancer(console)
    console.player_verbs = set()
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pateams")

    assert _told(console, admin) == ["this game has no forceteam command"]


# -- a switch that unbalances the teams -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_switch_that_unbalances_the_teams_is_reversed(console):
    plugin = _balancer(console)
    _sides(console, plugin, red=2, blue=3)
    console.clock.advance(1)
    turncoat = _teamed(console, "Turncoat", "blue", bits=0)

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=turncoat, data="blue"))

    assert console.verbs_applied[-1][0] == "forceteam"
    assert console.verbs_applied[-1][2] == {"team": "red"}
    assert any("you are back on red" in text for _who, text in console.told)


@pytest.mark.asyncio
async def test_joining_the_smaller_team_is_never_punished(console):
    """The classic asked "are the teams uneven?" and not "did *this* player make them uneven", so
    with red on five and blue on two, joining blue got you forced to "the smaller team" — the one you
    had just joined — plus two fabricated suicides deducted from your stats."""
    plugin = _balancer(console)
    _sides(console, plugin, red=5, blue=2)
    console.clock.advance(1)
    helper = _teamed(console, "Helper", "blue", bits=0)

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=helper, data="blue"))

    assert console.verbs_applied == []
    assert console.told == []


@pytest.mark.asyncio
async def test_going_to_the_spectators_is_not_a_switch(console):
    plugin = _balancer(console)
    _sides(console, plugin, red=5, blue=1)
    watcher = _teamed(console, "Watcher", "spec", bits=0)

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=watcher, data="spec"))

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_an_admin_may_switch_to_the_bigger_team(console):
    plugin = _balancer(console)
    _sides(console, plugin, red=3, blue=1)
    console.clock.advance(1)
    boss = _teamed(console, "Boss", "red", bits=64)

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=boss, data="red"))

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_reversing_a_switch_can_be_switched_off(console):
    plugin = _balancer(console, teambalance_on_team_change=False)
    _sides(console, plugin, red=2, blue=3)
    console.clock.advance(1)
    turncoat = _teamed(console, "Turncoat", "blue", bits=0)

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=turncoat, data="blue"))

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_team_change_records_when_they_joined(console):
    plugin = _balancer(console)
    bob = _teamed(console, "Bob", "red", bits=0)

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=bob, data="red"))

    assert bob.get_var(plugin, "team_time") == console.clock.now()


# -- locks and the end of a map -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locks_lapse_when_the_map_ends(console):
    plugin = _balancer(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue", bits=0)
    await _run(console, admin, "!paforce Bob red lock")

    await console.bus.publish(Event(EventType.GAME_EXIT))

    assert plugin.locked_to(bob) is None


@pytest.mark.asyncio
async def test_locks_can_be_asked_to_survive_the_map(console):
    plugin = _balancer(console, team_locks_permanent=True)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue", bits=0)
    await _run(console, admin, "!paforce Bob red lock")

    await console.bus.publish(Event(EventType.GAME_EXIT))

    assert plugin.locked_to(bob) == "red"


@pytest.mark.asyncio
async def test_a_player_locked_to_the_spectators_is_left_alone_there(console):
    """`!paforce` spells the spectators `s` for the engine; the parser reports them as `spec`. Held
    against each other unmapped, a locked spectator was force-teamed on every line they produced."""
    _balancer(console)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "blue", bits=0)
    await _run(console, admin, "!paforce Bob spec lock")
    bob.team = "spec"
    console.verbs_applied.clear()

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=bob, data="spec"))

    assert console.verbs_applied == []


# -- the skill balancer -------------------------------------------------------------------------


def _skiller(console, **settings):  # noqa: ANN001, ANN202
    """A plugin past its quiet window, on team deathmatch, with a shuffle that repeats."""
    plugin = _balancer(console, **settings)
    plugin.random.seed(1)
    return plugin


def _played(plugin, client, kills=0, deaths=0, team_kills=0, heads=0, minutes=5.0):  # noqa: ANN001, ANN202
    """Give a player a session, as though they had been on this team for `minutes`."""
    record = plugin.record(client)
    record.joined = plugin.console.clock.now() - minutes * 60
    record.kills, record.deaths = kills, deaths
    record.team_kills, record.head_hits = team_kills, heads
    now = plugin.console.clock.now()
    record.history = [(now, 1)] * kills + [(now, -1)] * deaths
    return record


@pytest.mark.asyncio
async def test_a_better_player_scores_higher(console):
    plugin = _skiller(console)
    good = _teamed(console, "Good", "red", bits=0)
    bad = _teamed(console, "Bad", "blue", bits=0)
    _played(plugin, good, kills=20, deaths=2)
    _played(plugin, bad, kills=2, deaths=20)

    scores = plugin.scores([good, bad])

    assert scores[good.cid] > scores[bad.cid]


@pytest.mark.asyncio
async def test_two_unauthenticated_players_are_scored_separately(console):
    """The classic keyed its scores on the *database id*, which is None for anybody the bot has not
    authenticated — and on a Quake3 server without `cl_guid` that is everybody, so every one of them
    shared the key `None` and overwrote each other's figures."""
    plugin = _skiller(console)
    good = _teamed(console, "Good", "red", bits=0)
    bad = _teamed(console, "Bad", "blue", bits=0)
    good.id = bad.id = None
    _played(plugin, good, kills=20, deaths=2)
    _played(plugin, bad, kills=2, deaths=20)

    scores = plugin.scores([good, bad])

    assert len(scores) == 2
    assert scores[good.cid] > scores[bad.cid]


@pytest.mark.asyncio
async def test_a_player_who_just_arrived_is_not_judged_on_two_kills(console):
    """Two kills in ten seconds is not evidence. The classic's readme records this being added after
    sprees right after joining spiked the arithmetic."""
    plugin = _skiller(console)
    veteran = _teamed(console, "Veteran", "red", bits=0)
    average = _teamed(console, "Average", "red", bits=0)
    fresh = _teamed(console, "Fresh", "blue", bits=0)
    _played(plugin, veteran, kills=20, deaths=2, minutes=10)
    _played(plugin, average, kills=10, deaths=10, minutes=10)
    _played(plugin, fresh, kills=30, deaths=0, minutes=0.1)

    scores = plugin.scores([veteran, average, fresh])

    # Best on every measure and still ranked below somebody with a real session behind them.
    assert scores[fresh.cid] < scores[veteran.cid]


@pytest.mark.asyncio
async def test_a_measure_everybody_shares_says_nothing(console):
    """Two players with identical figures score the same, and no key survives to separate them."""
    plugin = _skiller(console)
    one = _teamed(console, "One", "red", bits=0)
    two = _teamed(console, "Two", "blue", bits=0)
    _played(plugin, one, kills=5, deaths=5)
    _played(plugin, two, kills=5, deaths=5)

    scores = plugin.scores([one, two])

    assert scores[one.cid] == scores[two.cid] == 0.0


@pytest.mark.asyncio
async def test_the_mission_counts_for_more_than_the_scoreboard(console):
    """A flag carrier who dies a lot is winning the game, which is the whole point of CTF."""
    plugin = _skiller(console)
    carrier = _teamed(console, "Carrier", "red", bits=0)
    fragger = _teamed(console, "Fragger", "blue", bits=0)
    _played(plugin, carrier, kills=1, deaths=10)
    plugin.record(carrier).flag_captured = 3
    _played(plugin, fragger, kills=10, deaths=1)

    scores = plugin.scores([carrier, fragger])

    assert scores[carrier.cid] > scores[fragger.cid]


@pytest.mark.asyncio
async def test_the_score_is_divided_by_the_weight_that_actually_counted(console):
    """The classic divided by the weight of *every* measure, including the ones it had skipped and
    the two it read from xlrstats — so every score came out a fraction of what the weights say, and
    the autobalance threshold on CTF and bomb mode meant about twice what it appears to."""
    plugin = _skiller(console)
    best = _teamed(console, "Best", "red", bits=0)
    worst = _teamed(console, "Worst", "blue", bits=0)
    _played(plugin, best, kills=20, deaths=0)
    _played(plugin, worst, kills=0, deaths=20)

    scores = plugin.scores([best, worst])

    # Best on every surviving measure means the top of the range, not a fifth of it.
    assert scores[best.cid] == pytest.approx(1.0)
    assert scores[worst.cid] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_a_headshot_is_counted_without_turning_on_the_headshot_counter(console):
    """In the classic the head-hit figures this score reads were written only by `headshotcounter`,
    which is a separate feature and off by default — so `hsratio` was zero for everybody and dropped
    out of the score entirely, on any server that had not switched that on."""
    plugin = _skiller(console)
    sniper = _teamed(console, "Sniper", "red", bits=0)
    other = _teamed(console, "Other", "blue", bits=0)

    await console.bus.publish(
        Event(
            EventType.CLIENT_KILL,
            client=sniper,
            target=other,
            data=KillData(weapon="UT_MOD_SR8", damage=100, hit_location="head", means_of_death=""),
        )
    )

    assert plugin.record(sniper).head_hits == 1
    assert plugin.record(sniper).kills == 1
    assert plugin.record(other).deaths == 1


@pytest.mark.asyncio
async def test_a_team_change_starts_the_measurement_again(console):
    """What they did for the other side is not evidence about this one."""
    plugin = _skiller(console)
    bob = _teamed(console, "Bob", "red", bits=0)
    _played(plugin, bob, kills=10, deaths=1)

    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, client=bob, data="red"))

    assert plugin.record(bob).kills == 0


# -- shuffling ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_shuffle_evens_the_skill_out(console):
    """Four good players on one side and four poor ones on the other, dealt out again."""
    plugin = _skiller(console)
    good = [_teamed(console, f"Good{i}", "red", bits=0) for i in range(4)]
    poor = [_teamed(console, f"Poor{i}", "blue", bits=0) for i in range(4)]
    for i, client in enumerate(good):
        _played(plugin, client, kills=20 - i, deaths=2)
    for i, client in enumerate(poor):
        _played(plugin, client, kills=2, deaths=20 - i)
    before = abs(plugin._score_diff(poor, good, plugin.scores(good + poor)))

    plugin.skuffle()

    clients = list(console.clients.connected())
    blue = [c for c in clients if c.team == "blue"]
    red = [c for c in clients if c.team == "red"]
    assert len(blue) == len(red) == 4
    # The measurement restarts after a shuffle, so compare the arrangement, not the new scores.
    assert before > 0
    assert sum(1 for c in good if c.team == "blue") in (1, 2, 3)


@pytest.mark.asyncio
async def test_a_shuffle_leaves_a_locked_player_where_they_are(console):
    plugin = _skiller(console)
    reds = [_teamed(console, f"Red{i}", "red", bits=0) for i in range(4)]
    blues = [_teamed(console, f"Blue{i}", "blue", bits=0) for i in range(4)]
    for i, client in enumerate([*reds, *blues]):
        _played(plugin, client, kills=i, deaths=8 - i)
    reds[0].set_var(plugin, "locked_to", "red")

    plugin.skuffle()

    assert reds[0].team == "red"


@pytest.mark.asyncio
async def test_whoever_asked_for_the_shuffle_stays_put(console):
    """Being thrown across the map for asking is jarring, and the other arrangement is just as
    even."""
    plugin = _skiller(console)
    reds = [_teamed(console, f"Red{i}", "red", bits=0) for i in range(4)]
    blues = [_teamed(console, f"Blue{i}", "blue", bits=0) for i in range(4)]
    for i, client in enumerate([*reds, *blues]):
        _played(plugin, client, kills=i * 2, deaths=8 - i)
    asker = reds[0]

    await _run(console, asker, "!paskuffle")

    assert asker.team == "red"


@pytest.mark.asyncio
async def test_a_shuffle_that_cannot_improve_anything_says_so(console):
    plugin = _skiller(console)
    for i in range(2):
        _teamed(console, f"Red{i}", "red", bits=0)
        _teamed(console, f"Blue{i}", "blue", bits=0)

    plugin.skuffle()

    assert console.said == ["the teams cannot be made any more even than they are"]


@pytest.mark.asyncio
async def test_nobody_is_moved_into_a_full_team(console):
    """This engine refuses a `forceteam` that would overfill a side, so with the two sides equal one
    player is parked in the spectators to make room and brought back at the end."""
    plugin = _skiller(console)
    red = _teamed(console, "Red", "red", bits=0)
    blue = _teamed(console, "Blue", "blue", bits=0)
    _played(plugin, red, kills=10, deaths=1)
    _played(plugin, blue, kills=1, deaths=10)

    plugin.shuffle_into([red], [blue])

    verbs = [(name, who.name, values["team"]) for name, who, values in console.verbs_applied]
    assert verbs[0][2] == "s"  # parked
    assert verbs[-1][2] == "blue"  # and brought back
    assert {red.team, blue.team} == {"red", "blue"}


@pytest.mark.asyncio
async def test_unskuffle_puts_the_best_players_together(console):
    """The classic shipped this to test the balancer with, and it is worth keeping for that."""
    plugin = _skiller(console)
    admin = _join(console, "Admin", bits=64)
    players = [_teamed(console, f"P{i}", "red" if i % 2 else "blue", bits=0) for i in range(6)]
    for i, client in enumerate(players):
        _played(plugin, client, kills=i * 3, deaths=18 - i * 3)

    await _run(console, admin, "!paunskuffle")

    sides = {c.name: c.team for c in players}
    # The two halves by score end up on one side each; which side is not the point.
    assert sides["P0"] == sides["P1"] == sides["P2"]
    assert sides["P3"] == sides["P4"] == sides["P5"]
    assert sides["P0"] != sides["P5"]


@pytest.mark.asyncio
async def test_the_shuffle_is_announced_the_way_the_operator_asked(console):
    plugin = _skiller(console, teambalance_announce="say")
    good = [_teamed(console, f"Good{i}", "red", bits=0) for i in range(3)]
    poor = [_teamed(console, f"Poor{i}", "blue", bits=0) for i in range(3)]
    for client in good:
        _played(plugin, client, kills=20, deaths=1)
    for client in poor:
        _played(plugin, client, kills=1, deaths=20)

    plugin.skuffle()

    assert "shuffling the teams by skill" in console.said
    assert console.said_big == []


# -- !pabalance -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pabalance_moves_only_a_few(console):
    plugin = _skiller(console, skuffle_max_move_fraction=0.3)
    everybody = [_teamed(console, f"P{i}", "red" if i < 5 else "blue", bits=0) for i in range(10)]
    for i, client in enumerate(everybody):
        _played(plugin, client, kills=i * 2, deaths=20 - i * 2)

    plugin.balance_by_skill()

    moved = [name for name, _who, _values in console.verbs_applied]
    assert 0 < len(moved) <= 3


@pytest.mark.asyncio
async def test_pabalance_is_a_regulars_command(console):
    """It is the classic's replacement for `!teams`, and a regular could ask for that too."""
    _skiller(console)

    registered = console.command_registry.get("pabalance")
    assert registered is not None
    assert registered.min_level == 2


@pytest.mark.asyncio
async def test_a_regular_may_not_ask_twice_in_a_row(console):
    plugin = _skiller(console, skillbalance_min_interval=2)
    bob = _teamed(console, "Bob", "red", bits=2)
    _teamed(console, "Ann", "blue", bits=0)
    plugin._last_balance = console.clock.now()

    await _run(console, bob, "!pabalance")

    assert console.verbs_applied == []
    assert _told(console, bob) == ["the teams changed recently; give it a minute"]


@pytest.mark.asyncio
async def test_a_moderator_may(console):
    """The classic's rule was an `and` of three conditions, one being "a quiet window is open", so
    the wait it advertised almost never applied to anybody."""
    plugin = _skiller(console, skillbalance_min_interval=2)
    boss = _teamed(console, "Boss", "red", bits=8)
    _teamed(console, "Ann", "blue", bits=0)
    plugin._last_balance = console.clock.now()

    await _run(console, boss, "!pabalance")

    assert _told(console, boss) != ["the teams changed recently; give it a minute"]


# -- advice ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_even_teams_are_reported_as_fair(console):
    plugin = _skiller(console)

    plugin.advise(0.0, "advise")

    assert console.said == ["the teams look fair"]


@pytest.mark.asyncio
async def test_a_dominant_side_is_named(console):
    plugin = _skiller(console)

    plugin.advise(0.6, "advise")  # 5 * 0.6 = 3.0 -> "dominating", and unfair

    assert console.said == ["blue team is now dominating - !bal would even them up"]


@pytest.mark.asyncio
async def test_the_weaker_side_is_named_when_the_difference_is_negative(console):
    plugin = _skiller(console)

    plugin.advise(-0.6, "advise")

    assert console.said[0].startswith("red team is now")


@pytest.mark.asyncio
async def test_advice_says_what_changed_since_last_time(console):
    plugin = _skiller(console)

    plugin.advise(0.6, "quiet")
    plugin.advise(1.0, "quiet")

    assert console.said[-1] == "blue team has become overpowering"


@pytest.mark.asyncio
async def test_advice_notices_a_side_coming_back(console):
    plugin = _skiller(console)

    plugin.advise(1.2, "quiet")
    plugin.advise(0.5, "quiet")

    assert console.said[-1] == "blue team is just dominating"


@pytest.mark.asyncio
async def test_a_mild_difference_needs_no_action(console):
    plugin = _skiller(console)

    plugin.advise(0.3, "advise")  # 1.5 -> "stronger", below the unfair threshold

    assert console.said == ["blue team is now stronger - nothing worth doing about it yet"]


@pytest.mark.asyncio
async def test_paadvise_with_nobody_playing_says_so(console):
    """The figures are meaningless with nobody in the game, and the classic printed them anyway."""
    _skiller(console, skillbalance_min_players=3)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paadvise")

    assert console.said == []
    assert _told(console, admin) == ["not enough players to tell"]


# -- the automatic mode ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mode_can_be_read_and_set(console):
    plugin = _skiller(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paautoskuffle")
    await _run(console, admin, "!paautoskuffle balance")

    assert plugin.skill_mode() == "balance"
    assert _told(console, admin)[-1] == "skill balancing is now balance"


@pytest.mark.asyncio
async def test_yaml_reading_off_as_a_boolean_still_means_off(console):
    """`skillbalance_mode: off` in a YAML file arrives as `False`, not as the word. That is a trap
    the file format lays for the operator rather than anything they typed."""
    plugin = _skiller(console, skillbalance_mode=False)

    assert plugin.skill_mode() == "off"


@pytest.mark.asyncio
async def test_a_mode_in_the_config_that_is_not_a_mode_is_reported(console, caplog):
    plugin = _skiller(console, skillbalance_mode="sideways")

    with caplog.at_level("WARNING"):
        assert plugin.skill_mode() == "off"

    assert "sideways" in caplog.text


@pytest.mark.asyncio
async def test_the_classics_numbers_still_work(console):
    plugin = _skiller(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paautoskuffle 3")

    assert plugin.skill_mode() == "shuffle"


@pytest.mark.asyncio
async def test_a_mode_nobody_has_heard_of(console):
    plugin = _skiller(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!paautoskuffle sideways")

    assert plugin.skill_mode() == "off"
    assert "off, advise, balance, shuffle" in _told(console, admin)[-1]


@pytest.mark.asyncio
async def test_the_skill_check_does_nothing_when_it_is_off(console):
    plugin = _skiller(console, skillbalance_mode="off")
    good = [_teamed(console, f"Good{i}", "red", bits=0) for i in range(3)]
    poor = [_teamed(console, f"Poor{i}", "blue", bits=0) for i in range(3)]
    for client in [*good, *poor]:
        _played(plugin, client, kills=20 if client in good else 0, deaths=1)

    plugin.skillcheck()

    assert console.verbs_applied == []
    assert console.said == []


@pytest.mark.asyncio
async def test_advise_mode_talks_and_does_not_move_anybody(console):
    plugin = _skiller(console, skillbalance_mode="advise")
    good = [_teamed(console, f"Good{i}", "red", bits=0) for i in range(3)]
    poor = [_teamed(console, f"Poor{i}", "blue", bits=0) for i in range(3)]
    for client in good:
        _played(plugin, client, kills=30, deaths=0)
    for client in poor:
        _played(plugin, client, kills=0, deaths=30)

    plugin.skillcheck()

    assert console.verbs_applied == []
    assert console.said != []


@pytest.mark.asyncio
async def test_a_skill_balance_owed_mid_round_waits_for_the_end(console):
    plugin = _skiller(
        console,
        skillbalance_mode="shuffle",
        teambalance_gametypes="tdm, ts",
        skillbalance_min_players=2,
    )
    console.game.gametype = TS
    good = [_teamed(console, f"Good{i}", "red", bits=0) for i in range(3)]
    poor = [_teamed(console, f"Poor{i}", "blue", bits=0) for i in range(3)]
    for client in good:
        _played(plugin, client, kills=30, deaths=0)
    for client in poor:
        _played(plugin, client, kills=0, deaths=30)

    plugin.skillcheck()
    assert console.verbs_applied == []
    assert plugin._skill_pending is not None

    await console.bus.publish(Event(EventType.GAME_ROUND_END))

    assert plugin._skill_pending is None


@pytest.mark.asyncio
async def test_a_skill_balance_wins_over_a_head_count_one(console):
    """Both can fall due in the same round, and evening the skill evens the numbers as well."""
    plugin = _skiller(console, teambalance_gametypes="tdm, ts")
    console.game.gametype = TS
    plugin._balance_pending = True
    called: list[str] = []
    plugin._skill_pending = lambda: called.append("skill")

    await console.bus.publish(Event(EventType.GAME_ROUND_END))

    assert called == ["skill"]
    assert not plugin._balance_pending


@pytest.mark.asyncio
async def test_too_few_players_is_not_an_imbalance(console):
    plugin = _skiller(console, skillbalance_mode="shuffle", skillbalance_min_players=8)
    for i in range(3):
        _played(plugin, _teamed(console, f"P{i}", "red", bits=0), kills=30, deaths=0)
    _played(plugin, _teamed(console, "Lonely", "blue", bits=0), kills=0, deaths=30)

    plugin.skillcheck()

    assert console.verbs_applied == []


# -- snipers ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sniper_who_cannot_shoot_is_not_a_sniper(console):
    """Somebody carrying an SR8 with a poor kill ratio is carrying a rifle, not using one."""
    plugin = _skiller(console)
    good = _teamed(console, "Good", "red", bits=0)
    poor = _teamed(console, "Poor", "red", bits=0)
    good.gear = poor.gear = "GZAORWA"
    _played(plugin, good, kills=10, deaths=1)
    _played(plugin, poor, kills=1, deaths=10)

    assert plugin._count_snipers([good, poor]) == 1


@pytest.mark.asyncio
async def test_gear_arrives_from_the_infostring(console):
    """It is the only place this engine says what anybody is carrying, and nothing read it before."""
    from b3.parsers.q3.profiles import IOURT42
    from b3.parsers.q3.urt import UrtParser

    p = UrtParser(IOURT42)
    p.parse_line(r"ClientUserinfoChanged: 3 n\Bob\t\1\gear\GZAORWA")

    assert p.clients.get_by_cid("3").gear == "GZAORWA"


# -- the spectator check ------------------------------------------------------------------------


def _watcher(console, plugin, name, minutes=30.0, bits=0):  # noqa: ANN001, ANN202
    """Somebody who has been in the spectators for `minutes`."""
    client = _teamed(console, name, "spec", bits=bits)
    client.set_var(plugin, "team_time", console.clock.now() - minutes * 60)
    return client


@pytest.mark.asyncio
async def test_a_long_idle_spectator_is_warned_on_a_full_server(console):
    plugin = _balancer(console, speccheck_min_players=2, speccheck_max_spec_minutes=5)
    _watcher(console, plugin, "Watcher")
    _teamed(console, "Player", "red", bits=0)

    plugin.speccheck()

    assert [c.name for c, _reason, _admin in console.warned] == ["Watcher"]
    assert console.warned[0][1] == "spectator too long on a full server"


@pytest.mark.asyncio
async def test_a_spectator_who_only_just_sat_down_is_left_alone(console):
    plugin = _balancer(console, speccheck_min_players=2, speccheck_max_spec_minutes=5)
    _watcher(console, plugin, "Watcher", minutes=1)
    _teamed(console, "Player", "red", bits=0)

    plugin.speccheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_a_quiet_server_is_nobody_elses_business(console):
    """The point of the check is that somebody else wants the slot."""
    plugin = _balancer(console, speccheck_min_players=8)
    _watcher(console, plugin, "Watcher")
    _teamed(console, "Player", "red", bits=0)

    plugin.speccheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_an_admin_may_watch_as_long_as_they_like(console):
    plugin = _balancer(console, speccheck_min_players=2, speccheck_max_level=20)
    _watcher(console, plugin, "Boss", bits=64)
    _teamed(console, "Player", "red", bits=0)

    plugin.speccheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_the_default_level_does_not_exempt_everybody(console):
    """The classic's default for this was 0, which exempts every player there is — so a server that
    set the interval and left the rest alone ran the check and skipped the whole roster."""
    plugin = _balancer(console, speccheck_min_players=2)
    _watcher(console, plugin, "Watcher")
    _teamed(console, "Player", "red", bits=0)

    assert plugin.settings["speccheck_max_level"] == 20

    plugin.speccheck()

    assert [c.name for c, _reason, _admin in console.warned] == ["Watcher"]


@pytest.mark.asyncio
async def test_a_player_an_admin_put_in_the_spectators_is_left_there(console):
    plugin = _balancer(console, speccheck_min_players=2)
    watcher = _watcher(console, plugin, "Watcher")
    watcher.set_var(plugin, "locked_to", "s")
    _teamed(console, "Player", "red", bits=0)

    plugin.speccheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_a_bot_is_not_asked_to_play_or_leave(console):
    """It fills a slot, so it counts towards the server being full; it cannot read a warning, so it
    never gets one. The classic warned bots, and the escalation kicked one — whereupon its own bot
    support added another."""
    plugin = _balancer(console, speccheck_min_players=2)
    bot = _watcher(console, plugin, "Bot")
    bot.is_bot = True
    human = _watcher(console, plugin, "Human")

    plugin.speccheck()

    assert [c.name for c, _reason, _admin in console.warned] == [human.name]


@pytest.mark.asyncio
async def test_nothing_happens_while_the_server_manages_its_own_slots(console):
    """`g_maxGameClients` means the *server* is putting the surplus into the spectators. Warning
    them for it would be the bot punishing players for what the server did to them."""
    plugin = _balancer(console, speccheck_min_players=2)
    console.cvars["g_maxGameClients"] = "12"
    _watcher(console, plugin, "Watcher")
    _teamed(console, "Player", "red", bits=0)

    plugin.speccheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_that_cvar_is_read_when_the_check_runs(console):
    """The classic read it once at bot start, into an attribute — with no error handling, so a
    server that answered nothing for it raised inside `onStartup` and the whole plugin, all
    forty-nine commands of it, failed to load."""
    plugin = _balancer(console, speccheck_min_players=2)
    console.cvars["g_maxGameClients"] = "12"
    _watcher(console, plugin, "Watcher")
    _teamed(console, "Player", "red", bits=0)
    plugin.speccheck()
    assert console.warned == []

    console.cvars["g_maxGameClients"] = "0"
    plugin.speccheck()

    assert [c.name for c, _reason, _admin in console.warned] == ["Watcher"]


@pytest.mark.asyncio
async def test_how_full_is_full_comes_from_the_servers_own_slots(console):
    """With sixteen slots and four of them reserved, twelve players is a full public server."""
    plugin = _balancer(console, speccheck_min_players=0)
    console.game.max_players = 16
    console.cvars["sv_privateClients"] = "4"
    for index in range(11):
        _teamed(console, f"P{index}", "red", bits=0)
    _watcher(console, plugin, "Watcher")

    plugin.speccheck()

    assert [c.name for c, _reason, _admin in console.warned] == ["Watcher"]


@pytest.mark.asyncio
async def test_the_check_stands_down_in_the_quiet_window(console):
    plugin = _balancer(console, speccheck_min_players=2)
    _watcher(console, plugin, "Watcher")
    _teamed(console, "Player", "red", bits=0)
    plugin.hold_off()

    plugin.speccheck()

    assert console.warned == []


# -- bot support --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plugin_not_running_bots_touches_no_bot_cvar(console):
    """Writing `bot_minplayers 0` at startup would turn off bots an operator manages by hand, and on
    a title that has no such cvar it invents one."""
    _plugin(console)

    assert console.cvars == {}


@pytest.mark.asyncio
async def test_switching_the_feature_off_switches_the_engine_off(console):
    """The classic only ever wrote `bot_enable 1`, so turning the feature off in the config and
    reloading left the engine's bot subsystem running; only the count went to zero."""
    console.game.map_name = "ut4_abbey"
    plugin = _plugin(console, botsupport_enable=True, botsupport_maps="ut4_abbey")
    assert console.cvars["bot_enable"] == "1"
    assert console.cvars["bot_minplayers"] == "4"

    plugin.settings["botsupport_enable"] = False
    plugin._apply_bot_settings()

    assert console.cvars["bot_enable"] == "0"
    assert console.cvars["bot_minplayers"] == "0"


@pytest.mark.asyncio
async def test_the_skill_is_not_written_for_a_feature_that_is_off(console):
    """The classic wrote `g_spskill` on every config load, whether or not it was running bots."""
    _plugin(console, botsupport_enable=False, botsupport_skill=2)

    assert "g_spskill" not in console.cvars


@pytest.mark.asyncio
async def test_bots_fill_a_map_they_are_allowed_on(console):
    console.game.map_name = "ut4_abbey"
    _plugin(
        console,
        botsupport_enable=True,
        botsupport_maps="ut4_abbey, ut4_algiers",
        botsupport_min_players=6,
    )

    assert console.cvars["bot_minplayers"] == "6"
    assert console.cvars["g_spskill"] == "4"


@pytest.mark.asyncio
async def test_bots_stay_off_on_a_map_they_are_not_allowed_on(console):
    console.game.map_name = "ut4_casa"
    _plugin(console, botsupport_enable=True, botsupport_maps="ut4_abbey")

    assert console.cvars.get("bot_minplayers") != "4"


@pytest.mark.asyncio
async def test_an_empty_map_list_means_no_map(console):
    """For a feature whose own documentation warns it may take the server down, "not configured" has
    to mean off rather than everywhere."""
    console.game.map_name = "ut4_abbey"
    _plugin(console, botsupport_enable=True, botsupport_maps="")

    assert console.cvars.get("bot_minplayers") != "4"


@pytest.mark.asyncio
async def test_the_map_list_is_not_case_sensitive(console):
    console.game.map_name = "ut4_abbey"
    _plugin(console, botsupport_enable=True, botsupport_maps="UT4_Abbey")

    assert console.cvars["bot_minplayers"] == "4"


@pytest.mark.asyncio
async def test_a_map_change_reconsiders(console):
    console.game.map_name = "ut4_abbey"
    _plugin(console, botsupport_enable=True, botsupport_maps="ut4_abbey")
    assert console.cvars["bot_minplayers"] == "4"

    console.game.map_name = "ut4_casa"
    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE))

    assert console.cvars["bot_minplayers"] == "0"


@pytest.mark.asyncio
async def test_the_bots_go_before_the_next_map_loads(console):
    console.game.map_name = "ut4_abbey"
    _plugin(console, botsupport_enable=True, botsupport_maps="ut4_abbey")

    await console.bus.publish(Event(EventType.GAME_EXIT))

    assert console.cvars["bot_minplayers"] == "0"


@pytest.mark.asyncio
async def test_a_reload_on_a_map_bots_are_not_allowed_on_turns_them_off(console):
    """The classic's version did nothing at all here and relied on whoever called it having turned
    the bots off first — which its own config reload did not do."""
    console.game.map_name = "ut4_abbey"
    plugin = _plugin(console, botsupport_enable=True, botsupport_maps="ut4_abbey")
    assert console.cvars["bot_minplayers"] == "4"

    console.game.map_name = "ut4_casa"
    plugin._apply_bot_settings()

    assert console.cvars["bot_minplayers"] == "0"


@pytest.mark.asyncio
async def test_the_skill_is_kept_inside_what_the_engine_accepts(console):
    console.game.map_name = "ut4_abbey"
    _plugin(console, botsupport_enable=True, botsupport_maps="ut4_abbey", botsupport_skill=99)

    assert console.cvars["g_spskill"] == "5"


# -- the headshot counter -----------------------------------------------------------------------


def _hit(console, attacker, victim, where):  # noqa: ANN001, ANN202
    return console.bus.publish(
        Event(
            EventType.CLIENT_DAMAGE,
            client=attacker,
            target=victim,
            data=KillData(weapon="UT_MOD_M4", damage=50, hit_location=where, means_of_death=""),
        )
    )


@pytest.mark.asyncio
async def test_a_headshot_is_announced(console):
    _plugin(console, headshot_counter=True)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    await _hit(console, bob, ann, "head")

    assert console.said_big == ["Bob: 1 headshot!"]


@pytest.mark.asyncio
async def test_the_announcement_reaches_the_players(console):
    """The classic's `broadcast: True` — its default — handed the announcement to `console.write`,
    which sends an **rcon command**. A line of prose is not a command, so on the setting an operator
    was most likely running, every headshot announcement went to the server and nowhere else."""
    _plugin(console, headshot_counter=True, headshot_announce="say")
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    await _hit(console, bob, ann, "head")

    assert console.said == ["Bob: 1 headshot!"]
    assert console.rcon_sent == []


@pytest.mark.asyncio
async def test_a_helmet_hit_counts_as_a_headshot(console):
    _plugin(console, headshot_counter=True)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    await _hit(console, bob, ann, "head")
    await _hit(console, bob, ann, "helmet")

    assert console.said_big[-1] == "Bob: 2 headshots!"


@pytest.mark.asyncio
async def test_the_engine_is_asked_to_log_where_shots_land(console):
    """Without `g_loghits` the server logs no hits at all, so a counter switched on without it
    counts nothing and says nothing about why."""
    _plugin(console, headshot_counter=True)

    assert console.cvars["g_loghits"] == "1"


@pytest.mark.asyncio
async def test_that_cvar_is_left_alone_while_the_counter_is_off(console):
    _plugin(console)

    assert "g_loghits" not in console.cvars


@pytest.mark.asyncio
async def test_a_shot_in_the_leg_is_not_announced(console):
    _plugin(console, headshot_counter=True)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    await _hit(console, bob, ann, "legs")

    assert console.said_big == []


@pytest.mark.asyncio
async def test_the_percentage_appears_once_there_is_enough_to_go_on(console):
    _plugin(console, headshot_counter=True, headshot_percent_min=10)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    for _ in range(6):
        await _hit(console, bob, ann, "head")

    assert console.said_big[-1] == "Bob: 6 headshots! (100%)"


@pytest.mark.asyncio
async def test_a_poor_percentage_is_not_advertised(console):
    _plugin(console, headshot_counter=True, headshot_percent_min=90)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    for _ in range(20):
        await _hit(console, bob, ann, "legs")

    for _ in range(6):
        await _hit(console, bob, ann, "head")

    assert console.said_big[-1] == "Bob: 6 headshots!"


@pytest.mark.asyncio
async def test_the_counter_can_be_left_counting_quietly(console):
    plugin = _plugin(console, headshot_counter=True, headshot_announce="off")
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    await _hit(console, bob, ann, "head")

    assert (console.said, console.said_big) == ([], [])
    assert plugin.hits(bob).head == 1


@pytest.mark.asyncio
async def test_nothing_is_counted_while_the_feature_is_off(console):
    plugin = _plugin(console)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    await _hit(console, bob, ann, "head")

    assert plugin.hits(bob).head == 0
    assert console.said_big == []


@pytest.mark.asyncio
async def test_a_newcomer_is_told_what_a_helmet_is_for(console):
    _plugin(console, headshot_counter=True, headshot_warn_helmet_after=3)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    ann.connections = 2

    for _ in range(3):
        await _hit(console, bob, ann, "head")

    assert _told(console, ann) == ["3 hits to the head - a helmet would have stopped some of those"]


@pytest.mark.asyncio
async def test_a_regular_is_not_told_what_a_helmet_is_for(console):
    """Somebody on their two hundredth visit knows. The classic's own rule and its number."""
    _plugin(console, headshot_counter=True, headshot_warn_helmet_after=3)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    ann.connections = 200

    for _ in range(3):
        await _hit(console, bob, ann, "head")

    assert _told(console, ann) == []


@pytest.mark.asyncio
async def test_the_advice_is_given_once(console):
    _plugin(console, headshot_counter=True, headshot_warn_helmet_after=2)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    ann.connections = 2

    for _ in range(6):
        await _hit(console, bob, ann, "head")

    assert len(_told(console, ann)) == 1


@pytest.mark.asyncio
async def test_kevlar_advice_counts_the_torso(console):
    _plugin(console, headshot_counter=True, headshot_warn_kevlar_after=2)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    ann.connections = 2

    for _ in range(2):
        await _hit(console, bob, ann, "torso")

    assert _told(console, ann) == ["2 hits to the torso - kevlar would keep you alive longer"]


@pytest.mark.asyncio
async def test_the_counts_start_again_on_a_new_map(console):
    plugin = _plugin(console, headshot_counter=True, headshot_reset="map")
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    await _hit(console, bob, ann, "head")

    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE))

    assert plugin.hits(bob).head == 0


@pytest.mark.asyncio
async def test_the_counts_can_be_kept_across_maps(console):
    plugin = _plugin(console, headshot_counter=True, headshot_reset="no")
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    await _hit(console, bob, ann, "head")

    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE))
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert plugin.hits(bob).head == 1


@pytest.mark.asyncio
async def test_a_hit_that_says_nothing_about_where_it_landed_is_not_counted(console):
    """Guessing "torso" would advise players to buy armour they do not need."""
    plugin = _plugin(console, headshot_counter=True)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)

    await _hit(console, bob, ann, "")

    assert plugin.hits(bob).landed == 0


# -- the rotation manager -----------------------------------------------------------------------


ROTATIONS = {
    "rotation_small": "small.txt",
    "rotation_medium": "medium.txt",
    "rotation_large": "large.txt",
}


def _rotator(console, **settings):  # noqa: ANN001, ANN202
    """A rotation manager past its quiet window."""
    plugin = _plugin(console, rotation_manager=True, **{**ROTATIONS, **settings})
    console.clock.advance(plugin.settings["teambalance_quiet_seconds"] + 1)
    return plugin


def _fill(console, count, team="red"):  # noqa: ANN001, ANN202
    return [_teamed(console, f"P{i}", team, cid=str(100 + i), bits=0) for i in range(count)]


@pytest.mark.asyncio
async def test_a_quiet_server_gets_the_small_maps(console):
    plugin = _rotator(console, rotation_small_max=4, rotation_hysteresis=2)
    _fill(console, 1)

    plugin.rotation_check()

    assert console.cvars["g_mapcycle"] == "small.txt"


@pytest.mark.asyncio
async def test_a_busy_server_gets_the_big_maps(console):
    plugin = _rotator(console, rotation_small_max=4, rotation_medium_max=12, rotation_hysteresis=2)
    _fill(console, 20)

    plugin.rotation_check()

    assert console.cvars["g_mapcycle"] == "large.txt"


@pytest.mark.asyncio
async def test_joining_a_player_never_shrinks_the_maps(console):
    """The classic's hysteresis took a `delta` saying whether the last thing was a join or a part,
    and had the join case backwards: with the switch at 4 and hysteresis 2, five players who had
    just been *joined* were put on the small rotation, while the same five after somebody *left*
    were on the medium one. Joining a player could shrink the maps."""
    plugin = _rotator(console, rotation_small_max=4, rotation_medium_max=12, rotation_hysteresis=2)
    _fill(console, 11)
    plugin.rotation_check()
    assert console.cvars["g_mapcycle"] == "medium.txt"

    _teamed(console, "Latecomer", "blue", cid="200", bits=0)
    await console.bus.publish(
        Event(EventType.CLIENT_AUTH, client=console.clients.get_by_cid("200"))
    )

    assert console.cvars["g_mapcycle"] == "medium.txt"


@pytest.mark.asyncio
async def test_hovering_on_a_switch_point_does_not_flap(console):
    plugin = _rotator(console, rotation_small_max=4, rotation_medium_max=12, rotation_hysteresis=2)
    players = _fill(console, 7)
    plugin.rotation_check()
    assert console.cvars["g_mapcycle"] == "medium.txt"

    for _ in range(2):
        console.clients.remove(players.pop().cid)
        plugin.rotation_check()

    # Five is inside the margin either side of the switch at four: the rotation stays where it is.
    assert console.cvars["g_mapcycle"] == "medium.txt"


@pytest.mark.asyncio
async def test_falling_clearly_below_the_switch_does_shrink_them(console):
    plugin = _rotator(console, rotation_small_max=4, rotation_medium_max=12, rotation_hysteresis=2)
    players = _fill(console, 7)
    plugin.rotation_check()
    assert console.cvars["g_mapcycle"] == "medium.txt"

    while players:
        console.clients.remove(players.pop().cid)
    plugin.rotation_check()

    assert console.cvars["g_mapcycle"] == "small.txt"


@pytest.mark.asyncio
async def test_spectators_and_bots_are_not_the_reason_for_a_bigger_map(console):
    """A spectator is not going to be running around it, and a bot is there because this plugin's
    own bot support put it there."""
    plugin = _rotator(console, rotation_small_max=4, rotation_medium_max=12, rotation_hysteresis=2)
    _fill(console, 2)
    for index in range(20):
        watcher = _teamed(console, f"W{index}", "spec", cid=str(300 + index), bits=0)
        watcher.is_bot = index % 2 == 0

    plugin.rotation_check()

    assert console.cvars["g_mapcycle"] == "small.txt"


@pytest.mark.asyncio
async def test_a_half_configured_rotation_does_nothing(console):
    """A band with no list would switch the server to an empty map cycle."""
    plugin = _plugin(console, rotation_manager=True, rotation_small="small.txt")
    console.clock.advance(120)
    _fill(console, 1)

    plugin.rotation_check()

    assert "g_mapcycle" not in console.cvars


@pytest.mark.asyncio
async def test_nothing_happens_while_the_feature_is_off(console):
    plugin = _plugin(console, **ROTATIONS)
    console.clock.advance(120)
    _fill(console, 20)

    plugin.rotation_check()

    assert "g_mapcycle" not in console.cvars


@pytest.mark.asyncio
async def test_the_count_is_not_taken_while_the_server_is_reconnecting(console):
    """Everybody reconnects at a map change, so a count taken then means nothing."""
    plugin = _rotator(console)
    _fill(console, 20)
    plugin.hold_off()

    plugin.rotation_check()

    assert "g_mapcycle" not in console.cvars


@pytest.mark.asyncio
async def test_the_rotation_is_only_written_when_it_changes(console):
    plugin = _rotator(console)
    _fill(console, 1)
    plugin.rotation_check()
    console.cvars.clear()

    plugin.rotation_check()

    assert console.cvars == {}


# -- match mode ---------------------------------------------------------------------------------


class _Switchable:
    """A plugin that can be switched off, as the console's registry hands them out."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled

    def is_enabled(self) -> bool:
        return self.enabled

    def enable(self) -> None:
        self.enabled = True

    def disable(self) -> None:
        self.enabled = False


@pytest.mark.asyncio
async def test_match_mode_tells_the_engine(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamatch on")

    assert console.cvars["g_matchmode"] == "1"
    assert console.said_big == ["match mode is on"]


@pytest.mark.asyncio
async def test_match_mode_works_without_the_pa(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!match off")

    assert console.cvars["g_matchmode"] == "0"


@pytest.mark.asyncio
async def test_a_match_mode_nobody_has_heard_of_is_refused(console):
    _plugin(console)
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamatch sideways")

    assert console.cvars == {}
    assert _told(console, admin) == ["!pamatch on|off"]


@pytest.mark.asyncio
async def test_the_named_plugins_are_switched_off(console):
    plugin = _plugin(console, matchmode_plugins="tk, spamcontrol")
    console.plugins["tk"] = _Switchable()
    console.plugins["spamcontrol"] = _Switchable()
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamatch on")

    assert not console.plugins["tk"].enabled
    assert not console.plugins["spamcontrol"].enabled
    assert plugin.match_mode


@pytest.mark.asyncio
async def test_only_the_ones_this_switched_off_come_back(console):
    """The classic re-enabled every plugin on its list when the match ended, so one an operator had
    deliberately switched off came back on because a match had happened."""
    _plugin(console, matchmode_plugins="tk, spamcontrol")
    console.plugins["tk"] = _Switchable()
    console.plugins["spamcontrol"] = _Switchable(enabled=False)  # off on purpose
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamatch on")
    await _run(console, admin, "!pamatch off")

    assert console.plugins["tk"].enabled
    assert not console.plugins["spamcontrol"].enabled


@pytest.mark.asyncio
async def test_a_plugin_that_is_not_loaded_is_not_a_problem(console):
    _plugin(console, matchmode_plugins="tk, nosuchplugin")
    console.plugins["tk"] = _Switchable()
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamatch on")

    assert not console.plugins["tk"].enabled


@pytest.mark.asyncio
async def test_the_match_config_is_run(console):
    _plugin(console, matchmode_on_config="match_on.cfg", matchmode_off_config="match_off.cfg")
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pamatch on")
    await _run(console, admin, "!pamatch off")

    assert [values["file"] for name, values in console.server_verbs_applied if name == "exec"] == [
        "match_on.cfg",
        "match_off.cfg",
    ]


@pytest.mark.asyncio
async def test_a_config_name_that_is_not_a_filename_is_refused(console, caplog):
    """`exec` takes a filename, and a "filename" with a semicolon in it is somebody running a second
    command — the same rule `!paexec` applies to what an admin types."""
    _plugin(console, matchmode_on_config="match.cfg; quit")
    admin = _join(console, "Admin", bits=64)

    with caplog.at_level("WARNING"):
        await _run(console, admin, "!pamatch on")

    assert console.server_verbs_applied == []
    assert "quit" in caplog.text


@pytest.mark.asyncio
async def test_a_gametype_command_runs_the_config_kept_for_it(console):
    _plugin(console, matchmode_configs={"ctf": "config_ctf.cfg"})
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!pactf")

    assert ("exec", {"file": "config_ctf.cfg"}) in console.server_verbs_applied


@pytest.mark.asyncio
async def test_a_gametype_with_no_config_runs_nothing(console):
    _plugin(console, matchmode_configs={"ctf": "config_ctf.cfg"})
    admin = _join(console, "Admin", bits=64)

    await _run(console, admin, "!patdm")

    assert console.server_verbs_applied == []


@pytest.mark.asyncio
async def test_everything_automatic_stands_down_during_a_match(console):
    """Each of these exists to keep a *public* server pleasant, and the people in a match have
    agreed the teams between themselves. The classic checked its flag separately in six places."""
    plugin = _balancer(console, speccheck_min_players=2)
    admin = _join(console, "Admin", bits=64)
    _sides(console, plugin, red=5, blue=1)
    _watcher(console, plugin, "Watcher")
    await _run(console, admin, "!pamatch on")
    console.verbs_applied.clear()
    console.warned.clear()

    plugin._teamcheck()
    plugin.skillcheck()
    plugin.namecheck()
    plugin.speccheck()

    assert console.verbs_applied == []
    assert console.warned == []


@pytest.mark.asyncio
async def test_the_headshot_counter_stands_down_too(console):
    plugin = _plugin(console, headshot_counter=True)
    admin = _join(console, "Admin", bits=64)
    bob = _teamed(console, "Bob", "red", bits=0)
    ann = _teamed(console, "Ann", "blue", bits=0)
    await _run(console, admin, "!pamatch on")
    console.said_big.clear()

    await _hit(console, bob, ann, "head")

    assert console.said_big == []
    assert plugin.hits(bob).head == 0


@pytest.mark.asyncio
async def test_the_bots_go_away_for_a_match(console):
    console.game.map_name = "ut4_abbey"
    _plugin(console, botsupport_enable=True, botsupport_maps="ut4_abbey")
    admin = _join(console, "Admin", bits=64)
    assert console.cvars["bot_minplayers"] == "4"

    await _run(console, admin, "!pamatch on")

    assert console.cvars["bot_minplayers"] == "0"


# -- the vote delay -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voting_is_stopped_at_the_start_of_a_round(console):
    """A vote called in the first thirty seconds is one called before anybody has seen the map."""
    _plugin(console, vote_delay_minutes=2)
    console.cvars["g_allowvote"] = "536870911"

    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars["g_allowvote"] == "0"


@pytest.mark.asyncio
async def test_voting_comes_back_when_the_delay_is_up(console):
    plugin = _plugin(console, vote_delay_minutes=2)
    console.cvars["g_allowvote"] = "536870911"
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    console.clock.advance(121)
    plugin._run_repeats()

    assert console.cvars["g_allowvote"] == "536870911"


@pytest.mark.asyncio
async def test_voting_stays_off_until_then(console):
    plugin = _plugin(console, vote_delay_minutes=2)
    console.cvars["g_allowvote"] = "536870911"
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    console.clock.advance(60)
    plugin._run_repeats()

    assert console.cvars["g_allowvote"] == "0"


@pytest.mark.asyncio
async def test_a_second_round_inside_the_delay_does_not_hand_voting_back_early(console):
    """The classic started a `threading.Timer` per round and cancelled none of them, so the first
    one handed voting back while the second round still expected it off."""
    plugin = _plugin(console, vote_delay_minutes=2)
    console.cvars["g_allowvote"] = "536870911"
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    console.clock.advance(90)
    await console.bus.publish(Event(EventType.GAME_ROUND_START))
    console.clock.advance(40)  # past the *first* round's deadline
    plugin._run_repeats()

    assert console.cvars["g_allowvote"] == "0"

    console.clock.advance(90)
    plugin._run_repeats()
    assert console.cvars["g_allowvote"] == "536870911"


@pytest.mark.asyncio
async def test_the_delay_puts_back_what_was_there(console):
    plugin = _plugin(console, vote_delay_minutes=1)
    console.cvars["g_allowvote"] = "12345"
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    console.clock.advance(61)
    plugin._run_repeats()

    assert console.cvars["g_allowvote"] == "12345"


@pytest.mark.asyncio
async def test_a_server_with_voting_already_off_is_left_alone(console):
    _plugin(console, vote_delay_minutes=2)
    console.cvars["g_allowvote"] = "0"

    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars["g_allowvote"] == "0"


@pytest.mark.asyncio
async def test_voting_is_not_handed_back_to_a_server_that_never_had_it(console):
    plugin = _plugin(console, vote_delay_minutes=2)
    console.cvars["g_allowvote"] = "0"
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    console.clock.advance(300)
    plugin._run_repeats()

    assert console.cvars["g_allowvote"] == "0"


@pytest.mark.asyncio
async def test_the_delay_can_be_switched_off(console):
    _plugin(console, vote_delay_minutes=0)
    console.cvars["g_allowvote"] = "536870911"

    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars["g_allowvote"] == "536870911"


@pytest.mark.asyncio
async def test_no_delay_during_a_match(console):
    """The people in a match called it; they do not need protecting from each other's votes."""
    _plugin(console, vote_delay_minutes=2)
    admin = _join(console, "Admin", bits=64)
    console.cvars["g_allowvote"] = "536870911"
    await _run(console, admin, "!pamatch on")

    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars["g_allowvote"] == "536870911"


# -- through a real bot ----------------------------------------------------------------------------


def _real_bot(tmp_path, **settings):  # noqa: ANN001, ANN202
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
        server=ServerConfig(game="iourt42"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminurt")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = PoweradminurtPlugin(bot, {"settings": settings})
    bot.add_plugin(plugin, "poweradminurt")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()
    return bot, rcon, plugin


@pytest.mark.asyncio
async def test_a_real_urban_terror_switch_is_reversed(tmp_path):
    """End to end on real log lines, and the reason the parser changed.

    Urban Terror has no team-change line: the team is the `t` field of `ClientUserinfoChanged`, and
    this family published no `CLIENT_TEAM_CHANGE` at all — so nothing here had ever run against a
    live server, `!paforce … lock` included.
    """
    bot, rcon, _plugin = _real_bot(tmp_path)
    bot.game.gametype = "3"  # team deathmatch
    await bot.replay(
        [
            rf"ClientUserinfoChanged: {cid} n\P{cid}\t\{team}"
            for cid, team in (("1", "1"), ("2", "1"), ("3", "1"), ("4", "2"))
        ]
    )
    await bot.bus.drain()
    # Nothing was reversed while the bot was still filling its roster: that is the quiet window.
    assert not any(cmd.startswith("forceteam") for cmd in rcon.commands), rcon.commands
    bot.clock.advance(61)

    # A fourth red arrives — red 4, blue 1, and it is this switch that made it uneven.
    await bot.replay([r"ClientUserinfoChanged: 5 n\P5\t\1"])
    await bot.bus.drain()

    assert any(cmd.startswith("forceteam 5 blue") for cmd in rcon.commands), rcon.commands
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_real_urban_terror_lock_holds(tmp_path):
    """`!paforce … lock` on real log lines. It had never once held anybody on this family."""
    bot, rcon, plugin = _real_bot(tmp_path, teambalance_on_team_change=False)
    await bot.replay([r"ClientUserinfoChanged: 1 n\Bob\t\2"])
    await bot.bus.drain()
    bob = bot.clients.get_by_cid("1")
    assert bob is not None
    assert bob.team == "blue"
    bob.set_var(plugin, "locked_to", "blue")
    rcon.commands.clear()

    await bot.replay([r"ClientUserinfoChanged: 1 n\Bob\t\1"])  # switched to red
    await bot.bus.drain()

    assert any(cmd.startswith("forceteam 1 blue") for cmd in rcon.commands), rcon.commands
    bot.storage.close()


# -- the name checker ---------------------------------------------------------------------------


def _warned_names(console):  # noqa: ANN001, ANN202
    return sorted(c.name for c, _reason, _admin in console.warned)


@pytest.mark.asyncio
async def test_two_players_wearing_one_name_are_both_warned(console):
    """Which of them took it from the other is not knowable from here."""
    plugin = _balancer(console)
    _teamed(console, "Bob", "red", bits=0)
    _teamed(console, "Bob", "blue", bits=0)

    plugin.namecheck()

    assert _warned_names(console) == ["Bob", "Bob"]


@pytest.mark.asyncio
async def test_a_name_that_only_differs_by_its_colours_is_the_same_name(console):
    """The classic compared the raw strings, so `^1Bob` and `^2Bob` — identical on the scoreboard —
    were not duplicates of each other, which is the exact case the check exists for."""
    plugin = _balancer(console)
    _teamed(console, "^1Bob", "red", bits=0)
    _teamed(console, "^2Bob", "blue", bits=0)

    plugin.namecheck()

    assert len(console.warned) == 2


@pytest.mark.asyncio
async def test_two_different_names_are_left_alone(console):
    plugin = _balancer(console)
    _teamed(console, "Bob", "red", bits=0)
    _teamed(console, "Ann", "blue", bits=0)

    plugin.namecheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_the_default_nickname_is_a_forbidden_name(console):
    plugin = _balancer(console)
    _teamed(console, "New UrT Player", "red", bits=0)

    plugin.namecheck()

    assert _warned_names(console) == ["New UrT Player"]
    assert console.warned[0][1] == "that name is not allowed here"


@pytest.mark.asyncio
async def test_a_forbidden_name_in_colours_is_still_forbidden(console):
    """The classic called `stripColors` on its own constant, which has no colours in it, rather than
    on the player's name — so this walked straight past."""
    plugin = _balancer(console)
    _teamed(console, "^1New ^7UrT ^1Player", "red", bits=0)

    plugin.namecheck()

    assert len(console.warned) == 1


@pytest.mark.asyncio
async def test_the_word_admin_commands_use_for_everybody_is_forbidden(console):
    plugin = _balancer(console)
    _teamed(console, "all", "red", bits=0)

    plugin.namecheck()

    assert _warned_names(console) == ["all"]


@pytest.mark.asyncio
async def test_the_forbidden_list_is_a_list(console):
    """The classic had a setting called `checkbadnames` that turned on one hard-coded word."""
    plugin = _balancer(console, namecheck_forbidden_names="admin, owner")
    _teamed(console, "Owner", "red", bits=0)
    _teamed(console, "New UrT Player", "blue", bits=0)

    plugin.namecheck()

    assert _warned_names(console) == ["Owner"]


@pytest.mark.asyncio
async def test_an_admin_is_not_checked(console):
    plugin = _balancer(console, namecheck_max_level=20)
    _teamed(console, "all", "red", bits=64)

    plugin.namecheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_bots_sharing_a_name_are_not_warned_about_each_other(console):
    """They cannot read a warning, and several bots on one server routinely wear one name — the
    classic warned every one of them, on every sweep, forever."""
    plugin = _balancer(console)
    for index in range(3):
        bot = _teamed(console, "UrT Bot", "red", cid=str(90 + index), bits=0)
        bot.is_bot = True

    plugin.namecheck()

    assert console.warned == []


@pytest.mark.asyncio
async def test_the_sweep_stands_down_in_the_quiet_window(console):
    plugin = _balancer(console)
    _teamed(console, "Bob", "red", bits=0)
    _teamed(console, "Bob", "blue", bits=0)
    plugin.hold_off()

    plugin.namecheck()

    assert console.warned == []


# -- changing name over and over ------------------------------------------------------------------


async def _rename(console, client, name):  # noqa: ANN001, ANN202
    client.name = name
    await console.bus.publish(Event(EventType.CLIENT_NAME_CHANGE, client=client, data=name))


@pytest.mark.asyncio
async def test_a_player_who_will_not_settle_on_a_name_is_removed(console):
    _balancer(console, namecheck_max_changes=3)
    bob = _teamed(console, "Bob", "red", bits=0)

    for index in range(5):
        await _rename(console, bob, f"Bob{index}")

    # Once, not once per rename after the limit: whether they have actually gone is the server's
    # business, and kicking them again in the meantime helps nobody.
    assert [who for who, _reason, _admin in console.kicked] == [bob]


@pytest.mark.asyncio
async def test_a_few_changes_are_allowed(console):
    _balancer(console, namecheck_max_changes=7)
    bob = _teamed(console, "Bob", "red", bits=0)

    await _rename(console, bob, "Bobby")
    await _rename(console, bob, "Robert")

    assert console.kicked == []


@pytest.mark.asyncio
async def test_they_are_told_how_many_are_left(console):
    _balancer(console, namecheck_max_changes=4)
    bob = _teamed(console, "Bob", "red", bits=0)

    await _rename(console, bob, "Bobby")
    await _rename(console, bob, "Robert")

    assert _told(console, bob) == [
        "3 more name changes allowed on this map",
        "2 more name changes allowed on this map",
    ]


@pytest.mark.asyncio
async def test_the_engine_renaming_a_reconnecting_player_does_not_count(console):
    """Urban Terror appends `_<slot>` to a name already in use, so somebody who drops and reconnects
    comes back as `Bob_3`. That is the engine renaming them, not them renaming themselves."""
    _balancer(console, namecheck_max_changes=1)
    bob = _teamed(console, "Bob", "red", cid="3", bits=0)

    await _rename(console, bob, "Bob")
    await _rename(console, bob, "Bob_3")
    await _rename(console, bob, "Bob")

    assert console.kicked == []


@pytest.mark.asyncio
async def test_the_count_starts_again_on_a_new_map(console):
    _balancer(console, namecheck_max_changes=2)
    bob = _teamed(console, "Bob", "red", bits=0)
    await _rename(console, bob, "Bobby")
    await _rename(console, bob, "Robert")

    await console.bus.publish(Event(EventType.GAME_EXIT))
    await _rename(console, bob, "Rob")
    await _rename(console, bob, "Bobby")

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_admin_may_rename_as_often_as_they_like(console):
    """The classic hard-coded level 9 for this, which is not a group anybody has."""
    _balancer(console, namecheck_max_changes=1)
    boss = _teamed(console, "Boss", "red", bits=64)

    for index in range(5):
        await _rename(console, boss, f"Boss{index}")

    assert console.kicked == []


@pytest.mark.asyncio
async def test_the_limit_can_be_switched_off(console):
    _balancer(console, namecheck_max_changes=0)
    bob = _teamed(console, "Bob", "red", bits=0)

    for index in range(10):
        await _rename(console, bob, f"Bob{index}")

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_real_urban_terror_rename_is_counted(tmp_path):
    """`CLIENT_NAME_CHANGE` had the same problem the team change did: only the Source parser ever
    published it, so on this family `censor` could not catch somebody who connected with a clean
    name and then changed it, and `nickreg` could not catch somebody putting on an admin's name
    mid-session."""
    bot, rcon, plugin = _real_bot(tmp_path, namecheck_max_changes=1)
    await bot.replay([r"ClientUserinfoChanged: 1 n\Bob\t\1"])
    await bot.bus.drain()
    bob = bot.clients.get_by_cid("1")
    assert bob is not None
    rcon.commands.clear()

    await bot.replay([r"ClientUserinfoChanged: 1 n\Robert\t\1"])
    await bot.replay([r"ClientUserinfoChanged: 1 n\Roberto\t\1"])
    await bot.bus.drain()

    assert any("kick" in cmd.lower() for cmd in rcon.commands), rcon.commands
    bot.storage.close()
