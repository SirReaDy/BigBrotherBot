"""Parsing the replies to RCON *queries* — ``status`` and cvar reads, for any engine.

Log lines tell the bot what happened; these tell it what *is*. The status table is the only place a
player's IP appears, so this module is what makes authentication by IP, `!status` and player-list
reconciliation possible.

The row format differs per engine and arrives as a compiled pattern from the game's profile; what
is shared is the shape of the job — find the map, skip what does not parse, and decide which column
is the player's identity.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from b3.core.game import PlayerInfo

#: Header line of a status reply: ``map: mp_crash``.
MAP_LINE_RE = re.compile(r"^map:\s+(?P<map>.+)$", re.IGNORECASE)

#: The stock id Tech 3 status row, printed by Call of Duty and by most Quake3 titles.
#: Transplanted from the legacy ``cod4.py`` ``_regPlayer`` (hex guid, so it also
#: matches the purely numeric guids older CoD titles print). Anchored on the ip:port + qport +
#: rate tail, which is what makes it safe to have a free-form name in the middle.
PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"(?P<guid>[0-9a-f]+)\s+"
    r"(?P<name>.*?)\s+"
    r"(?P<lastmsg>[0-9]+)\s+"
    r"(?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])):?"
    r"(?P<port>-?[0-9]{1,5})\s*"
    r"(?P<qport>-?[0-9]{1,5})\s+"
    r"(?P<rate>[0-9]+)\s*$",
    re.IGNORECASE,
)

#: ``"sv_maxclients" is: "16^7" default: "8^7"`` — and the terser variants some titles print.
CVAR_RE = re.compile(
    r'^"(?P<cvar>[a-z0-9_.]+)"\s+is:\s*"(?P<value>.*?)(?:\^7)?"', re.IGNORECASE | re.MULTILINE
)

#: Map names inside an ``sv_mapRotation`` value: ``gametype dm map mp_crash map mp_vacant``.
ROTATION_MAP_RE = re.compile(r"map\s+(?P<map>[a-z0-9_]+)", re.IGNORECASE)


def parse_status(
    text: str,
    patterns: re.Pattern[str] | Sequence[re.Pattern[str]] | None = None,
    identity: str = "guid",
) -> tuple[str, list[PlayerInfo]]:
    """Parse a ``status`` reply into ``(map name, players)``.

    ``patterns`` is the engine's row format — one compiled pattern, or **several candidates tried
    in order**, which is what a profile supplies via ``GameProfile.status_patterns``. The first
    candidate that yields any row wins, so a mod that changes the table's shape (CoD4X with
    ``b3hide``, or a build that disagrees about a column) can be handled by adding a shape rather
    than by making one pattern loose enough to mis-read every server. Order them most specific
    first, and remember that a looser candidate never runs while a stricter one still matches:
    the difference only shows up on a server the strict shape does not fit at all.

    ``identity`` names the column holding the player's persistent id. On CoD4X that is ``steam``,
    not ``guid``: the guid column there is a per-session number, while the Steam64 id is what still
    identifies the same person tomorrow — so getting this wrong would quietly create a new player
    record, and lose their level and bans, on every reconnect.

    Unparseable lines (the header, the dashes, a truncated final datagram) are skipped rather than
    raising: a status reply arrives over UDP and can legitimately be cut short.
    """
    map_name = _parse_map(text)
    for row_re in _candidates(patterns):
        players = _parse_rows(text, row_re, identity)
        if players:
            return map_name, players
    return map_name, []


def _candidates(
    patterns: re.Pattern[str] | Sequence[re.Pattern[str]] | None,
) -> tuple[re.Pattern[str], ...]:
    """Normalise the caller's argument to a tuple of row patterns, never empty."""
    if patterns is None:
        return (PLAYER_LINE_RE,)
    if isinstance(patterns, re.Pattern):
        return (patterns,)
    return tuple(patterns) or (PLAYER_LINE_RE,)


def _parse_map(text: str) -> str:
    for line in text.splitlines():
        header = MAP_LINE_RE.match(line.strip())
        if header:
            return header["map"].strip()
    return ""


def _parse_rows(text: str, row_re: re.Pattern[str], identity: str) -> list[PlayerInfo]:
    players: list[PlayerInfo] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = row_re.match(line)
        if match is None:
            continue
        groups = match.groupdict()
        steam = (groups.get("steam") or "").strip()
        # "0" means the engine has no id for them yet; fall back rather than merging every
        # unidentified player onto one record.
        chosen = (groups.get(identity) or "").strip()
        if chosen in ("", "0"):
            # Fall back to the guid column, which some engines omit entirely (a plain Quake3
            # server without an auth mod has no persistent id at all). Never None: everything
            # downstream compares and lowercases this.
            chosen = (groups.get("guid") or "").strip()
        players.append(
            PlayerInfo(
                cid=match["slot"],
                name=match["name"].strip(),
                guid=chosen,
                steam_id=steam if steam not in ("", "0") else "",
                ip=match["ip"],
                port=int(match["port"]),
                ping=int(groups.get("ping") or 0),
                # Not every engine reports a score: BattlEye's player table has no such column.
                score=int(groups.get("score") or 0),
            )
        )
    return players


def parse_cvar(name: str, reply: str) -> str | None:
    """Extract a cvar's value from the server's reply, or None if it did not answer with one."""
    match = CVAR_RE.search(reply)
    if match is None or match["cvar"].lower() != name.lower():
        return None
    return match["value"]


def parse_rotation(value: str) -> list[str]:
    """Map names from an ``sv_mapRotation`` value, in rotation order."""
    return [m["map"] for m in ROTATION_MAP_RE.finditer(value)]
