"""The `poweradmincod7` plugin — Black Ops's playlists, map exclusions and DLC switches.

Like `codam`, this one has **no captured tests** in the classic tree, so its source is the only
description of what it did. It does have a shipped config and a README, which is how the levels and
aliases here are the classic's rather than invented.

The tests start where the reading did: a ranked Black Ops server cannot be told to load a map, so
`!pasetmap` works by excluding the other twenty-five and putting the operator's own exclusion list
back when the round ends. Everything else is either that mechanism or one of the faults around it —
a dvar written with a bare assignment the engine ignores, four `time.sleep` calls in command
handlers, a thread that was never a thread, and two crashes on ordinary typing.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.cod.maps import COD7_MAPS
from b3.plugins.admin import AdminPlugin
from b3.plugins.poweradmincod7 import (
    EXCLUDE_CVAR,
    GAMETYPE_SECONDS,
    PLAYLISTS,
    RESTART_SECONDS,
    Poweradmincod7Plugin,
)


def _plugin(console, ranked=True, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    console.cvars["sv_ranked"] = "2" if ranked else "0"
    # What a real Black Ops console has, from the title's profile: its two restart verbs, and the
    # map-name table that makes `!maps` and this plugin's replies say Nuketown rather than mp_nuked.
    console.server_verbs = console.server_verbs | {"fast_restart"}
    console.map_names = dict(COD7_MAPS)
    plugin = Poweradmincod7Plugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


def _boss(console):  # noqa: ANN001, ANN202
    client = Client(guid="BOSS", name="Boss", cid="1", id=1, group_bits=128)
    console.clients.add(client)
    console.register_client("boss", client)
    return client


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    told = [text for who, text in console.told if who is client]
    return told[-1] if told else ""


# -- the map exclusion list, which is the whole mechanism -----------------------------------------


@pytest.mark.asyncio
async def test_setting_a_map_excludes_every_other_one(console):
    """A ranked Black Ops server takes its map from a playlist and has no map verb at all. The only
    lever is `playlist_excludeMap`, so "play Nuketown next" is "exclude the other twenty-five"."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pasetmap nuketown")

    excluded = console.cvars[EXCLUDE_CVAR].split()
    assert "mp_nuked" not in excluded
    assert len(excluded) == len(COD7_MAPS) - 1
    assert "Nuketown" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_operators_own_exclusion_list_comes_back_at_the_round_start(console):
    plugin = _plugin(console)
    boss = _boss(console)
    console.cvars[EXCLUDE_CVAR] = "mp_villa mp_zoo"

    await _run(console, boss, "!pasetmap nuketown")
    assert console.cvars[EXCLUDE_CVAR] != "mp_villa mp_zoo"

    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars[EXCLUDE_CVAR] == "mp_villa mp_zoo"
    assert plugin._restore is None


@pytest.mark.asyncio
async def test_an_empty_exclusion_list_comes_back_empty_not_as_the_word_none(console):
    """The classic only assigned `_admin_excluded_maps` if the cvar could be read at startup, then
    interpolated it regardless — so a server that did not answer got `playlist_excludeMap "None"`."""
    _plugin(console)
    boss = _boss(console)
    console.cvars.pop(EXCLUDE_CVAR, None)

    await _run(console, boss, "!pasetmap nuketown")
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars[EXCLUDE_CVAR] == ""


@pytest.mark.asyncio
async def test_a_second_setmap_does_not_adopt_the_first_ones_exclusions(console):
    plugin = _plugin(console)
    boss = _boss(console)
    console.cvars[EXCLUDE_CVAR] = "mp_villa"

    await _run(console, boss, "!pasetmap nuketown")
    await _run(console, boss, "!pasetmap summit")
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars[EXCLUDE_CVAR] == "mp_villa"
    assert plugin._restore is None


@pytest.mark.asyncio
async def test_disabling_the_plugin_puts_the_list_back_too(console):
    """The one window this cannot cover is the bot being killed; a clean disable is covered."""
    plugin = _plugin(console)
    boss = _boss(console)
    console.cvars[EXCLUDE_CVAR] = "mp_villa"

    await _run(console, boss, "!pasetmap nuketown")
    plugin.disable()

    assert console.cvars[EXCLUDE_CVAR] == "mp_villa"


@pytest.mark.asyncio
async def test_a_map_is_matched_the_way_every_other_map_argument_is(console):
    """The classic took the console id, the id without `mp_`, or the exact friendly name — so
    `!pasetmap firing` failed where `!pasetmap firing range` worked."""
    _plugin(console)
    boss = _boss(console)

    for typed in ("mp_firingrange", "firingrange", "firing range", "firing"):
        console.cvars[EXCLUDE_CVAR] = ""
        await _run(console, boss, f"!pasetmap {typed}")
        assert "mp_firingrange" not in console.cvars[EXCLUDE_CVAR].split(), typed


@pytest.mark.asyncio
async def test_a_map_nobody_has_is_refused(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pasetmap carentan")

    assert EXCLUDE_CVAR not in console.cvars
    assert "not a stock Black Ops map" in _last(console, boss)


@pytest.mark.asyncio
async def test_two_candidates_are_a_question_not_a_coin_toss(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pasetmap ha")  # Hanoi, Havana, Hangar 18, Hazard...

    assert EXCLUDE_CVAR not in console.cvars
    assert "say which" in _last(console, boss)


@pytest.mark.asyncio
async def test_setmap_is_refused_on_an_unranked_server(console):
    """It would exclude twenty-five maps on a server that has a perfectly good map verb."""
    _plugin(console, ranked=False)
    boss = _boss(console)

    await _run(console, boss, "!pasetmap nuketown")

    assert EXCLUDE_CVAR not in console.cvars
    assert "ranked server" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_server_that_does_not_answer_for_sv_ranked_is_told_so(console):
    """Rather than guessed at either way: one guess breaks a rotation, the other does nothing."""
    _plugin(console)
    console.cvars.pop("sv_ranked")
    boss = _boss(console)

    await _run(console, boss, "!pasetmap nuketown")

    assert EXCLUDE_CVAR not in console.cvars
    assert "sv_ranked" in _last(console, boss)


@pytest.mark.asyncio
async def test_excludemaps_takes_friendly_names_and_becomes_the_operators_list(console):
    plugin = _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paexcludemaps nuketown mp_villa")

    assert set(console.cvars[EXCLUDE_CVAR].split()) == {"mp_nuked", "mp_villa"}
    assert plugin._restore is None


@pytest.mark.asyncio
async def test_excludemaps_with_nothing_after_it_names_its_own_help(console):
    """The classic's message said `try !help pasetplaylist`."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paexcludemaps")

    assert "!paexcludemaps" in _last(console, boss)


@pytest.mark.asyncio
async def test_one_bad_map_in_a_list_excludes_nothing(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paexcludemaps nuketown carentan")

    assert EXCLUDE_CVAR not in console.cvars


# -- playlists -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_current_playlist_is_named(console):
    _plugin(console)
    boss = _boss(console)
    console.cvars["playlist"] = "6"

    await _run(console, boss, "!paplaylist")

    assert "Domination" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_playlist_number_the_engine_reports_but_this_bot_does_not_know(console):
    """The classic indexed its table directly, so this raised inside the command."""
    _plugin(console)
    boss = _boss(console)
    console.cvars["playlist"] = "99"

    await _run(console, boss, "!paplaylist")

    assert "not one this bot knows" in _last(console, boss)


@pytest.mark.asyncio
async def test_every_playlist_is_listed_in_one_message(console):
    """Twenty-five, one second apart, was twenty-five seconds of frozen bot and twenty-five lines."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pagetplaylists")

    said = " ".join(text for who, text in console.told if who is boss)
    assert "Team Deathmatch" in said and "Team Tactical" in said


@pytest.mark.asyncio
async def test_a_playlist_is_set_with_the_titles_own_verb(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pasetplaylist 9")

    assert console.cvars["playlist"] == "9"
    assert "Hardcore Team Deathmatch" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_playlist_that_is_not_a_whole_number_is_answered_rather_than_raising(console):
    """`float('2.7')` passed the classic's check and `int('2.7')` then raised."""
    _plugin(console)
    boss = _boss(console)

    for typed in ("2.7", "0", "26", "tdm", ""):
        await _run(console, boss, f"!pasetplaylist {typed}")

    assert "playlist" not in console.cvars


@pytest.mark.asyncio
async def test_playlists_switched_off_on_an_unranked_server_refuse_the_command(console):
    _plugin(console, ranked=False)
    boss = _boss(console)
    console.cvars["playlist_enabled"] = "0"

    await _run(console, boss, "!pasetplaylist 9")

    assert "playlist" not in console.cvars
    assert "switched off" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_ranked_server_always_has_playlists(console):
    """It has nothing else: the playlist is where a ranked server's map and gametype come from."""
    _plugin(console)
    boss = _boss(console)
    console.cvars["playlist_enabled"] = "0"  # would switch them off on an unranked server

    await _run(console, boss, "!pasetplaylist 9")

    assert console.cvars["playlist"] == "9"


# -- cvars ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paset_with_no_value_is_answered_rather_than_raising(console):
    """`data.split(' ', 1)[1]` raised IndexError inside the classic's handler."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paset g_gametype")

    assert "!paset <cvar> <value>" in _last(console, boss)


@pytest.mark.asyncio
async def test_paset_writes_the_cvar(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paset scr_dom_scorelimit 300")

    assert console.cvars["scr_dom_scorelimit"] == "300"


@pytest.mark.asyncio
async def test_setting_the_exclusion_list_by_hand_cancels_the_pending_restore(console):
    plugin = _plugin(console)
    boss = _boss(console)
    console.cvars[EXCLUDE_CVAR] = "mp_villa"

    await _run(console, boss, "!pasetmap nuketown")
    await _run(console, boss, "!paset playlist_excludeMap mp_zoo")
    await console.bus.publish(Event(EventType.GAME_ROUND_START))

    assert console.cvars["playlist_excludemap"] == "mp_zoo"
    assert plugin._restore is None


@pytest.mark.asyncio
async def test_paget_answers_with_the_value(console):
    """The classic interpolated the Cvar object, so an admin saw a repr."""
    _plugin(console)
    boss = _boss(console)
    console.cvars["g_gametype"] = "dom"

    await _run(console, boss, "!paget g_gametype")

    assert _last(console, boss) == "g_gametype is dom"


@pytest.mark.asyncio
async def test_paget_says_so_when_nothing_answers(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!paget nothing_here")

    assert "not set" in _last(console, boss)


# -- the DLC switches -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_map_pack_is_switched_off_by_the_number_an_operator_says(console):
    """Treyarch counts its packs from 2, so DLC1 is `playlist_excludeDlc2`."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pasetdlc 1 off")

    assert console.cvars["playlist_excludeDlc2"] == "1"
    assert "DLC1 map pack is off" in _last(console, boss)


@pytest.mark.asyncio
async def test_and_back_on_again(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pasetdlc 3 on")

    assert console.cvars["playlist_excludeDlc4"] == "0"


@pytest.mark.asyncio
async def test_anything_but_on_or_off_is_answered_to_the_admin(console):
    """The classic printed its complaint to the bot's stdout and told the admin nothing."""
    _plugin(console)
    boss = _boss(console)

    for typed in ("1 maybe", "x off", "1", ""):
        await _run(console, boss, f"!pasetdlc {typed}")
        assert "!pasetdlc <number> <on|off>" in _last(console, boss), typed
    assert not [name for name in console.cvars if "dlc" in name]


# -- restarts and the gametype --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_is_announced_now_and_done_later_without_sleeping(console):
    plugin = _plugin(console, ranked=False)
    boss = _boss(console)

    await _run(console, boss, "!pamaprestart")

    assert any("restarting the map" in text for text in console.said)
    assert console.server_verbs_applied == []  # nothing has happened yet

    plugin._run_pending()
    assert console.server_verbs_applied == []  # and not before the deadline

    console.clock.advance(RESTART_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("map_restart", {})]


@pytest.mark.asyncio
async def test_a_fast_restart_uses_the_engines_own_verb(console):
    plugin = _plugin(console, ranked=False)
    boss = _boss(console)

    await _run(console, boss, "!pafastrestart")
    console.clock.advance(RESTART_SECONDS)
    plugin._run_pending()

    assert console.server_verbs_applied == [("fast_restart", {})]


@pytest.mark.asyncio
async def test_a_restart_is_refused_on_a_ranked_server(console):
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!pamaprestart")

    assert console.said == []
    assert "ranked server" in _last(console, boss)


@pytest.mark.asyncio
async def test_an_engine_with_no_restart_verb_says_so(console):
    plugin = _plugin(console, ranked=False)
    console.server_verbs = set()
    boss = _boss(console)

    await _run(console, boss, "!pamaprestart")
    plugin._run_pending()

    assert "no verb for that" in _last(console, boss)


@pytest.mark.asyncio
async def test_the_gametype_is_written_as_a_dvar_not_as_a_bare_assignment(console):
    """The classic sent `g_gametype tdm` as a command. Black Ops wants `setadmindvar`, so the
    gametype never changed and the map restarted anyway — the same fault as `g_logsync`."""
    plugin = _plugin(console, ranked=False)
    boss = _boss(console)

    await _run(console, boss, "!pagametype tdm")

    assert console.cvars["g_gametype"] == "tdm"
    assert console.rcon_sent == []  # not a raw line

    console.clock.advance(GAMETYPE_SECONDS)
    plugin._run_pending()
    assert console.server_verbs_applied == [("map_restart", {})]


@pytest.mark.asyncio
async def test_an_unknown_gametype_lists_the_ones_that_exist(console):
    _plugin(console, ranked=False)
    boss = _boss(console)

    await _run(console, boss, "!pagametype conquest")

    assert "g_gametype" not in console.cvars
    assert "tdm" in _last(console, boss)


# -- server config files --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_config_files_are_listed(console, tmp_path):
    (tmp_path / "match.cfg").write_text("set g_gametype sd\n")
    (tmp_path / "notes.txt").write_text("not a config\n")
    _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!palistcfg")

    assert "match.cfg" in _last(console, boss)
    assert "notes.txt" not in _last(console, boss)


@pytest.mark.asyncio
async def test_with_no_directory_configured_the_command_says_so(console):
    """The classic used the bot's own conf folder, which a plugin here cannot know."""
    _plugin(console)
    boss = _boss(console)

    await _run(console, boss, "!palistcfg")

    assert "no config directory" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_config_file_goes_out_a_line_at_a_time_from_the_scheduled_pass(console, tmp_path):
    """The classic's `threading.Thread(target=self._configloader(data))` *called* the loader and gave
    the thread its return value, so the whole file was sent from the handler with a sleep per line."""
    (tmp_path / "match.cfg").write_text(
        "// a comment\nset g_gametype sd\n\nset scr_sd_roundlimit 10\n"
    )
    plugin = _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!paload match.cfg")
    assert console.rcon_sent == []  # not from the handler
    assert "2 lines" in _last(console, boss)

    plugin._run_pending()
    assert console.rcon_sent == ["set g_gametype sd"]

    plugin._run_pending()
    assert console.rcon_sent == ["set g_gametype sd", "set scr_sd_roundlimit 10"]
    assert "sent in full" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_second_load_while_one_is_running_is_refused(console, tmp_path):
    (tmp_path / "a.cfg").write_text("one\ntwo\nthree\n")
    (tmp_path / "b.cfg").write_text("four\n")
    plugin = _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!paload a.cfg")
    plugin._run_pending()
    await _run(console, boss, "!paload b.cfg")

    assert "still being sent" in _last(console, boss)


@pytest.mark.asyncio
async def test_only_a_file_from_that_directory_can_be_loaded(console, tmp_path):
    """The classic joined whatever was typed onto the conf path."""
    _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!paload ../secrets.cfg")

    assert "there is no" in _last(console, boss)
    assert console.rcon_sent == []


@pytest.mark.asyncio
async def test_something_that_is_not_a_cfg_is_refused(console, tmp_path):
    _plugin(console, config_dir=str(tmp_path))
    boss = _boss(console)

    await _run(console, boss, "!paload notes.txt")

    assert "not a .cfg file" in _last(console, boss)


# -- it is a Black Ops plugin ---------------------------------------------------------------------


def test_the_loader_refuses_it_on_any_other_title(console):
    from b3.config.schema import Config, PluginEntry, ServerConfig
    from b3.core.pluginmgr import load_plugins

    loaded = load_plugins(
        console,
        Config(
            server=ServerConfig(game="cod4"),
            plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradmincod7")],
        ),
    )
    plugin = next(item for item in loaded if item.name == "poweradmincod7")

    assert plugin.enabled is False
    assert "does not support the 'cod4' parser" in plugin.reason


def test_black_ops_map_names_belong_to_the_title_not_to_this_plugin(console):
    """So `!maps`, `!nextmap` and `callvote`'s announcement print Nuketown rather than `mp_nuked`."""
    from b3.parsers.cod import profiles

    assert profiles.COD7.map_display("mp_nuked") == "Nuketown"
    assert profiles.COD7.map_names is COD7_MAPS
    assert profiles.COD4.map_display("mp_crossfire") == "mp_crossfire"  # no table, and none needed


def test_every_playlist_and_map_the_engine_ships_is_named():
    assert len(PLAYLISTS) == 25
    assert len(COD7_MAPS) == 26


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_command_reaches_a_real_black_ops_server(tmp_path):
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            if cmd.startswith("sv_ranked"):
                return '"sv_ranked" is: "2"'
            return ""

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod7"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="poweradmincod7")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = Poweradmincod7Plugin(bot, None)
    bot.add_plugin(plugin, "poweradmincod7")
    bot.start()
    admin.start()
    plugin.start()

    guid = "abcde"
    await bot.replay([f"J;{guid};1;Boss"])
    await bot.bus.drain()
    bot.clients.get_by_cid("1").group_bits = 128
    rcon.commands.clear()

    await bot.replay([f"say;{guid};1;Boss;!pasetmap nuketown"])
    await bot.bus.drain()

    written = [cmd for cmd in rcon.commands if cmd.startswith("setadmindvar playlist_excludeMap")]
    assert len(written) == 1
    assert "mp_nuked" not in written[0]
    assert "mp_villa" in written[0]
    bot.storage.close()
