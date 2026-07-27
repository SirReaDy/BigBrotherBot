"""Every game the bot can run, and which parser reads it.

`server.game` names a title; a title names an engine family; a family has one parser. Adding a
title to an existing family is a few lines of data (see the profile modules); adding a family is a
new parser and an entry here.
"""

from __future__ import annotations

from b3.core.clients import ClientManager
from b3.parsers.base import Parser
from b3.parsers.battleye import profiles as be_profiles
from b3.parsers.battleye.parser import BeParser
from b3.parsers.cod import profiles as cod_profiles
from b3.parsers.cod.parser import CodParser
from b3.parsers.frostbite import profiles as fb_profiles
from b3.parsers.frostbite.parser import FbParser
from b3.parsers.profile import GameProfile
from b3.parsers.q3 import profiles as q3_profiles
from b3.parsers.q3.et import EtParser
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.q3.sof2 import Sof2Parser
from b3.parsers.q3.urt import UrtParser

#: family -> the parser that reads it. `urt`, `et` and `sof2` are Quake3 titles with extra lines of
#: their own rather than separate engines, so each is a Q3Parser subclass and inherits the whole
#: shared grammar — a title never loses a line by gaining a family.
FAMILIES: dict[str, type[Parser]] = {
    "cod": CodParser,
    "q3": Q3Parser,
    "urt": UrtParser,
    "et": EtParser,
    "sof2": Sof2Parser,
    # Not a log format at all: a BattlEye server pushes its events down the RCON socket, so this
    # family's "log source" *is* its rcon client. See b3.net.battleye and cli._connect.
    "battleye": BeParser,
    # Frostbite goes further: its events are not even text. See b3.net.frostbite.
    "frostbite": FbParser,
}

#: Families whose events arrive over the RCON connection instead of a log file, so `server.game_log`
#: is meaningless for them and the CLI builds one object to serve as both.
PUSH_FAMILIES = frozenset({"battleye", "frostbite"})

#: `server.game` -> profile. Four engine families, twenty-nine titles.
PROFILES: dict[str, GameProfile] = {
    **cod_profiles.ALL,
    **q3_profiles.ALL,
    **be_profiles.ALL,
    **fb_profiles.ALL,
}

#: What an unknown `server.game` falls back to.
DEFAULT = cod_profiles.COD4


def parser_for(profile: GameProfile, clients: ClientManager | None = None) -> Parser:
    """Build the parser this profile's family needs."""
    family = FAMILIES.get(profile.family)
    if family is None:  # pragma: no cover - only reachable from a hand-written profile
        raise ValueError(f"{profile.name}: no parser for engine family {profile.family!r}")
    return family(profile, clients)
