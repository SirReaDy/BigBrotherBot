"""Source RCON: Valve's request/response protocol over TCP.

The eighth transport here, and the first that is *only* a command channel — a Source server has no
push connection at all. Its events go to a log file (``console.log``, written when the server is
started with ``-condebug``), so this client satisfies the ``RconClient`` contract alone and the log
source is the ordinary file tailer every Call of Duty title uses.

**The framing.** Every packet, both directions::

    <int32 size><int32 id><int32 type><body bytes>\\x00\\x00

``size`` counts everything after itself. The two trailing NULs are two empty-terminated strings: the
body, and a second string the protocol reserves and never uses. All integers are little-endian.

**The id is the correlator, and it is the whole reason this client is simple.** The server echoes
back the id it was sent, so a reply can always be matched to its request. That is what makes
:meth:`write` safe here: a command sent without reading its answer leaves packets in the socket, and
without ids they would be handed to the *next* caller as its reply — an empty ``status``, which reads
as "the server is empty" and drops every player from the roster. Ids mean those stale packets are
recognisable and can be discarded instead of guessed at.

**A reply may be split across packets, and nothing in a packet says so.** The documented workaround,
and the one used here, is to send a second, empty command straight after the real one: when its
reply comes back — recognised by *its* id — the real reply must be complete, because the server
answers in order. The classic implementation instead guessed from packet size ("probably split if
larger than 3696 bytes"), which is wrong in both directions: a reply that happens to land near the
threshold waits for a packet that never comes, and a long line makes a complete reply look split.
The sentinel is not trusted blindly either — the read is still bounded by a deadline, so a server
that declines to answer it costs a little latency rather than the reply.

**Authentication is a handshake, not a per-command password**, which is why this cannot be expressed
as a :class:`b3.net.rcon.RconDialect`. The client sends ``SERVERDATA_AUTH``; the server answers with
an empty ``SERVERDATA_RESPONSE_VALUE`` followed by ``SERVERDATA_AUTH_RESPONSE``, and **a rejected
password is signalled by an id of -1** rather than by any message. A refusal is never retried on a
loop: as everywhere else here, hammering a server with a bad password is how an address gets blocked.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: The default Source server port. RCON shares it with the game, over TCP rather than UDP.
DEFAULT_PORT = 27015

# Packet types. Note that 2 means two different things depending on direction, which is the
# protocol's doing rather than a mistake here: outbound it is "run this command", inbound it is
# "here is the verdict on your password".
SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_RESPONSE_VALUE = 0

#: The id the server puts on an auth reply when it did **not** accept the password. There is no
#: message with it — this integer is the entire rejection.
AUTH_FAILED_ID = -1

#: Longest command the server accepts. Past this it silently does nothing, so it is refused here
#: instead: the classic implementation found the figure by trial and error and it has held up.
MAX_COMMAND_LENGTH = 510

#: Body limits used to sanity-check a length prefix before trusting it to size a read.
MIN_PACKET_SIZE = 4 + 4 + 1 + 1  # id, type, and the two string terminators
MAX_PACKET_SIZE = 4 + 4 + 4096 + 2

#: Receives per pump, so a busy socket cannot hold the event loop indefinitely. Bounded by reads
#: rather than by the clock, for the reason recorded in `b3.net.battleye`: a loop whose exit depends
#: on time while its blocking depends on a socket never terminates against an injected clock.
MAX_PUMP = 64

#: What a server says in the *body* of a reply when the password was wrong but it answered anyway.
#: Some builds do this instead of using :data:`AUTH_FAILED_ID`, so both are checked.
BAD_PASSWORD = "Bad Password"


class SourceError(Exception):
    """The connection failed, or the server said something this client cannot read."""


class SourceAuthError(SourceError):
    """The server rejected the RCON password."""


@dataclass(frozen=True, slots=True)
class Packet:
    """One RCON packet, decoded."""

    id: int
    type: int
    body: str


def encode_packet(request_id: int, packet_type: int, body: str, encoding: str = "utf-8") -> bytes:
    """Frame one packet. ``size`` counts everything after itself, so it excludes its own four bytes."""
    payload = struct.pack("<ii", request_id, packet_type) + body.encode(encoding, "replace")
    payload += b"\x00\x00"
    return struct.pack("<i", len(payload)) + payload


def decode_packet(buffer: bytes, encoding: str = "utf-8") -> tuple[Packet, bytes] | None:
    """Read one packet off the front of ``buffer``.

    Returns ``(packet, rest)``, or None when the buffer does not yet hold a whole packet — TCP will
    happily deliver half of one. Raises :class:`SourceError` on a length prefix that cannot be real,
    which means the stream is out of step and the connection is no longer readable.
    """
    if len(buffer) < 4:
        return None
    (size,) = struct.unpack("<i", buffer[:4])
    if size < MIN_PACKET_SIZE or size > MAX_PACKET_SIZE:
        raise SourceError(f"packet claims an implausible size of {size} bytes")
    if len(buffer) < 4 + size:
        return None
    payload = buffer[4 : 4 + size]
    request_id, packet_type = struct.unpack("<ii", payload[:8])
    # The body is NUL-terminated and followed by a second, always-empty string. Splitting on the
    # first NUL takes the body and discards both terminators without needing to trust the second.
    body = payload[8:].split(b"\x00", 1)[0].decode(encoding, "replace")
    return Packet(request_id, packet_type, body), buffer[4 + size :]


class SourceRconClient:
    """Source RCON over one TCP connection, reconnecting when the server drops it.

    A Source server closes this socket on a level change and on restart, and — unlike the push
    families — that is not immediately visible, because nothing is expected to arrive between
    commands. So the connection is re-established on demand rather than watched: a failed send or a
    closed read reconnects once and retries the command, which is what the classic implementation
    did and the one thing about it that was right.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        password: str = "",
        *,
        timeout: float = 2.0,
        settle: float = 0.1,
        encoding: str = "utf-8",
        use_sentinel: bool = True,
        status_cache_ttl: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        #: How long to keep reading once a reply has started, when the sentinel has not come back.
        #: Only a fallback: with the sentinel the read ends as soon as the reply is known complete.
        self.settle = min(settle, timeout)
        self.encoding = encoding
        #: Send an empty command after each real one to find the end of a split reply. Off only for
        #: a server that turns out not to answer it, where the deadline does the work instead.
        self.use_sentinel = use_sentinel
        self.status_cache_ttl = status_cache_ttl

        self.connected = False
        self.authed = False
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._request_id = 0
        #: Set once the server has refused the password, so nothing tries again on its own. An
        #: operator fixing the config and restarting the bot is the way out; a retry loop here would
        #: just spend attempts against whatever rate limit the host has.
        self._password_refused = False
        #: Keyed by the command asked, because a title may have more than one way to ask — see
        #: `GameProfile.status_commands` and the b3hide case behind it.
        self._status_cache: dict[str, tuple[float, str]] = {}

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Connect and authenticate. Raises :class:`SourceAuthError` on a rejected password."""
        if self._password_refused:
            raise SourceAuthError(
                f"{self.host}:{self.port} already refused this password; "
                "fix server.rcon_password and restart"
            )
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise SourceError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        self._sock.settimeout(self.timeout)
        self._buffer = b""
        self.connected = True
        self.authed = False
        self._authenticate()

    def _authenticate(self) -> None:
        """Send the password once and read until the verdict arrives.

        Once, deliberately — see the class docstring. The empty ``SERVERDATA_RESPONSE_VALUE`` some
        servers send before the verdict is skipped rather than mistaken for it.
        """
        request_id = self._next_id()
        self._send_packet(request_id, SERVERDATA_AUTH, self.password)

        deadline = time.monotonic() + max(self.timeout, 1.0) * 2
        while self.connected and time.monotonic() < deadline:
            for packet in self._pump():
                if packet.id == AUTH_FAILED_ID or BAD_PASSWORD in packet.body:
                    self._password_refused = True
                    self.close()
                    raise SourceAuthError("the server refused the RCON password")
                if packet.type == SERVERDATA_AUTH_RESPONSE and packet.id == request_id:
                    self.authed = True
                    log.info("source: authenticated to %s:%s", self.host, self.port)
                    return
                # The empty RESPONSE_VALUE that precedes the verdict on most builds.
                log.debug("source: skipping %r while waiting for the auth verdict", packet)
        self.close()
        raise SourceError("the server never answered the authentication request")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer = b""
        self.connected = False
        self.authed = False

    def _ensure_open(self) -> None:
        if not (self.connected and self.authed):
            self.open()

    # -- commands ----------------------------------------------------------

    def command(self, cmd: str) -> str:
        """Send a command and return its reply, reconnecting once if the connection has gone.

        The retry covers exactly one case and it is the common one: the server closed the socket
        between commands (a level change, a restart) and nothing had reason to notice. A command that
        fails again after a fresh connection is a real failure and is raised.
        """
        cmd = cmd.strip()
        if not cmd:
            log.debug("source: refusing to send an empty command")
            return ""
        self._check_length(cmd)
        self._ensure_open()
        try:
            return self._run(cmd)
        except SourceAuthError:
            raise
        except SourceError as exc:
            log.info("source: %s; reconnecting and retrying %r", exc, cmd)
            self.open()
            return self._run(cmd)

    def write(self, cmd: str) -> None:
        """Send a command without waiting for its reply — chat output, where nothing reads it.

        Safe here only because replies carry the id they were asked with: whatever this leaves in
        the socket is recognisable as not belonging to the next command, and :meth:`_run` discards
        it rather than returning it.
        """
        cmd = cmd.strip()
        if not cmd:
            return
        self._check_length(cmd)
        self._ensure_open()
        self._send_packet(self._next_id(), SERVERDATA_EXECCOMMAND, cmd)

    @staticmethod
    def _check_length(cmd: str) -> None:
        if len(cmd) > MAX_COMMAND_LENGTH:
            raise SourceError(
                f"command is {len(cmd)} characters, over this protocol's "
                f"{MAX_COMMAND_LENGTH}: {cmd!r}"
            )

    def _run(self, cmd: str) -> str:
        """One command, one reply, with the sentinel trick for a reply split across packets."""
        request_id = self._next_id()
        self._send_packet(request_id, SERVERDATA_EXECCOMMAND, cmd)
        sentinel_id = -1
        if self.use_sentinel:
            sentinel_id = self._next_id()
            self._send_packet(sentinel_id, SERVERDATA_EXECCOMMAND, "")

        parts: list[str] = []
        deadline = time.monotonic() + self.timeout
        while self.connected and time.monotonic() < deadline:
            packets = self._pump()
            if not packets:
                # Nothing waiting. With something already in hand and no sentinel to wait for, a
                # quiet socket is the only end-of-reply signal there is.
                if parts and not self.use_sentinel:
                    break
                continue
            for packet in packets:
                if packet.id == AUTH_FAILED_ID or BAD_PASSWORD in packet.body:
                    self._password_refused = True
                    self.close()
                    raise SourceAuthError("the server refused the RCON password")
                if packet.id == request_id:
                    parts.append(packet.body)
                elif packet.id == sentinel_id:
                    return "".join(parts)
                else:
                    # A reply to an earlier fire-and-forget `write`, or to a command that timed out.
                    log.debug("source: discarding a stale reply (id %d)", packet.id)
            if parts and not self.use_sentinel:
                deadline = min(deadline, time.monotonic() + self.settle)
        if not parts:
            raise SourceError(f"no reply to {cmd!r} within {self.timeout:.1f}s")
        log.debug("source: reply to %r ended on the deadline rather than the sentinel", cmd)
        return "".join(parts)

    def get_status(self, command: str = "status", *, force: bool = False) -> str:
        """A status reply, cached briefly so a burst of roster reads does not hammer the server.

        Keyed by the command, so a title with more than one way to ask is never served the first
        command's reply for the second — the same rule as :meth:`b3.net.rcon.Rcon.get_status`.
        """
        now = time.monotonic()
        cached = self._status_cache.get(command)
        if not force and cached is not None:
            cached_at, value = cached
            if now - cached_at < self.status_cache_ttl:
                return value
        value = self.command(command)
        self._status_cache[command] = (now, value)
        return value

    # -- the socket --------------------------------------------------------

    def _next_id(self) -> int:
        # Wrapped well inside int32, and never 0 or -1: -1 is the auth-failure signal and 0 is what
        # an uninitialised field reads as, so neither should ever be a live request id.
        self._request_id = (self._request_id % 0x3FFF_FFFF) + 1
        return self._request_id

    def _send_packet(self, request_id: int, packet_type: int, body: str) -> None:
        if self._sock is None:
            raise SourceError("not connected")
        try:
            self._sock.sendall(encode_packet(request_id, packet_type, body, self.encoding))
        except OSError as exc:
            self.connected = False
            raise SourceError(f"send failed: {exc}") from exc

    def _pump(self) -> list[Packet]:
        """Read what is there and return whole packets. Returns as soon as it has something."""
        packets: list[Packet] = []
        for _ in range(MAX_PUMP):
            if packets:
                break
            chunk = self._receive()
            if chunk is None:
                break
            self._buffer += chunk
            while True:
                try:
                    decoded = decode_packet(self._buffer, self.encoding)
                except SourceError as exc:
                    log.error("source: %s; dropping the connection", exc)
                    self.close()
                    return packets
                if decoded is None:
                    break
                packet, self._buffer = decoded
                packets.append(packet)
        return packets

    def _receive(self) -> bytes | None:
        if self._sock is None:
            return None
        try:
            chunk = self._sock.recv(8192)
        except TimeoutError:
            return None
        except OSError as exc:
            log.warning("source: read failed: %s", exc)
            self.connected = False
            return None
        if not chunk:
            log.info("source: the server closed the connection")
            self.connected = False
            return None
        return chunk


__all__ = [
    "AUTH_FAILED_ID",
    "BAD_PASSWORD",
    "DEFAULT_PORT",
    "MAX_COMMAND_LENGTH",
    "SERVERDATA_AUTH",
    "SERVERDATA_AUTH_RESPONSE",
    "SERVERDATA_EXECCOMMAND",
    "SERVERDATA_RESPONSE_VALUE",
    "Packet",
    "SourceAuthError",
    "SourceError",
    "SourceRconClient",
    "decode_packet",
    "encode_packet",
]
