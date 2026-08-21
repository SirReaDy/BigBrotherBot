"""The `duel` plugin — two players settling something.

The captured tests for this one are a single file that exercises the handshake, so the handshake is
covered here in the same shape. The fault they could not show is what counts as a kill: the classic
listened for `EVT_CLIENT_KILL` alone, so **two players on the same team duelling each other never
scored** — their kills arrive as team kills — and on Enemy Territory, where most kills are gibs,
no duel scored at all.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.duel import DuelPlugin


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    plugin = DuelPlugin(console, {"settings": settings})
    plugin.start()
    return plugin


def _join(console, name, cid=None, bits=1, team="red"):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), group_bits=bits
    )
    client.team = team
    console.clients.add(client)
    console.register_client(name, client)
    return client


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


async def _kill(console, killer, victim, event_type=EventType.CLIENT_KILL):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(event_type, client=killer, target=victim, data=None))


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


def _last(console, client):  # noqa: ANN001, ANN202
    told = _told(console, client)
    return told[-1] if told else ""


# -- the handshake -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_challenge_has_to_be_accepted(console):
    plugin = _plugin(console)
    bob, ann = _join(console, "Bob"), _join(console, "Ann")

    await _run(console, bob, "!duel Ann")

    assert len(plugin.duels) == 1
    assert plugin.duels[0].accepted is False
    assert "challenged Ann" in _last(console, bob)
    assert "Bob challenges you" in _last(console, ann)


@pytest.mark.asyncio
async def test_accepting_starts_the_scoring(console):
    plugin = _plugin(console)
    bob, ann = _join(console, "Bob"), _join(console, "Ann")

    await _run(console, bob, "!duel Ann")
    await _run(console, ann, "!duel Bob")

    assert plugin.duels[0].accepted is True
    assert any("accepted Bob's challenge" in text for text in _told(console, ann))
    assert any("duel: you 0 - 0 Bob" in text for text in _told(console, ann))


@pytest.mark.asyncio
async def test_nothing_is_scored_before_it_is_accepted(console):
    plugin = _plugin(console)
    bob, ann = _join(console, "Bob"), _join(console, "Ann")
    await _run(console, bob, "!duel Ann")

    await _kill(console, bob, ann)

    assert plugin.duels[0].scores == {}


@pytest.mark.asyncio
async def test_challenging_the_same_player_again_nudges_them(console):
    plugin = _plugin(console)
    bob, ann = _join(console, "Bob"), _join(console, "Ann")

    await _run(console, bob, "!duel Ann")
    console.told.clear()
    await _run(console, bob, "!duel Ann")

    assert len(plugin.duels) == 1
    assert "Bob challenges you" in _last(console, ann)


@pytest.mark.asyncio
async def test_you_cannot_duel_yourself(console):
    plugin = _plugin(console)
    bob = _join(console, "Bob")

    await _run(console, bob, "!duel Bob")

    assert plugin.duels == []
    assert "cannot duel yourself" in _last(console, bob)


@pytest.mark.asyncio
async def test_duelling_somebody_you_already_duel_says_so(console):
    _plugin(console)
    bob, ann = _join(console, "Bob"), _join(console, "Ann")
    await _run(console, bob, "!duel Ann")
    await _run(console, ann, "!duel Bob")

    await _run(console, bob, "!duel Ann")

    assert "already duelling Ann" in _last(console, bob)


@pytest.mark.asyncio
async def test_duel_with_no_name_explains_itself(console):
    _plugin(console)
    bob = _join(console, "Bob")

    await _run(console, bob, "!duel")

    assert "challenge somebody" in _last(console, bob)


# -- scoring -------------------------------------------------------------------------------------


async def _accepted(console, plugin):  # noqa: ANN001, ANN202
    bob, ann = _join(console, "Bob"), _join(console, "Ann")
    await _run(console, bob, "!duel Ann")
    await _run(console, ann, "!duel Bob")
    console.told.clear()
    return bob, ann


@pytest.mark.asyncio
async def test_a_kill_between_the_two_is_counted_and_told_to_both(console):
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)

    await _kill(console, bob, ann)

    assert plugin.duels[0].score_of(bob) == 1
    assert plugin.duels[0].score_of(ann) == 0
    assert _told(console, bob) == ["duel: you 1 - 0 Ann"]
    assert _told(console, ann) == ["duel: you 0 - 1 Bob"]


@pytest.mark.asyncio
async def test_a_team_kill_between_two_duellists_counts(console):
    """The fault: two players on the same team are the commonest duel there is, and their kills
    arrive as team kills — so the classic's duel between teammates stayed 0:0 forever."""
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)

    await _kill(console, bob, ann, event_type=EventType.CLIENT_KILL_TEAM)

    assert plugin.duels[0].score_of(bob) == 1


@pytest.mark.asyncio
async def test_a_gib_counts(console):
    """Enemy Territory reports most kills as gibs, so no duel there ever scored."""
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)

    await _kill(console, bob, ann, event_type=EventType.CLIENT_GIB)

    assert plugin.duels[0].score_of(bob) == 1


@pytest.mark.asyncio
async def test_a_kill_involving_somebody_else_is_not_counted(console):
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)
    sue = _join(console, "Sue")

    await _kill(console, bob, sue)
    await _kill(console, sue, ann)

    assert plugin.duels[0].scores == {}
    assert _told(console, sue) == []


@pytest.mark.asyncio
async def test_the_score_survives_a_rename(console):
    """Scores are kept per side, not keyed by the player's name or object."""
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)
    await _kill(console, bob, ann)

    bob.name = "Robert"
    await _kill(console, bob, ann)

    assert plugin.duels[0].score_of(bob) == 2


@pytest.mark.asyncio
async def test_the_end_of_a_round_reports_the_score(console):
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)
    await _kill(console, bob, ann)
    console.told.clear()

    await console.bus.publish(Event(EventType.GAME_ROUND_END, data=""))

    assert _told(console, bob) == ["duel: you 1 - 0 Ann"]
    assert _told(console, ann) == ["duel: you 0 - 1 Bob"]


@pytest.mark.asyncio
async def test_an_unaccepted_challenge_is_not_reported_at_a_round_end(console):
    _plugin(console)
    bob, _ann = _join(console, "Bob"), _join(console, "Ann")
    await _run(console, bob, "!duel Ann")
    console.told.clear()

    await console.bus.publish(Event(EventType.GAME_ROUND_END, data=""))

    assert console.told == []


# -- cancelling and resetting --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_duel_can_be_called_off(console):
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)

    await _run(console, bob, "!duelcancel")

    assert plugin.duels == []
    assert "duel with Ann is off" in _last(console, bob)
    assert "duel with Bob is off" in _last(console, ann)


@pytest.mark.asyncio
async def test_cancelling_with_no_duel_says_so(console):
    _plugin(console)
    bob = _join(console, "Bob")

    await _run(console, bob, "!duelcancel")

    assert "no duel running" in _last(console, bob)


@pytest.mark.asyncio
async def test_with_two_duels_running_a_name_is_required(console):
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)
    sue = _join(console, "Sue")
    await _run(console, bob, "!duel Sue")
    await _run(console, sue, "!duel Bob")
    console.told.clear()

    await _run(console, bob, "!duelcancel")
    assert "2 duels running" in _last(console, bob)

    await _run(console, bob, "!duelcancel Sue")
    assert len(plugin.duels) == 1
    assert plugin.duels[0].involves(ann)


@pytest.mark.asyncio
async def test_cancelling_a_duel_you_do_not_have(console):
    plugin = _plugin(console)
    bob, _ann = await _accepted(console, plugin)
    _join(console, "Sue")

    await _run(console, bob, "!duelcancel Sue")

    assert len(plugin.duels) == 1
    assert "no duel with Sue" in _last(console, bob)


@pytest.mark.asyncio
async def test_a_score_can_be_reset(console):
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)
    await _kill(console, bob, ann)
    console.told.clear()

    await _run(console, bob, "!duelreset")

    assert plugin.duels[0].scores == {}
    assert "duel: you 0 - 0 Ann" in _told(console, bob)


# -- leaving -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leaving_ends_the_duel_and_reports_the_final_score(console):
    plugin = _plugin(console)
    bob, ann = await _accepted(console, plugin)
    await _kill(console, bob, ann)
    console.told.clear()

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=bob))

    assert plugin.duels == []
    assert _told(console, ann) == ["duel: you 0 - 1 Bob", "the duel with Bob is off"]


@pytest.mark.asyncio
async def test_leaving_with_a_challenge_outstanding_just_cancels_it(console):
    plugin = _plugin(console)
    bob, ann = _join(console, "Bob"), _join(console, "Ann")
    await _run(console, bob, "!duel Ann")
    console.told.clear()

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=ann))

    assert plugin.duels == []
    assert _told(console, bob) == ["the duel with Ann is off"]


# -- limits --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_challenge_nobody_answers_expires(console):
    """The classic's proposal had no answer but silence and no end."""
    plugin = _plugin(console, proposal_timeout=300)
    bob, _ann = _join(console, "Bob"), _join(console, "Ann")
    await _run(console, bob, "!duel Ann")

    console.clock.advance(301)
    plugin._expire_proposals()

    assert plugin.duels == []
    assert "expired" in _last(console, bob)


@pytest.mark.asyncio
async def test_an_accepted_duel_never_expires(console):
    plugin = _plugin(console, proposal_timeout=300)
    _bob, _ann = await _accepted(console, plugin)

    console.clock.advance(10_000)
    plugin._expire_proposals()

    assert len(plugin.duels) == 1


@pytest.mark.asyncio
async def test_zero_keeps_a_challenge_forever_as_the_classic_did(console):
    plugin = _plugin(console, proposal_timeout=0)
    bob, _ann = _join(console, "Bob"), _join(console, "Ann")
    await _run(console, bob, "!duel Ann")

    console.clock.advance(10_000)
    plugin._expire_proposals()

    assert len(plugin.duels) == 1


@pytest.mark.asyncio
async def test_there_is_a_limit_on_how_many_duels_one_player_may_have(console):
    """A dozen duels is a dozen private messages after every kill, which is a way to make the bot
    unusable. The classic had no limit."""
    plugin = _plugin(console, max_duels=2)
    bob = _join(console, "Bob")
    for name in ("Ann", "Sue", "Joe"):
        _join(console, name)

    await _run(console, bob, "!duel Ann")
    await _run(console, bob, "!duel Sue")
    await _run(console, bob, "!duel Joe")

    assert len(plugin.duels) == 2
    assert "already have 2 duels" in _last(console, bob)


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_real_players_duel_over_real_log_lines(tmp_path):
    """Both on the same team, which is the case the classic could not score."""
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="duel")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = DuelPlugin(bot, {"settings": {}})
    bot.add_plugin(plugin, "duel")
    bot.start()
    admin.start()
    plugin.start()

    bob, ann = "b" * 32, "a" * 32
    await bot.replay([f"J;{bob};1;Bob", f"J;{ann};2;Ann"])
    await bot.bus.drain()
    for cid in ("1", "2"):
        client = bot.clients.get_by_cid(cid)
        assert client is not None
        client.group_bits = 1  # `!duel` is for registered players, as the classic had it
    await bot.replay(["say;" + bob + ";1;Bob;!duel 2", "say;" + ann + ";2;Ann;!duel 1"])
    await bot.bus.drain()
    assert len(plugin.duels) == 1
    assert plugin.duels[0].accepted is True
    rcon.commands.clear()

    # Same team, so the engine reports a team kill — the event the classic ignored.
    await bot.replay([f"K;{ann};2;allies;Ann;{bob};1;allies;Bob;ak47;100;MOD_RIFLE;chest"])
    await bot.bus.drain()

    assert any("duel: you 1 - 0" in c for c in rcon.commands), rcon.commands
    bot.storage.close()
