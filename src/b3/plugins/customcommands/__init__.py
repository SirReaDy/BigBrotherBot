"""Commands an operator defines in config, without writing a plugin.

A port of the classic `customcommands` plugin. An operator writes a command name, the level it needs
and the rcon line it should send, and the bot registers it like any other — so `!slap bob` can exist on
a server whose game has a `slap` verb, without anybody writing Python for it.

The templates are the classic's, and its placeholder vocabulary is kept because operators' config
files are full of it:

* ``<ARG>`` — a required argument, substituted verbatim. ``<ARG:OPT:something>`` is the optional form,
  falling back to ``something``.
* ``<ARG:FIND_PLAYER:PID|GUID|PBID|NAME|B3ID>`` — a required argument naming a player, resolved the way
  every other command resolves one, and substituted as that field of them.
* ``<ARG:FIND_MAP>`` — a required argument naming a map, resolved against the rotation.
* ``<PLAYER:...>`` — the same fields, for whoever typed the command, plus ``ADMINGROUP_SHORT``,
  ``ADMINGROUP_LONG`` and ``ADMINGROUP_LEVEL``.
* ``<LAST_KILLER:...>`` and ``<LAST_VICTIM:...>`` — the last player to kill them, and the last they
  killed. This is what makes `!revenge`-style commands possible, and it is why the plugin listens to
  kills at all.

Changed, and the first one is a security fix:

* **Every substituted value is sanitised.** The classic pasted a player's *name* straight into an rcon
  line, so a player called ``bob"; quit`` turned somebody's `!slap` into a second command. Names, ids
  and arguments all go through `sanitize_rcon_value` here; the operator's own template does not,
  because that text is theirs.
* **A template is validated when it is loaded**, not when somebody runs it. An unknown placeholder was
  left in the rendered line by the classic and sent to the server as literal text — a command that
  looks configured and does nothing. Here the plugin refuses to register it and says which placeholder
  it did not recognise.
* **A name that collides is refused with the owner named.** The classic refused it too, but only
  reported the class name of the plugin that had it.
* **`EXACTNAME` and `NAME` mean the same thing**, because this bot keeps one name per player rather
  than a raw one and a colour-stripped one. Both spellings are accepted so an existing config keeps
  working.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from b3.core.commands import Command, CommandContext
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import match_names, sanitize_rcon_value
from b3.domain.client import Client
from b3.domain.permissions import level_for, max_group

log = logging.getLogger(__name__)

#: A command name: a letter, then letters/digits/underscores, at least two characters. The classic's
#: rule, and worth keeping — a one-character command collides with the aliases every plugin uses.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]+$", re.IGNORECASE)

#: Which fields of a player a template may ask for. `EXACTNAME` is accepted as a synonym of `NAME`
#: because this bot keeps one name per player, where the classic kept two.
PLAYER_FIELDS = ("PID", "GUID", "PBID", "NAME", "EXACTNAME", "B3ID")
GROUP_FIELDS = ("ADMINGROUP_SHORT", "ADMINGROUP_LONG", "ADMINGROUP_LEVEL")

ARG_RE = re.compile(r"<ARG>")
ARG_OPT_RE = re.compile(r"<ARG:OPT:(?P<default>[^>]*)>")
ARG_PLAYER_RE = re.compile(r"<ARG:FIND_PLAYER:(?P<field>[A-Z0-9_]+)>")
ARG_MAP_RE = re.compile(r"<ARG:FIND_MAP>")
SUBJECT_RE = re.compile(r"<(?P<subject>PLAYER|LAST_KILLER|LAST_VICTIM):(?P<field>[A-Z0-9_]+)>")
#: Anything shaped like a placeholder, so one nobody recognises can be refused rather than sent.
ANY_PLACEHOLDER_RE = re.compile(r"<[A-Z][A-Z0-9_:]*[^<>]*>")

MESSAGES = {
    "custom_needs_argument": "that command needs something after it",
    "custom_no_such_map": "no map like {map} — try {maps}",
    "custom_unknown_killer": "nobody has killed you yet",
    "custom_unknown_victim": "you have not killed anybody yet",
}


@dataclass(frozen=True, slots=True)
class Rendered:
    """The outcome of filling in a template: a command to send, or why it could not be."""

    command: str = ""
    refusal: str = ""


class CustomcommandsPlugin(Plugin):
    """Registers the commands an operator wrote in config, and renders their rcon lines."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        #: Command name -> the template it sends. Kept so `!plugin info` and the tests can see them.
        self.templates: dict[str, str] = {}

    # -- setup ---------------------------------------------------------------

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        # A kill is remembered against both players, which is the only reason this plugin watches
        # events at all: `<LAST_KILLER:...>` is what makes a revenge command possible.
        self.subscribe(EventType.CLIENT_KILL, self.on_kill)
        self.subscribe(EventType.CLIENT_KILL_TEAM, self.on_kill)
        self.load_commands()

    def load_commands(self) -> None:
        """Register every command in the config, refusing the ones that cannot work."""
        config = self.config if isinstance(self.config, dict) else {}
        groups = config.get("commands") or {}
        helps = {str(k).lower(): str(v) for k, v in (config.get("help") or {}).items()}
        if not isinstance(groups, dict) or not groups:
            log.warning("customcommands: no commands defined, so this plugin will do nothing")
            return
        for group_name, commands in groups.items():
            level = level_for(group_name)
            if level is None:
                log.error(
                    "customcommands: %r is not a group keyword or a level; its commands are ignored",
                    group_name,
                )
                continue
            if not isinstance(commands, dict):
                log.error(
                    "customcommands: commands.%s must be a mapping of name to line", group_name
                )
                continue
            for name, template in commands.items():
                self._register(str(name), str(template), level, helps)

    def _register(self, name: str, template: str, level: int, helps: dict[str, str]) -> None:
        wanted = name.strip().lower()
        problem = self._invalid(wanted, template)
        if problem is not None:
            log.error("customcommands: %s", problem)
            return
        self.templates[wanted] = template
        self.console.command_registry.register(
            Command(
                name=wanted,
                handler=self._handler_for(template),
                min_level=level,
                help=self._help_for(wanted, template, helps),
                plugin=self,
            )
        )
        log.info("customcommands: %s at level %d sends %r", wanted, level, template)

    def _invalid(self, name: str, template: str) -> str | None:
        """Why this command cannot be registered, or None. Checked now rather than when it is run.

        The classic validated the name and the argument count but not the *placeholders*, so a typo
        like `<PLAYER:CID>` was left in the rendered line and sent to the server as literal text —
        a command that looks configured, answers nothing and fails silently on every use.
        """
        if not NAME_RE.match(name):
            return (
                f"{name!r} is not a usable command name: it must start with a letter, be at least "
                "two characters long, and contain no spaces"
            )
        existing = self.console.command_registry.get(name)
        if existing is not None:
            owner = type(existing.plugin).__name__.replace("Plugin", "").lower() or "the core"
            return f"{name!r} is already a command, registered by {owner}"
        if not template.strip():
            return f"{name!r} has no command line to send"
        arguments = len(ARG_RE.findall(template) + ARG_OPT_RE.findall(template)) + len(
            ARG_PLAYER_RE.findall(template) + ARG_MAP_RE.findall(template)
        )
        if arguments > 1:
            return f"{name!r} asks for more than one argument, and a command can only take one"
        for found in ANY_PLACEHOLDER_RE.findall(template):
            if not self._known_placeholder(found):
                return f"{name!r} uses {found}, which is not a placeholder this bot knows"
        return None

    def _known_placeholder(self, text: str) -> bool:
        if ARG_RE.fullmatch(text) or ARG_OPT_RE.fullmatch(text) or ARG_MAP_RE.fullmatch(text):
            return True
        player = ARG_PLAYER_RE.fullmatch(text)
        if player is not None:
            return player["field"] in PLAYER_FIELDS
        subject = SUBJECT_RE.fullmatch(text)
        if subject is not None:
            if subject["subject"] == "PLAYER":
                return subject["field"] in PLAYER_FIELDS + GROUP_FIELDS
            return subject["field"] in PLAYER_FIELDS
        return False

    def _help_for(self, name: str, template: str, helps: dict[str, str]) -> str:
        """The help line, with the argument the template implies spelled out."""
        if ARG_PLAYER_RE.search(template):
            usage = f"{name} <player>"
        elif ARG_MAP_RE.search(template):
            usage = f"{name} <map>"
        elif ARG_OPT_RE.search(template):
            usage = f"{name} [<text>]"
        elif ARG_RE.search(template):
            usage = f"{name} <text>"
        else:
            usage = name
        described = helps.get(name)
        return f"{usage} - {described}" if described else usage

    def _handler_for(self, template: str) -> Callable[[CommandContext], None]:
        def handler(ctx: CommandContext) -> None:
            rendered = self.render(template, ctx)
            if rendered.refusal:
                ctx.reply(rendered.refusal)
                return
            if rendered.command:
                self.console.send_rcon(rendered.command)

        return handler

    # -- kills ---------------------------------------------------------------

    def on_kill(self, event: Event) -> None:
        killer, victim = event.client, event.target
        if killer is None or victim is None:
            return
        killer.set_var(self, "last_victim", victim)
        victim.set_var(self, "last_killer", killer)

    # -- rendering -----------------------------------------------------------

    def render(self, template: str, ctx: CommandContext) -> Rendered:
        """Fill in a template for this invocation, or say why it cannot be filled in.

        Everything substituted is sanitised, because all of it can come from a player: their own name,
        the name of whoever killed them, and the argument they typed. The classic pasted names in raw,
        which is how a player called ``bob"; quit`` could turn an admin's `!slap` into two commands.
        """
        command = template
        argument = ctx.args.strip()

        if ARG_RE.search(command):
            if not argument:
                return Rendered(refusal=self.message("custom_needs_argument"))
            command = ARG_RE.sub(_escaped(argument), command)

        optional = ARG_OPT_RE.search(command)
        if optional is not None:
            command = ARG_OPT_RE.sub(_escaped(argument or optional["default"]), command)

        player = ARG_PLAYER_RE.search(command)
        if player is not None:
            if not argument:
                return Rendered(refusal=self.message("custom_needs_argument"))
            target = self.resolve_client(ctx, argument)
            if target is None:
                return Rendered()  # `resolve_client` has already said why
            command = ARG_PLAYER_RE.sub(_escaped(self._field(target, player["field"])), command)

        if ARG_MAP_RE.search(command):
            if not argument:
                return Rendered(refusal=self.message("custom_needs_argument"))
            resolved = self._resolve_map(argument)
            if resolved is None:
                maps = ", ".join(self.console.get_maps()[:5]) or "nothing the server will list"
                return Rendered(refusal=self.message("custom_no_such_map", map=argument, maps=maps))
            command = ARG_MAP_RE.sub(_escaped(resolved), command)

        while True:
            subject = SUBJECT_RE.search(command)
            if subject is None:
                break
            who: Client | None = ctx.client
            if subject["subject"] == "LAST_KILLER":
                who = ctx.client.get_var(self, "last_killer")
                if who is None:
                    return Rendered(refusal=self.message("custom_unknown_killer"))
            elif subject["subject"] == "LAST_VICTIM":
                who = ctx.client.get_var(self, "last_victim")
                if who is None:
                    return Rendered(refusal=self.message("custom_unknown_victim"))
            command = command.replace(
                subject.group(0), _escaped(self._field(who, subject["field"]))
            )

        return Rendered(command=command.strip())

    def _field(self, client: Client | None, field: str) -> str:
        """One field of a player, as a template names it."""
        if client is None:
            return ""
        if field == "PID":
            return client.cid or ""
        if field == "GUID":
            return client.guid
        if field == "PBID":
            return client.pbid
        if field in ("NAME", "EXACTNAME"):
            return client.name
        if field == "B3ID":
            return f"@{client.id}" if client.id is not None else ""
        group = max_group(client.group_bits)
        if field == "ADMINGROUP_SHORT":
            return group.keyword if group else "guest"
        if field == "ADMINGROUP_LONG":
            return group.name if group else "Guest"
        if field == "ADMINGROUP_LEVEL":
            return str(group.level if group else 0)
        return ""  # unreachable: placeholders are validated at load time

    def _resolve_map(self, wanted: str) -> str | None:
        """A map the server has, from what the player typed. None when the rotation has no such map.

        Where the rotation cannot be read at all — several engines cannot be asked — what they typed
        is used as it is, which is all that can be done and matches how `!map` behaves there.
        """
        maps = self.console.get_maps()
        if not maps:
            return wanted
        found = match_names(wanted, [(m, self.console.map_display(m)) for m in maps])
        return found[0] if len(found) == 1 else None


def _escaped(value: str) -> str:
    """A value on its way into an rcon line. Never skipped — see `CustomcommandsPlugin.render`."""
    return sanitize_rcon_value(value)


__all__ = ["MESSAGES", "NAME_RE", "CustomcommandsPlugin", "Rendered"]
