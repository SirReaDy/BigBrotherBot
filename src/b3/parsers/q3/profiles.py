"""The Quake3-engine titles, as data.

Eleven of the classic tree's parsers are Quake3 games that share a log grammar and an RCON dialect.
They are profiles over :class:`b3.parsers.q3.parser.Q3Parser`, exactly as the CoD titles are
profiles over `CodParser`.

Teams in a Quake3 infostring are numbers, not words: 0 free-for-all, 1 red, 2 blue, 3 spectator.
The world — falling damage, drowning, map hazards — is slot 1022.
"""

from __future__ import annotations

from dataclasses import replace

from b3.parsers.profile import GameProfile
from b3.parsers.q3.status import Q3_PLAYER_LINE_RE

#: Quake3 team numbers, as they appear in an infostring.
Q3_TEAMS = {"0": "free", "1": "red", "2": "blue", "3": "spectator"}

#: Every Quake3 engine reports the world as this slot.
WORLD = "1022"

#: The shared shape of every Quake3 title. Each title below is this with a name and, at most, two
#: fields changed — expressed with `dataclasses.replace` rather than by splatting a dict of keyword
#: arguments, so a typo in a field name is a type error here instead of a TypeError at import time.
_BASE = GameProfile(
    name="",  # each title names itself below
    family="q3",
    world_cid=WORLD,
    teams=Q3_TEAMS,
    status_patterns=(Q3_PLAYER_LINE_RE,),
    # Quake3's own verbs. `clientkick` takes a slot; `kick` takes a name on most titles.
    say_template="say %s",
    tell_template="tell %(cid)s %(text)s",
    kick_template="clientkick %(cid)s",
    ban_template="banClient %(cid)s",
    unban_template="banClient %(target)s",
    # Most Quake3 servers have no usable persistent id unless a mod provides cl_guid, so the floor
    # is low and the bot leans on IP history instead. Titles that do better raise it below.
    guid_min_length=8,
)

#: Baseline Quake 3 Arena.
Q3 = replace(_BASE, name="q3")

#: OpenArena — Quake3 with a different game DLL; same log grammar.
OA081 = replace(_BASE, name="oa081")

#: Soldier of Fortune 2, and its Gold/Platinum variant. The `sof2` family adds its `Hit:` line —
#: see b3.parsers.q3.sof2.
_SOF2 = replace(_BASE, family="sof2")
SOF2 = replace(_SOF2, name="sof2")
SOF2PM = replace(_SOF2, name="sof2pm")

#: Smokin' Guns, World of Padman — Quake3 total conversions.
SMG = replace(_BASE, name="smg")
SMG11 = replace(_BASE, name="smg11")
WOP = replace(_BASE, name="wop")
#: World of Padman 1.5 writes its own kill and damage lines, so it needs its own parser class — see
#: b3.parsers.q3.wop. 1.x stays on the shared grammar; only 1.5 is known to differ.
WOP15 = replace(_BASE, name="wop15", family="wop15")

#: Wolfenstein: Enemy Territory. ET sends the infostring on a line of its own, which the parser
#: already handles, and its identity arrives on a `ConnectInfo:` line as a 32-character PunkBuster
#: id — which is why the `et` family exists and why the floor is that length: a plain ET server
#: sets no cl_guid at all, so without that line nobody would ever be identified.
_ET = replace(_BASE, family="et", guid_min_length=32)
ET = replace(_ET, name="et")
ETPRO = replace(_ET, name="etpro")

#: Urban Terror 4.1/4.2/4.3 — by far the most-played of these. Its auth system gives stable
#: 32-character ids, so the floor is raised to reject the short ones a client can spoof. The `urt`
#: family adds its own log lines (hits, radio, flags, bomb, votes) on top of the shared grammar —
#: see b3.parsers.q3.urt.
#: `name_max_length` guards the userinfo-overflow exploit: 4.2 lets a client connect with a nickname
#: longer than the 32 characters the userinfo string holds. Declared for all three, since 32 is the
#: protocol's limit on each of them and the exploit is in the client rather than the server build.
_URT = replace(_BASE, family="urt", guid_min_length=32, name_max_length=32)
IOURT41 = replace(_URT, name="iourt41")
IOURT42 = replace(_URT, name="iourt42")
IOURT43 = replace(_URT, name="iourt43")

#: Every Quake3 title, by the id used in `server.game`.
ALL: dict[str, GameProfile] = {
    p.name: p
    for p in (Q3, OA081, SOF2, SOF2PM, SMG, SMG11, WOP, WOP15, ET, ETPRO, IOURT41, IOURT42, IOURT43)
}

__all__ = [
    "ALL",
    "ET",
    "ETPRO",
    "IOURT41",
    "IOURT42",
    "IOURT43",
    "OA081",
    "Q3",
    "Q3_TEAMS",
    "SMG",
    "SMG11",
    "SOF2",
    "SOF2PM",
    "WOP",
    "WOP15",
    "WORLD",
]

# Not implemented, and read with the shared grammar alone: UrT's `auth` identity service and SoF2's
# gametype-specific lines. A server using either sees fewer events, never wrong ones.
