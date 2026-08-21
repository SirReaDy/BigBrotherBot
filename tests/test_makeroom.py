"""The `makeroom` plugin — taking somebody's slot away so a member can have it.

Every test here is really the same question: *who* gets kicked. The classic plugin's eight test files
answer it with clients whose `timeAdd` the test sets by hand, which is how the fault at the centre of
this port stayed invisible — `timeAdd` is when a player's database row was created, so on a server of
regulars the "last connected player" was whoever had registered most recently. The last two tests
here run real Call of Duty log lines through a real bot precisely so nothing sets that by hand.

Pinned as tests, because each is a case the classic got wrong: a bot is never the candidate (its
level is 0, so it was always the candidate), a masked admin is judged by their real level, automation
firing with no admin to reply to does not raise, and a kick waiting out its delay is re-checked
against the slot it was aimed at.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.makeroom import MakeroomPlugin, Pending


def _makeroom(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    plugin = MakeroomPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


def _join(console, name, bits=0, cid=None, is_bot=False, mask=0):  # noqa: ANN001, ANN202
    """A player arriving now — the arrival time comes from the clock, as it does in the runtime."""
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"),
        name=name,
        cid=slot,
        id=int(slot),
        group_bits=bits,
        mask_level=mask,
        is_bot=is_bot,
    )
    console.clients.add(client)
    console.register_client(name, client)
    return client


async def _auth(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


def _kicked(console):  # noqa: ANN001, ANN202
    return [client.name for client, _reason, _admin in console.kicked]


# -- who gets kicked -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_newest_arrival_from_the_lowest_group_goes(console):
    _makeroom(console, delay=0, retain_free_duration=0, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")
    console.clock.advance(60)
    _join(console, "Jack")

    await _run(console, admin, "!makeroom")

    assert _kicked(console) == ["Jack"]
    assert any("Jack was kicked" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_a_lower_group_goes_before_a_newer_arrival(console):
    """Group first, arrival second — the classic's order, and the right one.

    A `reg` who joined a minute ago keeps their slot over a guest who has been playing for an hour:
    the plugin exists to protect the people who support the server, not the punctual.
    """
    _makeroom(console, delay=0, retain_free_duration=0, only_when_full=False, non_member_level=2)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Guest")
    console.clock.advance(3600)
    _join(console, "Reg", bits=2)

    await _run(console, admin, "!makeroom")

    assert _kicked(console) == ["Guest"]


@pytest.mark.asyncio
async def test_a_member_is_never_the_candidate(console):
    _makeroom(console, delay=0, only_when_full=False, non_member_level=2)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Mod", bits=8)

    await _run(console, admin, "!makeroom")

    assert console.kicked == []
    assert any("nobody here" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_a_bot_is_never_the_candidate(console):
    """The classic's answer on any server running AI, every single time.

    A bot's level is 0 — the lowest group there is — and it sorts to the front of the candidate list.
    Kicking it frees nothing: the engine puts it straight back, and the admin still cannot join.
    """
    _makeroom(console, delay=0, retain_free_duration=0, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")
    console.clock.advance(60)
    _join(console, "[BOT]Rambo", is_bot=True)

    await _run(console, admin, "!makeroom")

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_a_masked_admin_keeps_their_slot(console):
    """A mask hides rank; it never removes it.

    The classic compared the *mask* level whenever one was set, so a superadmin playing incognito
    was a non-member — kicked to make room by somebody with a fraction of their rank.
    """
    _makeroom(console, delay=0, only_when_full=False, non_member_level=2)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Incognito", bits=128, mask=2)  # a superadmin wearing `reg`

    await _run(console, admin, "!makeroom")

    assert console.kicked == []


@pytest.mark.asyncio
async def test_the_admin_who_asked_is_never_the_answer(console):
    """Possible as soon as `non_member_level` is set above the level `!makeroom` needs."""
    _makeroom(console, delay=0, only_when_full=False, non_member_level=40)
    mod = _join(console, "Mod", bits=8)

    await _run(console, mod, "!makeroom")

    assert console.kicked == []
    assert any("nobody here" in text for text in _told(console, mod))


# -- when it refuses -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nothing_happens_on_a_server_that_is_not_full(console):
    console.game.max_players = 8
    _makeroom(console, delay=0)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")

    await _run(console, admin, "!makeroom")

    assert console.kicked == []
    assert any("2 of 8 slots" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_the_classic_behaviour_is_available(console):
    """`only_when_full: no` — an operator who wants the slot freed regardless still can."""
    console.game.max_players = 8
    _makeroom(console, delay=0, retain_free_duration=0, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")

    await _run(console, admin, "!makeroom")

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_nothing_is_announced_when_there_is_nobody_to_remove(console):
    """The classic told the whole server it was making room, waited, then found nobody to kick."""
    _makeroom(console, delay=5, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Mod", bits=8)

    await _run(console, admin, "!makeroom")

    assert console.said == []
    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_second_request_inside_the_delay_is_refused(console):
    _makeroom(console, delay=5, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")
    console.clock.advance(1)
    _join(console, "Jack")

    await _run(console, admin, "!makeroom")
    await _run(console, admin, "!makeroom")

    assert any("already running" in text for text in _told(console, admin))
    assert console.kicked == []  # neither has happened yet


# -- the delay -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_announcement_comes_first_and_the_kick_when_it_is_due(console):
    plugin = _makeroom(console, delay=5, retain_free_duration=0, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")

    await _run(console, admin, "!makeroom")
    assert any("making room" in text for text in console.said)
    assert console.kicked == []

    console.clock.advance(5)
    plugin._check_deadlines()

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_the_candidate_is_chosen_when_the_kick_is_due_not_when_it_is_asked_for(console):
    """A player who leaves during the delay is not still the answer."""
    plugin = _makeroom(console, delay=5, retain_free_duration=0, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")
    console.clock.advance(1)
    jack = _join(console, "Jack")

    await _run(console, admin, "!makeroom")
    console.clients.remove(jack.cid or "")
    console.clock.advance(5)
    plugin._check_deadlines()

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_a_pending_automation_kick_is_re_checked_against_the_slot(console):
    """The classic's timer fired at a client object whatever had happened to the slot since.

    Here the player who was announced has left and somebody else has taken slot 2, so the kick that
    comes due belongs to nobody: it is dropped rather than landing on the new arrival.
    """
    console.game.max_players = 2
    plugin = _makeroom(console, delay=5, automation=True, min_free_slots=1)
    _join(console, "Admin", bits=128)
    joe = _join(console, "Joe", cid="2")

    await _auth(console, joe)
    assert console.kicked == []

    console.clients.remove("2")
    _join(console, "Newcomer", cid="2")
    console.clock.advance(5)
    plugin._check_deadlines()

    assert console.kicked == []


# -- holding the slot ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_non_member_arriving_in_the_window_does_not_get_the_slot(console):
    plugin = _makeroom(console, delay=0, retain_free_duration=10, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    joe = _join(console, "Joe")

    await _run(console, admin, "!makeroom")
    assert _kicked(console) == ["Joe"]
    assert any("10s to take it" in text for text in _told(console, admin))

    console.clients.remove(joe.cid or "")  # the server confirms he has gone
    console.clock.advance(1)
    jack = _join(console, "Jack", cid="9")
    await _auth(console, jack)

    assert _kicked(console) == ["Joe", "Jack"]
    assert plugin._retain is not None


@pytest.mark.asyncio
async def test_the_window_ends_when_a_member_takes_the_slot(console):
    plugin = _makeroom(console, delay=0, retain_free_duration=10, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    joe = _join(console, "Joe")

    await _run(console, admin, "!makeroom")
    console.clients.remove(joe.cid or "")
    console.clock.advance(1)
    await _auth(console, _join(console, "Mod", bits=8, cid="8"))
    assert plugin._retain is None

    await _auth(console, _join(console, "Jack", cid="9"))
    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_the_window_expires(console):
    _makeroom(console, delay=0, retain_free_duration=10, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    joe = _join(console, "Joe")

    await _run(console, admin, "!makeroom")
    console.clients.remove(joe.cid or "")
    console.clock.advance(11)
    await _auth(console, _join(console, "Jack", cid="9"))

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_the_window_survives_a_kick_the_server_has_not_confirmed_yet(console):
    """The kicked player is still on the roster, because a kick is a request, not an event.

    The classic decided the window's threshold from a count taken *after* asking for the kick, so
    whether it held anything depended on whether the disconnect line had arrived yet. Here the
    threshold is read before, and the outcome is the same either way.
    """
    _makeroom(console, delay=0, retain_free_duration=10, only_when_full=False)
    admin = _join(console, "Admin", bits=128)

    _join(console, "Joe")
    await _run(console, admin, "!makeroom")  # Joe stays on the roster for now

    console.clock.advance(1)
    await _auth(console, _join(console, "Jack", cid="9"))

    assert _kicked(console) == ["Joe", "Jack"]


@pytest.mark.asyncio
async def test_a_request_while_a_slot_is_held_says_how_long_is_left(console):
    _makeroom(console, delay=0, retain_free_duration=10, only_when_full=False)
    admin = _join(console, "Admin", bits=128)
    _join(console, "Joe")
    console.clock.advance(1)
    _join(console, "Jack")

    await _run(console, admin, "!makeroom")
    console.clock.advance(6)
    await _run(console, admin, "!makeroom")

    assert any("4s left" in text for text in _told(console, admin))
    assert _kicked(console) == ["Jack"]


# -- automation ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_automation_kicks_the_non_member_who_filled_the_last_slot(console):
    console.game.max_players = 3
    _makeroom(console, delay=0, automation=True, min_free_slots=1)
    _join(console, "Admin", bits=128)
    _join(console, "Mod", bits=8)
    joe = _join(console, "Joe")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_automation_leaves_a_server_with_room_alone(console):
    console.game.max_players = 8
    _makeroom(console, delay=0, automation=True, min_free_slots=1)
    _join(console, "Admin", bits=128)
    joe = _join(console, "Joe")

    await _auth(console, joe)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_automation_frees_a_slot_when_a_member_fills_the_last_one(console):
    """The path that raised `AttributeError` in the classic, every time it ran.

    `cmd_makeroom()` was called with no client, and reporting the kick reached `admin.message` on
    None. The kick still went out, so what an operator saw was a plugin that worked and a traceback.
    """
    console.game.max_players = 3
    _makeroom(console, delay=0, retain_free_duration=0, automation=True, min_free_slots=1)
    _join(console, "Admin", bits=128)
    _join(console, "Joe")
    console.clock.advance(60)
    mod = _join(console, "Mod", bits=8)

    await _auth(console, mod)

    assert _kicked(console) == ["Joe"]
    # Nothing was addressed to nobody. This is the assertion that pins the fault: the classic
    # reported the kick to `admin` without ever asking whether there was one.
    assert all(who is not None for who, _text in console.told)


@pytest.mark.asyncio
async def test_two_arrivals_inside_the_delay_are_both_seen_off(console):
    """The classic held one timer, so the second announcement quietly replaced the first."""
    console.game.max_players = 3
    plugin = _makeroom(console, delay=5, automation=True, min_free_slots=1)
    _join(console, "Admin", bits=128)
    joe = _join(console, "Joe")
    jack = _join(console, "Jack")

    await _auth(console, joe)
    await _auth(console, jack)
    console.clock.advance(5)
    plugin._check_deadlines()

    assert sorted(_kicked(console)) == ["Jack", "Joe"]


@pytest.mark.asyncio
async def test_automation_with_no_slot_count_kicks_nobody(console):
    """`sv_maxclients` unread and nothing configured: the question has no answer, so nobody goes."""
    _makeroom(console, delay=0, automation=True, min_free_slots=1)
    _join(console, "Admin", bits=128)
    joe = _join(console, "Joe")

    await _auth(console, joe)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_configured_slots_beat_the_server(console):
    """An operator states a smaller number when slots are reserved outside the bot."""
    console.game.max_players = 32
    plugin = _makeroom(console, delay=0, automation=True, total_slots=3, min_free_slots=1)
    _join(console, "Admin", bits=128)
    _join(console, "Mod", bits=8)
    joe = _join(console, "Joe")

    assert plugin.total_slots() == 3
    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_the_command_switches_automation_on_and_off(console):
    console.game.max_players = 8
    plugin = _makeroom(console, delay=0)
    admin = _join(console, "Admin", bits=128)

    await _run(console, admin, "!makeroomauto on")
    assert plugin.settings["automation"] is True
    await _run(console, admin, "!makeroomauto off")
    assert plugin.settings["automation"] is False
    await _run(console, admin, "!makeroomauto f00")
    assert any("expecting on or off" in text for text in _told(console, admin))


@pytest.mark.asyncio
async def test_automation_cannot_be_switched_on_without_a_slot_count(console):
    """The classic deleted the command from the admin plugin's table instead of answering."""
    plugin = _makeroom(console, delay=0)
    admin = _join(console, "Admin", bits=128)

    await _run(console, admin, "!makeroomauto on")

    assert plugin.settings["automation"] is False
    assert any("how many slots" in text for text in _told(console, admin))


# -- config --------------------------------------------------------------------------------------


def test_the_retention_window_is_clamped(console):
    assert _makeroom(console, retain_free_duration=40).settings["retain_free_duration"] == 30
    assert _makeroom(console, retain_free_duration=-5).settings["retain_free_duration"] == 0
    assert _makeroom(console, retain_free_duration="f00").settings["retain_free_duration"] == 15


def test_a_slot_count_below_two_is_ignored(console):
    plugin = _makeroom(console, total_slots=1)

    assert plugin.settings["total_slots"] == 0


def test_automation_that_would_kick_everybody_stays_off(console):
    """`min_free_slots` not less than `total_slots` means every arrival is one too many."""
    plugin = _makeroom(console, automation=True, total_slots=6, min_free_slots=7)

    assert plugin.settings["automation"] is False


def test_a_disabled_plugin_forgets_what_it_was_about_to_do(console):
    """A kick announced before the plugin was switched off must not arrive after it."""
    plugin = _makeroom(console, delay=5, only_when_full=False)
    _join(console, "Joe")
    plugin._pending.append(Pending(due=console.clock.now() + 5))

    plugin.disable()

    assert plugin._pending == []
    assert plugin._retain is None


# -- through a real bot --------------------------------------------------------------------------


def _bot(tmp_path, **settings):  # noqa: ANN001, ANN202
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="makeroom")],
    )
    clock = FakeClock()
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=clock)
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = MakeroomPlugin(bot, {"settings": settings})
    bot.add_plugin(plugin, "makeroom")
    bot.start()
    admin.start()
    plugin.start()
    return bot, rcon, clock, plugin


@pytest.mark.asyncio
async def test_the_player_who_just_walked_in_is_the_one_who_goes(tmp_path):
    """Real log lines, a real database, and the fault this port exists to fix.

    Two guests. `Regular` has a database row from long ago and joins **last**; `Newcomer` has a row
    created moments ago and joins **first**. The classic sorted by `timeAdd` — the row — and so
    kicked `Newcomer`, who had been playing, while the player who had just arrived kept the slot. The
    arrival is what decides it here.
    """
    bot, rcon, clock, _plugin = _bot(
        tmp_path, delay=0, retain_free_duration=0, only_when_full=False
    )
    regular = Client(guid="r" * 32, name="Regular", time_add=1_000_000)
    bot.storage.save_client(regular)

    await bot.replay(["J;" + "a" * 32 + ";1;Admin", "J;" + "n" * 32 + ";2;Newcomer"])
    await bot.bus.drain()
    clock.advance(600)
    await bot.replay(["J;" + "r" * 32 + ";3;Regular"])
    await bot.bus.drain()

    admin = bot.clients.get_by_cid("1")
    assert admin is not None
    admin.group_bits = 128
    rcon.commands.clear()

    await bot.run_command(admin, "!makeroom")
    await bot.bus.drain()

    kick = " | ".join(rcon.commands)
    assert "Regular" in kick or "3" in kick
    assert "Newcomer" not in kick
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_full_server_holds_the_freed_slot_against_the_next_arrival(tmp_path):
    """End to end: the kick goes out over rcon, and so does the one for the player who takes it."""
    bot, rcon, clock, _plugin = _bot(
        tmp_path, delay=0, retain_free_duration=10, only_when_full=False
    )

    await bot.replay(["J;" + "a" * 32 + ";1;Admin", "J;" + "j" * 32 + ";2;Joe"])
    await bot.bus.drain()
    admin = bot.clients.get_by_cid("1")
    assert admin is not None
    admin.group_bits = 128
    rcon.commands.clear()

    await bot.run_command(admin, "!makeroom")
    await bot.bus.drain()
    assert any("kick" in c.lower() for c in rcon.commands)

    await bot.replay(["Q;" + "j" * 32 + ";2;Joe"])  # the server confirms Joe has gone
    await bot.bus.drain()
    clock.advance(1)
    rcon.commands.clear()

    await bot.replay(["J;" + "k" * 32 + ";4;Jack"])
    await bot.bus.drain()

    assert any("kick" in c.lower() for c in rcon.commands)
    bot.storage.close()


@pytest.mark.asyncio
async def test_automation_says_nothing_about_a_bot_taking_the_last_slot(console):
    """A slot an AI player is in is not one this plugin can free.

    A bot authenticates like anybody else — its guid is dropped, not its arrival — and kicking one
    frees nothing: the engine cycles it straight back, so acting would announce a kick every time.
    """
    console.game.max_players = 2
    _makeroom(console, delay=0, automation=True, min_free_slots=1)
    _join(console, "Admin", bits=128)
    rambo = _join(console, "[BOT]Rambo", is_bot=True)

    await _auth(console, rambo)

    assert console.kicked == []
    assert console.said == []
