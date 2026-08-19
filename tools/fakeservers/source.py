"""A fake Source server: RCON over TCP, A2S over UDP on the same port, and a console.log.

Three protocols at once, which is what a Source server really is:

* **RCON** — length-prefixed TCP packets, ``<size><id><type><body>\\x00\\x00``, with a login handshake
  and **the request id echoed back**. That echo is the whole reason the client can be simple, so this
  fake is careful with it: a reply always carries the id it was asked with, and the sentinel the client
  sends to find the end of a split reply gets its own reply too.
* **A2S** — the public query protocol over UDP, *on the same port number*, which is how the real thing
  works and worth modelling rather than moving to a convenient second port. It answers a first request
  with a **challenge** and only answers the question when the challenge comes back, which is the
  behaviour Valve added in December 2020 and the reason the classic bot's ``info()`` stopped working.
* **the log** — a file the bot tails, because a Source server pushes nothing down the RCON socket.

Two things here exist to prove the client rather than to be convenient:

* ``fragment`` **splits an RCON reply across packets**, with nothing in any packet saying so. That is
  the real protocol's nastiest property and the reason the client sends a sentinel; a fake that always
  replied in one packet would let a size-guessing client pass.
* ``require_challenge`` can be turned off, so the pre-2020 A2S behaviour is testable too.

    python -m tools.fakeservers.source --password test
"""

from __future__ import annotations

import argparse
import bz2
import random
import socket
import struct
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

#: The port a Source server listens on for both the game and RCON, unless told otherwise.
DEFAULT_PORT = 27015

SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_RESPONSE_VALUE = 0

#: The id a real server puts on the auth reply when the password was wrong. There is no message.
AUTH_FAILED_ID = -1

#: What ``sm version`` answers, and the text the bot insists on before it will run at all.
SM_VERSION = (
    " SourceMod Version Information:\n"
    "    SourceMod Version: 1.10.0.6502\n"
    "    SourcePawn Engine: 1.10.0.6502, jit-x86 (build 1.10.0.6502)\n"
    "    Compiled on: Mar  1 2021 12:00:00\n"
)

# A2S
A2S_INFO_REQUEST = ord("T")
A2S_PLAYER_REQUEST = ord("U")
A2S_RULES_REQUEST = ord("V")
A2S_INFO_REPLY = 0x49
A2S_PLAYER_REPLY = 0x44
A2S_RULES_REPLY = 0x45
#: First four bytes of a fragment, against -1 for a reply that fits in one datagram.
SPLIT = -2
S2C_CHALLENGE_REPLY = 0x41


def _now() -> str:
    """The HL log standard's timestamp: ``L 08/28/2012 - 01:28:40: ``."""
    return datetime.now(timezone.utc).strftime("L %m/%d/%Y - %H:%M:%S: ")


class FakeSourceServer:
    """One RCON connection at a time on a thread, plus a UDP query responder, in the shape the
    other fakes here use."""

    def __init__(
        self,
        password: str = "test",
        *,
        log_path: str | Path | None = None,
        fragment: int = 0,
        sourcemod: bool = True,
        require_challenge: bool = True,
        hostname: str = "Fake Insurgency Server",
        map_name: str = "buhriz",
        maps: tuple[str, ...] | None = None,
    ) -> None:
        self.password = password
        #: If set, an RCON reply is written as several packets of this many body bytes, all carrying
        #: the same request id and nothing marking them as parts. See the module docstring.
        self.fragment = fragment
        #: With this off, ``sm version`` answers as a stock server does — which is what the bot has to
        #: refuse to run against, since every reply and the whole of `!ban` go through SourceMod.
        self.sourcemod = sourcemod
        #: What ``sm plugins list`` reports. Add "B3 Say" to model a server whose admin installed the
        #: plugin the classic bot's better chat verbs come from — see `GameProfile.optional_mods`.
        self.sm_plugins: list[str] = ["Admin File Reader", "Basic Commands"]
        #: Split A2S replies into fragments this many bytes long. 0 = send them whole. A real server
        #: splits whenever the answer will not fit in a datagram, which for `A2S_RULES` is usual.
        self.a2s_fragment = 0
        #: bzip2 a split A2S reply, as a real server does when it is long enough to be worth it.
        #: Only meaningful with `a2s_fragment` set — compression is a property of a split reply.
        self.a2s_compress = False
        #: A2S: answer the first request with a challenge, as every current build does.
        self.require_challenge = require_challenge

        self.hostname = hostname
        self.map_name = map_name
        self.log_path = Path(log_path) if log_path else None

        #: Every command received, in order, and whether the login was accepted.
        self.received: list[str] = []
        self.authed = False
        self.auth_attempts = 0
        #: slot -> (name, steam id, team, ip, ping). A bot has no ip and no ping, which is what makes
        #: the status table two-shaped.
        self.players: dict[str, tuple[str, str, str, str, int]] = {}
        #: steam id -> (minutes, reason)
        self.bans: dict[str, tuple[str, str]] = {}
        self.cvars: dict[str, str] = {
            "sm_nextmap": "district",
            "mp_lobbytime": "10",
            "tv_password": "",
        }
        #: What `maps *` lists: every map *installed*, which is emphatically not a rotation. Per
        #: title, because a server pretending to be CS2 while answering with Insurgency's maps would
        #: let `!map` be exercised against a list no such server could ever return.
        self.maps: list[str] = list(maps or ("buhriz", "district", "sinjar", "siege", "market"))

        self._tcp: socket.socket | None = None
        self._udp: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._address: tuple[str, int] | None = None
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._challenges: set[int] = set()

    # -- lifecycle ---------------------------------------------------------

    @property
    def address(self) -> tuple[str, int]:
        assert self._address is not None, "start() first"
        return self._address

    def start(self, host: str = "127.0.0.1", port: int = 0) -> "FakeSourceServer":
        """Bind and serve. Port 0 takes an ephemeral one, which is what the tests want."""
        self._tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._tcp.bind((host, port))
        self._address = self._tcp.getsockname()[:2]
        self._tcp.listen(1)

        # The same port number, over UDP. A real Source server shares it between the game (and its
        # query protocol) and RCON, and putting the fake's query responder somewhere else would hide
        # any assumption the client makes about them being the same.
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind((host, self._address[1]))
        self._udp.settimeout(0.2)

        for target in (self._serve_rcon, self._serve_query):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    def stop(self) -> None:
        self._stop.set()
        for sock in (self._conn, self._tcp, self._udp):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=2)

    # -- the game writing its log ------------------------------------------

    def push(self, line: str) -> None:
        """Append one line to the console log, with the timestamp the engine writes."""
        if self.log_path is None:
            return
        with self._lock, self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(_now() + line + "\n")

    def add_player(
        self,
        slot: str,
        name: str,
        guid: str = "BOT",
        *,
        team: str = "#Team_Security",
        ip: str = "",
        ping: int = 0,
    ) -> None:
        """Put a player on the server and announce it the way the engine does — three lines.

        A bot's address is the literal ``none``, which is the detail that stops an AI player being
        given an IP and then authenticated by it.
        """
        with self._lock:
            self.players[slot] = (name, guid, team, ip, ping)
        self.push(f'"{name}<{slot}><{guid}><>" connected, address "{ip or "none"}"')
        self.push(f'"{name}<{slot}><{guid}><>" entered the game')
        self.push(f'"{name}<{slot}><{guid}><#Team_Unassigned>" joined team "{team}"')

    def remove_player(self, slot: str, reason: str = "Disconnected.") -> None:
        with self._lock:
            entry = self.players.pop(slot, None)
        if entry is None:
            return
        name, guid, team, _ip, _ping = entry
        self.push(f'"{name}<{slot}><{guid}><{team}>" disconnected (reason "{reason}")')

    def say_as(self, slot: str, text: str, *, scope: str = "") -> None:
        entry = self.players.get(slot)
        if entry is None:
            return
        name, guid, team, _ip, _ping = entry
        verb = "say_team" if scope == "team" else "say"
        self.push(f'"{name}<{slot}><{guid}><{team}>" {verb} "{text}"')

    def load_map(self, name: str) -> None:
        with self._lock:
            self.map_name = name
        self.push(f'Loading map "{name}"')
        self.push(f'Started map "{name}" (CRC "1592693790")')
        self.push("Gamerules: entering state 'GR_STATE_RND_RUNNING'")

    def kill(self, attacker: str, victim: str, weapon: str, *, headshot: bool = False) -> None:
        a, v = self.players.get(attacker), self.players.get(victim)
        if a is None or v is None:
            return
        tail = " (headshot)" if headshot else ""
        self.push(
            f'"{a[0]}<{attacker}><{a[1]}><{a[2]}>" killed '
            f'"{v[0]}<{victim}><{v[1]}><{v[2]}>" with "{weapon}"{tail}'
        )

    # -- RCON --------------------------------------------------------------

    def sent_command(self, prefix: str) -> bool:
        return any(c.strip().startswith(prefix) for c in self.received)

    def _serve_rcon(self) -> None:
        assert self._tcp is not None
        while not self._stop.is_set():
            try:
                conn, _addr = self._tcp.accept()
            except OSError:
                return
            self._conn = conn
            conn.settimeout(0.2)
            # Each connection is its own session: a reconnect must log in again, which is what makes
            # the client's reconnect-and-retry path real rather than assumed.
            self.authed = False
            buffer = b""
            try:
                while not self._stop.is_set():
                    try:
                        chunk = conn.recv(8192)
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while True:
                        packet = self._take_packet(buffer)
                        if packet is None:
                            break
                        (request_id, packet_type, body), buffer = packet
                        self._handle(conn, request_id, packet_type, body)
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    @staticmethod
    def _take_packet(buffer: bytes) -> tuple[tuple[int, int, str], bytes] | None:
        if len(buffer) < 4:
            return None
        (size,) = struct.unpack("<i", buffer[:4])
        if len(buffer) < 4 + size:
            return None
        payload = buffer[4 : 4 + size]
        request_id, packet_type = struct.unpack("<ii", payload[:8])
        body = payload[8:].split(b"\x00", 1)[0].decode("utf-8", "replace")
        return (request_id, packet_type, body), buffer[4 + size :]

    def _handle(self, conn: socket.socket, request_id: int, packet_type: int, body: str) -> None:
        if packet_type == SERVERDATA_AUTH:
            self.auth_attempts += 1
            if body == self.password:
                self.authed = True
                # Two packets, in this order, which is what every build does: an empty value packet
                # and then the verdict. A client that reads the first as the verdict never logs in.
                self._write(conn, request_id, SERVERDATA_RESPONSE_VALUE, "")
                self._write(conn, request_id, SERVERDATA_AUTH_RESPONSE, "")
            else:
                # The rejection is the **id**, not a message. Note the value packet still carries the
                # real id, so a client keying only on the id of the last packet still gets it right.
                self._write(conn, request_id, SERVERDATA_RESPONSE_VALUE, "")
                self._write(conn, AUTH_FAILED_ID, SERVERDATA_AUTH_RESPONSE, "")
            return

        if packet_type != SERVERDATA_EXECCOMMAND:
            return

        if not body:
            # The sentinel the client sends to find the end of a split reply. A real server answers
            # it with an empty value packet, and everything about split replies depends on that.
            self._write(conn, request_id, SERVERDATA_RESPONSE_VALUE, "")
            return

        if not self.authed:
            # A stock server simply says nothing useful to an unauthenticated command.
            self._write(conn, request_id, SERVERDATA_RESPONSE_VALUE, "Bad Password")
            return

        with self._lock:
            self.received.append(body)
        reply = self._run(body)
        self._write_reply(conn, request_id, reply)

    def _run(self, command: str) -> str:  # noqa: PLR0911 - a flat dispatch reads better here
        verb, _, rest = command.partition(" ")
        rest = rest.strip()

        if command.strip() == "sm version":
            return SM_VERSION if self.sourcemod else 'Unknown command "sm"\n'
        if command.strip() == "sm plugins list":
            if not self.sourcemod:
                return 'Unknown command "sm"\n'
            rows = "".join(
                f'{i:02d} "{name}" (1.0.0) by Courgette\n'
                for i, name in enumerate(self.sm_plugins, start=1)
            )
            return f"  Listing {len(self.sm_plugins)} plugins:\n{rows}"
        if verb == "status":
            return self._status()
        if command.strip() == "maps *":
            return "".join(f"PENDING:  (fs) {name}.bsp\n" for name in self.maps)
        if verb in ("sm_say", "sm_hsay", "sm_psay"):
            self.push(f'rcon from "127.0.0.1:40000": command "{command}"')
            return ""
        # Counter-Strike 2's native verbs, which is all that title has: SourceMod does not build for
        # Source 2, so a CS2 server answers `sm version` with nothing useful and the bot has to work
        # with these. Serve them alongside the SourceMod ones and let the caller decide which title
        # it is pretending to be by passing `sourcemod=False`.
        #
        # `say` records the command and writes no log line: whether a native `say` echoes itself into
        # the console log is not something anybody here has seen a real CS2 server do, and inventing
        # an echo would let the bot pass against behaviour that may not exist. `received` is the
        # honest record that it was sent.
        if verb == "say":
            return ""
        if verb == "kickid":
            return self._kick(rest)
        if verb == "sm_kick":
            return self._kick(rest)
        if verb == "sm_addban":
            return self._addban(rest)
        if verb == "sm_unban":
            steam = rest.strip('"')
            self.bans.pop(steam, None)
            return f"Removed {steam} from the ban list\n"
        if verb == "changelevel":
            self.load_map(rest.strip().strip('"'))
            return ""
        # A bare name reads a cvar; a name and a value sets one. That is the whole of this engine's
        # cvar interface -- there is no `set` verb, which is why the profile's set_template has none.
        if verb in self.cvars and not rest:
            return f'"{verb}" = "{self.cvars[verb]}" ( def. "{self.cvars[verb]}" )\n'
        if verb in self.cvars:
            self.cvars[verb] = rest.strip('"')
            return ""
        return f'Unknown command "{verb}"\n'

    def _kick(self, rest: str) -> str:
        slot = rest.split(" ", 1)[0].lstrip("#")
        entry = self.players.get(slot)
        if entry is None:
            return f'Player "{slot}" not found\n'
        reason = rest.split(" ", 1)[1].strip('"') if " " in rest else "Kicked by Console"
        self.remove_player(slot, reason)
        return f"Kicked {entry[0]}\n"

    def _addban(self, rest: str) -> str:
        parts = rest.split(" ", 2)
        minutes = parts[0] if parts else "0"
        steam = parts[1].strip('"') if len(parts) > 1 else ""
        reason = parts[2].strip('"') if len(parts) > 2 else ""
        self.bans[steam] = (minutes, reason)
        # The engine records it in the log too, which is where the bot sees a ban it did not issue.
        slot = next((s for s, p in self.players.items() if p[1] == steam), "0")
        entry = self.players.get(slot)
        name = entry[0] if entry else "unknown"
        duration = "permanent" if minutes == "0" else f"for {minutes}.00 minutes"
        self.push(f'Banid: "{name}<{slot}><{steam}><>" was banned "{duration}" by "Console"')
        return "Ban added\n"

    def _status(self) -> str:
        """The status table, in the shape the captured reply has — padded labels and two row shapes."""
        with self._lock:
            players = dict(self.players)
        humans = sum(1 for p in players.values() if p[1] != "BOT")
        bots = len(players) - humans
        lines = [
            f"hostname: {self.hostname}",
            "version : 1.17.5.1/11751 5038 secure",
            f"udp/ip  : 127.0.0.1:{self.address[1]}  (public ip: 127.0.0.1)",
            "os      :  Linux",
            "type    :  community dedicated",
            f"map     : {self.map_name}",
            f"players : {humans} humans, {bots} bots (20/20 max) (not hibernating)",
            "",
            "# userid name uniqueid connected ping loss state rate adr",
        ]
        for slot, (name, guid, _team, ip, ping) in players.items():
            if guid == "BOT":
                # No index, no duration, no ping, no rate and no address. This row is why one pattern
                # has to match both shapes.
                lines.append(f'#{slot} "{name}" BOT active')
            else:
                lines.append(f'#{slot} 2 "{name}" {guid} 33:48 {ping} 0 active 20000 {ip}:27005')
        lines.append("#end")
        return "\n".join(lines) + "\n"

    def _write_reply(self, conn: socket.socket, request_id: int, reply: str) -> None:
        """Write a reply, split across packets when asked to.

        Nothing in any packet says it is a part, and the parts all carry the same id — which is the
        real protocol and the reason the client cannot guess the end from a size.
        """
        if not self.fragment or len(reply) <= self.fragment:
            self._write(conn, request_id, SERVERDATA_RESPONSE_VALUE, reply)
            return
        for start in range(0, len(reply), self.fragment):
            self._write(
                conn, request_id, SERVERDATA_RESPONSE_VALUE, reply[start : start + self.fragment]
            )
            time.sleep(0.001)

    @staticmethod
    def _write(conn: socket.socket, request_id: int, packet_type: int, body: str) -> None:
        payload = struct.pack("<ii", request_id, packet_type) + body.encode("utf-8") + b"\x00\x00"
        try:
            conn.sendall(struct.pack("<i", len(payload)) + payload)
        except OSError:
            pass

    # -- A2S ---------------------------------------------------------------

    def _serve_query(self) -> None:
        assert self._udp is not None
        while not self._stop.is_set():
            try:
                datagram, addr = self._udp.recvfrom(1400)
            except TimeoutError:
                continue
            except OSError:
                return
            reply = self._answer_query(datagram)
            if reply is None:
                continue
            for packet in self._split_query_reply(reply):
                try:
                    self._udp.sendto(packet, addr)
                except OSError:
                    pass

    def _split_query_reply(self, reply: bytes) -> list[bytes]:
        """Break a query reply into fragments, and bzip2 it, when the test asked for that.

        A real server does this whenever the answer will not fit in one datagram, which for a busy
        server's `A2S_RULES` is always. Modelled here rather than assumed away because a fake that
        only ever sends whole replies cannot tell working reassembly from reassembly that has never
        run — and the compressed path in particular sat unexercised for exactly that reason.

        The layout is Valve's: ``-2``, the reply id with its top bit set when compressed, the total
        number of fragments, this fragment's number, its size — and then, **on fragment zero only**,
        the decompressed length and CRC32.
        """
        # A reply that already fits goes whole, which is what a real server does — and it matters
        # here: the challenge packet is nine bytes, and splitting *that* would exercise reassembly
        # on the handshake instead of on the long answer the setting is meant to be about.
        if not self.a2s_fragment or len(reply) <= self.a2s_fragment:
            return [reply]
        # What gets reassembled is the whole reply, `-1` header and all: the client checks for that
        # header once the pieces are back together, which is how it knows reassembly worked.
        body = reply
        reply_id = 0x1234
        extra = b""
        if self.a2s_compress:
            body = bz2.compress(reply)
            reply_id |= 0x8000_0000
            extra = struct.pack("<II", len(reply), zlib.crc32(reply) & 0xFFFF_FFFF)

        size = self.a2s_fragment
        chunks = [body[i : i + size] for i in range(0, len(body), size)] or [b""]
        packets: list[bytes] = []
        for number, chunk in enumerate(chunks):
            head = struct.pack("<iIBBH", SPLIT, reply_id, len(chunks), number, size)
            packets.append(head + (extra if number == 0 else b"") + chunk)
        return packets

    def _answer_query(self, datagram: bytes) -> bytes | None:
        if len(datagram) < 5 or datagram[:4] != b"\xff\xff\xff\xff":
            return None
        kind = datagram[4]
        body = datagram[5:]

        if kind == A2S_INFO_REQUEST:
            # The payload is "Source Engine Query\0" and then, on a current build, the challenge.
            _, _, tail = body.partition(b"\x00")
            challenge = struct.unpack("<i", tail[:4])[0] if len(tail) >= 4 else -1
        else:
            challenge = struct.unpack("<i", body[:4])[0] if len(body) >= 4 else -1

        if self.require_challenge and challenge not in self._challenges:
            issued = random.randint(1, 0x7FFF_FFFF)
            self._challenges.add(issued)
            return b"\xff\xff\xff\xff" + bytes([S2C_CHALLENGE_REPLY]) + struct.pack("<i", issued)

        if kind == A2S_INFO_REQUEST:
            return b"\xff\xff\xff\xff" + self._info_reply()
        if kind == A2S_PLAYER_REQUEST:
            return b"\xff\xff\xff\xff" + self._player_reply()
        if kind == A2S_RULES_REQUEST:
            return b"\xff\xff\xff\xff" + self._rules_reply()
        return None

    def _info_reply(self) -> bytes:
        def s(text: str) -> bytes:
            return text.encode("utf-8") + b"\x00"

        with self._lock:
            players = dict(self.players)
        bots = sum(1 for p in players.values() if p[1] == "BOT")
        return (
            bytes([A2S_INFO_REPLY])
            + bytes([17])  # protocol
            + s(self.hostname)
            + s(self.map_name)
            + s("insurgency")
            + s("Insurgency")
            # Insurgency is app 222880, and this field is sixteen bits — so what a real server sends
            # is the app id truncated, 26272. Modelled rather than tidied away: it is exactly the
            # value that shows up a client reading this field as *signed*.
            + struct.pack("<H", 222880 & 0xFFFF)
            + bytes([len(players), 20, bots])
            + b"d"  # dedicated
            + b"l"  # linux
            + bytes([0, 1])  # not passworded, vac on
            + s("1.17.5.1")
            + bytes([0x80 | 0x20])  # extra data: port and keywords
            # Unsigned, and an ephemeral test port is routinely above 32767 — which is the other half
            # of the signed-short fault, and the half an operator would actually hit.
            + struct.pack("<H", self.address[1])
            + s("coop,bots")
        )

    def _player_reply(self) -> bytes:
        with self._lock:
            players = list(self.players.items())
        out = bytes([A2S_PLAYER_REPLY, len(players)])
        for index, (_slot, (name, _guid, _team, _ip, _ping)) in enumerate(players):
            out += (
                bytes([index])
                + name.encode("utf-8")
                + b"\x00"
                + struct.pack("<i", 7)
                + struct.pack("<f", 123.5)
            )
        return out

    def _rules_reply(self) -> bytes:
        with self._lock:
            rules = dict(self.cvars)
        out = bytes([A2S_RULES_REPLY]) + struct.pack("<H", len(rules))
        for key, value in rules.items():
            out += key.encode("utf-8") + b"\x00" + value.encode("utf-8") + b"\x00"
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", default="test")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--log", default="console.log", help="the console.log to write")
    parser.add_argument(
        "--fragment",
        type=int,
        default=0,
        help="split rcon replies into packets of this many bytes",
    )
    args = parser.parse_args()

    server = FakeSourceServer(args.password, log_path=args.log, fragment=args.fragment)
    server.start("0.0.0.0", args.port)
    print(f"fake Source server on {args.port} (tcp rcon + udp query), log -> {args.log}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
