"""Plugin base class.

Keeps the genuinely good parts of the legacy design — the declarative ``requires*`` manifest and the
lifecycle hooks — while dropping the string-convention class discovery and the dependence on the
admin plugin. Commands are declared with :func:`b3.core.commands.command` and registered into the
CORE registry; event subscriptions go through the core bus and are gated by the plugin's enabled
state, so disabling a plugin cleanly silences its handlers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from abc import ABC
from collections.abc import Callable
from typing import Any

from b3.core.bus import Subscription
from b3.core.commands import Command, CommandContext
from b3.core.console import Console
from b3.core.events import EventType
from b3.core.scheduler import ScheduledTask
from b3.domain.client import Client

log = logging.getLogger(__name__)


class Plugin(ABC):
    # Declarative manifest (drives dependency resolution + load ordering in the loader).
    requires_plugins: tuple[str, ...] = ()
    load_after: tuple[str, ...] = ()
    requires_parsers: tuple[str, ...] | None = None

    def __init__(self, console: Console, config: object | None = None) -> None:
        self.console = console
        self.config = config
        self._enabled = True
        self._started = False
        self.disabled_reason = ""  # set by the loader when it starts a plugin off
        self._subs: list[Subscription] = []
        self._tasks: list[ScheduledTask] = []  # scheduled work, torn down on unload
        self._commands: list[Command] = []

    def resolve_path(self, value: str) -> Path:
        """Expand the `@b3` / `@conf` / `@home` tokens a path setting may use.

        `@conf` is the directory this server's main config was loaded from, which is what makes an
        instance directory portable: the database, the ban lists and the status file are named
        relative to the config that mentions them rather than to whatever directory the bot happened
        to be started from.

        Here rather than in each plugin because it was in three of them already, in three copies, and
        the plugins that had *not* copied it reported the token back as though it were a filename —
        "database '@conf/GeoLite2-City.mmdb' does not exist", which sends an operator looking for a
        directory called `@conf`.
        """
        from b3.config.loader import resolve_path_token

        config_path = getattr(self.console, "config_path", None)
        conf_dir = Path(config_path).parent if config_path else None
        return Path(resolve_path_token(value, conf_dir))

    # -- lifecycle hooks (override as needed) ------------------------------

    def on_load_config(self) -> None: ...
    def on_startup(self) -> None: ...
    def on_enable(self) -> None: ...
    def on_disable(self) -> None: ...

    # -- driven by the loader ----------------------------------------------

    def start(self) -> None:
        """Run the startup sequence. Idempotent — a plugin enabled at runtime starts on enable."""
        if self._started:
            return
        self._started = True
        self.on_load_config()
        self.register_commands()
        self.on_startup()

    def is_started(self) -> bool:
        return self._started

    def register_commands(self) -> None:
        """Discover @command-decorated methods and register them into the core registry."""
        seen: set[str] = set()
        for klass in type(self).__mro__:
            for name, member in vars(klass).items():
                if name in seen:
                    continue
                meta = getattr(member, "_command", None)
                if meta is None:
                    continue
                seen.add(name)
                cmd = Command(
                    name=meta["name"],
                    handler=getattr(self, name),  # bound method
                    min_level=meta["level"],
                    max_level=meta["max_level"],
                    alias=meta["alias"],
                    help=meta["help"],
                    plugin=self,
                )
                # A command another plugin already owns is refused, with both names in the log —
                # see `CommandRegistry.register`. Not appended here either, or `unload` would
                # unregister somebody else's command on its way out.
                if self.console.command_registry.register(cmd):
                    self._commands.append(cmd)

    def subscribe(self, event_type: EventType, handler: Callable[..., Any]) -> Subscription:
        sub = self.console.bus.subscribe(event_type, handler, is_enabled=self.is_enabled)
        self._subs.append(sub)
        return sub

    def schedule(self, handler: Callable[..., Any], **fields: Any) -> ScheduledTask:
        """Register timed work — the legacy ``PluginCronTab``.

        Accepts the crontab fields of :meth:`b3.core.scheduler.Scheduler.add` (``second``,
        ``minute``, ``hour``, ``day``, ``month``, ``dow``, ``one_shot``). The schedule is gated on
        this plugin being enabled and is removed when the plugin unloads.
        """
        fields.setdefault("name", f"{type(self).__name__}.{getattr(handler, '__name__', 'task')}")
        task = self.console.scheduler.add(handler, plugin=self, **fields)
        self._tasks.append(task)
        return task

    def message(self, key: str, **values: Any) -> str:
        """Look up an operator-customisable message template (see :mod:`b3.core.messages`)."""
        return self.console.messages.get(key, **values)

    def resolve_client(self, ctx: CommandContext, handle: str) -> Client | None:
        """Resolve a handle an admin typed to exactly one connected player, or refuse and explain.

        ``!ban bob`` with "bob" and "bobby" both in the server lists the candidates and does nothing
        until the admin names a slot, rather than acting on whichever the roster lists first.
        """
        found = self.console.find_clients(handle)
        if not found:
            ctx.reply(self.message("player_not_found", handle=handle))
            return None
        if len(found) > 1:
            listed = ", ".join(f"{c.name} [{c.cid}]" for c in found)
            ctx.reply(self.message("ambiguous_connected", count=len(found), candidates=listed))
            return None
        return found[0]

    def storage_engine(self) -> object:
        """The SQLAlchemy engine behind the bot's storage — for plugins that own tables.

        The classic bot let plugins write raw SQL through ``console.storage.query()``; that took
        a format string and was a live injection surface, so it is gone. A plugin that needs its
        own tables declares SQLAlchemy models and creates them here, sharing the core's engine and
        connection pool:

            Base.metadata.create_all(self.storage_engine())

        Keep to your own tables. The core's schema is Alembic-managed and yours is not — creating
        or altering a core table from a plugin will be undone by the next migration.

        Raises :class:`RuntimeError` if the configured storage has no engine to share.
        """
        engine = getattr(self.console.storage, "engine", None)
        if engine is None:
            raise RuntimeError(
                f"{type(self).__name__}: this storage backend exposes no SQLAlchemy engine"
            )
        return engine

    def register_messages(self, defaults: dict[str, str]) -> None:
        """Publish this plugin's own message defaults, so operators can reword them.

        Call it from ``on_startup``. Without it a plugin's text is hardcoded English while the
        core's is customisable — an inconsistency an external plugin author hits immediately.
        """
        register = getattr(self.console.messages, "register_defaults", None)
        if register is not None:
            register(defaults)

    # -- enable / disable --------------------------------------------------

    def mark_disabled(self, reason: str = "") -> None:
        """Put a not-yet-started plugin into the off state — used by the loader.

        Distinct from :meth:`disable`: nothing has run yet, so there is no ``on_disable`` to fire.
        """
        self._enabled = False
        self.disabled_reason = reason

    def is_enabled(self) -> bool:
        return self._enabled

    def enable(self) -> None:
        self._enabled = True
        self.disabled_reason = ""
        self.start()  # loaded-but-disabled plugins deferred their startup until now
        self.on_enable()

    def disable(self) -> None:
        self._enabled = False
        self.on_disable()

    def unload(self) -> None:
        for sub in self._subs:
            self.console.bus.unsubscribe(sub)
        self._subs.clear()
        for task in self._tasks:
            self.console.scheduler.remove(task)
        self._tasks.clear()
        self.console.command_registry.unregister_plugin(self)
        self._commands.clear()
        self._started = False  # a later enable() re-runs startup and re-registers
