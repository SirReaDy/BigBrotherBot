"""A fake Frostbite RCON server — Battlefield / Medal of Honor.

Speaks the real binary protocol, including the parts that are easy to get subtly wrong:

* **TCP is a stream, not messages.** This server can be told to dribble its replies out in small
  chunks (``chunk_size``), which is what proves a client reassembles by the size field rather than
  hoping each read is one packet.
* **The login is a challenge-response.** It generates a real salt and checks a real
  ``md5(salt + password)``, so a client that gets the hashing wrong fails here rather than in
  production.
* **Events must be acknowledged.** Server-originated packets are resent until answered, so a client
  that forgets stalls visibly.
* **Events are opt-in.** Nothing is pushed until ``admin.eventsEnabled true`` arrives — the mistake
  that makes a perfectly connected bot look broken.

Run it standalone to watch what a bot sends::

    python -m tools.fakeservers.frostbite --password test
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import socket
import threading
import time
from collections import deque

from b3.net.frostbite import HEADER_SIZE, decode, encode, packet_length

log = logging.getLogger(__name__)

#: A fixed salt, so a test can compute the expected hash without reaching into the server.
SALT_HEX = "1234567890ABCDEF1234567890ABCDEF"

#: The default `admin.listPlayers` answer: the protocol's self-describing block — a field count, the
#: field names, a player count, then that many values each.
DEFAULT_PLAYER_BLOCK = [
    "7",
    "name",
    "guid",
    "teamId",
    "squadId",
    "kills",
    "deaths",
    "score",
    "2",
    "Bravo17",
    "EA_00000000000000000000000000000001",
    "1",
    "0",
    "12",
    "3",
    "1500",
    "Bob",
    "EA_00000000000000000000000000000002",
    "2",
    "1",
    "0",
    "9",
    "20",
]


class FakeFrostbiteServer:
    """One thread, one TCP listener, and word lists in both directions."""

    def __init__(
        self,
        password: str = "test",
        *,
        replies: dict[str, list[str]] | None = None,
        chunk_size: int | None = None,
        resend_unacked_after: float = 1.0,
    ) -> None:
        self.password = password
        #: first word -> the words to answer with, *after* the leading OK.
        self.replies: dict[str, list[str]] = {
            "version": ["BF3", "1234567"],
            "admin.listPlayers": DEFAULT_PLAYER_BLOCK,
            "serverInfo": ["my server", "2", "16", "ConquestLarge0", "MP_001"],
            **(replies or {}),
        }
        #: Every command received, as word lists.
        self.received: list[list[str]] = []
        #: Sequence numbers the client has acknowledged.
        self.acked: list[int] = []
        self.logged_in = False
        self.events_enabled = False
        #: Set False to reject the password even when the hash is right.
        self.accept_login = True
        #: Send replies in slices this big, to exercise the client's stream reassembly.
        self.chunk_size = chunk_size
        self.resend_unacked_after = resend_unacked_after
        #: banList.add/remove keep real state, so a `!unban` can be checked properly.
        self.bans: list[tuple[str, str, str]] = []  # (id_type, target, reason)

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._listener.settimeout(0.05)
        self.address: tuple[str, int] = self._listener.getsockname()
        self._conn: socket.socket | None = None
        self._buffer = b""
        self._event_seq = 0
        self._pending: deque[tuple[int, bytes, float]] = deque()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-frostbite", daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> FakeFrostbiteServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        if self._conn is not None:
            self._conn.close()
        self._listener.close()

    def __enter__(self) -> FakeFrostbiteServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- driving it from a test ---------------------------------------------

    def push(self, words: list[str]) -> int:
        """Send a game event. Returns its sequence, so a test can check the acknowledgement."""
        sequence = self._event_seq
        self._event_seq = (self._event_seq + 1) & 0x3FFFFFFF
        packet = encode(words, sequence, from_server=True)
        self._pending.append((sequence, packet, time.monotonic()))
        self._send(packet)
        return sequence

    def wait_for_command(self, needle: str, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in " ".join(words) for words in self.received):
                return True
            time.sleep(0.01)
        return False

    def wait_for_ack(self, sequence: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if sequence in self.acked:
                return True
            time.sleep(0.01)
        return False

    # -- the server ---------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            if self._conn is None:
                try:
                    self._conn, _addr = self._listener.accept()
                    self._conn.settimeout(0.05)
                    self._buffer = b""
                except (TimeoutError, OSError):
                    continue
            self._resend_unacked()
            try:
                chunk = self._conn.recv(8192)
            except (TimeoutError, BlockingIOError):
                continue
            except OSError:
                self._drop()
                continue
            if not chunk:
                self._drop()
                continue
            self._buffer += chunk
            while (size := packet_length(self._buffer)) is not None:
                packet, self._buffer = self._buffer[:size], self._buffer[size:]
                self._handle(decode(packet))

    def _drop(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None
        self.logged_in = False
        self.events_enabled = False

    def _handle(self, packet) -> None:  # noqa: ANN001 - b3.net.frostbite.Packet
        if packet.from_server:
            # Our own event, acknowledged by the client.
            self.acked.append(packet.sequence)
            self._pending = deque(p for p in self._pending if p[0] != packet.sequence)
            return
        if packet.is_response:
            return
        self.received.append(packet.words)
        self._send(encode(self._answer(packet.words), packet.sequence, response=True))

    def _answer(self, words: list[str]) -> list[str]:
        """What the server replies. The leading word is always OK or the name of the problem."""
        if not words:
            return ["InvalidArguments"]
        verb = words[0]

        if verb == "login.hashed":
            return self._login(words[1:])
        if not self.logged_in and verb not in ("version",):
            # Every real Frostbite command needs a login first, and saying so precisely is the
            # difference between "wrong password" and "wrong port" for whoever is debugging.
            return ["LogInRequired"]
        if verb == "admin.eventsEnabled":
            self.events_enabled = len(words) > 1 and words[1] == "true"
            return ["OK"]
        if verb == "banList.add":
            return self._add_ban(words[1:])
        if verb == "banList.remove":
            return self._remove_ban(words[1:])
        if verb == "banList.list":
            return ["OK", *[w for ban in self.bans for w in (ban[0], ban[1], "perm", "0", ban[2])]]
        if verb in self.replies:
            return ["OK", *self.replies[verb]]
        return ["OK"]

    def _login(self, arguments: list[str]) -> list[str]:
        if not arguments:
            return ["OK", SALT_HEX]  # the challenge
        expected = (
            hashlib.md5(  # noqa: S324 - the protocol specifies md5
                bytes.fromhex(SALT_HEX) + self.password.encode("utf-8")
            )
            .hexdigest()
            .upper()
        )
        if self.accept_login and arguments[0].upper() == expected:
            self.logged_in = True
            return ["OK"]
        return ["InvalidPasswordHash"]

    def _add_ban(self, arguments: list[str]) -> list[str]:
        if len(arguments) < 2:
            return ["InvalidArguments"]
        reason = arguments[-1] if len(arguments) > 2 else ""
        self.bans.append((arguments[0], arguments[1], reason))
        return ["OK"]

    def _remove_ban(self, arguments: list[str]) -> list[str]:
        if len(arguments) < 2:
            return ["InvalidArguments"]
        before = len(self.bans)
        self.bans = [b for b in self.bans if not (b[0] == arguments[0] and b[1] == arguments[1])]
        return ["OK"] if len(self.bans) < before else ["NotFound"]

    def _resend_unacked(self) -> None:
        now = time.monotonic()
        for i, (sequence, packet, sent_at) in enumerate(list(self._pending)):
            if now - sent_at >= self.resend_unacked_after:
                self._send(packet)
                self._pending[i] = (sequence, packet, now)

    def _send(self, packet: bytes) -> None:
        if self._conn is None:
            return
        try:
            if self.chunk_size:
                # Dribble it out, because TCP is allowed to and a client must cope.
                for i in range(0, len(packet), self.chunk_size):
                    self._conn.sendall(packet[i : i + self.chunk_size])
                    time.sleep(0.001)
            else:
                self._conn.sendall(packet)
        except OSError:  # pragma: no cover - the client went away
            self._drop()


def main() -> None:  # pragma: no cover - a hand tool
    parser = argparse.ArgumentParser(description="a fake Frostbite rcon server")
    parser.add_argument("--password", default="test")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = FakeFrostbiteServer(password=args.password).start()
    print(
        f"fake Frostbite server on {server.address[0]}:{server.address[1]}, password {args.password!r}"
    )
    print("type an event as space-separated words to push it; ctrl-c to stop")
    print('  e.g. player.onChat Bravo17 "!ban Bob" all')
    try:
        import shlex

        while True:
            line = input("> ").strip()
            if line:
                server.push(shlex.split(line))
            print(f"  commands received so far: {server.received}")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    main()


__all__ = ["DEFAULT_PLAYER_BLOCK", "HEADER_SIZE", "SALT_HEX", "FakeFrostbiteServer"]
