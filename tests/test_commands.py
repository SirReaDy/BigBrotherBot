"""Core command service: registry, parsing, permission gating, reply routing."""

from __future__ import annotations

import pytest

from b3.core.commands import Command, CommandContext, CommandProcessor
from b3.domain.client import Client


def _admin() -> Client:
    return Client(guid="A", name="Admin", group_bits=128)  # superadmin, level 100


def _user() -> Client:
    return Client(guid="U", name="User", group_bits=0)  # guest, level 0


def test_parse_recognizes_prefixes():
    assert CommandProcessor.parse("!kick foo bar") == ("!", "kick", "foo bar")
    assert CommandProcessor.parse("@map") == ("@", "map", "")
    assert CommandProcessor.parse("hello world") is None  # not a command
    assert CommandProcessor.parse("!") is None  # prefix only


def test_a_slash_in_front_of_a_prefix_is_the_battlefield_habit_not_a_command_name():
    """Captured: `tests/core/parsers/frostbite2/test_abstractParser.py:433` (`test_slash_prefix`).

    Battlefield players type `/!help`, because `/` is how that engine's own console is addressed
    and the habit carries into chat. The classic's Frostbite and BattlEye parsers both rewrote it.
    Here `/` is already a prefix of its own — the silent one — so `/!help` parsed as the *silent*
    command named `!help`, which exists nowhere, and the player was told so.
    """
    assert CommandProcessor.parse("/!help") == ("!", "help", "")
    assert CommandProcessor.parse("/@map") == ("@", "map", "")
    assert CommandProcessor.parse("/&kick foo") == ("&", "kick", "foo")


def test_a_plain_slash_is_still_the_silent_prefix():
    """Only the doubled form is rewritten. `/` on its own is a prefix here, which the classic's
    admin plugin also had (`hidecmd_level`) — so turning `/kick` into `!kick` would delete a
    feature to fix a typo.
    """
    assert CommandProcessor.parse("/kick foo") == ("/", "kick", "foo")
    assert CommandProcessor.parse("/") is None  # prefix only, as with the other three


def test_registry_alias_and_usable_by(console):
    calls = []
    console.command_registry.register(
        Command(name="test", handler=lambda ctx: calls.append(ctx), min_level=40, alias="t")
    )
    assert console.command_registry.get("test") is console.command_registry.get("t")
    assert [c.name for c in console.command_registry.usable_by(_admin())] == ["test"]
    assert console.command_registry.usable_by(_user()) == []  # level 0 < 40


@pytest.mark.asyncio
async def test_permission_gate_blocks_low_level(console):
    ran = []
    console.command_registry.register(
        Command(name="secret", handler=lambda ctx: ran.append(True), min_level=40)
    )
    proc = CommandProcessor(console.command_registry, console)

    handled = await proc.handle(_user(), "!secret")
    assert handled is True
    assert ran == []  # blocked
    assert console.told and "sufficient access" in console.told[-1][1]


@pytest.mark.asyncio
async def test_permission_gate_allows_high_level(console):
    ran = []
    console.command_registry.register(
        Command(name="secret", handler=lambda ctx: ran.append(ctx), min_level=40)
    )
    proc = CommandProcessor(console.command_registry, console)

    await proc.handle(_admin(), "!secret with args")
    assert len(ran) == 1
    assert ran[0].args == "with args"


@pytest.mark.asyncio
async def test_unknown_command_reports(console):
    proc = CommandProcessor(console.command_registry, console)
    await proc.handle(_admin(), "!nope")
    assert console.told and "unknown command" in console.told[-1][1]


@pytest.mark.asyncio
async def test_non_command_is_ignored(console):
    proc = CommandProcessor(console.command_registry, console)
    handled = await proc.handle(_admin(), "just chatting")
    assert handled is False
    assert console.told == []


@pytest.mark.asyncio
async def test_loud_prefix_broadcasts_reply(console):
    def handler(ctx: CommandContext) -> None:
        ctx.reply("pong")

    console.command_registry.register(Command(name="ping", handler=handler, min_level=0))
    proc = CommandProcessor(console.command_registry, console)

    await proc.handle(_admin(), "@ping")
    assert console.said == ["pong"]  # loud -> say, not tell
    assert console.told == []


# -- prefixes are privileges too -------------------------------------------------------------


def _echo(console):
    """Register an `echo` command that replies with its arguments."""
    console.command_registry.register(
        Command(name="echo", handler=lambda ctx: ctx.reply(ctx.args), min_level=0)
    )
    return CommandProcessor(console.command_registry, console)


@pytest.mark.asyncio
async def test_broadcasting_a_reply_needs_a_minimum_level(console):
    """The classic bot gated `@`/`&` at level 9: a fresh player cannot spam the whole server."""
    proc = _echo(console)

    await proc.handle(_user(), "@echo hello")  # level 0
    assert console.said == []
    assert "sufficient access to broadcast" in console.told[-1][1]

    mod = Client(guid="M", name="Mod", cid="2", group_bits=8)  # level 20
    await proc.handle(mod, "@echo hello")
    assert console.said == ["hello"]


@pytest.mark.asyncio
async def test_the_silent_prefix_is_senioradmin_only(console):
    proc = _echo(console)

    mod = Client(guid="M", name="Mod", cid="2", group_bits=8)  # level 20
    await proc.handle(mod, "/echo hello")
    assert "silent commands" in console.told[-1][1]

    senior = Client(guid="S", name="Senior", cid="3", group_bits=64)  # level 80
    await proc.handle(senior, "/echo hello")
    assert console.told[-1] == (senior, "hello")  # ran, and answered privately
    assert console.said == []


# -- the admin-command audit trail ------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_a_command_publishes_admin_command(console):
    """Plugins (chat loggers, activity feeds, a dashboard) need to see what admins did."""
    from b3.core.events import EventType

    seen = []
    console.bus.subscribe(EventType.ADMIN_COMMAND, lambda e: seen.append(e))
    proc = _echo(console)

    await proc.handle(_admin(), "!echo hello there")
    await console.bus.drain()

    assert len(seen) == 1
    assert seen[0].client is not None
    assert seen[0].data == {"command": "echo", "args": "hello there", "success": True}


@pytest.mark.asyncio
async def test_a_failing_command_is_still_audited(console):
    from b3.core.events import EventType

    seen = []
    console.bus.subscribe(EventType.ADMIN_COMMAND, lambda e: seen.append(e))
    console.command_registry.register(Command(name="boom", handler=lambda ctx: 1 / 0, min_level=0))
    proc = CommandProcessor(console.command_registry, console)

    await proc.handle(_admin(), "!boom")
    await console.bus.drain()

    assert seen[0].data["success"] is False


@pytest.mark.asyncio
async def test_a_refused_command_is_not_audited_as_run(console):
    from b3.core.events import EventType

    seen = []
    console.bus.subscribe(EventType.ADMIN_COMMAND, lambda e: seen.append(e))
    console.command_registry.register(
        Command(name="secret", handler=lambda ctx: None, min_level=40)
    )
    proc = CommandProcessor(console.command_registry, console)

    await proc.handle(_user(), "!secret")
    await proc.handle(_user(), "!nosuchcommand")
    await console.bus.drain()

    assert seen == []


# -- two plugins wanting one word ----------------------------------------------------------------


class Alpha:
    """Stands in for a plugin. Only its class name reaches the message."""


class BravoPlugin:
    pass


def test_a_command_another_plugin_owns_is_refused_rather_than_overridden(console, caplog):
    """It used to override with a warning, which is the worst of both: an operator loading two
    plugins that both offer `!paset` — poweradminurt and poweradmincod7 do — got whichever started
    last, silently, and the one they lost is the one they had configured."""
    first = Command(name="paset", handler=lambda ctx: None, plugin=Alpha())
    second = Command(name="paset", handler=lambda ctx: None, plugin=BravoPlugin())

    assert console.command_registry.register(first) is True
    with caplog.at_level("ERROR"):
        assert console.command_registry.register(second) is False

    # The first to load keeps it — which is load order, which is the operator's `plugins:` list.
    assert console.command_registry.get("paset") is first
    assert "alpha" in caplog.text.lower() and "bravo" in caplog.text.lower()


def test_the_collision_is_remembered_so_it_can_be_reported_together(console):
    console.command_registry.register(
        Command(name="kill", handler=lambda ctx: None, plugin=Alpha())
    )
    console.command_registry.register(
        Command(name="kill", handler=lambda ctx: None, plugin=BravoPlugin())
    )

    clash = console.command_registry.conflicts[0]

    assert (clash.word, clash.kind, clash.owner, clash.refused) == (
        "kill",
        "command",
        "alpha",
        "bravo",
    )
    assert "kept by alpha" in clash.describe()


def test_an_alias_clash_costs_the_short_form_and_not_the_command(console, caplog):
    """Losing `!pb` is a smaller thing than losing the command, and `!cmdalias` can give it another
    short form. Losing the command cannot be undone from in game at all."""
    console.command_registry.register(
        Command(name="permban", handler=lambda ctx: None, alias="pb", plugin=Alpha())
    )
    second = Command(name="punkbuster", handler=lambda ctx: None, alias="pb", plugin=BravoPlugin())

    with caplog.at_level("ERROR"):
        assert console.command_registry.register(second) is True

    assert console.command_registry.get("punkbuster") is second  # the command is there
    assert console.command_registry.get("pb").name == "permban"  # the alias is not its
    assert second.alias is None
    assert "alias" in caplog.text


def test_the_same_plugin_registering_twice_is_not_a_collision(console):
    """A plugin disabled and enabled again re-runs its own startup. That is not two owners."""
    plugin = Alpha()
    first = Command(name="afk", handler=lambda ctx: None, plugin=plugin)

    assert console.command_registry.register(first) is True
    assert console.command_registry.register(first) is True
    assert console.command_registry.conflicts == []
