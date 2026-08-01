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

    # GUIDs that identify an AI player rather than a person. They must not be authenticated: every
    # bot on the server shares one, so a single database row would accumulate all of them, with one
    # shared level and one shared ban history.
    #
    # Two fields because the two engines that need this say it differently, and guessing either way
    # is wrong. Plutonium T6's bot guid is exactly "0", so an exact match is what is meant — treating
    # it as a prefix would swallow every real guid that happens to start with a zero. Plutonium IW5
    # gives its bots a long shared *prefix* and a varying tail, which no exact list could cover.
    bot_guids: frozenset[str] = field(default_factory=frozenset)
    bot_guid_prefixes: tuple[str, ...] = ()

    def is_bot_guid(self, guid: str) -> bool:
        """Whether ``guid`` belongs to an AI player rather than a person.

        A method on the profile rather than a check in one parser, because clients arrive by **two**
        routes: the game log, and the server's own status table read by
        :meth:`b3.runtime.bot.Bot.sync`. Filtering only the first still lets every bot on a Plutonium
        T6 server share one database row, since its bot guid ("0") is long enough to pass that
        title's length check.
        """
        if not guid:
            return False
        if guid in self.bot_guids:
            return True
        return bool(self.bot_guid_prefixes) and guid.startswith(self.bot_guid_prefixes)

    # Longest chat line this engine will show, when the engine imposes one. 0 means it does not, and
    # `bot.line_length` from the config decides alone. Where both apply the **smaller wins**: a
    # config value cannot lift an engine's limit, it can only ask for shorter lines. Plutonium is why
    # this exists — IW5 truncates at 43 characters and T6 at 72, so the 90-character default would
    # silently cut half of every reply off.
    line_length: int = 0

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
    # Setting a server variable. Black Ops (cod7) is the reason this is not hardcoded: it refuses a
    # plain `set` from rcon and wants `setadmindvar`, so every cvar the bot tried to set on it —
    # including the one that stops the game log being buffered — was quietly rejected.
    set_template: str = 'set %(name)s "%(value)s"'
    # *Reading* one. Sending the bare name is the Quake3 convention and what almost everything here
    # takes; Plutonium IW5 wants `get <name>` instead (IW4M-Admin's parser sends exactly that), and
    # answers with `name is "value"` rather than the quoted-and-colonned Quake3 form.
    get_cvar_template: str = "%(name)s"

    # Longest reason this engine will accept in a command. Frostbite *rejects* a command whose
    # reason runs past 80 characters, so on those titles this is the difference between a ban and no
    # ban at all — not a matter of tidiness.
    max_reason_length: int = 128

    # Longest name this engine's protocol allows, or 0 where there is no limit to enforce. Urban
    # Terror 4.2 lets a client connect with a nickname longer than the 32 characters the userinfo
    # string holds, which overflows it (BigBrotherBot/big-brother-bot#346). A name over this length
    # is truncated by the parser, and the runtime kicks the player unless `server.allow_long_names`
    # is set.
    name_max_length: int = 0

    # Sent once when the bot connects — e.g. asking CoD4X to report Steam64 ids.
    startup_commands: tuple[str, ...] = ()

    # Which RCON wire format this title speaks (see b3.net.rcon.DIALECTS). Black Ops frames its
    # packets differently from every other Infinity-Ward title; the rest share "quake3".
    rcon_dialect: str = "quake3"

    # Server queries and map control.
    #
    # More than one command is allowed, tried in order until one answers with players — the same
    # shape as `status_patterns` below, and for a related reason. A CoD4X server running the
    # `b3hide` mod strips hidden admins out of its `status` reply and answers **`b3status`** with the
    # full table instead; a server without the mod does not know that command at all. Asking for the
    # fullest answer first and falling back is the only way to serve both without being told which
    # one this is. An **empty tuple means the engine cannot be asked at all** (Altitude commands the
    # server by writing to a file and nothing ever replies), which is what stops the runtime sending
    # a question that would be appended to that file as a bogus command.
    status_commands: tuple[str, ...] = ("status",)
    # Row formats of the `status` reply, when they differ from the stock Infinity-Ward table.
    # Several candidates are allowed because a *mod* can change the table's shape on a game we
    # already support — CoD4X with `b3hide` is the case that prompted this. They are tried in
    # order and the first that yields any row wins, so the strict shape stays in charge on a
    # normal server and the fallback only exists for the server it does not fit. Empty = stock.
    status_patterns: "tuple[re.Pattern[str], ...]" = ()
    # Which named group of that row is the player's *persistent* identity. Stock CoD has only
    # `guid`; CoD4X also reports `steam`, which is the one that survives a reinstall.
    identity_field: str = "guid"
    # May hold **more than one command, one per line**: on Ravaged, loading a map is `addmap <name> 1`
    # (put it next in the rotation) followed by `nextmap` (go there now), and a single-command version
    # would appear to work while actually doing nothing until the round ended on its own.
    map_template: str = "map %s"
    rotate_command: str = "map_rotate"
    rotation_cvar: str = "sv_mapRotation"
    # For engines that have no cvars but *do* answer a question: Ravaged has `getmaplist false`.
    # Only consulted when `rotation_cvar` is empty, and only useful if the parser can read the reply
    # (`read_maps`) -- the same division as the roster, with the asking here and the parsing there.
    maplist_command: str = ""
