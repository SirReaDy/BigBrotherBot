"""Changes what a command is called, who may run it, and who may run it *this once* — at runtime.

A port of the classic `cmdmanager` plugin. Three genuinely useful things live in it: moving a command
to a different level without editing a file and restarting, giving it a shorter alias, and — the one
nothing else can do — **granting one player one command** without promoting them into a group that
carries everything else with it.

Kept from the classic: the five commands (`!cmdlevel`, `!cmdalias`, `!cmdgrant`, `!cmdrevoke`,
`!cmduse`), the `min-max` level form, group keywords or numbers, and grants that survive a restart.

Changed, and each for a reason:

* **Nothing is monkey-patched.** The classic rewrote `Command.canUse` on the class *and* on every
  instance already registered, which is why installing it changed the permission rule for every
  plugin at once and could not be undone. The registry has a `grant_check` hook instead: this plugin
  sets it, and a server without this plugin behaves exactly as if grants did not exist.
* **Other plugins' config files are left alone.** The classic wrote the new level back into the
  owning plugin's `.ini` or `.xml`, which meant a plugin editing a file it does not own — through a
  round trip that discards comments. Overrides live in this plugin's own table, applied at startup,
  which is also the only version of this that works when a level was set for a plugin whose config
  file does not exist.
* **An override survives `!plugin enable`.** Enabling a plugin re-registers its commands at their
  code defaults, so without re-applying, a level somebody set an hour ago silently reverts.
* **A grant is refused when it would grant nothing.** Granting a command the player can already run
  is a no-op the classic reported as success.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from sqlalchemy import Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from b3.core.commands import Command, CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.domain.client import Client
from b3.domain.permissions import group_by_level, level_for

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

#: `mod`, `20`, `mod-admin`, `20-40`. The classic's form, and the dash is what makes a *ceiling*
#: expressible — a command only the lower groups should see.
LEVEL_RE = re.compile(r"^(?P<low>\w+)(?:\s*-\s*(?P<high>\w+))?$")

MESSAGES = {
    "cmd_level_is": "{command} is {level}",
    "cmd_level_set": "{command} is now {level}",
    "cmd_alias_is": "{command} has the alias {alias}",
    "cmd_alias_none": "{command} has no alias",
    "cmd_alias_set": "{command} can now also be typed {alias}",
    "cmd_alias_taken": "{alias} is already {command}",
    "cmd_unknown": "there is no {command} command",
    "cmd_bad_level": "{level} is not a level or a group keyword",
    "cmd_bad_range": "{low} is above {high}, so nobody could use it",
    "cmd_above_you": "you cannot change {command}, which you cannot use yourself",
    "cmd_granted": "{name} may now use {command}",
    "cmd_grant_pointless": "{name} can already use {command}",
    "cmd_revoked": "{name} may no longer use {command}",
    "cmd_no_grant": "{name} had no grant for {command}",
    "cmd_can_use": "{name} can use {command}",
    "cmd_cannot_use": "{name} cannot use {command}",
    "cmd_not_stored": "changes will be lost when the bot restarts - this storage keeps nothing",
}


class Base(DeclarativeBase):
    """This plugin's own tables. Nothing here touches the core's Alembic-managed schema."""


class CommandOverride(Base):
    """A command's level and alias as an operator last set them, so a restart keeps them."""

    __tablename__ = "cmdmanager_overrides"

    command: Mapped[str] = mapped_column(String(32), primary_key=True)
    min_level: Mapped[int] = mapped_column(Integer, default=0)
    max_level: Mapped[int] = mapped_column(Integer, default=100)
    alias: Mapped[str] = mapped_column(String(32), default="")


class CommandGrant(Base):
    """One player, one command they may use whatever their level says."""

    __tablename__ = "cmdmanager_grants"

    client_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(String(32), primary_key=True)


def level_name(level: int) -> str:
    """A level as an operator wrote it: the group keyword where there is one, else the number."""
    group = group_by_level(level)
    return group.keyword if group is not None else str(level)


def describe_level(cmd: Command) -> str:
    """``mod`` or ``mod-admin`` — the ceiling is only mentioned when there is one."""
    low = level_name(cmd.min_level)
    if cmd.max_level >= 100:
        return low
    return f"{low}-{level_name(cmd.max_level)}"


def parse_level(text: str) -> tuple[int, int] | str:
    """Read `mod`, `20`, `mod-admin` or `20-40` into a range, or return which word was wrong.

    A string comes back rather than an exception because every caller has to tell the admin *which*
    half they typed wrongly — "invalid level" on its own sends somebody to the documentation to find
    out that they wrote `moderator` instead of `mod`.
    """
    match = LEVEL_RE.match(text.strip())
    if match is None:
        return text.strip()

    low = level_for(match["low"])
    if low is None:
        return match["low"]
    if match["high"] is None:
        # No ceiling asked for: superadmins can use everything, which is the classic's default and
        # the only sane one — the alternative is a command its own author cannot run.
        return low, 100
    high = level_for(match["high"])
    if high is None:
        return match["high"]
    return low, high


class CmdmanagerPlugin(Plugin):
    """Runtime control over command levels, aliases, and per-player grants."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        #: client id -> the commands granted to them. Loaded on authentication, so a grant given
        #: while somebody is offline is in force the next time they play.
        self._grants: dict[int, set[str]] = {}
        self._engine: Engine | None = None

    # -- setup ---------------------------------------------------------------

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self._engine = self._open_storage()
        # The hook, rather than the classic's monkey-patch of `Command.canUse`. Set here and cleared
        # on unload, so a server that removes this plugin goes back to levels deciding everything.
        self.console.command_registry.grant_check = self._may_use
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        # Enabling a plugin registers its commands again, at the levels in their code — which would
        # quietly undo an override an operator set an hour ago.
        self.subscribe(EventType.PLUGIN_ENABLED, self.on_plugin_enabled)
        self.apply_overrides()

    def on_enable(self) -> None:
        """Start granting again. `Plugin.start` is idempotent, so enabling would otherwise leave the
        hook cleared and every grant silently inert."""
        self.console.command_registry.grant_check = self._may_use

    def on_disable(self) -> None:
        """Stop granting anything. A disabled plugin must not still be widening permissions.

        Compared with `==` rather than `is`: `self._may_use` builds a *new* bound method on every
        attribute access, so identity is never true and the hook would never be cleared. Equality on
        bound methods is exactly the question being asked — same function, same instance.
        """
        if self.console.command_registry.grant_check == self._may_use:
            self.console.command_registry.grant_check = None

    def _open_storage(self) -> Engine | None:
        """This plugin's own tables, or None with a warning if the storage cannot share an engine.

        Without them it still works for the session, which is worth having — but it is said out loud,
        because "I set that level yesterday" and "the bot restarted" are the same sentence otherwise.
        """
        try:
            engine = self.storage_engine()
        except RuntimeError as exc:
            log.warning("cmdmanager: %s; levels and grants will not survive a restart", exc)
            return None
        try:
            Base.metadata.create_all(engine)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop the bot over its own table
            log.error("cmdmanager: could not create its tables (%s); nothing will be saved", exc)
            return None
        return engine  # type: ignore[return-value]

    # -- overrides -----------------------------------------------------------

    def apply_overrides(self) -> None:
        """Put every stored level and alias back on its command.

        A stored override for a command nobody registered is kept rather than dropped: it usually
        means the plugin that owns it is disabled today and will be enabled again tomorrow.
        """
        if self._engine is None:
            return
        with Session(self._engine) as session:
            for row in session.scalars(select(CommandOverride)).all():
                cmd = self.console.command_registry.get(row.command)
                if cmd is None:
                    log.debug("cmdmanager: no %s command loaded; keeping its override", row.command)
                    continue
                cmd.min_level = row.min_level
                cmd.max_level = row.max_level
                if row.alias and row.alias != cmd.alias:
                    self.console.command_registry.set_alias(cmd, row.alias)
                log.info(
                    "cmdmanager: %s is %s%s",
                    cmd.name,
                    describe_level(cmd),
                    f", alias {cmd.alias}" if cmd.alias else "",
                )

    def on_plugin_enabled(self, event: Event) -> None:
        self.apply_overrides()

    def _store_override(self, cmd: Command) -> None:
        if self._engine is None:
            return
        with Session(self._engine) as session:
            row = session.get(CommandOverride, cmd.name)
            if row is None:
                row = CommandOverride(command=cmd.name)
                session.add(row)
            row.min_level = cmd.min_level
            row.max_level = cmd.max_level
            row.alias = cmd.alias or ""
            session.commit()

    # -- grants --------------------------------------------------------------

    def on_auth(self, event: Event) -> None:
        client = event.client
        if client is None or client.id is None or self._engine is None:
            return
        with Session(self._engine) as session:
            rows = session.scalars(
                select(CommandGrant).where(CommandGrant.client_id == client.id)
            ).all()
        granted = {row.command for row in rows}
        if granted:
            self._grants[client.id] = granted
            log.info("cmdmanager: %s is granted %s", client.name, ", ".join(sorted(granted)))

    def _may_use(self, client: Client, name: str) -> bool:
        """The registry's hook: does this player hold a grant for this command?

        Only ever *widens* access, and only for the exact command name — never an alias, because a
        grant is a decision about a command and its aliases can change under it.
        """
        if client.id is None:
            return False
        return name in self._grants.get(client.id, set())

    def grant(self, client: Client, name: str) -> None:
        if client.id is None:
            return
        self._grants.setdefault(client.id, set()).add(name)
        if self._engine is None:
            return
        with Session(self._engine) as session:
            if session.get(CommandGrant, (client.id, name)) is None:
                session.add(CommandGrant(client_id=client.id, command=name))
                session.commit()

    def revoke(self, client: Client, name: str) -> bool:
        if client.id is None:
            return False
        had = name in self._grants.get(client.id, set())
        self._grants.get(client.id, set()).discard(name)
        if self._engine is not None:
            with Session(self._engine) as session:
                row = session.get(CommandGrant, (client.id, name))
                if row is not None:
                    session.delete(row)
                    session.commit()
                    had = True
        return had

    # -- helpers -------------------------------------------------------------

    def _find_command(self, ctx: CommandContext, name: str) -> Command | None:
        """The command an admin named, if they are allowed to touch it.

        An admin cannot change a command they could not run themselves. The classic checked this on
        `!cmdgrant` and `!cmduse` and not on `!cmdlevel`, so the level of a superadmin-only command
        could be lowered by anybody holding `!cmdlevel` — which is a way to hand yourself `!die`.
        """
        cmd = self.console.command_registry.get(name.strip().lower())
        if cmd is None:
            ctx.reply(self.message("cmd_unknown", command=name.strip().lower()))
            return None
        if not self.console.command_registry.may_use(ctx.client, cmd):
            ctx.reply(self.message("cmd_above_you", command=cmd.name))
            return None
        return cmd

    def _two_words(self, ctx: CommandContext, usage: str) -> tuple[str, str] | None:
        parts = ctx.args.split()
        if len(parts) != 2:
            ctx.reply(self.message("usage", usage=usage))
            return None
        return parts[0], parts[1].lower()

    def _report_storage(self, ctx: CommandContext) -> None:
        if self._engine is None:
            ctx.reply(self.message("cmd_not_stored"))

    # -- commands ------------------------------------------------------------

    @command(level=100)
    def cmd_cmdlevel(self, ctx: CommandContext) -> None:
        """cmdlevel <command> [<level>] - read or set the level a command needs (e.g. mod, 20, mod-admin)"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("usage", usage="cmdlevel <command> [<level>]"))
            return
        cmd = self._find_command(ctx, parts[0])
        if cmd is None:
            return
        if len(parts) == 1:
            ctx.reply(self.message("cmd_level_is", command=cmd.name, level=describe_level(cmd)))
            return
        parsed = parse_level(" ".join(parts[1:]))
        if isinstance(parsed, str):
            ctx.reply(self.message("cmd_bad_level", level=parsed))
            return
        low, high = parsed
        if low > high:
            ctx.reply(self.message("cmd_bad_range", low=level_name(low), high=level_name(high)))
            return
        cmd.min_level, cmd.max_level = low, high
        self._store_override(cmd)
        ctx.reply(self.message("cmd_level_set", command=cmd.name, level=describe_level(cmd)))
        self._report_storage(ctx)

    @command(level=100)
    def cmd_cmdalias(self, ctx: CommandContext) -> None:
        """cmdalias <command> [<alias>] - read or set the short form of a command"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("usage", usage="cmdalias <command> [<alias>]"))
            return
        cmd = self._find_command(ctx, parts[0])
        if cmd is None:
            return
        if len(parts) == 1:
            if cmd.alias:
                ctx.reply(self.message("cmd_alias_is", command=cmd.name, alias=cmd.alias))
            else:
                ctx.reply(self.message("cmd_alias_none", command=cmd.name))
            return
        alias = parts[1].lower()
        if not self.console.command_registry.set_alias(cmd, alias):
            taken = self.console.command_registry.get(alias)
            ctx.reply(
                self.message("cmd_alias_taken", alias=alias, command=taken.name if taken else alias)
            )
            return
        self._store_override(cmd)
        ctx.reply(self.message("cmd_alias_set", command=cmd.name, alias=cmd.alias))
        self._report_storage(ctx)

    @command(level=100)
    def cmd_cmdgrant(self, ctx: CommandContext) -> None:
        """cmdgrant <player> <command> - let one player use one command, whatever their level"""
        parsed = self._two_words(ctx, "cmdgrant <player> <command>")
        if parsed is None:
            return
        handle, name = parsed
        target = self.resolve_client(ctx, handle)
        if target is None:
            return
        cmd = self._find_command(ctx, name)
        if cmd is None:
            return
        if cmd.can_use(target):
            # A grant that grants nothing is worse than a refusal: it reads as "done" and then does
            # not survive the player being demoted, which is exactly when it was supposed to matter.
            ctx.reply(self.message("cmd_grant_pointless", name=target.name, command=cmd.name))
            return
        self.grant(target, cmd.name)
        ctx.reply(self.message("cmd_granted", name=target.name, command=cmd.name))
        self._report_storage(ctx)

    @command(level=100)
    def cmd_cmdrevoke(self, ctx: CommandContext) -> None:
        """cmdrevoke <player> <command> - take back a grant"""
        parsed = self._two_words(ctx, "cmdrevoke <player> <command>")
        if parsed is None:
            return
        handle, name = parsed
        target = self.resolve_client(ctx, handle)
        if target is None:
            return
        cmd = self._find_command(ctx, name)
        if cmd is None:
            return
        if self.revoke(target, cmd.name):
            ctx.reply(self.message("cmd_revoked", name=target.name, command=cmd.name))
        else:
            ctx.reply(self.message("cmd_no_grant", name=target.name, command=cmd.name))

    @command(level=100)
    def cmd_cmduse(self, ctx: CommandContext) -> None:
        """cmduse <player> <command> - check whether somebody can use a command"""
        parsed = self._two_words(ctx, "cmduse <player> <command>")
        if parsed is None:
            return
        handle, name = parsed
        target = self.resolve_client(ctx, handle)
        if target is None:
            return
        cmd = self._find_command(ctx, name)
        if cmd is None:
            return
        key = (
            "cmd_can_use"
            if self.console.command_registry.may_use(target, cmd)
            else "cmd_cannot_use"
        )
        ctx.reply(self.message(key, name=target.name, command=cmd.name))


__all__ = [
    "MESSAGES",
    "Base",
    "CmdmanagerPlugin",
    "CommandGrant",
    "CommandOverride",
    "describe_level",
    "level_name",
    "parse_level",
]
