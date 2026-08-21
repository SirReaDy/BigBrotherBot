"""The bundled plugins ported from the classic tree: spamcontrol, censor, pingwatch.

Each is driven through a real event or a real command, because the interesting part of a plugin is
its wiring, not its arithmetic.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client, PenaltyType
from b3.plugins.censor import CensorPlugin
from b3.plugins.pingwatch import PingwatchPlugin
from b3.plugins.spamcontrol import SpamcontrolPlugin


def _client(name="Bob", bits=0, cid="2", id_=7):  # noqa: ANN001, ANN202
    return Client(guid=name[0].upper(), name=name, cid=cid, id=id_, group_bits=bits)


async def _say(console, plugin, client, text, event=EventType.CLIENT_SAY):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(event, client=client, data=text))


# -- spamcontrol -------------------------------------------------------------------------------


def _spam(console, **settings):  # noqa: ANN001, ANN202
    plugin = SpamcontrolPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


@pytest.mark.asyncio
async def test_repeating_yourself_scores_worse_than_saying_something_new(console):
    plugin = _spam(console)
    bob = _client()

    await _say(console, plugin, bob, "hello")
    first = plugin.score(bob)
    await _say(console, plugin, bob, "hello")

    assert plugin.score(bob) - first == 3  # a repeat, versus 1 for a fresh line


@pytest.mark.asyncio
async def test_a_coloured_repeat_scores_worst_of_all(console):
    plugin = _spam(console)
    bob = _client()

    await _say(console, plugin, bob, "^1buy gold at example.com")
    await _say(console, plugin, bob, "^1buy gold at example.com")

    assert plugin.score(bob) == 7  # 2 for the colour, then 5 for the coloured repeat


@pytest.mark.asyncio
async def test_flooding_earns_a_warning_and_resets_the_score(console):
    plugin = _spam(console, max_spamins=5)
    bob = _client()

    for _ in range(5):
        await _say(console, plugin, bob, "spam spam spam")

    assert console.warned  # warned once the score crossed the threshold
    assert plugin.score(bob) == 0  # and the count starts again


@pytest.mark.asyncio
async def test_points_decay_so_an_ordinary_talker_never_trips_it(console):
    plugin = _spam(console, falloff_rate=10)
    bob = _client()

    await _say(console, plugin, bob, "nice shot")
    await _say(console, plugin, bob, "thanks")
    assert plugin.score(bob) == 2

    console.clock.advance(15)  # a decent pause in conversation
    assert plugin.score(bob) == pytest.approx(0.5)

    console.clock.advance(60)
    assert plugin.score(bob) == 0


@pytest.mark.asyncio
async def test_admins_are_not_scored(console):
    plugin = _spam(console)
    admin = _client("Admin", bits=8)  # mod, level 20

    for _ in range(20):
        await _say(console, plugin, admin, "same thing again")

    assert console.warned == []
    assert plugin.score(admin) == 0


@pytest.mark.asyncio
async def test_spamins_reports_a_score(console):
    plugin = _spam(console)
    bob = _client()
    console.register_client("Bob", bob)
    await _say(console, plugin, bob, "hello")

    proc = CommandProcessor(console.command_registry, console)
    await proc.handle(_client("Admin", bits=128, cid="1", id_=1), "!spamins Bob")

    assert "Bob has 1" in console.told[-1][1]


# -- censor ------------------------------------------------------------------------------------

CENSOR_CONFIG = {
    "settings": {"max_level": 40, "ignore_length": 3},
    "default_penalty": {"penalty": "warning", "reason": "watch your language"},
    "badwords": [
        {"name": "shit", "regexp": r"[s$]h[i!1*]+t\b"},
        {"name": "ass", "word": "ass"},
        {"name": "slur", "word": "slur", "penalty": "kick", "reason": "not here"},
        {"name": "worst", "word": "worst", "penalty": "tempban", "duration": "2h"},
    ],
    "badnames": [{"name": "badnick", "word": "cheater"}],
}


def _censor(console, config=None, **mute):  # noqa: ANN001, ANN202
    settings = CENSOR_CONFIG if config is None else config
    if mute:
        settings = {**settings, "mute": mute}
    plugin = CensorPlugin(console, settings)
    plugin.start()
    return plugin


@pytest.mark.asyncio
async def test_a_bad_word_earns_the_default_penalty(console):
    plugin = _censor(console)
    bob = _client()

    await _say(console, plugin, bob, "oh sh1t that was close")

    assert console.warned[-1][0] is bob
    assert console.warned[-1][1] == "watch your language"


@pytest.mark.asyncio
async def test_a_word_is_matched_on_boundaries_not_as_a_substring(console):
    """The Scunthorpe problem: the classic plugin kicked people for saying "class"."""
    plugin = _censor(console)
    bob = _client()

    await _say(console, plugin, bob, "nice class of players, glass everywhere")
    assert console.warned == []

    await _say(console, plugin, bob, "you ass")
    assert console.warned != []


@pytest.mark.asyncio
async def test_an_entry_can_carry_its_own_penalty(console):
    plugin = _censor(console)
    bob = _client()

    await _say(console, plugin, bob, "that is a slur")

    assert console.kicked[-1][0] is bob
    assert console.kicked[-1][1] == "not here"


@pytest.mark.asyncio
async def test_a_tempban_entry_uses_its_duration(console):
    plugin = _censor(console)
    bob = _client()

    await _say(console, plugin, bob, "the worst thing")

    assert console.tempbanned[-1][1] == 120  # 2h


@pytest.mark.asyncio
async def test_short_messages_are_not_checked(console):
    plugin = _censor(console)
    await _say(console, plugin, _client(), "ass")  # 3 chars, at the ignore_length
    assert console.warned == []


@pytest.mark.asyncio
async def test_admins_above_the_level_are_not_censored(console):
    plugin = _censor(console)
    senior = _client("Senior", bits=64)  # level 80 > max_level 40

    await _say(console, plugin, senior, "oh sh1t")

    assert console.warned == []


@pytest.mark.asyncio
async def test_a_bad_name_is_caught_when_the_player_authenticates(console):
    _censor(console)
    bob = _client("xXcheaterXx")  # no word boundaries — names are matched anywhere

    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=bob))

    assert console.warned[-1][0] is bob


def test_a_broken_regex_is_reported_and_skipped_rather_than_breaking_chat(console, caplog):
    """The XML version accepted a bad pattern happily, then raised on every chat line."""
    with caplog.at_level("WARNING"):
        plugin = _censor(console, {"badwords": [{"name": "oops", "regexp": "([unclosed"}]})

    assert plugin.badwords == []
    assert "not a valid regular expression" in caplog.text


def test_an_entry_with_no_pattern_is_reported(console, caplog):
    with caplog.at_level("WARNING"):
        plugin = _censor(console, {"badwords": [{"name": "empty"}]})
    assert plugin.badwords == []
    assert "neither" in caplog.text


def test_censor_runs_with_no_config_at_all(console):
    plugin = CensorPlugin(console)
    plugin.start()
    assert plugin.badwords == []


# -- pingwatch ---------------------------------------------------------------------------------


def _pingwatch(console, **settings):  # noqa: ANN001, ANN202
    settings.setdefault("settle_time", 0)  # most tests are not about the post-map-change grace
    plugin = PingwatchPlugin(console, {"settings": settings})
    plugin.start()
    return plugin


def test_a_ping_spike_is_forgiven(console):
    """Every connection spikes; only a sustained one is worth acting on."""
    plugin = _pingwatch(console, max_ping=200, max_ping_duration=90)
    bob = _client()
    console.clients.add(bob)
    console.players = [PlayerInfo(cid="2", name="Bob", ping=800)]

    plugin.check()  # first sighting: noted, not punished
    console.clock.advance(30)
    console.players = [PlayerInfo(cid="2", name="Bob", ping=50)]  # recovered
    plugin.check()

    assert console.kicked == []


def test_a_sustained_high_ping_is_kicked(console):
    plugin = _pingwatch(console, max_ping=200, max_ping_duration=90)
    bob = _client()
    console.clients.add(bob)
    console.players = [PlayerInfo(cid="2", name="Bob", ping=800)]

    plugin.check()
    console.clock.advance(120)
    plugin.check()

    assert console.kicked[-1][0] is bob
    assert "800" in console.kicked[-1][1]


def test_a_lost_connection_is_announced_as_such(console):
    plugin = _pingwatch(console, max_ping=200, max_ping_duration=10)
    bob = _client()
    console.clients.add(bob)
    console.players = [PlayerInfo(cid="2", name="Bob", ping=999)]

    plugin.check()
    console.clock.advance(20)
    plugin.check()

    assert any("connection was interrupted" in said for said in console.said)
    assert console.kicked != []


def test_admins_are_not_kicked_for_their_ping(console):
    plugin = _pingwatch(console, max_ping=200, max_ping_duration=10)
    admin = _client("Admin", bits=8)  # level 20 == kick_level
    console.clients.add(admin)
    console.players = [PlayerInfo(cid="2", name="Admin", ping=999)]

    plugin.check()
    console.clock.advance(20)
    plugin.check()

    assert console.kicked == []


def test_nothing_is_checked_while_the_server_settles(console):
    """Every ping spikes while a map loads; kicking half the server after a rotation is worse
    than missing a lagger."""
    plugin = _pingwatch(console, max_ping=200, max_ping_duration=10, settle_time=120)
    bob = _client()
    console.clients.add(bob)
    console.players = [PlayerInfo(cid="2", name="Bob", ping=999)]

    plugin.check()
    console.clock.advance(60)  # still inside the grace period
    plugin.check()
    assert console.kicked == []

    console.clock.advance(120)  # settled, and still terrible
    plugin.check()
    console.clock.advance(60)
    plugin.check()
    assert console.kicked != []


@pytest.mark.asyncio
async def test_the_end_of_a_map_starts_the_grace_period_again(console):
    plugin = _pingwatch(console, max_ping=200, max_ping_duration=10, settle_time=120)
    console.clock.advance(200)  # the initial grace has passed

    await console.bus.publish(Event(EventType.GAME_EXIT, data="executed"))

    assert plugin.ignore_until == console.clock.now() + 120


def test_a_dead_server_does_not_break_the_check(console):
    plugin = _pingwatch(console)

    def boom():
        raise OSError("rcon unreachable")

    console.get_players = boom
    plugin.check()  # must not raise


@pytest.mark.asyncio
async def test_the_ci_command_reports_a_healthy_player_instead_of_kicking(console):
    _pingwatch(console)
    bob = _client()
    console.register_client("Bob", bob)
    console.players = [PlayerInfo(cid="2", name="Bob", ping=60)]

    proc = CommandProcessor(console.command_registry, console)
    await proc.handle(_client("Admin", bits=128, cid="1", id_=1), "!ci Bob")

    assert console.kicked == []
    assert "ping of 60" in console.told[-1][1]


@pytest.mark.asyncio
async def test_the_ci_command_removes_an_interrupted_player(console):
    _pingwatch(console)
    bob = _client()
    console.register_client("Bob", bob)
    console.players = [PlayerInfo(cid="2", name="Bob", ping=999)]

    proc = CommandProcessor(console.command_registry, console)
    await proc.handle(_client("Admin", bits=128, cid="1", id_=1), "!ci Bob")

    assert console.kicked[-1][0] is bob


# -- they load the way the loader expects ---------------------------------------------------


@pytest.mark.parametrize("name", ["spamcontrol", "censor", "pingwatch"])
def test_each_is_resolvable_by_name_like_any_bundled_plugin(name):
    from b3.config.schema import PluginEntry
    from b3.core.pluginmgr import resolve_plugin_class

    assert issubclass(resolve_plugin_class(PluginEntry(name=name)), object)


@pytest.mark.asyncio
async def test_a_censor_warning_escalates_like_any_other(console):
    """Warnings from a plugin count towards the admin plugin's kick ladder — the point of
    hanging escalation off the event rather than off `!warn`."""
    from b3.plugins.admin import AdminPlugin

    admin = AdminPlugin(console, {"warn": {"delay": 0, "alert_at": 99, "kick_at": 2}})
    admin.start()
    censor = _censor(console)
    bob = _client()

    await _say(console, censor, bob, "oh sh1t")
    await console.bus.drain()
    await _say(console, censor, bob, "oh sh1t again")
    await console.bus.drain()

    assert len(console.storage.get_active_penalties(bob.id, PenaltyType.WARNING)) == 2
    assert console.tempbanned != []  # two warnings hit the configured kick threshold


# -- censor's escalating mute, which was the whole of the `censorurt` plugin ---------------------


@pytest.mark.asyncio
async def test_a_bad_word_can_mute_instead_of_warning(console):
    """The classic had a separate plugin (a *subclass* of censor) to do this on Urban Terror."""
    plugin = _censor(console, mute_minutes=[1, 5, 30], warn_after=3)
    bob = _client()
    console.clients.add(bob)

    await _say(console, plugin, bob, "you are an ass")

    assert [(name, who.name, values) for name, who, values in console.verbs_applied] == [
        ("mute", "Bob", {"seconds": "60"})
    ]
    assert console.warned == []  # the mute took the place of the penalty
    assert any("muted for 1 minutes" in text for text in console.said)


@pytest.mark.asyncio
async def test_the_mute_gets_longer_each_time(console):
    plugin = _censor(console, mute_minutes=[1, 5, 30], warn_after=9)
    bob = _client()
    console.clients.add(bob)

    for minutes in (1, 5, 30, 30):
        await _say(console, plugin, bob, "you are an ass")
        console.clock.advance(minutes * 60 + 1)
        plugin._check_mutes()

    assert [values["seconds"] for _n, _w, values in console.verbs_applied if _n == "mute"] == [
        "60",
        "0",
        "300",
        "0",
        "1800",
        "0",
        "1800",
        "0",
    ]


@pytest.mark.asyncio
async def test_a_mute_is_lifted_when_it_runs_out(console):
    """One scheduled task for the whole server, where the classic started a timer per player."""
    plugin = _censor(console, mute_minutes=[2])
    bob = _client()
    console.clients.add(bob)
    await _say(console, plugin, bob, "you are an ass")

    console.clock.advance(60)
    plugin._check_mutes()
    assert [n for n, _w, _v in console.verbs_applied] == ["mute"]  # still muted

    console.clock.advance(61)
    plugin._check_mutes()

    assert console.verbs_applied[-1] == ("mute", bob, {"seconds": "0"})
    assert any("can talk again" in text for _who, text in console.told)


@pytest.mark.asyncio
async def test_saying_it_again_while_muted_does_not_stack(console):
    plugin = _censor(console, mute_minutes=[5])
    bob = _client()
    console.clients.add(bob)

    await _say(console, plugin, bob, "you are an ass")
    await _say(console, plugin, bob, "you are an ass")

    assert len([n for n, _w, _v in console.verbs_applied if n == "mute"]) == 1


@pytest.mark.asyncio
async def test_after_warn_after_offences_the_words_own_penalty_applies_too(console):
    """Somebody who will not stop still gets kicked — the classic's rule and the point of the limit."""
    plugin = _censor(console, mute_minutes=[1], warn_after=2)
    bob = _client()
    console.clients.add(bob)

    for _ in range(3):
        await _say(console, plugin, bob, "you are an ass")
        console.clock.advance(120)
        plugin._check_mutes()

    assert len(console.warned) == 1  # only the third offence fell through to the penalty


@pytest.mark.asyncio
async def test_a_slap_can_come_with_it(console):
    plugin = _censor(console, mute_minutes=[1], slap=True)
    bob = _client()
    console.clients.add(bob)

    await _say(console, plugin, bob, "you are an ass")

    assert [n for n, _w, _v in console.verbs_applied] == ["slap", "mute"]


@pytest.mark.asyncio
async def test_on_an_engine_with_no_mute_verb_the_section_is_refused(console):
    """Every family but Urban Terror. The penalties still apply; nothing silently does nothing."""
    console.player_verbs = set()
    plugin = _censor(console, mute_minutes=[1, 5], slap=True)
    bob = _client()
    console.clients.add(bob)

    await _say(console, plugin, bob, "you are an ass")

    assert plugin.mute_ladder == []
    assert console.verbs_applied == []
    assert len(console.warned) == 1


@pytest.mark.asyncio
async def test_with_no_mute_configured_censor_behaves_exactly_as_before(console):
    plugin = _censor(console)
    bob = _client()
    console.clients.add(bob)

    await _say(console, plugin, bob, "you are an ass")

    assert console.verbs_applied == []
    assert len(console.warned) == 1


@pytest.mark.asyncio
async def test_a_player_who_leaves_takes_their_mute_record_with_them(console):
    """A mute is attached to a slot on the server, so the next player in it must not inherit one."""
    plugin = _censor(console, mute_minutes=[1, 5])
    bob = _client()
    console.clients.add(bob)
    await _say(console, plugin, bob, "you are an ass")

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bob))

    assert plugin._offences(bob) == 0
    assert plugin._muted_until(bob) == 0.0
