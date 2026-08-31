"""The `callvote` plugin — who may call a vote, and what happened to the last one.

A vote is the only power an ordinary player has over the whole server, so the tests that matter are
the refusals and the record. The classic plugin's two test files are the source of the real log lines
used here (`Callvote: 1 - "map ut4_dressingroom"`, `VotePassed: 3 - 0 - "map ut4_casa"`) and of the
level arithmetic; what they could not show is the fault at the top of the port, because every vote
type they used was one of the thirty in the plugin's own table.

Pinned as tests, each a case the classic got wrong: an unknown vote type (it raised `KeyError` before
any check, from outside the `try` written to catch it), a vote whose arguments are SQL, a result line
that does not match the vote in hand (it vetoed a vote that had already finished, losing the record),
an engine with no way to cancel a vote at all, and bots counted as voters.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.callvote import (
    CallvotePlugin,
    format_duration,
    group_name_for,
    parse_vote,
)


def _plugin(console, *, levels=None, maps=None, protect=None, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    config = {"settings": settings, "levels": levels or {}, "maps": maps or {}}
    if protect is not None:
        config["protect"] = protect
    plugin = CallvotePlugin(console, config)
    plugin.start()
    return plugin


def _join(console, name, bits=0, cid=None, team="red", is_bot=False):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"),
        name=name,
        cid=slot,
        id=int(slot),
        group_bits=bits,
        is_bot=is_bot,
    )
    client.team = team
    console.clients.add(client)
    console.register_client(name, client)
    return client


async def _call(console, client, vote, **extra):  # noqa: ANN001, ANN202
    """A vote starting, in Urban Terror's shape: the whole line in `data`."""
    await console.bus.publish(
        Event(EventType.CLIENT_CALLVOTE, data=vote, client=client, extra=extra)
    )


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- reading the payload -------------------------------------------------------------------------


def test_the_three_engines_report_a_vote_three_different_ways():
    """Read off the events rather than one engine's habit — Homefront is why.

    Urban Terror puts the whole line in `data`; Altitude puts the joined arguments there and the list
    in `extra`; Homefront puts only the *type* in `data`, with the type and its target in `extra`. A
    plugin that splits `data` and stops loses the name a Homefront `kick` vote is about.
    """
    urt = Event(EventType.CLIENT_CALLVOTE, data="map ut4_casa")
    altitude = Event(
        EventType.CLIENT_CALLVOTE, data="kick Courgette", extra={"arguments": ["kick", "Courgette"]}
    )
    homefront = Event(
        EventType.CLIENT_CALLVOTE, data="kick", extra={"arguments": ["kick", "76561198000000000"]}
    )

    assert parse_vote(urt) == ("map", "ut4_casa")
    assert parse_vote(altitude) == ("kick", "Courgette")
    assert parse_vote(homefront) == ("kick", "76561198000000000")


def test_a_vote_type_is_read_case_insensitively():
    assert parse_vote(Event(EventType.CLIENT_CALLVOTE, data="MAP ut4_casa")) == ("map", "ut4_casa")
    assert parse_vote(Event(EventType.CLIENT_CALLVOTE, data="reload")) == ("reload", "")


# -- the level policy ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_vote_from_somebody_allowed_it_is_left_alone(console):
    plugin = _plugin(console, levels={"map": "guest"})
    caller = _join(console, "Joe")
    _join(console, "Jack")

    await _call(console, caller, "map ut4_casa")

    assert console.vote_cancels == 0
    assert plugin.current is not None
    assert plugin.current.type == "map"
    assert plugin.current.args == "ut4_casa"


@pytest.mark.asyncio
async def test_a_vote_from_somebody_below_the_level_is_cancelled(console):
    plugin = _plugin(console, levels={"kick": "admin"})
    caller = _join(console, "Sara", bits=1)  # a registered user, level 1
    _join(console, "Jack")

    await _call(console, caller, "kick jack")

    assert console.vote_cancels == 1
    assert plugin.current is None
    assert _told(console, caller) == ["you may not call a kick vote - that needs Admin"]


@pytest.mark.asyncio
async def test_an_unlisted_vote_type_gets_the_default_level(console):
    """The classic raised `KeyError` here, from a line outside the `try` written to catch it.

    Nothing was checked, nothing recorded and nothing said — and it happened for every vote type not
    among the thirty in its table, which is what a newer build or a votable-cvar list produces.
    """
    plugin = _plugin(console, levels={"map": "guest"}, default_level=40)
    caller = _join(console, "Joe")
    _join(console, "Jack")

    await _call(console, caller, "g_deadchat 1")

    assert console.vote_cancels == 1
    assert plugin.current is None
    assert any("g_deadchat" in text for text in _told(console, caller))


@pytest.mark.asyncio
async def test_an_unlisted_type_is_allowed_when_the_default_allows_it(console):
    plugin = _plugin(console, levels={"map": "admin"}, default_level=0)
    caller = _join(console, "Joe")
    _join(console, "Jack")

    await _call(console, caller, "g_deadchat 1")

    assert console.vote_cancels == 0
    assert plugin.current is not None


@pytest.mark.asyncio
async def test_a_map_in_the_special_list_has_its_own_level(console):
    _plugin(console, levels={"map": "guest"}, maps={"ut4_abbey": "admin"})
    caller = _join(console, "Sara", bits=1)
    _join(console, "Jack")

    await _call(console, caller, "map ut4_abbey")
    assert console.vote_cancels == 1

    await _call(console, caller, "map ut4_casa")
    assert console.vote_cancels == 1  # not in the list, so the level for `map` applies


@pytest.mark.asyncio
async def test_the_special_list_can_open_a_map_up_as_well_as_lock_it_down(console):
    """The classic's rule: a named map *replaces* the level for the type, either way.

    That is the more useful of the two readings — `map` locked to admins, with the two maps that
    always fill the server left open to everybody.
    """
    plugin = _plugin(console, levels={"map": "admin"}, maps={"ut4_casa": "guest"})
    caller = _join(console, "Joe")
    _join(console, "Jack")

    await _call(console, caller, "map ut4_casa")

    assert console.vote_cancels == 0
    assert plugin.current is not None


@pytest.mark.asyncio
async def test_a_junk_level_is_refused_at_load_and_the_rest_still_work(console):
    plugin = _plugin(console, levels={"map": "wizard", "kick": "admin"})

    assert "map" not in plugin.levels
    assert plugin.levels["kick"] == 40


def test_a_level_above_every_group_is_named_by_number(console):
    """`.name` on the group that does not exist — an `AttributeError` in front of the player."""
    assert group_name_for(40) == "Admin"
    assert group_name_for(2) == "Regular"
    assert group_name_for(3) == "Moderator"  # the lowest group that reaches the level
    assert group_name_for(101) == "level 101"


# -- when it stays out of the way ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_is_checked_with_too_few_voters(console):
    """A vote with one voter has already passed by the time its line is read."""
    plugin = _plugin(console, levels={"map": "admin"}, min_voters=2)
    caller = _join(console, "Joe")

    await _call(console, caller, "map ut4_casa")

    assert console.vote_cancels == 0
    assert plugin.current is not None  # still recorded when it finishes


@pytest.mark.asyncio
async def test_bots_are_not_voters(console):
    """One human among bots is one voter, and the classic counted eight.

    Its checks then ran on a server where the vote passes instantly, so the cancellation arrived
    after the map had already changed.
    """
    _plugin(console, levels={"map": "admin"}, min_voters=2)
    caller = _join(console, "Joe")
    _join(console, "[BOT]Rambo", is_bot=True)
    _join(console, "[BOT]Sarge", is_bot=True)

    await _call(console, caller, "map ut4_casa")

    assert console.vote_cancels == 0


@pytest.mark.asyncio
async def test_spectators_are_not_voters(console):
    _plugin(console, levels={"map": "admin"}, min_voters=2)
    caller = _join(console, "Joe")
    _join(console, "Watcher", team="spec")

    await _call(console, caller, "map ut4_casa")

    assert console.vote_cancels == 0


@pytest.mark.asyncio
async def test_an_engine_that_cannot_cancel_a_vote_enforces_nothing(console):
    """Homefront and Altitude announce a vote and take no instruction about it.

    The classic was declared Urban-Terror-only for this reason. Here the plugin loads anywhere, and
    what it cannot do it does not pretend to: no cancellation, and no message telling a player they
    may not do the thing they are about to succeed at.
    """
    console.votes_can_be_cancelled = False
    plugin = _plugin(console, levels={"kick": "admin"})
    caller = _join(console, "Sara", bits=1)
    _join(console, "Jack")

    await _call(console, caller, "kick jack")

    assert console.vote_cancels == 0
    assert _told(console, caller) == []
    assert plugin.current is not None  # and it is still recorded


# -- announcing the next map ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cyclemap_vote_says_what_comes_next(console):
    console.next_map = "ut4_turnpike"
    _plugin(console, levels={"cyclemap": "guest"})
    caller = _join(console, "Joe")
    _join(console, "Jack")

    await _call(console, caller, "cyclemap")

    assert console.said == ["next map: ut4_turnpike"]


@pytest.mark.asyncio
async def test_nothing_is_announced_when_the_server_does_not_know(console):
    console.next_map = None
    _plugin(console, levels={"cyclemap": "guest"})
    caller = _join(console, "Joe")
    _join(console, "Jack")

    await _call(console, caller, "cyclemap")

    assert console.said == []


@pytest.mark.asyncio
async def test_a_cancelled_vote_announces_nothing(console):
    console.next_map = "ut4_turnpike"
    _plugin(console, levels={"cyclemap": "admin"})
    caller = _join(console, "Sara", bits=1)
    _join(console, "Jack")

    await _call(console, caller, "cyclemap")

    assert console.said == []


# -- the record ----------------------------------------------------------------------------------


def _bot(tmp_path, *, game="iourt42", server_id="", **kwargs):  # noqa: ANN001, ANN202
    """A real bot on a real database: the history is the half of this plugin that persists."""
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
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}", server_id=server_id),
        server=ServerConfig(game=game),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="callvote")],
    )
    clock = FakeClock()
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=clock)
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = CallvotePlugin(bot, {"settings": kwargs.pop("settings", {}), **kwargs})
    bot.add_plugin(plugin, "callvote")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()
    return bot, rcon, clock, plugin


@pytest.mark.asyncio
async def test_a_finished_vote_is_written_down(tmp_path):
    bot, _rcon, clock, plugin = _bot(tmp_path)
    caller = Client(guid="j" * 32, name="Joe", cid="1", team="red")
    bot.storage.save_client(caller)
    bot.clients.add(caller)
    other = Client(guid="k" * 32, name="Jack", cid="2", team="blue")
    bot.storage.save_client(other)
    bot.clients.add(other)

    await bot.bus.publish(Event(EventType.CLIENT_CALLVOTE, data="map ut4_casa", client=caller))
    clock.advance(10)
    await bot.bus.publish(
        Event(EventType.VOTE_PASSED, data={"yes": 3, "no": 0, "what": "map ut4_casa"})
    )

    record = plugin.last_vote()
    assert record is not None
    assert (record.name, record.type, record.args) == ("Joe", "map", "ut4_casa")
    assert (record.yes, record.no, record.passed) == (3, 0, True)
    assert record.voters == 2
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_vote_whose_argument_is_sql_is_stored_as_the_string_it_is(tmp_path):
    """`callvote map <anything>` is a player writing the argument.

    The classic pasted it into `INSERT INTO callvote VALUES (NULL, '%s', ...)`, so the string below
    was SQL rather than data. Nothing here builds a statement out of it.
    """
    bot, _rcon, _clock, plugin = _bot(tmp_path)
    caller = Client(guid="j" * 32, name="Joe", cid="1", team="red")
    bot.storage.save_client(caller)
    bot.clients.add(caller)
    nasty = "ut4_casa'); DROP TABLE callvote_history; --"

    await bot.bus.publish(Event(EventType.CLIENT_CALLVOTE, data=f"map {nasty}", client=caller))
    await bot.bus.publish(
        Event(EventType.VOTE_FAILED, data={"yes": 0, "no": 2, "what": f"map {nasty}"})
    )

    record = plugin.last_vote()
    assert record is not None
    assert record.args == nasty
    assert plugin.last_vote() is not None  # the table is still there
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_vote_with_no_arguments_stores_no_arguments(tmp_path):
    """The classic stored the four characters `None`, because `'%s' % None` is `'None'`."""
    bot, _rcon, _clock, plugin = _bot(tmp_path)
    caller = Client(guid="j" * 32, name="Joe", cid="1", team="red")
    bot.storage.save_client(caller)
    bot.clients.add(caller)

    await bot.bus.publish(Event(EventType.CLIENT_CALLVOTE, data="reload", client=caller))
    await bot.bus.publish(Event(EventType.VOTE_PASSED, data={"yes": 2, "no": 0, "what": "reload"}))

    record = plugin.last_vote()
    assert record is not None
    assert record.args == ""
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_result_that_does_not_match_is_recorded_not_vetoed(tmp_path):
    """The classic vetoed here — a cancellation aimed at a vote that had already ended.

    It also threw the record away, so the one thing an admin could have read afterwards was gone.
    """
    bot, rcon, _clock, plugin = _bot(tmp_path)
    caller = Client(guid="j" * 32, name="Joe", cid="1", team="red")
    bot.storage.save_client(caller)
    bot.clients.add(caller)

    await bot.bus.publish(Event(EventType.CLIENT_CALLVOTE, data="map ut4_casa", client=caller))
    rcon.commands.clear()
    await bot.bus.publish(Event(EventType.VOTE_PASSED, data={"yes": 1, "no": 3, "what": "reload"}))

    assert "veto" not in " ".join(rcon.commands)
    record = plugin.last_vote()
    assert record is not None
    assert record.type == "reload"  # what finished, not what was hoped for
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_result_reported_in_another_case_is_the_same_vote(tmp_path):
    """The type was lower-cased when stored and compared raw at the finish."""
    bot, rcon, _clock, plugin = _bot(tmp_path)
    caller = Client(guid="j" * 32, name="Joe", cid="1", team="red")
    bot.storage.save_client(caller)
    bot.clients.add(caller)

    await bot.bus.publish(Event(EventType.CLIENT_CALLVOTE, data="Map ut4_casa", client=caller))
    rcon.commands.clear()
    await bot.bus.publish(
        Event(EventType.VOTE_PASSED, data={"yes": 2, "no": 0, "what": "MAP ut4_casa"})
    )

    assert "veto" not in " ".join(rcon.commands)
    record = plugin.last_vote()
    assert record is not None
    assert record.type == "map"
    bot.storage.close()


@pytest.mark.asyncio
async def test_counts_an_engine_does_not_report_are_not_invented(tmp_path):
    """Homefront states a yes count and a percentage. 0 against would be a claim, not a reading."""
    bot, _rcon, _clock, plugin = _bot(tmp_path, game="homefront")
    caller = Client(guid="7" * 17, name="Joe", cid="1", team="red")
    bot.storage.save_client(caller)
    bot.clients.add(caller)

    await bot.bus.publish(
        Event(
            EventType.CLIENT_CALLVOTE,
            data="kick",
            client=caller,
            extra={"arguments": ["kick", "somebody"]},
        )
    )
    await bot.bus.publish(
        Event(EventType.VOTE_PASSED, data="passed", extra={"yes_votes": 4, "percent_for": "80"})
    )

    record = plugin.last_vote()
    assert record is not None
    assert (record.type, record.args) == ("kick", "somebody")
    assert record.yes == 4
    assert record.no is None
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_history_is_per_server(tmp_path):
    """One database, two bots: the classic's `!lastvote` read whichever row was newest."""
    first, _r1, _c1, plugin_a = _bot(tmp_path, server_id="pub")
    caller = Client(guid="j" * 32, name="Joe", cid="1", team="red")
    first.storage.save_client(caller)
    first.clients.add(caller)
    await first.bus.publish(Event(EventType.CLIENT_CALLVOTE, data="map ut4_casa", client=caller))
    await first.bus.publish(
        Event(EventType.VOTE_PASSED, data={"yes": 2, "no": 0, "what": "map ut4_casa"})
    )
    first.storage.close()

    second, _r2, _c2, plugin_b = _bot(tmp_path, server_id="match")

    assert plugin_a.last_vote() is not None
    assert plugin_b.last_vote() is None
    second.storage.close()


# -- the commands --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_veto_cancels_the_running_vote(console):
    plugin = _plugin(console, levels={"map": "guest"})
    caller = _join(console, "Joe")
    admin = _join(console, "Admin", bits=128)
    await _call(console, caller, "map ut4_casa")

    await _run(console, admin, "!veto")

    assert console.vote_cancels == 1
    assert plugin.current is None
    assert any("cancelled" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_veto_with_nothing_running_says_so(console):
    """The classic sent the verb regardless and answered nothing at all."""
    _plugin(console)
    admin = _join(console, "Admin", bits=128)

    await _run(console, admin, "!veto")

    assert console.vote_cancels == 0
    assert any("no vote running" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_veto_on_an_engine_that_cannot_says_that(console):
    console.votes_can_be_cancelled = False
    _plugin(console)
    admin = _join(console, "Admin", bits=128)

    await _run(console, admin, "!veto")

    assert any("no way to cancel" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_lastvote_reports_the_vote_and_its_result(tmp_path):
    bot, rcon, clock, _plugin = _bot(tmp_path)
    caller = Client(guid="j" * 32, name="Sara", cid="1", team="red")
    bot.storage.save_client(caller)
    bot.clients.add(caller)
    admin = Client(guid="a" * 32, name="Admin", cid="2", group_bits=128, team="blue")
    bot.storage.save_client(admin)
    bot.clients.add(admin)

    await bot.bus.publish(Event(EventType.CLIENT_CALLVOTE, data="map ut4_casa", client=caller))
    await bot.bus.publish(
        Event(EventType.VOTE_PASSED, data={"yes": 3, "no": 0, "what": "map ut4_casa"})
    )
    clock.advance(10)
    rcon.commands.clear()

    await bot.run_command(admin, "!lastvote")
    await bot.bus.drain()

    said = " | ".join(rcon.commands)
    assert "Sara" in said
    assert "10 seconds ago" in said
    assert "3:0" in said
    bot.storage.close()


@pytest.mark.asyncio
async def test_lastvote_with_an_empty_history_says_so(tmp_path):
    bot, rcon, _clock, _plugin = _bot(tmp_path)
    admin = Client(guid="a" * 32, name="Admin", cid="2", group_bits=128, team="blue")
    bot.storage.save_client(admin)
    bot.clients.add(admin)

    await bot.run_command(admin, "!lastvote")
    await bot.bus.drain()

    assert any("no vote has been recorded" in c for c in rcon.commands)
    bot.storage.close()


# -- odds and ends -------------------------------------------------------------------------------


def test_durations_read_the_way_a_person_says_them():
    """The classic divided integers under Python 2, so two minutes less a second was "1 minute"."""
    assert format_duration(0) == "0 seconds"
    assert format_duration(1) == "1 second"
    assert format_duration(45) == "45 seconds"
    assert format_duration(60) == "1 minute"
    assert format_duration(119) == "2 minutes"
    assert format_duration(3600) == "1 hour"
    assert format_duration(7300) == "2 hours"


@pytest.mark.asyncio
async def test_a_vote_the_plugin_never_saw_start_is_not_recorded(tmp_path):
    bot, _rcon, _clock, plugin = _bot(tmp_path)

    await bot.bus.publish(
        Event(EventType.VOTE_PASSED, data={"yes": 3, "no": 0, "what": "map ut4_casa"})
    )

    assert plugin.last_vote() is None
    bot.storage.close()


@pytest.mark.asyncio
async def test_urban_terror_log_lines_drive_the_whole_thing(tmp_path):
    """The captured lines, through a real parser: `Callvote:` and `VotePassed:`.

    Sara is a registered user and `kick` needs an admin, so the vote is cancelled over rcon with the
    engine's own verb — which is the one part of this plugin that is genuinely per-title.
    """
    bot, rcon, _clock, _plugin = _bot(tmp_path, levels={"kick": "admin", "map": "guest"})

    await bot.replay(
        [
            f"ClientUserinfo: 1 \\name\\Sara\\cl_guid\\{'s' * 32}",
            f"ClientUserinfo: 2 \\name\\Jack\\cl_guid\\{'k' * 32}",
        ]
    )
    await bot.bus.drain()
    for cid, bits in (("1", 1), ("2", 0)):
        client = bot.clients.get_by_cid(cid)
        assert client is not None, f"slot {cid} was not created"
        client.group_bits = bits
        client.team = "red"
    rcon.commands.clear()

    await bot.replay(['Callvote: 1 - "kick jack"'])
    await bot.bus.drain()

    assert any(c.strip() == "veto" for c in rcon.commands), rcon.commands
    bot.storage.close()


# -- protect: the vote protector, from poweradminhf ----------------------------------------------


async def _finish(console, passed=True, what=None):  # noqa: ANN001, ANN202
    """A vote ending. Urban Terror repeats the vote and counts; Homefront states neither."""
    data = {"yes": 4, "no": 1, "what": what} if what else {}
    await console.bus.publish(
        Event(EventType.VOTE_PASSED if passed else EventType.VOTE_FAILED, data=data)
    )


@pytest.mark.asyncio
async def test_nobody_is_protected_until_an_operator_says_so(console):
    """Off by default, deliberately: this plugin runs on every family that reports a vote, and the
    Homefront plugin it comes from ran on one title. An upgrade must not start punishing players."""
    plugin = _plugin(console)
    caller = _join(console, "Joe")
    boss = _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await _call(console, caller, f"kick {boss.name}")

    assert console.vote_cancels == 0
    assert console.warned == []
    assert plugin.current is not None and plugin.current.protected is False


@pytest.mark.asyncio
async def test_a_vote_against_an_admin_is_cancelled_where_the_engine_can(console):
    """The classic could only warn and then undo: Homefront has no verb for cancelling a vote. On
    Urban Terror there is one, and stopping the vote is a better answer than mending it."""
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Joe")
    boss = _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await _call(console, caller, f"kick {boss.name}")

    assert console.vote_cancels == 1
    assert "Boss" in _told(console, caller)[-1]
    assert [c.name for c, _, _ in console.warned] == ["Joe"]


@pytest.mark.asyncio
async def test_a_vote_against_an_ordinary_player_is_not_touched(console):
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Joe")
    _join(console, "Jack")
    _join(console, "Bob")

    await _call(console, caller, "kick Bob")

    assert console.vote_cancels == 0
    assert console.warned == []


@pytest.mark.asyncio
async def test_an_admin_may_still_be_voted_against_by_a_senior_one(console):
    """The rule is the classic's: the target has to outrank the caller as well as reach the level. Two
    admins of the same rank disagreeing is not this plugin's business — they both have `!kick`."""
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Senior", bits=64)
    _join(console, "Mod", bits=8, cid="2")
    _join(console, "Jack", cid="3")

    await _call(console, caller, "kick Mod")

    assert console.vote_cancels == 0
    assert console.warned == []


@pytest.mark.asyncio
async def test_a_vote_that_names_two_players_protects_neither(console):
    """Acting on a guess is worse than not acting, and the level policy still applies either way."""
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Joe")
    _join(console, "Boss", bits=128)
    console.register_clients("bo", [_join(console, "Bob"), _join(console, "Bobby")])

    await _call(console, caller, "kick bo")

    assert console.vote_cancels == 0


@pytest.mark.asyncio
async def test_a_ban_vote_that_passes_is_undone_where_it_could_not_be_stopped(console):
    """Homefront's case: no veto verb, so the caller is warned when the vote starts and the ban is
    lifted when it passes. The classic did both and this keeps both."""
    console.votes_can_be_cancelled = False
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Joe")
    boss = _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await _call(console, caller, f"ban {boss.name}")
    assert console.vote_cancels == 0
    assert [c.name for c, _, _ in console.warned] == ["Joe"]

    await _finish(console, passed=True)

    assert [c.name for c, _, _ in console.unbanned] == ["Boss"]
    assert "Boss" in console.said[-1]


@pytest.mark.asyncio
async def test_a_kick_vote_that_passes_has_nothing_to_undo(console):
    """A kicked player is already gone and can reconnect; there is no ban to lift, and pretending
    otherwise would tell an admin something had been mended."""
    console.votes_can_be_cancelled = False
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Joe")
    boss = _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await _call(console, caller, f"kick {boss.name}")
    await _finish(console, passed=True)

    assert console.unbanned == []


@pytest.mark.asyncio
async def test_a_protected_vote_that_fails_is_left_alone(console):
    console.votes_can_be_cancelled = False
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Joe")
    boss = _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await _call(console, caller, f"ban {boss.name}")
    await _finish(console, passed=False)

    assert console.unbanned == []


@pytest.mark.asyncio
async def test_the_warning_can_be_switched_off_without_losing_the_undo(console):
    console.votes_can_be_cancelled = False
    _plugin(console, protect={"level": "mod", "warn": False})
    caller = _join(console, "Joe")
    boss = _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await _call(console, caller, f"ban {boss.name}")
    await _finish(console, passed=True)

    assert console.warned == []
    assert [c.name for c, _, _ in console.unbanned] == ["Boss"]


@pytest.mark.asyncio
async def test_a_homefront_vote_names_its_target_on_the_event(console):
    """That engine resolves the player itself, so there is nothing to look up — and the vote line
    carries a Steam id, which no name lookup would find."""
    _plugin(console, protect={"level": "mod"})
    caller = _join(console, "Joe")
    boss = _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await console.bus.publish(
        Event(
            EventType.CLIENT_CALLVOTE,
            data="kick",
            client=caller,
            target=boss,
            extra={"arguments": ["kick", boss.guid]},
        )
    )

    assert console.vote_cancels == 1


@pytest.mark.asyncio
async def test_a_protect_level_that_is_not_one_is_reported_and_protects_nobody(console, caplog):
    with caplog.at_level("ERROR"):
        _plugin(console, protect={"level": "moderator"})
    caller = _join(console, "Joe")
    _join(console, "Boss", bits=128)
    _join(console, "Jack")

    await _call(console, caller, "kick Boss")

    assert console.vote_cancels == 0
    assert "protect.level" in caplog.text


# -- the game's own ban, on a game that applies one ------------------------------------------------


@pytest.mark.asyncio
async def test_a_protected_player_vote_kicked_gets_the_games_own_ban_lifted(console):
    """Altitude bans a player it kicks by vote for two minutes, by itself. An admin who loses a vote
    cannot come back and undo that, which is why the classic did it from inside its parser."""
    _plugin(console, protect={"level": "mod"})
    console.player_verbs = {"lift_ban"}
    boss = _join(console, "Boss", bits=128)

    await console.bus.publish(
        Event(EventType.CLIENT_DISCONNECT, client=boss, extra={"vote_kick": True})
    )

    assert [(name, c.name) for name, c, _ in console.verbs_applied] == [("lift_ban", "Boss")]


@pytest.mark.asyncio
async def test_an_ordinary_players_vote_kick_ban_stands(console):
    _plugin(console, protect={"level": "mod"})
    console.player_verbs = {"lift_ban"}
    joe = _join(console, "Joe")

    await console.bus.publish(
        Event(EventType.CLIENT_DISCONNECT, client=joe, extra={"vote_kick": True})
    )

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_leaving_of_your_own_accord_lifts_nothing(console):
    _plugin(console, protect={"level": "mod"})
    console.player_verbs = {"lift_ban"}
    boss = _join(console, "Boss", bits=128)

    await console.bus.publish(
        Event(EventType.CLIENT_DISCONNECT, client=boss, extra={"vote_kick": False})
    )

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_a_title_that_does_not_ban_on_a_vote_kick_is_not_sent_a_verb(console):
    """Every game but Altitude, where a kick is a kick and there is nothing to lift."""
    _plugin(console, protect={"level": "mod"})
    console.player_verbs = set()
    boss = _join(console, "Boss", bits=128)

    await console.bus.publish(
        Event(EventType.CLIENT_DISCONNECT, client=boss, extra={"vote_kick": True})
    )

    assert console.verbs_applied == []


@pytest.mark.asyncio
async def test_with_protection_off_the_ban_is_left_alone(console):
    """Off is off: this is the same setting that decides who may be voted against at all."""
    _plugin(console)
    console.player_verbs = {"lift_ban"}
    boss = _join(console, "Boss", bits=128)

    await console.bus.publish(
        Event(EventType.CLIENT_DISCONNECT, client=boss, extra={"vote_kick": True})
    )

    assert console.verbs_applied == []


def test_altitude_declares_the_verb_that_lifts_its_own_ban():
    """And it is *not* `Console.unban`, which would also lift whatever ban the bot holds on them —
    on a vote-kicked admin, precisely the record nobody wants dropped."""
    from b3.parsers.altitude.profiles import ALTITUDE

    assert ALTITUDE.player_verbs["lift_ban"] == "removeBan %(guid)s"
