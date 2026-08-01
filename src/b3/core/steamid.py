"""One Steam account, three spellings, and the arithmetic between them.

Counter-Strike 2 is why this exists. The same player arrives written three ways depending on which
part of the server is talking:

===========================  ==================  ==============================================
form                         example             where it comes from
===========================  ==================  ==============================================
legacy ("SteamID")           STEAM_1:0:1111111   2012-era Source logs, and what classic B3 stored
modern ("SteamID3")          [U:1:2222222]       what CS2 writes in its log
64-bit ("SteamID64")         76561198182488450   what `status` reports, and the Steam Web API
===========================  ==================  ==============================================

All three name one **account number**, so they convert exactly:

* legacy ``STEAM_1:Y:Z`` is account ``Z * 2 + Y`` -- Y is the account's low bit, Z the rest;
* modern ``[U:1:N]`` *is* the account number;
* 64-bit is the account number plus :data:`STEAM64_BASE`.

Why it has to be done rather than left alone: the bot keys a player's record, their level and their
bans on the guid it is given. Left as-is, one CS2 player is up to three different people -- the log
creates them under ``[U:1:N]`` while `status` re-creates them under a Steam64, and **an imported
classic database, keyed on the legacy form, matches neither**. So an operator upgrading from the
classic bot would find every ban and every promotion apparently gone, with no error to explain it.

:func:`canonical` therefore returns the **legacy** form, because that is what classic B3 wrote and
this project's whole reason for preserving the data model is that those databases exist. It is not
the prettiest of the three; it is the one already on disk.

The universe digit is always emitted as ``1``. Every form above is universe-agnostic in practice and
classic CS:GO logs used ``STEAM_1``; a database written against ``STEAM_0`` by some much older title
would not match, which is the one case this deliberately does not try to serve.
"""

from __future__ import annotations

import re

#: Account number 0 as a 64-bit id. Every SteamID64 for an individual is this plus the account
#: number, which is what makes the conversion subtraction rather than bit twiddling.
STEAM64_BASE = 76561197960265728

#: ``STEAM_1:0:1111111``. The universe digit is accepted loosely (0 and 1 both occur in the wild)
#: and discarded; the middle digit is the account's low bit, so only 0 and 1 are valid there.
LEGACY_RE = re.compile(r"^STEAM_([0-5]):([01]):(\d+)$", re.IGNORECASE)

#: ``[U:1:2222222]``. The ``U`` is "individual" and the ``1`` is the universe; anything else here is
#: a group, a game server or a chat room rather than a player, so it is deliberately not matched.
MODERN_RE = re.compile(r"^\[U:1:(\d+)\]$", re.IGNORECASE)

#: A 17-digit SteamID64 for an individual account. Matched by length and range rather than by prefix
#: so that a number that merely starts with the right digits but cannot be an account is rejected.
STEAM64_RE = re.compile(r"^\d{17}$")


def account_id(value: str) -> int | None:
    """The account number behind any of the three forms, or None if this is not a Steam id.

    None is the answer for ``BOT``, ``Console``, an empty string, and for the hex guids every other
    engine here uses -- so a caller can offer any guid at all and act only on the ones that convert.
    """
    text = value.strip()
    if not text:
        return None

    legacy = LEGACY_RE.match(text)
    if legacy is not None:
        low_bit, rest = int(legacy.group(2)), int(legacy.group(3))
        return rest * 2 + low_bit

    modern = MODERN_RE.match(text)
    if modern is not None:
        return int(modern.group(1))

    if STEAM64_RE.match(text):
        number = int(text) - STEAM64_BASE
        # Below the base it is not an account id at all, and a 17-digit number is not necessarily a
        # Steam id -- CoD4X reports a real Steam64, but some engines report long decimal session ids.
        if number > 0:
            return number

    return None


def to_legacy(account: int) -> str:
    """An account number as ``STEAM_1:Y:Z`` -- the form classic B3 stored."""
    return f"STEAM_1:{account % 2}:{account // 2}"


def to_steam64(account: int) -> str:
    """An account number as a 17-digit SteamID64 -- what the Steam Web API wants."""
    return str(account + STEAM64_BASE)


def canonical(value: str) -> str:
    """Any Steam id in the legacy form; anything that is not a Steam id, unchanged.

    Returning the input untouched matters as much as the conversion: this sits on the path every guid
    takes, including ``BOT`` and the hex guids of unrelated engines, and it must be a no-op for them.
    """
    account = account_id(value)
    return value if account is None else to_legacy(account)
