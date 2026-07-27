"""Command + permission handling — a CORE service.

This is the single biggest structural change from the classic bot, and a direct request from the
original B3 developer: in the legacy code the entire command framework (registration, the Command
object, argument parsing, permission checks) lived *inside the admin plugin*, and every other plugin
reached into it via ``console.getPlugin('admin')``. Here it is core infrastructure. Plugins declare
commands with the :func:`command` decorator and register them into the shared :class:`CommandRegistry`;
``admin`` becomes just another consumer.

Permission model is preserved: a command has a ``[min_level, max_level]`` band and is usable by a
client whose effective level (from its group bitmask) falls within it.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from b3.core.events import Event, EventType
from b3.domain.client import Client

if TYPE_CHECKING:
    from b3.core.console import Console

log = logging.getLogger(__name__)

# Chat prefixes that invoke a command: normal (private reply), loud/big (broadcast), and silent
# (the classic bot's `/`, which answers privately and is meant for commands you do not want other
# admins reading over your shoulder).
PREFIXES: dict[str, str] = {"!": "normal", "@": "loud", "&": "big", "/": "silent"}

#: Broadcasting a reply is itself a small privilege — the classic bot gated `@`/`&` at level 9.
DEFAULT_LOUD_LEVEL = 9
#: ...and the silent `/` prefix at senioradmin (its `hidecmd_level`).
DEFAULT_SILENT_LEVEL = 80


@dataclass(slots=True)
class Command:
    name: str
    handler: Callable[["CommandContext"], Any]
    min_level: int = 0
    max_level: int = 100
    alias: str | None = None
    help: str = ""
    plugin: object | None = None

    def can_use(self, client: Client) -> bool:
        return self.min_level <= client.max_level() <= self.max_level

    def is_available(self) -> bool:
        """False once the owning plugin has been disabled at runtime."""
        is_enabled = getattr(self.plugin, "is_enabled", None)
        return True if is_enabled is None else bool(is_enabled())


@dataclass(slots=True)
class CommandContext:
    client: Client  # the client issuing the command
    args: str  # everything after the command name
    command: Command
    console: "Console"
    loud: bool = False  # invoked with @ or & -> reply broadcast

    def reply(self, text: str) -> None:
        self.console.command_reply(self, text)

    def arg_list(self) -> list[str]:
        return self.args.split()


def command(
    name: str | None = None,
    *,
    level: int = 0,
    max_level: int = 100,
    alias: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a plugin method as a command handler.

    The command name defaults to the method name with a leading ``cmd_`` stripped; the help text is
    the method docstring. This replaces the legacy ``cmd_<name>`` naming convention + the config
    ``[commands]`` level table with an explicit, self-documenting declaration.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        cmd_name = name or func.__name__
        if cmd_name.startswith("cmd_"):
            cmd_name = cmd_name[len("cmd_"):]
        func._command = {  # type: ignore[attr-defined]
            "name": cmd_name,
            "level": level,
            "max_level": max_level,
            "alias": alias,
            "help": (func.__doc__ or "").strip(),
        }
        return func

    return decorator


class CommandRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._commands: list[Command] = []

    def register(self, cmd: Command) -> None:
        if cmd.name in self._by_name:
            log.warning("command %r already registered; overriding", cmd.name)
        self._by_name[cmd.name] = cmd
        if cmd.alias:
            self._by_name[cmd.alias] = cmd
        self._commands.append(cmd)

    def get(self, name: str) -> Command | None:
        return self._by_name.get(name.lower())

    def all(self) -> list[Command]:
        return list(self._commands)

    def usable_by(self, client: Client) -> list[Command]:
        return [c for c in self._commands if c.is_available() and c.can_use(client)]

    def for_plugin(self, plugin: object) -> list[Command]:
        """Every command a given plugin registered — what `!plugin info` reports."""
        return [c for c in self._commands if c.plugin is plugin]

    def unregister_plugin(self, plugin: object) -> None:
        self._commands = [c for c in self._commands if c.plugin is not plugin]
        self._by_name = {
            k: c for k, c in self._by_name.items() if c.plugin is not plugin
        }


class CommandProcessor:
    """Parses chat lines, enforces permissions, and dispatches to command handlers."""

    def __init__(
        self,
        registry: CommandRegistry,
        console: "Console",
        *,
        loud_level: int = DEFAULT_LOUD_LEVEL,
        silent_level: int = DEFAULT_SILENT_LEVEL,
    ) -> None:
        self._registry = registry
        self._console = console
        self._loud_level = loud_level
        self._silent_level = silent_level

    @staticmethod
    def parse(text: str) -> tuple[str, str, str] | None:
        """Return (prefix, name, args) if ``text`` is a command, else None."""
        if not text:
            return None
        prefix = text[0]
        if prefix not in PREFIXES:
            return None
        body = text[1:].strip()
        if not body:
            return None
        name, _, args = body.partition(" ")
        return prefix, name.lower(), args.strip()

    async def handle(self, issuer: Client, text: str) -> bool:
        """Handle a chat line as a potential command. Returns True if it was a command."""
        parsed = self.parse(text)
        if parsed is None:
            return False
        prefix, name, args = parsed

        cmd = self._registry.get(name)
        if cmd is None:
            self._tell(issuer, "unknown_command", command=name)
            return True

        if not cmd.is_available():
            self._tell(issuer, "command_disabled", command=name)
            return True

        if not cmd.can_use(issuer):
            self._tell(issuer, "insufficient_access", command=name)
            return True

        # The prefix is a privilege of its own: broadcasting to the server, or hiding a command
        # from the other admins, are both things a fresh player should not be able to do.
        level = issuer.max_level()
        if prefix in ("@", "&") and level < self._loud_level:
            self._tell(issuer, "prefix_denied_loud", prefix=prefix)
            return True
        if prefix == "/" and level < self._silent_level:
            self._tell(issuer, "prefix_denied_silent", prefix=prefix)
            return True

        ctx = CommandContext(
            client=issuer, args=args, command=cmd, console=self._console, loud=prefix in ("@", "&")
        )
        succeeded = True
        try:
            result = cmd.handler(ctx)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - a bad command must not crash the bot
            log.exception("command %r raised", name)
            self._tell(issuer, "command_error", command=name)
            succeeded = False
        # Announce what an admin did, so plugins can audit it (chat loggers, activity feeds, a
        # dashboard). The classic bot's EVT_ADMIN_COMMAND: present in our vocabulary from the
        # start, but nothing emitted it until a real plugin asked for it.
        self._console.bus.publish_soon(
            Event(
                EventType.ADMIN_COMMAND,
                client=issuer,
                data={"command": cmd.name, "args": args, "success": succeeded},
            )
        )
        return True

    def _tell(self, issuer: Client, key: str, **values: Any) -> None:
        """Reply with an operator-customisable template, falling back if none are configured."""
        messages = getattr(self._console, "messages", None)
        if messages is None:  # a minimal Console (e.g. an embedding host) may not provide them
            self._console.tell(issuer, key)
            return
        self._console.tell(issuer, messages.get(key, **values))
