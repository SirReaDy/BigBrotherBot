"""The `poweradminbfbc2` plugin — Bad Company 2's teams, playlists, yells and match mode.

No captured tests in the classic tree for this one, so the source and the shipped `.ini` are the
whole description. What the source says, read closely, is that **the team balancer never balanced
anything**: `getTeams()` appended both teams to the same list, so team 2 was always empty and the
gap was always the size of team 1; and `teambalance()` then asked a *bool* for `.value`
(`c.isvar(...)` returns True or False) and raised `AttributeError` — after announcing "Autobalancing
Teams!" on everybody's screen. Three of the first tests here are that announcement being followed by
an actual move.

The other half of the plugin was quieter about it. Four of the five `!payell` commands sent ordinary
chat, `!pachangeteam` compared an `int` team id to the string `"1"` and so moved everybody to team 2
while reporting success, and a match cost the server its team balancer permanently.
"""

from __future__ import annotations

import json

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.core.matchmode import COUNTDOWN_FROM, NAG_SECONDS
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.poweradminbfbc2 import (
    ANNOUNCE_SECONDS,
    MS,
    PLAYLISTS,
    QUIET_SECONDS,
    Poweradminbfbc2Plugin,
)

BFBC2_SERVER_VERBS = {"cyclemap", "map_restart", "exec", "yell"}
BFBC2_PLAYER_VERBS = {"kill", "move", "yell_player"}


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    # What a real Bad Company 2 console has, from the title's profile.
    console.server_verbs = set(BFBC2_SERVER_VERBS)
    console.player_verbs = set(BFBC2_PLAYER_VERBS)
    plugin = Poweradminbfbc2Plugin(console, {"settings": settings} if settings else None)
    plugin.start()
    # The plugin holds its automatic check down for a minute after startup, as the classic did.
    console.clock.advance(QUIET_SECONDS + 1)
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
    # The fake matches a handle exactly, so both spellings an admin might type are registered.
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


def _yells(console):  # noqa: ANN001, ANN202
    return [(c.name, v["text"], v["ms"]) for n, c, v in console.verbs_applied if n == "yell_player"]


# -- the balancer that never balanced --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_two_teams_are_counted_separately(console):
    """`getTeams()` appended team 2's players to team 1's list, so the second team was always empty
    and three-a-side looked like six against nobody — permanently "unbalanced"."""
    plugin = _plugin(console, teambalance=True)
    boss = _boss(console)
    for name in ("A", "B"):
        _client(console, name, team="red")
    for name in ("C", "D", "E"):
        _client(console, name, team="blue")

    assert plugin.team_counts() == {"1": 3, "2": 3}  # Boss is on red

    await _run(console, boss, "!pateams")

    assert _moves(console) == []
    assert "even" in _last(console, boss)


@pytest.mark.asyncio
async def test_an_announced_balance_actually_moves_somebody(console):
    """The classic said "Autobalancing Teams!" and then raised `AttributeError` on the next line —
    `c.isvar(...)` is a bool and it asked the bool for `.value`. Every balance, on both paths."""
    _plugin(console, teambalance=True)
    boss = _boss(console)
    for name in ("A", "B", "C", "D"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")

    await _run(console, boss, "!pateams")

    # Six on red against one on blue: a gap of five, so two move.
    assert len(_moves(console)) == 2
    assert all(team == "2" for _, team in _moves(console))
    assert "balancing" in console.said[0]


@pytest.mark.asyncio
async def test_the_players_moved_are_the_ones_who_joined_that_team_last(console):
    """The classic sorted the join times ascending and took the front, which is whoever has been on
    that team longest — the opposite end from the player who just made the teams uneven."""
    plugin = _plugin(console, teambalance=True)
    boss = _boss(console)
    veteran = _client(console, "Veteran", team="red")
    plugin._stamp(veteran)
    console.clock.advance(600)
    newcomer = _client(console, "Newcomer", team="red")
    plugin._stamp(newcomer)
    _client(console, "Z", team="blue")

    await _run(console, boss, "!pateams")

    assert [name for name, _ in _moves(console)] == ["Newcomer"]
    assert newcomer.team == "blue"
    assert veteran.team == "red"


@pytest.mark.asyncio
async def test_the_balancer_leaves_the_admins_where_they_are(console):
    """The classic had no exemption at all, so it moved the admin who had just joined to sort the
    teams out."""
    plugin = _plugin(console, teambalance=True, no_balance_level=20)
    boss = _boss(console)  # superadmin, on red
    mod = _client(console, "Mod", team="red", bits=8)
    plugin._stamp(mod)
    console.clock.advance(10)
    ordinary = _client(console, "Ordinary", team="red")
    plugin._stamp(ordinary)
    _client(console, "Z", team="blue")

    await _run(console, boss, "!pateams")

    assert [name for name, _ in _moves(console)] == ["Ordinary"]
    assert boss.team == "red"


@pytest.mark.asyncio
async def test_a_player_who_makes_the_teams_uneven_is_moved_back(console):
    plugin = _plugin(console, teambalance=True)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")
    joiner = _client(console, "Joiner", team="blue")

    joiner.team = "red"
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, data="red", client=joiner))

    assert _moves(console) == [("Joiner", "2")]
    assert "uneven" in _last(console, joiner)
    assert plugin.holding_off() is True  # and the bot's own move does not start a cascade


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


@pytest.mark.asyncio
async def test_the_check_stands_down_for_a_minute_after_a_round_starts(console):
    """A round start is a flurry of team changes, and reading them one at a time means moving half
    the server back again."""
    plugin = _plugin(console, teambalance=True)
    for name in ("A", "B", "C"):
        _client(console, name, team="red")
    _client(console, "Z", team="blue")
    joiner = _client(console, "Joiner", team="red")

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={}))
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, data="red", client=joiner))

    assert _moves(console) == []
    console.clock.advance(QUIET_SECONDS + 1)
    assert plugin.holding_off() is False


@pytest.mark.asyncio
async def test_the_balancer_can_be_switched_on_and_off_in_game(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pateambalance")
    assert "on|off" in _last(console, boss)

    await _run(console, boss, "!pateambalance on")
    assert plugin.settings["teambalance"] is True
    await _run(console, boss, "!pateambalance off")
    assert plugin.settings["teambalance"] is False


# -- which teams this mode has -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_sides_this_mode_has_are_counted(console):
    """The classic `bfbc2.py` parser called team 3 the *spectators*, while its own gametype table
    described Squad Deathmatch as four squads fighting each other. Only somebody with a live server
    can say which is right — and nothing here needs the answer, because outside that one mode 3 is
    not a side either way."""
    plugin = _plugin(console, teambalance=True)
    console.game.gametype = "RUSH"
    _client(console, "A", team="red")
    _client(console, "B", team="blue")
    for name in ("W1", "W2", "W3"):
        _client(console, name, team="green")  # engine team 3

    assert plugin.team_counts() == {"1": 1, "2": 1}
    assert plugin.balance() is False  # not a gap it could ever close


@pytest.mark.asyncio
async def test_squad_deathmatch_really_does_have_four_sides(console):
    plugin = _plugin(console, teambalance=True)
    console.game.gametype = "SQDM"
    _client(console, "A", team="red")
    _client(console, "B", team="blue")
    _client(console, "C", team="green")
    _client(console, "D", team="yellow")

    assert plugin.team_counts() == {"1": 1, "2": 1, "3": 1, "4": 1}


@pytest.mark.asyncio
async def test_the_other_team_rotates_where_there_are_four_of_them(console):
    plugin = _plugin(console)
    console.game.gametype = "SQDM"
    third = _client(console, "Third", team="green")

    assert plugin.other_team(third) == "4"

    console.game.gametype = "CONQUEST"
    assert plugin.other_team(third) == ""  # not a side in this mode, so there is no other one


# -- moving one player -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changeteam_moves_a_player_to_the_team_they_are_not_on(console):
    """`sclient.teamId == '1'` in the classic, against an `int` — never true, so *everybody* was
    moved to team 2, including the players already there, who stayed put while the bot reported
    "forced to the other team"."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Red", team="red")
    _client(console, "Blue", team="blue")

    await _run(console, boss, "!pachangeteam Red")
    await _run(console, boss, "!pachangeteam Blue")

    assert _moves(console) == [("Red", "2"), ("Blue", "1")]


@pytest.mark.asyncio
async def test_changeteam_says_so_when_the_server_has_not_given_a_team(console):
    """Team 0 is the deploy screen, not a team, so there is no other side to move them to."""
    _plugin(console)
    boss = _boss(console)
    _client(console, "Waiting", team="")

    await _run(console, boss, "!pachangeteam Waiting")

    assert _moves(console) == []
    assert "not said which team" in _last(console, boss)


@pytest.mark.asyncio
async def test_spectate_takes_a_player_out_of_the_game(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob", team="red")

    await _run(console, boss, "!paspectate Bob")

    assert _moves(console) == [("Bob", "0")]


@pytest.mark.asyncio
async def test_an_admin_may_not_kill_a_senior_one(console):
    """The classic checked nothing before killing, moving or spectating anybody, so the moderator
    who could not `!kick` a senior admin could `!pakill` them all afternoon."""
    _plugin(console, no_level_check_level=100)
    admin = _client(console, "Admin", bits=32)  # fulladmin, 60 — allowed the command
    _client(console, "Senior", bits=64)  # senioradmin, 80

    await _run(console, admin, "!pakill Senior")

    assert console.verbs_applied == []
    assert "Senior" in _last(console, admin)


@pytest.mark.asyncio
async def test_a_kill_is_announced_with_its_reason(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Bob")

    await _run(console, boss, "!pakill Bob spawn camping")

    assert [(n, c.name) for n, c, _ in console.verbs_applied] == [("kill", "Bob")]
    assert console.said_big == ["Bob was killed by an admin: spawn camping"]


# -- the yells that were not yells -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_team_yell_is_a_yell_and_not_a_chat_line(console):
    """`!payellteam`, `!payellenemy`, `!payellsquad` and `!payellplayer` all called
    `client.message(...)` in the classic, which is an ordinary private chat line. Only `!payell`
    ever reached anybody's screen."""
    _plugin(console)
    boss = _boss(console)  # red
    _client(console, "Mate", team="red")
    _client(console, "Enemy", team="blue")

    await _run(console, boss, "!payellteam hold the flag")

    yelled = _yells(console)
    assert sorted(name for name, _, _ in yelled) == ["Boss", "Mate"]
    assert all(text == "hold the flag" for _, text, _ in yelled)
    assert console.told == []  # nothing was quietly turned into chat


@pytest.mark.asyncio
async def test_the_enemy_yell_reaches_the_other_team_only(console):
    _plugin(console)
    boss = _boss(console)
    _client(console, "Mate", team="red")
    _client(console, "Enemy", team="blue")
    _client(console, "Waiting", team="")  # at the deploy screen, on no team

    await _run(console, boss, "!payellenemy we are coming")

    assert [name for name, _, _ in _yells(console)] == ["Enemy"]


@pytest.mark.asyncio
async def test_the_squad_yell_reaches_one_squad_of_one_team(console):
    _plugin(console)
    boss = _boss(console)
    boss.squad = "2"
    _client(console, "Squadmate", team="red", squad="2")
    _client(console, "OtherSquad", team="red", squad="3")
    _client(console, "SameSquadOtherTeam", team="blue", squad="2")

    await _run(console, boss, "!payellsquad on me")

    assert sorted(name for name, _, _ in _yells(console)) == ["Boss", "Squadmate"]


@pytest.mark.asyncio
async def test_a_yell_duration_goes_out_in_milliseconds(console):
    """This generation counts a yell in milliseconds. The profile was asking for ten of them."""
    _plugin(console, yell_duration=7)
    boss = _boss(console)
    _client(console, "Bob")

    await _run(console, boss, "!payellplayer Bob look out")

    assert _yells(console) == [("Bob", "look out", str(7 * MS))]


@pytest.mark.asyncio
async def test_the_yell_to_everybody_is_the_engines_own_subset(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!payell round starting")

    assert console.server_verbs_applied == [
        ("yell", {"text": "round starting", "ms": str(10 * MS)})
    ]


@pytest.mark.asyncio
async def test_a_yell_with_nothing_to_say_explains_itself(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!payellteam")

    assert "message" in _last(console, boss)
    assert _yells(console) == []


# -- the server's settings -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setting_something_with_no_value_says_how_rather_than_raising(console):
    """`data.split(' ', 1)[1]` in the classic — an `IndexError` on `!paset friendlyFire`."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paset friendlyFire")

    assert console.cvars == {}
    assert "<setting> <value>" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_setting_is_named_with_or_without_its_prefix(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paset friendlyFire true")
    await _run(console, boss, "!paset vars.idleTimeout 300")

    assert console.cvars == {"vars.friendlyFire": "true", "vars.idleTimeout": "300"}


@pytest.mark.asyncio
async def test_a_setting_name_that_could_be_a_second_command_is_refused(console):
    """On this engine the setting's *name* is the command, so a space in it is not a bad value."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, '!paset "friendlyFire;admin.password x" true')

    assert console.cvars == {}
    assert "not a setting name" in _last(console, boss)


@pytest.mark.asyncio
async def test_reading_a_setting_the_server_will_not_answer_for(console):
    """`getCvar(...).value` in the classic, which is an `AttributeError` on `None`."""
    _plugin(console)
    boss = _boss(console)
    console.cvars["vars.friendlyFire"] = "true"

    await _run(console, boss, "!paget friendlyFire")
    assert "vars.friendlyFire is true" in _last(console, boss)

    await _run(console, boss, "!paget somethingElse")
    assert "did not say" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_server_describes_itself_from_what_the_bot_already_knows(console):
    _plugin(console)
    boss = _boss(console)
    console.game.hostname = "A Server"
    console.game.max_players = 32
    console.game.map_name = "Levels/MP_012"
    console.game.gametype = "RUSH"
    console.map_names = {"levels/mp_012": "Port Valdez"}

    await _run(console, boss, "!paserverinfo")

    assert _last(console, boss) == "A Server: 1/32 on Port Valdez, playing RUSH"


@pytest.mark.asyncio
async def test_an_address_is_never_said_out_loud(console):
    """`sayLoudOrPM` in the classic, so `!!paident bob` put somebody's IP in public chat."""
    _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob")
    bob.ip = "10.0.0.9"

    await _run(console, boss, "&paident Bob")

    assert console.said == []
    assert "10.0.0.9" in _last(console, boss)


# -- the map ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_map_restart_is_announced_first_and_sent_from_the_scheduled_pass(console):
    """`time.sleep(1)` in the classic, on the bot's one thread."""
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pamaprestart")

    assert console.said == ["restarting the map"]
    assert console.server_verbs_applied == []

    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("map_restart", {})]


@pytest.mark.asyncio
async def test_reloading_the_map_points_the_rotation_at_it_rather_than_emptying_it(console):
    """The classic cleared the map list, appended the current level, cycled, and then ran
    `mapList.load` — which re-reads the list from the server's disk and so discarded the two
    commands before it, silently replacing any rotation an admin had built."""
    plugin = _plugin(console)
    boss = _boss(console)
    console.maps = ["MP_001", "MP_012", "MP_013"]
    console.game.map_name = "MP_012"

    await _run(console, boss, "!pamapreload")
    console.clock.advance(ANNOUNCE_SECONDS)
    plugin._run_pending()

    assert console.rcon_sent == ["mapList.nextLevelIndex 1"]
    assert "mapList.clear" not in console.rcon_sent
    assert console.server_verbs_applied == [("cyclemap", {})]


@pytest.mark.asyncio
async def test_the_map_list_command_takes_no_filename(console):
    """`write(('mapList.load %s' % data))` is a *string*, not a one-tuple, so the whole thing went
    out as a single word. `mapList.load` reads the server's own list and takes no argument anyway."""
    _plugin(console)
    boss = _boss(console)
    console.maps = ["MP_001", "MP_012"]

    await _run(console, boss, "!pamaplist rotation.txt")
    assert console.rcon_sent == []
    assert "no filename" in _last(console, boss)

    await _run(console, boss, "!pamaplist")
    assert console.rcon_sent[0] == "mapList.load"
    assert "2 map(s)" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_next_map_is_an_index_into_the_flat_rotation(console):
    """Frostbite 1 keeps a list of level names and the next map is an index into it."""
    _plugin(console)
    boss = _boss(console)
    console.maps = ["Levels/MP_001", "Levels/MP_012", "Levels/MP_013"]
    console.map_names = {"levels/mp_012": "Port Valdez"}

    await _run(console, boss, "!pasetnextmap valdez")

    assert console.rcon_sent == ["mapList.nextLevelIndex 1"]
    assert "Port Valdez" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_map_the_rotation_does_not_hold_is_inserted_and_pointed_at(console):
    """With no rotation to match against, what the admin typed is the map — the same rule `!map`
    follows. It has to be put into the list before an index can point at it."""
    _plugin(console)
    boss = _boss(console)
    console.maps = []
    console.rcon_words_replies["mapList.nextLevelIndex"] = ["1"]

    await _run(console, boss, "!pasetnextmap Levels/MP_012")

    assert console.rcon_sent == [
        "mapList.nextLevelIndex",
        'mapList.insert 1 "Levels/MP_012"',
        "mapList.nextLevelIndex 1",
    ]


@pytest.mark.asyncio
async def test_a_map_nobody_has_is_refused_with_a_few_that_exist(console):
    _plugin(console)
    boss = _boss(console)
    console.maps = ["Levels/MP_001", "Levels/MP_012"]

    await _run(console, boss, "!pasetnextmap atlantis")

    assert console.rcon_sent == []
    assert "no map like atlantis" in _last(console, boss)


# -- the playlists ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_playlist_command_sends_its_own_playlist(console):
    _plugin(console)
    boss = _boss(console)

    for command, playlist in PLAYLISTS.items():
        console.rcon_sent.clear()
        await _run(console, boss, f"!{command}")
        assert console.rcon_sent == [f"admin.setPlaylist {playlist}"]
        assert playlist in _last(console, boss)


@pytest.mark.asyncio
async def test_a_playlist_the_server_refuses_is_reported(console):
    """This engine answers a command it refused, and the classic threw the answer away."""
    _plugin(console)
    boss = _boss(console)
    console.rcon_words_replies["admin.setPlaylist SQDM"] = ["InvalidArguments"]

    await _run(console, boss, "!pasqdm")

    assert "would not set" in _last(console, boss)


# -- running things on the server ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_script_is_run_by_name_and_nothing_else_is(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!runscript match.cfg")
    assert console.server_verbs_applied == [("exec", {"file": "match.cfg"})]

    await _run(console, boss, "!runscript match.cfg; admin.password x")
    assert len(console.server_verbs_applied) == 1
    assert "not a config file" in _last(console, boss)


# -- match mode ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_match_readies_up_counts_down_and_restarts_the_map(console):
    plugin = _plugin(console)
    boss = _boss(console)
    bob = _client(console, "Bob")

    await _run(console, boss, "!pamatch on")
    assert plugin.match_mode is True
    assert "type !ready" in console.said_big[-1]

    # Nobody has readied up yet, so the first pass nags both of them and names them.
    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()
    assert sorted(name for name, _, _ in _yells(console)) == ["Bob", "Boss"]
    assert "waiting for" in console.said[-1]

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
    assert console.server_verbs_applied == [("map_restart", {})]
    assert plugin.ready_up.running() is False


@pytest.mark.asyncio
async def test_a_countdown_does_not_start_on_an_empty_server(console):
    """The classic found nobody unready, concluded everybody was ready, and counted an empty server
    down into a round restart. Nobody is not everybody."""
    plugin = _plugin(console)
    boss = _boss(console)
    await _run(console, boss, "!pamatch on")
    console.clients.remove(boss.cid)

    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()

    assert plugin.ready_up.counting_down() is False
    assert console.server_verbs_applied == []


@pytest.mark.asyncio
async def test_nobody_may_un_ready_once_the_countdown_has_started(console):
    plugin = _plugin(console)
    boss = _boss(console)
    await _run(console, boss, "!pamatch on")
    await _run(console, boss, "!ready")
    console.clock.advance(NAG_SECONDS)
    plugin._run_pending()

    await _run(console, boss, "!ready")

    assert "cannot change your mind" in _last(console, boss)
    assert plugin.ready_up.is_ready(boss) is True


@pytest.mark.asyncio
async def test_ready_outside_a_match_says_there_is_nothing_to_be_ready_for(console):
    """The classic registered `!ready` when a match started and deleted it afterwards by reaching
    into the admin plugin's command table; a `!match off` that raised first left it behind."""
    _plugin(console)
    bob = _client(console, "Bob")

    await _run(console, bob, "!ready")

    assert "no match" in _last(console, bob)


@pytest.mark.asyncio
async def test_a_match_only_switches_back_on_the_plugins_it_switched_off(console):
    """The classic re-enabled every plugin on its list, so one the operator had deliberately
    switched off came back on because a match had happened."""
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
    """`!pamatch on` set the team balancer's own setting to off and `!pamatch off` never set it
    back, so a match cost the server its balancer until the bot was restarted. It is suspended for
    the duration here instead."""
    plugin = _plugin(console, teambalance=True)
    boss = _boss(console)

    await _run(console, boss, "!pamatch on")
    assert plugin.holding_off() is True
    assert plugin.settings["teambalance"] is True

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


# -- it is a Bad Company 2 plugin ------------------------------------------------------------------


def test_the_loader_refuses_it_on_any_other_title(console):
    from b3.config.schema import Config, PluginEntry, ServerConfig
    from b3.core.pluginmgr import load_plugins

    loaded = load_plugins(
        console,
        Config(
            server=ServerConfig(game="bf3"),
            plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminbfbc2")],
        ),
    )
    plugin = next(item for item in loaded if item.name == "poweradminbfbc2")

    assert plugin.enabled is False
    assert "does not support the 'bf3' parser" in plugin.reason


def test_every_verb_this_plugin_uses_is_one_the_title_declares():
    """So a command can ask before it is offered, rather than sending a verb into the dark. Neither
    Frostbite 1 title had a single one of these until this plugin was written."""
    from b3.parsers.frostbite.profiles import BFBC2

    assert BFBC2_SERVER_VERBS <= set(BFBC2.server_verbs)
    assert BFBC2_PLAYER_VERBS <= set(BFBC2.player_verbs)


# -- through a real bot ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_yell_reaches_a_real_bad_company_2_server(tmp_path):
    """Everything at once: a real `Bot` on the bfbc2 profile, a push line from the engine, and the
    verb that goes back out — with its duration in the milliseconds this generation counts in."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.parsers.frostbite.profiles import BFBC2_R9
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            # The bot refuses to read a server that says it is running a different title.
            return f"BFBC2 {BFBC2_R9}" if cmd == "version" else "OK"

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="bfbc2"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradminbfbc2")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = Poweradminbfbc2Plugin(bot, None)
    bot.add_plugin(plugin, "poweradminbfbc2")
    bot.start()
    admin.start()
    plugin.start()

    guid = "EA_00000000000000000000000000000001"
    await bot.replay([json.dumps(["player.onAuthenticated", "Boss", guid])])
    await bot.bus.drain()
    bot.clients.get_by_cid("Boss").group_bits = 128
    rcon.commands.clear()

    await bot.replay(
        [json.dumps(["player.onChat", "Boss", "!payellplayer Boss get to the flag", "all"])]
    )
    await bot.bus.drain()

    yelled = [cmd for cmd in rcon.commands if cmd.startswith("admin.yell")]
    assert yelled == ['admin.yell "get to the flag" 10000 player "Boss"']
    bot.storage.close()
