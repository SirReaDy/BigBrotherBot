"""GameProfile — the data-driven differences between game titles.

The classic codebase modelled cod2/4/5/6/7/8 as six thin subclasses that overrode class-attribute
strings and regexes. The review found ~90% of those differences are *data*, not behaviour. So a
title is a :class:`GameProfile`: GUID length/validation, team names, RCON command verbs, the weapons
that don't count as team kills, etc. One parser per *engine family* reads a profile; only genuine
behavioural forks would ever need a subclass.

It lives here rather than under `cod/` because the Quake3 family needs exactly the same data — the
only engine-specific thing about it is which parser reads it, which is what `family` says.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class GameProfile:
    name: str

    #: Which parser reads this profile — the engine family, not the title. See b3.parsers.games.
    family: str = "cod"

    #: Slot id the engine uses for "the world" (falling damage, environment kills). Quake3 says
    #: 1022; the CoD engines say -1.
    world_cid: str = "-1"

    # GUID handling. A guid shorter than guid_min_length is treated as invalid (garbage/partial),
    # which prevents authenticating a player on a bad id. cod4 = 32-hex.
    guid_min_length: int = 32

    # Map raw log team tokens to canonical team names.
    teams: dict[str, str] = field(
        default_factory=lambda: {"allies": "blue", "axis": "red", "world": "world"}
    )

    # Weapons/means-of-death that must NOT be classified as team kills (objective explosions etc.).
    non_teamkill_weapons: frozenset[str] = field(default_factory=frozenset)

    # Titles that write *one line type per objective action* instead of a single generic action
    # line map each terse token to the action name to publish — Treyarch's CoD5 is the case that
    # needs it, where Infinity-Ward puts everything in `A;`. Empty = this engine uses the generic
    # line, so those two-letter lines are not ours to interpret.
    action_map: dict[str, str] = field(default_factory=dict)

    # Authenticate by IP instead of GUID when the server can't supply usable GUIDs.
    ips_only: bool = False

    # BattlEye reports a player's id twice: once as the client claimed it, and again once its own
    # backend has checked it. Authenticating on the unverified one is faster and wrong — it is what
    # the client said it was, so it can be borrowed from a banned player or made up. True only if an
    # operator decides they would rather have the speed.
    trust_unverified_guid: bool = False

    # RCON command templates (Infinity-Ward verbs by default). Every penalty template is given
    # the same values — cid, guid, name/target, reason, minutes — so a profile picks what its
    # engine understands without the runtime needing to know which.
    say_template: str = "say %s"
    tell_template: str = "tell %(cid)s %(text)s"
    kick_template: str = "clientkick %(cid)s"
    ban_template: str = "banclient %(cid)s"
    # None where the engine has no verb that can express it from what the bot knows. BattlEye's
    # `removeBan` takes a ban-list index rather than a player id, so lifting a ban server-side needs
    # the list read first; the bot still lifts its own record, which is what stops enforcement.
    unban_template: str | None = "unbanuser %(target)s"
    # Engines with a *native* timed ban set this; the rest fall back to a plain ban and rely on
    # the bot re-applying it on reconnect (which is how the classic bot did every tempban).
    tempban_template: str | None = None
    tempban_max_minutes: int = 0  # 0 = no engine-imposed ceiling
    # Big centre-screen text. None means the engine has no such verb, so it falls back to `say`.
    saybig_template: str | None = None

    # Longest reason this engine will accept in a command. Frostbite *rejects* a command whose
    # reason runs past 80 characters, so on those titles this is the difference between a ban and no
    # ban at all — not a matter of tidiness.
    max_reason_length: int = 128

    # Sent once when the bot connects — e.g. asking CoD4X to report Steam64 ids.
    startup_commands: tuple[str, ...] = ()

    # Which RCON wire format this title speaks (see b3.net.rcon.DIALECTS). Black Ops frames its
    # packets differently from every other Infinity-Ward title; the rest share "quake3".
    rcon_dialect: str = "quake3"

    # Server queries and map control.
    status_command: str = "status"
    # Row formats of the `status` reply, when they differ from the stock Infinity-Ward table.
    # Several candidates are allowed because a *mod* can change the table's shape on a game we
    # already support — CoD4X with `b3hide` is the case that prompted this. They are tried in
    # order and the first that yields any row wins, so the strict shape stays in charge on a
    # normal server and the fallback only exists for the server it does not fit. Empty = stock.
    status_patterns: "tuple[re.Pattern[str], ...]" = ()
    # Which named group of that row is the player's *persistent* identity. Stock CoD has only
    # `guid`; CoD4X also reports `steam`, which is the one that survives a reinstall.
    identity_field: str = "guid"
    map_template: str = "map %s"
    rotate_command: str = "map_rotate"
    rotation_cvar: str = "sv_mapRotation"
