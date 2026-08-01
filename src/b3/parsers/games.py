"""Every game the bot can run, and which parser reads it.

`server.game` names a title; a title names an engine family; a family has one parser. Adding a
title to an existing family is a few lines of data (see the profile modules); adding a family is a
new parser and an entry here.
"""

from __future__ import annotations

import difflib
import logging

from b3.core.clients import ClientManager
from b3.parsers.altitude import profiles as alt_profiles
from b3.parsers.altitude.parser import AltParser
from b3.parsers.base import Parser
from b3.parsers.battleye import profiles as be_profiles
from b3.parsers.battleye.parser import BeParser
from b3.parsers.cod import profiles as cod_profiles
from b3.parsers.cod.parser import CodParser
from b3.parsers.frostbite import profiles as fb_profiles
from b3.parsers.frostbite.parser import FbParser
from b3.parsers.homefront import profiles as hf_profiles
from b3.parsers.homefront.parser import HfParser
from b3.parsers.frontline import profiles as fl_profiles
from b3.parsers.frontline.parser import FlParser
from b3.parsers.profile import GameProfile
from b3.parsers.ravaged import profiles as rav_profiles
from b3.parsers.ravaged.parser import RavParser
from b3.parsers.q3 import profiles as q3_profiles
from b3.parsers.q3.et import EtParser
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.q3.sof2 import Sof2Parser
from b3.parsers.q3.urt import UrtParser
from b3.parsers.q3.wop import Wop15Parser
from b3.parsers.source import profiles as src_profiles
from b3.parsers.source.parser import SourceParser

#: family -> the parser that reads it. `urt`, `et` and `sof2` are Quake3 titles with extra lines of
#: their own rather than separate engines, so each is a Q3Parser subclass and inherits the whole
#: shared grammar — a title never loses a line by gaining a family.
FAMILIES: dict[str, type[Parser]] = {
    "cod": CodParser,
    "q3": Q3Parser,
    "urt": UrtParser,
    "et": EtParser,
    "sof2": Sof2Parser,
    "wop15": Wop15Parser,
    # Not a log format at all: a BattlEye server pushes its events down the RCON socket, so this
    # family's "log source" *is* its rcon client. See b3.net.battleye and cli._connect.
    "battleye": BeParser,
    # Frostbite goes further: its events are not even text. See b3.net.frostbite.
    "frostbite": FbParser,
    # Altitude has no admin socket at all: it writes a JSON log and reads commands out of a file.
    # See b3.net.altitude.
    "altitude": AltParser,
    # Homefront pushes its events too, over a length-prefixed TCP connection whose framing is
    # asymmetric -- six bytes of header out, seven back. See b3.net.homefront.
    "homefront": HfParser,
    # Ravaged pushes its events too, but it also *answers* questions -- and its framing is a
    # decimal length in brackets. See b3.net.ravaged.
    "ravaged": RavParser,
    # Frontline pushes everything too, and answers nothing: the server never says which command
    # it is replying to, so even its roster arrives as a line. See b3.net.frontline.
    "frontline": FlParser,
    # The Source engines read a log file like the Call of Duty titles do, but their rcon is a
    # request/response protocol over TCP with its own handshake, and the server's own facts come from
    # a separate query protocol on the game port. See b3.net.source and b3.net.a2s.
    "source": SourceParser,
}

#: Families whose events arrive over the RCON connection instead of a log file, so `server.game_log`
#: is meaningless for them and the CLI builds one object to serve as both.
PUSH_FAMILIES = frozenset({"battleye", "frostbite", "frontline", "homefront", "ravaged"})

#: Families the bot commands by appending to a file the game server reads, instead of over a socket.
#: They need `server.command_file` set, and there is no reply to anything they are sent.
FILE_RCON_FAMILIES = frozenset({"altitude"})

#: Families whose events come from a log file, as most do, but whose RCON is a stateful TCP session
#: rather than the connectionless UDP the Tech 3 engines use. They need their own client built, and
#: `server.port` is the game port they share it with.
TCP_RCON_FAMILIES = frozenset({"source"})

#: `server.game` -> profile. Deliberately unnumbered here: a hand-maintained count of titles in a
#: comment has gone wrong twice, and `len(PROFILES)` cannot.
PROFILES: dict[str, GameProfile] = {
    **cod_profiles.ALL,
    **q3_profiles.ALL,
    **be_profiles.ALL,
    **fb_profiles.ALL,
    **alt_profiles.ALL,
    **hf_profiles.ALL,
    **rav_profiles.ALL,
    **fl_profiles.ALL,
    **src_profiles.ALL,
}


log = logging.getLogger(__name__)


class UnknownGameError(ValueError):
    """`server.game` names a title nothing here can read."""


#: Title ids the classic bot used under a different spelling, accepted so an imported config starts
#: unchanged. The classic id for Quake 3 Arena is `q3a`; ours is `q3`.
ALIASES = {"q3a": "q3"}


def suggest(game: str) -> list[str]:
    """Title ids that look like a misspelling of ``game``; empty when nothing is close.

    Ratio first, then prefixes, because the two catch different mistakes and the prefix half is not
    optional: ``bf3_typo`` is only 0.55 similar to ``bf3`` — under difflib's cutoff — while sharing
    the whole of it, which is exactly the suffixed-name typo an operator makes.
    """
    wanted = game.lower()
    if wanted in ALIASES:
        return [ALIASES[wanted]]
    found = difflib.get_close_matches(wanted, sorted(PROFILES), n=3)
    if len(wanted) >= 2:  # one character, or none, is a prefix of far too much to be a hint
        found += [
            title
            for title in sorted(PROFILES)
            if title not in found and (title.startswith(wanted) or wanted.startswith(title))
        ]
    return found[:3]


def profile_for(game: str) -> GameProfile:
    """The profile for a ``server.game`` id, or raise.

    There used to be a ``DEFAULT`` here and an unrecognised id quietly got the CoD4 parser:
    ``game: bf3_typo`` ran ``CodParser`` against a Battlefield server, where every line fails to
    match and the only symptom is a bot that never reports anything. A guess that cannot be right
    is worse than a refusal, so this raises and names the near miss.
    """
    profile = PROFILES.get(game)
    if profile is not None:
        return profile
    renamed = ALIASES.get(game.lower())
    if renamed is not None:
        log.info("game %r is called %r here; using that profile", game, renamed)
        return PROFILES[renamed]
    near = suggest(game)
    did_you_mean = f" — did you mean {' or '.join(repr(n) for n in near)}?" if near else ""
    raise UnknownGameError(
        f"unknown game {game!r}{did_you_mean}\n"
        f"`b3 games` lists all {len(PROFILES)} titles this bot reads"
    )


def by_family() -> dict[str, list[str]]:
    """Title ids grouped by engine family — what `b3 games` prints and completion reads."""
    grouped: dict[str, list[str]] = {}
    for name, profile in PROFILES.items():
        grouped.setdefault(profile.family, []).append(name)
    return {family: sorted(ids) for family, ids in sorted(grouped.items())}


def parser_for(
    profile: GameProfile, clients: ClientManager | None = None, port: int = 0
) -> Parser:
    """Build the parser this profile's family needs.

    ``port`` is set on the parser afterwards rather than passed in, so that the one family that
    needs it (see :attr:`b3.parsers.base.Parser.port`) does not change the constructor every other
    family is built through.
    """
    family = FAMILIES.get(profile.family)
    if family is None:  # pragma: no cover - only reachable from a hand-written profile
        raise ValueError(f"{profile.name}: no parser for engine family {profile.family!r}")
    parser = family(profile, clients)
    parser.port = port
    return parser
