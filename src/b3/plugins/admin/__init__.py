"""Admin plugin — the standard moderation commands.

In the classic bot this 2,596-line plugin *was* the command framework. Here it is a thin consumer of
the core command service: it just declares command handlers with :func:`b3.core.commands.command`.
The framework (registration, parsing, permission checks) lives in ``b3.core.commands``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.core.plugin import Plugin
from b3.core.util import duration_text, match_names, parse_duration
from b3.domain.client import Client, NEVER_EXPIRES, Penalty, PenaltyType
from b3.domain.permissions import DEFAULT_GROUPS, Group, find_group, group_by_keyword, max_group

log = logging.getLogger(__name__)

#: Rotations up to this length are printed in full when `!map` finds no match; longer ones are not.
MAPS_WORTH_LISTING = 12

#: The `settings:` section of the plugin config — the classic bot's defaults, verbatim.
DEFAULT_SETTINGS: dict[str, object] = {
    # `!ban` is a 14-day tempban, as it was classically; `!permban` is the permanent one.
    # Set 0 to make `!ban` permanent instead.
    "ban_duration": "14d",
    "admins_level": 20,  # the level `!admins` lists from
    "noreason_level": 100,  # below this level a reason is compulsory; 0 makes it optional
    "long_tempban_level": 80,  # below this, tempbans are capped...
    "long_tempban_max_duration": "3h",  # ...at this
    "announce_registration": True,
}

#: `!rules` reads these keys from `spamages`, in order — the legacy convention.
RULE_KEYS = tuple(f"rule{n}" for n in range(1, 21))

#: Ways to poke someone, picked round-robin so the bot does not repeat itself.
POKES = ("wake up", "*poke*", "attention", "get up", "move out")

#: The `warn:` section of the plugin config, with the classic bot's defaults.
DEFAULT_WARN_SETTINGS: dict[str, object] = {
    "delay": 15,  # seconds before the same player may be warned again
    "alert_at": 3,  # warnings that trigger the public "you are about to be kicked" alert
    "grace": 25,  # seconds an admin then has to `!clear` them before it happens
    "kick_at": 5,  # warnings that tempban immediately, with no alert
    "tempban_at": 6,  # above this many, use the flat tempban_duration instead of the sum
    "tempban_duration": "1d",
    "max_duration": "1d",  # ceiling on a tempban computed from warning durations
    "duration_divider": 30,  # total warning minutes / this = the tempban length
    "pm_global": False,  # True: tell the player and the admin instead of announcing
}


class AdminPlugin(Plugin):
    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self._groups: tuple[Group, ...] | None = None
        # Populated by on_load_config; safe defaults so the plugin works with no config file.
        self.settings: dict[str, object] = dict(DEFAULT_SETTINGS)
        self.ban_duration = 0  # minutes; 0 == a permanent !ban
        self.spamages: dict[str, str] = {}
        self.warn_reasons: dict[str, str] = {}
        self.warn_settings: dict[str, object] = dict(DEFAULT_WARN_SETTINGS)
        self._poke_index = 0
        # client id -> (deadline epoch, admin) for the alert grace period; see _escalate.
        self._pending_kicks: dict[int, tuple[float, Client | None]] = {}

    def on_load_config(self) -> None:
        """Read the optional plugin config: settings, spam messages and warning reasons.

        Everything here is optional — an operator with no admin config file gets working commands
        and empty `!rules`/`!spams`, rather than a plugin that refuses to start.
        """
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULT_SETTINGS, **(config.get("settings") or {})}
        raw_duration = self.settings.get("ban_duration", 0)
        try:
            self.ban_duration = parse_duration(str(raw_duration)) if raw_duration else 0
        except ValueError:
            log.warning("admin: invalid ban_duration %r; !ban stays permanent", raw_duration)
            self.ban_duration = 0
        self.spamages = {str(k).lower(): str(v) for k, v in (config.get("spamages") or {}).items()}
        self.warn_reasons = {
            str(k).lower(): str(v) for k, v in (config.get("warn_reasons") or {}).items()
        }
        self.warn_settings = {**DEFAULT_WARN_SETTINGS, **(config.get("warn") or {})}

    def on_startup(self) -> None:
        # Escalation hangs off the event, not off `!warn`, so a warning issued by *any* plugin
        # counts towards a kick. In the classic bot every plugin had to call the admin plugin's
        # `warnClient` by hand to get this.
        self.subscribe(EventType.CLIENT_WARN, self._on_warned)
        # The alert gives an admin a few seconds to clear the warnings before the kick lands.
        # A per-second schedule replaces the legacy threading.Timer: no thread, and testable.
        self.schedule(self._check_pending_kicks, second="*", name="AdminPlugin.warn_kicks")

    # -- warning escalation --------------------------------------------------

    def _warn_setting(self, key: str) -> int:
        """A `warn:` setting as a number, tolerating durations like ``1d`` from the config."""
        value = self.warn_settings.get(key, DEFAULT_WARN_SETTINGS.get(key))
        try:
            return parse_duration(str(value))
        except ValueError:
            log.warning("admin: invalid warn.%s %r; using the default", key, value)
            return parse_duration(str(DEFAULT_WARN_SETTINGS.get(key, 0)))

    def warning_count(self, client: Client) -> int:
        if client.id is None:
            return 0
        return len(self.console.storage.get_active_penalties(client.id, PenaltyType.WARNING))

    def _on_warned(self, event: Event) -> None:
        """Announce a warning and escalate if the player has collected too many."""
        client = event.client
        if client is None or client.id is None:
            return
        count = self.warning_count(client)
        reason = str(event.data or self.message("no_reason_given"))
        announcement = self.message("warn_announce", count=count, name=client.name, reason=reason)
        if self.warn_settings.get("pm_global"):
            self.console.tell(client, announcement)
        else:
            self.console.say(announcement)
        self._escalate(client, count, reason)

    def _escalate(self, client: Client, count: int, reason: str) -> None:
        if count >= self._warn_setting("kick_at"):
            self._warn_tempban(client, reason)
            return
        if count >= self._warn_setting("alert_at") and client.id not in self._pending_kicks:
            grace = self._warn_setting("grace")
            self.console.say(
                self.message(
                    "warn_alert",
                    name=client.name,
                    count=count,
                    reason=reason,
                    seconds=grace,
                )
            )
            # Recorded against the clock, checked by the schedule below: an admin who clears the
            # warnings inside the grace window cancels the kick simply by making the count drop.
            self._pending_kicks[client.require_id()] = (self.console.clock.now() + grace, None)

    def _check_pending_kicks(self) -> None:
        """Carry out (or quietly drop) the kicks the alert promised."""
        now = self.console.clock.now()
        for client_id, (deadline, _admin) in list(self._pending_kicks.items()):
            if now < deadline:
                continue
            del self._pending_kicks[client_id]
            # Prefer the live object (it has a slot id, so the tempban can reach the server).
            client = next(
                (c for c in self.console.clients.connected() if c.id == client_id),
                self.console.storage.get_client_by_id(client_id),
            )
            if client is None:
                continue
            count = self.warning_count(client)
            if count >= self._warn_setting("alert_at"):
                self._warn_tempban(client, self._latest_warning_reason(client))

    def _latest_warning_reason(self, client: Client) -> str:
        """What they were last warned for — the ban should say, not just 'too many warnings'."""
        if client.id is None:
            return self.message("warn_too_many")
        warnings = self.console.storage.get_active_penalties(client.id, PenaltyType.WARNING)
        return (warnings[0].reason if warnings else "") or self.message("warn_too_many")

    def _warn_tempban(self, client: Client, reason: str) -> None:
        """Tempban for collecting warnings. Length grows with what they were warned *for*."""
        minutes = self._warn_kick_duration(client)
        if minutes <= 0:
            return
        self.console.tempban(
            client, minutes, reason=self.message("warn_tempban_reason", reason=reason)
        )

    def _warn_kick_duration(self, client: Client) -> int:
        """Legacy `warnKickDuration`: the summed warning time, divided down and capped.

        Past `tempban_at` warnings it stops scaling and applies the flat `tempban_duration` — at
        that point the player is not being nudged any more.
        """
        if client.id is None:
            return 0
        warnings = self.console.storage.get_active_penalties(client.id, PenaltyType.WARNING)
        if len(warnings) > self._warn_setting("tempban_at"):
            return self._warn_setting("tempban_duration")
        divider = max(1, self._warn_setting("duration_divider"))
        total = sum(w.duration for w in warnings)
        return max(1, min(total // divider, self._warn_setting("max_duration")))

    # -- helpers -----------------------------------------------------------

    def _setting(self, key: str) -> int:
        """A numeric `settings:` value, tolerating durations like ``3h``."""
        value = self.settings.get(key, DEFAULT_SETTINGS.get(key))
        try:
            return parse_duration(str(value))
        except ValueError:
            log.warning("admin: invalid settings.%s %r; using the default", key, value)
            return parse_duration(str(DEFAULT_SETTINGS.get(key, 0)))

    def _require_reason(self, ctx: CommandContext, reason: str) -> bool:
        """Enforce `noreason_level`: junior admins must say why they punished someone."""
        if reason.strip() or ctx.client.max_level(self.groups) >= self._setting("noreason_level"):
            return True
        ctx.reply(self.message("reason_required"))
        return False

    def resolve_reason(self, reason: str) -> tuple[int, str]:
        """Expand a one-word reason through `warn_reasons` — the legacy ``getReason``.

        A keyword maps to ``"<duration>, <text>"``, to plain text, or to another entry via
        ``/other`` or ``/spam#key``. Returns ``(minutes, text)``; minutes is 0 when the entry
        names no duration, and an unknown keyword is simply used as the reason it looks like.
        """
        key = reason.strip().lower()
        if not key or " " in key:
            return 0, reason.strip()

        value, seen = self.warn_reasons.get(key), {key}
        while value is not None and value.startswith("/"):
            ref = value[1:].strip().lower()
            if ref.startswith("spam#"):
                return 0, self.spamages.get(ref[5:], reason)
            if ref in seen:  # a config that points at itself must not hang the bot
                log.warning("admin: warn_reasons loop at %r", ref)
                value = None
                break
            seen.add(ref)
            value = self.warn_reasons.get(ref)
        if value is None:
            return 0, reason.strip()

        head, sep, tail = value.partition(",")
        if sep:
            try:
                return parse_duration(head), tail.strip()
            except ValueError:
                pass
        return 0, value.strip()

    def _matching(self, ctx: CommandContext, pattern: str) -> list[Client]:
        """Connected players matching a name fragment — the target set of the `*all` commands.

        Never includes the caller or anyone they may not act on, so a bulk command cannot be used
        to reach someone a single command would refuse.
        """
        needle = pattern.strip().lower()
        if not needle:
            return []
        return [
            c
            for c in self.console.clients.connected()
            if needle in c.name.lower()
            and c is not ctx.client
            and (c.id is None or c.id != ctx.client.id)
            and c.max_level(self.groups) < ctx.client.max_level(self.groups)
        ]

    @property
    def groups(self) -> tuple[Group, ...]:
        """The group table, read once from storage.

        Levels and keywords come from the database rather than the in-code defaults, so an operator
        who renamed or re-levelled a group gets what they configured. There is no command that edits
        groups at runtime, so caching it for the process lifetime is safe.
        """
        if self._groups is None:
            get_groups = getattr(self.console.storage, "get_groups", None)
            self._groups = tuple(get_groups()) if get_groups else DEFAULT_GROUPS
        return self._groups

    def _find_group(self, ctx: CommandContext, keyword: str) -> Group | None:
        group = find_group(keyword, self.groups)
        if group is None:
            known = ", ".join(g.keyword for g in sorted(self.groups, key=lambda g: -g.level))
            ctx.reply(self.message("group_unknown", group=keyword, known=known))
        return group

    def _may_act_on(self, ctx: CommandContext, target: Client) -> bool:
        """The classic bot's rule: you may not act on yourself, or on your equals and betters.

        Legacy applied this to `!kick`/`!ban`/`!tempban`/`!warn` but *not* to the group commands,
        which left a senioradmin able to `!putgroup` a superadmin down to `user`. Applying it
        uniformly closes that hole. Note it binds superadmins too — as it does in the classic bot —
        so one level-100 admin cannot demote or ban another.
        """
        admin = ctx.client
        if target is admin or (target.id is not None and target.id == admin.id):
            ctx.reply(self.message("action_denied_self"))
            return False
        if target.max_level(self.groups) >= admin.max_level(self.groups):
            key = "action_denied_masked" if target.is_masked() else "action_denied_level"
            ctx.reply(self.message(key, name=target.name))
            return False
        return True

    def _target_and_reason(self, ctx: CommandContext) -> tuple[Client | None, str]:
        tokens = ctx.arg_list()
        if not tokens:
            ctx.reply(self.message("usage", usage=f"{ctx.command.name} <player> [reason]"))
            return None, ""
        target = self.resolve_client(ctx, tokens[0])
        if target is None:
            return None, ""
        return target, " ".join(tokens[1:])

    def _stored_target(self, ctx: CommandContext, usage: str = "") -> tuple[Client | None, str]:
        """Resolve a target that may be *offline*, by @dbid, current name or a past alias."""
        tokens = ctx.arg_list()
        if not tokens:
            ctx.reply(self.message("usage", usage=usage or f"{ctx.command.name} <player> [reason]"))
            return None, ""
        found = self.console.lookup_clients(tokens[0])
        if not found:
            ctx.reply(self.message("stored_player_not_found", handle=tokens[0]))
            return None, ""
        if len(found) > 1:
            listed = ", ".join(f"@{c.id} {c.name}" for c in found)
            ctx.reply(self.message("ambiguous_target", count=len(found), candidates=listed))
            return None, ""
        return found[0], " ".join(tokens[1:])

    # -- commands ----------------------------------------------------------

    @command(level=40, alias="k")
    def cmd_kick(self, ctx: CommandContext) -> None:
        """kick <player> [reason] - remove a player from the server"""
        target, reason = self._target_and_reason(ctx)
        if target is None or not self._may_act_on(ctx, target):
            return
        if not self._require_reason(ctx, reason):
            return
        _, reason = self.resolve_reason(reason)
        self.console.kick(target, reason=reason, admin=ctx.client)
        ctx.reply(self._penalty_reply("kicked", target, reason))

    @command(level=60, alias="b")
    def cmd_ban(self, ctx: CommandContext) -> None:
        """ban <player> [reason] - ban a player (permanent unless ban_duration is configured)"""
        target, reason = self._target_and_reason(ctx)
        if target is None or not self._may_act_on(ctx, target):
            return
        if not self._require_reason(ctx, reason):
            return
        _, reason = self.resolve_reason(reason)
        self._ban(ctx, target, reason)

    @command(level=80, alias="pb")
    def cmd_permban(self, ctx: CommandContext) -> None:
        """permban <player> [reason] - ban a player permanently, whatever ban_duration says"""
        target, reason = self._target_and_reason(ctx)
        if target is None or not self._may_act_on(ctx, target):
            return
        if not self._require_reason(ctx, reason):
            return
        _, reason = self.resolve_reason(reason)
        self.console.ban(target, reason=reason, admin=ctx.client)
        ctx.reply(self._penalty_reply("banned", target, reason))

    def _ban(self, ctx: CommandContext, target: Client, reason: str) -> None:
        """`!ban` bans for `ban_duration` — 14 days by default, as in the classic bot.

        Worth knowing if you are new to B3: `!ban` is *temporary* out of the box and `!permban` is
        the permanent one. Set `ban_duration: 0` in the admin config to make `!ban` permanent.
        """
        if self.ban_duration:
            self.console.tempban(target, self.ban_duration, reason=reason, admin=ctx.client)
            ctx.reply(self._tempban_reply(target, self.ban_duration))
            return
        self.console.ban(target, reason=reason, admin=ctx.client)
        ctx.reply(self._penalty_reply("banned", target, reason))

    @command(level=40, alias="tb")
    def cmd_tempban(self, ctx: CommandContext) -> None:
        """tempban <player> <duration> [reason] - ban for a limited time (e.g. 30m, 2h, 1d)"""
        tokens = ctx.arg_list()
        if len(tokens) < 2:
            ctx.reply(self.message("usage", usage="tempban <player> <duration> [reason]"))
            return
        target = self.resolve_client(ctx, tokens[0])
        if target is None or not self._may_act_on(ctx, target):
            return
        try:
            minutes = parse_duration(tokens[1])
        except ValueError:
            ctx.reply(self.message("invalid_duration", value=tokens[1]))
            return
        # A junior admin should not be able to hand out a ten-year "temporary" ban.
        cap = self._setting("long_tempban_max_duration")
        if minutes > cap and ctx.client.max_level(self.groups) < self._setting(
            "long_tempban_level"
        ):
            ctx.reply(self.message("tempban_too_long", limit=duration_text(cap)))
            return
        if not self._require_reason(ctx, " ".join(tokens[2:])):
            return
        _, reason = self.resolve_reason(" ".join(tokens[2:]))
        self.console.tempban(target, minutes, reason=reason, admin=ctx.client)
        ctx.reply(self._tempban_reply(target, minutes))

    @command(level=20, alias="w")
    def cmd_warn(self, ctx: CommandContext) -> None:
        """warn <player> [reason] - issue a warning (a keyword from warn_reasons also sets its life)"""
        target, reason = self._target_and_reason(ctx)
        if target is None or not self._may_act_on(ctx, target):
            return
        # Rate limit, as the classic bot did: warnings arrive in threes when admins pile on, and
        # the third one is what triggers an automatic kick.
        delay = self._warn_setting("delay")
        last = target.get_var(self, "last_warn", 0.0)
        now = self.console.clock.now()
        if delay and now - last < delay:
            ctx.reply(self.message("warn_too_fast", seconds=delay))
            return
        target.set_var(self, "last_warn", now)
        minutes, reason = self.resolve_reason(reason)
        self.console.warn(target, reason=reason, admin=ctx.client, minutes=minutes)
        ctx.reply(self._penalty_reply("warned", target, reason))

    @command(level=60, alias="sp")
    def cmd_spank(self, ctx: CommandContext) -> None:
        """spank <player> [reason] - kick a player, loudly"""
        target, reason = self._target_and_reason(ctx)
        if target is None or not self._may_act_on(ctx, target):
            return
        if not self._require_reason(ctx, reason):
            return
        _, reason = self.resolve_reason(reason)
        self.console.say(self.message("spanked", name=target.name, admin=ctx.client.name))
        self.console.kick(target, reason=reason, admin=ctx.client)

    # -- penalty inspection / lifting --------------------------------------

    @command(level=60)
    def cmd_unban(self, ctx: CommandContext) -> None:
        """unban <player> [reason] - lift every active ban on a player (works offline, by @id)"""
        target, reason = self._stored_target(ctx)
        if target is None:
            return
        bans = self.console.storage.get_active_penalties(target.require_id(), PenaltyType.BAN)
        bans += self.console.storage.get_active_penalties(target.require_id(), PenaltyType.TEMPBAN)
        if not bans:
            ctx.reply(self.message("no_active_ban", name=target.name))
            return
        self.console.unban(target, reason=reason, admin=ctx.client)
        noun = "penalty" if len(bans) == 1 else "penalties"
        ctx.reply(self.message("unbanned", name=target.name, count=len(bans), noun=noun))

    @command(level=40, alias="bi")
    def cmd_baninfo(self, ctx: CommandContext) -> None:
        """baninfo <player> - show the ban currently in force on a player"""
        target, _ = self._stored_target(ctx)
        if target is None:
            return
        for type_ in (PenaltyType.BAN, PenaltyType.TEMPBAN):
            penalties = self.console.storage.get_active_penalties(target.require_id(), type_)
            if penalties:
                details = self._describe_penalty(penalties[0])
                ctx.reply(self.message("baninfo", name=target.name, details=details))
                return
        ctx.reply(self.message("baninfo_none", name=target.name))

    @command(level=20)
    def cmd_warns(self, ctx: CommandContext) -> None:
        """warns <player> - list a player's active warnings"""
        target, _ = self._stored_target(ctx)
        if target is None:
            return
        warnings = self.console.storage.get_active_penalties(
            target.require_id(), PenaltyType.WARNING
        )
        if not warnings:
            ctx.reply(self.message("warns_none", name=target.name))
            return
        no_reason = self.message("no_reason_given")
        reasons = ", ".join(w.reason or no_reason for w in warnings[:5])
        ctx.reply(self.message("warns", name=target.name, count=len(warnings), reasons=reasons))

    @command(level=80, alias="wc")
    def cmd_warnclear(self, ctx: CommandContext) -> None:
        """warnclear <player> - clear a player's active warnings"""
        target, _ = self._stored_target(ctx)
        if target is None:
            return
        cleared = self.console.storage.disable_penalties(target.require_id(), PenaltyType.WARNING)
        ctx.reply(self.message("warns_cleared", count=cleared, name=target.name))

    # -- identity history --------------------------------------------------

    @command(level=20, alias="alias")
    def cmd_aliases(self, ctx: CommandContext) -> None:
        """aliases <player> - list the other names a player has used"""
        target, _ = self._stored_target(ctx)
        if target is None:
            return
        others = [
            a.value
            for a in self.console.storage.get_aliases(target.require_id())
            if a.value != target.name
        ]
        if not others:
            ctx.reply(self.message("aliases_none", name=target.name))
            return
        ctx.reply(self.message("aliases", name=target.name, aliases=", ".join(others[:10])))

    @command(level=80)
    def cmd_clientinfo(self, ctx: CommandContext) -> None:
        """clientinfo <player> - show a player's stored identity, level and history"""
        target, _ = self._stored_target(ctx)
        if target is None:
            return
        aliases = self.console.storage.get_aliases(target.require_id())
        ips = self.console.storage.get_ip_aliases(target.require_id())
        ctx.reply(
            self.message(
                "clientinfo",
                id=target.id,
                name=target.name,
                guid=target.guid or "?",
                ip=target.ip or "?",
                level=target.max_level(self.groups),
                connections=target.connections,
                aliases=len(aliases),
                ips=len(ips),
            )
        )

    @command(level=20)
    def cmd_admins(self, ctx: CommandContext) -> None:
        """admins - list the admins currently connected"""
        # Masked admins are listed at — and filtered by — their masked level, which is the whole
        # point of `!mask`: a superadmin masked as a user does not show up here at all.
        admins = sorted(
            (
                c
                for c in self.console.clients.connected()
                if c.display_level(self.groups) >= self._setting("admins_level")
            ),
            key=lambda c: c.display_level(self.groups),
            reverse=True,
        )
        if not admins:
            ctx.reply(self.message("admins_none"))
            return
        listed = ", ".join(f"{c.name} [{c.display_level(self.groups)}]" for c in admins)
        ctx.reply(self.message("admins_online", admins=listed))

    # -- group management ---------------------------------------------------

    @command(level=80)
    def cmd_putgroup(self, ctx: CommandContext) -> None:
        """putgroup <player> <group> - put a player in a group (replaces their current one)"""
        target, keyword = self._stored_target(ctx, usage="putgroup <player> <group>")
        if target is None:
            return
        if not keyword:
            ctx.reply(self.message("usage", usage="putgroup <player> <group>"))
            return
        group = self._find_group(ctx, keyword)
        if group is None:
            return
        # Legacy rule, kept: you cannot hand out a group at or above your own level. Only a
        # superadmin can create another admin of equal standing.
        level = ctx.client.max_level(self.groups)
        if group.level >= level and level < 100:
            ctx.reply(self.message("group_beyond_reach", group=group.name))
            return
        if not self._may_act_on(ctx, target):
            return
        if target.in_group(group):
            ctx.reply(self.message("group_already_in", name=target.name, group=group.name))
            return
        self._save_group(target, group)
        ctx.reply(self.message("group_put", name=target.name, group=group.name))

    @command(level=80)
    def cmd_ungroup(self, ctx: CommandContext) -> None:
        """ungroup <player> <group> - remove a player from a group"""
        target, keyword = self._stored_target(ctx, usage="ungroup <player> <group>")
        if target is None:
            return
        if not keyword:
            ctx.reply(self.message("usage", usage="ungroup <player> <group>"))
            return
        group = self._find_group(ctx, keyword)
        if group is None:
            return
        if not self._may_act_on(ctx, target):
            return
        if not target.in_group(group):
            ctx.reply(self.message("group_not_in", name=target.name, group=group.name))
            return
        target.remove_group(group)
        self.console.storage.save_client(target)
        ctx.reply(self.message("group_removed", name=target.name, group=group.name))

    @command(level=80, alias="mr")
    def cmd_makereg(self, ctx: CommandContext) -> None:
        """makereg <player> - make a player a regular"""
        target, _ = self._stored_target(ctx, usage="makereg <player>")
        if target is None:
            return
        reg = self._find_group(ctx, "reg")
        if reg is None or not self._may_act_on(ctx, target):
            return
        if target.in_group(reg):
            ctx.reply(self.message("group_already_in", name=target.name, group=reg.name))
            return
        if target.max_level(self.groups) >= reg.level:
            ctx.reply(self.message("group_higher", name=target.name))
            return
        self._save_group(target, reg)
        ctx.reply(self.message("group_put", name=target.name, group=reg.name))

    @command(level=80, alias="ur")
    def cmd_unreg(self, ctx: CommandContext) -> None:
        """unreg <player> - take a player out of the regulars, back to plain user"""
        target, _ = self._stored_target(ctx, usage="unreg <player>")
        if target is None:
            return
        reg = self._find_group(ctx, "reg")
        user = self._find_group(ctx, "user")
        if reg is None or user is None or not self._may_act_on(ctx, target):
            return
        if not target.in_group(reg):
            ctx.reply(self.message("group_not_in", name=target.name, group=reg.name))
            return
        self._save_group(target, user)  # demoted, not stripped: they stay a registered user
        ctx.reply(self.message("group_removed", name=target.name, group=reg.name))

    @command(level=0)
    def cmd_register(self, ctx: CommandContext) -> None:
        """register - register yourself as a basic user"""
        user = self._find_group(ctx, "user")
        if user is None:
            return
        if ctx.client.in_group(user):
            ctx.reply(self.message("group_already_in", name=ctx.client.name, group=user.name))
            return
        if ctx.client.max_level(self.groups) >= user.level:
            ctx.reply(self.message("register_higher"))
            return
        self._save_group(ctx.client, user)
        ctx.reply(self.message("register_done", group=user.name))
        # Announced by default, as the classic bot was: seeing it encourages others to register.
        if self.settings.get("announce_registration", True):
            self.console.say(
                self.message("register_announce", name=ctx.client.name, group=user.name)
            )

    @command(level=20, alias="lt")
    def cmd_leveltest(self, ctx: CommandContext) -> None:
        """leveltest [player] - show a player's group and level"""
        if not ctx.arg_list():
            self._reply_level(ctx, ctx.client, masked=False)
            return
        target, _ = self._stored_target(ctx, usage="leveltest [player]")
        if target is None:
            return
        # Testing someone else shows their *masked* standing — that is what a mask is for.
        self._reply_level(ctx, target, masked=target is not ctx.client)

    @command(level=1)
    def cmd_regtest(self, ctx: CommandContext) -> None:
        """regtest - show your own group and level"""
        self._reply_level(ctx, ctx.client, masked=True)

    @command(level=40)
    def cmd_admintest(self, ctx: CommandContext) -> None:
        """admintest - show your own group and level"""
        self._reply_level(ctx, ctx.client, masked=True)

    @command(level=1, alias="regs")
    def cmd_regulars(self, ctx: CommandContext) -> None:
        """regulars - list the regular players currently connected"""
        reg = self._find_group(ctx, "reg")
        if reg is None:
            return
        regulars = [
            c for c in self.console.clients.connected() if c.display_level(self.groups) == reg.level
        ]
        if not regulars:
            ctx.reply(self.message("regulars_none"))
            return
        ctx.reply(self.message("regulars_online", regulars=", ".join(c.name for c in regulars)))

    # -- bulk actions -------------------------------------------------------
    #
    # Each acts on every *connected* player whose name contains the pattern. `_matching` already
    # excludes the caller and anyone out of their reach, so a bulk command can never do what the
    # single-target version would refuse.

    @command(level=80, alias="kall")
    def cmd_kickall(self, ctx: CommandContext) -> None:
        """kickall <pattern> [reason] - kick every player whose name matches"""
        self._bulk(ctx, "kicked", lambda t, r: self.console.kick(t, reason=r, admin=ctx.client))

    @command(level=80, alias="ball")
    def cmd_banall(self, ctx: CommandContext) -> None:
        """banall <pattern> [reason] - ban every player whose name matches"""
        self._bulk(ctx, "banned", lambda t, r: self.console.ban(t, reason=r, admin=ctx.client))

    @command(level=80, alias="sall")
    def cmd_spankall(self, ctx: CommandContext) -> None:
        """spankall <pattern> [reason] - spank every player whose name matches"""

        def spank(target: Client, reason: str) -> None:
            self.console.say(self.message("spanked", name=target.name, admin=ctx.client.name))
            self.console.kick(target, reason=reason, admin=ctx.client)

        self._bulk(ctx, "spanked", spank)

    def _bulk(self, ctx: CommandContext, verb: str, action: Callable[[Client, str], None]) -> None:
        tokens = ctx.arg_list()
        if not tokens:
            ctx.reply(self.message("usage", usage=f"{ctx.command.name} <pattern> [reason]"))
            return
        pattern = tokens[0]
        if not self._require_reason(ctx, " ".join(tokens[1:])):
            return
        _, reason = self.resolve_reason(" ".join(tokens[1:]))
        targets = self._matching(ctx, pattern)
        if not targets:
            ctx.reply(self.message("bulk_none", pattern=pattern))
            return
        for target in targets:
            action(target, reason)
        ctx.reply(self.message("bulk_result", verb=verb, count=len(targets), pattern=pattern))

    @command(level=80, alias="kiss")
    def cmd_clear(self, ctx: CommandContext) -> None:
        """clear [player] - clear a player's warnings, or everyone's"""
        if ctx.args.strip():
            target, _ = self._stored_target(ctx, usage="clear [player]")
            if target is None:
                return
            self.console.storage.disable_penalties(target.require_id(), PenaltyType.WARNING)
            self.console.say(self.message("cleared", admin=ctx.client.name, name=target.name))
            return
        for client in self.console.clients.connected():
            if client.id is not None:
                self.console.storage.disable_penalties(client.id, PenaltyType.WARNING)
        self.console.say(self.message("cleared_all", admin=ctx.client.name))

    # -- warnings -----------------------------------------------------------

    @command(level=20, alias="wi")
    def cmd_warninfo(self, ctx: CommandContext) -> None:
        """warninfo <player> - how many warnings a player is carrying"""
        target, _ = self._stored_target(ctx, usage="warninfo <player>")
        if target is None:
            return
        warnings = self.console.storage.get_active_penalties(
            target.require_id(), PenaltyType.WARNING
        )
        if not warnings:
            ctx.reply(self.message("warninfo_none", name=target.name))
            return
        latest = warnings[0]
        reason = latest.reason or self.message("no_reason_given")
        if latest.time_expire != NEVER_EXPIRES:
            remaining = (latest.time_expire - self.console.clock.epoch()) / 60
            reason = f"{reason} (expires in {duration_text(remaining)})"
        ctx.reply(self.message("warninfo", name=target.name, count=len(warnings), reason=reason))

    @command(level=20, alias="wr")
    def cmd_warnremove(self, ctx: CommandContext) -> None:
        """warnremove <player> - lift a player's most recent warning"""
        target, _ = self._stored_target(ctx, usage="warnremove <player>")
        if target is None:
            return
        warnings = self.console.storage.get_active_penalties(
            target.require_id(), PenaltyType.WARNING
        )
        if not warnings:
            ctx.reply(self.message("warns_none", name=target.name))
            return
        latest = warnings[0]  # get_active_penalties is newest-first
        self.console.storage.disable_penalty(latest.id)
        ctx.reply(
            self.message(
                "warn_removed",
                name=target.name,
                reason=latest.reason or self.message("no_reason_given"),
            )
        )

    @command(level=20, alias="wt")
    def cmd_warntest(self, ctx: CommandContext) -> None:
        """warntest <reason> - show what a warning reason expands to"""
        raw = ctx.args.strip()
        if not raw:
            ctx.reply(self.message("usage", usage="warntest <reason>"))
            return
        minutes, text = self.resolve_reason(raw)
        if minutes:
            text = f"{text} ({duration_text(minutes)})"
        ctx.reply(self.message("warntest_result", text=text))

    # -- lookup and listing --------------------------------------------------

    @command(level=80, alias="l")
    def cmd_lookup(self, ctx: CommandContext) -> None:
        """lookup <player> - find a player in the database, connected or not"""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("usage", usage="lookup <player>"))
            return
        found = self.console.lookup_clients(handle)
        if not found:
            ctx.reply(self.message("lookup_none", handle=handle))
            return
        for client in found[:5]:
            ctx.reply(
                self.message(
                    "lookup_found",
                    id=client.id,
                    name=client.name,
                    when=self.console.format_time(client.time_edit),
                )
            )

    @command(level=20)
    def cmd_find(self, ctx: CommandContext) -> None:
        """find <player> - find a connected player"""
        target, _ = self._target_and_reason(ctx)
        if target is None:
            return
        ctx.reply(self.message("found_player", name=target.name, cid=target.cid))

    @command(level=2)
    def cmd_seen(self, ctx: CommandContext) -> None:
        """seen <player> - when a player was last seen"""
        target, _ = self._stored_target(ctx, usage="seen <player>")
        if target is None:
            return
        ctx.reply(
            self.message("seen", name=target.name, when=self.console.format_time(target.time_edit))
        )

    @command(level=20)
    def cmd_list(self, ctx: CommandContext) -> None:
        """list - list the connected players and their slot ids"""
        clients = self.console.clients.connected()
        if not clients:
            ctx.reply(self.message("player_list_none"))
            return
        listed = ", ".join(f"[{c.cid}] {c.name}" for c in clients)
        ctx.reply(self.message("player_list", players=listed))

    @command(level=20)
    def cmd_longlist(self, ctx: CommandContext) -> None:
        """longlist - list the connected players with id, level and ping"""
        clients = self.console.clients.connected()
        if not clients:
            ctx.reply(self.message("player_list_none"))
            return
        pings = {p.cid: p.ping for p in self._safe_players()}
        for client in clients:
            ctx.reply(
                self.message(
                    "player_line",
                    cid=client.cid,
                    name=client.name,
                    id=client.id or 0,
                    level=client.display_level(self.groups),
                    ping=pings.get(client.cid or "", "?"),
                )
            )

    def _safe_players(self) -> list[PlayerInfo]:
        """Live player rows, or none at all — `!longlist` still works with a dead RCON."""
        try:
            return self.console.get_players()
        except Exception as exc:  # noqa: BLE001 - ping is a nicety, not the point of the command
            log.debug("player list unavailable: %s", exc)
            return []

    @command(level=40, alias="lbans")
    def cmd_lastbans(self, ctx: CommandContext) -> None:
        """lastbans - list the bans currently in force"""
        bans = self.console.storage.get_recent_penalties(
            (PenaltyType.BAN, PenaltyType.TEMPBAN), limit=5
        )
        if not bans:
            ctx.reply(self.message("lastbans_none"))
            return
        for penalty in bans:
            client = self.console.storage.get_client_by_id(penalty.client_id)
            name = client.name if client else f"@{penalty.client_id}"
            ctx.reply(self.message("baninfo", name=name, details=self._describe_penalty(penalty)))

    # -- output ---------------------------------------------------------------

    @command(level=20)
    def cmd_say(self, ctx: CommandContext) -> None:
        """say <message> - say something to everyone"""
        text = ctx.args.strip()
        if not text:
            ctx.reply(self.message("usage", usage="say <message>"))
            return
        self.console.say(text)

    @command(level=40)
    def cmd_scream(self, ctx: CommandContext) -> None:
        """scream <message> - announce something in the engine's largest text"""
        text = ctx.args.strip()
        if not text:
            ctx.reply(self.message("usage", usage="scream <message>"))
            return
        self.console.say_big(text)

    @command(level=20)
    def cmd_poke(self, ctx: CommandContext) -> None:
        """poke <player> - nudge a player who is not paying attention"""
        target, _ = self._target_and_reason(ctx)
        if target is None:
            return
        poke = POKES[self._poke_index % len(POKES)]
        self._poke_index += 1
        self.console.say(self.message("poked", message=poke, name=target.name))

    @command(level=40)
    def cmd_notice(self, ctx: CommandContext) -> None:
        """notice <player> <note> - record a note about a player"""
        target, note = self._target_and_reason(ctx)
        if target is None:
            return
        if not note:
            ctx.reply(self.message("usage", usage="notice <player> <note>"))
            return
        if not self._may_act_on(ctx, target):
            return
        self.console.notice(target, reason=note, admin=ctx.client)
        ctx.reply(self.message("noticed", name=target.name, notice=note))

    @command(level=0, alias="r")
    def cmd_rules(self, ctx: CommandContext) -> None:
        """rules - say the server rules"""
        rules = [self.spamages[key] for key in RULE_KEYS if key in self.spamages]
        if not rules:
            ctx.reply(self.message("rules_none"))
            return
        for rule in rules:
            ctx.reply(rule)

    @command(level=20, alias="s")
    def cmd_spam(self, ctx: CommandContext) -> None:
        """spam <keyword> - broadcast one of the configured messages"""
        keyword = ctx.args.strip().lower()
        if not keyword:
            ctx.reply(self.message("usage", usage="spam <keyword>"))
            return
        message = self.spamages.get(keyword)
        if message is None:
            ctx.reply(self.message("spam_unknown", keyword=keyword))
            return
        self.console.say(message)

    @command(level=20)
    def cmd_spams(self, ctx: CommandContext) -> None:
        """spams - list the configured spam messages"""
        if not self.spamages:
            ctx.reply(self.message("spams_none"))
            return
        ctx.reply(self.message("spams", keywords=", ".join(sorted(self.spamages))))

    @command(level=1)
    def cmd_time(self, ctx: CommandContext) -> None:
        """time - the server's current time"""
        ctx.reply(self.message("time", time=self.console.format_time()))

    @command(level=20)
    def cmd_b3(self, ctx: CommandContext) -> None:
        """b3 - what this bot is"""
        from b3 import __version__

        ctx.reply(
            self.message(
                "b3_version",
                name="Big Brother Bot",
                version=__version__,
                plugins=len(getattr(self.console, "plugins", {}) or {}),
                commands=len(self.console.command_registry.all()),
            )
        )

    # -- bot lifecycle --------------------------------------------------------

    @command(level=80)
    def cmd_rebuild(self, ctx: CommandContext) -> None:
        """rebuild - re-read the player list from the server"""
        clients = self.console.sync()
        ctx.reply(self.message("rebuilt", count=len(clients)))

    @command(level=80)
    def cmd_pause(self, ctx: CommandContext) -> None:
        """pause <duration> - stop acting on the game for a while (0 to resume)"""
        raw = ctx.args.strip()
        if not raw:
            ctx.reply(self.message("usage", usage="pause <duration>"))
            return
        try:
            minutes = parse_duration(raw)
        except ValueError:
            ctx.reply(self.message("invalid_duration", value=raw))
            return
        self.console.pause(minutes)
        if minutes > 0:
            self.console.say(self.message("paused", duration=duration_text(minutes)))
        else:
            self.console.say(self.message("unpaused"))

    @command(level=100, alias="su")
    async def cmd_runas(self, ctx: CommandContext) -> None:
        """runas <player> <command> - run a command as someone else"""
        tokens = ctx.arg_list()
        if len(tokens) < 2:
            ctx.reply(self.message("usage", usage="runas <player> <!command>"))
            return
        target = self.resolve_client(ctx, tokens[0])
        if target is None:
            return
        text = " ".join(tokens[1:])
        if text[0] not in "!@&":
            text = "!" + text
        # Runs with the target's level, not yours: that is the point — it is how an admin checks
        # what an ordinary player can actually do.
        await self.console.run_command(target, text)

    @command(level=100)
    def cmd_reconfig(self, ctx: CommandContext) -> None:
        """reconfig - re-read the configuration file"""
        try:
            self.console.reload_config()
        except RuntimeError as exc:  # e.g. replay mode, where no config path was recorded
            ctx.reply(self.message("reconfig_unavailable", reason=str(exc)))
            return
        ctx.reply(self.message("reconfigured"))

    @command(level=100)
    def cmd_die(self, ctx: CommandContext) -> None:
        """die - shut the bot down"""
        ctx.reply(self.message("shutting_down"))
        self.console.shutdown()

    @command(level=100)
    def cmd_restart(self, ctx: CommandContext) -> None:
        """restart - stop with a restart code, for whatever supervises the bot"""
        ctx.reply(self.message("restarting"))
        self.console.shutdown(restart=True)

    @command(level=100)
    def cmd_plugin(self, ctx: CommandContext) -> None:
        """plugin <list|enable|disable|info> [name] - turn a plugin on or off while the bot runs

        The loader already treats "disabled" as *inert, not absent*: a disabled plugin is
        instantiated with its handlers silenced and its commands hidden, and enabling it runs the
        startup it deferred. All that was missing was a way to say so from in-game.

        Level 100, and this one deserves it: enabling a plugin runs third-party code with full
        database and RCON access, and disabling the wrong one can silence the very moderation the
        server depends on.
        """
        parts = ctx.args.split()
        action = parts[0].lower() if parts else "list"
        name = parts[1] if len(parts) > 1 else ""

        if action == "list":
            plugins = self.console.plugins
            if not plugins:
                ctx.reply(self.message("plugin_list", plugins="none loaded"))
                return
            ctx.reply(
                self.message(
                    "plugin_list",
                    plugins=", ".join(
                        f"{n}{'' if p.is_enabled() else ' (off)'}"
                        for n, p in sorted(plugins.items())
                    ),
                )
            )
            return

        if not name:
            ctx.reply(self.message("usage", usage="plugin <list|enable|disable|info> [name]"))
            return

        plugin = self.console.get_plugin(name)
        if plugin is None:
            ctx.reply(self.message("plugin_unknown", name=name))
            return

        if action == "info":
            reason = plugin.disabled_reason
            ctx.reply(
                self.message(
                    "plugin_info",
                    name=name,
                    state="enabled" if plugin.is_enabled() else "disabled",
                    reason=f" ({reason})" if reason else "",
                    commands=len(self.console.command_registry.for_plugin(plugin)),
                )
            )
            return

        if action == "enable":
            if plugin.is_enabled():
                ctx.reply(self.message("plugin_already", name=name, state="enabled"))
                return
            plugin.enable()
            self.console.bus.publish_soon(Event(EventType.PLUGIN_ENABLED, data=name))
            ctx.reply(self.message("plugin_enabled", name=name))
            return

        if action == "disable":
            # `!plugin` lives in this plugin, so disabling it from in-game would remove the only
            # way to enable anything again, along with every moderation command. Disabling it stays
            # a config-and-restart decision.
            if plugin is self:
                ctx.reply(self.message("plugin_protected", name=name))
                return
            if not plugin.is_enabled():
                ctx.reply(self.message("plugin_already", name=name, state="disabled"))
                return
            plugin.disable()
            self.console.bus.publish_soon(Event(EventType.PLUGIN_DISABLED, data=name))
            ctx.reply(self.message("plugin_disabled", name=name))
            return

        ctx.reply(self.message("usage", usage="plugin <list|enable|disable|info> [name]"))

    # -- server control -----------------------------------------------------

    @command(level=80)
    def cmd_map(self, ctx: CommandContext) -> None:
        """map <name> - change to another map (a partial name will do)"""
        name = ctx.args.strip()
        if not name:
            ctx.reply(self.message("usage", usage="map <name>"))
            return
        chosen = self._resolve_map(ctx, name)
        if chosen is None:
            return
        self.console.say(self.message("map_changing", map=self.console.map_display(chosen)))
        self.console.change_map(chosen)

    def _resolve_map(self, ctx: CommandContext, wanted: str) -> str | None:
        """Turn what the admin typed into a map the server has, or reply saying why not.

        Map ids are awkward to type (`Thrust_Oilrig`, `mp_crossfire`, `MP_Subway`, `fl-harbor`) and
        sending one the server does not have fails silently, since `change_map` gets no reply to
        report. The name is matched against the rotation by id or display name, so `!map metro`
        resolves to `MP_Subway`.

        Where the rotation cannot be read at all the typed name is sent unchecked, which is all that
        can be done on those engines.
        """
        maps = self.console.get_maps()
        if not maps:
            return wanted
        found = match_names(wanted, [(m, self.console.map_display(m)) for m in maps])
        if not found:
            ctx.reply(self.message("map_not_found", map=wanted))
            if len(maps) <= MAPS_WORTH_LISTING:
                ctx.reply(self.message("maps_rotation", maps=self._map_list(maps)))
            return None
        if len(found) > 1:
            ctx.reply(self.message("map_ambiguous", count=len(found), maps=self._map_list(found)))
            return None
        return found[0]

    def _map_list(self, maps: list[str]) -> str:
        """Format maps for a reply, using display names where the title has them."""
        return ", ".join(self.console.map_display(m) for m in maps)

    @command(level=80)
    def cmd_maprotate(self, ctx: CommandContext) -> None:
        """maprotate - advance to the next map in the rotation"""
        self.console.say(self.message("map_rotating"))
        self.console.rotate_map()

    @command(level=2)
    def cmd_maps(self, ctx: CommandContext) -> None:
        """maps - list the server's map rotation"""
        maps = self.console.get_maps()
        if not maps:
            ctx.reply(self.message("maps_none"))
            return
        ctx.reply(self.message("maps_rotation", maps=self._map_list(maps)))

    @command(level=1)
    def cmd_nextmap(self, ctx: CommandContext) -> None:
        """nextmap - show the next map in the rotation"""
        next_map = self.console.get_next_map()
        if not next_map:
            ctx.reply(self.message("nextmap_unknown"))
            return
        ctx.reply(self.message("nextmap", map=self.console.map_display(next_map)))

    @command(level=20)
    def cmd_status(self, ctx: CommandContext) -> None:
        """status - report the bot's health and what the server is running"""
        try:
            self.console.storage.count_clients()
            database = "UP"
        except Exception as exc:  # noqa: BLE001 - reporting the outage *is* the command
            log.warning("status: database check failed: %s", exc)
            database = "DOWN"
        players = len(self.console.clients.connected())
        current = self.console.game.map_name or "?"
        ctx.reply(self.message("status", database=database, players=players, map=current))

    # -- masking ------------------------------------------------------------

    @command(level=100)
    def cmd_mask(self, ctx: CommandContext) -> None:
        """mask <group> [player] - appear to be in a lower group than you are"""
        tokens = ctx.arg_list()
        if not tokens:
            ctx.reply(self.message("usage", usage="mask <group> [player]"))
            return
        group = self._find_group(ctx, tokens[0])
        if group is None:
            return
        target = ctx.client
        if len(tokens) > 1:
            found = self.resolve_client(ctx, tokens[1])
            if found is None:
                return
            if found is not ctx.client and not self._may_act_on(ctx, found):
                return
            target = found
        target.mask_level = group.level
        self.console.storage.save_client(target)
        if target is not ctx.client:
            ctx.reply(self.message("masked_other", name=target.name, group=group.name))
        self.console.tell(target, self.message("masked", group=group.name))

    @command(level=100)
    def cmd_unmask(self, ctx: CommandContext) -> None:
        """unmask [player] - stop hiding a level"""
        tokens = ctx.arg_list()
        target = ctx.client
        if tokens:
            found = self.resolve_client(ctx, tokens[0])
            if found is None:
                return
            if found is not ctx.client and not self._may_act_on(ctx, found):
                return
            target = found
        target.mask_level = 0
        self.console.storage.save_client(target)
        if target is not ctx.client:
            ctx.reply(self.message("unmasked_other", name=target.name))
        self.console.tell(target, self.message("unmasked"))

    def _save_group(self, client: Client, group: Group) -> None:
        """Make ``group`` the client's only group and persist it."""
        client.set_group(group)
        self.console.storage.save_client(client)

    def _reply_level(self, ctx: CommandContext, target: Client, *, masked: bool) -> None:
        if masked and target.is_masked():
            group = next((g for g in self.groups if g.level == target.mask_level), None)
        elif target.group_bits == 0:
            group = None
        else:
            group = max_group(target.group_bits, self.groups)
        if group is None:
            ctx.reply(self.message("leveltest_nogroups", name=target.name, id=target.id or 0))
            return
        ctx.reply(
            self.message(
                "leveltest",
                name=target.name,
                id=target.id or 0,
                group=group.name,
                level=group.level,
            )
        )

    def _tempban_reply(self, target: Client, minutes: int) -> str:
        """ "tempbanned for 14 days" reads better than "for 20160 min"."""
        return self.message(
            "tempbanned", name=target.name, duration=duration_text(minutes), minutes=minutes
        )

    def _penalty_reply(self, key: str, target: Client, reason: str) -> str:
        """A penalty confirmation, with a separate template for the no-reason case."""
        if reason:
            return self.message(key, name=target.name, reason=reason)
        return self.message(f"{key}_no_reason", name=target.name)

    def _describe_penalty(self, penalty: Penalty) -> str:
        reason = penalty.reason or self.message("no_reason_given")
        if penalty.time_expire == NEVER_EXPIRES:
            return self.message("ban_permanent", reason=reason)
        return self.message(
            "ban_temporary",
            duration=duration_text(penalty.duration),
            minutes=penalty.duration,
            reason=reason,
        )

    @command(level=0, alias="h")
    def cmd_help(self, ctx: CommandContext) -> None:
        """help - list the commands you can use"""
        usable = self.console.command_registry.usable_by(ctx.client)
        names = sorted({c.name for c in usable})
        ctx.reply(self.message("help_commands", commands=", ".join(names)))

    @command(level=0)
    def cmd_iamgod(self, ctx: CommandContext) -> None:
        """iamgod - claim superadmin (only works while the server has no superadmin)"""
        if self.console.storage.has_superadmin():
            ctx.reply(self.message("iamgod_disabled"))
            return
        superadmin = group_by_keyword("superadmin")
        assert superadmin is not None
        ctx.client.add_group(superadmin)
        self.console.storage.save_client(ctx.client)
        ctx.reply(self.message("iamgod_done"))
