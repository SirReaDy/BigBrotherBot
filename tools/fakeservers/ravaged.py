"""A fake Ravaged server: parenthesised length prefixes, a two-step login, and a blacklist.

Ravaged's admin protocol is line-ish TCP with a framing nobody would guess:

* **What the client sends is a bare line** — ``<command>\\n``, utf-8, no prefix at all.
* **What the server sends is length-prefixed in text** — ``(<decimal size>)<payload>``, and anything
  before that opening bracket is junk to be discarded. So the two directions are not the same shape,
  which is the same trap Homefront sets, arrived at from a different direction.
* **A reply and an event travel the same way**, and are told apart by *content*: a payload wrapped in
  brackets or starting with ``RCon:(`` is an event, and otherwise ``<verb>:<response>`` is a reply to
  the command it names. A fake that answered replies on one channel and events on another would hide
  the whole problem.
* **What gets echoed is the bare verb** — ``kick 76561… "spam"`` is answered ``kick:`` — and the login
  verdict is the body of a ``pass:`` reply rather than a message of its own. Neither is documented
  anywhere; both are forced by the classic client, whose reply pattern rejects a name containing a
  space and which matched that name against ``login``/``pass``/the first word. Under any other shape
  every command with an argument would have timed out there. See :meth:`_reply`.

The login is two steps and the second one matters: ``LOGIN=<user>`` may simply not be answered — the
classic client tolerates a timeout there — and then ``PASS=<sha1 hex, uppercase>``. A wrong password
gets ``Login failed``; **too many attempts get ``Suspicious activity detected``**, which is a server-side
blacklist rather than a rejection, and a bot that retried through it would earn a longer one. This fake
models that (:attr:`max_attempts`), because it is the difference between "fix your password" and "you
are now locked out".

    python -m tools.fakeservers.ravaged --password test
"""

from __future__ import annotations

import hashlib
import socket
import threading
import time

#: The port the game listens on for admin connections, unless told otherwise.
DEFAULT_PORT = 13550

#: What the server says. Kept as constants because the client matches on them, and a typo in either
#: place would look like a protocol change.
LOGIN_SUCCESS = "Login success as "
LOGIN_FAILED = "Login failed"
BLACKLISTED = "Suspicious activity detected"
#: The reply to an unknown command — which the classic client uses as a positive handshake check, by
#: sending a command it knows does not exist and insisting on exactly this answer.
UNKNOWN_COMMAND = "Command not found, or invalid parameters given."
#: Said, bare and with no verb echoed in front of it, when a command arrives before the login.
NOT_SUPERUSER = "You must be a superuser to run this command."


def expected_login(password: str) -> str:
    """The password as the game wants it: uppercase SHA1 hex, no spaces (unlike Homefront's)."""
    return hashlib.sha1(password.encode("utf-8")).hexdigest().upper()


class FakeRavagedServer:
    """One connection at a time, on a thread, in the shape the other fakes here use."""

    def __init__(
        self,
        password: str = "test",
        user: str = "admin",
        *,
        max_attempts: int = 3,
        fragment: int = 0,
    ) -> None:
        self.password = password
        self.user = user
        #: Failed logins before this address is blacklisted rather than merely refused.
        self.max_attempts = max_attempts
        #: If set, replies are written in slices of this size — which is how the reassembly of a
        #: length-prefixed stream gets proved rather than assumed.
        self.fragment = fragment

        #: Every command received, in order.
        self.received: list[str] = []
        self.authed = False
        self.attempts = 0
        self.blacklisted = False
        #: steam id -> (name, score, kills, deaths, ping)
        self.players: dict[str, tuple[str, int, int, int, int]] = {}
        #: steam id -> (reason, days)
        self.bans: dict[str, tuple[str, str]] = {}
        #: The rotation, in order. The *first* entry is the current map — see `getmaplist`.
        self.maps: list[str] = [
            "CTR_Bridge",
            "CTR_Canyon",
            "CTR_Derelict",
            "Thrust_Oilrig",
        ]

        self._sock: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._address: tuple[str, int] | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    @property
    def address(self) -> tuple[str, int]:
        assert self._address is not None, "start() first"
        return self._address

    def start(self) -> "FakeRavagedServer":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._address = self._sock.getsockname()[:2]
        self._sock.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

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

    # -- the game telling the bot things -----------------------------------

    def push(self, data: str) -> None:
        """Send a game-log line, framed the way this server frames everything."""
        self._send(data)

    def add_player(
        self, guid: str, name: str, team: str = "1", ip: str = "192.168.0.1"
    ) -> None:
        """Put a player on the server and announce it the way the game does — three separate lines."""
        with self._lock:
            self.players[guid] = (name, 0, 0, 0, 40)
        # The connect line carries no name yet: the game does not know it at that point.
        self.push(f'"<{guid}><>" connected, address "{ip}"')
        self.push(f'"{name}<{guid}><{team}>" entered the game')
        self.push(f'"{name}<{guid}><{team}>" joined team "{team}"')

    def load_map(self, name: str) -> None:
        """Change map the way the server does: rotate the list, then announce it.

        The rotation and the map being played are one thing here, not two — `getmaplist` reports the
        current map at index 0 — so a fake that announced a map without moving its own list would
        answer `!nextmap` with the wrong map and nobody would notice.
        """
        with self._lock:
            if name in self.maps:
                while self.maps[0] != name:
                    self.maps.append(self.maps.pop(0))
            else:
                self.maps.insert(0, name)
        self.push(f'Loading map "{name}"')
        self.push("Round started")

    def remove_player(self, guid: str, team: str = "1") -> None:
        with self._lock:
            entry = self.players.pop(guid, None)
        name = entry[0] if entry else ""
        # No space before the word, which is what the server really writes.
        self.push(f'"{name}<{guid}><{team}>"disconnected')

    def say_as(self, guid: str, name: str, text: str, team: str = "1", scope: str = "") -> None:
        """A player talking. The game wraps chat in a colour tag, and prefixes team chat."""
        tag = "<FONT COLOR='#FF0000'> "
        if scope == "team":
            self.push(f'"{name}<{guid}><{team}>" say_team "(Team) {tag}{text}"')
        else:
            self.push(f'"{name}<{guid}><{team}>" say "{tag}{text}"')

    # -- the wire ----------------------------------------------------------

    def _send(self, payload: str) -> None:
        conn = self._conn
        if conn is None:
            return
        raw = payload.encode("utf-8")
        frame = f"({len(raw)})".encode() + raw
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
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
            except TimeoutError:
                continue
            except OSError:
                break
            # The client sends bare lines, so that is what is read here.
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                self._handle(line.decode("utf-8", "replace").strip())
        try:
            conn.close()
        except OSError:
            pass

    def _handle(self, command: str) -> None:
        if not command:
            return
        self.received.append(command)

        if command.startswith("LOGIN="):
            # Deliberately unanswered, which is what the real one does and what the classic client
            # tolerates with a timeout. A fake that replied would hide that the wait is expected.
            return
        if command.startswith("PASS="):
            self._login(command[len("PASS=") :])
            return
        if not self.authed:
            # Sent bare, with no verb echoed in front of it: the classic client compares the whole
            # payload against this exact string, so that is the shape the real server uses.
            self._send(NOT_SUPERUSER)
            return
        self._execute(command)

    def _login(self, digest: str) -> None:
        """The verdict, which arrives as the *body of a `pass:` reply* rather than on its own.

        Same evidence as :meth:`_reply`: the classic client waited for a reply named `pass`, and a bare
        `Login success as admin` would have been read as a game event there and timed out. So the
        verdict is framed like any other answer, and a client that only recognises the bare form never
        logs in.
        """
        if self.blacklisted:
            self._reply("PASS=", BLACKLISTED)
            return
        if digest == expected_login(self.password):
            self.authed = True
            self.attempts = 0
            self._reply("PASS=", f"{LOGIN_SUCCESS}{self.user}")
            return
        self.attempts += 1
        if self.attempts >= self.max_attempts:
            self.blacklisted = True
            self._reply("PASS=", BLACKLISTED)
        else:
            self._reply("PASS=", f"{LOGIN_FAILED} ({self.attempts})")

    def _reply(self, command: str, response: str) -> None:
        """A command's answer: the **verb** echoed, a colon, then the body.

        The verb only — `kick 76561… "spam"` is answered `kick:`, not with the whole line echoed back.
        That is not documented anywhere, but it is the only shape the classic client could work with:
        its reply pattern rejects a command name containing a space, so a full echo would have been
        classified as a game event and every multi-word command would have timed out. A fake that
        echoed the whole line would let a bot that gets this wrong pass its tests and then fail to
        kick anybody on a real server.
        """
        self._send(f"{self._reply_name(command)}:{response}")

    @staticmethod
    def _reply_name(command: str) -> str:
        """What gets echoed: the verb, or the word before the `=` on the two login steps."""
        for step in ("LOGIN=", "PASS="):
            if command.upper().startswith(step):
                return step[:-1].lower()
        return command.split(" ", 1)[0]

    def _execute(self, command: str) -> None:
        verb = command.split(" ", 1)[0]
        if command == "testrcon":
            # The handshake check: the classic client sends a command it knows is unknown and insists
            # on exactly this answer before believing the connection works.
            self._reply(command, UNKNOWN_COMMAND)
            return
        if command == "getplayerlist":
            with self._lock:
                roster = list(self.players.items())
            rows = "\n".join(
                f"{name} {score} pts {kills}:{deaths} {ping}ms steamid: {guid}"
                for guid, (name, score, kills, deaths, ping) in roster
            )
            self._reply(command, f"{len(roster)} players:\n{rows}\n" if rows else "0 players:\n")
            return
        if command.startswith("getmaplist"):
            with self._lock:
                listing = "\n".join(f"{i} {m}" for i, m in enumerate(self.maps))
            self._reply(command, f"{listing}\n")
            return
        if verb == "addmap":
            # `addmap <name> <position>` -- the position is what makes the classic parser's
            # `addmap X 1` + `nextmap` pair change map *now* rather than eventually.
            parts = command.split()
            if len(parts) >= 2:
                try:
                    position = int(parts[2]) if len(parts) > 2 else len(self.maps)
                except ValueError:
                    position = len(self.maps)
                with self._lock:
                    self.maps.insert(min(max(position, 0), len(self.maps)), parts[1])
            self._reply(command, "")
            return
        if command == "nextmap":
            with self._lock:
                if len(self.maps) > 1:
                    self.maps.append(self.maps.pop(0))
                current = self.maps[0]
            self._reply(command, "")
            self.push(f'Loading map "{current}"')
            self.push("Round started")
            self.push('Round finished, winning team is "0"')
            return
        if verb == "kick":
            guid = command.split()[1] if len(command.split()) > 1 else ""
            if guid in self.players:
                self.remove_player(guid)
            self._reply(command, "")
            return
        if verb == "kickban":
            parts = command.split(" ", 1)[1] if " " in command else ""
            guid = parts.split(" ", 1)[0] if parts else ""
            days = parts.rsplit(" ", 1)[-1] if parts else ""
            reason = parts[len(guid) : -len(days) or None].strip().strip('"') if parts else ""
            with self._lock:
                self.bans[guid] = (reason, days)
            if guid in self.players:
                self.remove_player(guid)
            self._reply(command, "")
            return
        if verb == "unban":
            guid = command.split()[1] if len(command.split()) > 1 else ""
            with self._lock:
                self.bans.pop(guid, None)
            self._reply(command, "")
            return
        if verb in ("say", "playersay"):
            # Not echoed back: unlike Homefront and Altitude, this server does not repeat the bot's
            # own output to it. Worth being faithful about — it is one less loop to guard against.
            self._reply(command, "")
            return
        self._reply(command, UNKNOWN_COMMAND)

    # -- helpers for tests -------------------------------------------------

    def sent_command(self, prefix: str) -> bool:
        return any(c.startswith(prefix) for c in self.received)

    def wait_for(self, predicate, timeout: float = 3.0) -> bool:  # noqa: ANN001
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False


def _demo() -> None:  # pragma: no cover - manual aid
    import argparse

    ap = argparse.ArgumentParser(description="a fake Ravaged server")
    ap.add_argument("--password", default="test")
    args = ap.parse_args()

    server = FakeRavagedServer(password=args.password).start()
    server.add_player("12312312312312312", "courgette")
    print(f"listening on {server.address[0]}:{server.address[1]}")
    print("expected PASS= digest:", expected_login(args.password))
    try:
        while True:
            time.sleep(1)
            print(f"  authed={server.authed} received={len(server.received)} bans={server.bans}")
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    _demo()
