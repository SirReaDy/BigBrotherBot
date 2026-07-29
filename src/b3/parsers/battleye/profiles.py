"""Arma 2 and Arma 3, as data.

The two titles differ in nothing this bot cares about — the same RCON verbs, the same message
grammar, the same 32-character BattlEye GUID — which is why they are two lines here rather than two
parsers, exactly as the eight Call of Duty titles are.

What is genuinely different from every earlier family is the **verbs**, and one of them is a real
improvement: BattlEye's ban takes a duration in minutes, so a temporary ban is enforced by the server
instead of by the bot re-applying it on every reconnect.
"""

from __future__ import annotations

from dataclasses import replace

from b3.parsers.battleye.status import PATTERNS
from b3.parsers.profile import GameProfile

_BASE = GameProfile(
    name="",  # each title names itself below
    family="battleye",
    # A BattlEye GUID is a 32-character hash. There is no "world" slot: nothing in BattlEye's
    # messages is attributed to the environment, so the sentinel simply never matches.
    guid_min_length=32,
    world_cid="",
    # Arma has factions rather than the two fixed teams the shooters use, and BattlEye's messages
    # never mention them, so there is nothing to map.
    teams={},
    # `say -1` is the whole server; `say <slot>` is one player.
    say_template="say -1 %s",
    tell_template="say %(cid)s %(text)s",
    kick_template="kick %(cid)s %(reason)s",
    # Duration in minutes, 0 being permanent — so one verb covers both, and a tempban is enforced by
    # the server rather than re-applied by the bot on reconnect.
    ban_template="ban %(cid)s 0 %(reason)s",
    tempban_template="ban %(cid)s %(minutes)s %(reason)s",
    tempban_max_minutes=0,  # no engine ceiling
    # BattlEye's `removeBan` takes a *ban list index*, not a player id, so an unban needs the list
    # read first. Not expressible as a template — see TODO.md §2.4. None means the bot lifts the
    # penalty in its own database (which is what stops enforcement) and says the server side needs
    # doing by hand.
    unban_template=None,
    # There is no log file: the RCON socket is the event stream (b3.net.battleye).
    status_commands=("players",),
    status_patterns=PATTERNS,
    # Arma has no in-game map vote or rotation cvar; missions are changed with `#mission`.
    map_template="#mission %s",
    rotate_command="#restart",
    rotation_cvar="",
)

#: Arma 2 (including Operation Arrowhead and DayZ mods) and Arma 3.
ARMA2 = replace(_BASE, name="arma2")
ARMA3 = replace(_BASE, name="arma3")

#: Every BattlEye title, by the id used in `server.game`.
ALL: dict[str, GameProfile] = {p.name: p for p in (ARMA2, ARMA3)}

__all__ = ["ALL", "ARMA2", "ARMA3"]
