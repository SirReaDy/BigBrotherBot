"""Every Call of Duty title, as data.

The classic tree carried one parser class per title — `cod2.py` through `cod8.py`, plus `cod4.py`,
`cod4x18.py` and `cod4gr.py` — and between them they overrode almost nothing but strings: a GUID
length, a `_commands` dict, a status-line regex. :class:`GameProfile` holds those strings instead,
so every title here shares one parser and no subclasses are needed.

They live in one module rather than one file per title because each is a handful of lines.

What each title changes:

* **cod** — Call of Duty 1. Every verb it uses is this file's default.
* **cod2** — short GUIDs, and it wants `g_logsync 1` rather than 3.
* **cod4** — the stock Infinity-Ward build: 32-character GUIDs.
* **cod4x** — CoD4X 1.8, what nearly every live CoD4 server runs: Steam64 identity and real ban
  verbs. See the notes on it below.
* **cod4gr** — CoD4 over GameRanger; the status table reports a GameRanger account id.
* **cod5** (World at War) — 8-character GUIDs.
* **cod6** (Modern Warfare 2) and **cod8** (Modern Warfare 3) — 16-character alphanumeric GUIDs, a
  shorter status table, and no working ban verb: `banclient` is accepted and ignored, so a ban is a
  kick the bot re-applies on every reconnect.
* **cod7** (Black Ops) — 5-character GUIDs, quoted reasons, its own RCON framing
  (:class:`b3.net.rcon.Cod7Dialect`), and the only title that will not take a plain `set`.
* **plutoiw5** / **plutot6** — Modern Warfare 3 and **Black Ops 2** on the Plutonium client, which is
  what the modern CoD scene runs. Black Ops 2 is a title the classic bot never supported at all.

Every CoD server is asked for `g_logsync` on connect. Without it the engine buffers its log file
and the bot reacts late or not at all — the classic bot set it on every startup for this reason,
and its absence looks like "the bot is broken" rather than "the log is buffered". Black Ops needs
that same request phrased its own way; getting it wrong there is silent, which is why the profile
carries the verb rather than the runtime assuming one.
"""

from __future__ import annotations

from dataclasses import replace

from b3.parsers.cod.profile import GameProfile
from b3.parsers.profile import VersionQuirk
from b3.parsers.cod.status import (
    COD4GR_PLAYER_LINE_RE,
    COD4X_PATTERNS,
    COD6_PLAYER_LINE_RE,
    COD7_PLAYER_LINE_RE,
    PLUTO_IW5_PLAYER_LINE_RE,
    PLUTO_T6_PLAYER_LINE_RE,
)

#: Unbuffered game logging. The engine's own cvar; 3 is append-unbuffered, which is what a tailing
#: bot needs. cod2 (and cod5, which inherited it) only understand 1.
LOGSYNC = ("g_logsync 3",)
LOGSYNC_COD2 = ("g_logsync 1",)

#: CoD4X refuses anything longer than 30 days on a native tempban.
MAX_TEMPBAN_MINUTES = 43_200

#: Shared by the Infinity-Ward CoD4-era titles.
_IW_TEAMS = {"allies": "blue", "axis": "red", "world": "world"}
#: An objective explosion, not a team kill — one of the oldest fixes in the classic tree.
_NOT_TEAMKILL = frozenset({"briefcase_bomb_mp"})

#: How much of a chat line a Call of Duty console displays. Anything past it is dropped by the
#: engine, so a longer line means a reply cut off mid-sentence. The two Plutonium titles set lower
#: values of their own (43 and 72); where both apply the smaller wins.
COD_LINE_LENGTH = 65

#: Every Call of Duty title the classic supported can run PunkBuster, and on the older ones it is the
#: only reliable identity a player has — `cod` and `cod2` hand out a six-character CD-key hash and
#: nothing else. Named rather than written as a bare `True` at eight call sites, so that what it
#: means is stated once. The two **Plutonium** titles are deliberately absent: that client postdates
#: PunkBuster's withdrawal from these games, so asking one would earn an unknown command at every
#: startup for a certain "no".
PUNKBUSTER = True

#: Call of Duty 1. Identity is the short CD-key hash rather than an IP, matching CoD2, and every
#: verb is this module's default — those defaults were taken from this title in the first place.
COD = GameProfile(
    name="cod",
    punkbuster=PUNKBUSTER,
    guid_min_length=6,
    line_length=COD_LINE_LENGTH,
    startup_commands=LOGSYNC,
)

#: Which cvar names the build, on every Call of Duty title. The classic bot read it through
#: `setVersionExceptions`, a hook `cod.py` declared and `cod2.py` was the only title to use.
VERSION_CVAR = "shortversion"

#: Call of Duty 2, and the two builds of it that are not like the others. Both faults are silent
#: ones, which is why they are worth carrying: a 1.0 server holds nobody's level and looks merely
#: broken, and a 1.2 server's PunkBuster ids fail a length check that never says so.
COD2_VERSIONS = {
    "1.0": VersionQuirk(
        warning=(
            "this build of Call of Duty 2 cannot authenticate players properly, so levels and "
            "bans will not hold. Either update the server, or run it in IP-only mode "
            "(server.game: cod2 with the server configured for it) and accept that players are "
            "identified by address"
        ),
        # A server already running IP-only has been configured around the fault; saying it every
        # restart would be noise about a decision that has been made.
        only_when_using_ids=True,
    ),
    # 32 everywhere else. A validator written to the documented length rejects every id this build
    # produces, and a rejected id looks exactly like a player who has none.
    "1.2": VersionQuirk(pb_id_length=31),
}

COD2 = GameProfile(
    name="cod2",
    punkbuster=PUNKBUSTER,
    guid_min_length=6,
    line_length=COD_LINE_LENGTH,
    startup_commands=LOGSYNC_COD2,
    version_cvar=VERSION_CVAR,
    version_quirks=COD2_VERSIONS,
)

COD4 = GameProfile(
    name="cod4",
    punkbuster=PUNKBUSTER,
    guid_min_length=32,
    line_length=COD_LINE_LENGTH,
    teams=_IW_TEAMS,
    non_teamkill_weapons=_NOT_TEAMKILL,
    startup_commands=LOGSYNC,
)

COD4X = GameProfile(
    name="cod4x",
    punkbuster=PUNKBUSTER,
    # A Steam64 id is 17 digits. Stock CoD4's 32 would reject every CoD4X player, and 0 (what the
    # reference parser uses) would accept the "0" the engine reports before a client is identified.
    guid_min_length=17,
    line_length=COD_LINE_LENGTH,
    teams=_IW_TEAMS,
    non_teamkill_weapons=_NOT_TEAMKILL,
    startup_commands=("g_logsync 3", "sv_usesteam64id 1"),
    # `b3status` first. A CoD4X server running the `b3hide` mod — which is how admins hide, and what
    # B3 on CoD4X is usually deployed with — leaves hidden players out of its `status` reply
    # altogether, and answers `b3status` with the full table. A server without the mod does not know
    # the command, so `status` follows as the fallback. Trying the fullest answer first is the only
    # way to serve both, since nothing in the reply says which kind of server this is.
    status_commands=("b3status", "status"),
    status_patterns=COD4X_PATTERNS,
    # The status table carries a per-session guid *and* a Steam64 id. Keying on the guid would
    # create a new player record — losing their level and their bans — on every reconnect.
    identity_field="steam",
    kick_template="kick %(cid)s %(reason)s",
    ban_template="permban %(cid)s %(reason)s",
    tempban_template="tempban %(cid)s %(minutes)sm %(reason)s",
    tempban_max_minutes=MAX_TEMPBAN_MINUTES,
    unban_template="unban %(guid)s",
    # 126, not the default 128: the reference fork cuts the reason there, and its changelog records
    # it as the server's own limit rather than a matter of taste.
    max_reason_length=126,
)

COD4GR = GameProfile(
    name="cod4gr",
    punkbuster=PUNKBUSTER,
    guid_min_length=10,
    line_length=COD_LINE_LENGTH,
    teams=_IW_TEAMS,
    non_teamkill_weapons=_NOT_TEAMKILL,
    status_patterns=(COD4GR_PLAYER_LINE_RE,),
    startup_commands=LOGSYNC,
)

#: Treyarch's per-action log lines (CoD5), mapped to the readable names Infinity-Ward writes in its
#: single `A;` line. The tokens are the classic parser's `_actionMap`; the names are ours, so a
#: plugin that watches for "bomb_planted" works on World at War and on CoD4 alike — the classic bot
#: published the bare token here and the spelled-out name there, which meant no plugin could
#: sensibly handle both.
TREYARCH_ACTIONS = {
    "ad": "actor_damage",  # dogs and AI
    "vd": "vehicle_damage",
    "bd": "bomb_defused",
    "bp": "bomb_planted",
    "fc": "flag_captured",
    "fr": "flag_returned",
    "ft": "flag_taken",
    "rc": "radio_captured",
    "rd": "radio_destroyed",
}

COD5 = GameProfile(
    name="cod5",
    punkbuster=PUNKBUSTER,
    guid_min_length=8,
    line_length=COD_LINE_LENGTH,
    startup_commands=LOGSYNC_COD2,  # cod5 inherited cod2's logsync value
    action_map=TREYARCH_ACTIONS,
)

#: MW2 and MW3 differ in nothing this bot cares about. Built with `dataclasses.replace` rather than
#: by splatting a dict, so a mistyped field name fails here instead of at import time.
_MODERN_WARFARE = GameProfile(
    name="",  # each title names itself below
    punkbuster=PUNKBUSTER,
    guid_min_length=16,
    line_length=COD_LINE_LENGTH,
    ban_template="clientkick %(cid)s",
    status_patterns=(COD6_PLAYER_LINE_RE,),
    startup_commands=LOGSYNC,
)

COD6 = replace(_MODERN_WARFARE, name="cod6")
COD8 = replace(_MODERN_WARFARE, name="cod8")

COD7 = GameProfile(
    name="cod7",
    punkbuster=PUNKBUSTER,
    guid_min_length=5,
    line_length=COD_LINE_LENGTH,
    kick_template='clientkick %(cid)s "%(reason)s"',
    ban_template="banclient %(cid)s",
    unban_template='unbanuser "%(target)s"',
    # `setadmindvar`, not a bare assignment. Black Ops refuses a plain `set` over rcon, and this
    # title was ported as data without noticing — so `g_logsync` was never applied and the engine
    # went on buffering its log, which is the failure this whole line exists to prevent. The second
    # command matters just as much: with timestamps in seconds the log line prefix stops being
    # `mm:ss`, the parser's timestamp stripper does not match it, and then *no line parses at all*.
    startup_commands=(
        "setadmindvar g_logsync 3",
        "setadmindvar g_logTimeStampInSeconds 0",
    ),
    set_template='setadmindvar %(name)s "%(value)s"',
    status_patterns=(COD7_PLAYER_LINE_RE,),
    rcon_dialect="cod7",  # Black Ops frames its RCON packets differently; see b3.net.rcon
)

#: Plutonium, the client the modern Call of Duty scene runs. Neither title existed in the classic
#: tree.
#:
#: The verbs and table shapes come from IW4M-Admin rather than from a B3 fork: the B3 parsers for
#: these titles (`xerxes-at/b3-parser-plutonium`, 2018) had IW5's kick verb wrong and its status
#: columns in the wrong order, where IW4M-Admin is actively maintained against these servers.
#:
#: Neither title has a working ban verb. IW4M-Admin maps both `Ban` and `TempBan` to the kick verb
#: on each, its history records `tempbanclient` being removed from the sibling TeknoMW3 parser, and
#: Plutonium's own admin script keeps bans in a JSON file and enforces them by kicking. So a ban
#: here is a kick re-applied from our database on reconnect, as it is on MW2 and MW3.
PLUTOIW5 = GameProfile(
    name="plutoiw5",
    # A guid is 15+ characters here (IW4M accepts 8-32), and an AI's is either a long shared prefix or
    # the literal form `bot<N>`. Both are named, because both are long enough to pass a length check.
    guid_min_length=15,
    bot_guid_prefixes=("FFFFFFFF000B07", "bot"),
    # The engine stops displaying a chat line after 43 characters. Not a preference: past it the
    # text is simply not shown.
    line_length=43,
    teams=_IW_TEAMS,
    non_teamkill_weapons=_NOT_TEAMKILL,
    # `clientkick`, not `dropclient` — that one belongs to TeknoMW3, a different Modern Warfare 3
    # stack, and sending it here would kick nobody.
    kick_template='clientkick %(cid)s "%(reason)s"',
    ban_template='clientkick %(cid)s "%(reason)s"',
    # Nothing to lift: the ban above never reached a server-side ban list, so our own record is the
    # whole of it. Declared inexpressible rather than sending an `unban` this engine does not know.
    unban_template=None,
    # This title alone wants `get <name>`; its reply is `name is "value"`, which `parse_cvar` accepts.
    get_cvar_template="get %(name)s",
    status_patterns=(PLUTO_IW5_PLAYER_LINE_RE,),
    startup_commands=LOGSYNC,
)

PLUTOT6 = GameProfile(
    name="plutot6",
    # Black Ops 2 guids really are that short — the B3 reference parser patches the client class to
    # allow a single character, which is the kind of thing a profile field exists to avoid.
    guid_min_length=1,
    # Exactly "0", not a prefix: as a prefix this would swallow every real guid starting with a zero.
    # The status table also states it outright in a `bot` column, which parse_status honours.
    bot_guids=frozenset({"0"}),
    line_length=72,
    teams=_IW_TEAMS,
    non_teamkill_weapons=_NOT_TEAMKILL,
    kick_template='clientkick_for_reason %(cid)s "%(reason)s"',
    ban_template='clientkick_for_reason %(cid)s "%(reason)s"',
    unban_template=None,  # as above: the ban is a kick, so there is nothing server-side to lift
    status_patterns=(PLUTO_T6_PLAYER_LINE_RE,),
    startup_commands=LOGSYNC,
)

#: Every title we can parse, by the id used in `server.game`.
ALL: dict[str, GameProfile] = {
    p.name: p for p in (COD, COD2, COD4, COD4X, COD4GR, COD5, COD6, COD7, COD8, PLUTOIW5, PLUTOT6)
}

__all__ = [
    "ALL",
    "COD",
    "COD_LINE_LENGTH",
    "COD2",
    "COD2_VERSIONS",
    "COD4",
    "COD4GR",
    "COD4X",
    "COD5",
    "COD6",
    "COD7",
    "COD8",
    "PLUTOIW5",
    "PLUTOT6",
    "TREYARCH_ACTIONS",
    "VERSION_CVAR",
]
