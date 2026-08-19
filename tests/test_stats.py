"""The `stats` plugin — a session's kills, deaths, damage, skill score and XP.

The figures come from the classic bot's captured tests (`tests/plugins/stats/`), which is the only
place the scoring formula is actually pinned down: two players on equal scores are worth **12.5** to
each other, a victim on double the killer's score is worth **20**, one on half is worth **8.75**, and
a team kill takes the same amount off the killer while adding nothing to their damage column.

Read those captures and two faults in the original are visible:

* `!topstats` ranked anybody the scoring code had *touched*, because "has a points variable" was the
  test for having played. Its own captured test asserts the victim of a team kill is top of the board
  with a perfect score, having done nothing but get shot.
* `show_awards` called the command with no client, and the empty-board branch then messaged that
  None — so a map on which nobody scored ended in a traceback.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.cod.parser import KillData
from b3.plugins.admin import AdminPlugin
from b3.plugins.stats import StatsPlugin


class SourceKill:
    """A Source kill payload: a weapon, and no damage figure anywhere."""

    def __init__(self, weapon: str = "ak47") -> None:
        self.weapon = weapon
        self.hit_location = ""


def _stats(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = StatsPlugin(console, {"settings": settings} if settings else None)
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
        console.register_client(name.lower(), client)
        made.append(client)
    return made


async def _kill(console, killer, victim, damage=100, team=False):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(
            EventType.CLIENT_KILL_TEAM if team else EventType.CLIENT_KILL,
            client=killer,
            target=victim,
            data=KillData(
                weapon="ak47", damage=damage, hit_location="chest", means_of_death="MOD_RIFLE"
            ),
        )
    )


async def _hit(console, attacker, victim, damage=25, team=False):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(
            EventType.CLIENT_DAMAGE_TEAM if team else EventType.CLIENT_DAMAGE,
            client=attacker,
            target=victim,
            data=KillData(
                weapon="ak47", damage=damage, hit_location="chest", means_of_death="MOD_RIFLE"
            ),
        )
    )


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last_told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client][-1]


# -- the formula the captures pin down -----------------------------------------------------------


def test_an_even_match_is_worth_twelve_and_a_half(console):
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")

    assert plugin.score(joe, mike) == 12.5


def test_killing_somebody_doing_better_is_worth_more(console):
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    plugin.stats(joe).points = 50
    plugin.stats(mike).points = 100

    assert plugin.score(joe, mike) == 20.0


def test_killing_somebody_doing_worse_is_worth_less(console):
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    plugin.stats(joe).points = 100
    plugin.stats(mike).points = 50

    assert plugin.score(joe, mike) == 8.75


def test_a_score_of_zero_is_treated_as_one(console):
    """Otherwise the ratio is either a division by zero or a windfall, depending on which side of it
    the flattened player is on. The captured test says 12.5, the same as an even match."""
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    plugin.stats(joe).points = 0
    plugin.stats(mike).points = 0

    assert plugin.score(joe, mike) == 12.5


# -- what a kill does ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_kill_moves_points_from_the_victim_to_the_killer(console):
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"

    await _kill(console, joe, mike)

    assert plugin.stats(joe).points == 112.5
    assert plugin.stats(mike).points == 87.5
    assert plugin.stats(joe).kills == 1
    assert plugin.stats(mike).deaths == 1
    assert plugin.stats(joe).damage_hit == 100


@pytest.mark.asyncio
async def test_a_team_kill_costs_the_killer_and_credits_them_with_no_damage(console):
    """Straight from the captures: after a team kill the killer's damage column is still 0, because
    damage to your own side is not a contribution to anything."""
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")

    await _kill(console, joe, mike, team=True)

    record = plugin.stats(joe)
    assert record.points == 87.5
    assert record.team_kills == 1
    assert record.damage_hit == 0
    assert record.damage_team_hit == 100
    assert plugin.stats(mike).deaths == 0  # a team kill is not scored against the victim


@pytest.mark.asyncio
async def test_a_hit_counts_for_both_sides_of_it(console):
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike", team="red")
    mike.team = "blue"

    await _hit(console, joe, mike, damage=25)

    assert (plugin.stats(joe).shots_hit, plugin.stats(joe).damage_hit) == (1, 25)
    assert (plugin.stats(mike).shots_got, plugin.stats(mike).damage_got) == (1, 25)


@pytest.mark.asyncio
async def test_damage_is_capped_at_a_kills_worth(console):
    """A single hit for 250 is an engine reporting overkill; the classic capped it at 100 and the
    skill arithmetic depends on that ceiling."""
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"

    await _hit(console, joe, mike, damage=250)

    assert plugin.stats(joe).damage_hit == 100


@pytest.mark.asyncio
async def test_a_kill_on_an_engine_that_reports_no_damage_still_counts_as_one(console):
    """Source, Frostbite, Homefront and Ravaged state no figure — the shared rule in
    `b3.core.events.damage_points` is what keeps this plugin and `tk` answering that the same way."""
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"

    await console.bus.publish(
        Event(EventType.CLIENT_KILL, client=joe, target=mike, data=SourceKill())
    )

    assert plugin.stats(joe).kills == 1
    assert plugin.stats(joe).damage_hit == 100


@pytest.mark.asyncio
async def test_xp_is_what_you_gained_weighted_by_how_often_you_die(console):
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"

    await _kill(console, joe, mike)

    assert plugin.stats(joe).experience == 12.5  # one kill worth 12.5, never died
    await _kill(console, mike, joe)
    # Mike is on 87.5 and Joe on 112.5, so the return kill is worth 14.64 — more than the 12.5 it
    # cost him, because Joe is now the one doing well. One kill, one death: (14.64 - 12.5) / 1.
    assert plugin.stats(mike).experience == pytest.approx(2.14, abs=0.01)


# -- the commands --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mapstats_on_a_quiet_session_reads_as_zeroes(console):
    _stats(console)
    (joe,) = _players(console, "Joe")

    await _run(console, joe, "!mapstats")

    assert "stats [Joe] K 0 D 0 TK 0 dmg 0 skill 100.00 XP 0" in _last_told(console, joe)


@pytest.mark.asyncio
async def test_mapstats_after_a_kill(console):
    _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"
    await _kill(console, joe, mike)

    await _run(console, joe, "!stats")

    assert "K 1 D 0 TK 0 dmg 100 skill 112.50 XP 12.5" in _last_told(console, joe)


@pytest.mark.asyncio
async def test_the_assist_column_appears_once_the_engine_reports_one(console):
    """Urban Terror 4.3 is the only engine here that does. The classic decided this with
    `gameName == "iourt43"` in three places; asking the server is one place and cannot go stale."""
    _stats(console)
    joe, mike = _players(console, "Joe", "Mike")

    await _run(console, joe, "!mapstats")
    assert " A " not in _last_told(console, joe)

    await console.bus.publish(
        Event(EventType.CLIENT_ASSIST, client=joe, target=mike, extra={"killer": mike})
    )
    await _run(console, joe, "!mapstats")

    assert "A 1" in _last_told(console, joe)


@pytest.mark.asyncio
async def test_testscore_says_what_a_kill_would_be_worth(console):
    _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"

    await _run(console, joe, "!testscore mike")

    assert "will get 12.5 skill points for killing Mike" in _last_told(console, joe)


@pytest.mark.asyncio
async def test_testscore_refuses_yourself_and_your_own_side(console):
    _stats(console)
    joe, mike = _players(console, "Joe", "Mike")  # both red

    await _run(console, joe, "!testscore joe")
    assert "killing yourself" in _last_told(console, joe)

    await _run(console, joe, "!testscore mike")
    assert "killing a teammate" in _last_told(console, joe)


@pytest.mark.asyncio
async def test_a_free_for_all_has_no_teammates_to_refuse(console):
    """Several engines report an empty team for everybody in a deathmatch. Reading that as "the same
    team" would refuse a question with a perfectly good answer."""
    _stats(console)
    joe, mike = _players(console, "Joe", "Mike", team="")

    await _run(console, joe, "!testscore mike")

    assert "will get 12.5 skill points" in _last_told(console, joe)


@pytest.mark.asyncio
async def test_the_board_leaves_out_the_people_who_did_nothing(console):
    """The classic's own captured test has the *victim* of a team kill topping the board on a perfect
    score, because being read by the scoring code counted as having played."""
    _stats(console)
    # A regular, because the board is regulars-and-up by default, as it was classically.
    joe, mike = _players(console, "Joe", "Mike", bits=2)
    await _kill(console, joe, mike, team=True)

    await _run(console, joe, "!topstats")

    board = _last_told(console, joe)
    assert "#1 Joe [87.5]" in board
    assert "Mike" not in board


@pytest.mark.asyncio
async def test_the_board_is_ranked_and_capped_at_five(console):
    plugin = _stats(console)
    players = _players(console, "A", "B", "C", "D", "E", "F", bits=2)
    for index, client in enumerate(players):
        record = plugin.stats(client)
        record.active = True
        record.points = 100 + index

    await _run(console, players[0], "!top")

    board = _last_told(console, players[0])
    assert board.index("#1 F") < board.index("#2 E")
    assert "A [100" not in board  # sixth place is off the board


@pytest.mark.asyncio
async def test_an_empty_board_says_so(console):
    _stats(console)
    (joe,) = _players(console, "Joe", bits=2)

    await _run(console, joe, "!topstats")

    assert "nobody has scored yet" in _last_told(console, joe)


@pytest.mark.asyncio
async def test_topxp_ranks_on_experience(console):
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike", bits=2)
    for client, xp in ((joe, 5.0), (mike, 50.0)):
        record = plugin.stats(client)
        record.active = True
        record.experience = xp

    await _run(console, joe, "!topxp")

    assert "most experienced: #1 Mike [50.0], #2 Joe [5.0]" in _last_told(console, joe)


def test_the_command_levels_come_from_the_config(console):
    _stats(console, mapstats_level=20, topstats_level=40)

    assert console.command_registry.get("mapstats").min_level == 20
    assert console.command_registry.get("topstats").min_level == 40


# -- rounds and maps -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_round_start_clears_the_counters_but_keeps_the_skill_score(console):
    """The default the classic shipped: a score that resets every round cannot say much about a
    session, while kills and deaths are per round by definition."""
    plugin = _stats(console)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"
    await _kill(console, joe, mike)

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))

    record = plugin.stats(joe)
    assert record.kills == 0
    assert record.points == 112.5
    assert record.previous_experience == 12.5  # filed away, not dropped
    assert record.experience == 0.0


@pytest.mark.asyncio
async def test_the_score_can_be_configured_to_reset_too(console):
    plugin = _stats(console, reset_score=True, reset_xp=True)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"
    await _kill(console, joe, mike)

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))

    record = plugin.stats(joe)
    assert record.points == 100.0
    assert (record.experience, record.previous_experience) == (0.0, 0.0)


@pytest.mark.asyncio
async def test_the_awards_are_announced_at_the_end_of_a_map(console):
    _stats(console, show_awards=True, show_awards_xp=True)
    joe, mike = _players(console, "Joe", "Mike")
    mike.team = "blue"
    await _kill(console, joe, mike)

    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE, data="mp_vacant"))

    assert any("top stats" in text for text in console.said)
    assert any("most experienced" in text for text in console.said)


@pytest.mark.asyncio
async def test_a_map_where_nobody_scored_announces_nothing(console):
    """The classic crashed here: `show_awards` called the command with no client, and the empty
    branch messaged that None."""
    _stats(console, show_awards=True, show_awards_xp=True)
    _players(console, "Joe", "Mike")

    await console.bus.publish(Event(EventType.GAME_MAP_CHANGE, data="mp_vacant"))

    assert console.said == []


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_log_lines_are_scored_and_reported(tmp_path):
    """A Call of Duty kill line off the log, then `!stats` typed in chat, through a real bot."""
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="stats")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = StatsPlugin(bot, None)
    bot.add_plugin(plugin, "stats")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()

    joe_guid, mike_guid = "1" * 32, "2" * 32
    await bot.replay(
        [
            f"J;{joe_guid};1;Joe",
            f"J;{mike_guid};2;Mike",
            f"K;{mike_guid};2;axis;Mike;{joe_guid};1;allies;Joe;mp5_mp;100;MOD_RIFLE;chest",
            f"say;{joe_guid};1;Joe;!stats",
        ]
    )
    await bot.bus.drain()

    assert any("K 1 D 0 TK 0 dmg 100 skill 112.50" in c for c in rcon.commands)
    bot.storage.close()
