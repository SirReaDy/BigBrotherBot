"""The `cmdmanager` plugin — command levels, aliases and per-player grants, at runtime.

The grant is the part nothing else can do: one player, one command, without promoting them into a
group that carries everything else with it. The classic delivered it by rewriting `Command.canUse` on
the class and on every instance already registered, which is why it could not be undone and why
installing the plugin changed the permission rule for every other plugin at once. Here the registry
has a hook, and these tests check both directions of it — a grant widens access, and clearing the
plugin narrows it back.

Persistence is checked against a real database rather than a fake, because "it worked until the bot
restarted" is the whole point of storing anything.
"""

from __future__ import annotations

import pytest

from b3.core.commands import Command, CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.cmdmanager import CmdmanagerPlugin, describe_level, parse_level


def _cmdmanager(console):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = CmdmanagerPlugin(console, None)
    plugin.start()
    return plugin


def _client(console, name="Su", cid="1", id_=1, bits=128):  # noqa: ANN001, ANN202
    client = Client(guid=name[0].upper() * 4, name=name, cid=cid, id=id_, group_bits=bits)
    console.clients.add(client)
    console.register_client(name.lower(), client)
    return client


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client][-1]


# -- reading levels ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mod", (20, 100)),
        ("20", (20, 100)),
        ("guest", (0, 100)),
        ("mod-admin", (20, 40)),
        ("2-20", (2, 20)),
        # A word that is not a group and not a number comes back as itself, so the refusal can name
        # the half the admin got wrong rather than saying "invalid level".
        ("moderator", "moderator"),
        ("mod-moderator", "moderator"),
        ("101", "101"),
    ],
)
def test_a_level_is_a_keyword_a_number_or_a_range(text, expected):  # noqa: ANN001
    assert parse_level(text) == expected


def test_a_level_reads_back_as_the_keyword_an_operator_typed():
    cmd = Command(name="x", handler=lambda ctx: None, min_level=20, max_level=100)
    assert describe_level(cmd) == "mod"
    cmd.max_level = 40
    assert describe_level(cmd) == "mod-admin"


# -- setting levels ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cmdlevel_reports_and_then_changes_a_level(console):
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdlevel kick")
    assert "kick is" in _last(console, su)

    await _run(console, su, "!cmdlevel kick senioradmin")

    assert console.command_registry.get("kick").min_level == 80
    # Searched across the replies rather than taking the last: with a fake storage the plugin also
    # says that nothing will be kept, which is its own test below.
    assert any("kick is now senioradmin" in text for _who, text in console.told)


@pytest.mark.asyncio
async def test_a_changed_level_is_enforced_immediately(console):
    _cmdmanager(console)
    su = _client(console)
    mod = _client(console, name="Mod", cid="2", id_=2, bits=8)  # level 20

    await _run(console, su, "!cmdlevel kick mod")
    await _run(console, mod, "!kick nobody")

    assert "sufficient access" not in _last(console, mod)


@pytest.mark.asyncio
async def test_an_impossible_range_is_refused(console):
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdlevel kick admin-mod")

    assert "above" in _last(console, su)
    assert console.command_registry.get("kick").min_level == 40  # unchanged


@pytest.mark.asyncio
async def test_a_typo_in_a_level_names_the_word_that_was_wrong(console):
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdlevel kick moderator")

    assert "moderator" in _last(console, su)


@pytest.mark.asyncio
async def test_an_admin_cannot_change_a_command_they_cannot_use(console):
    """The classic checked this on `!cmdgrant` and `!cmduse` but not on `!cmdlevel`, so anybody with
    `!cmdlevel` could lower a superadmin-only command — which is a way to hand yourself `!die`."""
    _cmdmanager(console)
    senior = _client(console, name="Senior", bits=64)  # level 80
    console.command_registry.get("cmdlevel").min_level = 80  # so Senior can run it at all

    await _run(console, senior, "!cmdlevel die guest")

    assert "cannot change" in _last(console, senior)
    assert console.command_registry.get("die").min_level == 100


@pytest.mark.asyncio
async def test_an_unknown_command_is_named(console):
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdlevel nonsense mod")

    assert "no nonsense command" in _last(console, su)


# -- aliases -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_alias_can_be_added_and_then_works(console):
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdalias maprotate rot")

    assert console.command_registry.get("rot") is console.command_registry.get("maprotate")
    assert any("can now also be typed rot" in text for _who, text in console.told)


@pytest.mark.asyncio
async def test_replacing_an_alias_retires_the_old_one(console):
    """Re-indexed rather than only re-labelled: a command answering to two words is a command whose
    config file is wrong about one of them."""
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdalias kick boot")
    assert console.command_registry.get("k") is None
    assert console.command_registry.get("boot") is not None


@pytest.mark.asyncio
async def test_an_alias_belonging_to_another_command_is_refused(console):
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdalias maprotate b")  # `b` is already !ban

    assert "already ban" in _last(console, su)
    assert console.command_registry.get("b").name == "ban"


@pytest.mark.asyncio
async def test_asking_about_an_alias_that_does_not_exist(console):
    _cmdmanager(console)
    su = _client(console)

    await _run(console, su, "!cmdalias maprotate")

    assert "no alias" in _last(console, su)


# -- grants --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_grant_lets_one_player_use_one_command(console):
    plugin = _cmdmanager(console)
    su = _client(console)
    bob = _client(console, name="Bob", cid="2", id_=2, bits=0)  # guest

    await _run(console, su, "!cmdgrant bob kick")

    assert plugin._may_use(bob, "kick") is True
    await _run(console, bob, "!kick nobody")
    assert "sufficient access" not in _last(console, bob)


@pytest.mark.asyncio
async def test_a_grant_does_not_widen_anything_else(console):
    _cmdmanager(console)
    su = _client(console)
    bob = _client(console, name="Bob", cid="2", id_=2, bits=0)

    await _run(console, su, "!cmdgrant bob kick")
    await _run(console, bob, "!ban nobody")

    assert "sufficient access" in _last(console, bob)


@pytest.mark.asyncio
async def test_a_grant_shows_up_in_what_a_player_can_use(console):
    """`!help` and the prompt have to agree: a grant visible at one and not the other is worse than
    no grants at all."""
    _cmdmanager(console)
    su = _client(console)
    bob = _client(console, name="Bob", cid="2", id_=2, bits=0)

    await _run(console, su, "!cmdgrant bob kick")

    assert "kick" in [c.name for c in console.command_registry.usable_by(bob)]


@pytest.mark.asyncio
async def test_granting_something_a_player_already_has_is_refused(console):
    """It reads as "done" and then does not survive them being demoted — which is exactly the moment
    it was supposed to matter."""
    _cmdmanager(console)
    su = _client(console)
    _client(console, name="Other", cid="2", id_=2, bits=128)

    await _run(console, su, "!cmdgrant other kick")

    assert "already use" in _last(console, su)


@pytest.mark.asyncio
async def test_a_grant_can_be_revoked(console):
    plugin = _cmdmanager(console)
    su = _client(console)
    bob = _client(console, name="Bob", cid="2", id_=2, bits=0)

    await _run(console, su, "!cmdgrant bob kick")
    await _run(console, su, "!cmdrevoke bob kick")

    assert plugin._may_use(bob, "kick") is False
    assert "no longer use" in _last(console, su)


@pytest.mark.asyncio
async def test_revoking_a_grant_nobody_had_says_so(console):
    _cmdmanager(console)
    su = _client(console)
    _client(console, name="Bob", cid="2", id_=2, bits=0)

    await _run(console, su, "!cmdrevoke bob kick")

    assert "had no grant" in _last(console, su)


@pytest.mark.asyncio
async def test_cmduse_answers_for_somebody_else(console):
    _cmdmanager(console)
    su = _client(console)
    _client(console, name="Bob", cid="2", id_=2, bits=0)

    await _run(console, su, "!cmduse bob kick")
    assert "cannot use" in _last(console, su)

    await _run(console, su, "!cmdgrant bob kick")
    await _run(console, su, "!cmduse bob kick")
    assert "can use kick" in _last(console, su)


@pytest.mark.asyncio
async def test_disabling_the_plugin_stops_granting_anything(console):
    """The classic could not do this at all: it had rewritten `Command.canUse` on the class."""
    plugin = _cmdmanager(console)
    su = _client(console)
    bob = _client(console, name="Bob", cid="2", id_=2, bits=0)

    await _run(console, su, "!cmdgrant bob kick")
    plugin.disable()
    await _run(console, bob, "!kick nobody")

    assert "sufficient access" in _last(console, bob)


@pytest.mark.asyncio
async def test_a_grant_is_never_matched_against_an_alias(console):
    """A grant is a decision about a command; its aliases can be changed under it by `!cmdalias`."""
    plugin = _cmdmanager(console)
    su = _client(console)
    bob = _client(console, name="Bob", cid="2", id_=2, bits=0)

    await _run(console, su, "!cmdgrant bob kick")

    assert plugin._may_use(bob, "k") is False


# -- with no storage to keep anything ------------------------------------------------------------


def test_a_storage_that_keeps_nothing_is_said_out_loud(console, caplog):
    """ "I set that level yesterday" and "the bot restarted" are otherwise the same sentence."""
    with caplog.at_level("WARNING"):
        plugin = _cmdmanager(console)

    assert plugin._engine is None
    assert "will not survive a restart" in caplog.text


# -- against a real database ---------------------------------------------------------------------


def _real_bot(tmp_path):  # noqa: ANN001, ANN202
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="cmdmanager")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = CmdmanagerPlugin(bot, None)
    bot.add_plugin(plugin, "cmdmanager")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()
    return bot, rcon, plugin


@pytest.mark.asyncio
async def test_a_level_and_an_alias_survive_a_restart(tmp_path):
    """The whole reason there is a table. The classic wrote this into the *owning plugin's* config
    file, which meant editing a file it did not own and losing that file's comments on the way."""
    bot, _rcon, _plugin = _real_bot(tmp_path)
    su = Client(guid="s" * 32, name="Su", cid="1", id=1, group_bits=128)
    bot.clients.add(su)

    processor = CommandProcessor(bot.command_registry, bot)
    await processor.handle(su, "!cmdlevel kick senioradmin")
    await processor.handle(su, "!cmdalias kick boot")
    bot.storage.close()

    # A second bot on the same database: a restart, as far as anything here can tell.
    bot2, _rcon2, _plugin2 = _real_bot(tmp_path)
    assert bot2.command_registry.get("kick").min_level == 80
    assert bot2.command_registry.get("boot") is not None
    bot2.storage.close()


@pytest.mark.asyncio
async def test_a_grant_survives_a_restart_and_arrives_on_authentication(tmp_path):
    bot, _rcon, _plugin = _real_bot(tmp_path)
    su = Client(guid="s" * 32, name="Su", cid="1", id=1, group_bits=128)
    bot.clients.add(su)
    bob_guid = "b" * 32
    await bot.replay([f"J;{bob_guid};2;Bob"])
    await bot.bus.drain()

    await CommandProcessor(bot.command_registry, bot).handle(su, "!cmdgrant bob kick")
    bot.storage.close()

    bot2, rcon2, plugin2 = _real_bot(tmp_path)
    await bot2.replay([f"J;{bob_guid};2;Bob"])
    await bot2.bus.drain()
    bob = bot2.clients.get_by_cid("2")

    assert plugin2._may_use(bob, "kick") is True
    rcon2.commands.clear()
    await CommandProcessor(bot2.command_registry, bot2).handle(bob, "!kick nobody")
    assert not any("sufficient access" in c for c in rcon2.commands)
    bot2.storage.close()


@pytest.mark.asyncio
async def test_an_override_is_reapplied_when_a_plugin_is_enabled_again(tmp_path):
    """Enabling a plugin re-registers its commands at the levels in their code, so without this a
    level somebody set an hour ago quietly reverts."""
    bot, _rcon, _plugin = _real_bot(tmp_path)
    su = Client(guid="s" * 32, name="Su", cid="1", id=1, group_bits=128)
    bot.clients.add(su)

    await CommandProcessor(bot.command_registry, bot).handle(su, "!cmdlevel kick senioradmin")
    # What `!plugin enable admin` does: the commands come back at their defaults.
    bot.command_registry.get("kick").min_level = 40
    await bot.bus.publish(Event(EventType.PLUGIN_ENABLED, data="admin"))

    assert bot.command_registry.get("kick").min_level == 80
    bot.storage.close()
