"""Frostbite RCON — Battlefield and Medal of Honor, over a binary TCP protocol.

The third transport, and the least like a game log of any of them. Nothing here is text: a Frostbite
server speaks in **word lists** over a length-prefixed TCP stream, and like BattlEye it *pushes*
events down the same connection it takes commands on.

    <sequence: uint32> <total size: uint32> <word count: uint32> then, per word:
    <length: uint32> <bytes> 0x00

The sequence field is three things at once — the top bit says whether the packet came from the
server, the next bit whether it is a response, and the low 30 bits are the number. Every packet the
*server* originates must be answered with an ``OK`` response carrying the same sequence, or it keeps
resending.

:class:`FrostbiteClient` follows the shape BattlEye established (see :mod:`b3.net.battleye`): it
satisfies both ``RconClient`` and ``LogSource``, so the runtime keeps polling a log source and never
learns that the log and the RCON are one TCP connection. Single-threaded for the same reason, and
with the same reconnect-with-back-off, because a Battlefield server restarts between maps.

Two things are genuinely new here and worth knowing before reading on.

**Commands are word lists, not strings.** The rest of the bot composes a command as a string from a
profile template, so this client splits that string into words the way a shell would — respecting
double quotes. That is safe rather than lucky: every value substituted into a template has been
through :func:`b3.core.util.sanitize_rcon_value`, which removes quotes, so ``"%(reason)s"`` can only
ever produce one well-formed word.

**Events are word lists too**, and a word may contain any character, so there is no delimiter that
could safely join them into a "line". They are handed up as JSON instead — reversible, unambiguous,
and readable in a log. :class:`b3.parsers.frostbite.parser.FbParser` reads them back.

**Login is a challenge-response.** ``login.hashed`` with no argument returns a salt; the answer is
the uppercase hex of ``md5(salt_bytes + password)``. The password itself never crosses the wire,
which is why the plain-text variant is not offered here even though the protocol has one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import select
import shlex
import socket
import struct
from collections import deque
from dataclasses import dataclass

from b3.core.clock import Clock, SystemClock

log = logging.getLogger(__name__)

#: Header bit flags packed into the sequence field.
FROM_SERVER = 0x80000000
IS_RESPONSE = 0x40000000
SEQUENCE_MASK = 0x3FFFFFFF

#: Bytes of header before the first word: sequence, total size, word count.
HEADER_SIZE = 12

#: What a Frostbite server says when a command worked. Anything else is the failure's name.
OK = "OK"

#: Replies that mean "the server understood but refused", as opposed to a broken connection. Ranked
#: and official servers disallow a good deal of what an admin bot would like to do.
REFUSALS = frozenset({"CommandDisallowedOnRanked", "CommandDisallowedOnOfficial", "LogInRequired"})

#: A Battlefield server drops an idle connection, so say something occasionally.
KEEPALIVE_SECONDS = 25.0

#: Seconds between chat lines, and the reason is **the display, not the protocol**. A Frostbite
#: server acknowledges every `admin.say` and TCP will not reorder them, so nothing is lost on the
#: wire — but the game shows each line for a moment and a burst replaces the earlier ones before
#: anybody can read them, so a wrapped four-line `!help` arrives as its last line. The classic bot's
#: `_message_delay`, and its two generations disagree: Frostbite 2 used 0.8s and Frostbite 1 used a
#: full 2s, its chat area being smaller.
SAY_INTERVAL = 0.8
SAY_INTERVAL_FROSTBITE1 = 2.0

#: Liveness and reconnect, matching b3.net.battleye and the remote log sources.
DEAD_AFTER = 90.0
RETRY_INTERVAL = 5.0
RETRY_MAX = 300.0

#: The cheapest question that proves the server is still answering.
PROBE_COMMAND = "version"

#: Runaway guard on the receive loops — see the same constant in b3.net.battleye for why the loops
#: are bounded by receives rather than by the clock.
MAX_PUMP = 64

#: Runaway guard on paging through the map list. A rotation this long is not a real one; a server
#: answering every offset with the same page is, and without a bound that is an endless loop.
MAX_ROTATION = 1000

#: What to ask for when the server will not say what the current map is being played as. Conquest is
#: the mode every Battlefield title in this family ships with, and `mapList.add` rejects an empty one
#: outright — so a wrong guess degrades to "the map loaded in the wrong mode", where an empty string
#: degrades to "the map did not load".
DEFAULT_GAMEMODE = "ConquestLarge0"
DEFAULT_ROUNDS = "2"


class FrostbiteError(Exception):
    """A Frostbite connection or command failed."""


class FrostbiteAuthError(FrostbiteError):
    """The server rejected the RCON password."""


class FrostbiteTimeout(FrostbiteError):
    """No reply arrived in time."""


class FrostbiteCommandError(FrostbiteError):
    """The server understood the command and refused it, or did not recognise it.

    Carries the server's own word for what was wrong (``UnknownCommand``,
    ``CommandDisallowedOnRanked``, ``InvalidPlayerName`` …), which is more use to an admin than
    "command failed".
    """

    def __init__(self, command: str, words: list[str]) -> None:
        self.command = command
        self.words = words
        super().__init__(f"{command!r} failed: {' '.join(words) or 'no reason given'}")


@dataclass(frozen=True, slots=True)
class Packet:
    from_server: bool
    is_response: bool
    sequence: int
    words: list[str]


def encode_words(words: list[str]) -> bytes:
    out = bytearray()
    for word in words:
        raw = word.encode("utf-8", "replace")
        out += struct.pack("<I", len(raw)) + raw + b"\x00"
    return bytes(out)


def encode(
    words: list[str], sequence: int, *, from_server: bool = False, response: bool = False
) -> bytes:
    """Frame a packet. Size counts the header, which is what the reader uses to find the end."""
    header = sequence & SEQUENCE_MASK
    if from_server:
        header |= FROM_SERVER
    if response:
        header |= IS_RESPONSE
    body = encode_words(words)
    return struct.pack("<III", header, len(body) + HEADER_SIZE, len(words)) + body


def packet_length(buffer: bytes) -> int | None:
    """How long the packet at the front of ``buffer`` is, or None if that is not yet knowable.

    TCP delivers a stream, not messages: a read can hand over half a packet or three and a half. The
    size field is the only thing that says where one ends.
    """
    if len(buffer) < 8:
        return None
    size = struct.unpack("<I", buffer[4:8])[0]
    if size < HEADER_SIZE:
        raise FrostbiteError(f"nonsensical packet size {size}")
    return size if len(buffer) >= size else None


def decode(packet: bytes) -> Packet:
    """Unframe one complete packet."""
    if len(packet) < HEADER_SIZE:
        raise FrostbiteError("packet shorter than its own header")
    header, size, count = struct.unpack("<III", packet[:HEADER_SIZE])
    words: list[str] = []
    offset = HEADER_SIZE
    for _ in range(count):
        if offset + 4 > len(packet):
            break  # a truncated packet: keep the words that did arrive
        length = struct.unpack("<I", packet[offset : offset + 4])[0]
        start = offset + 4
        words.append(packet[start : start + length].decode("utf-8", "replace"))
        offset = start + length + 1  # +1 for the terminating NUL
    return Packet(
        from_server=bool(header & FROM_SERVER),
        is_response=bool(header & IS_RESPONSE),
        sequence=header & SEQUENCE_MASK,
        words=words,
    )


def password_hash(salt: bytes, password: str) -> str:
    """The answer to a ``login.hashed`` challenge: uppercase hex of md5(salt + password)."""
    digest = hashlib.md5(salt + password.encode("utf-8", "replace")).digest()  # noqa: S324
    return digest.hex().upper()


def split_command(cmd: str) -> list[str]:
    """Turn a composed command string into the word list the protocol wants.

    Shell-like, so a quoted template value stays one word: ``admin.say "hello there" all`` is three
    words, not four. Safe because every substituted value has had quotes stripped by
    :func:`b3.core.util.sanitize_rcon_value` before it got here.
    """
    try:
        return shlex.split(cmd)
    except ValueError:
        # An unbalanced quote should not take the bot down; send it as plain words and let the
        # server say what it thinks.
        log.warning("frostbite: could not split %r into words; sending it whitespace-split", cmd)
        return cmd.split()


def parse_map_list(words: list[str]) -> list[str]:
    """Read a ``mapList.list`` reply (Frostbite 2) into map names, in rotation order.

    The block states its own shape: ``<number of maps> <words per map>`` and then that many words
    each, of which the first is the name. DICE documented the second field as future-proofing — extra
    words may be added after the first three — so it is *read* rather than assumed to be three, which
    is the difference between this surviving a patch and silently misreading every second map.

    A reply that does not start with two numbers is treated as the Frostbite 1 form: a flat list of
    level names with no header at all.
    """
    if len(words) < 2 or not (words[0].isdigit() and words[1].isdigit()):
        return [w for w in words if w]
    count, stride = int(words[0]), int(words[1])
    if stride < 1:
        return []
    rows = words[2:]
    return [rows[i * stride] for i in range(count) if i * stride < len(rows)]


class FrostbiteClient:
    """One TCP connection, serving as both the RCON client and the source of game events."""

    blocking = True

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        timeout: float = 3.0,
        poll_interval: float = 0.2,
        keepalive: float = KEEPALIVE_SECONDS,
        dead_after: float = DEAD_AFTER,
        retry_interval: float = RETRY_INTERVAL,
        retry_max: float = RETRY_MAX,
        legacy_maplist: bool = False,
        say_interval: float | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._addr = (host, port)
        self._password = password
        self._timeout = timeout
        #: Bad Company 2 and the 2010 Medal of Honor. The two Frostbite generations share every verb
        #: the bot uses for chat, kicks and bans — which is why one profile serves all six titles —
        #: but they do **not** share the map list, and this is the only place that difference shows.
        #: Frostbite 1 holds a flat list of level names and moves with `admin.runNextRound`;
        #: Frostbite 2 holds (map, gamemode, rounds) triplets and moves with `mapList.runNextRound`.
        self._legacy_maplist = legacy_maplist
        #: Seconds between chat lines. Defaults to the pace this generation's chat area can be read
        #: at — see SAY_INTERVAL — since the two differ and the caller already says which it is.
        self._say_interval = (
            say_interval
            if say_interval is not None
            else (SAY_INTERVAL_FROSTBITE1 if legacy_maplist else SAY_INTERVAL)
        )
        self._last_say = 0.0
        #: Chat waiting to be released at `say_interval`.
        self._outbox: deque[str] = deque()
        self.poll_interval = poll_interval
        self._keepalive = keepalive
        self._dead_after = dead_after
        self._retry_interval = retry_interval
        self._retry_max = retry_max
        self._clock = clock or SystemClock()

        self._sock: socket.socket | None = None
        self._seq = 0
        self._buffer = b""
        self._last_send = 0.0
        self._last_heard = 0.0
        self._retry_at = 0.0
        self._failures = 0
        #: Events received but not yet handed to the bot, already JSON-encoded.
        self._events: deque[str] = deque()

    # -- connection --------------------------------------------------------

    def open(self) -> None:
        """Connect, authenticate, and ask to be sent events.

        Raises, so a wrong password or port fails at startup. A connection that drops later is
        handled by :meth:`read_lines`.
        """
        self._connect()

    def _connect(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self._timeout)
        try:
            sock.connect(self._addr)
        except OSError as exc:
            sock.close()
            raise FrostbiteTimeout(f"cannot reach {self._addr[0]}:{self._addr[1]}: {exc}") from exc
        self._sock = sock
        self._seq = 0
        self._buffer = b""
        self._last_send = self._clock.now()
        self._last_heard = self._clock.now()

        try:
            salt_words = self._command_words(["login.hashed"])
            if not salt_words:
                raise FrostbiteAuthError("the server offered no login salt")
            salt = bytes.fromhex(salt_words[0])
            self._command_words(["login.hashed", password_hash(salt, self._password)])
            # Nothing arrives at all until this is asked for: the events are opt-in.
            self._command_words(["admin.eventsEnabled", "true"])
        except FrostbiteCommandError as exc:
            self.close()
            raise FrostbiteAuthError(
                f"the server rejected the rcon password ({exc.words})"
            ) from exc
        except (ValueError, FrostbiteError):
            self.close()
            raise

        self._failures = 0
        log.info("frostbite: logged in to %s:%d and subscribed to events", *self._addr)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- the LogSource half -------------------------------------------------

    def read_lines(self) -> list[str]:
        """Every event since last time, as JSON word lists, plus the connection's housekeeping."""
        if not self.connected and not self._try_reconnect():
            return []
        try:
            self._drain()
            self._flush_outbox()
            self._maybe_keepalive()
        except FrostbiteError as exc:
            self._lost(f"{exc}")
            return []
        if self._silent_too_long() and not self._still_there():
            self._lost(f"no answer to `{PROBE_COMMAND}` after {self._dead_after:.0f}s of silence")
        lines, self._events = list(self._events), deque()
        return lines

    # -- the RconClient half ------------------------------------------------

    def command(self, cmd: str) -> str:
        """Send a command; return its reply as one line of text.

        The reply is a word list, and the bot's callers expect a string, so the words are joined with
        spaces — which is right for the ``version``/``vars.*`` style answers anything generic asks
        for. Callers that need the structure (the player list) use :meth:`get_players`, which keeps
        the words intact.
        """
        # Anything queued goes first and unpaced. An admin's `!kick` must not wait behind a queue of
        # announcements, and the announcement explaining the kick must not arrive after it.
        self._flush_outbox(paced=False)
        return " ".join(self._command_words(split_command(cmd)))

    def write(self, cmd: str) -> None:
        """Queue a command with no reply expected — chat, paced.

        **Nothing is lost on the wire here, unlike BattlEye.** A Frostbite server acknowledges every
        `admin.say`, and TCP will not reorder them. The pacing is about the *display*: the game shows
        a chat line for a moment, so a burst of them replaces each earlier line before it can be
        read, and a wrapped four-line `!help` arrives as its last line. The classic bot paced it for
        exactly this reason (`_message_delay`, with a per-generation value).

        Queued rather than slept on, so the wait never lands on the event loop; the next
        `read_lines` releases the following line.
        """
        self._outbox.append(cmd)
        self._flush_outbox()

    def get_players(self) -> list[object]:
        """The live player table, from ``admin.listPlayers all``.

        Kept apart from :meth:`command` because this reply is a *structured* block — a list of field
        names followed by that many values per player — and flattening it to text would throw away
        the only thing that makes it readable.
        """
        from b3.parsers.frostbite.status import parse_player_block

        return list(parse_player_block(self._command_words(["admin.listPlayers", "all"])))

    # -- map control --------------------------------------------------------
    #
    # Here rather than as a profile template because a Frostbite map change is not one command and
    # not expressible as a fixed string: the map has to be *put into the rotation at an index* and the
    # server then pointed at that index, and the index is arithmetic over a reply. The bot reaches
    # these through the same duck-typed seam it uses for `get_players` and BattlEye's `remove_ban`.

    def get_maps(self) -> list[str]:
        """The rotation, in order. Paged, because a long rotation does not arrive in one reply.

        Frostbite 2 answers `mapList.list <offset>` with a bounded page and expects to be asked
        again; the classic bot looped until a page came back empty, and so does this. Frostbite 1
        takes no offset and answers with the whole flat list at once, so the first page is all of it.
        """
        if self._legacy_maplist:
            return parse_map_list(self._command_words(["mapList.list"]))
        maps: list[str] = []
        while True:
            page = parse_map_list(self._command_words(["mapList.list", str(len(maps))]))
            if not page:
                return maps
            maps.extend(page)
            if len(maps) > MAX_ROTATION:
                # A server that answers every offset with the same page would loop here forever,
                # holding the event loop's worker thread. Stopping with what we have is wrong in a way
                # an admin can see (`!maps` is short); never returning is not.
                log.warning(
                    "frostbite: mapList.list is still answering past %d maps; giving up on the rest",
                    MAX_ROTATION,
                )
                return maps

    def change_map(self, name: str, extras: dict[str, str] | None = None) -> None:
        """Load ``name`` next and end the current round, so it takes effect now.

        ``extras`` may carry ``gamemode`` and ``rounds``, which only Frostbite 2 can express — its
        map list stores them per entry. On Frostbite 1 they are dropped with a word about it rather
        than silently, because an admin who typed a gamemode and got none should be told which half
        of their command the engine cannot do.

        The map is *added* rather than searched for. A map already in the rotation would work too,
        but adding it unconditionally is what makes `!map` able to load a map that is installed and
        not currently rotated, which is the case an admin actually reaches for.
        """
        values = extras or {}
        if self._legacy_maplist:
            if values:
                log.warning(
                    "frostbite: this title's map list holds only level names, so %s cannot be set "
                    "with the map; loading %s with the server's current settings",
                    ", ".join(sorted(values)),
                    name,
                )
            index = self._next_index()
            self._command_words(["mapList.insert", str(index), name])
            self._command_words(["mapList.nextLevelIndex", str(index)])
            self._command_words(["admin.runNextRound"])
            return
        rounds = values.get("rounds") or self._current_rounds()
        gamemode = values.get("gamemode") or self._current_gamemode()
        index = self._next_index()
        self._command_words(["mapList.add", name, gamemode, str(rounds), str(index)])
        self._command_words(["mapList.setNextMapIndex", str(index)])
        self._command_words(["mapList.runNextRound"])

    def _next_index(self) -> int:
        """The slot just after the map being played, which is where a `!map` target belongs.

        Falls back to appending at the end of the rotation when the server will not say where it is.
        That is the safe direction to be wrong in: the map still loads, because the index is pointed
        at explicitly straight afterwards, and the rotation is left in an order that makes sense.
        """
        try:
            if self._legacy_maplist:
                current = self._command_words(["mapList.nextLevelIndex"])
                return int(current[0]) if current and current[0].isdigit() else len(self.get_maps())
            indices = self._command_words(["mapList.getMapIndices"])
        except FrostbiteError as exc:
            log.debug("frostbite: could not read the map indices (%s); appending instead", exc)
            return len(self.get_maps())
        if indices and indices[0].isdigit():
            return int(indices[0]) + 1
        return len(self.get_maps())

    def _current_gamemode(self) -> str:
        """The gamemode to keep when the admin did not name one.

        `mapList.getMapIndices` says *where* the current map is, and `mapList.list` says what mode it
        is being played in, so the answer is the two together. An empty answer would make
        `mapList.add` reject the whole command, so a last-resort default is better than a certain
        failure — and on any server that answers either question this is never reached.
        """
        try:
            indices = self._command_words(["mapList.getMapIndices"])
            if indices and indices[0].isdigit():
                words = self._command_words(["mapList.list", indices[0]])
                if len(words) >= 4 and words[1].isdigit() and int(words[1]) >= 2:
                    return words[3]
        except FrostbiteError as exc:
            log.debug("frostbite: could not read the current gamemode (%s)", exc)
        return DEFAULT_GAMEMODE

    def _current_rounds(self) -> str:
        """How many rounds the current entry is set to, or the engine's own default."""
        try:
            indices = self._command_words(["mapList.getMapIndices"])
            if indices and indices[0].isdigit():
                words = self._command_words(["mapList.list", indices[0]])
                if len(words) >= 5 and words[1].isdigit() and int(words[1]) >= 3:
                    return words[4]
        except FrostbiteError as exc:
            log.debug("frostbite: could not read the current round count (%s)", exc)
        return DEFAULT_ROUNDS

    # -- internals ---------------------------------------------------------

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise FrostbiteError("not connected to the Frostbite server")
        return self._sock

    def _next_sequence(self) -> int:
        sequence, self._seq = self._seq, (self._seq + 1) & SEQUENCE_MASK
        return sequence

    def _send(self, packet: bytes) -> None:
        sock = self._require_socket()
        try:
            sock.sendall(packet)
        except OSError as exc:
            raise FrostbiteError(f"send failed: {exc}") from exc
        self._last_send = self._clock.now()

    def _command_words(self, words: list[str]) -> list[str]:
        """Send a word list and wait for its response, returning the words after ``OK``."""
        if not words:
            return []
        sequence = self._next_sequence()
        self._send(encode(words, sequence))
        for _ in range(MAX_PUMP):
            packet = self._receive()
            if packet is None:
                break  # the socket timed out: no reply is coming
            if packet.from_server:
                self._handle_server_packet(packet)
                continue
            if not packet.is_response or packet.sequence != sequence:
                continue
            reply = packet.words
            if not reply:
                raise FrostbiteCommandError(words[0], [])
            if reply[0] != OK:
                raise FrostbiteCommandError(words[0], reply)
            return reply[1:]
        raise FrostbiteTimeout(f"no reply to {' '.join(words)!r}")

    def _receive(self) -> Packet | None:
        """The next complete packet, or None when the socket goes quiet.

        The buffering is the point: TCP hands over a byte stream, so a read may contain part of a
        packet, several packets, or both. Only the size field says where each one ends.
        """
        sock = self._require_socket()
        while True:
            try:
                size = packet_length(self._buffer)
            except FrostbiteError:
                # The stream is out of step and cannot be resynchronised — there are no framing
                # markers to search for. Better to drop the connection and log in again than to
                # keep handing garbage to the parser.
                self._buffer = b""
                raise
            if size is not None:
                packet, self._buffer = self._buffer[:size], self._buffer[size:]
                self._last_heard = self._clock.now()
                return decode(packet)
            try:
                chunk = sock.recv(8192)
            except (TimeoutError, BlockingIOError):
                return None
            except OSError as exc:
                raise FrostbiteError(f"read failed: {exc}") from exc
            if not chunk:
                raise FrostbiteError("the server closed the connection")
            self._buffer += chunk

    def _drain(self) -> None:
        """Read everything already waiting, without blocking."""
        sock = self._require_socket()
        while packet_length(self._buffer) is not None or select.select([sock], [], [], 0)[0]:
            packet = self._receive()
            if packet is None:
                return
            if packet.from_server:
                self._handle_server_packet(packet)
            # A response nobody is waiting for is the acknowledgement of a `write`; dropping it here
            # is what stops it being handed to the next caller as their answer.

    def _handle_server_packet(self, packet: Packet) -> None:
        """Acknowledge an event and queue it. Unacknowledged, the server sends it again."""
        if not packet.is_response:
            self._send(encode([OK], packet.sequence, from_server=True, response=True))
        if not packet.words:
            return
        # JSON, because a word may contain any character — including whatever delimiter would
        # otherwise be used to join these into a line. See the module docstring.
        self._events.append(json.dumps(packet.words))

    def _silent_too_long(self) -> bool:
        return self._clock.now() - self._last_heard > self._dead_after

    def _still_there(self) -> bool:
        """Ask, rather than assume: an empty server between rounds is quiet but perfectly healthy."""
        try:
            self._command_words([PROBE_COMMAND])
        except FrostbiteCommandError:
            return True  # it answered, even to refuse — which is all this asks
        except FrostbiteError:
            return False
        return True

    def _flush_outbox(self, paced: bool = True) -> None:
        """Release queued chat, one line per `say_interval`.

        ``paced=False`` empties it outright, which is what a real command does before sending: an
        admin typing `!kick` must not wait behind a queue of announcements, and the ordering matters
        the other way round too — the kick should not land before the line explaining it.
        """
        now = self._clock.now()
        while self._outbox:
            if paced and now - self._last_say < self._say_interval:
                return
            command = self._outbox.popleft()
            self._last_say = now
            try:
                self._command_words(split_command(command))
            except FrostbiteCommandError as exc:
                # Chat is not worth raising over — the caller is a plugin announcing something, not
                # an admin waiting for a verdict — but it must not vanish silently either.
                log.warning("frostbite: %s", exc)

    def _maybe_keepalive(self) -> None:
        if self._clock.now() - self._last_send >= self._keepalive:
            try:
                self._command_words([PROBE_COMMAND])
            except FrostbiteCommandError:
                pass  # answered, which is the whole purpose

    def _lost(self, why: str) -> None:
        log.warning("frostbite: connection to %s:%d lost — %s", *self._addr, why)
        self.close()
        self._buffer = b""
        self._failures += 1
        delay = min(self._retry_interval * (2 ** (self._failures - 1)), self._retry_max)
        self._retry_at = self._clock.now() + delay
        log.info("frostbite: reconnecting in %.0fs", delay)

    def _try_reconnect(self) -> bool:
        if self._clock.now() < self._retry_at:
            return False
        try:
            self._connect()
        except FrostbiteAuthError:
            # It will not fix itself; stop hammering and say so plainly.
            self._failures = 99
            self._retry_at = self._clock.now() + self._retry_max
            log.error(
                "frostbite: %s:%d rejected the rcon password; fix server.rcon_password and restart",
                *self._addr,
            )
            return False
        except FrostbiteError as exc:
            self._failures += 1
            delay = min(self._retry_interval * (2 ** (self._failures - 1)), self._retry_max)
            self._retry_at = self._clock.now() + delay
            log.debug("frostbite: reconnect failed (%s); next try in %.0fs", exc, delay)
            return False
        log.info("frostbite: reconnected to %s:%d", *self._addr)
        return True
