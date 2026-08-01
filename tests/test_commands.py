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
