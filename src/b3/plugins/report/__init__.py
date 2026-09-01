"""`!report` — a player telling the bot that somebody is worth looking at.

**New here.** The classic bot has no `!report`, and the gap shows on every server that runs one: the
way a player raises a cheater is to type it in chat and hope an admin is reading, and if none is in
the game nobody ever hears it. Every community solves this outside the bot — a Discord channel, a
forum thread, a screenshot — and then has to match "some guy called xX_sniper" to a player who left
twenty minutes ago. This plugin is the small part of that which belongs in the bot: it turns the
complaint into an event carrying the *identified* player, so whatever is listening — admins in the
game, `discord`, anything written later — gets a name, a database id and a reason rather than a line
of chat.

**A report is not a penalty.** Nothing happens to the player named. That is the whole design: the
moment a report does something on its own it becomes a weapon, and three players who dislike a fourth
can remove them. So this records, announces and relays; a human decides.

Four things it does that a chat line cannot, and each is a setting:

* **It cannot be spammed.** One report per `cooldown` seconds per reporter, and reporting the same
  player twice inside `duplicate_gap` answers "already passed on" without a second announcement.
* **It names one player.** The handle is resolved against the roster the same way `!ban` resolves it,
  so an ambiguous name is refused with the candidates rather than reported against the wrong person.
* **It reaches the admins who are here**, privately, at `admin_level` and above.
* **And the ones who are not**, through whatever relays `CLIENT_REPORT` — which today means
  `discord`.

**A reason is required** (`force_reason`, on by default). "SirReaDy is aimbotting" is something an
admin can act on an hour later; "SirReaDy" on its own is a name in a channel that somebody has to
come back and ask about, by which time the player has gone. An operator who would rather have a bare
report than no report can switch it off.
"""

from __future__ import annotations

import logging

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

DEFAULTS: dict[str, object] = {
    # Who may report. Guests included, on purpose: the player who notices a cheater first is usually
    # the one being shot by them, and asking them to register first loses the report.
    "level": 0,
    # Refuse a report with no reason. On, because the report is read by somebody who was not there:
    # "SirReaDy" on its own is a name in a channel that an admin has to come back and ask about, and
    # by then the player has gone. Turn it off on a server where a bare report is better than none.
    "force_reason": True,
    # Seconds one reporter must wait before their next report of anybody.
    "cooldown": 60,
    # Reporting the same player again within this many seconds is answered, not announced twice.
    "duplicate_gap": 300,
    # Tell the admins who are in the game, privately. They are the ones who can act now.
    "tell_admins": True,
    "admin_level": 20,
    # Say to the whole server that a report was made. Off: it tells the reported player they were
    # reported, which is how a reporter gets shot for the rest of the map.
    "announce": False,
}

MESSAGES = {
    "report_done": "thanks - {name} has been reported to the admins",
    "report_no_admins": "thanks - {name} has been reported; no admin is in the game, so it has been passed on",
    "report_to_admin": "{reporter} reported {name}: {reason}",
    "report_announce": "{name} has been reported",
    "report_self": "you cannot report yourself",
    "report_needs_reason": "say what they are doing: !report <player> <reason>",
    "report_too_soon": "you have just reported somebody - wait {seconds}s",
    "report_already": "{name} has already been reported",
    "report_no_reason": "no reason given",
}


class ReportPlugin(Plugin):
    """Lets a player report another, and puts the report where an admin will see it."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: Reporter database id -> when they last reported anybody.
        self._last_report: dict[int, float] = {}
        #: (reporter id, reported id) -> when. Keyed by both, so two players each reporting the same
        #: cheater both get through: it is the same player complaining twice that is noise.
        self._recent: dict[tuple[int, int], float] = {}

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        registered = self.console.command_registry.get("report")
        if registered is not None:  # pragma: no branch - registration is the framework's job
            registered.min_level = as_level(self.settings.get("level"), 0)

    # -- the command ---------------------------------------------------------

    @command(level=0)
    def cmd_report(self, ctx: CommandContext) -> None:
        """report <player> <reason> - tell the admins about a player"""
        tokens = ctx.arg_list()
        if not tokens:
            # The usage follows the setting rather than the docstring: an operator who has turned
            # `force_reason` off should not be told to type something the command does not want.
            required = self.settings.get("force_reason")
            ctx.reply(
                self.message(
                    "usage",
                    usage="report <player> <reason>" if required else "report <player> [reason]",
                )
            )
            return

        target = self.resolve_client(ctx, tokens[0])
        if target is None:
            return
        if target is ctx.client:
            ctx.reply(self.message("report_self"))
            return

        reason = " ".join(tokens[1:]).strip()
        if not reason and self.settings.get("force_reason"):
            ctx.reply(self.message("report_needs_reason"))
            return

        if not self._may_report(ctx, target):
            return

        self._remember(ctx.client, target)
        told = self._tell_admins(ctx.client, target, reason or self.message("report_no_reason"))
        # Published whether or not an admin saw it — the event is how it reaches the people who are
        # not here, which is the case this plugin exists for.
        self.console.bus.publish_soon(
            Event(EventType.CLIENT_REPORT, client=target, target=ctx.client, data=reason)
        )
        if self.settings.get("announce"):
            self.console.say(self.message("report_announce", name=target.name))
        ctx.reply(self.message("report_done" if told else "report_no_admins", name=target.name))

    # -- the guards ----------------------------------------------------------

    def _may_report(self, ctx: CommandContext, target: Client) -> bool:
        """Cooldown and duplicate checks, each answering the reporter rather than going quiet."""
        reporter_id, target_id = ctx.client.id, target.id
        if reporter_id is None or target_id is None:
            return True  # nothing to key a cooldown on; a report is better than a refusal
        now = self.console.clock.now()

        cooldown = as_int(self.settings.get("cooldown"), 60)
        last = self._last_report.get(reporter_id)
        if last is not None and now - last < cooldown:
            ctx.reply(self.message("report_too_soon", seconds=int(cooldown - (now - last))))
            return False

        gap = as_int(self.settings.get("duplicate_gap"), 300)
        seen = self._recent.get((reporter_id, target_id))
        if seen is not None and now - seen < gap:
            ctx.reply(self.message("report_already", name=target.name))
            return False
        return True

    def _remember(self, reporter: Client, target: Client) -> None:
        now = self.console.clock.now()
        if reporter.id is not None:
            self._last_report[reporter.id] = now
            if target.id is not None:
                self._recent[(reporter.id, target.id)] = now

    def _tell_admins(self, reporter: Client, target: Client, reason: str) -> bool:
        """Privately, to every admin in the game. True if anybody was there to tell."""
        if not self.settings.get("tell_admins"):
            return False
        level = as_level(self.settings.get("admin_level"), 20)
        text = self.message(
            "report_to_admin", reporter=reporter.name, name=target.name, reason=reason
        )
        told = False
        for client in self.console.clients.connected():
            if client is reporter or client.max_level() < level:
                continue
            self.console.tell(client, text)
            told = True
        return told


__all__ = ["DEFAULTS", "MESSAGES", "ReportPlugin"]
