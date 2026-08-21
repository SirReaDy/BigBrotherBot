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


# -- slice 2: the match settings ------------------------------------------------------------------


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


# -- slice 3: moving players between teams -------------------------------------------------------


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


# -- slice 3b: the rest of the server commands ---------------------------------------------------


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
