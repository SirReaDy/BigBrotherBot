"""The `tk` plugin — team damage points, forgiving, and the ban at the end of it.

Written against the classic bot's own captured tests (`tests/plugins/tk/`), which is where the
numbers come from: a team kill by a guest is **200** points, `!forgiveinfo` says so, and three of
them in a row is a ban. Those tests are also what settled the shape of the escalation — announce
once, then ban if nobody forgives — which reading the plugin alone would have left ambiguous.

Two things the captures could not tell us, because the classic bot never met them:

* **Four families report no damage figure at all.** Source, Frostbite, Homefront and Ravaged carry
  the weapon and nothing else, deliberately. A plugin reading `event.data.damage` gets `None` there,
  so a team kill on half the supported titles would score zero.
* **`CLIENT_GIB_TEAM` exists here** and did not there, so gibbing a teammate on Enemy Territory was
  free.
"""

from __future__ import annotations

import pathlib

import logging

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client, PenaltyType
from b3.parsers.cod.parser import KillData
from b3.plugins.admin import AdminPlugin
from b3.plugins.tk import TkPlugin


# A Source kill payload: the weapon and whether it was a headshot, and no damage figure anywhere.
class SourceKill:
    def __init__(self, weapon: str = "ak47") -> None:
        self.weapon = weapon
        self.hit_location = ""


def _client(name="Bob", cid="2", id_=7, bits=0):  # noqa: ANN001, ANN202
    return Client(guid=name[0].upper() * 4, name=name, cid=cid, id=id_, group_bits=bits)


def _tk(console, **settings):  # noqa: ANN001, ANN202
    """The plugin, plus the admin plugin it asks for warning keywords."""
    admin = AdminPlugin(
        console,
        # Both spellings, as the shipped admin config carries them: this bot's `spawnfire`, and
        # `sfire` aliased to it for a config carried over from the classic bot.
        {"warn_reasons": {"spawnfire": "3h, do not shoot at spawn", "sfire": "/spawnfire"}},
    )
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = TkPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


def _players(console, *names):  # noqa: ANN001, ANN202
    """Connect players in slots 1..n, all on the same team, all guests."""
    made = []
    for index, name in enumerate(names, start=1):
        client = _client(name=name, cid=str(index), id_=index)
        client.team = "red"
        console.clients.add(client)
        made.append(client)
    return made


async def _kill(console, attacker, victim, damage=100):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(
            EventType.CLIENT_KILL_TEAM,
            client=attacker,
            target=victim,
            data=KillData(
                weapon="ak47", damage=damage, hit_location="", means_of_death="MOD_RIFLE"
            ),
        )
    )


async def _damage(console, attacker, victim, damage=5):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(
            EventType.CLIENT_DAMAGE_TEAM,
            client=attacker,
            target=victim,
            data=KillData(
                weapon="ak47", damage=damage, hit_location="", means_of_death="MOD_RIFLE"
            ),
        )
    )


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


# -- the arithmetic the captures pinned down -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_team_kill_by_a_guest_costs_two_hundred_points(console):
    """The captured figure: 100 damage times a guest's kill multiplier of 2."""
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await _kill(console, joe, mike)

    assert plugin.points(joe) == 200
    assert plugin.points(mike) == 0  # the victim owes nothing


@pytest.mark.asyncio
async def test_the_points_are_held_by_the_victim_because_only_they_can_clear_them(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await _damage(console, joe, mike, damage=40)

    assert plugin.info(mike).attackers == {"1": 40}
    assert plugin.info(joe).attacked == {"2"}


@pytest.mark.asyncio
async def test_ordinary_team_damage_warns_nobody(console):
    """Five glancing hits are not a team-killer; the threshold is a kill's worth of damage."""
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    for _ in range(5):
        await _damage(console, joe, mike, damage=5)

    assert plugin.points(joe) == 25
    assert console.warned == []


@pytest.mark.asyncio
async def test_a_team_kill_warns_the_attacker_and_tells_the_victim_they_can_forgive(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await _kill(console, joe, mike)

    assert [c.name for c, _reason, _admin in console.warned] == ["Joe"]
    assert any("!fp" in text for client, text in console.told if client is mike)
    assert plugin.points(joe) == 200


@pytest.mark.asyncio
async def test_an_attacker_is_not_warned_again_for_three_minutes(console):
    """Without this a player in a crossfire with a teammate collects a warning per bullet."""
    _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await _kill(console, joe, mike)
    await _kill(console, joe, mike)
    assert len(console.warned) == 1

    console.clock.advance(181)
    await _kill(console, joe, mike)
    assert len(console.warned) == 2


@pytest.mark.asyncio
async def test_an_admin_is_scored_but_not_lectured(console):
    """`warn_level` is about the telling-off, not the tracking: an admin still collects points."""
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    joe.group_bits = 16  # admin, level 40

    await _kill(console, joe, mike)

    assert plugin.points(joe) == 75  # a level-40 kill multiplier of 0.75
    assert console.warned == []


@pytest.mark.asyncio
async def test_a_player_above_the_table_is_exempt_entirely(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    joe.group_bits = 128  # superadmin, level 100 — above the top entry, which is 40

    await _kill(console, joe, mike)

    assert plugin.points(joe) == 0


# -- the payloads that carry no damage figure ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_kill_on_an_engine_that_reports_no_damage_still_scores(console):
    """Source, Frostbite, Homefront and Ravaged state no damage figure, on purpose.

    Their parsers refuse to invent one, so the plugin has to supply the only number a kill can
    honestly be scored as. Reading `event.data.damage` and trusting it would score every team kill
    on four of the nine families as zero — silently, which is this project's most-repeated fault.
    """
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await console.bus.publish(
        Event(EventType.CLIENT_KILL_TEAM, client=joe, target=mike, data=SourceKill())
    )

    assert plugin.points(joe) == 200


@pytest.mark.asyncio
async def test_team_damage_with_no_figure_is_not_guessed_at(console):
    """A *kill* is 100 damage. A hit of unknown size is not, so it scores nothing rather than 100."""
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await console.bus.publish(
        Event(EventType.CLIENT_DAMAGE_TEAM, client=joe, target=mike, data=SourceKill())
    )

    assert plugin.points(joe) == 0


@pytest.mark.asyncio
async def test_gibbing_a_teammate_counts_as_killing_one(console):
    """Enemy Territory tells a gib from a kill; the classic tk plugin never saw that event."""
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await console.bus.publish(
        Event(
            EventType.CLIENT_GIB_TEAM,
            client=joe,
            target=mike,
            data=KillData(weapon="mp40", damage=100, hit_location="", means_of_death="MOD_MP40"),
        )
    )

    assert plugin.points(joe) == 200


# -- firing at spawn -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shooting_a_teammate_at_spawn_costs_triple(console):
    plugin = _tk(console, round_grace=10)
    joe, mike = _players(console, "Joe", "Mike")
    console.game.start_round(console.clock.now())

    await _damage(console, joe, mike, damage=20)

    assert plugin.points(joe) == 60  # 20 damage, ×1 for a guest, ×3 for the spawn


@pytest.mark.asyncio
async def test_the_spawn_warning_is_the_operators_own_keyword(console):
    """`spawnfire` is a `warn_reasons` key, not a sentence. Warning with the literal word would be
    telling the player off in a language only the config file speaks."""
    _tk(console, round_grace=10)
    joe, mike = _players(console, "Joe", "Mike")
    console.game.start_round(console.clock.now())

    await _kill(console, joe, mike)

    assert [reason for _c, reason, _a in console.warned] == ["do not shoot at spawn"]
    assert console.storage.penalties[-1].duration == 180  # the 3h the keyword names


@pytest.mark.asyncio
async def test_the_grace_period_ends_with_the_round_it_belongs_to(console):
    plugin = _tk(console, round_grace=10)
    joe, mike = _players(console, "Joe", "Mike")
    console.game.start_round(console.clock.now())
    console.clock.advance(11)

    await _damage(console, joe, mike, damage=20)

    assert plugin.points(joe) == 20  # no multiplier: the round is under way


# -- gametypes with no teams ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deathmatch_has_no_team_damage_to_punish(console):
    """Several engines report a team for everybody whatever the gametype, so without this every
    kill in a free-for-all is a team kill and the server empties itself."""
    plugin = _tk(console, round_grace=0)
    console.game.gametype = "dm"
    joe, mike = _players(console, "Joe", "Mike")

    await _kill(console, joe, mike)

    assert plugin.points(joe) == 0


# -- forgiving -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forgiving_clears_the_attackers_points(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)

    await _run(console, mike, "!forgive")

    assert plugin.points(joe) == 0
    assert any("has forgiven" in text for _client, text in console.told)


@pytest.mark.asyncio
async def test_forgiving_lifts_the_warning_it_was_issued_for(console):
    """Otherwise the warning goes on counting towards the admin plugin's own escalation — the act
    was forgiven and the player is still on their way to being kicked for it."""
    _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)
    assert console.storage.get_active_penalties(joe.require_id(), PenaltyType.WARNING)

    await _run(console, mike, "!forgive")

    assert console.storage.get_active_penalties(joe.require_id(), PenaltyType.WARNING) == []


@pytest.mark.asyncio
async def test_with_several_attackers_forgive_lists_them_rather_than_guessing(console):
    plugin = _tk(console, round_grace=0)
    joe, mike, bill = _players(console, "Joe", "Mike", "Bill")
    await _damage(console, joe, bill, damage=14)
    await _damage(console, mike, bill, damage=84)

    await _run(console, bill, "!forgive")

    listed = [text for _client, text in console.told if "forgive who?" in text]
    assert listed and "[1] Joe [14]" in listed[-1] and "[2] Mike [84]" in listed[-1]
    assert plugin.points(joe) == 14  # nothing was cleared


@pytest.mark.asyncio
async def test_forgiveprev_forgives_only_the_last_one(console):
    plugin = _tk(console, round_grace=0)
    joe, mike, bill = _players(console, "Joe", "Mike", "Bill")
    await _damage(console, joe, bill, damage=14)
    await _damage(console, mike, bill, damage=84)

    await _run(console, bill, "!fp")

    assert plugin.points(mike) == 0
    assert plugin.points(joe) == 14


@pytest.mark.asyncio
async def test_forgiveall_spares_anybody_you_hold_a_grudge_against(console):
    plugin = _tk(console, round_grace=0)
    joe, mike, bill = _players(console, "Joe", "Mike", "Bill")
    await _damage(console, joe, bill, damage=14)
    await _damage(console, mike, bill, damage=84)

    await _run(console, bill, "!grudge 2")  # Mike, by slot
    await _run(console, bill, "!forgiveall")

    assert plugin.points(joe) == 0
    assert plugin.points(mike) == 84


@pytest.mark.asyncio
async def test_forgivelist_names_the_slot_to_type(console):
    _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    await _damage(console, joe, mike, damage=14)

    await _run(console, mike, "!forgivelist")

    assert any("[1] Joe [14]" in text for _client, text in console.told)


@pytest.mark.asyncio
async def test_nobody_to_forgive_is_said_rather_than_nothing(console):
    _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await _run(console, mike, "!forgive")

    assert any("nobody to forgive" in text for _client, text in console.told)


@pytest.mark.asyncio
async def test_forgiveinfo_reports_both_directions(console):
    """The captured wording: what they owe, whom they hurt, and who owes them."""
    _tk(console, round_grace=0)
    joe, mike, bill = _players(console, "Joe", "Mike", "Bill")
    admin = _client(name="Su", cid="9", id_=9, bits=128)
    console.clients.add(admin)
    console.register_client("joe", joe)

    await _kill(console, joe, mike)
    await _damage(console, joe, bill, damage=6)
    await _damage(console, mike, joe, damage=27)

    await _run(console, admin, "!forgiveinfo joe")

    said = [text for client, text in console.told if client is admin][-1]
    assert "Joe has 206 TK point(s)" in said
    assert "attacked: Mike (200), Bill (6)" in said
    assert "attacked by: [2] Mike [27]" in said


@pytest.mark.asyncio
async def test_forgiveclear_wipes_somebodys_points(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    admin = _client(name="Su", cid="9", id_=9, bits=128)
    console.clients.add(admin)
    console.register_client("joe", joe)
    await _kill(console, joe, mike)

    await _run(console, admin, "!forgiveclear joe")

    assert plugin.points(joe) == 0
    assert plugin.info(mike).attackers == {}


@pytest.mark.asyncio
async def test_a_forgive_can_be_announced_to_everybody_instead(console):
    _tk(console, round_grace=0, private_messages=False)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)

    await _run(console, mike, "!forgive")

    assert any("has forgiven" in text for text in console.said)


# -- the ban at the end of it --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_second_team_kill_announces_it_once(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await _kill(console, joe, mike)
    await _kill(console, joe, mike)

    alerts = [text for text in console.said if "will be kicked" in text]
    assert len(alerts) == 1
    assert "!forgive 1" in alerts[0]  # the slot to type, since the victim has seconds to act
    assert console.tempbanned == []  # announced, not banned
    assert plugin.info(joe).ban_due  # ...but a deadline is running


@pytest.mark.asyncio
async def test_a_third_team_kill_bans_without_waiting(console):
    """Half again over the limit is past discussing: the classic banned outright at 150%."""
    _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")

    await _kill(console, joe, mike)
    await _kill(console, joe, mike)
    await _kill(console, joe, mike)

    assert [c.name for c, _m, _r, _a in console.tempbanned] == ["Joe"]


@pytest.mark.asyncio
async def test_the_ban_lands_when_the_forgive_window_closes(console):
    plugin = _tk(console, round_grace=0, forgive_grace=30)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)
    await _kill(console, joe, mike)

    console.clock.advance(31)
    plugin._tick()

    assert [(c.name, minutes) for c, minutes, _r, _a in console.tempbanned] == [("Joe", 2)]


@pytest.mark.asyncio
async def test_being_forgiven_inside_the_window_calls_the_ban_off(console):
    """The whole point of announcing it rather than banning: the victim gets to decide."""
    plugin = _tk(console, round_grace=0, forgive_grace=30)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)
    await _kill(console, joe, mike)

    await _run(console, mike, "!forgive")
    console.clock.advance(31)
    plugin._tick()

    assert console.tempbanned == []


@pytest.mark.asyncio
async def test_the_ban_is_as_long_as_the_number_of_teammates_hurt(console):
    plugin = _tk(console, round_grace=0, forgive_grace=30)
    joe, mike, bill = _players(console, "Joe", "Mike", "Bill")
    await _kill(console, joe, mike)
    await _kill(console, joe, bill)

    console.clock.advance(31)
    plugin._tick()

    assert [minutes for _c, minutes, _r, _a in console.tempbanned] == [4]  # 2 minutes × 2 victims


@pytest.mark.asyncio
async def test_a_level_that_is_never_banned_for_it_is_not_banned_for_it(console):
    """`ban_minutes: 0` on the mod entry is the classic's way of saying "score them, do not ban"."""
    plugin = _tk(console, round_grace=0, forgive_grace=30, max_points=100)
    joe, mike = _players(console, "Joe", "Mike")
    joe.group_bits = 8  # mod, level 20 — ban_minutes 0
    await _kill(console, joe, mike)

    console.clock.advance(31)
    plugin._tick()

    assert console.tempbanned == []


# -- points fading -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_round_ending_halves_everything(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)

    await console.bus.publish(Event(EventType.GAME_ROUND_END, data=""))

    assert plugin.points(joe) == 100


@pytest.mark.asyncio
async def test_a_round_end_and_a_map_change_together_halve_once(console):
    """Some engines report both. The classic chose between them with a per-title list of game
    names, which is a table that is wrong one patch after it is written."""
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)

    await console.bus.publish(Event(EventType.GAME_ROUND_END, data=""))
    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE, data="mp_vacant"))

    assert plugin.points(joe) == 100


@pytest.mark.asyncio
async def test_a_debt_that_halves_to_nothing_is_cleared_outright(console):
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    await _damage(console, joe, mike, damage=1)

    await console.bus.publish(Event(EventType.GAME_ROUND_END, data=""))

    assert plugin.info(mike).attackers == {}
    assert plugin.info(joe).attacked == set()


@pytest.mark.asyncio
async def test_halflife_halves_the_points_while_the_round_is_still_running(console):
    plugin = _tk(console, round_grace=0, halflife=60)
    joe, mike = _players(console, "Joe", "Mike")
    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))
    await _kill(console, joe, mike)

    console.clock.advance(61)
    plugin._tick()

    assert plugin.points(joe) == 100


@pytest.mark.asyncio
async def test_leaving_takes_both_the_debt_and_the_credit(console):
    """A victim who has left cannot forgive, so leaving their record behind would hold the attacker
    at the limit with nobody able to clear them."""
    plugin = _tk(console, round_grace=0)
    joe, mike = _players(console, "Joe", "Mike")
    await _kill(console, joe, mike)

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=joe, data="left"))

    assert plugin.info(mike).attackers == {}


# -- configuration -------------------------------------------------------------------------------


def test_the_levels_table_takes_group_keywords_as_well_as_numbers(console):
    plugin = TkPlugin(
        console,
        {"levels": {"guest": {"kill": 3, "damage": 2, "ban_minutes": 5}}},
    )
    plugin.on_load_config()

    assert plugin.levels[0].kill == 3.0
    assert plugin.max_level == 0  # nobody above a guest is scored, which is what the table says


def test_one_bad_level_entry_does_not_take_the_rest_with_it(console, caplog):
    """The classic raised on the first bad section and fell back to defaults for the whole table,
    so a single typo silently changed every level's multipliers."""
    plugin = TkPlugin(
        console,
        {
            "levels": {
                "guest": {"kill": 2, "damage": 1, "ban_minutes": 2},
                "wizard": {"kill": 9, "damage": 9, "ban_minutes": 9},
            }
        },
    )
    with caplog.at_level("ERROR"):
        plugin.on_load_config()

    assert set(plugin.levels) == {0}
    assert "wizard" in caplog.text


def test_an_unusable_levels_table_keeps_the_built_in_one(console, caplog):
    plugin = TkPlugin(console, {"levels": {"wizard": "very"}})
    with caplog.at_level("ERROR"):
        plugin.on_load_config()

    assert set(plugin.levels) == {0, 1, 2, 20, 40}


@pytest.mark.asyncio
async def test_grudges_can_be_turned_off_and_the_command_says_so(console):
    """The classic did not register the command at all, so `!grudge` answered "unknown command" —
    indistinguishable from a typo on a server where grudges simply are not used."""
    _tk(console, round_grace=0, grudge_enable=False)
    joe, mike = _players(console, "Joe", "Mike")
    await _damage(console, joe, mike, damage=14)

    await _run(console, mike, "!grudge 1")

    assert any("grudges are turned off" in text for _client, text in console.told)


def test_the_grudge_level_comes_from_the_config(console):
    _tk(console, grudge_level=20)

    assert console.command_registry.get("grudge").min_level == 20


# -- through a real bot, from real log lines ------------------------------------------------------
#
# The tests above drive the plugin through a fake console, which proves the arithmetic. This section
# proves the *wiring*: a Call of Duty team-kill line off the log, the warning reaching the real
# penalty table, `!forgive` typed in chat, and the warning being lifted in the database rather than
# in a dict. Every serious fault this project has found appeared here rather than in a unit test.


def _real_bot(tmp_path, **settings):  # noqa: ANN001, ANN202
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.runtime.bot import Bot

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="tk")],
    )

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            return ""

    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(
        bot, {"warn_reasons": {"spawnfire": "3h, do not shoot at spawn", "sfire": "/spawnfire"}}
    )
    bot.add_plugin(admin, "admin")
    tk = TkPlugin(bot, {"settings": {"round_grace": 0, **settings}})
    bot.add_plugin(tk, "tk")
    bot.start()
    admin.start()
    tk.start()
    rcon.commands.clear()
    return bot, rcon, tk


GJOE = "1" * 32
GMIKE = "2" * 32


@pytest.mark.asyncio
async def test_a_real_team_kill_line_is_scored_warned_and_forgiven(tmp_path):
    bot, rcon, tk = _real_bot(tmp_path)
    await bot.replay(
        [
            f"J;{GJOE};1;Joe",
            f"J;{GMIKE};2;Mike",
            # A CoD4 team kill: both players on `allies`, so the parser calls it one.
            f"K;{GMIKE};2;allies;Mike;{GJOE};1;allies;Joe;mp5_mp;100;MOD_RIFLE;chest",
        ]
    )
    joe = bot.clients.get_by_cid("1")

    assert tk.points(joe) == 200
    assert bot.storage.get_active_penalties(joe.require_id(), PenaltyType.WARNING)
    assert any("!fp" in c for c in rcon.commands)  # Mike was told he can forgive

    await bot.replay([f"say;{GMIKE};2;Mike;!forgive"])
    await bot.bus.drain()

    assert tk.points(joe) == 0
    # Lifted in the database, not only in the plugin: the admin plugin counts warnings from there,
    # so a forgiven team kill must stop counting towards a kick for collecting warnings.
    assert bot.storage.get_active_penalties(joe.require_id(), PenaltyType.WARNING) == []
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_ban_a_real_server_gets_is_a_tempban_command(tmp_path):
    """Three team kills with nobody forgiving: the ban has to reach the server, not just the table."""
    bot, rcon, tk = _real_bot(tmp_path)
    await bot.replay([f"J;{GJOE};1;Joe", f"J;{GMIKE};2;Mike"])
    kill = f"K;{GMIKE};2;allies;Mike;{GJOE};1;allies;Joe;mp5_mp;100;MOD_RIFLE;chest"
    await bot.replay([kill, kill, kill])
    await bot.bus.drain()

    joe = bot.clients.get_by_cid("1")
    assert bot.storage.get_active_penalties(joe.require_id(), PenaltyType.TEMPBAN)
    assert any("tempbanclient" in c or "banclient" in c for c in rcon.commands)
    bot.storage.close()


def test_the_shipped_defaults_agree_with_the_shipped_admin_table():
    """The two configs shipped disagreeing, and the symptom was a word in the game.

    `tk` kept the classic bot's default keyword (`sfire`); this bot's admin config spells the same
    entry `spawnfire`. Nothing failed — `_resolve_reason` falls back to the keyword — so a player
    who shot a teammate at spawn was warned with the bare word "sfire". A test rather than a
    comment, because the two files are edited by different hands at different times.
    """
    import yaml

    from b3.plugins.tk import DEFAULTS

    root = pathlib.Path(__file__).resolve().parent.parent
    admin_config = yaml.safe_load((root / "examples" / "plugin_admin.yaml").read_text("utf-8"))
    tk_config = yaml.safe_load((root / "examples" / "plugin_tk.yaml").read_text("utf-8"))
    reasons = admin_config["warn_reasons"]

    assert DEFAULTS["issue_warning"] in reasons, (
        "the plugin default must name a keyword that exists"
    )
    assert tk_config["settings"]["issue_warning"] in reasons, "and so must the example config"
    assert reasons["sfire"] == "/spawnfire", "the classic bot's spelling still resolves"


@pytest.mark.asyncio
async def test_a_keyword_that_is_not_in_the_table_is_said_out_loud(console, caplog):
    """Warning somebody with a bare config keyword is baffling in the game and silent in the log."""
    admin = AdminPlugin(console, {"warn_reasons": {"spawnfire": "3h, do not shoot at spawn"}})
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = TkPlugin(console, {"settings": {"round_grace": 10, "issue_warning": "nosuchkeyword"}})
    plugin.start()
    joe, mike = _players(console, "Joe", "Mike")
    console.game.start_round(console.clock.now())

    with caplog.at_level(logging.WARNING):
        await _kill(console, joe, mike)

    assert "not in the admin plugin's warn_reasons" in caplog.text
    assert [reason for _c, reason, _a in console.warned] == ["nosuchkeyword"]
