"""Frostbite's structured replies — the player-info block, and the ban list.

Everything else in this bot parses text. These are word lists, and the player block is the one place
where the protocol carries its *own* schema: a count of field names, then the names, then a count of
players, then that many values per player.

    ["7", "name", "guid", "teamId", "squadId", "kills", "deaths", "score",
     "2",
     "Bravo17", "EA_1234…", "1", "0", "12", "3", "1500",
     "Bob",     "EA_5678…", "2", "1", "0",  "9", "20"]

That design is why a Battlefield server can add a column without breaking every tool that reads it —
and why this reads the names rather than assuming positions.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from b3.core.game import PlayerInfo

log = logging.getLogger(__name__)

#: Field names, mapped to what the bot calls them. Anything not listed is ignored rather than
#: guessed at; a newer game adding a column must not break the parse.
FIELDS = {
    "name": "name",
    "guid": "guid",
    "teamId": "team",
    "squadId": "squad",
    "kills": "kills",
    "deaths": "deaths",
    "score": "score",
    "ping": "ping",
    "type": "type",
}


def parse_player_block(words: list[str]) -> Iterator[PlayerInfo]:
    """Read an ``admin.listPlayers`` reply into :class:`PlayerInfo` rows.

    A malformed or truncated block yields the players it *can* read and logs the rest: a reply cut
    short mid-row should cost one player, not the whole list.

    Team and squad come through as the engine's own digits (see `PlayerInfo.team`), because the
    verbs that move a player take those and not the bot's colour names.

    Note there is no IP address here. Frostbite does not tell an admin tool where a player is
    connecting from — PunkBuster does, on a separate message stream — so IP history stays empty on
    these games unless PunkBuster is in play.
    """
    if not words:
        return
    try:
        field_count = int(words[0])
        names = words[1 : 1 + field_count]
        player_count = int(words[1 + field_count])
    except (IndexError, ValueError):
        log.warning("frostbite: unreadable player block header %r", words[:4])
        return

    offset = 2 + field_count
    for index in range(player_count):
        row = words[offset + index * field_count : offset + (index + 1) * field_count]
        if len(row) < field_count:
            log.warning(
                "frostbite: player block ended mid-row after %d of %d players", index, player_count
            )
            return
        fields = {
            FIELDS[name]: value for name, value in zip(names, row, strict=False) if name in FIELDS
        }
        name = fields.get("name", "")
        if not name:
            continue  # a row with no name identifies nobody
        yield PlayerInfo(
            # The player's *name* is their handle on this engine: `admin.kickPlayer` takes a name,
            # not a slot, so that is what has to go in `cid`.
            cid=name,
            name=name,
            guid=fields.get("guid", ""),
            ip="",  # not reported by this protocol; see the docstring
            port=0,
            ping=_as_int(fields.get("ping")),
            score=_as_int(fields.get("score")),
            # Read rather than dropped, and kept in the engine's own spelling. This is the *only*
            # place a player's squad is stated for somebody who has not changed it since the bot
            # started, and the verbs that move a player take these digits: `admin.movePlayer <name>
            # <team> <squad> true`. See `PlayerInfo.team`.
            team=fields.get("team", ""),
            squad=fields.get("squad", ""),
        )


def _as_int(value: str | None) -> int:
    try:
        return int(value) if value is not None else 0
    except ValueError:
        return 0


#: ``banList.list`` returns flat groups of five: id-type, id, ban-type, seconds/rounds, reason.
BAN_FIELDS = 5


def parse_ban_list(words: list[str]) -> list[dict[str, str]]:
    """Read a ``banList.list`` reply into one dict per ban.

    Only needed to answer "is this player still banned server-side"; the bot's own table is what it
    enforces from. Groups that do not divide evenly are dropped with a warning rather than
    misaligning every ban after them.
    """
    bans: list[dict[str, str]] = []
    if len(words) % BAN_FIELDS:
        log.warning("frostbite: ban list is not a multiple of %d words; ignoring it", BAN_FIELDS)
        return bans
    for index in range(0, len(words), BAN_FIELDS):
        id_type, target, ban_type, amount, reason = words[index : index + BAN_FIELDS]
        bans.append(
            {
                "id_type": id_type,  # "name", "ip" or "guid"
                "target": target,
                "ban_type": ban_type,  # "perm", "rounds" or "seconds"
                "amount": amount,
                "reason": reason,
            }
        )
    return bans
