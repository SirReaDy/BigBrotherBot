"""PunkBuster — a second identity, a second ban list, and a screenshot request.

PunkBuster is an anti-cheat that runs *beside* the game server and is driven through the same RCON
socket, with its own ``PB_SV_*`` verbs. Where it is installed it is worth having for one reason above
the others: **it knows who a player is on engines that do not.** A plain Quake 3 or an older Call of
Duty server may hand out no usable `cl_guid` at all, and on those titles a PunkBuster id is the only
persistent thing about a player — without it a level cannot be held and a ban cannot be matched.

Ported from the classic ``b3/parsers/punkbuster.py``, with its captured test data
(``tests/core/parsers/test_punkbuster.py``, 300 lines) as the specification rather than the code.
Four things in that data decide the whole shape of this module, and none of them is guessable:

* **PunkBuster counts slots from 1 and the game counts from 0.** Every id would be filed against the
  wrong player without that subtraction — the one mistake here that silently bans the wrong person.
* **Ids are 30 to 32 hex characters, not 32.** The captures contain 31-character ids from real
  servers, which is the same fact §1.19 records about Call of Duty 2 build 1.2. A validator written
  to the documented length rejects them all, and a rejected id is indistinguishable from a player
  who has none.
* **Replies lose characters at random.** The classic's captures include a whole file of rows with a
  colon missing, a space missing, a quote missing, the ``(W)`` bracket half gone. This is a real
  reported defect of PunkBuster's output, not damage in transit, so the pattern is deliberately loose
  about everything except the three fields worth having.
* **The prefix is not fixed.** ``: ``, ``^3PunkBuster Server: ``, ``whatever: ``, or nothing at all,
  depending on the game and the mod. Anchoring to any of them loses whole titles.

What is deliberately **not** ported is the classic's ``__setattr__`` hook, which turned *any*
attribute assignment on this object into a ``PB_SV_<Name>`` command. A typo in an attribute name sent
a nonsense command to the anti-cheat and reported nothing. :meth:`PunkBuster.setting` does the same
job by asking for it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from b3.core.util import sanitize_rcon_value
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: The command that proves PunkBuster is running, and the word its reply carries. A server without
#: it answers an empty string or something beginning "Unknown command"; the classic checked exactly
#: that pair of conditions, and looking for the product's own name is the same test stated positively.
VERSION_COMMAND = "PB_SV_Ver"
VERSION_EXPECT = "punkbuster"

#: One row of a ``PB_SV_PList`` reply. The classic bot's pattern, ported as-is, because it is the
#: product of years of bug reports about malformed output — see the module docstring.
#:
#:     : 4 27b26543216546163546513465135135(-) 111.11.1.11:28960 OK 1 3.0 0 (W) "ShyRat"
#:
#: The slot is *ungreedy* so that a row with no space between the slot and the id ("19c0356d…")
#: still splits in the only way that leaves a valid id behind. The address is matched as a bare IP
#: rather than host:port because the captures contain **negative** port numbers (``:-28960``), so
#: anything insisting on a well-formed port loses the row entirely.
PB_PLAYER_RE = re.compile(
    r"""
    ^.*?                                        # junk before the row, ungreedy
    (.*?):?\s*                                  # end of the prefix, whose last character may be gone
      (?P<slot>[1-9][0-9]??)                    # PunkBuster's slot, 1-99, ungreedy
    (?:\s+|)                                    # whitespace, or none at all
      (?P<pbid>[a-f0-9]{30,32})                 # the id: 30-32 hex, not the documented 32
    [^a-f0-9].*?                                # anything that is not id, ungreedy
      (?P<ip>(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}
             (?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9]))
    .+?                                         # junk
    (?:\)\s*"|\)\s*|\s+")                       # the start of the name, however mangled
      (?P<name>.*?)"?                           # the name, whose closing quote may be gone
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: The header of a player list, which is not a row and is not worth reporting as unreadable.
LIST_HEADER = "player list:"


@dataclass(frozen=True, slots=True)
class PbPlayer:
    """One player as PunkBuster sees them."""

    #: The engine's slot — PunkBuster's, minus one. **This subtraction is the point of this class.**
    cid: str
    #: The PunkBuster id: persistent, and on some titles the only persistent thing about a player.
    pbid: str
    ip: str
    name: str
    #: PunkBuster's own 1-based slot, kept because its `PB_SV_UnBan` takes that and not the game's.
    pb_slot: str


def parse_player_list(reply: str) -> dict[str, PbPlayer]:
    """Read a ``PB_SV_PList`` reply into players, keyed by the **engine's** slot id.

    Rows whose slot does not advance are dropped, which is the classic's rule and worth keeping: a
    mangled row can parse into a plausible-looking one with a slot already seen, and filing a
    stranger's id against a connected player is how the wrong person ends up banned. The cost is
    that a genuinely out-of-order listing loses its later rows, and that trade is the right way
    round — a missing id is a player the bot cannot act on, a wrong one is a player it acts on
    incorrectly.
    """
    players: dict[str, PbPlayer] = {}
    highest = 0
    for line in reply.splitlines():
        if not line.strip():
            continue
        match = PB_PLAYER_RE.match(line)
        if match is None:
            if LIST_HEADER not in line.lower():
                log.debug("punkbuster: cannot read %r as a player row", line)
            continue
        slot = int(match["slot"])
        if slot <= highest:
            log.debug(
                "punkbuster: ignoring slot %s, which does not follow %s — a duplicate or a "
                "mangled row",
                slot,
                highest,
            )
            continue
        highest = slot
        players[str(slot - 1)] = PbPlayer(
            cid=str(slot - 1),
            pbid=match["pbid"],
            ip=match["ip"],
            name=match["name"],
            pb_slot=match["slot"],
        )
    return players


class PunkBuster:
    """The ``PB_SV_*`` verbs, over whatever RCON the bot already has.

    Not a transport: PunkBuster answers on the game server's own RCON socket, so this takes a
    ``send`` callable and knows nothing about how the command gets there.
    """

    def __init__(self, send: Callable[[str], str]) -> None:
        self._send = send

    # -- asking ------------------------------------------------------------

    @staticmethod
    def is_installed(reply: str) -> bool:
        """Whether a ``PB_SV_Ver`` reply came from a server that actually has PunkBuster.

        A server without it answers nothing, or something starting "Unknown command". The classic
        checked for both; looking for the product's own name says the same thing positively, and
        does not mistake an unrelated error message for a version string.
        """
        return VERSION_EXPECT in (reply or "").lower()

    def version(self) -> str:
        return self._send(VERSION_COMMAND).strip()

    def player_list(self) -> dict[str, PbPlayer]:
        """``PB_SV_PList`` — every player PunkBuster can see, by the engine's slot id."""
        return parse_player_list(self._send("PB_SV_PList"))

    def screenshot(self, client: Client) -> bool:
        """``PB_SV_GetSs`` — ask this player's client to send a screenshot to the PB server.

        Needs a slot: PunkBuster is asking a *connected* game client for a picture, so there is
        nothing to ask when the player has gone.
        """
        if client.cid is None:
            return False
        self._send(f'PB_SV_GetSs "{self._pb_slot(client.cid)}"')
        return True

    # -- moderation --------------------------------------------------------

    def kick(self, client: Client, minutes: int = 1, reason: str = "", private: str = "") -> bool:
        """``PB_SV_Kick`` — remove a player and keep them out for ``minutes``.

        PunkBuster's kick is not written to the ban file; it lasts until the time runs out or the
        server restarts, whichever comes first.
        """
        if client.cid is None:
            return False
        self._send(
            f'PB_SV_Kick "{self._pb_slot(client.cid)}" "{minutes}" '
            f'"{sanitize_rcon_value(reason)}" "{sanitize_rcon_value(private)}"'
        )
        return True

    def ban(self, client: Client, reason: str = "", private: str = "") -> bool:
        """``PB_SV_Ban`` for a connected player, falling back to a ban by id for one who has left.

        Two verbs because they take different things: the slot form is what PunkBuster wants while
        the player is there, and the id form is the only one that reaches somebody who is not.
        """
        if client.cid is not None:
            self._send(
                f'PB_SV_Ban "{self._pb_slot(client.cid)}" "{sanitize_rcon_value(reason)}" '
                f'"{sanitize_rcon_value(private)}"'
            )
            return True
        return self.ban_id(client, reason)

    def ban_id(self, client: Client, reason: str = "") -> bool:
        """``PB_SV_BanGuid`` — write an id straight to the permanent ban list.

        PunkBuster's own documentation says to pass ``???`` where the name or address is unknown,
        and that is done here rather than sending an empty argument: an empty one shifts every
        following argument along by one, so the reason would land in the address field.
        """
        if not client.pbid:
            return False
        name = sanitize_rcon_value(client.name) or "???"
        ip = sanitize_rcon_value(client.ip) or "???"
        self._send(f'PB_SV_BanGuid "{client.pbid}" "{name}" "{ip}" "{sanitize_rcon_value(reason)}"')
        return True

    def unban_id(self, client: Client) -> bool:
        """``PB_SV_UnBanGuid``, then write the ban file.

        The second command is not optional and it is the half that is easy to miss: the unban only
        changes the list in memory, so without `pb_sv_updbanfile` the ban comes back the next time
        the server starts — and it comes back looking like a ban nobody remembers making.
        """
        if not client.pbid:
            return False
        self._send(f'PB_SV_UnBanGuid "{client.pbid}"')
        self._send("pb_sv_updbanfile")
        return True

    def unban_slot(self, ban_slot: str) -> None:
        """``PB_SV_UnBan`` — by position in PunkBuster's *ban list*, not by player slot."""
        self._send(f'PB_SV_UnBan "{sanitize_rcon_value(ban_slot)}"')
        self._send("pb_sv_updbanfile")

    # -- name filtering ----------------------------------------------------

    def bad_name(self, grace_seconds: int, text_filter: str) -> None:
        """``PB_SV_BadName`` — disallow a substring in player names."""
        self._send(f'PB_SV_BadName "{grace_seconds}" "{sanitize_rcon_value(text_filter)}"')

    def bad_name_del(self, entry: str) -> None:
        """``PB_SV_BadNameDel`` — by position in the bad-name list."""
        self._send(f'PB_SV_BadNameDel "{sanitize_rcon_value(entry)}"')

    def setting(self, name: str, value: str) -> None:
        """Set any ``PB_SV_<Name>`` value.

        The classic did this with a ``__setattr__`` hook, so that assigning *any* attribute sent a
        command — which meant a mistyped attribute name sent a nonsense command to the anti-cheat
        and reported nothing at all. Asking for it explicitly costs one method and cannot misfire.
        """
        self._send(f"PB_SV_{name.title()} {sanitize_rcon_value(value)}")

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _pb_slot(cid: str) -> str:
        """The game's slot as PunkBuster numbers it — one higher. See :class:`PbPlayer`.

        A non-numeric slot is passed through: PunkBuster's verbs accept a name in place of a slot,
        and the engines whose "slot" is a player's name (Frostbite) are exactly the ones that would
        arrive here with one.
        """
        try:
            return str(int(cid) + 1)
        except ValueError:
            return cid


__all__ = [
    "LIST_HEADER",
    "PB_PLAYER_RE",
    "VERSION_COMMAND",
    "VERSION_EXPECT",
    "PbPlayer",
    "PunkBuster",
    "parse_player_list",
]
