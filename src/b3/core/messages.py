"""Operator-customisable message templates + game-chat line wrapping.

The classic bot kept every piece of user-facing text in a config ``[messages]`` section, read through
``Parser.getMessage``, and split long output with ``Parser.getWrap`` so it survived the game's chat
line limit. The rewrite had neither: replies were hardcoded English f-strings that no server owner
could change and that would be truncated mid-word by the game.

This restores both, with the sharp edges filed off:

* Defaults live **in code**, so nothing breaks when a key is absent from the config, and the config
  only has to name what it wants to change.
* Templates use :meth:`str.format` (``{name}``) rather than the legacy ``%(name)s``, and a broken
  template can never take a command down: a missing placeholder is logged and the raw template is
  returned instead of raising mid-reply.
* Wrapping is word-aware and understands an embedded ``\\n``, like the legacy multiline handling.
"""

from __future__ import annotations

import logging
from textwrap import TextWrapper
from typing import Any

log = logging.getLogger(__name__)

# Game chat lines are short; 90 matches the classic bot's default for the CoD engines.
DEFAULT_LINE_LENGTH = 90

# Every piece of user-facing text, with the wording the rewrite already used. Override any of these
# from the main config's `messages:` block.
DEFAULT_MESSAGES: dict[str, str] = {
    # -- command framework ------------------------------------------------
    "unknown_command": "unknown command: {command}",
    "command_disabled": "command is currently disabled: {command}",
    "insufficient_access": "you do not have sufficient access for this command",
    "prefix_denied_loud": "you do not have sufficient access to broadcast with '{prefix}'",
    "prefix_denied_silent": "you do not have sufficient access to run silent commands",
    "reason_required": "you must supply a reason",
    "tempban_too_long": "you may not ban for longer than {limit}",
    "command_error": "error running command: {command}",
    "usage": "usage: {usage}",
    # -- target resolution -------------------------------------------------
    "player_not_found": "no player found matching '{handle}'",
    "stored_player_not_found": "no stored player found matching '{handle}'",
    "ambiguous_target": "{count} players match — be more specific or use @id: {candidates}",
    # Connected players are named by slot rather than @dbid: the slot is on screen and in `!status`,
    # and a player who has just joined may not have a database id yet.
    "ambiguous_connected": "{count} players match — be more specific or use the slot: {candidates}",
    # -- moderation --------------------------------------------------------
    "kicked": "{name} was kicked ({reason})",
    "kicked_no_reason": "{name} was kicked",
    "banned": "{name} was banned ({reason})",
    "banned_no_reason": "{name} was banned",
    "tempbanned": "{name} was tempbanned for {duration}",
    "warned": "{name} was warned ({reason})",
    "warned_no_reason": "{name} was warned",
    "unbanned": "{name} was unbanned ({count} {noun})",
    "no_active_ban": "{name} has no active ban",
    "invalid_duration": "invalid duration: '{value}' (try 30m, 2h, 1d)",
    "name_too_long": "your name is too long — this game allows {limit} characters",
    # -- penalty inspection ------------------------------------------------
    "baninfo": "{name}: {details}",
    "baninfo_none": "{name} is not banned",
    "lastbans_none": "no bans are currently in force",
    "ban_permanent": "permanent ban ({reason})",
    "ban_temporary": "tempban, {duration} ({reason})",
    "no_reason_given": "no reason given",
    "warns": "{name} has {count} warning(s): {reasons}",
    "warns_none": "{name} has no active warnings",
    "warns_cleared": "cleared {count} warning(s) on {name}",
    # -- identity ----------------------------------------------------------
    "aliases": "{name} also known as: {aliases}",
    "aliases_none": "{name} has no known aliases",
    "clientinfo": (
        "@{id} {name} guid={guid} ip={ip} level={level} connections={connections} "
        "aliases={aliases} ips={ips}"
    ),
    # -- group management ----------------------------------------------------
    "group_unknown": "no such group: '{group}' (known: {known})",
    "group_beyond_reach": "group {group} is beyond your reach",
    "group_already_in": "{name} is already in group {group}",
    "group_not_in": "{name} is not in group {group}",
    "group_put": "{name} put in group {group}",
    "group_removed": "{name} removed from group {group}",
    "group_higher": "{name} is already in a higher-level group",
    "leveltest": "{name} [@{id}] is {group} [{level}]",
    "leveltest_nogroups": "{name} [@{id}] is not in any group",
    "regulars_online": "regular players online: {regulars}",
    "regulars_none": "no regular players are currently connected",
    "register_done": "you are now a member of the group {group}",
    "register_announce": "{name} put in group {group}",
    "register_higher": "you are already in a higher-level group",
    "masked": "masked as {group}",
    "masked_other": "masked {name} as {group}",
    "unmasked": "un-masked",
    "unmasked_other": "un-masked {name}",
    # -- server / map --------------------------------------------------------
    "map_changing": "changing map to {map}",
    "map_rotating": "rotating to the next map",
    "map_current": "current map: {map}",
    "map_unknown": "the server did not report a map",
    "maps_rotation": "map rotation: {maps}",
    "maps_none": "the server has no map rotation configured",
    "map_not_found": "no map in the rotation matching '{map}'",
    "map_ambiguous": "{count} maps match — be more specific: {maps}",
    "nextmap": "next map: {map}",
    "nextmap_unknown": "could not work out the next map",
    "status": "database {database}, {players} player(s) on {map}",
    # -- lookup / info -------------------------------------------------------
    "found_player": "found {name} in slot {cid}",
    "lookup_found": "@{id} {name} — last seen {when}",
    "lookup_none": "no stored player matches '{handle}'",
    "seen": "{name} was last seen {when}",
    "player_list": "players: {players}",
    "player_list_none": "nobody is connected",
    "player_line": "[{cid}] {name} @{id} level {level} ping {ping}",
    "time": "server time: {time}",
    "b3_version": "{name} {version} — {plugins} plugin(s), {commands} commands",
    "poked": "{message} {name}!",
    "noticed": "notice added to {name}: {notice}",
    "rules_none": "no rules are configured",
    "spam_unknown": "no spam message called '{keyword}'",
    "spams": "spam messages: {keywords}",
    "spams_none": "no spam messages are configured",
    # -- bulk + lifecycle ----------------------------------------------------
    "bulk_result": "{verb} {count} player(s) matching '{pattern}'",
    "bulk_none": "no players match '{pattern}'",
    "spanked": "{name} was spanked by {admin}",
    "cleared": "{admin} cleared {name}'s warnings",
    "cleared_all": "{admin} cleared everyone's warnings",
    "paused": "not acting on the game for {duration}",
    "unpaused": "back on duty",
    "shutting_down": "shutting down",
    "restarting": "restarting",
    "reconfigured": "configuration reloaded",
    "reconfig_unavailable": "cannot reload the configuration: {reason}",
    "rebuilt": "player list synchronised: {count} player(s)",
    # -- runtime plugin control ---------------------------------------------
    "plugin_list": "plugins: {plugins}",
    "plugin_unknown": "no plugin called '{name}' is loaded",
    "plugin_enabled": "plugin '{name}' enabled",
    "plugin_disabled": "plugin '{name}' disabled",
    "plugin_already": "plugin '{name}' is already {state}",
    "plugin_info": "{name}: {state}{reason}, {commands} command(s)",
    "plugin_protected": "'{name}' cannot be disabled from in-game — see TODO.md §4.4",
    "warninfo": "{name} has {count} warning(s), latest: {reason}",
    "warninfo_none": "{name} has no active warnings",
    "warn_removed": "removed {name}'s latest warning ({reason})",
    "warntest_result": "warning would read: {text}",
    "warn_announce": "WARNING [{count}]: {name}, {reason}",
    "warn_alert": "ALERT: {name} will be banned in {seconds}s unless the {count} warnings are cleared",
    "warn_too_fast": "only one warning per {seconds} seconds can be issued",
    "warn_too_many": "too many warnings",
    "warn_tempban_reason": "too many warnings: {reason}",
    # -- action guards -------------------------------------------------------
    "action_denied_self": "you cannot do that to yourself",
    "action_denied_level": "{name} is at or above your level; action cancelled",
    "action_denied_masked": "{name} is a masked higher-level player; action cancelled",
    # -- misc --------------------------------------------------------------
    "admins_online": "admins online: {admins}",
    "admins_none": "no admins are currently connected",
    "help_commands": "commands: {commands}",
    "iamgod_done": "you are now superadmin",
    "iamgod_disabled": "there is already a superadmin; iamgod is disabled",
}


class Messages:
    """Template lookup + formatting + line wrapping for everything the bot says."""

    def __init__(
        self,
        overrides: dict[str, str] | None = None,
        *,
        line_length: int = DEFAULT_LINE_LENGTH,
        color_prefix: str = "",
    ) -> None:
        self._overrides = dict(overrides or {})
        self._plugin_defaults: dict[str, str] = {}  # filled in by plugins at startup
        self.line_length = line_length
        self.color_prefix = color_prefix
        self._wrapper = TextWrapper(
            width=max(8, line_length),
            drop_whitespace=True,
            break_long_words=True,
            break_on_hyphens=False,
        )
        for key in self._overrides:
            if key not in DEFAULT_MESSAGES:
                # Still worth saying — it is usually a typo — but a plugin that registers this
                # key at startup is a legitimate reason for it to be here.
                log.warning(
                    "config defines message %r, which no core command uses "
                    "(a plugin may register it at startup)",
                    key,
                )

    def register_defaults(self, defaults: dict[str, str]) -> None:
        """Add a plugin's own message defaults.

        Without this a plugin's text is either hardcoded English or renders as `[key]`, and an
        operator cannot translate or reword it the way they can the core's. Keys already defined
        by the operator's config keep winning — registering never overrides a choice they made.
        """
        for key, value in defaults.items():
            if key in DEFAULT_MESSAGES and DEFAULT_MESSAGES[key] != value:
                log.warning("plugin message %r would shadow a core message; ignored", key)
                continue
            self._plugin_defaults.setdefault(key, value)

    def template(self, key: str) -> str:
        """The raw template for a key: the operator's override, else the built-in default."""
        if key in self._overrides:
            return self._overrides[key]
        if key in DEFAULT_MESSAGES:
            return DEFAULT_MESSAGES[key]
        if key in self._plugin_defaults:
            return self._plugin_defaults[key]
        log.warning("no message defined for %r", key)
        return f"[{key}]"

    def get(self, key: str, **values: Any) -> str:
        """Format a message. A broken template is logged, never raised."""
        template = self.template(key)
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("message %r could not be formatted (%s); using it verbatim", key, exc)
            return template

    def wrap(self, text: str) -> list[str]:
        """Split text into game-chat-sized lines, honouring embedded newlines.

        Continuation lines get ``color_prefix`` prepended, which is how the classic bot kept a
        wrapped message readable on engines that reset colour per line.
        """
        if not text:
            return []
        paragraphs = [p for p in text.replace("\\n", "\n").split("\n") if p.strip()]
        lines: list[str] = []
        for paragraph in paragraphs:
            lines.extend(self._wrapper.wrap(paragraph) or [paragraph])
        if self.color_prefix and len(lines) > 1:
            lines = [lines[0], *(f"{self.color_prefix}{line}" for line in lines[1:])]
        return lines
