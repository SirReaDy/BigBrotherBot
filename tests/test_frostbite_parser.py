"""The events a Frostbite server pushes, and the six Battlefield / Medal of Honor profiles.

Sample word lists come from the docstrings of the classic ``frostbite/abstractParser.py`` and
``frostbite2/abstractParser.py``, captured from real servers. Not verified against a live Battlefield
server; see TODO.md §2.1.

The identity tests matter most. On this engine a player's *name* is their handle — `admin.kickPlayer`
takes a name, not a slot — while bans are by EA GUID, which arrives in a separate event. Getting that
split wrong means either kicking nobody or banning the wrong person.
"""

from __future__ import annotations

import json

import pytest

from b3.core.events import EventType
from b3.domain.client import Client
from b3.parsers.frostbite.parser import FbParser, KillData
from b3.parsers.frostbite.profiles import BF3, BF4, BFBC2, BFH, MOH, MOHW
from b3.parsers.frostbite.status import parse_ban_list, parse_player_block
from b3.parsers.games import PROFILES, PUSH_FAMILIES, parser_for

GUID = "EA_00000000000000000000000000000001"
GUID2 = "EA_00000000000000000000000000000002"


@pytest.fixture
def parser() -> FbParser:
    return FbParser(BF3)


def feed(p: FbParser, words: list[str]):  # noqa: ANN201
    """Events reach the parser as JSON, which is how a word list survives a line-shaped contract."""
    return p.parse_line(json.dumps(words))


def one(p: FbParser, words: list[str]):  # noqa: ANN201
    events = feed(p, words)
    assert len(events) == 1, f"expected 1 event from {words!r}, got {events!r}"
    return events[0]


def _with_players(profile=BF3) -> FbParser:  # noqa: ANN001
    p = FbParser(profile)
    p.clients.add(Client(cid="Bravo17", name="Bravo17", guid=GUID, team="red"))
    p.clients.add(Client(cid="Bob", name="Bob", guid=GUID2, team="blue"))
    return p


# -- identity --------------------------------------------------------------


def test_authentication_is_the_join(parser):  # noqa: ANN001
    """Before the GUID lands there is no identity to match a ban against."""
    ev = one(parser, ["player.onAuthenticated", "Bravo17", GUID])

    assert ev.type is EventType.CLIENT_JOIN
    client = parser.clients.get_by_cid("Bravo17")
    assert client.guid == GUID
    assert client.cid == "Bravo17"  # the name is the handle: `admin.kickPlayer` takes it


def test_a_join_is_only_a_connection(parser):  # noqa: ANN001
    """The classic parser ignored this event: it arrives before the game client has really connected,
    and one that then fails to connect never produces a matching leave."""
    ev = one(parser, ["player.onJoin", "Bravo17", GUID])
    assert ev.type is EventType.CLIENT_CONNECT


def test_leaving_removes_the_player(parser):  # noqa: ANN001
    feed(parser, ["player.onAuthenticated", "Bravo17", GUID])
    ev = one(parser, ["player.onLeave", "Bravo17", "0", "name", "0"])

    assert ev.type is EventType.CLIENT_DISCONNECT
    assert parser.clients.get_by_cid("Bravo17") is None


def test_a_leave_for_somebody_unknown_is_not_invented(parser):  # noqa: ANN001
    assert feed(parser, ["player.onLeave", "Ghost"]) == []


def test_a_kick_carries_its_reason():
    p = _with_players()
    ev = one(p, ["player.onKicked", "Bob", "idle for too long"])
    assert ev.type is EventType.CLIENT_DISCONNECT
    assert ev.data == "idle for too long"
    assert p.clients.get_by_cid("Bob") is None


def test_the_server_is_never_a_player(parser):  # noqa: ANN001
    """`Server` speaks on the chat event — including the bot's own output coming back."""
    assert feed(parser, ["player.onChat", "Server", "b3: Bob was kicked", "all"]) == []
    assert parser.clients.get_by_cid("Server") is None


# -- chat ------------------------------------------------------------------


def test_public_chat():
    p = _with_players()
    ev = one(p, ["player.onChat", "Bravo17", "hello b3", "all"])
    assert ev.type is EventType.CLIENT_SAY
    assert ev.data == "hello b3"
    assert ev.client.cid == "Bravo17"


@pytest.mark.parametrize("target", ["team 1", "squad 1 2", "player Bob"])
def test_restricted_chat_is_team_chat(target):  # noqa: ANN001
    p = _with_players()
    ev = one(p, ["player.onChat", "Bravo17", "on my six", target])
    assert ev.type is EventType.CLIENT_TEAM_SAY
    assert ev.extra["target"] == target


def test_chat_with_no_target_is_public():
    p = _with_players()
    assert one(p, ["player.onChat", "Bravo17", "hi"]).type is EventType.CLIENT_SAY


def test_chat_from_an_unseen_name_creates_the_player():
    """Unlike Quake3, the name *is* the id here, so a first sighting is a usable identity."""
    p = FbParser(BF3)
    ev = one(p, ["player.onChat", "Newcomer", "hello", "all"])
    assert ev.client.cid == "Newcomer"


def test_a_command_typed_in_chat_survives_intact():
    """The classic parser stripped the last word off a command line to undo its own mangling."""
    p = _with_players()
    ev = one(p, ["player.onChat", "Bravo17", "!ban Bob cheating in the tank", "all"])
    assert ev.data == "!ban Bob cheating in the tank"


# -- combat ----------------------------------------------------------------


def test_a_kill():
    p = _with_players()
    ev = one(p, ["player.onKill", "Bravo17", "Bob", "M67", "false"])

    assert ev.type is EventType.CLIENT_KILL
    assert ev.client.cid == "Bravo17" and ev.target.cid == "Bob"
    assert ev.data == KillData(weapon="M67", headshot=False)


def test_a_headshot_is_recorded():
    p = _with_players()
    ev = one(p, ["player.onKill", "Bravo17", "Bob", "M416", "true"])
    assert ev.data.headshot is True


def test_a_team_kill():
    p = FbParser(BF3)
    p.clients.add(Client(cid="A", name="A", guid=GUID, team="red"))
    p.clients.add(Client(cid="B", name="B", guid=GUID2, team="red"))
    assert one(p, ["player.onKill", "A", "B", "M67", "false"]).type is EventType.CLIENT_KILL_TEAM


def test_players_with_no_team_yet_are_not_team_mates():
    """Team 0 means "still at the deploy screen". Treating it as a team makes every early kill a TK."""
    p = FbParser(BF3)
    p.clients.add(Client(cid="A", name="A", guid=GUID, team=""))
    p.clients.add(Client(cid="B", name="B", guid=GUID2, team=""))
    assert one(p, ["player.onKill", "A", "B", "M67", "false"]).type is EventType.CLIENT_KILL


def test_killing_yourself():
    p = _with_players()
    assert one(p, ["player.onKill", "Bob", "Bob", "M67", "false"]).type is EventType.CLIENT_SUICIDE


def test_a_kill_with_no_killer_is_a_suicide():
    """`['', 'Bob', 'DamageArea', 'false']` — a fall, a fire, the server. Not a player called ""."""
    p = _with_players()
    ev = one(p, ["player.onKill", "", "Bob", "DamageArea", "false"])
    assert ev.type is EventType.CLIENT_SUICIDE
    assert ev.client.cid == "Bob"
    assert p.clients.get_by_cid("") is None


def test_a_kill_by_the_server_is_a_suicide_too():
    p = _with_players()
    ev = one(p, ["player.onKill", "Server", "Bob", "Death", "false"])
    assert ev.type is EventType.CLIENT_SUICIDE


# -- teams and spawning ----------------------------------------------------


def test_a_team_change():
    p = _with_players()
    ev = one(p, ["player.onTeamChange", "Bob", "1", "0"])
    assert ev.type is EventType.CLIENT_TEAM_CHANGE
    assert p.clients.get_by_cid("Bob").team == "red"


def test_a_squad_change_also_states_the_team():
    p = _with_players()
    one(p, ["player.onSquadChange", "Bob", "2", "3"])
    assert p.clients.get_by_cid("Bob").team == "blue"


def test_the_squad_is_kept_and_not_only_the_team():
    """It was dropped before, and on this engine it is not decoration: a squad is four players who
    spawn on each other, so swapping two players between teams has to put each into the *other's*
    squad or they both land in "no squad" and the swap is half done."""
    p = _with_players()

    one(p, ["player.onSquadChange", "Bob", "2", "3"])

    assert p.clients.get_by_cid("Bob").squad == "3"


def test_a_team_change_with_no_squad_leaves_the_squad_alone():
    p = _with_players()
    one(p, ["player.onSquadChange", "Bob", "2", "3"])

    one(p, ["player.onTeamChange", "Bob", "1"])

    assert p.clients.get_by_cid("Bob").team == "red"
    assert p.clients.get_by_cid("Bob").squad == "3"


# -- the end-of-round scoreboard -------------------------------------------


def test_the_final_scoreboard_arrives_as_an_event():
    """`server.onRoundOverPlayers` carries the same block the roster does, in the window between a
    round ending and the next map loading. It is the only moment the bot is told what each player
    did, which is the only thing that lets a team scrambler be fair rather than random."""
    p = _with_players()

    ev = one(
        p,
        [
            "server.onRoundOverPlayers",
            "4",
            "name",
            "teamId",
            "squadId",
            "score",
            "2",
            "Bravo17",
            "1",
            "0",
            "1500",
            "Bob",
            "2",
            "3",
            "220",
        ],
    )

    assert ev.type is EventType.GAME_ROUND_PLAYER_SCORES
    assert [(row.name, row.team, row.squad, row.score) for row in ev.data] == [
        ("Bravo17", "1", "0", 1500),
        ("Bob", "2", "3", 220),
    ]


def test_the_team_totals_arrive_too():
    p = _with_players()

    ev = one(p, ["server.onRoundOverTeamScores", "2", "300", "271", "300"])

    assert ev.type is EventType.GAME_ROUND_TEAM_SCORES
    assert ev.data == ["300", "271"]  # the target score on the end is not a team


def test_a_truncated_team_score_list_is_reported_rather_than_half_read():
    p = _with_players()
    assert feed(p, ["server.onRoundOverTeamScores", "4", "300"]) == []
    assert feed(p, ["server.onRoundOverTeamScores"]) == []


def test_an_empty_scoreboard_publishes_nothing():
    p = _with_players()
    assert feed(p, ["server.onRoundOverPlayers", "0", "0"]) == []


def test_spawning_sets_the_team_on_frostbite_2():
    p = _with_players()
    ev = one(p, ["player.onSpawn", "Bob", "1"])
    assert ev.type is EventType.CLIENT_SPAWN
    assert p.clients.get_by_cid("Bob").team == "red"


def test_spawning_on_frostbite_1_reports_a_kit_not_a_team():
    """BC2 puts the kit here. Reading it as a team would relabel everyone as "assault"."""
    p = _with_players(BFBC2)
    before = p.clients.get_by_cid("Bob").team
    one(p, ["player.onSpawn", "Bob", "assault", "gadget", "pistol"])
    assert p.clients.get_by_cid("Bob").team == before


# -- the round -------------------------------------------------------------


def test_a_new_level_reads_like_a_round_start(parser):  # noqa: ANN001
    """Published with the same payload shape the other families use, so `Game` needs no special case."""
    ev = one(parser, ["server.onLevelLoaded", "MP_001", "ConquestLarge0", "1", "2"])
    assert ev.type is EventType.GAME_ROUND_START
    assert ev.data["mapname"] == "MP_001"
    assert ev.data["g_gametype"] == "ConquestLarge0"


def test_the_end_of_a_round(parser):  # noqa: ANN001
    ev = one(parser, ["server.onRoundOver", "1"])
    assert ev.type is EventType.GAME_ROUND_END


def test_a_punkbuster_message_is_passed_through_not_dropped(parser):  # noqa: ANN001
    ev = one(parser, ["punkBuster.onMessage", "PunkBuster Server: Player GUID Computed abc"])
    assert ev.type is EventType.CUSTOM
    assert ev.extra["kind"] == "punkbuster"


# -- robustness ------------------------------------------------------------


def test_an_unknown_event_is_ignored_quietly(parser):  # noqa: ANN001
    """Battlefield servers emit plenty this bot has no use for."""
    assert feed(parser, ["vars.somethingNobodyPortedYet", "true"]) == []


def test_a_truncated_event_does_not_raise(parser):  # noqa: ANN001
    assert feed(parser, ["player.onKill"]) == []
    assert feed(parser, ["player.onChat", "Bravo17"]) == []
    assert feed(parser, []) == []


def test_a_line_that_is_not_json_is_reported_not_raised(parser):  # noqa: ANN001
    assert parser.parse_line("player.onChat Bravo17 hello") == []
    assert parser.parse_line("") == []


def test_json_that_is_not_a_word_list_is_refused(parser):  # noqa: ANN001
    assert parser.parse_line('{"player.onChat": "Bravo17"}') == []
    assert parser.parse_line("[1, 2, 3]") == []


# -- the player block ------------------------------------------------------


def test_reading_a_player_block():
    players = list(
        parse_player_block(
            ["3", "name", "guid", "score", "2", "Bravo17", GUID, "1500", "Bob", GUID2, "20"]
        )
    )
    assert [(p.name, p.guid, p.score) for p in players] == [
        ("Bravo17", GUID, 1500),
        ("Bob", GUID2, 20),
    ]


def test_an_empty_player_block():
    assert list(parse_player_block(["3", "name", "guid", "score", "0"])) == []
    assert list(parse_player_block([])) == []


def test_a_block_cut_short_yields_what_arrived():
    """It says two players and delivers one and a half; the first must still come through."""
    players = list(parse_player_block(["2", "name", "guid", "2", "Bravo17", GUID, "Bob"]))
    assert [p.name for p in players] == ["Bravo17"]


def test_a_malformed_header_is_survivable():
    assert list(parse_player_block(["notanumber", "name"])) == []


def test_a_row_with_no_name_identifies_nobody():
    assert list(parse_player_block(["2", "name", "guid", "1", "", GUID])) == []


def test_no_ip_is_reported_and_none_is_invented():
    """Frostbite does not tell an admin tool where a player connects from. PunkBuster does."""
    (player,) = parse_player_block(["2", "name", "guid", "1", "Bravo17", GUID])
    assert player.ip == ""


def test_a_column_this_bot_does_not_read_is_ignored_rather_than_guessed_at():
    """A newer title adding a column must not break the parse — and `type`, which is how BF4 and
    Battlefield Hardline report a spectator, is exactly such a column. It is named in
    `UNREAD_FIELDS` rather than silently absent, because that is the evidence against the classic
    parsers' claim that team 3 means "spectator" on this family."""
    from b3.parsers.frostbite.status import UNREAD_FIELDS

    players = list(
        parse_player_block(
            # fmt: off
            [
                "5",
                "name",
                "guid",
                "teamId",
                "squadId",
                "type",
                "1",
                "Watcher",
                GUID,
                "0",
                "0",
                "1",
            ]
            # fmt: on
        )
    )

    assert [(p.name, p.team, p.squad) for p in players] == [("Watcher", "0", "0")]
    assert "type" in UNREAD_FIELDS


def test_reading_a_ban_list():
    bans = parse_ban_list(
        ["guid", GUID, "perm", "0", "cheating", "ip", "1.2.3.4", "rounds", "5", "x"]
    )
    assert bans[0] == {
        "id_type": "guid",
        "target": GUID,
        "ban_type": "perm",
        "amount": "0",
        "reason": "cheating",
    }
    assert bans[1]["id_type"] == "ip"


def test_a_ban_list_that_does_not_divide_evenly_is_refused():
    """Misaligning it would attribute every ban after the fault to the wrong player."""
    assert parse_ban_list(["guid", GUID, "perm"]) == []


# -- profiles and wiring ---------------------------------------------------


@pytest.mark.parametrize("game", ["bf3", "bf4", "bfbc2", "bfh", "moh", "mohw"])
def test_every_title_is_configurable(game):  # noqa: ANN001
    profile = PROFILES[game]
    assert profile.family == "frostbite"
    assert isinstance(parser_for(profile), FbParser)
    assert profile.family in PUSH_FAMILIES  # so the CLI builds one object for both halves


def test_the_titles_differ_in_nothing_but_the_map_list():
    """Both protocol generations take the same verbs for chat, kicks and bans — that is what makes
    one profile serve six titles, and it is worth a test because it is the family's whole premise.

    **The map list is the exception, and it was found the hard way.** Frostbite 1 (Bad Company 2, the
    2010 Medal of Honor) keeps a flat list of level names and ends a round with `admin.runNextRound`;
    Frostbite 2 keeps (map, gamemode, rounds) entries and uses `mapList.runNextRound`. This test used
    to assert the six were identical in every field, and they were — with the Frostbite 2 rotate verb
    on all six, so `!maprotate` was an unknown command on the two Frostbite 1 titles, and with a map
    template that was the Frostbite 1 rotate verb plus an argument it does not take, so `!map` loaded
    nothing anywhere. Asserting sameness is what made both invisible.

    `map_names` and `version_check` are excluded because both are per-title data by definition —
    the second is the title *saying which title it is*, so it could not be shared even in principle.
    """
    from dataclasses import replace

    same_as_bf3 = {
        "map_names": BF3.map_names,
        "name": "bf3",
        "version_check": BF3.version_check,
    }
    for profile in (BF4, BFH, MOHW):
        assert replace(profile, **same_as_bf3) == BF3

    for profile in (BFBC2, MOH):
        # Everything but the map list, the round control and the two things the 2010 Medal of Honor
        # turned out not to share with anybody — named here one by one rather than papered over,
        # since the docstring above is about what asserting sameness cost the last time.
        assert (
            replace(
                profile,
                **same_as_bf3,
                rotate_command=BF3.rotate_command,
                map_arguments=BF3.map_arguments,
                saybig_template=BF3.saybig_template,
                server_verbs=BF3.server_verbs,
                player_verbs=BF3.player_verbs,
                teams=BF3.teams,
            )
            == BF3
        )
        assert profile.map_arguments == ()
        # And the two titles of this generation do not even agree with each other: Bad Company 2
        # moves the game on in levels and the 2010 Medal of Honor in rounds. One profile carrying
        # Medal of Honor's for both is what `poweradminbfbc2` found.
        assert BFBC2.rotate_command == "admin.runNextLevel"
        assert MOH.rotate_command == "admin.runNextRound"


def test_the_2010_medal_of_honor_shares_two_fewer_things_than_the_rest():
    """Both found by `poweradminmoh`, and both invisible from the plugin's side.

    Its `admin.movePlayer` takes **three** arguments where the other five titles take four — the
    classic's own changelog calls fixing that "a major fix … this affected all team balancing
    features", which is what a refused move costs. And its team 3 is the **spectators**, which its
    own parser says and which is safe to state for this title alone: it has no four-sided gamemode
    for teams 3 and 4 to be playing sides in, where Bad Company 2's Squad Deathmatch does.
    """
    assert MOH.player_verbs["move"] == 'admin.movePlayer "%(cid)s" %(team)s true'
    assert BFBC2.player_verbs["move"] == 'admin.movePlayer "%(cid)s" %(team)s %(squad)s true'
    assert MOH.teams["3"] == "spec"
    assert BFBC2.teams["3"] == "green"
    # And no centre-screen message at all: the classic's `moh.py` overrides `saybig` to call `say`.
    assert MOH.saybig_template is None
    assert MOH.server_verbs == {
        "round_next": "admin.runNextRound",
        "round_restart": "admin.restartRound",
    }


def test_the_centre_screen_message_is_the_engines_own_verb():
    """With no `saybig_template` every `say_big` fell back to `say_template`, so `firstkill`'s
    announcement and everything else meant to be unmissable was an ordinary chat line on all six
    titles. `admin.yell` is what the classic bot used, and it takes a duration."""
    assert BF3.saybig_template == 'admin.yell "%s" 10'


def test_frostbite_1_counts_its_yell_in_milliseconds():
    """The same ten, and it was ten *milliseconds* — gone before the frame it drew on.

    The classic's Bad Company 2 parser passed `duration=2400` for a message meant to be read, and
    `poweradminbfbc2` above it passed 900 for a one-second countdown step; both are milliseconds.
    Frostbite 2 takes seconds. So the one figure that served all six titles was right for four of
    them and invisible on the other two — `admin.yell` accepted it either way, which is what made it
    impossible to see.
    """
    assert BFBC2.saybig_template == 'admin.yell "%s" 10000 all'
    assert "%(ms)s" in BFBC2.server_verbs["yell"]
    assert "%(seconds)s" in BF3.server_verbs["yell"]


def test_frostbite_1_declares_the_verbs_its_own_titles_take():
    """Read for `poweradminbfbc2`, which is what the round control on this generation was waiting on.

    Named as what they do, so a plugin asking "can this server restart the map for me?" does not
    have to know which engine it is talking to: `cyclemap` and `map_restart` are the same two names
    Urban Terror declares, and `exec` is the same name for "run a file of commands on the server".
    """
    assert BFBC2.server_verbs["cyclemap"] == "admin.runNextLevel"
    assert BFBC2.server_verbs["map_restart"] == "admin.restartMap"
    assert BFBC2.server_verbs["exec"] == 'admin.runScript "%(file)s"'
    assert "player" in BFBC2.player_verbs["yell_player"]
    # The team and squad yell subsets are Frostbite 2's here. This generation has them in the
    # protocol, but the classic never sent one from Bad Company 2 — its team, enemy and squad
    # commands sent ordinary chat — so there is no captured grammar and none is invented.
    assert "yell_team" not in BFBC2.server_verbs
    assert "yell_squad" not in BFBC2.server_verbs


def test_the_round_and_yell_verbs_are_asked_for_rather_than_assumed():
    """Every one of them is a `server_verbs`/`player_verbs` entry, so `poweradminbf3` can ask whether
    this title has it before offering an admin a command that would do nothing."""
    assert BF3.server_verbs["round_end"] == "mapList.endRound %(team)s"
    assert BF3.server_verbs["server_shutdown"] == "admin.shutDown"
    assert BF3.player_verbs["move"] == 'admin.movePlayer "%(cid)s" %(team)s %(squad)s true'
    assert "squad %(team)s %(squad)s" in BF3.server_verbs["yell_squad"]


def test_a_setting_is_set_by_naming_it_with_no_verb_in_front():
    """The default template is `set <name> "<value>"`, which on this engine is the unknown command
    `set` with two arguments — so every `set_cvar` was answered `UnknownCommand` and nothing changed.
    *Reading* was already right, which is what made the asymmetry invisible."""
    assert BF3.set_template == '%(name)s "%(value)s"'
    assert BF3.get_cvar_template == "%(name)s"
    for profile in (BFBC2, MOH, BF4, BFH, MOHW):
        assert profile.set_template == BF3.set_template


def test_a_team_name_maps_back_to_the_engines_own_id():
    """`Client.team` says `red`; `admin.movePlayer` takes `1`. One table, read both ways."""
    assert BF3.team_id("red") == "1"
    assert BF3.team_id("blue") == "2"
    assert BF3.team_id("spec") == ""  # this engine has no such team


def test_bans_are_native_and_by_guid():
    """So they hold across a name change, and across the bot not running."""
    assert "banList.add guid" in BF3.ban_template
    assert "seconds" in BF3.tempban_template
    assert BF3.unban_template == 'banList.remove guid "%(guid)s"'


def test_the_reason_cap_is_the_engines_not_ours():
    """Frostbite *rejects* a command whose reason runs past 80 characters — no ban at all."""
    assert BF3.max_reason_length == 80


def test_the_templates_quote_every_value():
    """This engine takes word lists, so an unquoted reason with a space would become two words."""
    for template in (BF3.tell_template, BF3.kick_template, BF3.ban_template, BF3.tempban_template):
        assert template.count('"') >= 2


# -- the roster is the other place a team comes from ------------------------


@pytest.mark.asyncio
async def test_the_roster_gives_a_team_and_a_squad_to_a_player_who_never_changed_either(tmp_path):
    """A player who has not switched sides since the bot started produced no `onTeamChange`, so
    before this the bot had no idea which team they were on — and a plugin asked to swap two of them
    had nothing to go on. The status block is the only other source, and it is authoritative."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.core.game import PlayerInfo
    from b3.runtime.bot import Bot

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="bf3"),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, clock=FakeClock())
    bot.start()

    bot._reconcile([PlayerInfo(cid="Bob", name="Bob", guid=GUID2, team="2", squad="3")])
    bot._reconcile([PlayerInfo(cid="Bob", name="Bob", guid=GUID2, team="2", squad="3")])
    bob = bot.clients.get_by_cid("Bob")
    await bot.bus.drain()

    assert (bob.team, bob.squad) == ("blue", "3")

    seen: list[str] = []
    bot.bus.subscribe(EventType.CLIENT_TEAM_CHANGE, lambda ev: seen.append(str(ev.data)))
    bot._reconcile([PlayerInfo(cid="Bob", name="Bob", guid=GUID2, team="1", squad="3")])
    await bot.bus.drain()

    assert bob.team == "red"
    assert seen == ["red"]  # published once, and only because it actually changed
    bot.storage.close()
