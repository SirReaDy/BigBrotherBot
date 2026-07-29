"""Ravaged's admin protocol: bare lines out, text-length-prefixed frames back, one connection.

Another engine with no game log — the same socket carries commands, their replies and pushed events —
so :class:`RavagedClient` satisfies both the ``RconClient`` and ``LogSource`` contracts and the live
loop is unchanged. What is peculiar here is the framing and the classification.

**The two directions are not the same shape.** The client writes a bare line::

    getplayerlist\\n

and the server answers with a *text* length prefix, in brackets::

    (23)getplayerlist:1 players:…

Anything before the opening bracket is junk to be discarded, which is the classic client's own rule.

**A reply and an event arrive the same way, and are told apart by content**, which is the part worth
being careful about:

* a payload wrapped in ``(…)`` — ``(127.0.0.1:3508 has connected remotely)`` — is an event;
* one starting with ``RCon:(`` is an event;
* ``You must be a superuser to run this command.`` is a reply, to whatever was just sent;
* otherwise ``<command>:<response>`` is the reply to the command it names;
* and anything else is an event.

That ordering is the classic client's, kept because the ambiguity is real: a game-log line could
contain a colon (``"name<id><team>" say "hi: there"``), and the only thing separating it from a reply
is that no command was outstanding by that name.

**What the server echoes is the bare verb, not the command it was sent.** ``kick 76561…  "spam"`` is
answered ``kick:``, and — the part that is easy to get wrong — ``PASS=<hash>`` is answered
``pass:Login success as admin`` rather than a bare verdict. Neither is documented anywhere; both are
forced by the classic client, whose reply pattern rejects a command name containing a space and whose
matcher compares that name, lowercased, against ``login``/``pass``/the first word. Under any other
shape every multi-word command and the login itself would have timed out there, so this is what the
real server does. The verdict is accepted bare as well, because being wrong about it costs a lockout
rather than an error message.

**The login is two steps, and the second one can lock you out.** ``LOGIN=<user>`` is often not answered
at all — the classic client waits for a timeout there and carries on — then
``PASS=<sha1 hex, uppercase>``. A wrong password is answered ``Login failed``; *repeated* attempts get
``Suspicious activity detected``, which is a blacklist rather than a refusal. So a failed login is
never retried here: it would turn a fixable mistake into a locked-out address.
"""

from __future__ import annotations

import hashlib
import logging
import re
import socket
import time

log = logging.getLogger(__name__)

#: The admin port, unless the operator says otherwise.
DEFAULT_PORT = 13550

#: What the server says at each stage of the login. Matched as prefixes, as the classic client does.
LOGIN_SUCCESS = "Login success as "
LOGIN_FAILED = "Login failed"
BLACKLISTED = "Suspicious activity detected"

#: Sent as a handshake check: a command known not to exist, whose refusal proves the connection really
#: works rather than merely being open. The classic client insists on this exact answer.
HANDSHAKE_COMMAND = "testrcon"
UNKNOWN_COMMAND = "Command not found, or invalid parameters given."

#: A reply is the command echoed, a colon, then the body. Non-greedy on the command so the first colon
#: wins, and DOTALL because a reply — the player list, the map list — spans lines.
REPLY_RE = re.compile(r"^(?P<command>[^\s:]+):(?P<response>.*)$", re.DOTALL)

#: Said when a command arrives before the login has been accepted.
NOT_SUPERUSER = "You must be a superuser to run this command."

#: A frame's payload cannot plausibly be longer than this; past it the stream is out of step.
MAX_PAYLOAD = 1_000_000
#: Receives per pump, so a chatty server cannot hold the event loop. Bounded by reads rather than by
#: the clock, for the reason recorded in TODO §2.4.
MAX_PUMP = 32
#: How long to wait for one command's reply before giving up on it.
COMMAND_TIMEOUT = 2.0


class RavagedError(Exception):
    """The connection failed, or the server said something this client cannot read."""


class RavagedAuthError(RavagedError):
    """The server rejected the password."""


class RavagedBlacklistedError(RavagedAuthError):
    """The server has blacklisted this address for too many failed logins.

    Its own category because the response differs: a rejection wants the password fixed, a blacklist
    wants the bot to stop connecting entirely until the server forgets.
    """


def login_hash(password: str) -> str:
    """Uppercase SHA1 hex — and unlike Homefront's, with no spaces in it."""
    return hashlib.sha1(password.encode("utf-8")).hexdigest().upper()


def decode(buffer: bytes) -> tuple[str, bytes] | None:
    """Read one ``(<size>)<payload>`` frame. Returns ``(payload, rest)``, or None if incomplete.

    Junk before the opening bracket is discarded — the classic client's rule, and it is what makes the
    framing recoverable at all: there is no checksum here, so the bracket is the only landmark.
    """
    start = buffer.find(b"(")
    if start == -1:
        return None
    if start:
        log.debug("ravaged: discarding %d byte(s) before a frame header", start)
        buffer = buffer[start:]
    end = buffer.find(b")")
    if end == -1:
        return None
    try:
        size = int(buffer[1:end])
    except ValueError:
        raise RavagedError(f"frame header is not a length: {buffer[: end + 1]!r}") from None
    if size < 0 or size > MAX_PAYLOAD:
        raise RavagedError(f"implausible frame length {size}")
    payload_start = end + 1
    if len(buffer) < payload_start + size:
        return None
    payload = buffer[payload_start : payload_start + size].decode("utf-8", "replace")
    return payload, buffer[payload_start + size :]


def reply_name(cmd: str) -> str:
    """The name the server will echo when it answers ``cmd``, lowercased.

    The verb only — and the login steps are named for the word before the ``=``, not the whole thing.
    """
    cmd = cmd.strip()
    for step in ("LOGIN=", "PASS="):
        if cmd.upper().startswith(step):
            return step[:-1].lower()
    return cmd.split(" ", 1)[0].lower()


def reply_body(payload: str) -> str:
    """A reply's body, with the echoed verb stripped if there is one.

    Used on the login verdict, which arrives as ``pass:Login success as admin`` — and, on a server
    that does not do that, bare. Both are read the same way here.
    """
    match = REPLY_RE.match(payload)
    return match["response"].lstrip() if match else payload


def is_event(payload: str) -> bool:
    """Whether this payload is a pushed game event rather than a reply. See the module docstring."""
    if payload.startswith("(") and payload.endswith(")"):
        return True
    if payload.startswith("RCon:("):
        return True
    if payload == NOT_SUPERUSER:
        return False
    return REPLY_RE.match(payload) is None


class RavagedClient:
    """Commands and events over one TCP connection: both halves of the bot's input.

    Unlike the other pushed engines here, this one *does* answer questions — the player list and the
    map list are synchronous replies — so :meth:`command` really waits for one.
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
        timeout: float = 2.0,
        command_timeout: float = COMMAND_TIMEOUT,
        playerlist_interval: float = 15.0,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.user = user
        self.timeout = timeout
        self.command_timeout = command_timeout
        #: How often to ask for the roster. The reply carries the scores and pings, which no log line
        #: does, so somebody has to ask — the classic parser used a 15-second cron thread; this comes
        #: off the same pump the events do, and needs no thread. 0 disables it.
        self.playerlist_interval = playerlist_interval
        self._last_playerlist = 0.0

        self.connected = False
        self.authed = False
        self._sock: socket.socket | None = None
        self._buffer = b""
        #: Events read while waiting for a reply. Kept rather than dropped: they are as real as any
        #: other, and a kill that happened during a `!status` is still a kill.
        self._pending: list[str] = []

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Connect and log in.

        A refusal is **not** retried. Repeated attempts earn a blacklist from this server, so turning
        a wrong password into a lockout is the one failure mode worth being careful of here.
        """
        self.close()
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise RavagedError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        self._sock.settimeout(self.timeout)
        self._buffer = b""
        self._pending = []
        self.connected = True
        self.authed = False
        self._last_playerlist = time.monotonic()

        # The user step is frequently not answered at all; the classic client waits out a timeout and
        # carries on, so a silent reply here is expected rather than a fault.
        self._write_line(f"LOGIN={self.user}")
        verdict = self._await_login()
        if verdict.startswith(BLACKLISTED):
            self.close()
            raise RavagedBlacklistedError(
                f"{self.host}:{self.port} has blacklisted this address after too many failed "
                "logins; stop connecting until the server forgets, then fix server.rcon_password"
            )
        if not verdict.startswith(LOGIN_SUCCESS):
            self.close()
            raise RavagedAuthError(f"the server refused the password ({verdict or 'no answer'})")
        self.authed = True
        log.info("ravaged: %s", verdict)

        # And prove commands actually work, rather than trusting an accepted password.
        answer = self.command(HANDSHAKE_COMMAND)
        if answer.strip() != UNKNOWN_COMMAND:
            self.close()
            raise RavagedError(
                f"logged in, but {HANDSHAKE_COMMAND!r} answered {answer!r} instead of the expected "
                "refusal, so this connection cannot be trusted to carry commands"
            )

    def _await_login(self) -> str:
        """Send the password **once**, then read until the verdict arrives or the deadline passes.

        Once, deliberately: a second `PASS=` counts as a second attempt on the server's side, and it
        is attempts that earn the blacklist. So the loop here reads, it does not ask again — anything
        that is not the verdict (an event pushed while we were logging in) is kept rather than
        dropped, because it is as real as any other event.
        """
        self._write_line(f"PASS={login_hash(self.password)}")
        deadline = time.monotonic() + max(self.timeout, 2.0) * 2
        while self.connected and time.monotonic() < deadline:
            for payload in self._pump():
                # The verdict is the *body* of a `pass:` reply, so the echoed verb comes off first.
                body = reply_body(payload)
                if body.startswith((LOGIN_SUCCESS, LOGIN_FAILED, BLACKLISTED)):
                    return body
                self._pending.append(payload)
        return ""

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
        """Every game event since last time, plus the roster reply when one is due.

        Replies are otherwise not events and never appear here. The player list is the exception on
        purpose: it is the only way to learn a score or a ping on this engine, so the client asks for it
        on a timer and passes it through as though the server had volunteered it. The parser reads it
        with the same router it reads everything else — see `RavParser.on_player_list`.
        """
        out = self._pending
        self._pending = []
        if not self.connected:
            return out

        now = time.monotonic()
        if self.playerlist_interval and now - self._last_playerlist >= self.playerlist_interval:
            self._last_playerlist = now
            try:
                reply = self.command("getplayerlist")
            except RavagedError as exc:
                log.debug("ravaged: could not refresh the player list: %s", exc)
            else:
                # Events seen while waiting for that reply happened *before* it, so they go first --
                # otherwise a disconnect would be reported after a roster that still listed them.
                out.extend(self._pending)
                self._pending = []
                if reply.strip():
                    out.append(reply)

        out.extend(p for p in self._pump() if is_event(p))
        return out

    def _pump(self) -> list[str]:
        """Read what is there and return whole payloads, replies and events alike.

        Returns as soon as it has *something*, rather than reading until the socket goes quiet: the
        only way to know nothing more is coming is to let a receive time out, and paying that on every
        command would put the socket's whole timeout on the latency of every `!kick`.
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
                except RavagedError as exc:
                    log.error("ravaged: %s; dropping the connection", exc)
                    self.close()
                    return payloads
                if decoded is None:
                    break
                payload, self._buffer = decoded
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
            log.warning("ravaged: read failed: %s", exc)
            self.connected = False
            return None
        if not chunk:
            log.warning("ravaged: the server closed the connection")
            self.connected = False
            return None
        return chunk

    # -- writing -----------------------------------------------------------

    def command(self, cmd: str) -> str:
        """Send a command and wait for its reply, keeping any events that arrive meanwhile.

        The reply is matched by the command name the server echoes back, so a game-log line that
        happens to look like ``something:something`` cannot be mistaken for the answer.
        """
        cmd = cmd.strip()
        if not cmd:
            log.debug("ravaged: refusing to send an empty command")
            return ""
        if not self.connected:
            raise RavagedError("not connected")
        self._write_line(cmd)

        wanted = reply_name(cmd)
        deadline = time.monotonic() + self.command_timeout
        while time.monotonic() < deadline and self.connected:
            for payload in self._pump():
                if is_event(payload):
                    self._pending.append(payload)
                    continue
                if payload == NOT_SUPERUSER:
                    raise RavagedError(f"{cmd!r}: {NOT_SUPERUSER}")
                match = REPLY_RE.match(payload)
                # Case-insensitively: the echoed verb's case is not something to depend on, and the
                # classic client did not either.
                if match is not None and match["command"].lower() == wanted:
                    return match["response"]
                # A reply to something else — a command that timed out earlier. Not ours to return.
                log.debug("ravaged: ignoring a late reply to %r", match["command"] if match else "?")
        log.warning("ravaged: no reply to %r within %.1fs", cmd, self.command_timeout)
        return ""

    def write(self, cmd: str) -> None:
        """Send without waiting for the reply — chat output, where nothing reads the answer."""
        if not cmd.strip():
            return
        if not self.connected:
            raise RavagedError("not connected")
        self._write_line(cmd)

    def _write_line(self, line: str) -> None:
        if self._sock is None:
            raise RavagedError("not connected")
        try:
            self._sock.sendall((line + "\n").encode("utf-8"))
        except OSError as exc:
            self.connected = False
            raise RavagedError(f"send failed: {exc}") from exc


__all__ = [
    "BLACKLISTED",
    "COMMAND_TIMEOUT",
    "DEFAULT_PORT",
    "HANDSHAKE_COMMAND",
    "LOGIN_FAILED",
    "LOGIN_SUCCESS",
    "NOT_SUPERUSER",
    "UNKNOWN_COMMAND",
    "RavagedAuthError",
    "RavagedBlacklistedError",
    "RavagedClient",
    "RavagedError",
    "decode",
    "is_event",
    "login_hash",
    "reply_body",
    "reply_name",
]
