"""Homefront's admin protocol: length-prefixed TCP, one connection for commands and events.

Like BattlEye and Frostbite, this engine has **no game log** — the same socket carries commands,
replies and pushed events — so :class:`HomefrontClient` satisfies both the ``RconClient`` and
``LogSource`` contracts and the live loop is unchanged. What is new here is the shape of the wire.

**The framing is asymmetric, and that is the one thing to get right.** A packet the *client* sends is::

    type(2 ASCII)  length(4, big-endian signed)  data(length bytes, utf-8)

A packet the *server* sends carries a channel byte the client's does not::

    type(2 ASCII)  channel(1)  length(4, big-endian signed)  data(length bytes, utf-8)

Six bytes of header one way, seven the other. A codec that assumes symmetry reads every reply one byte
out of step and produces garbage rather than an error, which is a long evening. The asymmetry is not a
guess: it is what the classic ``protocol.py`` encodes and decodes, and the fake server insists on it.

**The password never crosses the wire.** Login is ``PASS: "<sha1 hex, uppercase, in space-separated
pairs>"`` — an unusual format, and neither an unspaced nor a lowercase digest is accepted.

**Silence kills the connection.** The server hangs up after ten seconds without a packet from the
client, so pinging is not an optimisation. :meth:`read_lines` sends one when it is due, which keeps
the whole thing single-threaded: everything happens when the caller asks, exactly as the BattlEye
client works.

**And when the connection does go, it comes back.** This engine closes the socket outright — on a
restart, a map change, or ten seconds of quiet — so a client that only noticed would leave the bot
permanently deaf, reading nothing for the rest of the evening while looking perfectly healthy. That
was a real BattlEye bug once; here it is more likely rather than less, because the server hangs up on
purpose. :meth:`read_lines` reconnects with a back-off, and tells the parser it happened so the roster
can be rebuilt: after an outage nobody knows who is still playing, and the old list is a guess.

Events reach the parser as **JSON** — ``[channel, data]`` — for the reason the Frostbite client does
the same: a message's text may contain any character, newlines included (the player-list reply is
newline-separated *inside* one message), so no delimiter could safely join the channel to the data.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import time
from struct import pack, unpack

log = logging.getLogger(__name__)

#: Message types, client and server.
CLIENT_TRANSMISSION = b"CT"
CLIENT_DISCONNECT = b"CD"
CLIENT_PING = b"CP"
SERVER_TRANSMISSION = b"ST"

#: Channels a server message arrives on. Only SERVER and CHATTER carry anything this bot reads.
CHANNEL_BROADCAST = 0
CHANNEL_NORMAL = 1
CHANNEL_CHATTER = 2
CHANNEL_GAMEPLAY = 3
CHANNEL_SERVER = 4

#: The server hangs up after this long without hearing from us.
IDLE_TIMEOUT = 10.0
#: So ping well inside it. Twice the margin, because a missed ping costs the whole connection.
PING_INTERVAL = 4.0

#: Reconnect back-off: try again quickly, then ease off, capped — the same shape the remote log
#: sources and the BattlEye client use, so an operator sees one behaviour across every family.
RETRY_INTERVAL = 5.0
RETRY_MAX = 300.0

#: The channel a *synthetic* message carries — one this client invents rather than the server sending
#: it. Negative, so it can never collide with a real channel. There is exactly one: the note that the
#: connection was re-established, which the parser reads as "your roster is no longer trustworthy".
CHANNEL_CLIENT_NOTICE = -1
RECONNECTED_NOTICE = "RECONNECTED"

#: A reply longer than this is not a reply; it is a desynchronised stream.
MAX_PAYLOAD = 1_000_000
#: How many receives one `read_lines` will make before returning, so a chatty server cannot hold the
#: event loop. The clock is not the bound: a loop bounded by time and blocked by a socket is fragile
#: even when it works, and never terminates against an injected clock.
MAX_PUMP = 32


class HomefrontError(Exception):
    """The connection failed, or the server said something this client cannot read."""


class HomefrontAuthError(HomefrontError):
    """The server rejected the password."""


def login_hash(password: str) -> str:
    """The login string the game expects: uppercase SHA1 hex, in space-separated pairs."""
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    return " ".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def encode(message: bytes, data: str) -> bytes:
    """Frame a *client* packet: type, length, payload. No channel byte — see the module docstring."""
    payload = data.encode("utf-8")
    return message[:2] + pack(">i", len(payload)) + payload


def decode(buffer: bytes) -> tuple[int, str, bytes] | None:
    """Read one *server* packet out of ``buffer``.

    Returns ``(channel, data, rest)``, or None while the buffer holds less than a whole packet. Raises
    :class:`HomefrontError` on a length that cannot be real, because there is nothing in this protocol
    to resynchronise on: no framing marker, no checksum. Dropping the connection and reconnecting is
    recoverable; carrying on from the wrong offset is not.
    """
    if len(buffer) < 7:
        return None
    (length,) = unpack(">i", buffer[3:7])
    if length < 0 or length > MAX_PAYLOAD:
        raise HomefrontError(f"implausible packet length {length}: the stream is out of step")
    if len(buffer) < 7 + length:
        return None
    (channel,) = unpack(">B", buffer[2:3])
    data = buffer[7 : 7 + length].decode("utf-8", "replace")
    return channel, data, buffer[7 + length :]


class HomefrontClient:
    """Commands and events over one TCP connection. Both halves of the bot's input.

    ``blocking`` is True: reads happen on a socket with a timeout, so the live loop runs
    :meth:`read_lines` on a worker thread (see `cli._run_live`).
    """

    blocking = True
    poll_interval = 0.0

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        *,
        timeout: float = 2.0,
        playerlist_interval: float = 15.0,
        ping_interval: float = PING_INTERVAL,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        #: How often to ask the server for its roster. There is no synchronous way to get it: the
        #: reply arrives as pushed PLAYER messages, so somebody has to keep asking. The classic
        #: parser used a 15-second cron; this does it from the pump, which needs no thread.
        self.playerlist_interval = playerlist_interval
        #: How often to ping. A parameter rather than a constant because it has to stay under the
        #: server's patience, and nothing in the protocol states what that is — ten seconds is what
        #: this build does. A server that is less patient needs a shorter interval, not a new client.
        self.ping_interval = ping_interval

        self.connected = False
        #: Messages read before `read_lines` was first called — during the login, say. A real list
        #: from the start, so nothing has to guess whether the attribute exists yet.
        self._pending: list[str] = []
        self.authed = False
        self.server_version = ""
        self._sock: socket.socket | None = None
        self._buffer = b""
        self._last_ping = 0.0
        self._last_playerlist = 0.0
        #: When to try connecting again, and how many attempts have failed — see `_try_reconnect`.
        self._retry_at = 0.0
        self._failures = 0

    # -- LogSource + RconClient lifecycle ----------------------------------

    def open(self) -> None:
        """Connect, log in, and wait for the server to accept us."""
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise HomefrontError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        self._sock.settimeout(self.timeout)
        self._buffer = b""
        self.connected = True
        self.authed = False
        self._send(CLIENT_TRANSMISSION, f'PASS: "{login_hash(self.password)}"')
        self._last_ping = self._last_playerlist = time.monotonic()

        # The verdict arrives as a pushed message like any other, so read until it turns up. Anything
        # else that arrives meanwhile is kept: a HELLO, or a player joining as we connect.
        pending: list[str] = []
        deadline = time.monotonic() + max(self.timeout, 2.0) * 2
        while not self.authed and time.monotonic() < deadline:
            got = self._pump()
            pending.extend(got)
            if not got:
                continue
        if not self.authed:
            self.close()
            raise HomefrontAuthError(
                "the server did not accept the password (or never answered the login)"
            )
        self._pending = pending

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._send(CLIENT_DISCONNECT, "")
            except Exception:  # noqa: BLE001 - saying goodbye is a courtesy, not a requirement
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.connected = False
        self.authed = False

    # -- reading -----------------------------------------------------------

    def read_lines(self) -> list[str]:
        """Everything the server has said since last time, as JSON ``[channel, data]`` strings.

        Also does the connection's housekeeping, because this is the one method the live loop calls
        often: it pings before the server's ten-second patience runs out, and asks for the roster
        every ``playerlist_interval`` — there being no synchronous way to ask for it.
        """
        # Take the buffer and replace it, rather than reading it and clearing it only when it had
        # something in it: `out` would otherwise *be* `self._pending`, and extending it below would
        # append this read's messages back into the buffer — so every line came out twice, once now
        # and once on the next call. Found by an end-to-end run, invisible to a unit test that reads
        # once.
        out = self._pending
        self._pending = []
        if not self.connected:
            # Reconnect rather than reporting nothing for ever. Returning what was already buffered
            # first, because those messages are real whatever the socket is doing now.
            if self._try_reconnect():
                out.append(json.dumps([CHANNEL_CLIENT_NOTICE, RECONNECTED_NOTICE]))
                out.extend(self._pending)
                self._pending = []
            return out

        now = time.monotonic()
        if now - self._last_ping >= self.ping_interval:
            self._last_ping = now
            self._send(CLIENT_PING, "PING")
        if self.playerlist_interval and now - self._last_playerlist >= self.playerlist_interval:
            self._last_playerlist = now
            self.command("RETRIEVE PLAYERLIST")

        out.extend(self._pump())
        return out

    def _try_reconnect(self) -> bool:
        """Attempt to re-establish the connection, honouring the back-off. True if it is up again.

        A rejected password backs off straight to the ceiling and says so once, loudly: it will not fix
        itself, and hammering a server with a bad password is how an address gets blocked. Same call the
        BattlEye client makes, for the same reason.
        """
        if time.monotonic() < self._retry_at:
            return False
        try:
            self.open()
        except HomefrontAuthError:
            self._failures = 99
            self._retry_at = time.monotonic() + RETRY_MAX
            log.error(
                "homefront: %s:%d rejected the password; fix server.rcon_password and restart",
                self.host,
                self.port,
            )
            return False
        except HomefrontError as exc:
            self._failures += 1
            delay = min(RETRY_INTERVAL * (2 ** (self._failures - 1)), RETRY_MAX)
            self._retry_at = time.monotonic() + delay
            log.debug("homefront: reconnect failed (%s); next try in %.0fs", exc, delay)
            return False
        self._failures = 0
        self._retry_at = 0.0
        log.info("homefront: reconnected to %s:%d", self.host, self.port)
        # Ask for the roster at once rather than waiting for the next interval: everything the bot
        # believed about who is playing was learned before the outage.
        self.command("RETRIEVE PLAYERLIST")
        return True

    def _pump(self) -> list[str]:
        """Read what is there, decode whole packets, and hand back the ones a parser wants."""
        lines: list[str] = []
        for _ in range(MAX_PUMP):
            chunk = self._receive()
            if chunk is None:
                break
            self._buffer += chunk
            while True:
                try:
                    decoded = decode(self._buffer)
                except HomefrontError as exc:
                    # Out of step, and unrecoverable by design: drop the connection so the next open
                    # starts clean rather than reporting nonsense for the rest of the session.
                    log.error("homefront: %s; dropping the connection", exc)
                    self.close()
                    return lines
                if decoded is None:
                    break
                channel, data, self._buffer = decoded
                if self._is_housekeeping(channel, data):
                    continue
                lines.append(json.dumps([channel, data]))
        return lines

    def _is_housekeeping(self, channel: int, data: str) -> bool:
        """Swallow what the *connection* owns, and let everything else through to the parser."""
        if data == "PONG":
            return True
        if channel == CHANNEL_SERVER:
            if data.startswith("AUTH: "):
                self.authed = data == "AUTH: true"
                if not self.authed:
                    log.error("homefront: the server rejected the password")
                return True
            if data.startswith("HELLO: "):
                self.server_version = data[len("HELLO: ") :].strip()
                log.info("homefront server version %s", self.server_version)
                return True
        return False

    def _receive(self) -> bytes | None:
        """One read, or None when the socket had nothing within its timeout."""
        if self._sock is None:
            return None
        try:
            chunk = self._sock.recv(8192)
        except TimeoutError:
            return None
        except OSError as exc:
            log.warning("homefront: read failed: %s", exc)
            self.connected = False
            return None
        if not chunk:
            # An orderly close from the other end: the server hung up.
            log.warning("homefront: the server closed the connection")
            self.connected = False
            return None
        return chunk

    # -- writing -----------------------------------------------------------

    def command(self, cmd: str) -> str:
        """Send a command. Returns "" — replies to these arrive as *pushed messages*, not answers.

        The bot's queries do not go through here (there is nothing to query synchronously); the roster
        is assembled from the PLAYER messages the server pushes, by the parser.
        """
        self.write(cmd)
        return ""

    def write(self, cmd: str) -> None:
        """Send a command without expecting anything back."""
        if not cmd.strip():
            log.debug("homefront: refusing to send an empty command")
            return
        self._send(CLIENT_TRANSMISSION, cmd)

    def _send(self, message: bytes, data: str) -> None:
        if self._sock is None:
            raise HomefrontError("not connected")
        try:
            self._sock.sendall(encode(message, data))
        except OSError as exc:
            self.connected = False
            raise HomefrontError(f"send failed: {exc}") from exc


__all__ = [
    "CHANNEL_BROADCAST",
    "CHANNEL_CLIENT_NOTICE",
    "CHANNEL_CHATTER",
    "CHANNEL_GAMEPLAY",
    "CHANNEL_NORMAL",
    "CHANNEL_SERVER",
    "IDLE_TIMEOUT",
    "PING_INTERVAL",
    "RECONNECTED_NOTICE",
    "RETRY_INTERVAL",
    "RETRY_MAX",
    "HomefrontAuthError",
    "HomefrontClient",
    "HomefrontError",
    "decode",
    "encode",
    "login_hash",
]
