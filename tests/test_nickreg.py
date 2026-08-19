"""The `nickreg` plugin — a nickname that belongs to the player who registered it.

Impersonating an admin is the cheapest trick on a game server, so a player reserves the names that are
theirs and anybody else wearing one is warned. Registrations live in the plugin's own table, so the
tests that matter run against a real database.

Two of the classic's faults are pinned here as tests:

* it matched with `SELECT ... WHERE name LIKE '<the player's own name>'`, so `%` and `_` in a name were
  **wildcards** — a player calling themselves `%` matched every registered nickname at once;
* its sweep had an unconditional `continue` after reading a player's last-check time, so anybody checked
  once was never checked again for the rest of their session.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client, PenaltyType
from b3.plugins.admin import AdminPlugin
from b3.plugins.nickreg import NickregPlugin, normalise


def _bot(tmp_path, **settings):  # noqa: ANN001, ANN202
    """A real bot on a real database: this plugin is about what survives a session."""
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="nickreg")],
    )
    clock = FakeClock()
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=clock)
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = NickregPlugin(bot, {"settings": settings} if settings else None)
    bot.add_plugin(plugin, "nickreg")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()
    return bot, rcon, plugin


def _client(bot, name="Admin", cid="1", bits=8):  # noqa: ANN001, ANN202
    """A player with a guid of their own.

    Derived from the *slot* rather than the name, because two players wearing the same name is the
    entire subject of this plugin — a guid taken from the name would make the impostor and the owner
    one database row, and every test would pass for the wrong reason.
    """
    client = Client(guid=f"guid{cid}".ljust(32, "0"), name=name, cid=cid, group_bits=bits)
    bot.storage.save_client(client)
    bot.clients.add(client)
    return client


async def _run(bot, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(bot.command_registry, bot).handle(client, text)


def _said_to(rcon):  # noqa: ANN001, ANN202
    return " | ".join(rcon.commands)


# -- normalising ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("^1Adm^7in", "admin"),
        ("  Admin  ", "admin"),
        ("ADMIN", "admin"),
        ("^3B^3o^3b", "bob"),
    ],
)
def test_a_name_is_compared_as_players_see_it(written, expected):  # noqa: ANN001
    """`^1Adm^7in` and `admin` are the same player to everybody in the server."""
    assert normalise(written) == expected


# -- registering ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registering_reserves_the_name_you_are_using(tmp_path):
    bot, rcon, plugin = _bot(tmp_path)
    admin = _client(bot)

    await _run(bot, admin, "!registernick")

    assert "reserved for you" in _said_to(rcon)
    assert plugin.owner_of("admin") is not None
    assert plugin.owner_of("ADMIN") is not None  # and case does not matter
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_name_somebody_else_holds_cannot_be_registered_again(tmp_path):
    bot, rcon, _plugin = _bot(tmp_path)
    first = _client(bot, name="Admin", cid="1")
    await _run(bot, first, "!registernick")

    impostor = _client(bot, name="^1Admin", cid="2")
    rcon.commands.clear()
    await _run(bot, impostor, "!registernick")

    assert "already registered" in _said_to(rcon)
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_cap_on_how_many_names_one_player_may_hold(tmp_path):
    bot, rcon, _plugin = _bot(tmp_path, max_nicks=2)
    admin = _client(bot)

    for name in ("First", "Second", "Third"):
        admin.name = name
        rcon.commands.clear()
        await _run(bot, admin, "!regnick")

    assert "already have 2 registered" in _said_to(rcon)
    bot.storage.close()


@pytest.mark.asyncio
async def test_registrations_survive_a_restart(tmp_path):
    """The whole point of the plugin is what happens when the owner is *not* there."""
    bot, _rcon, _plugin = _bot(tmp_path)
    admin = _client(bot)
    await _run(bot, admin, "!registernick")
    bot.storage.close()

    bot2, _rcon2, plugin2 = _bot(tmp_path)
    assert plugin2.owner_of("admin") is not None
    bot2.storage.close()


# -- catching somebody wearing it -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_somebody_else_using_a_registered_name_is_warned(tmp_path):
    bot, rcon, plugin = _bot(tmp_path)
    owner = _client(bot, name="Admin", cid="1")
    await _run(bot, owner, "!registernick")

    impostor = _client(bot, name="Admin", cid="2", bits=0)
    rcon.commands.clear()
    plugin.check(impostor)
    await bot.bus.drain()  # the warning announcement is published, not sent inline

    assert bot.storage.get_active_penalties(impostor.require_id(), PenaltyType.WARNING)
    # Asserted on a fragment: Call of Duty's console takes 65 characters, so the announcement wraps
    # and the phrase itself straddles two lines.
    assert "nickname is registered" in _said_to(rcon)
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_owner_is_never_warned_for_their_own_name(tmp_path):
    bot, _rcon, plugin = _bot(tmp_path)
    owner = _client(bot, name="Admin", cid="1")
    await _run(bot, owner, "!registernick")

    assert plugin.check(owner) is False
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_player_named_with_a_wildcard_matches_nothing(tmp_path):
    """The classic's `LIKE` made `%` a wildcard, so a player calling themselves `%` was warned for
    stealing every registered nickname on the server."""
    bot, _rcon, plugin = _bot(tmp_path)
    owner = _client(bot, name="Admin", cid="1")
    await _run(bot, owner, "!registernick")

    for hostile in ("%", "_", "%%", "adm%"):
        player = _client(bot, name=hostile, cid="9", bits=0)
        player.set_var(plugin, "checked_at", 0.0)
        assert plugin.check(player) is False
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_quote_in_a_name_is_a_character_and_not_sql(tmp_path):
    """The classic pasted the name into its SQL with a hand-rolled quote escape."""
    bot, _rcon, plugin = _bot(tmp_path)
    hostile = _client(bot, name="bob'; DROP TABLE nickreg_nicks; --", cid="1")

    await _run(bot, hostile, "!registernick")

    assert plugin.owner_of("bob'; drop table nickreg_nicks; --") is not None
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_superadmin_is_not_impersonating_anybody(tmp_path):
    bot, _rcon, plugin = _bot(tmp_path)
    owner = _client(bot, name="Admin", cid="1")
    await _run(bot, owner, "!registernick")

    su = _client(bot, name="Admin", cid="2", bits=128)
    assert plugin.check(su) is False
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_sweep_checks_again_later(tmp_path):
    """The classic's sweep skipped anybody it had checked once, for the rest of their session — an
    unconditional `continue` in the branch that read the last-check time."""
    bot, _rcon, plugin = _bot(tmp_path, interval=30)
    owner = _client(bot, name="Admin", cid="1")
    await _run(bot, owner, "!registernick")
    impostor = _client(bot, name="Admin", cid="2", bits=0)

    plugin.sweep()
    first = len(bot.storage.get_active_penalties(impostor.require_id(), PenaltyType.WARNING))
    assert first == 1

    plugin.sweep()  # inside the cooldown: nothing new
    assert len(bot.storage.get_active_penalties(impostor.require_id(), PenaltyType.WARNING)) == 1

    bot.clock.advance(31)
    plugin.sweep()
    assert len(bot.storage.get_active_penalties(impostor.require_id(), PenaltyType.WARNING)) == 2
    bot.storage.close()


@pytest.mark.asyncio
async def test_nothing_is_checked_right_after_a_map_change(tmp_path):
    """A player still loading has whatever name the engine gave them, and is not stealing anything."""
    bot, _rcon, plugin = _bot(tmp_path, interval=30)
    owner = _client(bot, name="Admin", cid="1")
    await _run(bot, owner, "!registernick")
    impostor = _client(bot, name="Admin", cid="2", bits=0)

    await bot.bus.publish(Event(EventType.GAME_MAP_CHANGE, data="mp_vacant"))
    plugin.sweep()

    assert bot.storage.get_active_penalties(impostor.require_id(), PenaltyType.WARNING) == []
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_name_change_is_checked_at_once(tmp_path):
    bot, _rcon, plugin = _bot(tmp_path)
    owner = _client(bot, name="Admin", cid="1")
    await _run(bot, owner, "!registernick")
    impostor = _client(bot, name="Nobody", cid="2", bits=0)

    impostor.name = "Admin"
    await bot.bus.publish(Event(EventType.CLIENT_NAME_CHANGE, client=impostor, data="Admin"))

    assert bot.storage.get_active_penalties(impostor.require_id(), PenaltyType.WARNING)
    del plugin
    bot.storage.close()


# -- listing and deleting -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_listing_your_own_names(tmp_path):
    bot, rcon, _plugin = _bot(tmp_path)
    admin = _client(bot)
    await _run(bot, admin, "!registernick")

    rcon.commands.clear()
    await _run(bot, admin, "!listnick")

    assert "has registered: Admin" in _said_to(rcon)
    bot.storage.close()


@pytest.mark.asyncio
async def test_listing_when_there_is_nothing(tmp_path):
    bot, rcon, _plugin = _bot(tmp_path)
    admin = _client(bot)

    await _run(bot, admin, "!listnick")

    assert "no registered nicknames" in _said_to(rcon)
    bot.storage.close()


@pytest.mark.asyncio
async def test_deleting_your_own_registration(tmp_path):
    bot, rcon, plugin = _bot(tmp_path)
    admin = _client(bot)
    await _run(bot, admin, "!registernick")

    rcon.commands.clear()
    await _run(bot, admin, "!delnick Admin")

    assert "deleted the registration" in _said_to(rcon)
    assert plugin.owner_of("admin") is None
    bot.storage.close()


@pytest.mark.asyncio
async def test_deleting_a_name_nobody_registered(tmp_path):
    bot, rcon, _plugin = _bot(tmp_path)
    admin = _client(bot)

    await _run(bot, admin, "!deletenick Ghost")

    assert "no registered nickname called Ghost" in _said_to(rcon)
    bot.storage.close()


@pytest.mark.asyncio
async def test_somebody_elses_registration_needs_the_manage_level(tmp_path):
    bot, rcon, plugin = _bot(tmp_path, manage_level=100)
    owner = _client(bot, name="Admin", cid="1", bits=8)
    await _run(bot, owner, "!registernick")
    other = _client(bot, name="Mod", cid="2", bits=8)  # also a mod: not senior enough

    rcon.commands.clear()
    await _run(bot, other, "!delnick Admin")

    assert "cannot manage" in _said_to(rcon)
    assert plugin.owner_of("admin") is not None

    su = _client(bot, name="Su", cid="3", bits=128)
    rcon.commands.clear()
    await _run(bot, su, "!delnick Admin")
    assert plugin.owner_of("admin") is None
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_senior_cannot_free_a_superadmins_name(tmp_path):
    """Even with the manage level: the second condition is that the owner is not above you."""
    bot, rcon, plugin = _bot(tmp_path, manage_level=80)
    su = _client(bot, name="Su", cid="1", bits=128)
    await _run(bot, su, "!registernick")
    senior = _client(bot, name="Senior", cid="2", bits=64)

    rcon.commands.clear()
    await _run(bot, senior, "!listnick su")

    assert "cannot manage" in _said_to(rcon)
    assert plugin.owner_of("su") is not None
    bot.storage.close()
