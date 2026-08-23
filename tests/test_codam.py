"""The `codam` plugin — a Call of Duty admin mod's verbs, offered as bot commands.

The classic plugin has **no captured tests and no config file anywhere in its tree**, so its source is
the only description of what it did. Reading it is what turned up the fault these tests start with:
the verbs that named no player were sent to the mod with the bot's own `c` prefix still attached, so
`!crestart` asked the mod for `crestart`. The half of the plugin that took a player stripped it, which
is what shows the intent.

Everything else here is about the same theme — a passthrough that says nothing when it fails is worse
than no passthrough at all — plus the two guards the classic did not have: an escaped rcon line, and
the ordinary rule about acting on somebody at or above your own level.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.codam import CodamPlugin


def _codam(console, config=None):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = CodamPlugin(console, config)
    plugin.start()
    return plugin


def _client(console, name="Bob", cid="2", id_=2, bits=0, mask=0):  # noqa: ANN001, ANN202
    client = Client(
        guid=name[0].upper() * 4, name=name, cid=cid, id=id_, group_bits=bits, mask_level=mask
    )
    console.clients.add(client)
    console.register_client(name.lower(), client)
    return client


def _admin(console, bits=128):  # noqa: ANN001, ANN202
    return _client(console, name="Boss", cid="1", id_=1, bits=bits)


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    told = [text for who, text in console.told if who is client]
    return told[-1] if told else ""


# -- the verb the mod is actually sent ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_bots_prefix_is_not_part_of_what_the_mod_is_sent(console):
    """The classic sent `cmd.command` — the registered name — so the mod got `crestart`, an unknown
    command, and its complaint was thrown away. Every verb in that half of the config was dead."""
    _codam(console, {"commands": {"admin": ["restart"]}})
    boss = _admin(console)

    await _run(console, boss, "!crestart")

    assert console.rcon_sent == ['command "restart"']


@pytest.mark.asyncio
async def test_a_verb_that_names_a_player_is_sent_the_slot_first(console):
    """The classic built `<verb> <text> <slot>`, so `!ckick 3 spamming` reached the mod as
    `kick spamming 3` and it read the reason as the player."""
    _codam(console, {"player_commands": {"admin": ["kick"]}})
    boss = _admin(console)
    _client(console, name="Bob", cid="7", id_=7)

    await _run(console, boss, "!ckick bob spamming the radio")

    assert console.rcon_sent == ['command "kick 7 spamming the radio"']


@pytest.mark.asyncio
async def test_a_verb_with_nothing_after_it_sends_just_the_verb(console):
    _codam(console, {"commands": {"admin": ["fastrestart"]}})
    boss = _admin(console)

    await _run(console, boss, "!cfastrestart")

    assert console.rcon_sent == ['command "fastrestart"']


@pytest.mark.asyncio
async def test_the_passthrough_sends_the_line_as_it_was_typed(console):
    _codam(console)
    boss = _admin(console)

    await _run(console, boss, "!codam status all")

    assert console.rcon_sent == ['command "status all"']


@pytest.mark.asyncio
async def test_the_passthrough_with_nothing_after_it_says_so(console):
    _codam(console)
    boss = _admin(console)

    await _run(console, boss, "!codam")

    assert "name the admin-mod command" in _last(console, boss)
    assert console.rcon_sent == []


# -- what the admin is told ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mods_own_answer_comes_back(console):
    """The classic discarded it, so `!ckick 3` looked the same whether the mod ran the command, did
    not recognise it, or was not installed at all."""
    _codam(console, {"commands": {"admin": ["reloadscripts"]}})
    console.rcon_replies['command "reloadscripts"'] = "unknown command"
    boss = _admin(console)

    await _run(console, boss, "!creloadscripts")

    assert _last(console, boss) == "unknown command"


@pytest.mark.asyncio
async def test_a_mod_that_says_nothing_gets_a_confirmation_instead(console):
    _codam(console, {"commands": {"admin": ["restart"]}})
    boss = _admin(console)

    await _run(console, boss, "!crestart")

    assert "sent restart" in _last(console, boss)


# -- the rcon line is a quoted argument ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_quote_cannot_end_the_argument_and_start_a_second_command(console):
    """`command "%s"` with the text pasted in raw: a `"` closes the argument and everything after
    the `;` is a command of its own."""
    _codam(console, {"player_commands": {"admin": ["kick"]}})
    boss = _admin(console)
    _client(console, name="Bob", cid="7", id_=7)

    await _run(console, boss, '!ckick bob said "hi"; quit')

    assert console.rcon_sent == ['command "kick 7 said hi quit"']


@pytest.mark.asyncio
async def test_the_passthrough_is_escaped_too(console):
    _codam(console)
    boss = _admin(console)

    await _run(console, boss, '!codam say "hello"; quit')

    assert console.rcon_sent == ['command "say hello quit"']


# -- who may be acted on -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mods_kick_will_not_reach_an_equal(console):
    """The classic checked nothing here, so `!ckick` went past a level the bot's own `!kick` will
    not touch."""
    _codam(console, {"player_commands": {"mod": ["kick"]}})
    one = _client(console, name="One", cid="1", id_=1, bits=16)  # admin, level 40
    _client(console, name="Two", cid="2", id_=2, bits=16)

    await _run(console, one, "!ckick two")

    assert console.rcon_sent == []
    assert "at or above your level" in _last(console, one)


@pytest.mark.asyncio
async def test_nor_yourself(console):
    _codam(console, {"player_commands": {"mod": ["kick"]}})
    boss = _admin(console)

    await _run(console, boss, "!ckick boss")

    assert console.rcon_sent == []
    assert "yourself" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_masked_admin_is_refused_as_a_masked_admin(console):
    _codam(console, {"player_commands": {"mod": ["kick"]}})
    one = _client(console, name="One", cid="1", id_=1, bits=16)
    _client(console, name="Two", cid="2", id_=2, bits=64, mask=1)  # senioradmin, hiding it

    await _run(console, one, "!ckick two")

    assert console.rcon_sent == []
    assert "masked" in _last(console, one)


@pytest.mark.asyncio
async def test_a_player_is_resolved_the_way_every_other_command_resolves_one(console):
    """Two candidates is a question, not a coin toss — and the classic's own rule refused a handle
    of fewer than two characters, so a one-letter name could not be named at all."""
    _codam(console, {"player_commands": {"admin": ["kick"]}})
    boss = _admin(console)
    bob = _client(console, name="Bob", cid="7", id_=7)
    bobby = _client(console, name="Bobby", cid="8", id_=8)
    console.register_clients("bob", [bob, bobby])

    await _run(console, boss, "!ckick bob")

    assert console.rcon_sent == []
    assert "Bob" in _last(console, boss) and "Bobby" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_verb_that_names_a_player_needs_one(console):
    _codam(console, {"player_commands": {"admin": ["kick"]}})
    boss = _admin(console)

    await _run(console, boss, "!ckick")

    assert console.rcon_sent == []
    assert "ckick <player>" in _last(console, boss)


@pytest.mark.asyncio
async def test_a_player_with_no_slot_number_cannot_be_named_to_the_mod(console):
    """The mod is told about a player by slot; there is nothing to send for somebody the server has
    not given one to, and sending the word `None` is how the classic would have found out."""
    _codam(console, {"player_commands": {"admin": ["kick"]}})
    boss = _admin(console)
    _client(console, name="Ghost", cid="", id_=9)

    await _run(console, boss, "!ckick ghost")

    assert console.rcon_sent == []
    assert "no slot number" in _last(console, boss)


# -- what the config offers, and what it refuses -------------------------------------------------


@pytest.mark.asyncio
async def test_a_verb_is_registered_at_the_level_its_group_names(console):
    _codam(console, {"commands": {"fulladmin": ["reloadscripts"]}})
    admin_level = _client(console, name="Ann", cid="3", id_=3, bits=16)  # 40, under fulladmin

    await _run(console, admin_level, "!creloadscripts")

    assert "sufficient access" in _last(console, admin_level)
    assert console.rcon_sent == []


def test_a_level_number_works_as_well_as_a_keyword(console):
    _codam(console, {"commands": {"55": ["restart"]}})

    assert console.command_registry.get("crestart").min_level == 55


def test_a_single_verb_need_not_be_written_as_a_list(console):
    _codam(console, {"commands": {"admin": "restart"}})

    assert console.command_registry.get("crestart") is not None


def test_a_group_nobody_recognises_is_refused_by_name(console, caplog):
    with caplog.at_level("ERROR"):
        _codam(console, {"commands": {"moderators": ["restart"]}})

    assert console.command_registry.get("crestart") is None
    assert "moderators" in caplog.text


def test_a_verb_that_could_not_be_a_command_name_is_refused(console, caplog):
    with caplog.at_level("ERROR"):
        _codam(console, {"commands": {"admin": ["kick 3", "say hello", ""]}})

    assert console.command_registry.get("ckick 3") is None
    assert console.command_registry.get("csay hello") is None
    assert "an admin mod could have" in caplog.text


def test_a_name_that_belongs_to_another_plugin_is_refused_with_the_owner(console, caplog):
    """`!clear` is the admin plugin's. Without this the mod's `lear` would shadow it silently."""
    with caplog.at_level("ERROR"):
        _codam(console, {"commands": {"admin": ["lear"]}})

    assert console.command_registry.get("clear").plugin.__class__.__name__ == "AdminPlugin"
    assert "already a command, registered by admin" in caplog.text


def test_no_verbs_at_all_says_so(console, caplog):
    with caplog.at_level("WARNING"):
        _codam(console)

    assert "no admin-mod commands are configured" in caplog.text


def test_a_section_written_as_a_list_rather_than_a_table_is_reported(console, caplog):
    with caplog.at_level("ERROR"):
        _codam(console, {"commands": ["restart"]})

    assert "must be a mapping of level" in caplog.text


def test_both_halves_of_the_config_are_read(console):
    plugin = _codam(
        console,
        {"commands": {"admin": ["restart"]}, "player_commands": {"mod": ["kick", "warn"]}},
    )

    assert set(plugin.verbs) == {"crestart", "ckick", "cwarn"}
    assert plugin.verbs["crestart"].takes_player is False
    assert plugin.verbs["ckick"].takes_player is True


# -- it is a Call of Duty plugin ------------------------------------------------------------------


def test_the_loader_refuses_it_on_a_game_with_no_such_mod(console):
    """The classic loaded on any title, where the one verb it sends exists on none of the others."""
    from b3.config.schema import Config, PluginEntry, ServerConfig
    from b3.core.pluginmgr import load_plugins

    loaded = load_plugins(
        console,
        Config(
            server=ServerConfig(game="urt"),
            plugins=[PluginEntry(name="admin"), PluginEntry(name="codam")],
        ),
    )
    codam = next(item for item in loaded if item.name == "codam")

    assert codam.enabled is False
    assert "does not support the 'urt' parser" in codam.reason


def test_every_call_of_duty_title_is_accepted(console):
    from b3.parsers.cod import profiles as cod_profiles

    assert set(CodamPlugin.requires_parsers or ()) == set(cod_profiles.ALL)


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_configured_verb_reaches_a_real_server(tmp_path):
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
        server=ServerConfig(game="cod"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="codam")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = CodamPlugin(bot, {"player_commands": {"guest": ["kick"]}})
    bot.add_plugin(plugin, "codam")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()

    boss, bob = "a" * 6, "b" * 6
    await bot.replay([f"J;{boss};1;Boss", f"J;{bob};2;Bob"])
    await bot.bus.drain()
    bot.clients.get_by_cid("1").group_bits = 128  # somebody has to outrank the player being kicked

    await bot.replay([f"say;{boss};1;Boss;!ckick bob being rude"])
    await bot.bus.drain()

    assert 'command "kick 2 being rude"' in rcon.commands
    bot.storage.close()
