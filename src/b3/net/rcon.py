"""RCON client.

The legacy ``q3a/rcon.py`` was the most fragile, most load-bearing module in the project, and its
Py2->3 port was broken (``str(sock.recv())`` corrupted every reply; ``encode_data`` raised). This
rewrite fixes the root cause by splitting three concerns that were tangled together:

* **Dialect** — the wire encoding (how a command/query becomes bytes, how a reply is stripped).
  This is where the per-game protocol knowledge lives (Quake3/IW plaintext vs the cod7 encrypted
  variant, added in a later phase). Pure functions, trivially testable, no sockets.
* **Transport** — actually moving bytes over UDP. Injectable, so tests feed recorded responses.
* **Rcon** — orchestration: the single bytes-in / str-out boundary, retry policy (never retry
  ``quit``/``map`` — they make the server stop responding), and the TTL ``status`` cache.

Preserved domain rules: four-``0xFF`` out-of-band prefix; the ``\\xff\\xff\\xff\\xffprint\\n`` reply
header is stripped; ``quit``/``map`` are never retried; ``status`` responses are cached briefly so a
burst of ``getPlayerList`` calls doesn't hammer the server.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Protocol, runtime_checkable

from b3.core.clock import Clock, SystemClock

log = logging.getLogger(__name__)

# Commands that must never be retried: they make the server stop replying, so a retry
# would spuriously report an error (or, worse, run the command twice).
NO_RETRY_COMMANDS = ("quit", "map", "map_rotate", "map_restart")


class RconError(Exception):
    """RCON command failed after exhausting retries."""


class RconTimeout(RconError):
    """No response from the server within the timeout budget."""


# --- dialects -------------------------------------------------------------


@runtime_checkable
class RconDialect(Protocol):
    """Wire encoding for a game's RCON protocol."""

    def encode_command(self, password: str, cmd: str, encoding: str) -> bytes: ...
    def encode_query(self, cmd: str, encoding: str) -> bytes: ...
    def strip_reply(self, data: bytes, encoding: str, errors: str = "replace") -> str: ...


class Quake3Dialect:
    """Plaintext Quake3 / Infinity-Ward CoD dialect.

    Command:  ``\\xff\\xff\\xff\\xff rcon "<password>" <cmd>\\n``
    Query:    ``\\xff\\xff\\xff\\xff <cmd>\\n``           (no password, e.g. getstatus)
    Reply:    prefixed with ``\\xff\\xff\\xff\\xffprint\\n`` which we strip.
    """

    PREFIX = b"\xff\xff\xff\xff"
    REPLY_HEADER = b"\xff\xff\xff\xffprint\n"

    def encode_command(self, password: str, cmd: str, encoding: str) -> bytes:
        return self.PREFIX + f'rcon "{password}" {cmd}\n'.encode(encoding, "replace")

    def encode_query(self, cmd: str, encoding: str) -> bytes:
        return self.PREFIX + f"{cmd}\n".encode(encoding, "replace")

    def strip_reply(self, data: bytes, encoding: str, errors: str = "replace") -> str:
        """Every header, not just the leading one.

        Stripped on bytes, before decode: the header contains 0xFF, which is not a clean text
        character in any of the encodings these servers use.

        **Every** occurrence, because a reply that does not fit one datagram arrives as several and
        each one carries its own header. This used to strip the leading header *or* — as an
        either/or — all of them, so on a long reply the second packet's header stayed, in the middle
        of the text. The split lands mid-row, so that is one corrupted player line and nothing else
        to see: on CoD4X the row read `...2310346614714116451 <header>76561198137164452 SirReaDy`
        and matched no pattern, so a `status` big enough to span two datagrams parsed as **zero
        players**. Which reads exactly like an empty server — the auth poll finds nobody and gives
        up, nobody is authenticated, and every admin on a busy server is suddenly a stranger. With
        the header gone the two halves are the row again, because the packet boundary falls inside
        the line rather than replacing anything.
        """
        return data.replace(self.REPLY_HEADER, b"").decode(encoding, errors)


class Cod7Dialect(Quake3Dialect):
    """Black Ops framing: the same plaintext protocol in a different envelope.

    Command:  ``<0xFF x4> 0x00 <password> <cmd> 0x00`` — a NUL byte where the other Infinity-Ward
    titles write the literal word ``rcon``, and another one at the end.
    Reply:    the header carries a 0x01 before ``print``.

    Nothing here is encrypted. That claim gets repeated about Black Ops, and this rewrite carried
    it in a comment until it was checked against the classic ``Cod7Rcon`` — which is a plain
    subclass of the Quake3 RCON with two different constants, exactly as below.
    """

    REPLY_HEADER = b"\xff\xff\xff\xff\x01print\n"

    def encode_command(self, password: str, cmd: str, encoding: str) -> bytes:
        body = f"{password} {cmd}".encode(encoding, "replace")
        return self.PREFIX + b"\x00" + body + b"\x00"


#: Dialects a profile can name, so the wire format is data like everything else about a game.
DIALECTS: dict[str, type[RconDialect]] = {"quake3": Quake3Dialect, "cod7": Cod7Dialect}


def dialect_for(name: str) -> RconDialect:
    """Build the named dialect, falling back to plain Quake3 with a warning."""
    factory = DIALECTS.get(name)
    if factory is None:
        log.warning("unknown rcon dialect %r; using quake3", name)
        factory = Quake3Dialect
    return factory()


# --- transport ------------------------------------------------------------


@runtime_checkable
class RconTransport(Protocol):
    """Moves an already-encoded payload to the server and returns the raw reply bytes."""

    def request(self, payload: bytes) -> bytes: ...
    def send(self, payload: bytes) -> None: ...
    def close(self) -> None: ...


class UdpRconTransport:
    """Real UDP transport, with two ways to send.

    ``request`` sends and waits for the reply; ``send`` sends and returns immediately. The
    distinction is the whole point: waiting for a reply costs at minimum the *settle* window on
    every call, because there is no length prefix in this protocol and the only way to know a
    multi-datagram ``status`` reply has finished is for the socket to go quiet. Chat output does
    not need a reply at all, so it must not pay that.

    Not unit-tested against a live server; the Rcon logic is exercised via a fake transport in the
    tests, and the socket behaviour below is deliberately thin enough to read.
    """

    def __init__(self, host: str, port: int, timeout: float = 0.8, settle: float = 0.1) -> None:
        self._addr = (host, port)
        self._timeout = timeout
        # How long to keep listening *after* a reply has started arriving, in case it spans several
        # datagrams. Waiting the full timeout again is what made every query cost ~a second.
        self._settle = min(settle, timeout)
        self._sock: socket.socket | None = None

    def _socket(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(self._timeout)
            self._sock.connect(self._addr)
        return self._sock

    def send(self, payload: bytes) -> None:
        """Fire and forget: hand the datagram to the kernel and return."""
        try:
            self._socket().send(payload)
        except OSError as e:  # pragma: no cover - network
            raise RconError(f"send failed: {e}") from e

    def request(self, payload: bytes) -> bytes:
        sock = self._socket()
        # Anything already waiting is the reply to an earlier fire-and-forget send (a server does
        # answer `say`, we just don't read it). Left in the buffer it would be returned as *this*
        # command's reply — an empty `status`, which reads as "the server is empty" and would drop
        # every client from the player list on the next sync.
        self._discard_pending(sock)
        try:
            sock.send(payload)
        except OSError as e:  # pragma: no cover - network
            raise RconError(f"send failed: {e}") from e

        chunks: list[bytes] = []
        # A status reply can span several datagrams; read until the socket goes quiet.
        while True:
            try:
                sock.settimeout(self._timeout if not chunks else self._settle)
                chunks.append(sock.recv(8192))
            except TimeoutError:
                break
            except OSError as e:  # pragma: no cover - network
                if not chunks:
                    raise RconTimeout(str(e)) from e
                break
        sock.settimeout(self._timeout)
        if not chunks:
            raise RconTimeout("no response")
        return b"".join(chunks)

    @staticmethod
    def _discard_pending(sock: socket.socket) -> None:
        sock.settimeout(0)
        dropped = 0
        try:
            while True:
                sock.recv(8192)
                dropped += 1
        except (BlockingIOError, TimeoutError):
            pass
        except OSError:  # pragma: no cover - network
            pass
        if dropped:
            log.debug("discarded %d stale rcon datagram(s) before querying", dropped)

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None


# --- orchestrator ---------------------------------------------------------


class Rcon:
    """High-level RCON client. The only place bytes become text and text becomes bytes."""

    def __init__(
        self,
        transport: RconTransport,
        password: str,
        *,
        dialect: RconDialect | None = None,
        encoding: str = "latin-1",
        clock: Clock | None = None,
        max_retries: int = 2,
        retry_delay: float = 0.05,
        status_cache_ttl: float = 5.0,
    ) -> None:
        self._transport = transport
        self._password = password
        self._dialect = dialect or Quake3Dialect()
        self._encoding = encoding
        self._clock = clock or SystemClock()
        self._max_retries = max(1, max_retries)
        self._retry_delay = retry_delay
        self._status_cache_ttl = status_cache_ttl
        # Keyed by the command asked, because a title may have more than one way to ask.
        self._status_cache: dict[str, tuple[float, str]] = {}

    # -- low level ---------------------------------------------------------

    def command(self, cmd: str, *, retry: bool = True) -> str:
        """Send an authenticated rcon command; return the (header-stripped) reply text."""
        payload = self._dialect.encode_command(self._password, cmd, self._encoding)
        return self._send(payload, cmd, retry=retry)

    def write(self, cmd: str) -> None:
        """Send a command without waiting for a reply — for output, not for questions.

        Chat is the bot's most frequent traffic by a wide margin: every command reply, every
        warning, every plugin announcement. Waiting for the server to acknowledge each line stalls
        whichever thread is doing it for as long as the settle window, which on the event-loop
        thread means the scheduler misses ticks and event delivery is late.

        The trade is deliberate and it is why penalties do *not* use this: nothing here can tell
        you the server never received the line. A ban keeps :meth:`command` so a dead server is
        reported to the admin who typed it.
        """
        payload = self._dialect.encode_command(self._password, cmd, self._encoding)
        writer = getattr(self._transport, "send", None)
        if writer is None:  # a transport from before `send` existed: fall back to a full round trip
            self._send(payload, cmd, retry=False)
            return
        writer(payload)

    def query(self, cmd: str) -> str:
        """Send an out-of-band query (no password), e.g. ``getstatus``/``getinfo``."""
        payload = self._dialect.encode_query(cmd, self._encoding)
        return self._send(payload, cmd, retry=True)

    def _send(self, payload: bytes, cmd: str, *, retry: bool) -> str:
        attempts = 1 if (not retry or self._is_no_retry(cmd)) else self._max_retries
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                raw = self._transport.request(payload)
                return self._dialect.strip_reply(raw, self._encoding)
            except RconError as e:
                last_exc = e
                if attempt + 1 < attempts:
                    time.sleep(self._retry_delay)
        raise RconError(f"command {cmd!r} failed after {attempts} attempt(s)") from last_exc

    @staticmethod
    def _is_no_retry(cmd: str) -> bool:
        head = cmd.strip().split(" ", 1)[0].lower()
        return head in NO_RETRY_COMMANDS

    # -- conveniences ------------------------------------------------------

    def say(self, text: str, *, template: str = "say %s") -> str:
        return self.command(template % text)

    def set_cvar(self, name: str, value: str) -> str:
        return self.command(f'set {name} "{value}"')

    def get_status(self, command: str = "status", *, force: bool = False) -> str:
        """Return a status reply, cached for ``status_cache_ttl`` seconds.

        Many callers ask for the player list in quick succession; the cache prevents a burst
        of identical queries from hammering the server (legacy behaviour, capped short).

        ``command`` is a parameter because not every title answers the same question: a CoD4X server
        with the `b3hide` mod hides admins from `status` and answers `b3status` with the whole table
        (see :attr:`b3.parsers.profile.GameProfile.status_commands`). The cache is keyed by command,
        so falling back from one to the other does not serve the first one's reply for the second.
        """
        now = self._clock.now()
        cached = self._status_cache.get(command)
        if not force and cached is not None:
            cached_at, value = cached
            if now - cached_at < self._status_cache_ttl:
                return value
        value = self.command(command)
        self._status_cache[command] = (now, value)
        return value

    def close(self) -> None:
        self._transport.close()
