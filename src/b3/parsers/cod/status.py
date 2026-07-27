"""The shapes a Call of Duty server prints — the row patterns for its `status` table.

Log lines tell the bot what happened; these tell it what *is*. The status table is the only place
a player's IP appears (the ``J;`` join line has none), so this module is what makes authentication
by IP, `!status` and player-list reconciliation possible.

Kept as pure functions over text, exactly like the line parser: the CoD-specific shape of the reply
lives here, the socket work stays in :mod:`b3.net.rcon`, and both are testable without the other.

A CoD ``status`` reply looks like::

    map: mp_crash
    num score ping guid                             name          lastmsg address        qport rate
    --- ----- ---- -------------------------------- ------------- ------- -------------- ----- ----
      0    12   47 1a2b3c...                        ^1Bob^7            50 192.0.2.44:28960 12345 25000
"""

from __future__ import annotations

import re

from b3.parsers.status import (
    MAP_LINE_RE,
    PLAYER_LINE_RE,
    parse_cvar,
    parse_rotation,
    parse_status,
)



#: CoD4X 1.8 reports an extra **Steam64** column and drops the stock table's `lastmsg`, so it needs
#: its own row pattern (transplanted from leiizko/b3_cod4x, the reference CoD4X parser). Both the
#: `lastmsg` and the trailing `qport`/`rate` are optional here: CoD4X builds disagree about them,
#: and a row that is one column off should still yield the player rather than nothing.
COD4X_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"(?P<guid>[0-9]+)\s+"
    r"(?P<steam>[0-9]+)\s+"
    r"(?P<name>.*?)\s+"
    r"(?:(?P<lastmsg>[0-9]+)\s+)?"
    r"(?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])):?"
    r"(?P<port>-?[0-9]{1,5})"
    r"(?:\s+(?P<qport>-?[0-9]+))?(?:\s+(?P<rate>[0-9]+))?\s*$",
    re.IGNORECASE,
)

#: cod6 (MW2) and cod8 (MW3) print an alphanumeric guid and stop after ip:port — no qport or rate.
COD6_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"(?P<guid>[a-z0-9]+)\s+"
    r"(?P<name>.*?)\s+"
    r"(?P<lastmsg>[0-9]+)\s+"
    r"(?P<ip>[0-9.]+):"
    r"(?P<port>-?[0-9]+)",
    re.IGNORECASE,
)

#: Last resort for a CoD4X server whose table has no usable id columns at all — which is what the
#: `b3hide` mod produces when it strips them. It is deliberately the *loosest* row shape we accept
#: (slot/score/ping, a name, an address) and it is only ever reached when
#: :data:`COD4X_PLAYER_LINE_RE` matched nothing, so no normal server is parsed by it. The players it
#: yields have an empty guid, exactly like a plain Quake3 server without an auth mod: they are
#: visible to `!status` and to the player-list sync, and authenticated by IP if the profile says so.
#:
#: NOTE this shape is inferred, not captured from a b3hide server — see TODO.md §1.1. It cannot make
#: a working setup worse (it never runs while the strict pattern matches), but the day someone posts
#: a real b3hide `status` reply, replace it with the measured one.
COD4X_HIDDEN_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"(?P<name>.*?)\s+"
    r"(?:(?P<lastmsg>[0-9]+)\s+)?"
    r"(?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])):?"
    r"(?P<port>-?[0-9]{1,5})"
    r"(?:\s+(?P<qport>-?[0-9]+))?(?:\s+(?P<rate>[0-9]+))?\s*$",
    re.IGNORECASE,
)

#: The shapes a CoD4X server can print, most specific first.
#:
#: The `lastmsg` column stays an *optional group* inside the strict pattern rather than becoming a
#: fourth candidate on purpose: both variants match the same rows, so splitting them would not make
#: the choice any better defined — and the ambiguity that remains lives in the free-form name column
#: (a player called "Sniper 2" looks exactly like "Sniper" plus a lastmsg of 2), which no amount of
#: candidate ordering resolves.
COD4X_PATTERNS = (COD4X_PLAYER_LINE_RE, COD4X_HIDDEN_PLAYER_LINE_RE)

#: CoD4 over GameRanger: the guid column is the GameRanger account id, behind a fixed prefix.
COD4GR_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"GameRanger-Account-ID_(?P<guid>[0-9]+)\s+"
    r"(?P<name>.*?)\s+"
    r"(?P<lastmsg>[0-9]+)\s*"
    r"(?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])):?"
    r"(?P<port>-?[0-9]{1,5})\s*"
    r"(?P<qport>-?[0-9]{1,5})\s+"
    r"(?P<rate>[0-9]+)$",
    re.IGNORECASE,
)




__all__ = [
    "COD4GR_PLAYER_LINE_RE",
    "COD4X_HIDDEN_PLAYER_LINE_RE",
    "COD4X_PATTERNS",
    "COD4X_PLAYER_LINE_RE",
    "COD6_PLAYER_LINE_RE",
    "MAP_LINE_RE",
    "PLAYER_LINE_RE",
    "parse_cvar",
    "parse_rotation",
    "parse_status",
]
