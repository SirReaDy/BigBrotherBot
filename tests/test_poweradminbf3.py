"""The `poweradminbf3` plugin — Battlefield 3's teams, scrambler, VIP list and server configs.

This one has captured tests in the classic tree (thirty-four files, four thousand lines), and they
are where the numbers come from: a team swap threshold of 3 with a floor of 2, junk and floats
falling back to the default, and the auto-scrambler's round/map/blacklist matrix.

The matrix is also where the plugin's worst fault hid. `tests/plugins/poweradminbf3/test_scrambler.py`
asserts that a blacklisted gamemode produces no scramble, and it does — because the log line
announcing the skip raised `TypeError` (`r"...%s..." + " ...'" % blacklist` binds the `%` to the
second literal, which has no placeholder). Everything after it in the round-start handler was
skipped, which is the flag that lets autoassign run and the autobalance timer. So the captured test
asserting correct-looking behaviour is what kept two whole features dead.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.poweradminbf3 import (
    ANNOUNCE_SECONDS,
    GUNMASTER_PRESETS,
    NUKE_SECONDS,
    Poweradminbf3Plugin,
)

BF3_SERVER_VERBS = {
    "round_next",
    "round_restart",
    "round_end",
    "server_shutdown",
    "yell",
    "yell_team",
    "yell_squad",
}
BF3_PLAYER_VERBS = {"kill", "move", "yell_player"}


def _plugin(console, scrambler=None, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    # What a real Battlefield 3 console has, from the title's profile.
    console.server_verbs = set(BF3_SERVER_VERBS)
    console.player_verbs = set(BF3_PLAYER_VERBS)
    config = {}
    if settings:
        config["settings"] = settings
    if scrambler is not None:
        config["scrambler"] = scrambler
    plugin = Poweradminbf3Plugin(console, config or None)
    plugin.start()
    return plugin


def _client(console, name, team="red", squad="0", bits=0, cid=None, id_=None):  # noqa: ANN001, ANN202
    client = Client(
        guid=f"EA_{name}",
        name=name,
        cid=cid or name,
        id=id_ if id_ is not None else abs(hash(name)) % 10000,
        group_bits=bits,
        team=team,
        squad=squad,
    )
    console.clients.add(client)
    console.register_client(name.lower(), client)
    return client


def _boss(console):  # noqa: ANN001, ANN202
    return _client(console, "Boss", bits=128, id_=1)


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    told = [text for who, text in console.told if who is client]
    return told[-1] if told else ""


def _moves(console):  # noqa: ANN001, ANN202
    return [(c.name, v["team"], v["squad"]) for n, c, v in console.verbs_applied if n == "move"]


# -- the fault the captured tests asserted as correct ---------------------------------------------


@pytest.mark.asyncio
async def test_a_blacklisted_gamemode_does_not_stop_the_rest_of_the_round_start(console):
    """The classic's skip announcement raised `TypeError`, so `_scramblingdone` was never set and
    the autobalance timer never restarted. Autoassign and autobalance were dead for the whole map on
    any server whose gamemode was on the list — which no test noticed, because the test asserted
    only that no scramble happened."""
    plugin = _plugin(
        console,
        scrambler={"mode": "round", "gamemodes_blacklist": ["SquadDeathMatch0"]},
        autobalance=True,
    )
    for name in ("A", "B", "C"):
        _client(console, name)

    await console.bus.publish(
        Event(
            EventType.GAME_ROUND_START,
            data={"g_gametype": "SquadDeathMatch0", "roundsPlayed": "1"},
        )
    )

    assert _moves(console) == []  # no scramble, which is the part the classic got right
    assert plugin._settled is True  # and the rest of the handler ran, which it did not
    assert plugin._next_balance > 0


@pytest.mark.asyncio
async def test_a_gamemode_not_on_the_list_is_scrambled(console):
    plugin = _plugin(console, scrambler={"mode": "round", "gamemodes_blacklist": ["SquadRush0"]})
    for name in ("A", "B", "C", "D"):
        _client(console, name)

    await console.bus.publish(
        Event(EventType.GAME_ROUND_START, data={"g_gametype": "Rush0", "roundsPlayed": "1"})
    )

    assert len(_moves(console)) == 4
    assert plugin._settled is True


@pytest.mark.asyncio
async def test_a_map_scramble_only_runs_on_the_first_round_of_the_map(console):
    _plugin(console, scrambler={"mode": "map"})
    for name in ("A", "B", "C"):
        _client(console, name)

    await console.bus.publish(
        Event(EventType.GAME_ROUND_START, data={"g_gametype": "Rush0", "roundsPlayed": "1"})
    )
    assert _moves(console) == []

    await console.bus.publish(
        Event(EventType.GAME_ROUND_START, data={"g_gametype": "Rush0", "roundsPlayed": "0"})
    )
    assert len(_moves(console)) == 3


@pytest.mark.asyncio
async def test_the_scrambler_off_scrambles_nothing(console):
    _plugin(console, scrambler={"mode": "off"})
    for name in ("A", "B", "C"):
        _client(console, name)

    await console.bus.publish(
        Event(EventType.GAME_ROUND_START, data={"g_gametype": "Rush0", "roundsPlayed": "0"})
    )

    assert _moves(console) == []


def test_the_scrambler_mode_written_as_a_bare_off_is_still_the_word(console):
    """YAML reads `mode: off` as the boolean False, so a word-valued setting arrives as a bool and
    matches nothing."""
    plugin = _plugin(console, scrambler={"mode": False})
    assert plugin.scrambler["mode"] == "off"


# -- the scrambler itself --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_manual_scramble_happens_when_the_round_starts(console):
    plugin = _plugin(console)
    boss = _boss(console)
    for name in ("A", "B", "C"):
        _client(console, name)

    await _run(console, boss, "!scramble")
    assert "will be scrambled" in _last(console, boss)
    assert _moves(console) == []

    await console.bus.publish(
        Event(EventType.GAME_ROUND_START, data={"g_gametype": "Rush0", "roundsPlayed": "1"})
    )

    assert len(_moves(console)) == 4
    assert plugin.scramble_planned is False  # and it does not happen twice


@pytest.mark.asyncio
async def test_asking_twice_cancels_it(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!scramble")
    await _run(console, boss, "!scramble")

    assert plugin.scramble_planned is False
    assert "cancelled" in _last(console, boss)


def test_a_scramble_deals_the_players_out_one_each(console):
    plugin = _plugin(console)
    for name in ("A", "B", "C", "D", "E"):
        _client(console, name)

    assert plugin.scramble() is True

    teams = [team for _name, team, _squad in _moves(console)]
    assert teams.count("1") == 3 and teams.count("2") == 2


def test_too_few_players_to_scramble(console):
    plugin = _plugin(console)
    _client(console, "A")
    _client(console, "B")

    assert plugin.scramble() is False
    assert _moves(console) == []


@pytest.mark.asyncio
async def test_a_score_scramble_ranks_scores_as_numbers(console):
    """The classic sorted the *strings* a Frostbite block reports, so `"9"` outranked `"100"` and the
    strategy that exists to even out skill dealt the teams almost at random."""
    plugin = _plugin(console, scrambler={"strategy": "score"})
    for name in ("Low", "High", "Mid"):
        _client(console, name)

    await console.bus.publish(
        Event(
            EventType.GAME_ROUND_PLAYER_SCORES,
            data=[
                PlayerInfo(cid="Low", name="Low", score=9),
                PlayerInfo(cid="High", name="High", score=100),
                PlayerInfo(cid="Mid", name="Mid", score=50),
            ],
        )
    )

    assert [c.name for c in plugin.scramble_order()] == ["High", "Mid", "Low"]


@pytest.mark.asyncio
async def test_a_score_scramble_with_no_scoreboard_falls_back_to_random(console):
    plugin = _plugin(console, scrambler={"strategy": "score"})
    for name in ("A", "B", "C"):
        _client(console, name)

    assert len(plugin.scramble_order()) == 3  # and no exception for the missing scoreboard


@pytest.mark.asyncio
async def test_scramblemode_takes_a_whole_word_not_a_first_letter(console):
    """`data[0].lower() == 'r'` in the classic, so `!scramblemode rubbish` meant random."""
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!scramblemode rubbish")
    assert plugin.scrambler["strategy"] == "random"  # unchanged
    assert "!scramblemode random|score" in _last(console, boss)

    await _run(console, boss, "!scramblemode score")
    assert plugin.scrambler["strategy"] == "score"


# -- round control ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_round_change_is_announced_now_and_done_later(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!roundnext")

    assert any("next round" in text for text in console.said)
    assert console.server_verbs_applied == []

    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("round_next", {})]


@pytest.mark.asyncio
async def test_ending_a_round_needs_a_winner_and_says_so(console):
    """`max()` over an empty dict in the classic, which raises rather than answering."""
    _plugin(console)
    boss = _boss(console)
    console.clients.remove(boss.cid)  # nobody on any team, and no scoreboard

    await _run(console, boss, "!endround")

    assert console.server_verbs_applied == []
    assert "!endround" in _last(console, boss)


@pytest.mark.asyncio
async def test_ending_a_round_picks_the_team_that_is_ahead(console):
    plugin = _plugin(console)
    boss = _boss(console)
    await console.bus.publish(
        Event(
            EventType.GAME_ROUND_PLAYER_SCORES,
            data=[
                PlayerInfo(cid="A", name="A", team="1", score=10),
                PlayerInfo(cid="B", name="B", team="2", score=90),
            ],
        )
    )

    await _run(console, boss, "!endround")
    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("round_end", {"team": "2"})]


@pytest.mark.asyncio
async def test_a_named_team_wins_instead(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!endround 1")
    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("round_end", {"team": "1"})]


@pytest.mark.asyncio
async def test_restarting_the_game_server_asks_first(console):
    """The classic carried a `@todo: Demand that the user wants to restart really` for years."""
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!serverreboot")
    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()
    assert console.server_verbs_applied == []
    assert "!serverreboot yes" in _last(console, boss)

    await _run(console, boss, "!serverreboot yes")
    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()
    assert console.server_verbs_applied == [("server_shutdown", {})]


@pytest.mark.asyncio
async def test_a_title_with_no_verb_for_it_says_so(console):
    _plugin(console)
    console.server_verbs = set()
    boss = _boss(console)

    await _run(console, boss, "!roundnext")

    assert console.said == []
    assert "no verb for that" in _last(console, boss)


# -- players ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_killing_a_player_tells_them_why(console):
    _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob")

    await _run(console, boss, "!kill bob being rude")

    assert [(n, c.name) for n, c, _v in console.verbs_applied] == [("kill", "Bob")]
    assert "being rude" in _last(console, bob)


@pytest.mark.asyncio
async def test_a_nuke_will_not_reach_an_admin_at_your_level(console):
    """The classic checked nothing here at all, so an admin who could not `!kill` one senior admin
    could kill every one of them with this."""
    plugin = _plugin(console)
    one = _client(console, "One", bits=16, id_=1)  # admin, 40
    _client(console, "Two", bits=16, id_=2)
    _client(console, "Bob", id_=3)

    await _run(console, one, "!nuke all")
    console.clock.advance(NUKE_SECONDS)
    plugin._run_pending()

    assert [c.name for n, c, _v in console.verbs_applied if n == "kill"] == ["Bob"]


@pytest.mark.asyncio
async def test_a_nuke_can_name_one_team(console):
    plugin = _plugin(console)
    boss = _boss(console)
    _client(console, "Red", team="red")
    _client(console, "Blue", team="blue")

    await _run(console, boss, "!nuke blue")
    console.clock.advance(NUKE_SECONDS)
    plugin._run_pending()

    assert [c.name for n, c, _v in console.verbs_applied if n == "kill"] == ["Blue"]
    assert any("incoming nuke" in text for text in console.said_big)


@pytest.mark.asyncio
async def test_a_nuke_with_nobody_to_kill_says_so(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!nuke red")

    assert "nobody there" in _last(console, boss)
    assert console.said_big == []


@pytest.mark.asyncio
async def test_changing_a_players_team(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob", team="blue")

    await _run(console, boss, "!changeteam bob")

    assert _moves(console) == [("Bob", "1", "0")]
    assert "red team" in _last(console, boss)


@pytest.mark.asyncio
async def test_squad_deathmatch_has_four_teams_not_two(console):
    """The classic's rule, and why the arithmetic is a rotation rather than a swap of 1 and 2."""
    _plugin(console)
    console.game.gametype = "SquadDeathMatch0"
    boss = _boss(console)
    _client(console, "Bob", team="green")  # team 3 of 4

    await _run(console, boss, "!changeteam bob")

    assert _moves(console) == [("Bob", "4", "0")]


@pytest.mark.asyncio
async def test_a_player_with_no_team_cannot_be_moved(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob", team="")

    await _run(console, boss, "!changeteam bob")

    assert _moves(console) == []
    assert "not said which team" in _last(console, boss)


# -- the swap, which is the command that needs squads ---------------------------------------------


@pytest.mark.asyncio
async def test_a_swap_puts_each_player_in_the_others_squad(console):
    """Three moves, not two: the first player has to leave their squad before the second can be put
    into it, or the server refuses a full squad."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Ann", team="red", squad="1")
    _client(console, "Bob", team="blue", squad="2")

    await _run(console, boss, "!swap ann bob")

    assert _moves(console) == [("Ann", "2", "0"), ("Bob", "1", "1"), ("Ann", "2", "2")]
    assert "swapped Ann with Bob" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_swap_with_one_spectator_is_refused(console):
    """`A not in (1, 2) and B not in (1, 2)` in the classic, so it only refused when *both* were
    teamless — with one of them at the deploy screen the other was moved to team 0."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Ann", team="red", squad="1")
    _client(console, "Ghost", team="")

    await _run(console, boss, "!swap ann ghost")

    assert _moves(console) == []
    assert "not said which team" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_swap_with_one_name_swaps_with_whoever_asked(console):
    _plugin(console)
    boss = _client(console, "Boss", bits=128, id_=1, team="blue", squad="4")
    _client(console, "Ann", team="red", squad="1")

    await _run(console, boss, "!swap ann")

    assert _moves(console) == [("Ann", "2", "0"), ("Boss", "1", "1"), ("Ann", "2", "4")]


@pytest.mark.asyncio
async def test_a_one_character_name_is_a_player_not_a_shorthand(console):
    """`if len(pA) == 1 or otherparams is None` in the classic, so a player really called `x` could
    never be the first half of a swap."""
    _plugin(console)
    boss = _client(console, "Boss", bits=128, id_=1, team="blue", squad="0")
    _client(console, "x", team="red", squad="1")

    await _run(console, boss, "!swap x")

    assert [name for name, _t, _s in _moves(console)] == ["x", "Boss", "x"]


@pytest.mark.asyncio
async def test_two_players_already_together_are_not_swapped(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Ann", team="red", squad="1")
    _client(console, "Bob", team="red", squad="1")

    await _run(console, boss, "!swap ann bob")

    assert _moves(console) == []
    assert "same team and squad" in _last(console, boss)


# -- one immunity rule -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_same_rule_covers_kill_swap_and_changeteam(console):
    """Three different comparisons in the classic, and none at all on `!nuke`."""
    _plugin(console, no_level_check_level=100)
    one = _client(console, "One", bits=16, id_=1)  # admin, 40
    _client(console, "Two", bits=16, id_=2, team="blue")

    for text in ("!kill two", "!changeteam two", "!swap two one"):
        await _run(console, one, text)
        assert "at or above your level" in _last(console, one), text

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_an_admin_above_the_escape_hatch_is_not_level_checked(console):
    """`no_level_check_level` is the classic's own setting, at its own default of fulladmin."""
    _plugin(console, no_level_check_level=60)
    senior = _client(console, "Senior", bits=64, id_=1)  # 80, over the hatch
    _client(console, "Other", bits=64, id_=2)

    await _run(console, senior, "!kill other")

    assert [n for n, _c, _v in console.verbs_applied] == ["kill"]


# -- keeping the teams even ------------------------------------------------------------------------


async def _round_has_finished(console):  # noqa: ANN001, ANN202
    """One round over and the next started, which is when this plugin starts moving anybody."""
    await console.bus.publish(Event(EventType.GAME_ROUND_END, data="1"))
    await console.bus.publish(
        Event(EventType.GAME_ROUND_START, data={"g_gametype": "Rush0", "roundsPlayed": "1"})
    )
    console.verbs_applied.clear()


@pytest.mark.asyncio
async def test_an_arrival_on_the_bigger_team_is_moved(console):
    _plugin(console, autoassign=True, team_swap_threshold=2)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "D", team="blue")
    await _round_has_finished(console)

    late = _client(console, "Late", team="red")
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=late))

    assert _moves(console) == [("Late", "2", "0")]


@pytest.mark.asyncio
async def test_nobody_is_moved_before_a_round_has_finished(console):
    """Moving people mid-round because the bot has just started is not balancing, it is meddling."""
    _plugin(console, autoassign=True, team_swap_threshold=2)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "D", team="blue")

    late = _client(console, "Late", team="red")
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=late))

    assert _moves(console) == []


@pytest.mark.asyncio
async def test_an_exempt_player_is_not_moved(console):
    _plugin(console, autoassign=True, team_swap_threshold=2, no_autoassign_level=20)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "D", team="blue")
    await _round_has_finished(console)

    mod = _client(console, "Mod", team="red", bits=8)  # level 20
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=mod))

    assert _moves(console) == []


@pytest.mark.asyncio
async def test_the_balancer_moves_the_most_recent_joiners(console):
    plugin = _plugin(console, autobalance=True, team_swap_threshold=2)
    for name in ("Old", "Mid", "New"):
        client = _client(console, name, team="red")
        await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))
    _client(console, "Blue", team="blue")
    await _round_has_finished(console)

    assert plugin.balance() is True

    assert [name for name, _t, _s in _moves(console)] == ["New"]


@pytest.mark.asyncio
async def test_even_teams_are_left_alone(console):
    plugin = _plugin(console, autobalance=True, team_swap_threshold=2)
    _client(console, "A", team="red")
    _client(console, "B", team="blue")
    await _round_has_finished(console)

    assert plugin.balance() is False
    assert _moves(console) == []


@pytest.mark.asyncio
async def test_a_rename_does_not_leave_a_ghost_in_the_join_order(console):
    """The classic kept names and removed the name the leave line carried, so a renamed player was
    never removed and the list grew for as long as the bot ran. The balancer walks that list."""
    plugin = _plugin(console)
    bob = _client(console, "Bob")
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=bob))
    bob.name = "Robert"

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bob))

    assert plugin._joined == []


def test_the_threshold_has_a_floor_and_falls_back_on_junk(console):
    """From `tests/plugins/poweradminbf3/test_team_swap_threshold.py`: default 3, floor 2, and junk
    or a float falls back to the default."""
    assert _plugin(console).settings["team_swap_threshold"] == 3
    assert _plugin(console, team_swap_threshold=6).settings["team_swap_threshold"] == 6
    assert _plugin(console, team_swap_threshold=1).settings["team_swap_threshold"] == 2
    assert _plugin(console, team_swap_threshold=-2).settings["team_swap_threshold"] == 2
    assert _plugin(console, team_swap_threshold="junk").settings["team_swap_threshold"] == 3


def test_the_threshold_can_grow_with_the_server(console):
    plugin = _plugin(console, team_swap_threshold=2, team_swap_threshold_prop=True)

    assert plugin.swap_threshold(10) == 2
    assert plugin.swap_threshold(30) == 3
    assert plugin.swap_threshold(50) == 4


def test_autobalance_switches_autoassign_on_with_it(console):
    """Balancing without assigning evens the teams and lets the next arrival undo it."""
    plugin = _plugin(console, autobalance=True)
    assert plugin.settings["autoassign"] is True


@pytest.mark.asyncio
async def test_turning_autoassign_off_turns_autobalance_off_too(console):
    plugin = _plugin(console, autobalance=True)
    boss = _boss(console)

    await _run(console, boss, "!autoassign off")

    assert plugin.settings["autobalance"] is False


# -- settings --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_setting_is_written_with_the_vars_name(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!unlockmode stats")

    assert console.cvars["vars.unlockMode"] == "stats"


@pytest.mark.asyncio
async def test_an_unknown_unlock_mode_lists_the_real_ones(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!unlockmode everything")

    assert "vars.unlockMode" not in console.cvars
    assert "all|common|stats|none" in _last(console, boss)


@pytest.mark.asyncio
async def test_vehicles_are_a_true_or_false_on_this_engine(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!vehicles off")
    assert console.cvars["vars.vehicleSpawnAllowed"] == "false"

    await _run(console, boss, "!vehicles")
    assert "vehicle spawning is off" in _last(console, boss)


@pytest.mark.asyncio
async def test_idle_kicking_takes_minutes_and_remembers_the_old_value(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!idle 7")
    assert console.cvars["vars.idleTimeout"] == "420"
    assert "after 7 minutes" in _last(console, boss)

    await _run(console, boss, "!idle off")
    assert console.cvars["vars.idleTimeout"] == "0"

    await _run(console, boss, "!idle on")
    assert console.cvars["vars.idleTimeout"] == "420"


@pytest.mark.asyncio
async def test_the_gun_master_preset_takes_the_whole_number(console):
    """`int(data[0])` in the classic — the first character, so a two-digit preset was unreachable."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, f"!gunmaster {len(GUNMASTER_PRESETS) - 1}")

    assert console.cvars["vars.gunMasterWeaponsPreset"] == "8"
    assert GUNMASTER_PRESETS[8] in _last(console, boss)


@pytest.mark.asyncio
async def test_a_preset_that_does_not_exist_is_refused(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!gunmaster 99")

    assert "vars.gunMasterWeaponsPreset" not in console.cvars
    assert "0-8" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_presets_can_be_listed(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!gunmaster show")

    assert "Snipers Heaven" in _last(console, boss)


# -- yelling ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_yell_goes_to_the_engines_own_verb_with_the_configured_duration(console):
    _plugin(console, yell_duration=4)
    boss = _boss(console)

    await _run(console, boss, "!yell get on the objective")

    assert console.server_verbs_applied == [
        ("yell", {"text": "get on the objective", "seconds": "4"})
    ]


@pytest.mark.asyncio
async def test_a_yell_can_be_aimed_at_a_team_a_squad_or_a_player(console):
    _plugin(console)
    boss = _client(console, "Boss", bits=128, id_=1, team="blue", squad="3")
    _client(console, "Bob")

    await _run(console, boss, "!yellteam hold the flag")
    assert console.server_verbs_applied[-1] == (
        "yell_team",
        {"text": "hold the flag", "seconds": "10", "team": "2"},
    )

    await _run(console, boss, "!yellsquad regroup")
    assert console.server_verbs_applied[-1] == (
        "yell_squad",
        {"text": "regroup", "seconds": "10", "team": "2", "squad": "3"},
    )

    await _run(console, boss, "!yellplayer bob stop that")
    assert [(n, c.name, v) for n, c, v in console.verbs_applied if n == "yell_player"] == [
        ("yell_player", "Bob", {"text": "stop that", "seconds": "10"})
    ]


@pytest.mark.asyncio
async def test_a_yell_with_nothing_to_say_names_its_own_help(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!yellteam")

    assert "!yellteam <message>" in _last(console, boss)
    assert console.server_verbs_applied == []


# -- the reserved slots list -----------------------------------------------------------------------


def _vips(console, *names):  # noqa: ANN001, ANN202
    """The paged `reservedSlotsList.list` answers, as the engine gives them: words, not text."""
    console.rcon_words_replies["reservedSlotsList.list 0"] = list(names)
    console.rcon_words_replies[f"reservedSlotsList.list {len(names)}"] = []


@pytest.mark.asyncio
async def test_the_vip_list_is_read_as_words_so_a_name_may_contain_a_space(console):
    """`command` joins the reply with spaces, and a Battlefield player may be called `Sgt Pepper` —
    once it is one string nothing can tell that from two players."""
    plugin = _plugin(console)
    _vips(console, "Sgt Pepper", "Bob")

    assert plugin.vip_list() == ["Sgt Pepper", "Bob"]


@pytest.mark.asyncio
async def test_the_connected_vips_are_the_ones_who_are_here(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob")
    _vips(console, "Bob", "Absent")

    await _run(console, boss, "!vips")

    assert "connected VIPs: Bob" in _last(console, boss)


@pytest.mark.asyncio
async def test_no_vips_connected_says_so(console):
    _plugin(console)
    boss = _boss(console)
    _vips(console, "Absent")

    await _run(console, boss, "!vips")

    assert "no VIPs are connected" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_vip_can_be_added_for_somebody_who_is_not_here(console):
    """The ordinary case: the list is names, and a reserved slot is what gets somebody *in*."""
    _plugin(console)
    boss = _boss(console)
    _vips(console)

    await _run(console, boss, "!vipadd SomeoneElse")

    assert 'reservedSlotsList.add "SomeoneElse"' in console.rcon_sent
    assert "SomeoneElse is a VIP now" in _last(console, boss)


@pytest.mark.asyncio
async def test_adding_a_connected_player_uses_the_name_the_server_uses(console):
    _plugin(console)
    boss = _boss(console)
    console.register_client("bravo", _client(console, "Bravo17"))
    _vips(console)

    await _run(console, boss, "!vipadd bravo")

    assert 'reservedSlotsList.add "Bravo17"' in console.rcon_sent


@pytest.mark.asyncio
async def test_removing_a_vip_nobody_has_says_so(console):
    _plugin(console)
    boss = _boss(console)
    _vips(console, "Bob")

    await _run(console, boss, "!vipremove Nobody")

    assert not [line for line in console.rcon_sent if "remove" in line]
    assert "no VIP called Nobody" in _last(console, boss)


@pytest.mark.asyncio
async def test_removing_a_vip_matches_the_case_the_list_uses(console):
    _plugin(console)
    boss = _boss(console)
    _vips(console, "Bravo17")

    await _run(console, boss, "!vipremove bravo17")

    assert 'reservedSlotsList.remove "Bravo17"' in console.rcon_sent


@pytest.mark.asyncio
async def test_the_vip_list_can_be_emptied_read_and_written(console):
    _plugin(console)
    boss = _boss(console)
    _vips(console, "Bob")

    await _run(console, boss, "!vipclear")
    await _run(console, boss, "!vipload")
    await _run(console, boss, "!vipsave")

    for verb in ("clear", "load", "save"):
        assert f"reservedSlotsList.{verb}" in console.rcon_sent


@pytest.mark.asyncio
async def test_a_long_vip_list_is_shown_in_part(console):
    _plugin(console)
    boss = _boss(console)
    _vips(console, *[f"Vip{n:02d}" for n in range(20)])

    await _run(console, boss, "!viplist")

    assert "and 5 more" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_vip_filter_that_matches_nothing_says_how_many_there_are(console):
    _plugin(console)
    boss = _boss(console)
    _vips(console, "Bob", "Ann")

    await _run(console, boss, "!viplist zzz")

    assert "among the 2 there are" in _last(console, boss)


# -- preset server configs -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_configs_are_listed_by_name_without_the_extension(console, tmp_path):
    (tmp_path / "hardcore.cfg").write_text("vars.friendlyFire true\n")
    (tmp_path / "notes.txt").write_text("not a config\n")
    _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!listconfig")

    assert "hardcore" in _last(console, boss)
    assert "notes" not in _last(console, boss)


@pytest.mark.asyncio
async def test_a_config_is_matched_rather_than_spelled_exactly(console, tmp_path):
    (tmp_path / "hardcore-tdm.cfg").write_text("vars.friendlyFire true\n")
    plugin = _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!loadconfig hardco")

    assert plugin._loading is not None
    assert plugin._loading.name == "hardcore-tdm"


@pytest.mark.asyncio
async def test_two_matching_configs_are_a_question(console, tmp_path):
    (tmp_path / "hardcore-tdm.cfg").write_text("vars.friendlyFire true\n")
    (tmp_path / "hardcore-conquest.cfg").write_text("vars.friendlyFire true\n")
    plugin = _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!loadconfig hardco")

    assert plugin._loading is None
    assert "did you mean" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_config_goes_out_from_the_scheduled_pass_not_the_handler(console, tmp_path):
    """`thread.start_new_thread` in the classic, with a sleep per line."""
    (tmp_path / "match.cfg").write_text(
        "// a comment\nvars.friendlyFire true\nvars.roundLockdownCountdown 30\n"
    )
    plugin = _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!loadconfig match")
    assert console.rcon_sent == []
    assert "2 line(s)" in _last(console, boss)

    plugin._run_pending()

    assert console.rcon_sent == ["vars.friendlyFire true", "vars.roundLockdownCountdown 30"]
    assert "sent in full" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_rotation_in_a_config_rebuilds_the_map_list(console, tmp_path):
    (tmp_path / "rot.cfg").write_text(
        "vars.friendlyFire true\nMP_001 ConquestLarge0 2\nMP_007 RushLarge0 1\n"
    )
    plugin = _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!loadconfig rot")
    for _ in range(3):
        plugin._run_pending()

    assert console.rcon_sent == [
        "vars.friendlyFire true",
        "mapList.clear",
        'mapList.add "MP_001" "ConquestLarge0" 2',
        'mapList.add "MP_007" "RushLarge0" 1',
        "mapList.save",
    ]


@pytest.mark.asyncio
async def test_a_line_that_is_neither_is_reported_rather_than_dropped(console, tmp_path, caplog):
    """The classic dropped it in silence, so a config with an `admin.password` line in it looked
    like it had been applied in full."""
    (tmp_path / "odd.cfg").write_text("vars.friendlyFire true\nadmin.password hunter2\n")
    _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    with caplog.at_level("WARNING"):
        await _run(console, boss, "!loadconfig odd")

    assert "neither a setting nor a map" in caplog.text


@pytest.mark.asyncio
async def test_a_second_load_while_one_is_running_is_refused(console, tmp_path):
    (tmp_path / "a.cfg").write_text("\n".join(f"vars.x{n} 1" for n in range(20)))
    (tmp_path / "b.cfg").write_text("vars.y 1\n")
    plugin = _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!loadconfig a")
    plugin._run_pending()
    await _run(console, boss, "!loadconfig b")

    assert "still being sent" in _last(console, boss)


@pytest.mark.asyncio
async def test_with_no_directory_configured_the_command_says_so(console):
    """The classic looked inside its own installed source tree."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!listconfig")

    assert "no config directory" in _last(console, boss)


# -- the next map ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_next_map_is_an_index_into_the_rotation(console):
    """There is no next-map setting on this engine: the map goes into the rotation just after the one
    being played, and the server is pointed at that index."""
    _plugin(console)
    console.maps = ["MP_001", "MP_007"]
    console.map_names = {"mp_001": "Grand Bazaar", "mp_007": "Caspian Border"}
    console.game.gametype = "RushLarge0"
    console.rcon_words_replies["mapList.getMapIndices"] = ["0", "1"]
    boss = _boss(console)

    await _run(console, boss, "!setnextmap caspian")

    assert console.rcon_sent == [
        "mapList.getMapIndices",
        'mapList.add "MP_007" "RushLarge0" "2" 1',
        "mapList.setNextMapIndex 1",
    ]
    assert "Caspian Border" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_map_nobody_has_is_refused_with_a_few_that_exist(console):
    _plugin(console)
    console.maps = ["MP_001"]
    boss = _boss(console)

    await _run(console, boss, "!setnextmap moon")

    assert console.rcon_sent == []
    assert "no map like moon" in _last(console, boss)


# -- it is a Battlefield 3 plugin ------------------------------------------------------------------


def test_the_loader_refuses_it_on_any_other_title(console):
    from b3.config.schema import Config, PluginEntry, ServerConfig
    from b3.core.pluginmgr import load_plugins

    loaded = load_plugins(
        console,
        Config(
            server=ServerConfig(game="bfbc2"),
            plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminbf3")],
        ),
    )
    plugin = next(item for item in loaded if item.name == "poweradminbf3")

    assert plugin.enabled is False
    assert "does not support the 'bfbc2' parser" in plugin.reason


def test_every_verb_this_plugin_uses_is_one_the_title_declares():
    """So a command can ask before it is offered, rather than sending a verb into the dark."""
    from b3.parsers.frostbite.profiles import BF3

    assert BF3_SERVER_VERBS <= set(BF3.server_verbs)
    assert BF3_PLAYER_VERBS <= set(BF3.player_verbs)
