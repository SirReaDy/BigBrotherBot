"""A Call of Duty admin mod's own commands, reachable as bot commands.

A port of the classic `codam` plugin. CoDAM takes its instructions through a single rcon verb —
`command "<line>"` — and this plugin turns the mod's verbs into ordinary bot commands, so that an
admin types `!ckick bob` in game instead of somebody opening an rcon console. The bot cannot ask the
mod what it understands, so the operator writes the verb list down; everything else follows from that.

Kept from the classic: the `c` prefix on every configured verb, which is what keeps the mod's `kick`
from colliding with the bot's own `!kick`; the split between verbs that name a player and verbs that
do not; and the raw `!codam <line>` passthrough for anything nobody configured.

Changed, and the first of these is the reason the plugin never worked:

* **The mod was sent the bot's own prefix.** A verb that named no player was registered as
  `c` + verb and then sent to the mod as `cmd.command` — the *registered* name — so CoDAM was asked
  for `crestart` rather than `restart`, and every verb in that half of the config was an unknown
  command. The other half stripped the prefix (`cmd.command[1:]`), which is what proves the intent.
  Nothing raised: an admin mod that does not recognise a command says so to the rcon caller, and the
  classic dropped that reply on the floor.
* **It shipped no configuration at all.** There is no `plugin_codam.ini` in the classic tree and no
  mention of the plugin in its distribution config, not even a commented-out one. Since the whole
  command surface comes from config, a `codam` that loaded successfully registered nothing. There is
  an example config here, and a plugin that finds no verbs configured says so at startup.
* **The player was put last, after the free text.** The classic built `<verb> <text> <slot>`, so
  `!ckick 3 spamming` reached the mod as `kick spamming 3` and it read the reason as the player.
  Every Call of Duty verb that names a player takes the slot first — `kick`, `banclient`,
  `tempbanclient` — and so does the mod; the slot goes first here.
* **Nothing was escaped, and the line is a quoted argument.** `command "%s"` with a `"` in a reason
  closes the argument early, and a `;` after it starts a second command. Everything an admin types
  goes through `sanitize_rcon_value`.
* **The admin was told nothing.** The mod's reply was discarded, so `!ckick 3` looked exactly the
  same whether the mod ran it, did not recognise the verb, or was not installed. What the mod says
  comes back to whoever typed the command; where it says nothing, the bot confirms what it sent.
* **A verb that names a player now checks whose player it is.** `!ckick` on a fellow admin was
  allowed, where the bot's own `!kick` is not. Same rule as `admin` applies: not yourself, and not
  your equals and betters.
* **A player is resolved the way every other command resolves one.** The classic required the handle
  to be two characters or all digits, so a one-letter name could not be named at all.
* **It is gated to the Call of Duty titles.** The classic plugin loaded on any game, and the verb it
  sends exists on none of the others — another set of commands that answered nothing. An operator who
  enables it elsewhere is told which titles it is for.

The mod's verb list is the operator's to write, and this plugin deliberately knows nothing about it:
whichever build of whichever admin mod is installed, the config says what to offer. A verb whose
arguments do not fit either shape here belongs in `customcommands`, which sends a line of the
operator's own composition.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from b3.core.commands import Command, CommandContext, command
from b3.core.console import Console
from b3.core.plugin import Plugin
from b3.core.util import sanitize_rcon_value
from b3.domain.client import Client
from b3.domain.permissions import level_for
from b3.parsers.cod import profiles as cod_profiles

log = logging.getLogger(__name__)

#: What a configured verb is called in game: the mod's verb with a `c` in front. The classic's own
#: convention, and the reason `!ckick` can exist on a bot that already has `!kick`.
PREFIX = "c"

#: A verb the mod could plausibly have. Restricted because it is pasted into an rcon line: an
#: operator's typo becomes an unknown command at worst rather than an argument of its own.
VERB_RE = re.compile(r"^[a-z][a-z0-9_]*$", re.IGNORECASE)

#: Every Call of Duty title. The `command` verb belongs to an admin mod for this engine, so the
#: mistake worth catching is this plugin enabled on a game that has no such mod at all. Which of
#: these titles actually has one installed is not something the bot can ask, so the gate is the
#: family rather than the two the mod originally shipped for.
COD_TITLES: tuple[str, ...] = tuple(sorted(cod_profiles.ALL))

MESSAGES = {
    "codam_needs_command": "name the admin-mod command to send — see !help codam",
    "codam_sent": "sent {command} to the admin mod",
    "codam_no_slot": "the server has given {name} no slot number, so the mod cannot be told",
}


@dataclass(frozen=True, slots=True)
class Verb:
    """One of the mod's commands, as this plugin offers it."""

    name: str  # what an admin types, prefix included
    verb: str  # what the mod is sent
    takes_player: bool


class CodamPlugin(Plugin):
    """Registers the admin-mod verbs an operator listed, and hands their lines to the mod."""

    requires_plugins = ("admin",)
    requires_parsers = COD_TITLES

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        #: Command name -> the verb behind it. Kept so `!plugin info` and the tests can see them.
        self.verbs: dict[str, Verb] = {}

    # -- setup ---------------------------------------------------------------

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.load_verbs()

    def load_verbs(self) -> None:
        """Register every verb the config lists, refusing the ones that cannot work."""
        config = self.config if isinstance(self.config, dict) else {}
        for section, takes_player in (("commands", False), ("player_commands", True)):
            self._load_section(config.get(section), section, takes_player)
        if not self.verbs:
            log.warning(
                "codam: no admin-mod commands are configured, so !codam is all this plugin offers"
            )
            return
        log.info("codam: %d admin-mod command(s) registered", len(self.verbs))

    def _load_section(self, section: object, where: str, takes_player: bool) -> None:
        if section is None:
            return
        if not isinstance(section, dict):
            log.error("codam: %s must be a mapping of level to a list of the mod's commands", where)
            return
        for group_name, listed in section.items():
            level = level_for(group_name)
            if level is None:
                log.error(
                    "codam: %r in %s is neither a group keyword nor a level 0-100; its commands "
                    "are ignored",
                    group_name,
                    where,
                )
                continue
            verbs = _as_list(listed)
            if verbs is None:
                log.error("codam: %s.%s must be a list of the mod's commands", where, group_name)
                continue
            for verb in verbs:
                self._register(verb, level, takes_player, where)

    def _register(self, verb: str, level: int, takes_player: bool, where: str) -> None:
        wanted = verb.strip().lower()
        if not VERB_RE.match(wanted):
            log.error("codam: %r in %s is not a command name an admin mod could have", verb, where)
            return
        name = PREFIX + wanted
        existing = self.console.command_registry.get(name)
        if existing is not None:
            owner = type(existing.plugin).__name__.replace("Plugin", "").lower() or "the core"
            log.error(
                "codam: %r is already a command, registered by %s, so the mod's %r is not offered",
                name,
                owner,
                wanted,
            )
            return
        entry = Verb(name=name, verb=wanted, takes_player=takes_player)
        self.verbs[name] = entry
        self.console.command_registry.register(
            Command(
                name=name,
                handler=self._handler_for(entry),
                min_level=level,
                help=self._help_for(entry),
                plugin=self,
            )
        )
        log.info("codam: %s at level %d sends the mod's %r", name, level, wanted)

    def _help_for(self, verb: Verb) -> str:
        if verb.takes_player:
            return f"{verb.name} <player> [<text>] - the admin mod's {verb.verb}"
        return f"{verb.name} [<text>] - the admin mod's {verb.verb}"

    def _handler_for(self, verb: Verb) -> Callable[[CommandContext], None]:
        def handler(ctx: CommandContext) -> None:
            if verb.takes_player:
                self._about_a_player(ctx, verb)
            else:
                self.send(ctx, verb.verb, ctx.args)

        return handler

    # -- the passthrough -----------------------------------------------------

    @command("codam", level=100)
    def cmd_codam(self, ctx: CommandContext) -> None:
        """codam <command> - send a line to the admin mod as it is written"""
        line = ctx.args.strip()
        if not line:
            ctx.reply(self.message("codam_needs_command"))
            return
        self.send(ctx, "", line)

    # -- sending -------------------------------------------------------------

    def _about_a_player(self, ctx: CommandContext, verb: Verb) -> None:
        """A verb that names a player: the slot first, then whatever else was typed."""
        tokens = ctx.args.split(None, 1)
        if not tokens:
            ctx.reply(self.message("usage", usage=f"{verb.name} <player> [<text>]"))
            return
        target = self.resolve_client(ctx, tokens[0])
        if target is None:
            return  # `resolve_client` has already said why
        if not self._may_act_on(ctx, target):
            return
        if not target.cid:
            ctx.reply(self.message("codam_no_slot", name=target.name))
            return
        rest = tokens[1] if len(tokens) > 1 else ""
        self.send(ctx, verb.verb, f"{target.cid} {rest}")

    def send(self, ctx: CommandContext, verb: str, rest: str) -> None:
        """Hand one line to the mod, and tell whoever asked what came back.

        The line is a quoted rcon argument, so everything going into it is escaped: the classic's
        `command "%s"` would end the argument on the first `"` an admin typed and take whatever
        followed a `;` as a second command.
        """
        line = " ".join(part for part in (verb, sanitize_rcon_value(rest)) if part)
        answer = sanitize_rcon_value(self.console.send_rcon(f'command "{line}"'))
        ctx.reply(answer or self.message("codam_sent", command=line))

    # -- who may be acted on -------------------------------------------------

    def _may_act_on(self, ctx: CommandContext, target: Client) -> bool:
        """`admin`'s rule, applied to a family of verbs the classic left unguarded.

        These are the mod's own kicks and bans; the classic checked nothing, so `!ckick` reached
        past a level the bot's own `!kick` refuses to touch.
        """
        if target is ctx.client or (target.id is not None and target.id == ctx.client.id):
            ctx.reply(self.message("action_denied_self"))
            return False
        if target.max_level() >= ctx.client.max_level():
            key = "action_denied_masked" if target.is_masked() else "action_denied_level"
            ctx.reply(self.message(key, name=target.name))
            return False
        return True


def _as_list(value: object) -> list[str] | None:
    """The verbs under one level. A bare string is one verb, which is how an operator writes one."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return None


__all__ = ["COD_TITLES", "MESSAGES", "PREFIX", "VERB_RE", "CodamPlugin", "Verb"]
