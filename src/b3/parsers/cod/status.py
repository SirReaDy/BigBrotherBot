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
    BOT_PING,
    MAP_LINE_RE,
    PLAYER_LINE_RE,
    parse_cvar,
    parse_hostname,
    parse_rotation,
    parse_status,
    unparsed_rows,
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
    # An address, or the literal word `bot` — what CoD4X writes for an AI player, and the only
    # thing in the row that identifies one. Without the alternative, a table holding bots *and*
    # people yielded only the people (the first pattern that matches any row wins), so every bot
    # was dropped from the roster a second after the log put it there — and `!kick botd` could not
    # find a player who was plainly on the server.
    r"(?:(?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])):?"
    r"(?P<port>-?[0-9]{1,5})|bot)"
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

#: CoD4X **without** `sv_usesteam64id`, which is a live configuration rather than a hypothetical: it
#: is what `kristiandz/ebc-b3` — a fork maintained against a real CoD4X 1.8 server — parses, and its
#: `_guidLength = 6` says the same thing. One id column, purely numeric, and the stock `lastmsg`
#: still present.
#:
#: This shape has to be matched *explicitly*. Left to a looser fallback, a row like
#: ``0  12  47  123456  ^1Bob^7  50  192.0.2.44:28960 …`` parses with the id absorbed into the name
#: (``name='123456 ^1Bob^7'``, no guid at all) — so nobody authenticates, `!status` shows nonsense,
#: and `sync` writes those names to the database as aliases. That is worse than not parsing the row.
COD4X_NO_STEAM_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"(?P<guid>[0-9]+)\s+"
    r"(?P<name>.*?)\s+"
    r"(?:(?P<lastmsg>[0-9]+)\s+)?"
    r"(?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}"
    r"(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])):?"
    r"(?P<port>-?[0-9]{1,5})"
    r"(?:\s+(?P<qport>-?[0-9]+))?(?:\s+(?P<rate>[0-9]+))?\s*$",
    re.IGNORECASE,
)

#: The shapes a CoD4X server can print, most specific first: with a Steam64 column, then without one.
#:
#: **There is deliberately no loose "unknown shape" candidate.** There used to be, to cover a table
#: the `b3hide` mod had stripped, and it was inferred rather than captured — which is the whole
#: problem with it: a guess that is *almost* right reads the id column as part of the name and puts
#: the result in the database. The real mechanism for a b3hide server is to ask a different question
#: (`b3status`, see `GameProfile.status_commands`), and a shape neither pattern fits is now reported
#: as such — see `b3.parsers.status.unparsed_rows` and `Bot._read_status`.
#:
#: The `lastmsg` column stays an *optional group* inside each pattern rather than becoming another
#: candidate: both variants match the same rows, so splitting them would not make the choice any
#: better defined — and the ambiguity that remains lives in the free-form name column (a player
#: called "Sniper 2" looks exactly like "Sniper" plus a lastmsg of 2), which no amount of candidate
#: ordering resolves.
COD4X_PATTERNS = (COD4X_PLAYER_LINE_RE, COD4X_NO_STEAM_PLAYER_LINE_RE)

#: Black Ops (cod7). Two differences from the stock table, both from the classic `cod7.py` and both
#: missed when this title was ported as data:
#:
#: * the guid is **numeric and may be negative** — `-1234567`. Servers running the teknoops mod report
#:   those, and the shared hex pattern skips the row entirely, so the player is invisible to `!status`
#:   and to the player-list sync. (Transplanted from `miltann/b3-parser-cod7`, which fixed exactly
#:   this.)
#: * between the port and the qport there can be **dashes rather than spaces** (`qportsep` in the
#:   classic parser), which no other title prints.
#:
#: Deliberately not matched: the row for `[3arc]democlient`, whose address is `unknown`. The classic
#: parser had a second pattern for it purely to count it during pre-match detection; here there is
#: nothing to gain from turning the demo recorder into a player.
COD7_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"(?P<guid>-?[0-9]+)\s+"
    r"(?P<name>.*?)\s+"
    r"(?P<lastmsg>[0-9]+)\s+"
    r"(?P<ip>[0-9.]+):"
    r"(?P<port>-?[0-9]+)"
    r"[-\s]+"
    r"(?P<qport>-?[0-9]+)\s+"
    r"(?P<rate>[0-9]+)\s*$",
    re.IGNORECASE,
)

#: The addresses these engines print where an IP would go. `bot`/`loopback` are self-explanatory;
#: `unknown` and `0.0:-1` turn up for a client the server has not finished accepting.
_MODERN_ADDRESS = r"(?:[0-9]{1,3}(?:\.[0-9]{1,3}){3}|0+\.0+|loopback|unknown|bot|localhost)"

#: Plutonium IW5 (Modern Warfare 3), from IW4M-Admin's own parser — the tool the modern Call of Duty
#: scene runs, still maintained (its parsers were ported to C# in June 2026). It publishes the header
#: outright::
#:
#:     num  score  bot  ping  guid  name  address  qport
#:
#: Worth knowing, because three of those columns are unlike every older title:
#:
#: * there is an explicit **`bot` column** (`0`/`1`) — a far better statement of "this is an AI" than
#:   any guid convention, and :func:`b3.parsers.status.parse_status` honours it;
#: * the **ping can be letters** for a bot, not just digits;
#: * the address may be `loopback`, `unknown` or literally `bot`, with no port at all.
#:
#: This replaces a transplant of `xerxes-at/b3-parser-plutonium` (2018), whose regex put the score
#: **last** and captured no guid at all — its own author called it rough in a comment. Trusting it
#: would have meant nobody on an IW5 server ever being identified from the status table.
PLUTO_IW5_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<bot>[01])\s+"
    r"(?P<ping>[A-Z]+|[0-9]+)\s+"
    r"(?P<guid>[a-z0-9]{8,32}|bot[0-9]+|[0-9]+)\s*"
    r"(?P<name>.{0,32}?)\s+"
    rf"(?P<ip>{_MODERN_ADDRESS})(?::(?P<port>-?[0-9]{{1,5}}))?\s+"
    r"(?P<qport>-?[0-9]+)\s*$",
    re.IGNORECASE,
)

#: Plutonium T6 (Black Ops 2), likewise from IW4M-Admin::
#:
#:     num  score  bot  ping  guid  name  lastmsg  address  qport  rate
#:
#: The guid is hex (or a bare `0` for a bot) and there is a `lastmsg` column, which IW5 lacks. The
#: colour code before the name is **optional**: the reference transplant required a literal `^7`, so a
#: server that does not print one would have parsed as an empty table — a bot that sees nobody.
PLUTO_T6_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<bot>[01])\s+"
    r"(?P<ping>[0-9]*)\s+"
    r"(?P<guid>[a-f0-9]+)\s+"
    r"(?:\^7)?(?P<name>.+?)\s+"
    r"(?P<lastmsg>[0-9]+)\s+"
    rf"(?P<ip>{_MODERN_ADDRESS})(?::(?P<port>-?[0-9]{{1,5}}))?\s+"
    r"(?P<qport>-?[0-9]+)\s+"
    r"(?P<rate>[0-9]+)\s*$",
    re.IGNORECASE,
)

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
    "BOT_PING",
    "COD4GR_PLAYER_LINE_RE",
    "COD7_PLAYER_LINE_RE",
    "PLUTO_IW5_PLAYER_LINE_RE",
    "PLUTO_T6_PLAYER_LINE_RE",
    "COD4X_NO_STEAM_PLAYER_LINE_RE",
    "COD4X_PATTERNS",
    "COD4X_PLAYER_LINE_RE",
    "COD6_PLAYER_LINE_RE",
    "MAP_LINE_RE",
    "PLAYER_LINE_RE",
    "parse_cvar",
    "parse_hostname",
    "parse_rotation",
    "parse_status",
    "unparsed_rows",
]
