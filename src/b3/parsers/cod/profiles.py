"""Every Call of Duty title, as data.

The classic tree carried one parser class per title — `cod2.py` through `cod8.py`, plus `cod4.py`,
`cod4x18.py` and `cod4gr.py` — and between them they overrode almost nothing but strings: a GUID
length, a `_commands` dict, a status-line regex. That observation is what :class:`GameProfile` was
built on, and this module is the payoff: eight titles, one parser, no subclasses.

They are all here rather than one file per title because each is a handful of lines; splitting
them across modules made the two we happened to write first look more important than they are.

What each title changes:

* **cod2** — the original; short GUIDs, and it wants `g_logsync 1` rather than 3.
* **cod4** — the stock Infinity-Ward build: 32-character GUIDs.
* **cod4x** — CoD4X 1.8, what nearly every live CoD4 server runs: Steam64 identity and real ban
  verbs. See the notes on it below.
* **cod4gr** — CoD4 over GameRanger; the status table reports a GameRanger account id.
* **cod5** (World at War) — 8-character GUIDs.
* **cod6** (Modern Warfare 2) and **cod8** (Modern Warfare 3) — 16-character alphanumeric GUIDs, a
  shorter status table, and no working ban verb: `banclient` is accepted and ignored, so a ban is a
  kick the bot re-applies on every reconnect.
* **cod7** (Black Ops) — 5-character GUIDs, quoted reasons, and its own RCON framing
  (:class:`b3.net.rcon.Cod7Dialect`).

Every CoD server is asked for `g_logsync` on connect. Without it the engine buffers its log file
and the bot reacts late or not at all — the classic bot set it on every startup for this reason,
and its absence looks like "the bot is broken" rather than "the log is buffered".
"""

from __future__ import annotations

from dataclasses import replace

from b3.parsers.cod.profile import GameProfile
from b3.parsers.cod.status import COD4GR_PLAYER_LINE_RE, COD4X_PATTERNS, COD6_PLAYER_LINE_RE

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


COD2 = GameProfile(
    name="cod2",
    guid_min_length=6,
    startup_commands=LOGSYNC_COD2,
)

COD4 = GameProfile(
    name="cod4",
    guid_min_length=32,
    teams=_IW_TEAMS,
    non_teamkill_weapons=_NOT_TEAMKILL,
    startup_commands=LOGSYNC,
)

COD4X = GameProfile(
    name="cod4x",
    # A Steam64 id is 17 digits. Stock CoD4's 32 would reject every CoD4X player, and 0 (what the
    # reference parser uses) would accept the "0" the engine reports before a client is identified.
    guid_min_length=17,
    teams=_IW_TEAMS,
    non_teamkill_weapons=_NOT_TEAMKILL,
    startup_commands=("g_logsync 3", "sv_usesteam64id 1"),
    status_patterns=COD4X_PATTERNS,
    # The status table carries a per-session guid *and* a Steam64 id. Keying on the guid would
    # create a new player record — losing their level and their bans — on every reconnect.
    identity_field="steam",
    kick_template="kick %(cid)s %(reason)s",
    ban_template="permban %(cid)s %(reason)s",
    tempban_template="tempban %(cid)s %(minutes)sm %(reason)s",
    tempban_max_minutes=MAX_TEMPBAN_MINUTES,
    unban_template="unban %(guid)s",
)

COD4GR = GameProfile(
    name="cod4gr",
    guid_min_length=10,
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
    guid_min_length=8,
    startup_commands=LOGSYNC_COD2,  # cod5 inherited cod2's logsync value
    action_map=TREYARCH_ACTIONS,
)

#: MW2 and MW3 differ in nothing this bot cares about. Built with `dataclasses.replace` rather than
#: by splatting a dict, so a mistyped field name fails here instead of at import time.
_MODERN_WARFARE = GameProfile(
    name="",  # each title names itself below
    guid_min_length=16,
    ban_template="clientkick %(cid)s",
    status_patterns=(COD6_PLAYER_LINE_RE,),
    startup_commands=LOGSYNC,
)

COD6 = replace(_MODERN_WARFARE, name="cod6")
COD8 = replace(_MODERN_WARFARE, name="cod8")

COD7 = GameProfile(
    name="cod7",
    guid_min_length=5,
    kick_template='clientkick %(cid)s "%(reason)s"',
    ban_template="banclient %(cid)s",
    unban_template='unbanuser "%(target)s"',
    startup_commands=LOGSYNC,
    rcon_dialect="cod7",  # Black Ops frames its RCON packets differently; see b3.net.rcon
)

#: Every title we can parse, by the id used in `server.game`.
ALL: dict[str, GameProfile] = {
    p.name: p for p in (COD2, COD4, COD4X, COD4GR, COD5, COD6, COD7, COD8)
}

__all__ = [
    "ALL",
    "COD2",
    "COD4",
    "COD4GR",
    "COD4X",
    "COD5",
    "COD6",
    "COD7",
    "COD8",
    "TREYARCH_ACTIONS",
]

# Gaps in this file's coverage are tracked as work, with a proposed fix each, in TODO.md §1:
# cod5's actor-damage lines (§1.2), cod4x + b3hide changing the status table (§1.1), and cod7's
# missing log service (§1.5). They are listed there rather than described here, because a
# limitation in a comment is one nobody ever picks up.
