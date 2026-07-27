"""Parsing a Quake3 ``status`` reply.

Same idea as the CoD version, different table. A Quake3 server prints::

    map: q3dm17
    num score ping name            lastmsg address               qport rate
    --- ----- ---- --------------- ------- --------------------- ----- -----
      0    12   47 Bob                  50 192.0.2.44:27960      12345 25000

Some titles insert a guid column (PunkBuster or a mod supplies it), so it is optional here: a
server without one still yields its players rather than nothing.
"""

from __future__ import annotations

import re

#: Header line of a status reply.
MAP_LINE_RE = re.compile(r"^map:\s+(?P<map>.+)$", re.IGNORECASE)

#: One player row, with the guid column optional.
Q3_PLAYER_LINE_RE = re.compile(
    r"^\s*(?P<slot>[0-9]+)\s+"
    r"(?P<score>-?[0-9]+)\s+"
    r"(?P<ping>[0-9]+)\s+"
    r"(?:(?P<guid>[0-9a-z]{8,32})\s+)?"
    r"(?P<name>.*?)\s+"
    r"(?P<lastmsg>[0-9]+)\s+"
    r"(?P<ip>[0-9.]+):(?P<port>-?[0-9]+)"
    r"(?:\s+(?P<qport>-?[0-9]+))?(?:\s+(?P<rate>[0-9]+))?\s*$",
    re.IGNORECASE,
)
