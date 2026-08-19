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

from b3.core import steamid


@dataclass(frozen=True, slots=True)
class RequiredMod:
    """A server-side mod the bot cannot work without, and how to prove it is installed.

    The Source engines are why this exists. A stock Source server has no verb for a private message
    and none for a ban that outlives a restart, so on those titles *every* command reply and the
    whole of `!ban` go through SourceMod — a third-party plugin. Starting anyway produces a bot that
    looks healthy and silently does nothing, which is the failure mode this whole codebase keeps
    finding; so the runtime asks, and refuses to start when the answer is wrong.

    Data rather than a check in one parser, because the shape is general: ask a command, expect some
    text in the reply.
    """

    #: What to call it when telling the operator what is missing.
    name: str
    #: The command whose reply proves it is there.
    command: str
    #: Text that must appear in that reply. A server without the mod answers something like
    #: ``Unknown command "sm"``, which contains none of it.
    expect: str


@dataclass(frozen=True, slots=True)
class MapRequest:
    """What an admin meant by ``!map <something>``, split the way this engine splits it.

    Three fields rather than two because "too many arguments" has to be *reportable*. Silently
    dropping the tail is how `!map market push` came to load the right map in the wrong mode in the
    first place, and quietly gluing the tail onto the last argument would send the server a gamemode
    that does not exist. So the surplus comes back, and `cmd_map` answers with the usage line.
    """

    #: The map, as typed. Still has to be resolved against the rotation by the caller.
    name: str
    #: The engine's extra arguments that were given, keyed by `GameProfile.map_arguments`. Only the
    #: ones the admin actually typed appear; the rest are the server's own defaults.
    extras: dict[str, str] = field(default_factory=dict)
    #: Anything typed past the last argument this engine understands.
    surplus: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OptionalMod:
    """A server-side plugin that is not required, but gives this engine better verbs when present.

    SourceMod's "B3 Say" is the case this was built for: with it installed, ``b3_say``/``b3_hsay``/
    ``b3_psay`` display far better on screen than the stock ``sm_`` verbs. The classic bot asked
    ``sm plugins list`` at connect and switched templates when it saw the plugin named.

    The awkward part, and the reason this is data: a :class:`GameProfile` is **frozen**, deliberately,
    so a title cannot be edited at runtime by whatever happens to be loaded. So the profile declares
    what it would *become* if the mod turns out to be there, and the runtime swaps in that whole
    profile once — rather than the classic's arrangement, where the parser reached into itself and
    rewrote a template mid-flight.

    Distinct from :class:`RequiredMod`, and the difference is the whole point: a missing RequiredMod
    stops the bot, because without it nothing works. A missing OptionalMod is not mentioned twice.
    """

    #: What the listing calls it — matched against the plugin *name*, not the whole reply line.
    name: str
    #: The command that lists what is installed.
    command: str
    #: `GameProfile` field -> the value to use instead, when the mod is there. Field names are
    #: checked against the dataclass at startup, so a typo is a refusal rather than a silent no-op.
    #: Values are not only templates — "B3 Say" also turns `prefix_messages` off, because a mod that
    #: draws the bot's messages distinctly itself makes the bot's own prefix redundant clutter.
    overrides: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VersionCheck:
    """Refuse to run against a server that is not the game this title parses.

    The Frostbite family is why. Its six titles share one parser and one set of verbs, so pointing a
    ``bf3`` bot at a Battlefield 4 server connects, authenticates and then reads a grammar that is
    almost but not quite right — which is the expensive kind of wrong. One command settles it: the
    server answers ``version`` with its own game name and build number, so it *says* which game it is.

    The classic bot checked exactly this, per title (`checkVersion`), and raised on a mismatch. Kept
    as a refusal rather than a warning for the reason it originally was: the alternative is a bot that
    runs and misreads, and every event it gets wrong looks like a quiet server.

    ``min_build`` is the classic's second check and the one that could age badly, so it is a floor
    taken straight from the version of the parser being ported rather than a number invented here.
    Every currently-running server is far above it; it exists to reject a server so old that the
    events this parser expects were not in it yet.
    """

    #: The command to ask. Its reply must start with :attr:`game_name`.
    command: str
    #: What this game calls itself in that reply — "BF3", "BFHL", "MOHW".
    game_name: str
    #: Lowest build number this parser was written against. 0 = accept any build.
    min_build: int = 0


@dataclass(frozen=True, slots=True)
class VersionQuirk:
    """Something true of one *build* of a title and not of its siblings.

    Call of Duty 2 is why this exists, and it has one of each kind. Build 1.0 cannot authenticate
    players properly at all, which the classic bot said out loud at startup because the alternative
    is a bot that looks healthy and holds nobody's level. Build 1.2 with PunkBuster reports
    **31-character** PunkBuster ids where every other build reports 32, so a validator written to the
    documented length rejects every id that server produces — silently, since a rejected id is
    indistinguishable from a player who has none.

    Data rather than a hook, because both are statements about a build rather than behaviour, and
    because the classic bot's version of this (`setVersionExceptions`) was a method each parser
    overrode — which is how the 1.2 rule ended up applying to a title nobody checked.
    """

    #: Said once at startup. Empty for a build that only differs in the fields below.
    warning: str = ""
    #: Only warn when the bot is actually relying on ids. A 1.0 server running in IP-only mode has
    #: been configured around the fault and does not need telling about it every restart.
    only_when_using_ids: bool = False
    #: This build's PunkBuster id length, where it differs. 0 = the profile's own.
    pb_id_length: int = 0


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

    def canonical_guid(self, guid: str) -> str:
        """Fold the spellings of one identity into the one this bot stores.

        Only Counter-Strike 2 needs it so far, and it needs it badly: that engine writes ``[U:1:N]``
        in its log while ``status`` answers with a 17-digit Steam64, so without this the same player
        is two records, and a database imported from classic B3 -- keyed on the third spelling,
        ``STEAM_1:Y:Z`` -- matches neither. See :mod:`b3.core.steamid` for the arithmetic and for why
        the legacy form is the one kept.

        A no-op unless the title asks for it, so no other family pays for it.
        """
        if not self.normalise_steam_ids:
            return guid
        return steamid.canonical(guid)

    def map_display(self, map_id: str) -> str:
        """The name to show a player for a map id, or the id itself if the title has no table.

        Matching is exact first, then longest prefix, because some titles report a level path with a
        suffix: Bad Company 2 says ``Levels/MP_001`` and Medal of Honor ``levels/mp_01_elimination``.
        On Medal of Honor that suffix names a different map ("Bagram Hanger" against
        "Mazar-i-Sharif Airfield"), so the longest match has to win and table order must not matter.
        """
        if not self.map_names:
            return map_id
        key = map_id.strip().lower()
        exact = self.map_names.get(key)
        if exact is not None:
            return exact
        best = max(
            (candidate for candidate in self.map_names if key.startswith(candidate)),
            key=len,
            default="",
        )
        return self.map_names[best] if best else map_id

    def parse_map_request(self, text: str) -> MapRequest:
        """Split what an admin typed after ``!map`` into a map and this engine's extra arguments.

        On a title that takes no extras the whole line is the map name, spaces and all — which it has
        to be, because plenty of map names contain one (``Grand Bazaar``). That is why the extras are
        opt-in per title rather than "split on whitespace and hope": splitting by default would break
        `!map grand bazaar` on every engine here to serve the two that want a second argument.

        Empty arguments are dropped rather than passed on as blank (``!map metro,,2`` means the map
        and the rounds, leaving the gamemode alone), since an empty positional is not what the admin
        meant by leaving it out and some of these servers read it as one.
        """
        if not self.map_arguments:
            return MapRequest(name=text.strip())
        parts = [p.strip() for p in text.split(self.map_argument_separator)]
        name, rest = parts[0], parts[1:]
        extras: dict[str, str] = {}
        for key, value in zip(self.map_arguments, rest, strict=False):
            if value:
                extras[key] = value
        surplus = tuple(p for p in rest[len(self.map_arguments) :] if p)
        return MapRequest(name=name, extras=extras, surplus=surplus)

    def map_usage(self) -> str:
        """The `!map` argument list past the map name, in this engine's own separator. Empty if none.

        Formatted here rather than in the admin plugin because the separator is a fact about the
        title: telling a Source admin to type `!map market, push` would be a usage line that does not
        work, which is worse than one that omits the argument.
        """
        if not self.map_arguments:
            return ""
        joiner = self.map_argument_separator
        spaced = joiner if joiner.endswith(" ") else joiner + " "
        return "".join(f"{spaced}[{name}]" for name in self.map_arguments)

    # Whether the bot puts `bot.prefix` in front of what it says on this title. Almost always yes:
    # without it the bot's messages are indistinguishable from player chat on a busy server. Turned
    # **off** by a mod that already draws them distinctly — SourceMod's "B3 Say" is the case, and the
    # classic switched to its verbs *and* stopped prefixing for exactly this reason, since two
    # markers on one line is worse than either.
    prefix_messages: bool = True

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

    # Engine map id -> the name players use for it, keys lowercased. Frostbite titles report
    # `MP_Subway` for the map everyone calls "Operation Metro", so `!map`, `!maps` and `!nextmap`
    # would otherwise print ids nobody recognises. Consulted through `map_display` below.
    map_names: dict[str, str] = field(default_factory=dict)

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
    # Whether this title reports one Steam account under more than one spelling, so that they have
    # to be folded together before anything is keyed on the result. Counter-Strike 2 does: `[U:1:N]`
    # in the log, a 17-digit Steam64 in `status`, and `STEAM_1:Y:Z` in any database imported from
    # classic B3. See `canonical_guid` above and :mod:`b3.core.steamid`.
    normalise_steam_ids: bool = False

    # Sent once when the bot connects — e.g. asking CoD4X to report Steam64 ids.
    startup_commands: tuple[str, ...] = ()

    # A command whose reply describes the server itself — hostname, player limit, gametype, map,
    # rounds. The classic bot's `getServerVars`/`getServerInfo`. Only the Frostbite family needs it:
    # every other engine here reports the same values as cvars, which `Game.update_cvars` already
    # reads, and Frostbite has no cvars at all. The reply is positional, so the parser that knows the
    # shape reads it (`read_server_info`) and this only says what to ask.
    server_info_command: str = ""
    # Proof that the server is the game this title parses, asked once at startup — see
    # :class:`VersionCheck`. None on every family where the question cannot be got wrong, either
    # because the engine hosts one game or because its log states the title on every line.
    version_check: VersionCheck | None = None
    # The cvar naming this server's build, read once at startup. `shortversion` on the Call of Duty
    # engines. Empty means the bot does not ask, which is right for every family whose builds do not
    # differ in anything it depends on.
    version_cvar: str = ""
    # What is true of particular builds — see :class:`VersionQuirk`. Keyed by the value the cvar
    # reports, matched on the leading part so that "1.2" covers "1.2 build 4". A build not named here
    # is an ordinary one.
    version_quirks: dict[str, VersionQuirk] = field(default_factory=dict)

    def quirk_for(self, version: str) -> VersionQuirk | None:
        """The quirk for a reported version string, matched longest-prefix-first.

        Longest first because a server reports something like ``1.2 build 4`` and both ``1.2`` and
        ``1`` could be listed: the more specific statement is the one meant. Same reasoning as
        `map_display`, and the same reason table order must not decide it.
        """
        if not version or not self.version_quirks:
            return None
        stated = version.strip()
        matches = [key for key in self.version_quirks if stated.startswith(key)]
        if not matches:
            return None
        return self.version_quirks[max(matches, key=len)]

    # A server-side mod without which this title cannot be administered at all. Checked on connect;
    # None for every engine that needs nothing installed. See :class:`RequiredMod`.
    required_mod: RequiredMod | None = None
    # Whether this engine can run PunkBuster at all. The anti-cheat is driven over the game's own
    # RCON socket with its own `PB_SV_*` verbs, so a title either has it or the commands are unknown
    # — there is nothing in between. Where it is present it is worth having chiefly because **it
    # knows who a player is on engines that do not**: a plain Quake 3 or an older Call of Duty server
    # may hand out no usable `cl_guid`, and there a PunkBuster id is the only persistent thing about
    # a player. Whether it is actually installed is asked at startup, not assumed from this.
    punkbuster: bool = False
    # Mods that are not needed, but improve things when they are there — see :class:`OptionalMod`.
    # Asked about once, at connect. A tuple because a server can have more than one, and they are
    # applied in order so a later one wins a field an earlier one also names.
    optional_mods: tuple[OptionalMod, ...] = ()

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
    #
    # Two substitution forms, and which one is used depends on `map_arguments` below. With no extra
    # arguments the template takes the map name positionally (`map %s`), which is what almost every
    # title here wants and what they were all written as. A title that *does* take extras is rendered
    # by name instead — `%(map)s`, plus one placeholder per entry in `map_arguments` — because with
    # more than one value positional order stops being self-evident at the point it matters. Only the
    # opt-in titles pay for the second form.
    map_template: str = "map %s"
    # Extra arguments this engine's map verb accepts after the map name, in the order it takes them.
    # Insurgency is the case: `changelevel market push` loads a map *in a gamemode*, and without this
    # the mode an admin typed was dropped, so they got the right map in whatever mode the server
    # happened to be configured for. Empty = the verb takes a bare map name.
    #
    # The classic bot did this by monkey-patching the admin plugin from the parser
    # (`patch_b3_admin_plugin` in frostbite2/abstractParser.py), which is what made that version of
    # the feature unportable — the plugin had to be loaded, and the patch had to be reapplied. Here it
    # is data on the profile and one branch in `cmd_map`.
    map_arguments: tuple[str, ...] = ()
    # How an admin separates those arguments when typing `!map`. The two engines that want extras
    # disagree, and both spellings are the one their own players already use: Frostbite reads
    # `!map metro, rush, 2` (the classic bot's comma form, needed because a Frostbite map name has
    # spaces in it) and Source reads `!map market push`, matching the server verb it becomes.
    map_argument_separator: str = ","
    rotate_command: str = "map_rotate"
    rotation_cvar: str = "sv_mapRotation"
    # A cvar that names the map coming next, for engines whose map *list* is not a rotation. The
    # Source titles are why: what they can be asked for (`maps *`) is every map installed, in no
    # particular order, so deriving "the next one" by stepping through that list reports a map the
    # server has no intention of loading. SourceMod publishes the real answer in `sm_nextmap`.
    # Consulted before any arithmetic over `get_maps`, and empty means the engine has no such cvar.
    next_map_cvar: str = ""
    # Ask the server for **one** player's userinfo, for the slots the log never told us about. The
    # Quake 3 family is the case and `dumpuser <slot>` is the verb: a bot started mid-match, or one
    # that missed a `ClientUserinfo` line, has a player it cannot identify, and without this it stays
    # that way until they reconnect — the status table on these titles carries a name and a ping and
    # no persistent id at all. Empty on every engine whose status table already answers it.
    userinfo_command: str = ""
    # For engines that have no cvars but *do* answer a question: Ravaged has `getmaplist false`.
    # Only consulted when `rotation_cvar` is empty, and only useful if the parser can read the reply
    # (`read_maps`) -- the same division as the roster, with the asking here and the parsing there.
    maplist_command: str = ""


__all__ = [
    "GameProfile",
    "MapRequest",
    "OptionalMod",
    "RequiredMod",
    "VersionCheck",
    "VersionQuirk",
]
