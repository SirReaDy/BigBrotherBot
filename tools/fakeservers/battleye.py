"""A fake BattlEye RCON server.

Speaks the real protocol — checksums, sequence numbers, acknowledgements, split replies — because
those are the parts that are easy to get subtly wrong, and a fake that skips them proves nothing.

It deliberately does the two awkward things a real server does:

* it **requires acknowledgement** of the messages it pushes, and **resends** one that is not
  acknowledged, which is what makes a bot act on the same chat line twice if it does not deduplicate;
* it can **split a reply across packets**, which is how a long ban list arrives.

Run it standalone to watch what a bot sends::

    python -m tools.fakeservers.battleye --password test
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import threading
import time
from collections import deque

from b3.net.battleye import COMMAND, LOGIN, MESSAGE, decode, encode

log = logging.getLogger(__name__)

#: How the server tells a client which id is in which slot — and how this fake learns it too.
VERIFIED_GUID_RE = re.compile(
    r"^Verified GUID \((?P<guid>[0-9a-f]+)\) of player #(?P<slot>\d+)", re.IGNORECASE
)

#: What `players` answers with unless a test replaces it. Two players, one still in the lobby with no
#: id yet — the shape that made a single row pattern with an optional GUID necessary.
DEFAULT_PLAYERS = (
    "Players on server:\n"
    "[#] [IP Address]:[Port] [Ping] [GUID] [Name]\n"
    "--------------------------------------------------\n"
    "0   76.108.91.78:2304    31   80a5885ebe2420bab5e1581234567890(OK) Bravo17\n"
    "1   198.51.100.7:2304    -1   -                                    Joining (Lobby)\n"
    "(2 players in total)"
)


class FakeBattleyeServer:
    """One thread, one UDP socket, and a scripted set of replies."""

    def __init__(
        self,
        password: str = "test",
        *,
        replies: dict[str, str] | None = None,
        resend_unacked_after: float = 1.0,
        max_packet: int = 512,
        guid_bans: list[tuple[str, str, str]] | None = None,
        ip_bans: list[tuple[str, str, str]] | None = None,
    ) -> None:
        self.password = password
        #: (target, minutes-or-"perm", reason) rows, kept as real state so `bans` and `removeBan`
        #: agree with each other — including the renumbering that makes removing by index awkward.
        self.guid_bans = list(guid_bans or [])
        self.ip_bans = list(ip_bans or [])
        #: command -> reply text. A command with no entry gets an empty reply, as the real server
        #: does for `say`.
        self.replies = {"players": DEFAULT_PLAYERS, **(replies or {})}
        #: Every command the bot has sent, in order — what a test asserts on.
        self.received: list[str] = []
        #: Sequence numbers the bot has acknowledged.
        self.acked: list[int] = []
        #: True once a login has been accepted.
        self.logged_in = False
        #: Set False to make the server reject the password.
        self.accept_login = True
        #: How long to wait before resending a message nobody acknowledged. Large in tests that do
        #: not care; small in the one that checks deduplication.
        self.resend_unacked_after = resend_unacked_after
        self.max_packet = max_packet
        #: slot -> GUID, learned from the identity messages this server pushes (see `push`), so a
        #: `ban <slot>` lands on the right id. A test can also seed it directly.
        self.slot_guids: dict[str, str] = {}

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.05)
        self.address: tuple[str, int] = self._sock.getsockname()
        self._client: tuple[str, int] | None = None
        self._pending: deque[tuple[int, bytes, float]] = deque()  # unacknowledged pushes
        self._message_seq = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-battleye", daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> FakeBattleyeServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()

    def __enter__(self) -> FakeBattleyeServer:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- driving it from a test ---------------------------------------------

    def push(self, text: str) -> int:
        """Send a server message, as the game does for chat and connects. Returns its sequence.

        Messages that announce an identity are also *remembered*, because a real server knows which
        GUID is in which slot — that is how `ban <slot>` ends up on the right id in its ban list. A
        fake that forgot would let a wrong-slot bug pass unnoticed.
        """
        identified = VERIFIED_GUID_RE.match(text)
        if identified:
            self.slot_guids[identified["slot"]] = identified["guid"]
        sequence = self._message_seq
        self._message_seq = (self._message_seq + 1) & 0xFF
        packet = encode(MESSAGE, sequence, text)
        self._pending.append((sequence, packet, time.monotonic()))
        self._send(packet)
        return sequence

    def wait_for_command(self, needle: str, timeout: float = 2.0) -> bool:
        """Block until the bot has sent a command containing ``needle``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(needle in cmd for cmd in self.received):
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
            self._resend_unacked()
            try:
                raw, addr = self._sock.recvfrom(8192)
            except (TimeoutError, OSError):
                continue
            self._client = addr
            packet = decode(raw)
            if packet is None:
                log.warning("fake battleye: undecodable packet from %s", addr)
                continue
            if packet.type == LOGIN:
                self._handle_login(packet.payload)
            elif packet.type == COMMAND:
                self._handle_command(packet.sequence, packet.data)
            elif packet.type == MESSAGE:
                self.acked.append(packet.sequence)
                self._pending = deque(p for p in self._pending if p[0] != packet.sequence)

    def _handle_login(self, payload: bytes) -> None:
        # `payload`, not `data`: a login has no sequence byte, so the byte a command would spend on
        # one is the first character of the password.
        ok = self.accept_login and payload.decode("utf-8", "replace") == self.password
        self.logged_in = ok
        # The verdict rides in the sequence byte: 1 accepted, 0 rejected.
        self._send(encode(LOGIN, 1 if ok else 0))

    def _handle_command(self, sequence: int, data: bytes) -> None:
        cmd = data.decode("utf-8", "replace")
        if not cmd:
            return  # keepalive: a real server does not answer these
        self.received.append(cmd)
        verb, _, rest = cmd.partition(" ")
        if verb == "bans" and "bans" not in self.replies:
            reply = self._bans_reply()
        elif verb == "removeBan":
            reply = self._remove_ban(rest.strip())
        elif verb == "ban" and "ban" not in self.replies:
            reply = self._add_ban(rest.strip())
        else:
            reply = self.replies.get(verb, self.replies.get(cmd, ""))
        body = reply.encode("utf-8")
        if len(body) <= self.max_packet:
            self._send(encode(COMMAND, sequence, body))
            return
        # Split it, the way a long ban list arrives: 0x00, total, index, then the slice.
        chunks = [body[i : i + self.max_packet] for i in range(0, len(body), self.max_packet)]
        for index, chunk in enumerate(chunks):
            self._send(encode(COMMAND, sequence, bytes([0, len(chunks), index]) + chunk))

    # -- the ban list, modelled rather than canned ---------------------------
    #
    # `removeBan` takes a row *index*, and removing a row renumbers everything after it. Faking that
    # with a fixed string would hide exactly the mistake worth catching.

    def _bans_reply(self) -> str:
        lines = ["GUID Bans:", "[#] [GUID] [Minutes left] [Reason]", "-" * 50]
        lines += [f"{i}  {t} {m} {r}" for i, (t, m, r) in enumerate(self.guid_bans)]
        lines += ["", "IP Bans:", "[#] [IP Address] [Minutes left] [Reason]", "-" * 50]
        lines += [f"{i}  {t} {m} {r}" for i, (t, m, r) in enumerate(self.ip_bans)]
        return "\n".join(lines)

    def _remove_ban(self, argument: str) -> str:
        try:
            index = int(argument)
        except ValueError:
            return "Invalid ban id"
        if 0 <= index < len(self.guid_bans):
            removed = self.guid_bans.pop(index)
            return f"Ban removed: {removed[0]}"
        return "Ban not found"

    def _add_ban(self, argument: str) -> str:
        """`ban <slot> <minutes> <reason>` — the server looks the slot's GUID up itself."""
        parts = argument.split(" ", 2)
        if len(parts) < 2:
            return "Invalid arguments"
        slot, minutes = parts[0], parts[1]
        reason = parts[2] if len(parts) > 2 else ""
        guid = self.slot_guids.get(slot, f"{slot:0>32}")
        self.guid_bans.append((guid, "perm" if minutes == "0" else minutes, reason))
        return f"Player #{slot} banned"

    def _resend_unacked(self) -> None:
        """A real BattlEye server keeps sending a message until it is acknowledged."""
        now = time.monotonic()
        for i, (sequence, packet, sent_at) in enumerate(list(self._pending)):
            if now - sent_at >= self.resend_unacked_after:
                self._send(packet)
                self._pending[i] = (sequence, packet, now)

    def _send(self, packet: bytes) -> None:
        if self._client is None:
            return
        try:
            self._sock.sendto(packet, self._client)
        except OSError:  # pragma: no cover - the socket closed under us
            pass


def main() -> None:  # pragma: no cover - a hand tool
    parser = argparse.ArgumentParser(description="a fake BattlEye rcon server")
    parser.add_argument("--password", default="test")
    parser.add_argument("--port", type=int, default=0, help="0 picks a free port")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    server = FakeBattleyeServer(password=args.password)
    if args.port:
        server._sock.close()
        server._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server._sock.bind(("127.0.0.1", args.port))
        server._sock.settimeout(0.05)
        server.address = server._sock.getsockname()
    server.start()
    print(f"fake BattlEye server on {server.address[0]}:{server.address[1]}, password {args.password!r}")
    print("type a server message to push it to the bot; ctrl-c to stop")
    try:
        while True:
            line = input("> ").strip()
            if line:
                server.push(line)
            print(f"  commands received so far: {server.received}")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    main()
