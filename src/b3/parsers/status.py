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
#:
#: Whitespace is allowed **before** the colon because the Source engines pad their header labels into
#: a column — ``map     : cs_foobar`` — so a pattern anchored on ``map:`` finds no map at all there,
#: and `!map` on those titles would report nothing while the reply plainly said otherwise.
#:
#: The value is one token rather than the rest of the line, which matters for the same family:
#: current Source builds append the spawn position (``map     : de_dust2 at: 0 x, 0 y, 0 z``) and
#: taking `.+` there would hand the whole of that back as the map's name. No engine here has a map
#: name containing a space.
MAP_LINE_RE = re.compile(r"^map\s*:\s*(?P<map>\S+)", re.IGNORECASE)

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
#:
#: The quotes around the name and the colon after `is` are both **optional**, because Plutonium
#: answers ``sv_maxclients is "18"`` with neither. Widening this rather than giving those titles their
#: own pattern is safe: `is` plus a quoted value is still required, so nothing else in a reply matches
#: — and the alternative was a cvar read that silently returned None on two whole titles.
#: The Source engines answer with an equals sign instead of the word — ``"tv_password" = ""`` — and
#: they are the reason ``=`` is an alternative here rather than a second pattern: `parse_cvar` still
#: checks that the name it read is the name that was asked, so widening the separator cannot make a
#: reply to one question answer another. Without it every cvar read on that family returns None, which
#: is indistinguishable from a server that does not have the cvar.
CVAR_RE = re.compile(
    r'^"?(?P<cvar>[a-z0-9_.]+)"?\s*(?:is:?|=)\s*"(?P<value>.*?)(?:\^7)?"',
    re.IGNORECASE | re.MULTILINE,
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
    # Keyed by slot, because a slot holds one player and some engines print them **twice**. An
    # Arma server prints a second row for a player who is also in the lobby, carrying the lobby
    # connection's address and port rather than the game one's:
    #
    #     0   76.108.91.78:2304     63   80a5…(OK) Bravo17
    #     0   192.168.0.100:2316    0    80a5…(OK) Bravo17 (Lobby)
    #
    # Appending both left the *last* row to win at every call site that reads a roster by slot,
    # so the address the bot recorded, banned on and matched shared ban lists against was the
    # lobby one — a private address, for a player whose real address was in the reply all along.
    # The in-game row wins; a lobby-only player still has their row, because they are connected
    # and kickable like anybody else.
    players: dict[str, PlayerInfo] = {}
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
        if (groups.get("bot") or "").strip() == "1":
            # Some tables say outright that a row is an AI player — the modern Call of Duty platforms
            # have a `bot` column, which beats every guid convention for telling. It is applied *after*
            # the fallback above, and that order is the whole point: the fallback would otherwise put
            # the bot's guid straight back, and every AI that ever played would end up sharing one
            # database row, with a single level and ban history between them.
            chosen = ""
        # The identity under the *other* name this table gives it — the one the log uses. Blank for
        # an AI player, whose `chosen` was just cleared: giving them a second spelling of an identity
        # they must not have would put every bot back on one shared record through the other door.
        raw_guid = (groups.get("guid") or "").strip()
        alt_guid = (
            raw_guid
            if chosen and raw_guid not in ("", "0") and raw_guid.lower() != chosen.lower()
            else ""
        )
        lobby = bool(groups.get("lobby"))
        seen = players.get(match["slot"])
        if seen is not None and (lobby or not seen.lobby):
            # Already have this slot, and this row is not a better view of it.
            continue
        players[match["slot"]] = PlayerInfo(
            cid=match["slot"],
            name=match["name"].strip(),
            guid=chosen,
            steam_id=steam if steam not in ("", "0") else "",
            alt_guid=alt_guid,
            ip=groups.get("ip") or "",
            # Every one of these is read defensively, because a row can legitimately omit them:
            # a Plutonium table prints `localhost` with no port for a listen server, and an
            # empty ping column for a bot. `int("")` would raise, and one such row would take
            # down the whole player list — including the ninety-nine players who parsed.
            port=_as_int(groups.get("port")),
            ping=_ping(groups.get("ping")),
            # Not every engine reports a score: BattlEye's player table has no such column.
            score=_as_int(groups.get("score")),
            lobby=lobby,
        )
    return list(players.values())


def _as_int(value: str | None, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


#: What a bot's ping is reported as, once the engine's placeholder is translated. 999 is the
#: convention the Plutonium parsers use, and it reads correctly everywhere a ping is compared:
#: ping-watching plugins kick on *high* pings, and an AI player has no connection to measure.
BOT_PING = 999


def _ping(value: str | None) -> int:
    """A ping column, which is not always a number.

    An AI player's ping is a placeholder — dashes on some builds, letters on others (IW4M-Admin's
    Plutonium IW5 pattern allows `[A-Z]+` there), and blank on Plutonium T6. Read as 0 those look
    like the best connection on the server, which is backwards for anything watching for high pings;
    refused outright, the row does not parse and the bot cannot see the player at all.
    """
    if value is None:
        return 0
    text = value.strip()
    if not text:
        return 0
    try:
        # Signed, because a number is a measurement whatever its sign: an Arma server reports **-1**
        # for a player who is still in the lobby, and turning that into a bot's ping would lose the
        # one thing it says about them. Only a *non-numeric* column is a placeholder.
        return int(text)
    except ValueError:
        return BOT_PING  # dashes, or a word


#: What a status *data* row looks like before anyone tries to read its columns: a slot, a score and a
#: ping. Loose on purpose — it exists only to tell "this reply had rows we could not read" apart from
#: "this reply had no rows", which are the same thing to :func:`parse_status` and very different
#: things to an operator.
#:
#: Two shapes, because the Source engines number their rows differently: ``#194 2 "courgette" …``
#: leads with a ``#`` and puts a quoted name where the Tech 3 table has a score, so the Tech 3 shape
#: recognises none of it. Without the alternative, `b3 probe` and the runtime's "rows we cannot read"
#: warning would report a Source table this bot failed to parse as an *empty server* — the one
#: distinction this pattern exists to make.
DATA_ROW_RE = re.compile(r"^\s*(?:#\s*[0-9]+\s+\S|[0-9]+\s+-?[0-9]+\s+[0-9]+\s+\S)")


def unparsed_rows(
    text: str, patterns: re.Pattern[str] | Sequence[re.Pattern[str]] | None = None
) -> list[str]:
    """Lines that look like player rows and matched none of the engine's row patterns.

    A caller that got no players out of a non-empty reply cannot otherwise tell an empty server from
    a table shape this bot does not know — and those want opposite responses. The second is worth
    saying out loud: it used to be answered by guessing at a looser pattern, and a guess that is
    almost right silently reads the id column as part of the player's name.
    """
    candidates = _candidates(patterns)
    rows = []
    for raw in text.splitlines():
        line = raw.strip()
        if not DATA_ROW_RE.match(line):
            continue
        if not any(row_re.match(line) for row_re in candidates):
            rows.append(line)
    return rows


def parse_cvar(name: str, reply: str) -> str | None:
    """Extract a cvar's value from the server's reply, or None if it did not answer with one."""
    match = CVAR_RE.search(reply)
    if match is None or match["cvar"].lower() != name.lower():
        return None
    return match["value"]


def parse_rotation(value: str) -> list[str]:
    """Map names from an ``sv_mapRotation`` value, in rotation order."""
    return [m["map"] for m in ROTATION_MAP_RE.finditer(value)]
