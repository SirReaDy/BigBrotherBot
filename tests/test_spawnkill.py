"""The `spawnkill` plugin — shooting somebody who cannot fight back yet.

The classic's captured tests are thorough about the penalties and the two bypasses (a high level, and
a victim with no recorded spawn), and those are kept. What they never touched is the team-kill handler,
which is where the fault is: it applied no penalty at all and did not check the delay, so team
spawn-killing was free while every team kill of anybody who had ever spawned was announced as one.
"""

from __future__ import annotations

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client, PenaltyType
from b3.plugins.admin import AdminPlugin
from b3.plugins.spawnkill import SpawnkillPlugin


def _plugin(console, hit=None, kill=None):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    config: dict[str, object] = {}
    if hit is not None:
        config["hit"] = hit
    if kill is not None:
        config["kill"] = kill
    plugin = SpawnkillPlugin(console, config)
    plugin.start()
    return plugin


def _join(console, name, cid=None, bits=0, team="red"):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), group_bits=bits
    )
    client.team = team
    console.clients.add(client)
    return client


async def _spawn(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_SPAWN, client=client))


async def _event(console, event_type, killer, victim):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(event_type, client=killer, target=victim, data=None))


def _warnings(console, client):  # noqa: ANN001, ANN202
    return [reason for who, reason, _admin in console.warned if who is client]


# -- the two windows -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_killing_somebody_who_just_spawned_is_punished(console):
    _plugin(console, kill={"delay": 3, "penalty": "warn", "duration": 5})
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_KILL, bob, ann)

    assert _warnings(console, bob) == ["spawn-killing is not allowed here"]


@pytest.mark.asyncio
async def test_killing_them_after_the_window_is_not(console):
    _plugin(console, kill={"delay": 3})
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")
    await _spawn(console, ann)
    console.clock.advance(3)

    await _event(console, EventType.CLIENT_KILL, bob, ann)

    assert console.warned == []


@pytest.mark.asyncio
async def test_shooting_them_has_its_own_shorter_window(console):
    _plugin(console, hit={"delay": 2, "reason": "let them get up first"})
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")
    await _spawn(console, ann)
    console.clock.advance(1)

    await _event(console, EventType.CLIENT_DAMAGE, bob, ann)

    assert _warnings(console, bob) == ["let them get up first"]


@pytest.mark.asyncio
async def test_a_victim_who_has_never_been_seen_to_spawn_is_not_a_case(console):
    """What makes a bot started mid-round safe: it watched nobody spawn, so it accuses nobody."""
    _plugin(console)
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")

    await _event(console, EventType.CLIENT_KILL, bob, ann)

    assert console.warned == []


@pytest.mark.asyncio
async def test_a_high_level_player_is_exempt(console):
    _plugin(console, kill={"max_level": 40})
    admin, ann = _join(console, "Admin", bits=16), _join(console, "Ann", team="blue")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_KILL, admin, ann)

    assert console.warned == []


@pytest.mark.asyncio
async def test_dying_to_your_own_grenade_is_not_a_spawn_kill(console):
    _plugin(console)
    bob = _join(console, "Bob")
    await _spawn(console, bob)

    await _event(console, EventType.CLIENT_KILL, bob, bob)

    assert console.warned == []


# -- the team-kill fault -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_team_spawn_kill_is_punished_like_any_other(console):
    """The classic's team handler applied no penalty at all, so this was free."""
    _plugin(console, kill={"delay": 3})
    bob, ann = _join(console, "Bob"), _join(console, "Ann")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_KILL_TEAM, bob, ann)

    assert _warnings(console, bob) == ["spawn-killing is not allowed here"]


@pytest.mark.asyncio
async def test_a_team_kill_outside_the_window_is_not_a_spawn_kill(console):
    """The classic's team handler never looked at the delay: it treated any team kill of anybody who
    had spawned at some point in the map as a spawn-kill."""
    _plugin(console, kill={"delay": 3})
    bob, ann = _join(console, "Bob"), _join(console, "Ann")
    await _spawn(console, ann)
    console.clock.advance(60)

    await _event(console, EventType.CLIENT_KILL_TEAM, bob, ann)

    assert console.warned == []


@pytest.mark.asyncio
async def test_shooting_a_spawning_teammate_counts(console):
    _plugin(console, hit={"delay": 2})
    bob, ann = _join(console, "Bob"), _join(console, "Ann")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_DAMAGE_TEAM, bob, ann)

    assert len(console.warned) == 1


@pytest.mark.asyncio
async def test_a_gib_is_a_kill(console):
    """Enemy Territory reports most kills as gibs, and the classic watched only `EVT_CLIENT_KILL`."""
    _plugin(console, kill={"delay": 3})
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_GIB, bob, ann)

    assert len(console.warned) == 1


# -- the penalties -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_kick_penalty(console):
    _plugin(console, kill={"penalty": "kick", "reason": "no spawn killing"})
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_KILL, bob, ann)

    assert [(c.name, r) for c, r, _a in console.kicked] == [("Bob", "no spawn killing")]


@pytest.mark.asyncio
async def test_a_tempban_penalty_uses_the_duration(console):
    _plugin(console, kill={"penalty": "tempban", "duration": 30})
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_KILL, bob, ann)

    assert [(c.name, m) for c, m, _r, _a in console.tempbanned] == [("Bob", 30)]


@pytest.mark.asyncio
async def test_a_warning_is_recorded_with_its_lifetime(console):
    _plugin(console, kill={"penalty": "warn", "duration": 5})
    bob, ann = _join(console, "Bob"), _join(console, "Ann", team="blue")
    await _spawn(console, ann)

    await _event(console, EventType.CLIENT_KILL, bob, ann)

    penalties = console.storage.get_active_penalties(bob.id, PenaltyType.WARNING)
    assert len(penalties) == 1
    assert penalties[0].duration == 5


def test_an_engine_verb_penalty_is_refused_by_name(console):
    """`slap`, `nuke` and `kill` were `inflictCustomPenalty` calls — verbs this bot has no seam for.

    Configuring one falls back to the default rather than silently doing nothing at all, which is what
    a plugin that accepted the setting and had no way to carry it out would do.
    """
    plugin = _plugin(console, kill={"penalty": "slap"}, hit={"penalty": "nuke"})

    assert plugin.windows["kill"].penalty == "warn"
    assert plugin.windows["hit"].penalty == "warn"


def test_an_unknown_penalty_falls_back_to_the_default(console):
    plugin = _plugin(console, kill={"penalty": "explode"})

    assert plugin.windows["kill"].penalty == "warn"


def test_the_defaults_are_the_classics(console):
    plugin = _plugin(console)

    assert plugin.windows["hit"].delay == 2
    assert plugin.windows["kill"].delay == 3
    assert plugin.windows["hit"].max_level == 40
    assert plugin.windows["kill"].duration == 5


def test_the_settings_are_per_instance(console):
    """The classic held them in a dict on the class and mutated it in place at load."""
    first = _plugin(console, kill={"delay": 9})
    second = _plugin(console)

    assert first.windows["kill"].delay == 9
    assert second.windows["kill"].delay == 3


def test_a_negative_delay_is_refused(console):
    assert _plugin(console, kill={"delay": -5}).windows["kill"].delay == 0


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_urban_terror_spawn_lines_drive_it(tmp_path):
    """`ClientSpawn:` then a `Kill:` a second later, on a real bot.

    The classic declared this plugin Urban-Terror-only; what it actually needs is a spawn event, which
    is why nothing here mentions a game.
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
        server=ServerConfig(game="iourt42"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="spawnkill")],
    )
    clock = FakeClock()
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=clock)
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = SpawnkillPlugin(bot, {"kill": {"delay": 3, "penalty": "kick"}})
    bot.add_plugin(plugin, "spawnkill")
    bot.start()
    admin.start()
    plugin.start()

    bob, ann = "b" * 32, "a" * 32
    await bot.replay(
        [
            f"ClientUserinfo: 1 \\name\\Bob\\cl_guid\\{bob}",
            f"ClientUserinfo: 2 \\name\\Ann\\cl_guid\\{ann}",
        ]
    )
    await bot.bus.drain()
    for cid, team in (("1", "red"), ("2", "blue")):
        client = bot.clients.get_by_cid(cid)
        assert client is not None
        client.team = team
    rcon.commands.clear()

    await bot.replay(["ClientSpawn: 2"])
    await bot.bus.drain()
    clock.advance(1)
    await bot.replay(["Kill: 1 2 19: Bob killed Ann by UT_MOD_M4"])
    await bot.bus.drain()

    assert any("kick" in c.lower() for c in rcon.commands), rcon.commands
    bot.storage.close()
