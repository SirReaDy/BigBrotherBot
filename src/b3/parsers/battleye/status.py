"""The shape of a BattlEye ``players`` reply.

The equivalent of the other families' ``status`` table, and the only place a connected player's IP
and GUID appear together::

    Players on server:
    [#] [IP Address]:[Port] [Ping] [GUID] [Name]
    --------------------------------------------------
    0   76.108.91.78:2304     31   80a5885ebe2420bab5e1581234567890(OK) Bravo17
    1   198.51.100.7:2304     -1   -                                    Joining (Lobby)
    (2 players in total)

Three differences from a Quake3-style table, all of which the row pattern has to allow for:

* there is **no score column**, so the shared reader treats a missing score as 0;
* the GUID carries its verification state in brackets — ``(OK)`` once BattlEye has checked it, ``(?)``
  while it has not;
* a player still in the lobby has no GUID at all (a bare ``-``) and a ping of ``-1``. They are real,
  connected, kickable players, so their row must still parse.

That last point is why this is **one pattern with an optional GUID** rather than two candidates.
``parse_status`` keeps the first pattern that matches *any* row — the right rule when a mod changes a
table's shape, which is what it was built for — but here both shapes appear in the *same* reply, so
two candidates would quietly drop everyone in the lobby.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: One row of the ``players`` table, with or without a GUID.
PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>\d+)\s+"
    r"(?P<ip>[0-9.]+):(?P<port>\d+)\s+"
    r"(?P<ping>-?\d+)\s+"
    # Either an id with its verification state, or a bare `-` for a player still in the lobby.
    r"(?:(?P<guid>[0-9a-f]+)\((?P<verified>[A-Z?]+)\)|-)\s+"
    r"(?P<name>.*?)"
    r"(?:\s+\(Lobby\))?\s*$",
    re.IGNORECASE,
)

PATTERNS = (PLAYER_LINE_RE,)

__all__ = ["BAN_LINE_RE", "Ban", "PATTERNS", "PLAYER_LINE_RE", "parse_bans"]


#: Section headers in a ``bans`` reply. The two lists are numbered **separately**, which is the whole
#: reason this is parsed rather than pattern-matched: `removeBan <n>` takes an index, and taking it
#: from the wrong section would lift a completely unrelated ban.
GUID_BANS_HEADER = re.compile(r"^GUID Bans:", re.IGNORECASE)
IP_BANS_HEADER = re.compile(r"^IP Bans:", re.IGNORECASE)

#: ``0  80a5885ebe2420bab5e1581234567890 perm Script Restriction #107``
BAN_LINE_RE = re.compile(
    r"^\s*(?P<index>\d+)\s+"
    r"(?P<target>\S+)\s+"
    r"(?P<minutes>perm|-?\d+)"
    r"(?:\s+(?P<reason>.*))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Ban:
    """One row of the server's own ban list."""

    index: int
    target: str  # a GUID or an IP, depending on `kind`
    kind: str  # "guid" or "ip"
    minutes: int | None  # None == permanent
    reason: str


def parse_bans(text: str) -> list[Ban]:
    """Read a ``bans`` reply.

    Rows are attributed to the section they appear under, so a GUID ban and an IP ban that happen to
    share an index stay distinguishable. Anything unparseable — headers, dashes, the trailing count —
    is skipped, as everywhere else: this arrives over UDP and can be cut short.
    """
    bans: list[Ban] = []
    kind = "guid"  # a reply that omits the header at all is the GUID list
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if GUID_BANS_HEADER.match(line):
            kind = "guid"
            continue
        if IP_BANS_HEADER.match(line):
            kind = "ip"
            continue
        match = BAN_LINE_RE.match(line)
        if match is None:
            continue
        raw_minutes = match["minutes"].lower()
        bans.append(
            Ban(
                index=int(match["index"]),
                target=match["target"],
                kind=kind,
                minutes=None if raw_minutes == "perm" else int(raw_minutes),
                reason=(match["reason"] or "").strip(),
            )
        )
    return bans
