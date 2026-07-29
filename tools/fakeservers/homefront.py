"""A fake Homefront server: length-prefixed TCP, a SHA1 login, and a connection that dies if ignored.

Homefront's admin protocol is its own thing, and three of its properties are the sort that only a
running server proves:

* **The framing is asymmetric.** A packet *from* the client is ``type(2) + length(4) + data``; a
  packet *from* the server is ``type(2) + channel(1) + length(4) + data``. Six bytes one way, seven
  the other. A codec that assumes symmetry reads every reply one byte out of step, and the symptom is
  garbage rather than an error — so this fake insists on the client's shape and always sends its own.
* **The login is a hash with spaces in it.** ``PASS: "<sha1 hex, uppercase, in pairs>"`` — the
  password never crosses the wire, and neither an unspaced nor a lowercase digest is accepted here,
  because neither is accepted by the game.
* **Silence kills the connection.** The real server drops a client after ten seconds without traffic,
  so a bot that does not ping is a bot that mysteriously stops working after ten seconds. This models
  it (:attr:`idle_timeout`), which is the only way to prove the keepalive is real rather than assumed.

State is modelled rather than canned, for the reason the other fakes do it: it keeps a roster it
answers ``RETRIEVE PLAYERLIST`` from, and a ban list that ``admin kickban`` and ``admin unban``
actually change. It also **echoes the bot's own chat back on the chatter channel**, which the real
server does — a bot that reads that channel as player speech answers itself forever.

    python -m tools.fakeservers.homefront --password test
"""

from __future__ import annotations

import hashlib
import socket
import threading
import time
from struct import pack, unpack

#: Message types. Two ASCII characters, and the first letter says who may send it.
CLIENT_CONNECT = b"CC"
CLIENT_TRANSMISSION = b"CT"
CLIENT_DISCONNECT = b"CD"
CLIENT_PING = b"CP"
SERVER_ANNOUNCE = b"SA"
SERVER_RESPONSE = b"SR"
SERVER_DISCONNECT = b"SD"
SERVER_TRANSMISSION = b"ST"

#: Channels a server message can arrive on.
CHANNEL_BROADCAST = 0
CHANNEL_NORMAL = 1
CHANNEL_CHATTER = 2
CHANNEL_GAMEPLAY = 3
CHANNEL_SERVER = 4

#: How long the real server tolerates silence before hanging up.
IDLE_TIMEOUT = 10.0


def expected_login(password: str) -> str:
    """The exact string the game wants: uppercase SHA1 hex, split into space-separated pairs."""
    digest = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    return " ".join(digest[i : i + 2] for i in range(0, len(digest), 2))


class FakeHomefrontServer:
    """One connection at a time, on a thread, exactly like the other fakes here."""

    def __init__(
        self,
        password: str = "test",
        version: str = "1.0.0.0",
        *,
        idle_timeout: float = IDLE_TIMEOUT,
        fragment: int = 0,
    ) -> None:
        self.password = password
        self.version = version
        #: Seconds of client silence before the server hangs up. 0 disables it.
        self.idle_timeout = idle_timeout
        #: If set, replies are written in slices of this many bytes — which is how the stream
        #: reassembly gets proved rather than assumed. The Frostbite fake earns its keep the same way.
        self.fragment = fragment

        #: Every command the client sent, in order, unframed.
        self.received: list[str] = []
        #: Frames that did not obey the client's 6-byte header shape.
        self.malformed: list[bytes] = []
        self.authed = False
        self.pings = 0
        #: steam id -> (team, clan, name, kills, deaths)
        self.players: dict[str, tuple[str, str, str, int, int]] = {}
        #: steam id -> name
        self.bans: dict[str, str] = {}
        #: True once the server hung up because the client stopped talking.
        self.dropped_for_silence = False

        self._sock: socket.socket | None = None
        self._conn: socket.socket | None = None
        #: Remembered at bind time, so the address survives `stop()` — which is what makes `restart()`
        #: usable in the order it is actually needed: the server goes away *first*, then comes back.
        self._address: tuple[str, int] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def address(self) -> tuple[str, int]:
        assert self._address is not None, "start() first"
        return self._address

    def start(self) -> "FakeHomefrontServer":
        return self._listen(("127.0.0.1", 0))

    def _listen(self, where: tuple[str, int]) -> "FakeHomefrontServer":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(where)
        host, port = self._sock.getsockname()[:2]
        self._address = (host, port)
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def restart(self) -> "FakeHomefrontServer":
        """Come back on the *same* address, as a restarted game server does.

        Which is the interesting case: this engine closes the socket on a restart, a map change, or ten
        seconds of quiet, so "the server went away and came back" is ordinary rather than exotic — and a
        bot that only noticed the first half is a bot that is deaf for the rest of the evening.
        """
        where = self.address
        self.stop()
        self._stop = threading.Event()
        self._conn = None
        self.authed = False
        self.dropped_for_silence = False
        return self._listen(where)

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._conn, self._sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    # -- the player roster this server believes in -------------------------

    def add_player(
        self, uid: str, name: str, team: str = "1", clan: str = "", kills: int = 0, deaths: int = 0
    ) -> None:
        """Put a player on the server, and tell the bot the way the game does."""
        with self._lock:
            self.players[uid] = (team, clan, name, kills, deaths)
        self.push(f"LOGIN: {name} {uid}", CHANNEL_SERVER)
        self.push(f"UID: {name} {uid}", CHANNEL_SERVER)
        # And the team, because that is the only message that carries one: a bot which never sees a
        # TEAM CHANGE has no idea who is on whose side, and every team kill reads as a plain kill.
        self.push(f"TEAM CHANGE: {uid} {team}", CHANNEL_SERVER)

    def remove_player(self, uid: str) -> None:
        with self._lock:
            self.players.pop(uid, None)
        self.push(f"LOGOUT: {uid}", CHANNEL_SERVER)

    def say_as(self, name: str, text: str, kind: str = "") -> None:
        """A player talking. ``kind`` is "team" or "squad" for the restricted channels.

        Sent on the chatter channel *twice*, as the real server does: once as a BROADCAST, and once as
        the plain repeat that every chatter message also produces.
        """
        # A space always, and the scope in parentheses only for the restricted channels:
        # "Bob says: hi" or "Bob (team)says: hi". Losing that space is not cosmetic -- the parser's
        # pattern requires it, exactly as the classic one does, so this fake got it wrong first and
        # the end-to-end run caught it.
        marker = f" ({kind})" if kind else " "
        self.push(f"BROADCAST: {name}{marker}says: {text}", CHANNEL_CHATTER)
        self.push(f"{name}: {text}", CHANNEL_CHATTER)

    # -- the wire ----------------------------------------------------------

    def push(self, data: str, channel: int = CHANNEL_SERVER) -> None:
        """Send a server message on ``channel``."""
        self._send(SERVER_TRANSMISSION, channel, data)

    def _send(self, message: bytes, channel: int, data: str) -> None:
        conn = self._conn
        if conn is None:
            return
        payload = data.encode("utf-8")
        frame = message[:2] + pack(">B", channel) + pack(">i", len(payload)) + payload
        try:
            if self.fragment:
                for start in range(0, len(frame), self.fragment):
                    conn.sendall(frame[start : start + self.fragment])
                    time.sleep(0.001)
            else:
                conn.sendall(frame)
        except OSError:
            pass

    def _serve(self) -> None:
        assert self._sock is not None
        try:
            conn, _addr = self._sock.accept()
        except OSError:
            return
        self._conn = conn
        conn.settimeout(0.2)
        # The server speaks first: its version, on the server channel.
        self.push(f"HELLO: {self.version}", CHANNEL_SERVER)

        buffer = b""
        last_heard = time.time()
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                last_heard = time.time()
            except TimeoutError:
                chunk = b""
            except OSError:
                break

            if self.idle_timeout and time.time() - last_heard > self.idle_timeout:
                # What the real server does, and the reason the client must keep pinging.
                self.dropped_for_silence = True
                break

            buffer = self._consume(buffer)
        try:
            conn.close()
        except OSError:
            pass

    def _consume(self, buffer: bytes) -> bytes:
        """Read whole client frames out of the buffer: type(2) + length(4) + data. No channel byte."""
        while len(buffer) >= 6:
            message = buffer[0:2]
            (length,) = unpack(">i", buffer[2:6])
            if length < 0 or length > 1_000_000:
                # Nothing to resynchronise on, so this is fatal rather than skippable — the same call
                # the Frostbite codec makes, and for the same reason.
                self.malformed.append(buffer[:16])
                return b""
            if len(buffer) < 6 + length:
                return buffer  # a partial frame: wait for the rest
            data = buffer[6 : 6 + length].decode("utf-8", "replace")
            buffer = buffer[6 + length :]
            self._handle(message, data)
        return buffer

    def _handle(self, message: bytes, data: str) -> None:
        if message == CLIENT_PING:
            self.pings += 1
            self.push("PONG", CHANNEL_SERVER)
            return
        if message == CLIENT_DISCONNECT:
            self._stop.set()
            return
        if message != CLIENT_TRANSMISSION:
            self.malformed.append(message)
            return

        self.received.append(data)
        if data.startswith('PASS: "'):
            self.authed = data == f'PASS: "{expected_login(self.password)}"'
            self.push(f"AUTH: {'true' if self.authed else 'false'}", CHANNEL_SERVER)
            return
        if not self.authed:
            # A real server ignores commands from a client that has not logged in, and a fake that
            # obeys them anyway would hide a login the bot never completed.
            return
        self._execute(data)

    def _execute(self, command: str) -> None:
        if command == "RETRIEVE PLAYERLIST":
            with self._lock:
                roster = list(self.players.items())
            for uid, (team, clan, name, kills, deaths) in roster:
                # Newline-separated fields inside one message, which is why the transport cannot use
                # newlines as its own delimiter.
                self.push(f"PLAYER: {uid}\n{team}\n{clan}\n{name}\n{kills}\n{deaths}")
            return
        if command == "RETRIEVE BANLIST":
            with self._lock:
                bans = list(self.bans.items())
            for uid, name in bans:
                self.push(f"BAN ITEM: {name} {uid}")
            return
        if command.startswith("adminsay ") or command.startswith("adminbigsay "):
            # Echoed back on the chatter channel, exactly as the real server does. A bot that reads
            # that as a player talking will answer its own announcements.
            said = command.split(" ", 1)[1]
            self.push(f"Server: {said}", CHANNEL_CHATTER)
            return
        if command.startswith("adminpm "):
            return  # a private message is not echoed anywhere
        if command.startswith("admin kickban "):
            uid = _first_quoted(command)
            name = self._name_of(uid)
            with self._lock:
                self.bans[uid] = name or uid
            self.push(f"BAN ADDED: {name or uid} {uid}")
            self.remove_player(uid)
            return
        if command.startswith("admin kick "):
            uid = _first_quoted(command)
            if uid in self.players:
                self.remove_player(uid)
            return
        if command.startswith("admin unban "):
            uid = command.split(" ", 2)[2].strip().strip('"')
            with self._lock:
                existed = self.bans.pop(uid, None)
            if existed is not None:
                self.push(f"BAN REMOVE: {uid}")
            return
        if command == "admin nextmap":
            self.push("CHANGE LEVEL: fl-harbor")
            return

    def _name_of(self, uid: str) -> str:
        with self._lock:
            entry = self.players.get(uid)
        return entry[2] if entry else ""

    # -- helpers for tests -------------------------------------------------

    def wait_for(self, predicate, timeout: float = 3.0) -> bool:  # noqa: ANN001
        """Wait for something to become true on the server side. Returns False on timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def sent_command(self, prefix: str) -> bool:
        return any(c.startswith(prefix) for c in self.received)


def _first_quoted(command: str) -> str:
    """The first quoted argument of a command, or the last word if it is unquoted."""
    if '"' in command:
        return command.split('"')[1]
    return command.rsplit(" ", 1)[-1]


def _demo() -> None:  # pragma: no cover - manual aid
    import argparse

    ap = argparse.ArgumentParser(description="a fake Homefront server")
    ap.add_argument("--password", default="test")
    args = ap.parse_args()

    server = FakeHomefrontServer(password=args.password).start()
    server.add_player("76561197963239764", "Courgette", team="1")
    print(f"listening on {server.address[0]}:{server.address[1]}, password {args.password!r}")
    print("expected login string:", expected_login(args.password))
    try:
        while True:
            time.sleep(1)
            print(f"  authed={server.authed} pings={server.pings} received={len(server.received)}")
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    _demo()
