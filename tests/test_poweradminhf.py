"""The `poweradminhf` plugin — Homefront's teams, match mode and player verbs.

No captured tests in the classic tree; the source and the shipped `.ini` are the description. Read
closely, the source says two features had never worked. `!paautobalance off` sent the server the
single word `false` — `'admin SetAutoBalance %s' % 'true' if on else 'false'` binds the `%` before the
conditional — and the "you unbalanced the teams" warning compared a player's *name* against a list of
Steam ids, so it never once fired.

The third is the one worth the port on its own: the whole match manager was a **Battlefield** one,
copied in without a word changed. Its nags and countdown send `('admin.yell', …)` and it finishes with
`('admin.restartMap',)` — Frostbite verbs, as tuples, to a parser whose `write` takes a string. So
`!pamatch on` announced a match and then everything after the announcement went nowhere.
"""

from __future__ import annotations

import json

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.core.matchmode import COUNTDOWN_FROM, NAG_SECONDS
from b3.domain.client import Client
from b3.parsers.homefront.profiles import HOMEFRONT
from b3.plugins.admin import AdminPlugin
from b3.plugins.poweradminhf import (
    ANNOUNCE_SECONDS,
    QUIET_SECONDS,
    PoweradminhfPlugin,
)

HF_SERVER_VERBS = {"cyclemap", "autobalance"}
HF_PLAYER_VERBS = {"kill", "switch_team", "spectate"}


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    # What a real Homefront console has, from the title's profile.
    console.server_verbs = set(HF_SERVER_VERBS)
    console.player_verbs = set(HF_PLAYER_VERBS)
    console.teams = dict(HOMEFRONT.teams)
    plugin = PoweradminhfPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    console.clock.advance(QUIET_SECONDS + 1)
    return plugin


def _client(console, name, team="red", bits=0, guid=None, id_=None):  # noqa: ANN001, ANN202
    client = Client(
        # On this engine the handle every verb takes is the Steam id, and `cid` is the name.
        guid=guid or f"7656119800000000{abs(hash(name)) % 10}",
        name=name,
        cid=name,
        id=id_ if id_ is not None else abs(hash(name)) % 10000,
        group_bits=bits,
        team=team,
    )
    console.clients.add(client)
    console.register_client(name, client)
    console.register_client(name.lower(), client)
    return client


def _boss(console):  # noqa: ANN001, ANN202
    return _client(console, "Boss", bits=128, id_=1)


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    told = [text for who, text in console.told if who is client]
    return told[-1] if told else ""


def _switched(console):  # noqa: ANN001, ANN202
    return [c.name for name, c, _ in console.verbs_applied if name == "switch_team"]


# -- the two commands the % operator broke ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_server_balancer_can_be_switched_off_as_well_as_on(console):
    """`'admin SetAutoBalance %s' % 'true' if on else 'false'` is
    `('admin SetAutoBalance %s' % 'true') if on else 'false'` — so `off` sent the server the single
    word `false`, and the command said nothing either way, so nobody could tell."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paautobalance on")
    assert console.server_verbs_applied == [("autobalance", {"state": "true"})]
    assert "on" in _last(console, boss)

    await _run(console, boss, "!paautobalance off")
    assert console.server_verbs_applied[-1] == ("autobalance", {"state": "false"})
    assert "off" in _last(console, boss)


@pytest.mark.asyncio
async def test_asking_whether_the_team_check_is_on_gets_a_sentence(console):
    """The same fault in the same shape: with the check off the player was sent the bare word `off`."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pateambalance")
    assert _last(console, boss) == "the automatic team check is off"

    await _run(console, boss, "!pateambalance on")
    await _run(console, boss, "!pateambalance")
    assert _last(console, boss) == "the automatic team check is on"


# -- the warning that could never fire -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_player_who_makes_the_teams_uneven_is_warned_and_moved(console):
    """The classic tested `client.name in biggestteam` against a list of `c.cid` — which on this
    engine is the Steam id, since a 2011 change moved every verb here to Steam ids and left this
    comparison behind. So it warned nobody and moved nobody, ever."""
    plugin = _plugin(console, teambalance=True)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")
    joiner = _client(console, "Joiner", team="blue")

    joiner.team = "red"
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, data="red", client=joiner))

    assert _switched(console) == ["Joiner"]
    assert "uneven" in _last(console, joiner)
    assert plugin.holding_off() is True


@pytest.mark.asyncio
async def test_the_teams_are_counted_by_side_and_not_by_handle(console):
    plugin = _plugin(console, teambalance=True)
    _client(console, "A", team="red")
    _client(console, "B", team="blue")
    _client(console, "W", team="spec")
    _client(console, "N", team="")  # not on a side yet

    assert plugin.team_counts() == {"red": 1, "blue": 1}


@pytest.mark.asyncio
async def test_the_balancer_moves_the_most_recent_joiners(console):
    plugin = _plugin(console, teambalance=True)
    boss = _boss(console)
    veteran = _client(console, "Veteran", team="red")
    plugin._stamp(veteran)
    console.clock.advance(600)
    newcomer = _client(console, "Newcomer", team="red")
    plugin._stamp(newcomer)
    _client(console, "Z", team="blue")

    await _run(console, boss, "!pateams")

    assert _switched(console) == ["Newcomer"]
    assert newcomer.team == "blue"
    assert veteran.team == "red"


@pytest.mark.asyncio
async def test_even_teams_are_reported_rather_than_shuffled(console):
    _plugin(console, teambalance=True)
    boss = _boss(console)  # red
    _client(console, "Z", team="blue")

    await _run(console, boss, "!pateams")

    assert _switched(console) == []
    assert "even" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_balancer_leaves_the_admins_where_they_are(console):
    plugin = _plugin(console, teambalance=True, no_balance_level=20)
    boss = _boss(console)
    mod = _client(console, "Mod", team="red", bits=8)
    plugin._stamp(mod)
    console.clock.advance(10)
    ordinary = _client(console, "Ordinary", team="red")
    plugin._stamp(ordinary)
    _client(console, "Z", team="blue")

    await _run(console, boss, "!pateams")

    assert _switched(console) == ["Ordinary"]


@pytest.mark.asyncio
async def test_the_check_stands_down_for_a_minute_after_a_round_starts(console):
    plugin = _plugin(console, teambalance=True)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")
    joiner = _client(console, "Joiner", team="red")

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={"mapname": "angel island"}))
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, data="red", client=joiner))

    assert _switched(console) == []
    console.clock.advance(QUIET_SECONDS + 1)
    assert plugin.holding_off() is False


@pytest.mark.asyncio
async def test_the_bots_own_move_is_not_read_as_a_player_switching_sides(console):
    plugin = _plugin(console, teambalance=True)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")
    moved = _client(console, "Moved", team="red")

    plugin.switch(moved)
    console.verbs_applied.clear()
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, data="blue", client=moved))

    assert _switched(console) == []


# -- players ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changing_a_players_team_names_no_team_because_the_verb_takes_none(console):
    """`admin forceteamswitch` swaps whoever is named and cannot be asked for a side."""
    _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob", team="red")

    await _run(console, boss, "!pachangeteam Bob")

    assert [(n, c.name, v) for n, c, v in console.verbs_applied] == [("switch_team", "Bob", {})]
    assert bob.team == "blue"


@pytest.mark.asyncio
async def test_a_player_can_be_sent_to_the_spectators(console):
    _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob", team="red")

    await _run(console, boss, "!paspectate Bob")

    assert [(n, c.name) for n, c, _ in console.verbs_applied] == [("spectate", "Bob")]
    assert bob.team == "spec"


@pytest.mark.asyncio
async def test_a_kill_is_announced_with_its_reason_after_it_is_sent(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob")

    await _run(console, boss, "!pakill Bob spawn camping")

    assert [(n, c.name) for n, c, _ in console.verbs_applied] == [("kill", "Bob")]
    assert console.said_big == ["Bob was killed by an admin: spawn camping"]


@pytest.mark.asyncio
async def test_an_admin_may_not_kill_a_senior_one(console):
    """The classic checked nothing before killing, moving or spectating anybody."""
    _plugin(console, no_level_check_level=100)
    admin = _client(console, "Admin", bits=32)  # fulladmin, allowed the command
    _client(console, "Senior", bits=64)

    await _run(console, admin, "!pakill Senior")

    assert console.verbs_applied == []
    assert "Senior" in _last(console, admin)


@pytest.mark.asyncio
async def test_a_steam_id_is_never_said_out_loud(console):
    """`sayLoudOrPM` in the classic, so `!!paident bob` put it in public chat — and its help offered
    an IP address this engine never reports."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob", guid="76561198000000123")

    await _run(console, boss, "&paident Bob")

    assert console.said == []
    assert "76561198000000123" in _last(console, boss)


# -- the map ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_next_map_is_announced_first_and_sent_from_the_scheduled_pass(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!panextmap")
    assert console.said == ["moving on to the next map"]
    assert console.server_verbs_applied == []

    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("cyclemap", {})]


@pytest.mark.asyncio
async def test_a_yell_is_the_engines_big_message(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!payell round starting")

    assert console.said_big == ["Boss: round starting"]


# -- match mode, which sent Battlefield commands ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_match_readies_up_counts_down_and_moves_the_map_on(console):
    """The classic's nags, countdown and finish were `('admin.yell', …)` and `('admin.restartMap',)` —
    Frostbite verbs, as tuples, on a Homefront server. None of it arrived."""
    plugin = _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob")

    await _run(console, boss, "!pamatch on")
    assert plugin.match_mode is True
    assert "type !ready" in console.said_big[-1]

    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()
    assert "waiting for" in console.said[-1]
    assert "we are waiting for you" in _last(console, bob)

    await _run(console, boss, "!ready")
    await _run(console, bob, "!ready")
    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()
    assert "counting down" in console.said[-1]

    for expected in range(COUNTDOWN_FROM, 0, -1):
        console.clock.advance(1.0)
        plugin._run_pending()
        assert console.said_big[-1] == f"match starting in {expected}"

    console.clock.advance(1.0)
    plugin._run_pending()

    assert console.said_big[-1] == "fight!"
    # This engine cannot restart a map at all — `admin nextmap` is the only thing it will do to the
    # rotation, so a match starts on the next map rather than on this one again.
    assert console.server_verbs_applied == [("cyclemap", {})]


@pytest.mark.asyncio
async def test_a_countdown_does_not_start_on_an_empty_server(console):
    plugin = _plugin(console)
    boss = _boss(console)
    await _run(console, boss, "!pamatch on")
    console.clients.remove(boss.cid)

    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()

    assert plugin.ready_up.counting_down() is False
    assert console.server_verbs_applied == []


@pytest.mark.asyncio
async def test_ready_outside_a_match_says_there_is_nothing_to_be_ready_for(console):
    _plugin(console)
    bob = _client(console, "Bob")

    await _run(console, bob, "!ready")

    assert "no match" in _last(console, bob)


@pytest.mark.asyncio
async def test_a_match_only_switches_back_on_the_plugins_it_switched_off(console):
    _plugin(console, match_plugins="spree, tk")

    class Fake:
        def __init__(self, on: bool) -> None:
            self.on = on

        def is_enabled(self) -> bool:
            return self.on

        def enable(self) -> None:
            self.on = True

        def disable(self) -> None:
            self.on = False

    spree, tk = Fake(True), Fake(False)
    console.plugins.update({"spree": spree, "tk": tk})
    boss = _boss(console)

    await _run(console, boss, "!pamatch on")
    assert (spree.on, tk.on) == (False, False)

    await _run(console, boss, "!pamatch off")
    assert (spree.on, tk.on) == (True, False)


@pytest.mark.asyncio
async def test_a_match_costs_the_server_nothing_when_it_ends(console):
    """`!pamatch on` set the team check's own flag to off and `!pamatch off` never set it back."""
    plugin = _plugin(console, teambalance=True)
    boss = _boss(console)

    await _run(console, boss, "!pamatch on")
    assert plugin.holding_off() is True

    await _run(console, boss, "!pamatch off")
    console.clock.advance(QUIET_SECONDS + 1)

    assert plugin.settings["teambalance"] is True
    assert plugin.holding_off() is False


@pytest.mark.asyncio
async def test_disabling_the_plugin_stops_a_countdown_nobody_can_see(console):
    plugin = _plugin(console)
    boss = _boss(console)
    await _run(console, boss, "!pamatch on")

    plugin.disable()

    assert plugin.ready_up.running() is False


# -- it is a Homefront plugin ----------------------------------------------------------------------


def test_the_loader_refuses_it_on_any_other_title(console):
    """It was filed with the Frostbite `poweradmin*` plugins for years, in this project's notes as
    well as by resemblance. It is not one of them."""
    from b3.config.schema import Config, PluginEntry, ServerConfig
    from b3.core.pluginmgr import load_plugins

    loaded = load_plugins(
        console,
        Config(
            server=ServerConfig(game="bfbc2"),
            plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminhf")],
        ),
    )
    plugin = next(item for item in loaded if item.name == "poweradminhf")

    assert plugin.enabled is False
    assert "does not support the 'bfbc2' parser" in plugin.reason


def test_every_verb_this_plugin_uses_is_one_the_title_declares():
    """And each of them names the **Steam id**, which is what `apply_verb` could not supply before."""
    assert HF_SERVER_VERBS <= set(HOMEFRONT.server_verbs)
    assert HF_PLAYER_VERBS <= set(HOMEFRONT.player_verbs)
    for template in HOMEFRONT.player_verbs.values():
        assert "%(guid)s" in template
    # No team argument anywhere: this engine's team verb swaps whoever is named.
    assert HOMEFRONT.player_verbs["switch_team"] == 'admin forceteamswitch "%(guid)s"'


# -- through a real bot ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_command_reaches_a_real_homefront_server(tmp_path):
    """Everything at once: a real `Bot` on the homefront profile, a pushed line from the engine, and
    the verb that goes back out — keyed on the Steam id, because on this title that is the handle."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.net.homefront import CHANNEL_CHATTER, CHANNEL_SERVER
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            return ""

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="homefront"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminhf")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = PoweradminhfPlugin(bot, None)
    bot.add_plugin(plugin, "poweradminhf")
    bot.start()
    admin.start()
    plugin.start()

    # This engine hands every message over as JSON `[channel, data]`: the same text means different
    # things on different channels, and chat arrives on its own.
    await bot.replay(
        [
            json.dumps([CHANNEL_SERVER, "LOGIN: Boss 76561198000000123"]),
            json.dumps([CHANNEL_SERVER, "LOGIN: Bob 76561198000000124"]),
        ]
    )
    await bot.bus.drain()
    bot.clients.get_by_cid("Boss").group_bits = 128
    rcon.commands.clear()

    await bot.replay([json.dumps([CHANNEL_CHATTER, "BROADCAST: Boss says: !pakill Bob camping"])])
    await bot.bus.drain()

    assert 'admin kill "76561198000000124"' in rcon.commands
    bot.storage.close()
