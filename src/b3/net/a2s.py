"""A2S: the Source *query* protocol, over UDP on the game port.

The prerequisite the 2026-07-29 audit turned up — the classic tree carried
``b3/lib/sourcelib/SourceQuery.py`` and used it from every Source title to read the hostname, the map
and the player count, and nothing here had an equivalent. It is a separate protocol from RCON, on a
separate socket, and it needs no password: anyone may ask.

Three questions, each a single request byte:

* ``A2S_INFO`` (``T``) — hostname, map, game directory, player and bot counts, version;
* ``A2S_PLAYER`` (``U``) — the connected players, with score and time connected;
* ``A2S_RULES`` (``V``) — the server's public cvars.

**Every one of them now takes a challenge, and this is where the classic code stopped working.** In
December 2020 Valve added a challenge step to ``A2S_INFO`` as an amplification-attack fix: a server
answers the first request with ``S2C_CHALLENGE`` (``A``) and a four-byte number, and only answers the
question once that number is sent back. ``A2S_PLAYER`` and ``A2S_RULES`` always worked this way; the
classic client handled the challenge for those two and not for info, because in 2010 info did not
need one. So its ``info()`` against any current server returns nothing at all — the reply is there,
it is just a challenge nobody answers. Both paths here send, and re-send with, whatever challenge
they are given.

**Replies can be split across datagrams** (a long ``A2S_RULES`` almost always is), and a split reply
may additionally be bzip2-compressed. Reassembly is implemented; compression is *detected and
reported* rather than guessed at, because a mangled cvar table is worse than a missing one and no
server seen here compresses.
"""

from __future__ import annotations

import bz2
import logging
import socket
import struct
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: A single (unsplit) reply carries this as its first four bytes; a fragment carries -2.
WHOLE = -1
SPLIT = -2

#: The value to send when no challenge is held yet.
NO_CHALLENGE = -1

# Request bytes and the reply byte each one is answered with.
A2S_INFO = b"T"
A2S_INFO_PAYLOAD = b"Source Engine Query\x00"
A2S_INFO_REPLY = 0x49
A2S_PLAYER = b"U"
A2S_PLAYER_REPLY = 0x44
A2S_RULES = b"V"
A2S_RULES_REPLY = 0x45
#: "Here is a challenge, ask again with it."
S2C_CHALLENGE_REPLY = 0x41

#: A datagram never exceeds this on this protocol.
PACKET_SIZE = 1400

#: The Ship reports three extra fields in the middle of an info reply that no other game does.
THE_SHIP_APPID = 2400

#: How many times to re-ask after being handed a challenge. One is the documented flow; a second is
#: allowed for a server that rotates challenges between the two datagrams, and past that something
#: is wrong and looping would just be a flood.
MAX_CHALLENGE_RETRIES = 2


class A2SError(Exception):
    """The server did not answer, or answered something this cannot read."""


class A2SCompressedError(A2SError):
    """The reply was compressed. Reassembly works; decompression is not verified against a server."""


class _Reader:
    """Sequential reader for the primitives this protocol is built from.

    Every getter raises :class:`A2SError` past the end of the buffer rather than returning a partial
    value, because a truncated reply is common here (a 32-slot server's player list is documented to
    arrive incomplete) and the difference between "no more players" and "half a player" matters.
    """

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos

    def rest(self) -> bytes:
        """Everything not yet read — how a split reply's payload is taken after its header."""
        return self._data[self._pos :]

    def _take(self, count: int) -> bytes:
        if self.remaining < count:
            raise A2SError(f"reply ended after {len(self._data)} bytes, needed {count} more")
        chunk = self._data[self._pos : self._pos + count]
        self._pos += count
        return chunk

    def byte(self) -> int:
        return self._take(1)[0]

    def short(self) -> int:
        return int(struct.unpack("<h", self._take(2))[0])

    def ushort(self) -> int:
        """An *unsigned* 16-bit field, which is what several of them really are.

        Valve's own documentation calls them all "short", and reading them signed is wrong in two
        places that matter. A **port** above 32767 is entirely ordinary — an ephemeral or
        second-instance server port — and comes back negative. The **app id** is worse: it is the
        Steam id truncated to sixteen bits, so Insurgency's 222880 arrives as 26272 and a game whose
        low word has the top bit set arrives negative, which then fails the comparison that decides
        whether to read The Ship's three extra fields, and *that* desynchronises the rest of the
        reply. The classic client read both signed.
        """
        return int(struct.unpack("<H", self._take(2))[0])

    def long(self) -> int:
        return int(struct.unpack("<i", self._take(4))[0])

    def long_long(self) -> int:
        return int(struct.unpack("<Q", self._take(8))[0])

    def single(self) -> float:
        return float(struct.unpack("<f", self._take(4))[0])

    def string(self) -> str:
        end = self._data.find(b"\x00", self._pos)
        if end == -1:
            raise A2SError("unterminated string in reply")
        value = self._data[self._pos : end].decode("utf-8", "replace")
        self._pos = end + 1
        return value


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """An ``A2S_INFO`` reply."""

    hostname: str = ""
    map_name: str = ""
    game_dir: str = ""
    game_desc: str = ""
    app_id: int = 0
    players: int = 0
    max_players: int = 0
    bots: int = 0
    server_type: str = ""
    environment: str = ""
    visibility: int = 0
    vac: int = 0
    version: str = ""
    #: Round-trip time in seconds, which is the only ping figure available for the server itself.
    ping: float = 0.0
    #: Present only when the server sends the extra-data flag for them.
    port: int | None = None
    steam_id: int | None = None
    keywords: str = ""


@dataclass(frozen=True, slots=True)
class PlayerEntry:
    """One row of an ``A2S_PLAYER`` reply.

    Note what is *not* here: no Steam id, and no IP. This protocol names players and nothing more, so
    it can refresh a score or a ping but it can never identify anybody — that is the log's job, and
    the reason the roster is not built from this.
    """

    index: int
    name: str
    score: int
    duration: float = 0.0


@dataclass
class _Fragment:
    total: int = 0
    compressed: bool = False
    parts: dict[int, bytes] = field(default_factory=dict)


class A2SClient:
    """Ask a Source server the three public questions, on its game port.

    One socket per call, deliberately: this is a request/response protocol with no session, the calls
    are minutes apart (a status sync, an operator's `!status`), and a long-lived UDP socket here would
    only accumulate stale datagrams to be mistaken for the next answer.
    """

    def __init__(self, host: str, port: int = 27015, *, timeout: float = 2.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout

    # -- the three questions ----------------------------------------------

    def info(self) -> ServerInfo:
        """Hostname, map, player counts. Answers the challenge if the server asks for one."""
        before = time.monotonic()
        reader = self._ask(A2S_INFO, A2S_INFO_REPLY, payload=A2S_INFO_PAYLOAD)
        ping = time.monotonic() - before

        protocol = reader.byte()
        hostname = reader.string()
        map_name = reader.string()
        game_dir = reader.string()
        game_desc = reader.string()
        app_id = reader.ushort()
        players = reader.byte()
        max_players = reader.byte()
        bots = reader.byte()
        server_type = chr(reader.byte())
        environment = chr(reader.byte())
        visibility = reader.byte()
        vac = reader.byte()
        if app_id == THE_SHIP_APPID:
            reader.byte()  # game mode
            reader.byte()  # witnesses needed
            reader.byte()  # duration
        version = reader.string()
        log.debug("a2s: %s:%s speaks protocol %d", self.host, self.port, protocol)

        # The extra-data flag and everything behind it is optional, and a server that omits it is
        # not in error. Anything unreadable past this point costs the optional fields only.
        port: int | None = None
        steam_id: int | None = None
        keywords = ""
        try:
            flags = reader.byte()
            if flags & 0x80:
                port = reader.ushort()
            if flags & 0x10:
                steam_id = reader.long_long()
            if flags & 0x40:
                reader.ushort()  # spectator port
                reader.string()  # spectator server name
            if flags & 0x20:
                keywords = reader.string()
        except A2SError:
            log.debug("a2s: %s:%s sent no extra data block", self.host, self.port)

        return ServerInfo(
            hostname=hostname,
            map_name=map_name,
            game_dir=game_dir,
            game_desc=game_desc,
            app_id=app_id,
            players=players,
            max_players=max_players,
            bots=bots,
            server_type=server_type,
            environment=environment,
            visibility=visibility,
            vac=vac,
            version=version,
            ping=ping,
            port=port,
            steam_id=steam_id,
            keywords=keywords,
        )

    def players(self) -> list[PlayerEntry]:
        """The connected players, by name and score.

        A truncated reply is normal on a well-filled server — the protocol is documented to send an
        incomplete list rather than split it — so the rows that did arrive are returned and the tail
        is not treated as an error.
        """
        reader = self._ask(A2S_PLAYER, A2S_PLAYER_REPLY)
        count = reader.byte()
        found: list[PlayerEntry] = []
        for _ in range(count):
            try:
                found.append(
                    PlayerEntry(
                        index=reader.byte(),
                        name=reader.string(),
                        score=reader.long(),
                        duration=reader.single(),
                    )
                )
            except A2SError:
                log.debug(
                    "a2s: %s:%s said %d players and sent %d", self.host, self.port, count, len(found)
                )
                break
        return found

    def rules(self) -> dict[str, str]:
        """The server's public cvars.

        The stated count is read but not relied on, for the same reason as above: some builds
        under-report it. Reading until the buffer runs out is what the classic client did too.
        """
        reader = self._ask(A2S_RULES, A2S_RULES_REPLY)
        stated = reader.ushort()
        found: dict[str, str] = {}
        while reader.remaining > 1:
            try:
                key = reader.string()
                found[key] = reader.string()
            except A2SError:
                break
        if len(found) != stated:
            log.debug("a2s: %s:%s stated %d rules and sent %d", self.host, self.port, stated, len(found))
        return found

    # -- the wire ----------------------------------------------------------

    def _ask(self, request: bytes, expect: int, *, payload: bytes = b"") -> _Reader:
        """Send one question, answering a challenge if the server demands one, and return its reply."""
        challenge = NO_CHALLENGE
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(self.timeout)
            try:
                sock.connect((self.host, self.port))
            except OSError as exc:
                raise A2SError(f"cannot reach {self.host}:{self.port}: {exc}") from exc

            for _ in range(MAX_CHALLENGE_RETRIES + 1):
                self._send(sock, request + payload + struct.pack("<i", challenge))
                reader = self._receive(sock)
                kind = reader.byte()
                if kind == expect:
                    return reader
                if kind == S2C_CHALLENGE_REPLY:
                    challenge = reader.long()
                    continue
                raise A2SError(f"expected reply type {expect:#x}, got {kind:#x}")
        raise A2SError(f"{self.host}:{self.port} kept asking for a new challenge")

    @staticmethod
    def _send(sock: socket.socket, body: bytes) -> None:
        try:
            sock.send(struct.pack("<i", WHOLE) + body)
        except OSError as exc:
            raise A2SError(f"send failed: {exc}") from exc

    def _receive(self, sock: socket.socket) -> _Reader:
        """Read one logical reply, reassembling it if the server split it across datagrams."""
        fragment: _Fragment | None = None
        fragment_id = 0
        deadline = time.monotonic() + self.timeout

        while time.monotonic() < deadline:
            try:
                datagram = sock.recv(PACKET_SIZE * 2)
            except TimeoutError as exc:
                raise A2SError(f"no reply from {self.host}:{self.port}") from exc
            except OSError as exc:
                raise A2SError(f"read failed: {exc}") from exc

            reader = _Reader(datagram)
            kind = reader.long()
            if kind == WHOLE:
                return reader
            if kind != SPLIT:
                raise A2SError(f"reply header is neither whole nor split: {kind}")

            this_id = reader.long()
            total = reader.byte()
            number = reader.byte()
            reader.ushort()  # per-fragment size, which reassembly does not need
            if fragment is None:
                fragment_id = this_id
                # The top bit of the id says the payload is bzip2-compressed.
                fragment = _Fragment(total=total, compressed=bool(this_id & 0x8000_0000))
                if fragment.compressed:
                    reader.long()  # decompressed size
                    reader.long()  # crc32
            elif this_id != fragment_id:
                raise A2SError("fragments from two different replies arrived together")
            fragment.parts[number] = reader.rest()

            if len(fragment.parts) == fragment.total:
                joined = b"".join(fragment.parts[i] for i in sorted(fragment.parts))
                if fragment.compressed:
                    raise A2SCompressedError(
                        f"{self.host}:{self.port} sent a bzip2-compressed reply, which is "
                        "reassembled here but not decompressed; report this with the game and "
                        "version so it can be verified against a real server"
                    )
                whole = _Reader(joined)
                if whole.long() != WHOLE:
                    raise A2SError("reassembled reply does not start with a whole-packet header")
                return whole
        raise A2SError(f"{self.host}:{self.port} never completed a split reply")


def decompress(payload: bytes) -> bytes:
    """Decompress a bzip2 A2S payload.

    Kept out of :meth:`A2SClient._receive` on purpose: no server observed here compresses its
    replies, so calling this on a live reply has never been verified. It exists so that when one
    does turn up, the fix is to call it rather than to work out the format.
    """
    return bz2.decompress(payload)


__all__ = [
    "A2S_INFO",
    "A2S_PLAYER",
    "A2S_RULES",
    "MAX_CHALLENGE_RETRIES",
    "NO_CHALLENGE",
    "PACKET_SIZE",
    "SPLIT",
    "WHOLE",
    "A2SClient",
    "A2SCompressedError",
    "A2SError",
    "PlayerEntry",
    "ServerInfo",
    "decompress",
]
