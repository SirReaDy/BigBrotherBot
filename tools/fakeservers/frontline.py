"""A fake Frontlines: Fuel of War server: an MD5 challenge, 0x04 frames, and a silent rejection.

This protocol is documented — the classic parser quotes the game's own RCON documentation — and three
of its properties shape everything:

* **Packets are separated by ``\\x04``**, not by newlines. The documentation says "'\\n' or 0x04", but
  that must not be taken literally when *reading*: the ``PlayerList:`` reply is a single packet with
  newlines inside it, so a client splitting on ``\\n`` would shred the one message that carries the
  entire roster. This fake therefore frames with ``\\x04`` and puts real newlines inside a packet, which
  is the only way for a client to get that wrong and be caught.
* **The login is a challenge/response.** The server greets with
  ``WELCOME! Frontlines: Fuel of War (RCON) VER=2 CHALLENGE=38D384D07C``, and the client answers
  ``RESPONSE <user> <md5(challenge + password)>``. So it needs a **username as well as a password**, and
  the password never crosses the wire.
* **A failed login is not answered — the connection is closed.** There is no "wrong password" message
  to match on, so a client that waits for one waits for ever. This fake hangs up exactly as the real
  server does, which is what makes that failure visible.

And two more that a fake is the only way to exercise:

* **Ten seconds of silence costs the connection** (:attr:`idle_timeout`), so the client must ping.
* **Nothing is reported unless the bot turns it on.** A freshly logged-in connection is silent: no chat
  and no debug lines arrive until ``CHATLOGGING TRUE`` and ``DebugLogging TRUE`` are sent. This fake
  honours that (:attr:`chat_logging`), because a bot that forgets it sees a server where nobody talks.

    python -m tools.fakeservers.frontline --password test

"""

from __future__ import annotations

import hashlib
import socket
import threading
import time

#: The remote console port, unless the operator says otherwise. The classic parser's own example
#: programs use it twice, and it is the only figure quoted anywhere.
DEFAULT_PORT = 14507

#: Packet separator. See the module docstring: this and not the newline.
TERMINATOR = "\x04"

#: The greeting, which carries the challenge the password is hashed against.
WELCOME = "WELCOME! Frontlines: Fuel of War (RCON) VER=%s CHALLENGE=%s"
#: What a successful login is answered with. The game's documentation writes it "Login Success!" and
#: the classic parser matched "Login SUCCESS!" — the parser wins, because its *other* handler names the
#: field that follows (`User:`), which nobody would invent. A client should match case-insensitively.
LOGIN_SUCCESS = "Login SUCCESS! User:%s"

#: The columns of a `PLAYERLIST` row, tab separated, in this order.
PLAYER_COLUMNS = (
    "ID",
    "Name",
    "Ping",
    "Team",
    "Squad",
    "Score",
    "Kills",
    "Deaths",
    "TK",
    "CP",
    "Time",
    "Idle",
    "Loadout",
    "Role",
    "RoleLvl",
    "Vehicle",
    "Hash",
    "ProfileID",
)


def expected_response(challenge: str, password: str) -> str:
    """``md5(challenge + password)``, lowercase hex — the whole of the authentication."""
    return hashlib.md5(f"{challenge}{password}".encode()).hexdigest()


class FakePlayer:
    """One row of the roster. A slot number, a profile id, and the numbers the game reports."""

    def __init__(
        self,
        cid: str,
        name: str,
        profile_id: str,
        team: str = "0",
        ping: int = 40,
        score: int = 0,
        kills: int = 0,
        deaths: int = 0,
        pb_hash: str = "",
    ) -> None:
        self.cid = cid
        self.name = name
        self.profile_id = profile_id
        self.team = team
        self.ping = ping
        self.score = score
        self.kills = kills
        self.deaths = deaths
        self.pb_hash = pb_hash

    def row(self) -> str:
        values = {
            "ID": self.cid,
            "Name": self.name,
            "Ping": str(self.ping),
            "Team": self.team,
            "Squad": "0",
            "Score": str(self.score),
            "Kills": str(self.kills),
            "Deaths": str(self.deaths),
            "TK": "0",
            "CP": "0",
            "Time": "120",
            "Idle": "0",
            "Loadout": "0",
            "Role": "Assault",
            "RoleLvl": "1",
            "Vehicle": "",
            "Hash": self.pb_hash,
            "ProfileID": self.profile_id,
        }
        return "\t".join(values[column] for column in PLAYER_COLUMNS)


class FakeFrontlineServer:
    """One connection at a time, on a thread, in the shape the other fakes here use."""

    def __init__(
        self,
        password: str = "test",
        user: str = "admin",
        *,
        challenge: str = "38D384D07C",
        version: str = "2",
        idle_timeout: float = 0.0,
        fragment: int = 0,
        chat_logging: bool = False,
    ) -> None:
        self.password = password
        self.user = user
        #: Fixed rather than random, so a test can compute the expected hash. The real server varies it
        #: and does not fix its length, which is why the client must read it rather than assume one.
        self.challenge = challenge
        self.version = version
        #: Seconds of silence before hanging up. 0 disables it. The real server uses 10.
        self.idle_timeout = idle_timeout
        #: If set, writes go out in slices of this size, so reassembly is proved rather than assumed.
        self.fragment = fragment
        #: Whether chat is reported. False is the real server's starting state: **nothing is logged
        #: until the bot asks for it**, and this is the flag `CHATLOGGING TRUE` sets.
        self.chat_logging = chat_logging
        self.debug_logging = False
        self.pb_logging = False

        #: Every command received, in order.
        self.received: list[str] = []
        self.authed = False
        #: True when this connection was closed because the password was wrong, which the real server
        #: does *silently* — the distinction a client has to be able to draw.
        self.rejected = False
        #: True when a client was hung up on for going quiet.
        self.dropped_for_silence = False
        self.pings = 0

        #: slot -> player.
        self.players: dict[str, FakePlayer] = {}
        #: profile id -> (name, minutes) — 0 minutes meaning permanent.
        self.bans: dict[str, tuple[str, int]] = {}

        self.map_name = "CQ-Gnaw"
        self.maps: list[str] = ["CQ-Gnaw", "FL-Oilfield", "CQ-Bridge", "FL-Harbor"]
        self.round = 1
        self.total_rounds = 3
        self.max_players = 32
        self.remaining_time = 739
        self.tickets = (500, 500)

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

    def start(self) -> "FakeFrontlineServer":
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if self._address is None:
            self._sock.bind(("127.0.0.1", 0))
            self._address = self._sock.getsockname()[:2]
        else:
            # Remembered from the first bind, so `restart()` comes back on the same address — which is
            # what a client's reconnect has to survive.
            self._sock.bind(self._address)
        self._sock.listen(1)
        self._stop.clear()
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
        self._conn = None
        if self._thread is not None:
            self._thread.join(timeout=2)

    def restart(self) -> "FakeFrontlineServer":
        """Come back on the same address, the way a restarted game server does."""
        self.stop()
        self.authed = False
        self.chat_logging = False
        self.debug_logging = False
        return self.start()

    # -- the game telling the bot things -----------------------------------

    def push(self, data: str) -> None:
        """Send a line, framed the way this server frames everything."""
        self._send(data)

    def add_player(
        self,
        cid: str,
        name: str,
        profile_id: str,
        team: str = "0",
        ip: str = "192.168.0.1",
        pb_hash: str = "",
    ) -> None:
        """Put a player on the server.

        Note what is *not* sent: there is no "player connected" line on this engine. A join is only
        ever learned from the next `PLAYERLIST`, which is why that reply is the event stream and not a
        status poll. The one hint the server does give is the RendezVous debug line, and only when
        debug logging is on.
        """
        with self._lock:
            self.players[cid] = FakePlayer(cid, name, profile_id, team=team, pb_hash=pb_hash)
        if self.debug_logging:
            self.push(f"DEBUG: RendezVous: Update Gathering {profile_id}: {len(self.players)}/0")
        if self.pb_logging and pb_hash:
            self.push(
                f"PunkBuster Server: Player GUID Computed {pb_hash}(-) "
                f"(slot #{cid}) {ip}:14567 {name}"
            )

    def remove_player(self, cid: str) -> None:
        """Take a player off the roster — again with no line to announce it."""
        with self._lock:
            self.players.pop(cid, None)

    def say_as(self, name: str, text: str, channel: str = "Say") -> None:
        """A player talking — reported only if chat logging was turned on."""
        if not self.chat_logging:
            return
        self.push(f'CHAT: PlayerName="{name}" Channel="{channel}" Message="{text}"')

    # -- the wire ----------------------------------------------------------

    def _send(self, payload: str) -> None:
        conn = self._conn
        if conn is None:
            return
        frame = (payload + TERMINATOR).encode("utf-8")
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
        self._send(WELCOME % (self.version, self.challenge))
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
                if self.idle_timeout and time.time() - last_heard > self.idle_timeout:
                    self.dropped_for_silence = True
                    break
                continue
            except OSError:
                break
            # Commands arrive terminated the same way, and a client that sends a bare newline instead
            # is tolerated: the documentation allows either, and only *reading* has to be strict.
            while True:
                cuts = [i for i in (buffer.find(b"\x04"), buffer.find(b"\n")) if i >= 0]
                if not cuts:
                    break
                at = min(cuts)
                command, buffer = buffer[:at], buffer[at + 1 :]
                self._handle(command.decode("utf-8", "replace").strip())
        try:
            conn.close()
        except OSError:
            pass
        self._conn = None

    def _handle(self, command: str) -> None:
        if not command:
            return
        self.received.append(command)

        if command.startswith("RESPONSE "):
            self._login(command)
            return
        if not self.authed:
            # Unauthenticated commands are ignored in silence, as everything is here.
            return
        self._execute(command)

    def _login(self, command: str) -> None:
        parts = command.split(" ")
        user = parts[1] if len(parts) > 1 else ""
        digest = parts[2] if len(parts) > 2 else ""
        if user == self.user and digest.lower() == expected_response(self.challenge, self.password):
            self.authed = True
            self._send(LOGIN_SUCCESS % user)
            return
        # No message, no explanation: the connection simply goes. This is the behaviour a client has
        # to be able to tell apart from a network failure, and there is nothing on the wire to help it.
        self.rejected = True
        self._stop.set()

    def _execute(self, command: str) -> None:
        upper = command.upper()

        if upper == "ECHONET PING":
            self.pings += 1
            return

        if upper.startswith("CHATLOGGING"):
            self.chat_logging = command.rsplit(" ", 1)[-1].upper() == "TRUE"
            self._send(f"ChatLogging now {str(self.chat_logging).upper()}")
            return
        if upper.startswith("DEBUGLOGGING"):
            self.debug_logging = command.rsplit(" ", 1)[-1].upper() == "TRUE"
            self._send(f"DebugLogging now {str(self.debug_logging).upper()}")
            return
        if upper.startswith("PUNKBUSTERLOGGING"):
            self.pb_logging = command.rsplit(" ", 1)[-1].upper() == "TRUE"
            self._send(f"PunkBusterLogging now {str(self.pb_logging).upper()}")
            return

        if upper == "PLAYERLIST":
            self._send(self.player_list())
            return
        if upper == "MAPLIST":
            self._send(self.map_list())
            return
        if upper == "GETCURRENTMAP":
            self._send(f"CurrentMap is: {self.map_name}")
            return
        if upper == "GETNEXTMAP":
            self._send(f"NextMap is: {self.next_map()}")
            return
        if upper == "NEXTMAP":
            self.change_map(self.next_map())
            return
        if upper.startswith("FORCEMAPCHANGE "):
            self.change_map(command.split(" ", 1)[1].strip())
            return

        if upper.startswith("KICK "):
            self._kick(command)
            return
        if upper.startswith("BAN "):
            self._ban(command)
            return
        if upper.startswith("UNBAN "):
            self._unban(command)
            return

        if upper.startswith("SAY ") or upper.startswith("PLAYERSAY "):
            # Not echoed back. Unlike Homefront and Altitude, this server does not repeat the bot's own
            # output to it — one less loop to guard against.
            return

    # -- the commands that change something --------------------------------

    def _field(self, command: str, name: str) -> str:
        """Read a `Name=value` field out of a command, quoted or not."""
        for part in _split_fields(command):
            key, _, value = part.partition("=")
            if key.strip().lower() == name.lower():
                return value.strip().strip('"')
        return ""

    def _by_profile(self, profile_id: str) -> FakePlayer | None:
        with self._lock:
            for player in self.players.values():
                if player.profile_id == profile_id:
                    return player
        return None

    def _kick(self, command: str) -> None:
        profile_id = self._field(command, "ProfileID")
        player = self._by_profile(profile_id)
        if player is not None:
            self.remove_player(player.cid)

    def _ban(self, command: str) -> None:
        profile_id = self._field(command, "ProfileID")
        minutes = self._field(command, "BanTime") or "0"
        player = self._by_profile(profile_id)
        name = player.name if player is not None else ""
        cid = player.cid if player is not None else "-1"
        with self._lock:
            self.bans[profile_id] = (name, int(minutes))
        if player is not None:
            self.remove_player(player.cid)
        # Two lines, in this order — the kick that carries out the ban, then the ban itself. The
        # duration is reported as -1 for permanent and the word "Permanently" is appended.
        duration = "-1" if minutes == "0" else minutes
        suffix = " Permanently" if minutes == "0" else ""
        self.push(
            f'Kicked Player as part of ban: PlayerName="{name}" PlayerID={cid} '
            f"ProfileID={profile_id} Hash= BanDuration={duration}"
        )
        self.push(
            f'Banned Player: PlayerName="{name}" PlayerID={cid} '
            f"ProfileID={profile_id} Hash= BanDuration={duration}{suffix}"
        )

    def _unban(self, command: str) -> None:
        profile_id = self._field(command, "ProfileID")
        with self._lock:
            entry = self.bans.pop(profile_id, None)
        if entry is None:
            self.push(f"UnBan failed! Player ProfileID or Hash is not banned: {profile_id}")
            return
        self.push(f'UnBanned Player: PlayerName="" PlayerID=-1 ProfileID={profile_id} Hash=')

    def change_map(self, name: str) -> None:
        with self._lock:
            if name in self.maps:
                while self.maps[0] != name:
                    self.maps.append(self.maps.pop(0))
            else:
                self.maps.insert(0, name)
            self.map_name = name
            self.players.clear()  # everybody is dropped through the loading screen
        self.push("Forced transition to next map")
        if self.debug_logging:
            self.push(f"DEBUG: SeamlessTravel: Loading {name}")

    def next_map(self) -> str:
        with self._lock:
            return self.maps[1] if len(self.maps) > 1 else self.maps[0]

    # -- the multi-line replies --------------------------------------------

    def player_list(self) -> str:
        """The roster: a header line stating the counts, a column-header line, then one row each.

        One packet with newlines in it, which is the whole reason this protocol's framing matters.
        """
        with self._lock:
            rows = [player.row() for player in self.players.values()]
            count = len(self.players)
            header = (
                f"PlayerList: Map={self.map_name} Time={self.remaining_time} "
                f"Players={count}/{self.max_players} "
                f"Tickets={self.tickets[0]},{self.tickets[1]} "
                f"Round={self.round}/{self.total_rounds}"
            )
        return "\n".join([header, "\t".join(PLAYER_COLUMNS), *rows])

    def map_list(self) -> str:
        with self._lock:
            rows = [f"{index}\t{name}\tConquest" for index, name in enumerate(self.maps)]
            header = f"MapList: Count={len(self.maps)}"
        return "\n".join([header, "ID\tMapName\tGameType", *rows])

    # -- helpers for tests -------------------------------------------------

    def sent_command(self, prefix: str) -> bool:
        return any(c.upper().startswith(prefix.upper()) for c in self.received)

    def wait_for(self, predicate, timeout: float = 3.0) -> bool:  # noqa: ANN001
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False


def _split_fields(command: str) -> list[str]:
    """Split a command into `Name=value` parts, keeping quoted values whole.

    `BAN ProfileID=1 BanTime=0 Reason="two words"` has to survive, and a reason can contain spaces
    and `=` alike.
    """
    parts: list[str] = []
    current = ""
    in_quotes = False
    for char in command:
        if char == '"':
            in_quotes = not in_quotes
            current += char
        elif char == " " and not in_quotes:
            if current:
                parts.append(current)
            current = ""
        else:
            current += char
    if current:
        parts.append(current)
    return parts


def _demo() -> None:  # pragma: no cover - manual aid
    import argparse

    ap = argparse.ArgumentParser(description="a fake Frontlines: Fuel of War server")
    ap.add_argument("--password", default="test")
    ap.add_argument("--user", default="admin")
    args = ap.parse_args()

    server = FakeFrontlineServer(password=args.password, user=args.user).start()
    server.add_player("1", "Courgette", "1561500", pb_hash="a" * 32)
    print(f"listening on {server.address[0]}:{server.address[1]}")
    print("expected RESPONSE digest:", expected_response(server.challenge, args.password))
    try:
        while True:
            time.sleep(1)
            print(
                f"  authed={server.authed} chat={server.chat_logging} "
                f"received={len(server.received)} bans={server.bans}"
            )
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    _demo()
