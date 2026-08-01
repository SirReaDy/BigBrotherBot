"""BattlEye RCON — Arma 2 / Arma 3, and the first engine that *pushes* its events.

Every game before this wrote a log file the bot could tail, and answered RCON questions on the side.
BattlEye has no log file at all: the same UDP socket carries commands, their replies, **and** a
stream of server messages the game sends unprompted (chat, connects, disconnects, its own kicks).

So :class:`BattleyeClient` is deliberately *both* of the bot's input contracts at once — it satisfies
``RconClient`` (``command``/``write``/``close``) and ``LogSource`` (``open``/``read_lines``/``close``).
Nothing in the runtime changes: the live loop keeps polling a log source, the admin commands keep
sending RCON, and neither needs to know that this time they are the same socket. Frostbite, which
pushes events the same way, reuses the pattern.

**Single-threaded on purpose.** The classic implementation ran four threads (poll, read, write, plus
the connection itself) with a lock and three queues, and its own comments record the trouble that
caused. Here everything happens when the caller asks: ``read_lines`` drains whatever has arrived,
acknowledges it, and sends a keepalive if one is due; ``command`` pumps the socket until its reply
turns up, queueing any server messages it meets on the way so none are lost while waiting.

The wire format, from the classic ``battleye/protocol.py``::

    'BE' + <crc32 of payload, little-endian> + payload
    payload = 0xFF + <type> + [<sequence>] + [<data>]

    type 0x00  login          -> reply carries 0x01 (accepted) or 0x00 (wrong password)
    type 0x01  command        -> reply may arrive split across several packets
    type 0x02  server message -> **must be acknowledged**, or the server sends it again

Keepalive: an empty command packet. BattlEye hangs up after ~45 seconds of silence.
"""

from __future__ import annotations

import logging
import select
import socket
import struct
import zlib
from collections import deque
from dataclasses import dataclass

from b3.core.clock import Clock, SystemClock

log = logging.getLogger(__name__)

#: Packet types.
LOGIN = 0x00
COMMAND = 0x01
MESSAGE = 0x02

HEADER = b"BE"
PREFIX = 0xFF

#: The one-byte body of a login reply. Named rather than written inline as an escape, because an
#: escape typed into this file once arrived as a *raw* 0x01 control character instead — which works,
#: and is invisible in an editor.
LOGIN_ACCEPTED = b"\x01"

#: Send an empty command this often to keep the connection open. The server drops us at ~45s.
KEEPALIVE_SECONDS = 25.0

#: Chat pacing. An Arma server silently drops rapid `say` commands, so the classic parser slept
#: 0.8s between lines. Sleeping is not an option here — this is the event-loop thread — so queued
#: chat is released at this rate by whichever call comes next. See :meth:`BattleyeClient.write`.
SAY_INTERVAL = 0.8

#: How many recently-seen server messages to remember, so a retransmission is not acted on twice.
#: The sequence byte wraps at 256, so a window this small cannot mistake a genuinely new message for
#: an old one — and the payload has to match as well.
SEEN_WINDOW = 16

#: Treat the connection as lost after this long with nothing heard from the server *despite* having
#: sent a keepalive. There is no goodbye packet in this protocol, so silence is the only signal: an
#: Arma server that restarts (mission change, nightly cron) simply stops answering, and a bot that
#: does not notice sits there reading nothing for the rest of the night.
DEAD_AFTER = 90.0

#: Reconnect back-off, matching the remote log sources: first retry quickly, then back off, capped.
RETRY_INTERVAL = 5.0
RETRY_MAX = 300.0

#: What to ask the server when it has gone quiet, to find out whether it is still there. Silence on
#: its own proves nothing: an Arma server with nobody on it says nothing for hours.
PROBE_COMMAND = "players"

#: Runaway guard on the receive loops. Each iteration either consumes one waiting datagram or ends
#: the loop, so this is only reached by a server flooding us — never by ordinary waiting. It exists
#: because the loops used to be bounded by the *clock*, which meant an injected clock that does not
#: advance in real time (a test's) made them spin forever.
MAX_PUMP = 64


class BattleyeError(Exception):
    """A BattlEye connection or command failed."""


class BattleyeAuthError(BattleyeError):
    """The server rejected the RCON password.

    Distinct from a timeout on purpose: "wrong password" and "nothing is listening" are the two
    failures an operator confuses, and the classic bot reported the same silence for both.
    """


class BattleyeTimeout(BattleyeError):
    """No reply arrived in time."""


@dataclass(frozen=True, slots=True)
class Packet:
    """A decoded packet.

    The protocol is not symmetrical, and this is where that shows: a **login packet carries no
    sequence byte**, so the byte in that position is the first byte of its content — the first
    character of the password on the way out, and the accepted/rejected verdict on the way back.
    Hence two views of the same bytes:

    * ``payload`` — everything after the type byte. What a login is.
    * ``data`` — everything after the *sequence* byte. What a command or a message is.

    Reading a login through ``data`` silently loses its first character, which is exactly the bug the
    fake server caught the first time it was pointed at this.
    """

    type: int
    #: Meaningful for COMMAND and MESSAGE. For LOGIN it is ``payload[0]``.
    sequence: int
    payload: bytes
    data: bytes


def crc(payload: bytes) -> bytes:
    """The 4-byte little-endian CRC32 that follows ``BE`` in every packet."""
    return struct.pack("<I", zlib.crc32(payload) & 0xFFFFFFFF)


def encode(packet_type: int, sequence: int | None = None, data: bytes | str = b"") -> bytes:
    """Frame a packet. ``sequence`` is omitted for a login, present for everything else."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    payload = bytearray([PREFIX, packet_type])
    if sequence is not None:
        payload.append(sequence & 0xFF)
    payload.extend(data)
    return HEADER + crc(bytes(payload)) + bytes(payload)


def decode(packet: bytes) -> Packet | None:
    """Unframe a packet, or None if it is not one of ours or the checksum fails.

    A bad checksum returns None rather than raising: it means a corrupt datagram, which is a normal
    event on a UDP link and is handled by ignoring it (the server will resend anything that mattered).
    """
    if len(packet) < 9 or packet[:2] != HEADER:
        return None
    if packet[2:6] != crc(packet[6:]):
        log.debug("battleye: bad crc, ignoring %d-byte packet", len(packet))
        return None
    if packet[6] != PREFIX:
        return None
    return Packet(type=packet[7], sequence=packet[8], payload=packet[8:], data=packet[9:])


class BattleyeClient:
    """One socket, playing both roles: RCON client and source of game events."""

    #: `read_lines` does socket I/O, so the live loop runs it off the event-loop thread.
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
        say_interval: float = SAY_INTERVAL,
        dead_after: float = DEAD_AFTER,
        retry_interval: float = RETRY_INTERVAL,
        retry_max: float = RETRY_MAX,
        clock: Clock | None = None,
    ) -> None:
        self._addr = (host, port)
        self._password = password
        self._timeout = timeout
        #: Seconds between `read_lines` calls. Short, because these are live events, not a file.
        self.poll_interval = poll_interval
        self._keepalive = keepalive
        self._say_interval = say_interval
        self._dead_after = dead_after
        self._retry_interval = retry_interval
        self._retry_max = retry_max
        self._clock = clock or SystemClock()

        self._sock: socket.socket | None = None
        self._seq = 0
        self._last_send = 0.0
        self._last_say = 0.0
        #: When the server was last heard from, and when to try connecting again after a failure.
        self._last_heard = 0.0
        self._retry_at = 0.0
        self._failures = 0
        #: Server messages received but not yet handed to the bot.
        self._events: deque[str] = deque()
        #: Chat waiting to be released at `say_interval`.
        self._outbox: deque[str] = deque()
        #: (sequence, payload) of recent server messages, to drop a retransmission.
        self._seen: deque[tuple[int, bytes]] = deque(maxlen=SEEN_WINDOW)
        #: Parts of a reply that arrived split across packets.
        self._parts: dict[int, bytes] = {}

    # -- connection --------------------------------------------------------

    def open(self) -> None:
        """Connect and log in.

        Raises, so a wrong password or a wrong port fails at startup rather than silently later. A
        connection that drops *afterwards* is a different matter, handled by :meth:`read_lines`.
        """
        self._login()

    def _login(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(self._timeout)
        sock.connect(self._addr)
        self._sock = sock
        self._seq = 0
        self._last_send = self._clock.now()

        sock.send(encode(LOGIN, None, self._password))
        # Bounded by *receives*, not by the clock: `_receive` already waits up to the socket timeout
        # and returns None when it expires, so one None means the budget is spent. Waiting on the
        # clock instead would spin whenever the clock is not the wall (see MAX_PUMP).
        for _ in range(MAX_PUMP):
            packet = self._receive()
            if packet is None:
                break  # nothing more is coming
            if packet.type != LOGIN:
                continue  # a leftover message from a previous session; keep looking
            # A login reply is a single byte: 1 accepted, 0 rejected. No guessing at reply text.
            if packet.payload[:1] == LOGIN_ACCEPTED:
                log.info("battleye: logged in to %s:%d", *self._addr)
                self._last_heard = self._clock.now()
                self._failures = 0
                self._seen.clear()  # a fresh session numbers its messages from scratch
                return
            self.close()
            raise BattleyeAuthError("the server rejected the rcon password")
        self.close()
        raise BattleyeTimeout(
            f"no login reply from {self._addr[0]}:{self._addr[1]} — is the BattlEye rcon port right?"
        )

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- the LogSource half -------------------------------------------------

    def read_lines(self) -> list[str]:
        """Everything the server has said since last time, as lines for the parser.

        Also does everything the connection needs doing: acknowledging server messages (an
        unacknowledged one is sent again), releasing paced chat, sending a keepalive, and
        **reconnecting when the server has gone away**.

        That last part is not optional on this engine. There is no goodbye packet in the protocol, so
        a server that restarts — a mission change, a nightly cron — simply stops answering, and
        without this the bot would sit reading nothing until somebody noticed. Returns ``[]`` while
        reconnecting rather than raising, exactly as the remote log sources do: a server bouncing is
        an expected event, not a reason for the bot to stop.
        """
        if not self.connected and not self._try_reconnect():
            return []
        try:
            self._drain()
            self._flush_outbox()
            self._maybe_keepalive()
        except BattleyeError as exc:
            self._lost(f"{exc}")
            return []
        if self._silent_too_long() and not self._still_there():
            self._lost(f"no answer to `{PROBE_COMMAND}` after {self._dead_after:.0f}s of silence")
        lines, self._events = list(self._events), deque()
        return lines

    def _silent_too_long(self) -> bool:
        return self._clock.now() - self._last_heard > self._dead_after

    def _still_there(self) -> bool:
        """Ask the server something, because silence on its own proves nothing.

        An Arma server with nobody on it is silent for hours, and it is *not* certain that BattlEye
        answers the empty keepalive packet — so treating silence alone as death would reconnect a
        perfectly healthy idle server every couple of minutes, forever. One cheap command settles it:
        an answer means alive (and resets the clock), a timeout means gone.
        """
        try:
            self.command(PROBE_COMMAND)
        except BattleyeError:
            return False
        return True

    def _lost(self, why: str) -> None:
        """Drop the connection and schedule the next attempt."""
        log.warning("battleye: connection to %s:%d lost — %s", *self._addr, why)
        self.close()
        self._failures += 1
        self._outbox.clear()  # chat aimed at a session that no longer exists
        delay = min(self._retry_interval * (2 ** (self._failures - 1)), self._retry_max)
        self._retry_at = self._clock.now() + delay
        log.info("battleye: reconnecting in %.0fs", delay)

    def _try_reconnect(self) -> bool:
        if self._clock.now() < self._retry_at:
            return False
        try:
            self._login()
        except BattleyeAuthError:
            # The password will not fix itself, so stop hammering: back off to the ceiling and say
            # plainly what is wrong rather than repeating the same rejection every few seconds.
            self._failures = 99
            self._retry_at = self._clock.now() + self._retry_max
            log.error(
                "battleye: %s:%d rejected the rcon password; fix server.rcon_password and restart",
                *self._addr,
            )
            return False
        except BattleyeError as exc:
            self._failures += 1
            delay = min(self._retry_interval * (2 ** (self._failures - 1)), self._retry_max)
            self._retry_at = self._clock.now() + delay
            log.debug("battleye: reconnect failed (%s); next try in %.0fs", exc, delay)
            return False
        log.info("battleye: reconnected to %s:%d", *self._addr)
        return True

    # -- the RconClient half ------------------------------------------------

    def command(self, cmd: str) -> str:
        """Send a command and wait for its reply.

        Server messages that arrive while waiting are queued rather than dropped — the reason this
        can be single-threaded at all. Any paced chat still waiting is released first, so a "banning
        Bob" announcement cannot arrive after the ban it describes.
        """
        self._require_socket()
        self._flush_outbox(paced=False)
        sequence = self._next_sequence()
        self._send(encode(COMMAND, sequence, cmd))

        self._parts.clear()
        for _ in range(MAX_PUMP):
            packet = self._receive()
            if packet is None:
                break  # the socket timed out: no reply is coming
            if packet.type == MESSAGE:
                self._handle_message(packet)
                continue
            if packet.type != COMMAND or packet.sequence != sequence:
                continue
            reply = self._assemble(packet.data)
            if reply is None:
                continue  # a further part of a split reply is still to come
            return reply.decode("utf-8", "replace")
        raise BattleyeTimeout(f"no reply to {cmd!r}")

    def remove_ban(self, guid: str) -> bool:
        """Lift a ban on ``guid`` server-side, and confirm it is gone. True if it was.

        BattlEye's `removeBan` takes a **row number in the ban list**, not a player id, so this is a
        read-match-remove sequence rather than one command — which is why it cannot be expressed as a
        profile template and lives here instead (see ``GameProfile.unban_template``).

        Two safety rules, because removing the wrong row lifts a stranger's ban:

        * only the **GUID Bans** section is searched. The two sections are numbered separately, and
          the bot only ever creates GUID bans, so an index taken from the IP list would be wrong.
        * the list is **re-read afterwards** and the removal verified. Reporting success without
          checking is how a player ends up banned by a server nobody remembers banning them on.
        """
        from b3.parsers.battleye.status import parse_bans

        wanted = guid.strip().lower()
        if not wanted:
            return False
        matches = [
            ban
            for ban in parse_bans(self.command("bans"))
            if ban.kind == "guid" and ban.target.lower() == wanted
        ]
        if not matches:
            log.info("battleye: %s is not on the server's ban list; nothing to remove", guid)
            return True  # the desired state, which is what the caller asked about

        # Highest index first: removing a row renumbers the ones after it.
        for ban in sorted(matches, key=lambda b: b.index, reverse=True):
            self.command(f"removeBan {ban.index}")

        still_there = [
            ban
            for ban in parse_bans(self.command("bans"))
            if ban.kind == "guid" and ban.target.lower() == wanted
        ]
        if still_there:
            log.warning(
                "battleye: removeBan did not clear %s (still %d entr%s) — remove it with the game's "
                "own tools",
                guid,
                len(still_there),
                "y" if len(still_there) == 1 else "ies",
            )
            return False
        return True

    def write(self, cmd: str) -> None:
        """Queue a command with no reply expected — chat, paced.

        The pacing is the engine's requirement, not a preference: an Arma server drops `say`
        commands sent back to back. Queueing (rather than sleeping) keeps it off the event loop, and
        the next `read_lines` releases the line, so a wrapped multi-line reply trickles out at the
        rate the server accepts instead of mostly vanishing.
        """
        self._require_socket()
        self._outbox.append(cmd)
        self._flush_outbox()

    # -- internals ---------------------------------------------------------

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise BattleyeError("not connected to the BattlEye server")
        return self._sock

    def _next_sequence(self) -> int:
        sequence, self._seq = self._seq, (self._seq + 1) & 0xFF
        return sequence

    def _send(self, packet: bytes) -> None:
        sock = self._require_socket()
        try:
            sock.send(packet)
        except OSError as exc:  # pragma: no cover - network
            raise BattleyeError(f"send failed: {exc}") from exc
        self._last_send = self._clock.now()

    def _receive(self) -> Packet | None:
        """One packet, or None if nothing arrived before the socket timeout."""
        sock = self._require_socket()
        try:
            raw = sock.recv(8192)
        except (TimeoutError, BlockingIOError):
            return None
        except ConnectionError:
            # A connected UDP socket surfaces "nothing is listening on that port" once the ICMP
            # unreachable comes back, and the platforms spell it differently: Windows raises
            # ConnectionResetError, Linux ConnectionRefusedError, and neither subclasses the other.
            # Both are silence, reported by the caller's own timeout with a message that names the
            # likely cause (BattlEye's port is not the game port).
            log.debug("battleye: nothing listening at %s:%d", *self._addr)
            return None
        except OSError as exc:  # pragma: no cover - network
            raise BattleyeError(f"read failed: {exc}") from exc
        # Anything at all, even a packet we go on to discard, proves the server is still there.
        self._last_heard = self._clock.now()
        return decode(raw)

    def _drain(self) -> None:
        """Read everything already waiting, without blocking."""
        sock = self._require_socket()
        while select.select([sock], [], [], 0)[0]:
            packet = self._receive()
            if packet is None:
                continue
            if packet.type == MESSAGE:
                self._handle_message(packet)
            # A late command reply with nobody waiting for it is of no use; dropping it here is what
            # keeps it from being handed to the *next* caller as their answer.

    def _handle_message(self, packet: Packet) -> None:
        """Acknowledge a server message and queue its text, unless it is a retransmission."""
        self._send(encode(MESSAGE, packet.sequence))
        key = (packet.sequence, packet.data)
        if key in self._seen:
            # The server resends anything it thinks we missed, which happens whenever an ack is
            # lost. Acting on it twice would run a chat command twice — `!ban` included.
            log.debug("battleye: dropping repeat of message #%d", packet.sequence)
            return
        self._seen.append(key)
        text = packet.data.decode("utf-8", "replace").strip()
        if text:
            self._events.append(text)

    def _assemble(self, data: bytes) -> bytes | None:
        """Reassemble a reply split over several packets; None until the last part arrives."""
        if not data.startswith(b"\x00") or len(data) < 3:
            return data
        total, index, part = data[1], data[2], data[3:]
        self._parts[index] = part
        self._parts_expected = total
        if len(self._parts) < total:
            return None
        joined = b"".join(self._parts.get(i, b"") for i in range(total))
        self._parts.clear()
        return joined

    def _flush_outbox(self, paced: bool = True) -> None:
        now = self._clock.now()
        while self._outbox:
            if paced and now - self._last_say < self._say_interval:
                return
            self._send(encode(COMMAND, self._next_sequence(), self._outbox.popleft()))
            self._last_say = now

    def _maybe_keepalive(self) -> None:
        if self._clock.now() - self._last_send >= self._keepalive:
            self._send(encode(COMMAND, self._next_sequence()))
