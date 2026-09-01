"""The `report` plugin — a player telling the bot that somebody is worth looking at.

New here rather than ported, so there are no captured tests to match. What is worth testing is the
part that makes a report different from a line of chat: it identifies one player, it cannot be
spammed, it reaches the admins who are in the game *and* the ones who are not, and it does nothing
to the player named. That last one has a test of its own because it is the design: the moment a
report acts on its own, three players who dislike a fourth can remove them.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.report import ReportPlugin


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    plugin = ReportPlugin(console, {"settings": settings})
    plugin.start()
    # The reports this run produced. Subscribed here rather than read off the bus, because the bus
    # keeps no history — and what reaches a subscriber is exactly what `discord` will see.
    console.reports = []
    console.bus.subscribe(EventType.CLIENT_REPORT, console.reports.append)
    return plugin


def _join(console, name, cid=None, bits=1):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), group_bits=bits
    )
    console.clients.add(client)
    console.register_client(name, client)
    return client


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)
    # The report is published fire-and-forget, as every event a command raises here is.
    await console.bus.drain()


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


def _reports(console):  # noqa: ANN001, ANN202
    return console.reports


# -- what a report is ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_report_names_the_player_and_reaches_the_admins_here(console):
    _plugin(console)
    reporter = _join(console, "Ann")
    _join(console, "Bob")
    mod = _join(console, "Mod", bits=8)  # level 20

    await _run(console, reporter, "!report Bob aimbotting")

    assert "Ann reported Bob: aimbotting" in _told(console, mod)[-1]
    assert "reported" in _told(console, reporter)[-1]


@pytest.mark.asyncio
async def test_a_report_publishes_the_event_that_reaches_everybody_else(console):
    """The point of the plugin: an admin who is not in the game still hears about it."""
    _plugin(console)
    reporter = _join(console, "Ann")
    bob = _join(console, "Bob")

    await _run(console, reporter, "!report Bob wallhack")

    event = _reports(console)[-1]
    assert event.client is bob, "the reported player is the subject, as with every penalty here"
    assert event.target is reporter
    assert event.data == "wallhack"


@pytest.mark.asyncio
async def test_a_report_does_nothing_to_the_player(console):
    """A report that acted on its own would be a way for three players to remove a fourth."""
    _plugin(console)
    reporter = _join(console, "Ann")
    _join(console, "Bob")

    await _run(console, reporter, "!report Bob cheating")

    assert console.kicked == [] and console.banned == [] and console.warned == []


@pytest.mark.asyncio
async def test_the_report_still_goes_out_when_no_admin_is_in_the_game(console):
    """This is the case the plugin exists for, so the reporter is told the truth about it."""
    _plugin(console)
    reporter = _join(console, "Ann")
    _join(console, "Bob")

    await _run(console, reporter, "!report Bob cheating")

    assert _reports(console), "the event is published either way"
    assert "no admin is in the game" in _told(console, reporter)[-1]


# -- the guards ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reason_is_required(console):
    """A bare name in a channel is something an admin has to come back and ask about."""
    _plugin(console)
    reporter = _join(console, "Ann")
    _join(console, "Bob")

    await _run(console, reporter, "!report Bob")

    assert "say what they are doing" in _told(console, reporter)[-1]
    assert _reports(console) == []


@pytest.mark.asyncio
async def test_a_reason_can_be_made_optional(console):
    _plugin(console, force_reason=False)
    reporter = _join(console, "Ann")
    _join(console, "Bob")

    await _run(console, reporter, "!report Bob")

    assert _reports(console), "the report goes through"


@pytest.mark.asyncio
async def test_nobody_can_report_themselves(console):
    _plugin(console)
    reporter = _join(console, "Ann")

    await _run(console, reporter, "!report Ann being great")

    assert "cannot report yourself" in _told(console, reporter)[-1]
    assert _reports(console) == []


@pytest.mark.asyncio
async def test_a_second_report_too_soon_is_refused_with_the_wait(console):
    _plugin(console, cooldown=60)
    reporter = _join(console, "Ann")
    _join(console, "Bob")
    _join(console, "Cat")

    await _run(console, reporter, "!report Bob cheating")
    await _run(console, reporter, "!report Cat also cheating")

    assert "wait" in _told(console, reporter)[-1]
    assert len(_reports(console)) == 1

    console.clock.advance(61)
    await _run(console, reporter, "!report Cat also cheating")
    assert len(_reports(console)) == 2


@pytest.mark.asyncio
async def test_reporting_the_same_player_again_is_answered_not_announced_twice(console):
    _plugin(console, cooldown=0, duplicate_gap=300)
    reporter = _join(console, "Ann")
    _join(console, "Bob")

    await _run(console, reporter, "!report Bob cheating")
    await _run(console, reporter, "!report Bob still cheating")

    assert "already been reported" in _told(console, reporter)[-1]
    assert len(_reports(console)) == 1


@pytest.mark.asyncio
async def test_two_players_reporting_the_same_cheater_both_get_through(console):
    """It is one player complaining twice that is noise; two agreeing is the opposite."""
    _plugin(console, cooldown=0)
    ann = _join(console, "Ann")
    _join(console, "Bob")
    cat = _join(console, "Cat")

    await _run(console, ann, "!report Bob cheating")
    await _run(console, cat, "!report Bob cheating")

    assert len(_reports(console)) == 2


@pytest.mark.asyncio
async def test_an_ambiguous_name_is_refused_rather_than_reported_against_the_wrong_player(console):
    _plugin(console)
    reporter = _join(console, "Ann")
    bobby = _join(console, "Bobby")
    bobbie = _join(console, "Bobbie")
    console.register_clients("Bob", [bobby, bobbie])

    await _run(console, reporter, "!report Bob cheating")

    assert _reports(console) == []
    assert "Bobby" in _told(console, reporter)[-1] and "Bobbie" in _told(console, reporter)[-1]


@pytest.mark.asyncio
async def test_the_reported_player_is_not_told_by_default(console):
    """Announcing it is how a reporter gets shot for the rest of the map."""
    _plugin(console)
    reporter = _join(console, "Ann")
    _join(console, "Bob")

    await _run(console, reporter, "!report Bob cheating")

    assert console.said == []


@pytest.mark.asyncio
async def test_announcing_can_be_switched_on(console):
    _plugin(console, announce=True)
    reporter = _join(console, "Ann")
    _join(console, "Bob")

    await _run(console, reporter, "!report Bob cheating")

    assert "Bob has been reported" in console.said[-1]
