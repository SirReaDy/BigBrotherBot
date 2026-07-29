"""Frontlines: Fuel of War's admin protocol: an MD5 challenge, 0x04 frames, and a silent rejection.

One TCP connection carries commands and events, so :class:`FrontlineClient` satisfies both the
``RconClient`` and ``LogSource`` contracts and the live loop is unchanged — the pattern BattlEye
established. Unlike Ravaged, nothing here is a *reply*: the server never echoes the command it is
answering, so every packet is a line for the parser and ``command`` does not wait for anything.

The protocol is documented, and the classic parser quotes the game's own RCON documentation for the
login. Five properties matter, and each is a place where being wrong looks like nothing being wrong.

**Packets are separated by ``\\x04``.** The documentation says "'\\n' or 0x04", and that must not be
taken literally when reading: the ``PlayerList:`` reply is a *single packet with newlines inside it*, so
a client that also split on ``\\n`` would shred the one message carrying the whole roster — and, because
this engine has no connect or disconnect lines at all, the whole of its identity information.

**The login is a challenge/response, and it needs a username.** The server greets with
``WELCOME! Frontlines: Fuel of War (RCON) VER=2 CHALLENGE=38D384D07C``; the client answers
``RESPONSE <user> <md5(challenge + password)>``. The challenge varies and its length is not fixed, so
it has to be read rather than assumed. The password itself never crosses the wire.

**A failed login is not answered — the connection is simply closed.** There is no message to match, so
a client waiting for one waits until its timeout and then reports the wrong cause. Here, a close before
``Login SUCCESS!`` *is* the rejection, and it is reported as an auth error rather than a network one.

**Ten seconds of silence costs the connection**, so the ping is not an optimisation. It goes out from
``read_lines`` — the single-threaded pattern the other pushed families use.

**Nothing is reported until the bot asks for it.** A freshly authenticated connection is silent: no chat
and no debug lines arrive until ``CHATLOGGING TRUE`` and ``DebugLogging TRUE`` are sent. Those go out
from :meth:`open` for that reason, not as a preference — a bot that forgets them sees a server where
nobody ever speaks and cannot tell it from an empty one.

One thing the classic parser could not have got right, worth recording because it is why its details are
treated as evidence rather than as fact: its login called ``md5.new(...)`` on a ``hashlib.md5`` import,
which raises ``AttributeError`` every time, and its ``authorizeClients`` raised ``NotImplementedError``.
Neither can ever have run. Where its behaviour and the documented protocol disagree, the documentation
wins here.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import time

log = logging.getLogger(__name__)

#: The remote console port, unless the operator says otherwise.
DEFAULT_PORT = 14507

#: Packet separator, both ways. See the module docstring: this, and deliberately not the newline.
TERMINATOR = "\x04"

#: The greeting, whose challenge the password is hashed against.
WELCOME_RE = re.compile(
    r"^WELCOME! Frontlines: Fuel of War \(RCON\) VER=(?P<version>\S+) CHALLENGE=(?P<challenge>.+)$"
)
#: The one positive answer. Matched case-insensitively and by prefix: the game's documentation writes
#: it "Login Success!" while the classic parser matched "Login SUCCESS! User:<name>", and there is no
#: reason to lose a connection over which of them a given build prints.
LOGIN_SUCCESS_PREFIX = "login success!"

#: Sent once, immediately after logging in, because this server reports nothing until asked. Their
#: order does not matter; that they are sent at all does.
LOGGING_COMMANDS = ("CHATLOGGING TRUE", "DebugLogging TRUE", "Punkbusterlogging TRUE")

#: The keepalive. Ten seconds of quiet costs the connection, so this goes out well inside that.
PING_COMMAND = "ECHONET PING"
PING_INTERVAL = 4.0

#: How often to ask for the roster. This is **not** a status poll: with no connect or disconnect lines
#: on this engine, the reply is the only way to learn that anybody arrived or left, so the interval is
#: the latency of every join and part. The classic parser used 3 seconds for that reason.
PLAYERLIST_INTERVAL = 3.0
PLAYERLIST_COMMAND = "PLAYERLIST"

#: A packet cannot plausibly be longer than this; past it the stream is out of step.
MAX_PACKET = 1_000_000
#: Receives per pump, so a chatty server cannot hold the event loop.
MAX_PUMP = 32

#: Reconnect back-off, as used by every other pushed family here.
RETRY_INTERVAL = 5.0
RETRY_MAX = 300.0

#: Handed to the parser after a reconnect. Nothing else can say it: this engine reports no departures,
#: so every player who left while the socket was down is a ghost until the roster is thrown away.
RECONNECTED_NOTICE = "B3-RECONNECTED"


class FrontlineError(Exception):
    """The connection failed, or the server said something this client cannot read."""


class FrontlineAuthError(FrontlineError):
    """The server rejected the login — which it does by hanging up, saying nothing at all."""


def login_response(challenge: str, password: str) -> str:
    """``md5(challenge + password)``, lowercase hex. The whole of the authentication."""
    return hashlib.md5(f"{challenge}{password}".encode()).hexdigest()


def encode(command: str) -> bytes:
    """One outgoing packet: the command, then the terminator."""
    return (command.strip() + TERMINATOR).encode("utf-8")


def decode(buffer: bytes) -> tuple[str, bytes] | None:
    """Read one packet. Returns ``(payload, rest)``, or None if it is not all here yet.

    Splits on ``\\x04`` **only**. A newline is data: the roster arrives as one packet with a line per
    player inside it.
    """
    at = buffer.find(TERMINATOR.encode())
    if at < 0:
        if len(buffer) > MAX_PACKET:
            raise FrontlineError(
                f"{len(buffer)} bytes with no packet terminator; the stream is out of step"
            )
        return None
    return buffer[:at].decode("utf-8", "replace"), buffer[at + 1 :]


class FrontlineClient:
    """Commands and events over one TCP connection: both halves of the bot's input.

    Nothing on this engine can be asked synchronously — there is no correlation between a command and
    the packet that answers it — so :meth:`command` sends and returns immediately, and everything the
    server says reaches the parser as a line.
    """

    blocking = True
    poll_interval = 0.0

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        password: str = "",
        *,
        user: str = "admin",
        timeout: float = 1.0,
        ping_interval: float = PING_INTERVAL,
        playerlist_interval: float = PLAYERLIST_INTERVAL,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        #: This protocol authenticates a *user*, not just a password. `server.rcon_user` in the config.
        self.user = user
        self.timeout = timeout
        self.ping_interval = ping_interval
        self.playerlist_interval = playerlist_interval

        self.connected = False
        self.authed = False
        #: The RCON version the server announced, for the record.
        self.server_version = ""

        self._sock: socket.socket | None = None
        self._buffer = b""
        #: Packets read while waiting for something else. Kept rather than dropped: they are events.
        self._pending: list[str] = []
        self._last_ping = 0.0
        self._last_playerlist = 0.0
        self._retry_at = 0.0
        self._retry_delay = RETRY_INTERVAL

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Connect, log in, and turn the server's reporting on."""
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise FrontlineError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        self._sock.settimeout(self.timeout)
        self._buffer = b""
        self._pending = []
        self.connected = True
        self.authed = False

        challenge = self._await_challenge()
        self._write_raw(f"RESPONSE {self.user} {login_response(challenge, self.password)}")
        self._await_login()

        now = time.monotonic()
        self._last_ping = now
        self._last_playerlist = 0.0  # ask at once: the roster is this engine's only identity source
        self._retry_delay = RETRY_INTERVAL

        # Not a preference: without these the server reports nothing and the bot looks at an empty
        # world. See the module docstring.
        for command in LOGGING_COMMANDS:
            self._write_raw(command)

    def _await_challenge(self) -> str:
        """Read the greeting and return its challenge, which is what the password is hashed against."""
        deadline = time.monotonic() + max(self.timeout, 1.0) * 3
        while self.connected and time.monotonic() < deadline:
            for payload in self._pump():
                match = WELCOME_RE.match(payload.strip())
                if match is not None:
                    self.server_version = match["version"]
                    log.info("frontline: connected, RCON version %s", self.server_version)
                    return match["challenge"].strip()
                self._pending.append(payload)
        self.close()
        raise FrontlineError(
            f"{self.host}:{self.port} sent no RCON greeting; is that the remote console port?"
        )

    def _await_login(self) -> None:
        """Wait for ``Login SUCCESS!`` — and read a closed connection as the rejection it is."""
        deadline = time.monotonic() + max(self.timeout, 1.0) * 3
        while time.monotonic() < deadline:
            for payload in self._pump():
                if payload.strip().lower().startswith(LOGIN_SUCCESS_PREFIX):
                    self.authed = True
                    log.info("frontline: %s", payload.strip())
                    return
                self._pending.append(payload)
            if not self.connected:
                # The whole of the rejection: this server does not say no, it hangs up. Reporting it
                # as a network fault would send an operator to check a firewall that is working.
                self.close()
                raise FrontlineAuthError(
                    f"{self.host}:{self.port} closed the connection without answering the login, "
                    "which is how this engine refuses a wrong user or password"
                )
        self.close()
        raise FrontlineError(f"{self.host}:{self.port} never answered the login")

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.connected = False
        self.authed = False

    # -- reading -----------------------------------------------------------

    def read_lines(self) -> list[str]:
        """Every packet since last time, plus the housekeeping this connection cannot live without.

        Three things happen from here rather than from a thread, which is what keeps the whole client
        single-threaded: the keepalive (ten seconds of silence costs the connection), the periodic
        ``PLAYERLIST`` (this engine's only statement of who is playing), and the reconnect.
        """
        out = self._pending
        self._pending = []
        if not self.connected:
            out.extend(self._try_reconnect())
            return out

        now = time.monotonic()
        if self.ping_interval and now - self._last_ping >= self.ping_interval:
            self._last_ping = now
            self._housekeep(PING_COMMAND)
        if self.playerlist_interval and now - self._last_playerlist >= self.playerlist_interval:
            self._last_playerlist = now
            self._housekeep(PLAYERLIST_COMMAND)

        out.extend(self._pump())
        return out

    def _housekeep(self, command: str) -> None:
        """Send one of this method's own upkeep commands, treating a failure as a lost connection.

        Not `write`: that raises, and this is called from `read_lines`, which the runtime polls. A
        keepalive that fails on a socket the server has already dropped is *how you find out* the
        connection is gone — so it becomes a reconnect on the next cycle rather than an exception
        thrown at a caller who only asked what the server had said.
        """
        try:
            self._write_raw(command)
        except FrontlineError as exc:
            log.warning("frontline: %s failed (%s); treating the connection as lost", command, exc)
            self.connected = False

    def _try_reconnect(self) -> list[str]:
        """Come back after the server went away, on a back-off, and say so when we do.

        The same shape as Homefront's, and needed for the same reason: a client that merely *notices*
        the connection died returns nothing for ever while the bot looks healthy. The notice matters
        more here than anywhere else — this engine reports no departures at all, so every player who
        left during the outage is a ghost until the roster is dropped.
        """
        now = time.monotonic()
        if now < self._retry_at:
            return []
        self._retry_at = now + self._retry_delay
        try:
            self.open()
        except FrontlineAuthError as exc:
            # A rejected login will be rejected again. Back off hard, and say so once, loudly.
            self._retry_delay = RETRY_MAX
            self._retry_at = now + self._retry_delay
            log.error("frontline: %s; not retrying for %.0fs", exc, self._retry_delay)
            return []
        except FrontlineError as exc:
            self._retry_delay = min(self._retry_delay * 2, RETRY_MAX)
            log.warning("frontline: reconnect failed (%s); next try in %.0fs", exc, self._retry_delay)
            return []
        log.info("frontline: reconnected to %s:%s", self.host, self.port)
        return [RECONNECTED_NOTICE]

    def _pump(self) -> list[str]:
        """Read what is there and return whole packets.

        Returns as soon as it has something rather than reading until the socket goes quiet: the only
        way to know nothing more is coming is to let a receive time out, and paying that every cycle
        would put the whole timeout on the latency of everything.
        """
        payloads: list[str] = []
        for _ in range(MAX_PUMP):
            if payloads:
                break
            chunk = self._receive()
            if chunk is None:
                break
            self._buffer += chunk
            while True:
                try:
                    decoded = decode(self._buffer)
                except FrontlineError as exc:
                    log.error("frontline: %s; dropping the connection", exc)
                    self.close()
                    return payloads
                if decoded is None:
                    break
                payload, self._buffer = decoded
                if payload.strip():
                    payloads.append(payload)
        return payloads

    def _receive(self) -> bytes | None:
        if self._sock is None:
            return None
        try:
            chunk = self._sock.recv(8192)
        except TimeoutError:
            return None
        except OSError as exc:
            log.warning("frontline: read failed: %s", exc)
            self.connected = False
            return None
        if not chunk:
            log.warning("frontline: the server closed the connection")
            self.connected = False
            return None
        return chunk

    # -- writing -----------------------------------------------------------

    def command(self, cmd: str) -> str:
        """Send a command. There is no reply to wait for, so this returns immediately.

        Nothing in this protocol correlates an answer with the command that caused it — the server
        just talks — so whatever it says arrives through :meth:`read_lines` and the parser reads it.
        Returning an empty string is honest: this client genuinely does not know the answer.
        """
        self.write(cmd)
        return ""

    def write(self, cmd: str) -> None:
        """Send without waiting, which is the only way anything is sent here."""
        if not cmd.strip():
            return
        if not self.connected:
            raise FrontlineError("not connected")
        self._write_raw(cmd)

    def _write_raw(self, cmd: str) -> None:
        if self._sock is None:
            raise FrontlineError("not connected")
        try:
            self._sock.sendall(encode(cmd))
        except OSError as exc:
            self.connected = False
            raise FrontlineError(f"send failed: {exc}") from exc


__all__ = [
    "DEFAULT_PORT",
    "LOGGING_COMMANDS",
    "LOGIN_SUCCESS_PREFIX",
    "PING_COMMAND",
    "PING_INTERVAL",
    "PLAYERLIST_COMMAND",
    "PLAYERLIST_INTERVAL",
    "RECONNECTED_NOTICE",
    "TERMINATOR",
    "WELCOME_RE",
    "FrontlineAuthError",
    "FrontlineClient",
    "FrontlineError",
    "decode",
    "encode",
    "login_response",
]
