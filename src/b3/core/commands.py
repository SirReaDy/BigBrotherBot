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
            cmd_name = cmd_name[len("cmd_") :]
        func._command = {  # type: ignore[attr-defined]
            "name": cmd_name,
            "level": level,
            "max_level": max_level,
            "alias": alias,
            "help": (func.__doc__ or "").strip(),
        }
        return func

    return decorator


@dataclass(frozen=True, slots=True)
class Conflict:
    """Two plugins wanting one word, and which of them got it."""

    word: str
    kind: str  # "command" or "alias"
    owner: str
    refused: str

    def describe(self) -> str:
        return f"{self.kind} {self.word!r}: kept by {self.owner}, refused to {self.refused}"


def _plugin_name(plugin: object) -> str:
    """What to call a plugin in a message: the name an operator wrote in their `plugins:` list.

    Derived from the class name — every plugin class here is `<Name>Plugin` — rather than asked for,
    because a `Plugin` does not know the name it was loaded under and adding a field for one message
    would be the bigger change.
    """
    if plugin is None:
        return "the bot"
    name = type(plugin).__name__
    return (name[: -len("Plugin")] if name.endswith("Plugin") else name).lower()


class CommandRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._commands: list[Command] = []
        #: An extra source of permission, asked only when a client's level is too low: a callable
        #: ``(client, command name) -> bool``. `cmdmanager` sets it, so one player can be given one
        #: command without being moved up a group. None means levels are the whole story, which is
        #: the case on a server that does not load that plugin.
        #:
        #: Held on the registry rather than patched onto `Command`, which is what the classic bot
        #: did (it rewrote `Command.canUse` on the class *and* on every instance already made). A
        #: hook on the object that owns the commands can be read, tested and turned off again.
        self.grant_check: Callable[[Client, str], bool] | None = None
        #: Names two plugins both wanted. Kept rather than only logged, so the runtime can report
        #: them together once every plugin has started — one line an operator reads, instead of
        #: several scattered through a startup they scrolled past.
        self.conflicts: list[Conflict] = []

    def register(self, cmd: Command) -> bool:
        """Add a command. **Refuses** a name another plugin already owns, and says who owns it.

        This used to override with a warning, which is the worst of both: an operator who loads two
        plugins offering `!paset` — `poweradminurt` and `poweradmincod7` both do — got whichever
        started last, silently, and the log line explaining it scrolled past at startup. The one they
        lost is the one they configured.

        Refusing is the same rule the loader applies to a missing dependency: a half-configured bot is
        worse than one that says what is wrong. The command that was registered *first* wins, which is
        load order, which is the operator's `plugins:` list — so the fix is theirs to make and this
        names both sides of it.

        An **alias** clash is not fatal to the command: the command registers under its own name and
        loses only the short form, because losing `!pb` is a smaller thing than losing the command,
        and `!cmdalias` can give it another one.
        """
        existing = self._by_name.get(cmd.name)
        if existing is not None:
            if existing.plugin is cmd.plugin:
                return True  # the same plugin restarting; nothing has changed
            self._refuse(cmd.name, existing, cmd, "command")
            return False
        self._by_name[cmd.name] = cmd
        if cmd.alias:
            clash = self._by_name.get(cmd.alias)
            if clash is not None and clash.plugin is not cmd.plugin:
                self._refuse(cmd.alias, clash, cmd, "alias")
                cmd.alias = None
            else:
                self._by_name[cmd.alias] = cmd
        self._commands.append(cmd)
        return True

    def _refuse(self, word: str, existing: Command, wanted: Command, kind: str) -> None:
        """Record and report one collision, naming both owners so the fix is obvious."""
        owner = _plugin_name(existing.plugin)
        asker = _plugin_name(wanted.plugin)
        self.conflicts.append(Conflict(word=word, kind=kind, owner=owner, refused=asker))
        log.error(
            "%s %r belongs to the %s plugin, so %s cannot have it%s",
            kind,
            word,
            owner,
            asker,
            "" if kind == "command" else " — that command keeps its full name",
        )

    def get(self, name: str) -> Command | None:
        return self._by_name.get(name.lower())

    def set_alias(self, cmd: Command, alias: str) -> bool:
        """Give a command a different short form at runtime — `cmdmanager`'s `!cmdalias`.

        False when the alias is already something else's, because two commands answering to one word
        means whichever registered last wins, silently. Re-indexes rather than only setting the
        field: the old alias has to stop working, or a command ends up with two and the config that
        describes it is wrong about one of them.
        """
        wanted = alias.strip().lower()
        if not wanted:
            return False
        existing = self._by_name.get(wanted)
        if existing is not None and existing is not cmd:
            return False
        if cmd.alias:
            self._by_name.pop(cmd.alias, None)
        cmd.alias = wanted
        self._by_name[wanted] = cmd
        return True

    def all(self) -> list[Command]:
        return list(self._commands)

    def may_use(self, client: Client, cmd: Command) -> bool:
        """Whether this client may run this command — by level, or by a grant given to them.

        One place, so `!help` lists exactly what the processor will let somebody run. A grant that
        showed up at the prompt but not in the list (or the other way round) would be worse than no
        grants at all.
        """
        if cmd.can_use(client):
            return True
        return bool(self.grant_check and self.grant_check(client, cmd.name))

    def usable_by(self, client: Client) -> list[Command]:
        return [c for c in self._commands if c.is_available() and self.may_use(client, c)]

    def for_plugin(self, plugin: object) -> list[Command]:
        """Every command a given plugin registered — what `!plugin info` reports."""
        return [c for c in self._commands if c.plugin is plugin]

    def unregister_plugin(self, plugin: object) -> None:
        self._commands = [c for c in self._commands if c.plugin is not plugin]
        self._by_name = {k: c for k, c in self._by_name.items() if c.plugin is not plugin}


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

        if not self._registry.may_use(issuer, cmd):
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
