"""The `poweradminmoh` plugin — the 2010 Medal of Honor's teams, scrambler and reserved slots.

No captured tests in the classic tree, so the source, the shipped `.ini` and the plugin's own README
are the whole description. The README is unusually useful here: its changelog records "major fix to
the `admin.movePlayer` MoH command. This affected all team balancing features" — which is the fault
this port has to get right, because this title's move verb takes **three** arguments where the rest
of the family takes four. A four-argument move is refused, and every feature that moves anybody —
`!changeteam`, `!swap`, `!spect`, the balancer, the scrambler — does nothing at all.

The rest of the list is the shape of a plugin nobody had read closely in fifteen years: `!swap` with
no arguments crashed on the *builtin* `input`, a swap with one spectator went ahead, the score
scramble sorted score strings, a config section the plugin looked for took the whole plugin down, and
"already has a reserved slot" was compared as a string to a list so it could never be said.
"""

from __future__ import annotations

import json

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.core.matchmode import COUNTDOWN_FROM, NAG_SECONDS
from b3.domain.client import Client
from b3.parsers.frostbite.profiles import MOH, MOH_TEAMS
from b3.plugins.admin import AdminPlugin
from b3.plugins.poweradminmoh import (
    ANNOUNCE_SECONDS,
    QUIET_SECONDS,
    PoweradminmohPlugin,
)

MOH_SERVER_VERBS = {"round_next", "round_restart"}
MOH_PLAYER_VERBS = {"kill", "move"}


def _plugin(console, scrambler=None, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    # What a real Medal of Honor console has, from the title's profile — including a team table in
    # which 3 is the *spectators*, which is this title's own and not the family's.
    console.server_verbs = set(MOH_SERVER_VERBS)
    console.player_verbs = set(MOH_PLAYER_VERBS)
    console.teams = dict(MOH_TEAMS)
    config: dict[str, object] = {}
    if settings:
        config["settings"] = settings
    if scrambler is not None:
        config["scrambler"] = scrambler
    plugin = PoweradminmohPlugin(console, config or None)
    plugin.start()
    # Past the quiet minute the plugin holds after startup, with the next automatic pass a full
    # interval away — which is where a server that has been running for a while actually is.
    console.clock.advance(QUIET_SECONDS + 1)
    plugin._schedule_balance()
    return plugin


def _client(console, name, team="red", bits=0, cid=None, id_=None):  # noqa: ANN001, ANN202
    client = Client(
        guid=f"EA_{name}",
        name=name,
        cid=cid or name,
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


def _moves(console):  # noqa: ANN001, ANN202
    return [(c.name, v["team"]) for name, c, v in console.verbs_applied if name == "move"]


# -- the fault the README calls a major fix --------------------------------------------------------


def test_this_titles_move_verb_takes_three_arguments_not_four():
    """ "Major fix to the `admin.movePlayer` MoH command. This affected all team balancing features."

    Four arguments on a three-argument verb is refused, so every feature that moves anybody does
    nothing — and the plugin has no way to know, because the classic swallowed the error. The verb is
    the title's, on the profile, so there is one place for it to be right.
    """
    assert MOH.player_verbs["move"] == 'admin.movePlayer "%(cid)s" %(team)s true'
    assert "%(squad)s" not in MOH.player_verbs["move"]
    # And a caller written for the rest of the family still works: the squad it passes is ignored
    # rather than being a missing-argument error.
    assert MOH.player_verbs["move"] % {"cid": "Bob", "team": "2", "squad": "0"} == (
        'admin.movePlayer "Bob" 2 true'
    )


def test_this_title_has_no_centre_screen_message():
    """The classic's `moh.py` overrides `saybig` to call `say`, which is a parser author saying there
    is no `admin.yell` here as plainly as it can be said. With a template it would be answered
    `UnknownCommand`; without one, `say_big` falls back to chat."""
    assert MOH.saybig_template is None


def test_team_three_is_the_spectators_on_this_title():
    """Its own parser says so and this plugin sends a player there for `!spect`. It has no four-sided
    gamemode for 3 and 4 to be playing teams in, which is what makes it safe to state here."""
    assert MOH.teams["3"] == "spec"
    assert MOH.team_id("spec") == "3"


# -- the teams -------------------------------------------------------------------------------------


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

    await _run(console, boss, "!teams")

    assert [name for name, _ in _moves(console)] == ["Newcomer"]
    assert veteran.team == "red"


@pytest.mark.asyncio
async def test_spectators_are_not_counted_as_a_team(console):
    """Team 3 is the spectators here, so counting them as a side would have the balancer chasing a
    gap that no move can close."""
    plugin = _plugin(console, teambalance=True)
    _client(console, "A", team="red")
    _client(console, "B", team="blue")
    for name in ("W1", "W2", "W3"):
        _client(console, name, team="spec")

    assert plugin.team_counts() == {"1": 1, "2": 1}
    assert plugin.balance() is False


@pytest.mark.asyncio
async def test_the_balancer_leaves_the_admins_where_they_are(console):
    plugin = _plugin(console, teambalance=True, no_balance_level=40)
    boss = _boss(console)
    admin = _client(console, "Admin", team="red", bits=16)  # level 40, at the exemption
    plugin._stamp(admin)
    console.clock.advance(10)
    ordinary = _client(console, "Ordinary", team="red")
    plugin._stamp(ordinary)
    _client(console, "Z", team="blue")

    await _run(console, boss, "!teams")

    assert [name for name, _ in _moves(console)] == ["Ordinary"]


@pytest.mark.asyncio
async def test_the_automatic_pass_runs_on_the_configured_interval(console):
    """The classic wrote the interval into a crontab minute field; here it is a deadline on the
    plugin's own pass."""
    plugin = _plugin(console, teambalance=True, check_interval=1)
    for name in ("A", "B", "C", "D"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")

    plugin._run_pending()
    assert _moves(console) == []  # not due yet

    console.clock.advance(60)
    plugin._run_pending()

    assert len(_moves(console)) == 1


@pytest.mark.asyncio
async def test_a_player_leaving_does_not_start_a_balance(console):
    """Somebody leaving unbalances the teams without anybody having done anything wrong, and the slot
    is usually filled within seconds. The classic's ten."""
    plugin = _plugin(console, teambalance=True, check_interval=1)
    for name in ("A", "B", "C", "D"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, data=None))
    plugin._next_balance = console.clock.now()  # a pass falling due in the same moment
    plugin._run_pending()
    assert _moves(console) == []

    # Once the ten seconds are up, the next pass balances as usual.
    console.clock.advance(60)
    plugin._run_pending()
    assert len(_moves(console)) == 1


@pytest.mark.asyncio
async def test_changeteam_moves_a_player_to_the_other_side(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Red", team="red")
    _client(console, "Blue", team="blue")

    await _run(console, boss, "!changeteam Red")
    await _run(console, boss, "!changeteam Blue")

    assert _moves(console) == [("Red", "2"), ("Blue", "1")]


@pytest.mark.asyncio
async def test_spect_sends_a_player_to_the_spectators(console):
    _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob", team="red")

    await _run(console, boss, "!spect Bob")

    assert _moves(console) == [("Bob", "3")]
    assert bob.team == "spec"


# -- !swap, which crashed on the word `input` ------------------------------------------------------


@pytest.mark.asyncio
async def test_swap_with_no_arguments_says_how_to_use_it(console):
    """`if not input:` in the classic — the *builtin*, which is always truthy — so the check for
    empty data never ran and the line below it indexed `None`."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!swap")

    assert _moves(console) == []
    assert "<player>" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_swap_with_one_spectator_is_refused(console):
    """`A not in (1, 2) and B not in (1, 2)` refused only when *both* were teamless, so with one of
    them spectating the other was moved to team 0."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Player", team="red")
    _client(console, "Watcher", team="spec")

    await _run(console, boss, "!swap Player Watcher")

    assert _moves(console) == []
    assert "not said which team" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_swap_exchanges_two_players(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Red", team="red")
    _client(console, "Blue", team="blue")

    await _run(console, boss, "!swap Red Blue")

    assert _moves(console) == [("Red", "2"), ("Blue", "1")]
    assert "swapped Red with Blue" in _last(console, boss)


@pytest.mark.asyncio
async def test_two_players_on_one_team_cannot_be_swapped(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "One", team="red")
    _client(console, "Two", team="red")

    await _run(console, boss, "!swap One Two")

    assert _moves(console) == []
    assert "same team" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_moderator_may_not_move_a_senior_admin(console):
    """The classic level-checked in the balancer only, so any moderator could `!spect` a senior
    admin out of the game."""
    _plugin(console, no_level_check_level=100)
    mod = _client(console, "Mod", bits=8)
    _client(console, "Senior", bits=64)

    await _run(console, mod, "!spect Senior")

    assert _moves(console) == []
    assert "Senior" in _last(console, mod)


# -- the bot's own moves ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_move_the_server_refuses_does_not_silence_the_next_real_team_change(console):
    """`movedByBot` was set *before* the move and never cleared when it failed — and this is the only
    plugin in the classic tree that reads that variable, so the player's next genuine team change was
    ignored and their team-join time never updated."""
    plugin = _plugin(console, teambalance=True)
    console.player_verbs = {"kill"}  # a server that will not take a move at all
    bob = _client(console, "Bob", team="red")

    assert plugin.move(bob, "2") is False
    assert bob.get_var(plugin, "moved_by_bot", False) is False


@pytest.mark.asyncio
async def test_the_bots_own_move_is_not_read_as_a_player_switching_sides(console):
    plugin = _plugin(console, teambalance=True)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")
    moved = _client(console, "Moved", team="red")

    plugin.move(moved, "2")
    console.verbs_applied.clear()
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, data="blue", client=moved))

    assert _moves(console) == []


# -- the round and the map -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_next_round_is_announced_first_and_sent_from_the_scheduled_pass(console):
    """`time.sleep(1)` in the classic, on the bot's one thread."""
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!runnextround")
    assert console.said == ["moving to the next round"]
    assert console.server_verbs_applied == []

    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("round_next", {})]


@pytest.mark.asyncio
async def test_restarting_the_round_uses_this_generations_own_verb(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!restartround")
    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("round_restart", {})]


@pytest.mark.asyncio
async def test_an_unknown_map_is_answered_with_maps_rather_than_letters(console):
    """`", ".join(data)` over a *string* joins its characters, so `!setnextmap nuketown` came back
    "did you mean n, u, k, e, t, o, w, n?"."""
    _plugin(console)
    boss = _boss(console)
    console.maps = ["Levels/MP_01", "Levels/MP_02"]
    console.map_names = {"levels/mp_01": "Kabul City Ruins", "levels/mp_02": "Kandahar"}

    await _run(console, boss, "!setnextmap atlantis")

    assert console.rcon_sent == []
    assert "Kabul City Ruins" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_next_map_is_an_index_into_the_flat_rotation(console):
    _plugin(console)
    boss = _boss(console)
    console.maps = ["Levels/MP_01", "Levels/MP_02"]
    console.map_names = {"levels/mp_02": "Kandahar"}

    await _run(console, boss, "!setnextmap kandahar")

    assert console.rcon_sent == ["mapList.nextLevelIndex 1"]
    assert "Kandahar" in _last(console, boss)


# -- the scrambler ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_planned_scramble_happens_at_the_next_round_start(console):
    plugin = _plugin(console)
    boss = _boss(console)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")

    await _run(console, boss, "!scramble")
    assert _moves(console) == []

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))

    assert sorted(team for _, team in _moves(console)) == ["1", "1", "2", "2"]
    assert plugin.scramble_planned is False  # and it does not happen again next round


@pytest.mark.asyncio
async def test_a_scramble_can_be_cancelled_before_the_round_ends(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!scramble")
    await _run(console, boss, "!scramble")

    assert plugin.scramble_planned is False
    assert "cancelled" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_auto_scrambler_runs_at_a_map_change_only_when_asked_to(console):
    plugin = _plugin(console, scrambler={"mode": "map"})
    for name in ("A", "B", "C"):
        _client(console, name, team="red")

    console.game.rounds = 2
    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))
    assert _moves(console) == []

    console.game.rounds = 0
    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))
    assert _moves(console) != []
    assert plugin.scrambler["mode"] == "map"


@pytest.mark.asyncio
async def test_a_match_stops_the_auto_scrambler(console):
    """Scrambling the teams two people have just agreed is not a service to either of them."""
    _plugin(console, scrambler={"mode": "round"})
    boss = _boss(console)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    await _run(console, boss, "!match on")
    console.verbs_applied.clear()

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))

    assert _moves(console) == []


def test_the_score_scramble_deals_by_score_as_a_number(console):  # noqa: ANN001
    """`sorted(scores, key=lambda x: x['score'])` over the *strings* a Frostbite block reports, so
    `"9"` outranked `"100"` and the strategy that exists to even out skill dealt almost at random."""
    plugin = _plugin(console, scrambler={"strategy": "score"})
    weak = _client(console, "Weak", team="red")
    middle = _client(console, "Middle", team="red")
    strong = _client(console, "Strong", team="red")
    plugin.on_scores(
        Event(
            EventType.GAME_ROUND_PLAYER_SCORES,
            data=[
                PlayerInfo(cid="Weak", name="Weak", score=9),
                PlayerInfo(cid="Middle", name="Middle", score=40),
                PlayerInfo(cid="Strong", name="Strong", score=100),
            ],
        )
    )

    order = plugin.scramble_order()

    assert order == [strong, middle, weak]
    plugin.scramble()
    # Dealt alternately, so the two strongest end up on opposite sides.
    assert _moves(console) == [("Strong", "1"), ("Middle", "2"), ("Weak", "1")]


def test_a_score_scramble_with_no_scoreboard_yet_falls_back_to_random(console):  # noqa: ANN001
    plugin = _plugin(console, scrambler={"strategy": "score"})
    for name in ("A", "B", "C"):
        _client(console, name, team="red")

    assert len(plugin.scramble_order()) == 3
    assert plugin.scramble() is True


def test_the_scrambler_leaves_out_spectators_and_bots(console):  # noqa: ANN001
    """It scrambled `clients.getList()` — everybody the bot knew about — so anybody watching was put
    on a side."""
    plugin = _plugin(console)
    _client(console, "A", team="red")
    _client(console, "B", team="blue")
    _client(console, "C", team="red")
    _client(console, "Watcher", team="spec")
    bot = _client(console, "Bot", team="blue")
    bot.is_bot = True

    assert sorted(c.name for c in plugin.scramble_order()) == ["A", "B", "C"]


def test_two_players_are_already_one_each(console):  # noqa: ANN001
    plugin = _plugin(console)
    _client(console, "A", team="red")
    _client(console, "B", team="blue")

    assert plugin.scramble() is False
    assert console.said == []


# -- the reserved spectator slots ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reserved_slot_is_read_changed_and_written_back(console):
    """Three commands, as the classic sent: the list lives on the server's disk, so a change that is
    not saved is lost at the next restart."""
    _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob")

    await _run(console, boss, "!reserveslot Bob")

    assert console.rcon_sent == [
        "reservedSpectateSlots.load",
        'reservedSpectateSlots.addPlayer "Bob"',
        "reservedSpectateSlots.save",
    ]
    assert "may use a reserved slot now" in _last(console, boss)
    assert (bob, "you may use the reserved slots now") in console.told


@pytest.mark.asyncio
async def test_a_player_who_already_has_one_is_told_so(console):
    """`err.message == ['PlayerAlreadyInList']` compares a *string* to a list, so this answer could
    never be given and the admin got the raw refusal instead."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob")
    console.rcon_words_replies['reservedSpectateSlots.addPlayer "Bob"'] = ["PlayerAlreadyInList"]

    await _run(console, boss, "!reserveslot Bob")

    assert "already has a reserved slot" in _last(console, boss)
    # And the list is not written back, since nothing changed.
    assert "reservedSpectateSlots.save" not in console.rcon_sent


@pytest.mark.asyncio
async def test_taking_away_a_slot_nobody_had(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob")
    console.rcon_words_replies['reservedSpectateSlots.removePlayer "Bob"'] = ["PlayerNotInList"]

    await _run(console, boss, "!unreserveslot Bob")

    assert "had no reserved slot" in _last(console, boss)


@pytest.mark.asyncio
async def test_any_other_refusal_is_repeated_to_the_admin(console):
    """A refusal is a reply on this engine, not an exception — so it can be said out loud."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob")
    console.rcon_words_replies['reservedSpectateSlots.addPlayer "Bob"'] = ["InvalidArguments"]

    await _run(console, boss, "!reserveslot Bob")

    assert "invalidarguments" in _last(console, boss).lower()


# -- match mode ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_match_readies_up_counts_down_and_restarts_the_round(console):
    plugin = _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob")

    await _run(console, boss, "!match on")
    assert plugin.match_mode is True
    assert "type !ready" in console.said[-1]

    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()
    assert "waiting for" in console.said[-1]

    await _run(console, boss, "!ready")
    await _run(console, bob, "!ready")
    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()
    assert "counting down" in console.said[-1]

    for expected in range(COUNTDOWN_FROM, 0, -1):
        console.clock.advance(1.0)
        plugin._run_pending()
        assert console.said[-1] == f"match starting in {expected}"

    console.clock.advance(1.0)
    plugin._run_pending()

    assert console.said[-1] == "fight!"
    assert console.server_verbs_applied == [("round_restart", {})]
    # Chat throughout, because this title has no centre-screen message to use.
    assert console.said_big == []


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

    await _run(console, boss, "!match on")
    assert (spree.on, tk.on) == (False, False)

    await _run(console, boss, "!match off")
    assert (spree.on, tk.on) == (True, False)


@pytest.mark.asyncio
async def test_a_match_costs_the_server_nothing_when_it_ends(console):
    """`!match on` set the balancer's own setting to off and `!match off` never set it back."""
    plugin = _plugin(console, teambalance=True)
    boss = _boss(console)

    await _run(console, boss, "!match on")
    assert plugin.holding_off() is True

    await _run(console, boss, "!match off")
    console.clock.advance(QUIET_SECONDS + 1)

    assert plugin.settings["teambalance"] is True
    assert plugin.holding_off() is False


@pytest.mark.asyncio
async def test_ready_outside_a_match_says_there_is_nothing_to_be_ready_for(console):
    _plugin(console)
    bob = _client(console, "Bob")

    await _run(console, bob, "!ready")

    assert "no match" in _last(console, bob)


# -- config ----------------------------------------------------------------------------------------


def test_a_scrambler_mode_of_off_written_as_yaml_is_still_a_word(console):  # noqa: ANN001
    """`mode: off` in YAML is the boolean False, which matches none of off/round/map."""
    plugin = _plugin(console, scrambler={"mode": False, "strategy": "score"})

    assert plugin.scrambler["mode"] == "off"
    assert plugin.scrambler["strategy"] == "score"


def test_junk_in_the_scrambler_config_is_reported_and_ignored(console, caplog):  # noqa: ANN001
    with caplog.at_level("WARNING"):
        plugin = _plugin(console, scrambler={"mode": "sometimes", "strategy": "vibes"})

    assert (plugin.scrambler["mode"], plugin.scrambler["strategy"]) == ("off", "random")
    assert "sometimes" in caplog.text


def test_the_difference_and_the_interval_keep_the_classics_bounds(console):  # noqa: ANN001
    plugin = _plugin(console, max_difference=0, check_interval=600)
    assert (plugin.settings["max_difference"], plugin.settings["check_interval"]) == (1, 59)

    plugin = _plugin(console, max_difference=99)
    assert plugin.settings["max_difference"] == 9


# -- it is a Medal of Honor plugin -----------------------------------------------------------------


def test_the_loader_refuses_it_on_any_other_title(console):
    from b3.config.schema import Config, PluginEntry, ServerConfig
    from b3.core.pluginmgr import load_plugins

    loaded = load_plugins(
        console,
        Config(
            server=ServerConfig(game="mohw"),
            plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminmoh")],
        ),
    )
    plugin = next(item for item in loaded if item.name == "poweradminmoh")

    assert plugin.enabled is False
    assert "does not support the 'mohw' parser" in plugin.reason


def test_every_verb_this_plugin_uses_is_one_the_title_declares():
    assert MOH_SERVER_VERBS <= set(MOH.server_verbs)
    assert MOH_PLAYER_VERBS <= set(MOH.player_verbs)


# -- through a real bot ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_move_reaches_a_real_medal_of_honor_server(tmp_path):
    """The whole way through: a real `Bot` on the moh profile, a push line from the engine, and the
    three-argument move that comes back out. A fourth argument is what the classic sent."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            return "MOH 1.0" if cmd == "version" else "OK"

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="moh"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminmoh")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = PoweradminmohPlugin(bot, None)
    bot.add_plugin(plugin, "poweradminmoh")
    bot.start()
    admin.start()
    plugin.start()

    guid = "EA_00000000000000000000000000000001"
    await bot.replay(
        [
            json.dumps(["player.onAuthenticated", "Boss", guid]),
            json.dumps(["player.onTeamChange", "Boss", "1", "0"]),
            json.dumps(["player.onAuthenticated", "Bob", guid.replace("1", "2")]),
            json.dumps(["player.onTeamChange", "Bob", "1", "0"]),
        ]
    )
    await bot.bus.drain()
    bot.clients.get_by_cid("Boss").group_bits = 128
    rcon.commands.clear()

    await bot.replay([json.dumps(["player.onChat", "Boss", "!changeteam Bob", "all"])])
    await bot.bus.drain()

    moved = [cmd for cmd in rcon.commands if cmd.startswith("admin.movePlayer")]
    assert moved == ['admin.movePlayer "Bob" 2 true']
    bot.storage.close()
