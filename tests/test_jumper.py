"""The `jumper` plugin — timed runs on Urban Terror jump maps, and the last plugin port.

Two captured test files in the classic tree, and the useful half of them is the log lines: the setUp
of `tests/plugins/jumper/test_commands.py` drives twenty runs through `ClientJumpRunStarted:` and
`ClientJumpRunStopped:` on two maps, which is where the times below come from (537000, 349000, 122000
milliseconds on `ut42_bstjumps_u2`).

What the captured tests could not show is what happens when a record's player has been deleted from
the database: the classic went to log a warning about it and read `r1['client_id']` off a row from
`SELECT DISTINCT way_id`, so the branch written to survive that case raised `KeyError`.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.jumper import (
    MAX_CYCLES,
    WELCOME_SECONDS,
    JumperPlugin,
    format_date,
    format_time,
    parse_release_date,
)

MAP = "ut42_bstjumps_u2"


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.server_verbs = {"cyclemap"}
    console.game.map_name = MAP
    settings.setdefault("demo_record", False)
    settings.setdefault("skip_standard_maps", False)
    settings.setdefault("catalogue_url", "")
    plugin = JumperPlugin(console, {"settings": settings})
    plugin.start()
    return plugin


def _client(console, name, bits=0, cid=None, id_=None):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{name}".ljust(32, "0"),
        name=name,
        cid=slot,
        id=id_ if id_ is not None else abs(hash(name)) % 10000,
        group_bits=bits,
        team="red",
    )
    console.clients.add(client)
    console.register_client(name, client)
    console.register_client(name.lower(), client)
    console.register_lookup(f"@{client.id}", [client])
    return client


async def _run(console, client, way=1, time_ms=100000):  # noqa: ANN001, ANN202
    """One complete jump run, as the two log lines the parser turns into events."""
    await console.bus.publish(
        Event(EventType.CLIENT_JUMP_RUN_START, client=client, data={"way_id": way})
    )
    await console.bus.publish(
        Event(
            EventType.CLIENT_JUMP_RUN_STOP,
            client=client,
            data={"way_id": way, "way_time": time_ms},
        )
    )


async def _cmd(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- the run, and what makes it a record -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_first_run_on_a_way_is_a_map_record(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")

    await _run(console, joe, way=1, time_ms=537000)

    records = plugin.records_of(joe, MAP)
    assert [(r.way_id, r.way_time) for r in records] == [(1, 537000)]
    assert "Joe set a new map record" in console.said_big[-1]


@pytest.mark.asyncio
async def test_beating_your_own_time_replaces_it(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")

    await _run(console, joe, way=1, time_ms=537000)
    await _run(console, joe, way=1, time_ms=349000)

    records = plugin.records_of(joe, MAP)
    assert [(r.way_id, r.way_time) for r in records] == [(1, 349000)]


@pytest.mark.asyncio
async def test_a_slower_run_is_not_recorded_and_says_the_time_to_beat(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")

    await _run(console, joe, way=1, time_ms=349000)
    console.told.clear()
    await _run(console, joe, way=1, time_ms=537000)

    assert [r.way_time for r in plugin.records_of(joe, MAP)] == [349000]
    assert "your best on this way is 0:05:49.000" in _told(console, joe)[-1]


@pytest.mark.asyncio
async def test_beating_somebody_elses_time_is_a_map_record_and_beating_your_own_is_not(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")
    bill = _client(console, "Bill")

    await _run(console, joe, way=1, time_ms=122000)
    console.said_big.clear()
    console.told.clear()

    # Bill is slower than Joe but this is his first run: a personal record, not a map one.
    await _run(console, bill, way=1, time_ms=349000)
    assert console.said_big == []
    assert "a new personal record" in _told(console, bill)[-1]

    # And now he beats Joe.
    await _run(console, bill, way=1, time_ms=84000)
    assert "Bill set a new map record" in console.said_big[-1]
    assert [r.way_time for r in plugin.records_of(bill, MAP)] == [84000]


@pytest.mark.asyncio
async def test_ways_are_kept_apart(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")

    await _run(console, joe, way=1, time_ms=537000)
    await _run(console, joe, way=2, time_ms=84000)

    assert [(r.way_id, r.way_time) for r in plugin.records_of(joe, MAP)] == [
        (1, 537000),
        (2, 84000),
    ]


@pytest.mark.asyncio
async def test_maps_are_kept_apart(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")

    await _run(console, joe, way=1, time_ms=537000)
    console.game.map_name = "ut42_jupiter"
    await _run(console, joe, way=1, time_ms=123000)

    assert [r.way_time for r in plugin.records_of(joe, MAP)] == [537000]
    assert [r.way_time for r in plugin.records_of(joe, "ut42_jupiter")] == [123000]


@pytest.mark.asyncio
async def test_a_run_by_a_player_the_database_has_never_seen_is_not_stored(console):
    """The classic wrote `None` into `client_id`, producing rows nothing could read back."""
    plugin = _plugin(console)
    stranger = _client(console, "Stranger")
    stranger.id = None

    await _run(console, stranger, way=1, time_ms=100000)

    assert plugin.map_records(MAP) == []


@pytest.mark.asyncio
async def test_a_cancelled_run_records_nothing(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")

    await console.bus.publish(
        Event(EventType.CLIENT_JUMP_RUN_START, client=joe, data={"way_id": 1})
    )
    await console.bus.publish(
        Event(EventType.CLIENT_JUMP_RUN_CANCEL, client=joe, data={"way_id": 1})
    )

    assert plugin.runs == []
    assert plugin.map_records(MAP) == []


@pytest.mark.asyncio
async def test_going_to_the_spectators_abandons_a_run(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")
    await console.bus.publish(
        Event(EventType.CLIENT_JUMP_RUN_START, client=joe, data={"way_id": 1})
    )

    joe.team = "spec"
    await console.bus.publish(Event(EventType.CLIENT_TEAM_CHANGE, data="spec", client=joe))

    assert plugin.runs == []


@pytest.mark.asyncio
async def test_a_run_the_bot_never_saw_start_is_still_timed(console):
    """A player already running when the bot connected. There is no demo and no start, but the time
    is the server's and it stands."""
    plugin = _plugin(console)
    joe = _client(console, "Joe")

    await console.bus.publish(
        Event(
            EventType.CLIENT_JUMP_RUN_STOP,
            client=joe,
            data={"way_id": 3, "way_time": 12345},
        )
    )

    assert [(r.way_id, r.way_time) for r in plugin.records_of(joe, MAP)] == [(3, 12345)]


# -- reading the records back ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_lists_your_own_times_on_this_map(console):
    _plugin(console)
    joe = _client(console, "Joe")
    await _run(console, joe, way=1, time_ms=537000)
    await _run(console, joe, way=2, time_ms=84000)
    console.told.clear()

    await _cmd(console, joe, "!record")

    lines = _told(console, joe)
    assert "records for Joe" in lines[0]
    assert "[1] 0:08:57.000" in lines[1]
    assert "[2] 0:01:24.000" in lines[2]


@pytest.mark.asyncio
async def test_record_of_a_map_nobody_has_run(console):
    _plugin(console)
    joe = _client(console, "Joe")

    await _cmd(console, joe, "!record")

    assert "no record for Joe" in _told(console, joe)[-1]


@pytest.mark.asyncio
async def test_maprecord_names_the_holder_of_each_way(console):
    _plugin(console)
    joe = _client(console, "Joe")
    bill = _client(console, "Bill")
    await _run(console, joe, way=1, time_ms=122000)
    await _run(console, bill, way=1, time_ms=349000)
    await _run(console, bill, way=2, time_ms=91000)
    console.told.clear()

    await _cmd(console, joe, "!maprecord")

    lines = _told(console, joe)
    assert any("[1] Joe with 0:02:02.000" in line for line in lines)
    assert any("[2] Bill with 0:01:31.000" in line for line in lines)


@pytest.mark.asyncio
async def test_a_record_whose_player_is_gone_is_listed_by_id_rather_than_raising(console):
    """The classic's own branch for this read `r1['client_id']` from a row selected as
    `SELECT DISTINCT way_id` — a `KeyError` in the code written to handle the case."""
    _plugin(console)
    joe = _client(console, "Joe")
    await _run(console, joe, way=1, time_ms=122000)
    console.clients.remove(joe.cid)
    console._lookup.clear()
    watcher = _client(console, "Watcher")

    await _cmd(console, watcher, "!maprecord")
    await _cmd(console, watcher, "!topruns")

    assert any("@" in line for line in _told(console, watcher))


@pytest.mark.asyncio
async def test_topruns_places_three_per_way(console):
    _plugin(console)
    joe = _client(console, "Joe")
    bill = _client(console, "Bill")
    mark = _client(console, "Mark")
    await _run(console, joe, way=1, time_ms=537000)
    await _run(console, bill, way=1, time_ms=349000)
    await _run(console, mark, way=1, time_ms=122000)
    console.told.clear()

    await _cmd(console, joe, "!topruns")

    lines = [line for line in _told(console, joe) if "#" in line]
    assert "#1 Mark" in lines[0]
    assert "#2 Bill" in lines[1]
    assert "#3 Joe" in lines[2]


@pytest.mark.asyncio
async def test_a_way_can_be_given_a_name(console):
    _plugin(console)
    boss = _client(console, "Boss", bits=128)
    joe = _client(console, "Joe")
    await _run(console, joe, way=2, time_ms=84000)

    await _cmd(console, boss, "!setway 2 the roof route")
    console.told.clear()
    await _cmd(console, joe, "!maprecord")

    assert any("[the roof route]" in line for line in _told(console, joe))


@pytest.mark.asyncio
async def test_a_way_name_is_stored_rather_than_pasted_into_sql(console):
    """`INSERT INTO jumpways VALUES (NULL, '%s', '%d', '%s')` with a name a *player* typed."""
    plugin = _plugin(console)
    boss = _client(console, "Boss", bits=128)

    await _cmd(console, boss, "!setway 1 x'); DROP TABLE jumper_runs; --")

    assert plugin.way_names(MAP) == {1: "x'); DROP TABLE jumper_runs; --"}
    # And the table is still there to be read.
    assert plugin.map_records(MAP) == []


@pytest.mark.asyncio
async def test_setway_with_no_name_says_how_to_use_it(console):
    """And names a command that exists: the classic's usage line said `!help jmpsetway`."""
    _plugin(console)
    boss = _client(console, "Boss", bits=128)

    await _cmd(console, boss, "!setway 2")

    assert _told(console, boss)[-1] == "!setway <way> <name>"


# -- deleting ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anybody_may_delete_their_own_records(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")
    await _run(console, joe, way=1, time_ms=537000)

    await _cmd(console, joe, "!delrecord")

    assert plugin.records_of(joe, MAP) == []
    assert "removed 1 record(s) for Joe" in _told(console, joe)[-1]


@pytest.mark.asyncio
async def test_deleting_somebody_elses_needs_the_level(console):
    plugin = _plugin(console, min_level_delete="senioradmin")
    joe = _client(console, "Joe")
    await _run(console, joe, way=1, time_ms=537000)
    mod = _client(console, "Mod", bits=8)
    senior = _client(console, "Senior", bits=64)

    await _cmd(console, mod, "!delrecord Joe")
    assert plugin.records_of(joe, MAP) != []
    assert "may not delete Joe's records" in _told(console, mod)[-1]

    await _cmd(console, senior, "!delrecord Joe")
    assert plugin.records_of(joe, MAP) == []


@pytest.mark.asyncio
async def test_a_level_that_is_not_one_is_reported_and_falls_back(console, caplog):
    plugin = _plugin(console, min_level_delete="seniormoderator")
    joe = _client(console, "Joe")
    await _run(console, joe, way=1, time_ms=537000)
    senior = _client(console, "Senior", bits=64)

    with caplog.at_level("WARNING"):
        await _cmd(console, senior, "!delrecord Joe")

    assert "min_level_delete" in caplog.text
    assert plugin.records_of(joe, MAP) == []  # senioradmin, which is the documented default


# -- the demo of a run -----------------------------------------------------------------------------


class FakeDemos:
    """Stands in for `urtserversidedemo`, which is what the real plugin looks like to this one."""

    def __init__(self, filename: str = "serverdemos/run.dm_68") -> None:
        self.filename = filename
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.stopped_all = 0

    def is_enabled(self) -> bool:
        return True

    def record(self, client, minutes: int = 0, admin=None):  # noqa: ANN001, ANN202, ARG002
        from b3.plugins.urtserversidedemo import Started

        self.started.append(client.name)
        return Started(filename=self.filename)

    def stop(self, client, admin=None) -> str:  # noqa: ANN001, ARG002
        self.stopped.append(client.name)
        return "stopserverdemo: stopped recording"

    def stop_all(self, admin=None) -> str:  # noqa: ANN001, ARG002
        self.stopped_all += 1
        return ""


@pytest.mark.asyncio
async def test_a_run_is_recorded_and_the_demo_kept_when_it_is_a_record(console, tmp_path):
    demos = FakeDemos()
    plugin = _plugin(console, demo_record=True)
    console.plugins["urtserversidedemo"] = demos
    demo = tmp_path / "run.dm_68"
    demo.write_text("a demo")
    console.cvars.update({"fs_basepath": str(tmp_path), "fs_game": ""})
    demos.filename = "run.dm_68"
    joe = _client(console, "Joe")

    await _run(console, joe, way=1, time_ms=537000)

    assert demos.started == ["Joe"] and demos.stopped == ["Joe"]
    assert demo.exists()  # a record, so the demo is kept
    assert plugin.records_of(joe, MAP)[0].demo == "run.dm_68"


@pytest.mark.asyncio
async def test_the_demo_of_a_run_that_beat_nothing_is_deleted(console, tmp_path):
    demos = FakeDemos()
    _plugin(console, demo_record=True)
    console.plugins["urtserversidedemo"] = demos
    console.cvars.update({"fs_basepath": str(tmp_path), "fs_game": ""})
    joe = _client(console, "Joe")

    (tmp_path / "first.dm_68").write_text("a demo")
    demos.filename = "first.dm_68"
    await _run(console, joe, way=1, time_ms=349000)

    (tmp_path / "second.dm_68").write_text("a demo")
    demos.filename = "second.dm_68"
    await _run(console, joe, way=1, time_ms=537000)  # slower

    assert (tmp_path / "first.dm_68").exists()
    assert not (tmp_path / "second.dm_68").exists()


@pytest.mark.asyncio
async def test_beating_your_own_record_deletes_the_demo_it_replaces(console, tmp_path):
    demos = FakeDemos()
    _plugin(console, demo_record=True)
    console.plugins["urtserversidedemo"] = demos
    console.cvars.update({"fs_basepath": str(tmp_path), "fs_game": ""})
    joe = _client(console, "Joe")

    (tmp_path / "old.dm_68").write_text("a demo")
    demos.filename = "old.dm_68"
    await _run(console, joe, way=1, time_ms=537000)

    (tmp_path / "new.dm_68").write_text("a demo")
    demos.filename = "new.dm_68"
    await _run(console, joe, way=1, time_ms=349000)

    assert not (tmp_path / "old.dm_68").exists()
    assert (tmp_path / "new.dm_68").exists()


@pytest.mark.asyncio
async def test_a_demo_this_machine_cannot_see_is_reported_once(console, caplog):
    """The game server writes the file, so deleting it only works where the bot is on that machine —
    which the classic assumed in silence."""
    demos = FakeDemos()
    _plugin(console, demo_record=True)
    console.plugins["urtserversidedemo"] = demos
    joe = _client(console, "Joe")

    with caplog.at_level("WARNING"):
        await _run(console, joe, way=1, time_ms=537000)
        await _run(console, joe, way=1, time_ms=600000)
        await _run(console, joe, way=1, time_ms=700000)

    assert caplog.text.count("jump-run demos will not be deleted") == 1


@pytest.mark.asyncio
async def test_anything_the_server_was_already_recording_is_stopped_at_startup(console):
    """A demo started before the bot came up has a filename nothing here knows, so it could never be
    attached to a run or deleted. The classic did this too."""
    demos = FakeDemos()
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin, "urtserversidedemo": demos}
    console.game.map_name = MAP
    plugin = JumperPlugin(console, {"settings": {"catalogue_url": ""}})
    plugin.start()

    assert demos.stopped_all == 1


@pytest.mark.asyncio
async def test_the_demo_half_is_switched_off_where_the_plugin_is_absent(console, caplog):
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.game.map_name = MAP

    with caplog.at_level("WARNING"):
        plugin = JumperPlugin(console, {"settings": {"demo_record": True, "catalogue_url": ""}})
        plugin.start()
    joe = _client(console, "Joe")
    await _run(console, joe, way=1, time_ms=537000)

    assert "urtserversidedemo plugin is not loaded" in caplog.text
    # ...and the run is still timed and recorded, which is the point of saying it rather than failing.
    assert plugin.records_of(joe, MAP) != []


# -- the map ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stock_map_is_cycled_off(console):
    plugin = _plugin(console, skip_standard_maps=True)
    console.game.map_name = "ut4_casa"

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={"mapname": "ut4_casa"}))

    assert console.server_verbs_applied == [("cyclemap", {})]
    assert plugin._cycles == 1


@pytest.mark.asyncio
async def test_only_so_many_stock_maps_in_a_row_are_cycled_past(console):
    """A burst limit, not a total: five in a row, then one gets played and the count starts again.

    Which is the classic's own rule — a server whose whole rotation is stock maps would otherwise
    cycle for ever, and refusing to cycle again *ever* would strand it on them. What the classic did
    not do is apply the limit when the plugin was **enabled**, which is the one moment an operator is
    watching the log.
    """
    plugin = _plugin(console, skip_standard_maps=True)

    for _ in range(MAX_CYCLES):
        console.game.map_name = "ut4_casa"
        await console.bus.publish(Event(EventType.GAME_ROUND_START, data={"mapname": "ut4_casa"}))
    assert len(console.server_verbs_applied) == MAX_CYCLES
    assert plugin._cycles == MAX_CYCLES

    # The next one is played rather than cycled, and the count starts over.
    console.server_verbs_applied.clear()
    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={"mapname": "ut4_casa"}))
    assert console.server_verbs_applied == []
    assert plugin._cycles == 0


@pytest.mark.asyncio
async def test_a_jump_map_is_left_alone_and_gets_a_greeting(console):
    plugin = _plugin(console, skip_standard_maps=True)

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={"mapname": MAP}))
    assert console.server_verbs_applied == []
    assert console.said == []

    console.clock.advance(WELCOME_SECONDS)
    plugin._run_pending()

    assert console.said == [f"welcome to {MAP}"]
    # And only once, however many times the pass runs.
    plugin._run_pending()
    assert len(console.said) == 1


@pytest.mark.asyncio
async def test_a_new_map_abandons_every_run_in_progress(console):
    plugin = _plugin(console)
    joe = _client(console, "Joe")
    await console.bus.publish(
        Event(EventType.CLIENT_JUMP_RUN_START, client=joe, data={"way_id": 1})
    )

    await console.bus.publish(Event(EventType.GAME_ROUND_START, data={"mapname": "ut42_jupiter"}))

    assert plugin.runs == []


# -- the map catalogue -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mapinfo_reads_the_catalogue(console):
    plugin = _plugin(console)
    plugin.catalogue = {
        MAP: {
            "pk3": MAP,
            "nom": "BST Jumps u2",
            "mapper": "BST",
            "mdate": "2011-07-16",
            "nway": "4",
            "njump": "60",
            "level": "42",
        }
    }
    joe = _client(console, "Joe")

    await _cmd(console, joe, "!mapinfo")

    lines = _told(console, joe)
    assert "BST Jumps u2 was built by BST" in lines[0]
    assert "released Sat, 16 Jul 2011" in lines[1]
    assert "4 way(s), 60 jump(s)" in lines[2]
    assert "difficulty 42/100" in lines[3]


@pytest.mark.asyncio
async def test_mapinfo_with_no_catalogue_says_so(console):
    _plugin(console)
    joe = _client(console, "Joe")

    await _cmd(console, joe, "!mapinfo")

    assert "catalogue could not be read" in _told(console, joe)[-1]


@pytest.mark.asyncio
async def test_the_catalogue_is_fetched_off_the_bots_thread(console, tmp_path):
    """`requests.get` on the bot's own thread at every map change in the classic, so a slow endpoint
    stopped the bot answering for as long as it took."""
    import json

    catalogue = tmp_path / "maps.json"
    catalogue.write_text(json.dumps([{"pk3": "UT42_JUPITER", "mapper": "Jup"}]))
    plugin = _plugin(console, catalogue_url=catalogue.as_uri())

    await plugin._refresh_catalogue()

    assert plugin.catalogue["ut42_jupiter"]["mapper"] == "Jup"


@pytest.mark.asyncio
async def test_a_catalogue_that_cannot_be_read_leaves_the_old_one_alone(console, caplog):
    plugin = _plugin(console, catalogue_url="http://127.0.0.1:9/nothing")
    plugin.catalogue = {MAP: {"pk3": MAP, "mapper": "BST"}}

    with caplog.at_level("WARNING"):
        await plugin._refresh_catalogue()

    assert plugin.catalogue[MAP]["mapper"] == "BST"
    assert "could not read the map catalogue" in caplog.text


@pytest.mark.asyncio
async def test_an_empty_catalogue_url_asks_nobody_anything(console):
    """An operator who does not want the bot talking to a third party empties the setting."""
    plugin = _plugin(console, catalogue_url="")

    await plugin._refresh_catalogue()

    assert plugin.catalogue == {}


# -- formatting ------------------------------------------------------------------------------------


def test_a_run_time_reads_as_players_read_it():
    assert format_time(537000) == "0:08:57.000"
    assert format_time(12345) == "0:00:12.345"
    assert format_time(3661234) == "1:01:01.234"
    assert format_time(0) == "0:00:00.000"


def test_a_release_date_is_read_portably():
    """`strptime(...).strftime('%s')` in the classic — `%s` is a glibc extension, so every Windows
    operator lost the date and the map greeting that quotes it."""
    assert format_date(parse_release_date("2011-07-16")) == "Sat, 16 Jul 2011"
    assert parse_release_date("not a date") == 0
    assert parse_release_date("") == 0


# -- it is an Urban Terror 4.2 plugin --------------------------------------------------------------


def test_the_loader_refuses_it_on_any_other_title(console):
    from b3.config.schema import Config, PluginEntry, ServerConfig
    from b3.core.pluginmgr import load_plugins

    loaded = load_plugins(
        console,
        Config(
            server=ServerConfig(game="iourt41"),
            plugins=[PluginEntry(name="admin"), PluginEntry(name="jumper")],
        ),
    )
    plugin = next(item for item in loaded if item.name == "jumper")

    assert plugin.enabled is False
    assert "does not support the 'iourt41' parser" in plugin.reason


# -- through a real bot ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_jump_run_reaches_the_records_from_a_real_log_line(tmp_path):
    """The captured lines, through a real `Bot`: the parser turns them into events and the plugin
    turns those into a record. These are the lines `tests/plugins/jumper/test_commands.py` drives."""
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="jumper")],
    )
    bot = Bot(config, rcon=Rcon(), clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = JumperPlugin(
        bot, {"settings": {"demo_record": False, "skip_standard_maps": False, "catalogue_url": ""}}
    )
    bot.add_plugin(plugin, "jumper")
    bot.start()
    admin.start()
    plugin.start()
    bot.game.map_name = MAP

    await bot.replay(
        [
            r"ClientUserinfo: 1 \name\Mike\cl_guid\MIKEGUID0000000000000000000000000",
            "ClientBegin: 1",
            "ClientJumpRunStarted: 1 - way: 1",
            "ClientJumpRunStopped: 1 - way: 1 - time: 537000",
        ]
    )
    await bot.bus.drain()

    mike = bot.clients.get_by_cid("1")
    assert [(r.way_id, r.way_time) for r in plugin.records_of(mike, MAP)] == [(1, 537000)]
    bot.storage.close()
