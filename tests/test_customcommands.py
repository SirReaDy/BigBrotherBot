"""The `customcommands` plugin — commands an operator writes in config rather than in Python.

The classic's placeholder vocabulary is kept verbatim, because operators' config files are full of it,
and its own tests (`tests/plugins/customcommands/test_render_cmd_template.py`) are what pin the corner
cases: one argument per command, an optional argument falling back to its default, and the last
killer/victim being remembered from kills.

The important difference is a security one. The classic substituted a **player's name** straight into
an rcon line, so a player called `bob"; quit` turned an admin's `!slap` into two commands. Everything
substituted here goes through `sanitize_rcon_value`, and the test for it is below.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.parsers.cod.parser import KillData
from b3.plugins.admin import AdminPlugin
from b3.plugins.customcommands import CustomcommandsPlugin


def _custom(console, commands, helps=None):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = CustomcommandsPlugin(console, {"commands": commands, "help": helps or {}})
    plugin.start()
    return plugin


def _client(console, name="Bob", cid="2", id_=2, bits=0, guid="", pbid=""):  # noqa: ANN001, ANN202
    client = Client(
        guid=guid or name[0].upper() * 4,
        name=name,
        cid=cid,
        id=id_,
        group_bits=bits,
        pbid=pbid,
    )
    console.clients.add(client)
    console.register_client(name.lower(), client)
    return client


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _last(console, client):  # noqa: ANN001, ANN202
    told = [text for who, text in console.told if who is client]
    return told[-1] if told else ""


# -- registering ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_configured_command_exists_and_sends_its_line(console):
    _custom(console, {"guest": {"teams": "say the teams are red and blue"}})
    bob = _client(console)

    await _run(console, bob, "!teams")

    assert console.rcon_sent == ["say the teams are red and blue"]


@pytest.mark.asyncio
async def test_a_command_is_registered_at_the_level_its_group_names(console):
    _custom(console, {"mod": {"restartround": "map_restart"}})
    guest = _client(console)
    mod = _client(console, name="Mod", cid="3", id_=3, bits=8)

    await _run(console, guest, "!restartround")
    assert "sufficient access" in _last(console, guest)
    assert console.rcon_sent == []

    await _run(console, mod, "!restartround")
    assert console.rcon_sent == ["map_restart"]


def test_a_level_number_works_as_well_as_a_keyword(console):
    _custom(console, {"40": {"thing": "say hello"}})

    assert console.command_registry.get("thing").min_level == 40


def test_a_group_nobody_recognises_is_refused_by_name(console, caplog):
    with caplog.at_level("ERROR"):
        _custom(console, {"moderators": {"thing": "say hello"}})

    assert console.command_registry.get("thing") is None
    assert "moderators" in caplog.text


def test_the_help_line_names_the_argument_the_template_implies(console):
    _custom(
        console,
        {
            "guest": {
                "slap": "slap <ARG:FIND_PLAYER:PID>",
                "shout": "say <ARG>",
                "greet": "say hello <ARG:OPT:everyone>",
                # Not "nextmap": that is already an admin command, which is its own test below.
                "warpto": "map <ARG:FIND_MAP>",
            }
        },
        helps={"slap": "slap a player about"},
    )
    registry = console.command_registry

    assert registry.get("slap").help == "slap <player> - slap a player about"
    assert registry.get("shout").help == "shout <text>"
    assert registry.get("greet").help == "greet [<text>]"
    assert registry.get("warpto").help == "warpto <map>"


# -- what is refused, and why ---------------------------------------------------------------------


def test_a_name_that_belongs_to_another_plugin_is_refused_with_the_owner(console, caplog):
    with caplog.at_level("ERROR"):
        _custom(console, {"guest": {"kick": "say nope"}})

    assert console.command_registry.get("kick").plugin.__class__.__name__ == "AdminPlugin"
    assert "already a command, registered by admin" in caplog.text


@pytest.mark.parametrize("name", ["a", "1st", "two words", ""])
def test_an_unusable_command_name_is_refused(console, name):  # noqa: ANN001
    _custom(console, {"guest": {name: "say hello"}})

    assert console.command_registry.get(name.strip().lower()) is None


def test_a_template_with_two_arguments_is_refused(console, caplog):
    """A command takes one argument: the text after its name. Two placeholders means one of them was
    never going to be filled in."""
    with caplog.at_level("ERROR"):
        _custom(console, {"guest": {"pair": "say <ARG> and <ARG>"}})

    assert console.command_registry.get("pair") is None
    assert "one argument" in caplog.text


def test_an_unknown_placeholder_is_refused_when_the_command_is_loaded(console, caplog):
    """The classic left it in the rendered line and sent it to the server as literal text, so the
    command looked configured, answered nothing, and failed the same silent way on every use."""
    with caplog.at_level("ERROR"):
        _custom(console, {"guest": {"oops": "kick <PLAYER:CID>"}})

    assert console.command_registry.get("oops") is None
    assert "<PLAYER:CID>" in caplog.text


def test_an_empty_template_is_refused(console, caplog):
    with caplog.at_level("ERROR"):
        _custom(console, {"guest": {"nothing": "   "}})

    assert console.command_registry.get("nothing") is None
    assert "no command line" in caplog.text


def test_no_commands_at_all_says_so(console, caplog):
    with caplog.at_level("WARNING"):
        _custom(console, {})

    assert "will do nothing" in caplog.text


# -- the placeholders ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_required_argument_must_be_given(console):
    _custom(console, {"guest": {"shout": "say <ARG>"}})
    bob = _client(console)

    await _run(console, bob, "!shout")
    assert "needs something after it" in _last(console, bob)
    assert console.rcon_sent == []

    await _run(console, bob, "!shout hello all")
    assert console.rcon_sent == ["say hello all"]


@pytest.mark.asyncio
async def test_an_optional_argument_falls_back_to_its_default(console):
    _custom(console, {"guest": {"greet": "say hello <ARG:OPT:everyone>"}})
    bob = _client(console)

    await _run(console, bob, "!greet")
    assert console.rcon_sent == ["say hello everyone"]

    await _run(console, bob, "!greet Ann")
    assert console.rcon_sent[-1] == "say hello Ann"


@pytest.mark.asyncio
async def test_a_player_argument_is_resolved_and_substituted(console):
    _custom(console, {"guest": {"slap": "slap <ARG:FIND_PLAYER:PID>"}})
    bob = _client(console)
    _client(console, name="Ann", cid="5", id_=5)

    await _run(console, bob, "!slap ann")

    assert console.rcon_sent == ["slap 5"]


@pytest.mark.asyncio
async def test_a_player_argument_can_be_any_of_their_fields(console):
    _custom(
        console,
        {
            "guest": {
                "byguid": "ban <ARG:FIND_PLAYER:GUID>",
                "bypb": "pbban <ARG:FIND_PLAYER:PBID>",
                "byname": "say hello <ARG:FIND_PLAYER:NAME>",
                "byid": "note <ARG:FIND_PLAYER:B3ID>",
            }
        },
    )
    bob = _client(console)
    _client(console, name="Ann", cid="5", id_=5, guid="ANNGUID", pbid="ANNPB")

    for text, expected in (
        ("!byguid ann", "ban ANNGUID"),
        ("!bypb ann", "pbban ANNPB"),
        ("!byname ann", "say hello Ann"),
        ("!byid ann", "note @5"),
    ):
        await _run(console, bob, text)
        assert console.rcon_sent[-1] == expected


@pytest.mark.asyncio
async def test_an_ambiguous_player_is_refused_rather_than_guessed(console):
    """Same rule as every other command that takes a player: `!slap bob` with Bob and Bobby both on
    the server does nothing until an admin says which."""
    _custom(console, {"guest": {"slap": "slap <ARG:FIND_PLAYER:PID>"}})
    caller = _client(console, name="Caller", cid="1", id_=1)
    bob = _client(console, name="Bob", cid="2", id_=2)
    bobby = _client(console, name="Bobby", cid="3", id_=3)
    console.register_clients("bob", [bob, bobby])

    await _run(console, caller, "!slap bob")

    assert console.rcon_sent == []
    assert "Bob" in _last(console, caller) and "Bobby" in _last(console, caller)


@pytest.mark.asyncio
async def test_the_caller_can_be_substituted(console):
    _custom(
        console,
        {
            "guest": {
                "me": "say <PLAYER:NAME> is slot <PLAYER:PID>",
                "mygroup": "say <PLAYER:ADMINGROUP_LONG> level <PLAYER:ADMINGROUP_LEVEL>",
            }
        },
    )
    mod = _client(console, name="Mod", cid="7", id_=7, bits=8)

    await _run(console, mod, "!me")
    assert console.rcon_sent[-1] == "say Mod is slot 7"

    await _run(console, mod, "!mygroup")
    assert console.rcon_sent[-1] == "say Moderator level 20"


@pytest.mark.asyncio
async def test_the_last_killer_and_victim_are_remembered_from_kills(console):
    """The only reason this plugin watches events: a revenge command needs to know who did it."""
    _custom(
        console,
        {
            "guest": {
                "revenge": "say <PLAYER:NAME> wants <LAST_KILLER:NAME>",
                "gloat": "say <PLAYER:NAME> got <LAST_VICTIM:NAME>",
            }
        },
    )
    bob = _client(console)
    ann = _client(console, name="Ann", cid="5", id_=5)

    await console.bus.publish(
        Event(
            EventType.CLIENT_KILL,
            client=ann,
            target=bob,
            data=KillData(weapon="ak47", damage=100, hit_location="", means_of_death="MOD_RIFLE"),
        )
    )

    await _run(console, bob, "!revenge")
    assert console.rcon_sent[-1] == "say Bob wants Ann"

    await _run(console, ann, "!gloat")
    assert console.rcon_sent[-1] == "say Ann got Bob"


@pytest.mark.asyncio
async def test_a_command_about_a_killer_nobody_has_is_refused(console):
    _custom(console, {"guest": {"revenge": "kick <LAST_KILLER:PID>"}})
    bob = _client(console)

    await _run(console, bob, "!revenge")

    assert "nobody has killed you" in _last(console, bob)
    assert console.rcon_sent == []


@pytest.mark.asyncio
async def test_a_map_argument_is_resolved_against_the_rotation(console):
    _custom(console, {"guest": {"go": "map <ARG:FIND_MAP>"}})
    console.maps = ["mp_crossfire", "mp_vacant"]
    bob = _client(console)

    await _run(console, bob, "!go vacant")

    assert console.rcon_sent == ["map mp_vacant"]


@pytest.mark.asyncio
async def test_a_map_nobody_has_is_refused_with_a_few_that_exist(console):
    _custom(console, {"guest": {"go": "map <ARG:FIND_MAP>"}})
    console.maps = ["mp_crossfire", "mp_vacant"]
    bob = _client(console)

    await _run(console, bob, "!go moon")

    assert "no map like moon" in _last(console, bob)
    assert console.rcon_sent == []


# -- the injection the classic allowed ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_hostile_name_cannot_become_a_second_command(console):
    """The classic pasted a player's name into the rcon line untouched. A player called
    `bob"; quit` was therefore able to make somebody else's `!slap` shut the server down."""
    _custom(console, {"guest": {"slap": 'slap "<ARG:FIND_PLAYER:NAME>"'}})
    caller = _client(console, name="Caller", cid="1", id_=1)
    hostile = _client(console, name='bob"; quit', cid="4", id_=4)
    console.register_client("bob", hostile)

    await _run(console, caller, "!slap bob")

    assert console.rcon_sent == ['slap "bob quit"']  # one command, and it is a slap


@pytest.mark.asyncio
async def test_a_hostile_argument_cannot_either(console):
    _custom(console, {"guest": {"shout": "say <ARG>"}})
    bob = _client(console)

    await _run(console, bob, '!shout hello"; quit')

    assert console.rcon_sent == ["say hello quit"]


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_configured_command_reaches_a_real_server(tmp_path):
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
        plugins=[PluginEntry(name="admin"), PluginEntry(name="customcommands")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = CustomcommandsPlugin(
        bot, {"commands": {"guest": {"nextround": "map_restart <PLAYER:PID>"}}}
    )
    bot.add_plugin(plugin, "customcommands")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()

    guid = "b" * 32
    await bot.replay([f"J;{guid};2;Bob", f"say;{guid};2;Bob;!nextround"])
    await bot.bus.drain()

    assert "map_restart 2" in rcon.commands
    bot.storage.close()
