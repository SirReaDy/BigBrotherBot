"""A fake Altitude server: a JSON log it writes, and a command file it reads back.

Altitude has no admin socket at all. The server *writes* one JSON object per line to a log file,
and it *reads* commands from a second file that anything with write access can append to. So this
fake is a pair of files and an execution loop, not a socket — which is exactly why it is worth
having, because the awkward parts of this "protocol" are all in the file handling:

* **A command line is framed** ``<port>,console,<command>``. The port matters: one Altitude
  installation runs several servers off **one shared command file**, so a line addressed to another
  port is not ours. This fake ignores those and records them separately, so a bot that forgot the
  prefix, or wrote a bare command, fails here rather than in production.
* **The log is shared too.** Every line carries the port that produced it, and this fake can be told
  to emit traffic for a neighbouring server, which the bot must not read as its own.
* **The server never truncates the command file.** It remembers how far it has read. That is *why*
  the bot has to empty the file when it shuts down: :meth:`restart` rewinds to byte zero and
  re-executes everything it finds, which is what a real server does after a crash or a map-change
  restart — and it is how a stale ``kick`` or ``addBan`` gets run a second time against whoever
  holds that name now.
* **The server echoes its own messages into the log** as ``chat`` events with ``"server": true``.
  A bot that ignored that flag would read its own output back as a player talking, and answer
  itself. Proving that does not happen needs a server that really does echo.

State is modelled rather than canned, for the reason the BattlEye fake models its ban list: it keeps
players (learned from the ``clientAdd`` lines it emits, because a real server knows who is on it) and
a real ban list keyed by vapor id, so a ban aimed at the wrong player cannot pass unnoticed.

    python -m tools.fakeservers.altitude            # write a session to a temp dir and print it

**One thing here is inferred, not measured**, and it is marked in the code: the ``clientRemove``
reason a console ``kick`` produces. The legacy parser only ever recorded the two reasons a player
*leaving* and a *vote kick* produce, and no captured log exists for an admin kick. The bot must
therefore treat an unrecognised reason as "they are gone" rather than matching on the string, which
is the behaviour this fake exercises.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: How a command must be framed in the command file. Anything else is not a command.
COMMAND_LINE_RE = re.compile(r"^(?P<port>\d+),(?P<channel>[a-z]+),(?P<command>.*)$")

#: The vapor id a server uses for itself, and for a bot player. Never a real account.
NOBODY = "00000000-0000-0000-0000-000000000000"

#: What a console `kick` makes the server write. INFERRED — see the module docstring.
KICK_REASON = "Kicked."


class FakeAltitudeServer:
    """A pair of files that behave like an Altitude dedicated server.

    ``push`` writes a log line; ``poll`` reads and executes whatever the bot has queued. Nothing
    runs on a thread: the caller decides when the server gets a turn, which keeps a failing test
    from hanging and makes the ordering in a session explicit.
    """

    def __init__(
        self,
        directory: str | Path,
        port: int = 27276,
        hostname: str = "b3 test server",
        max_players: int = 14,
    ) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.hostname = hostname
        self.max_players = max_players
        self.log_path = self.directory / "log.txt"
        self.command_path = self.directory / "command.txt"
        #: Commands addressed to us, in order, unframed.
        self.received: list[str] = []
        #: Lines that were not framed as a command at all — a bot bug if this is ever non-empty.
        self.malformed: list[str] = []
        #: Commands addressed to another server sharing this command file.
        self.for_other_ports: list[tuple[int, str]] = []
        #: vapor id -> (duration, unit, reason), the server's own ban list.
        self.bans: dict[str, tuple[str, str, str]] = {}
        #: slot -> (nickname, vapor id) for everyone the server believes is connected.
        self.players: dict[int, tuple[str, str]] = {}
        #: How far into the command file we have read. The server never truncates it.
        self._offset = 0
        self._clock_ms = 100_000

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> "FakeAltitudeServer":
        """Create both files and emit the two lines a starting server writes."""
        self.log_path.write_text("", encoding="utf-8")
        # Created empty, not truncated on every start: the bot appends to whatever is here, and a
        # real installation's file survives between runs. That is the point of `restart`.
        if not self.command_path.exists():
            self.command_path.write_text("", encoding="utf-8")
        self.push({"type": "serverInit", "name": self.hostname, "maxPlayerCount": self.max_players})
        self.push({"type": "serverStart"})
        return self

    def stop(self) -> None:
        """Nothing to close — both handles are opened per operation, as the game's own are."""

    def restart(self) -> list[str]:
        """Re-read the command file from the beginning, as a restarted server does.

        The whole reason the bot must empty the file on shutdown. Returns the commands that ran
        again, so a caller can assert the file was left clean — an empty list is the healthy answer.
        """
        self._offset = 0
        before = len(self.received)
        self.poll()
        return self.received[before:]

    # -- the log the server writes ----------------------------------------

    def push(self, event: dict[str, object], port: int | None = None) -> str:
        """Append one event to the game log and return the line written.

        ``port`` overrides ours, which is how a neighbouring server's traffic is simulated: the
        real file is shared, and reading somebody else's line is a real failure mode.
        """
        self._clock_ms += 137  # a plausible gap; the bot does not read this field
        line = json.dumps({"port": self.port if port is None else port, "time": self._clock_ms, **event})
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        return line

    def add_player(self, slot: int, nickname: str, vapor_id: str, ip: str = "10.0.0.1") -> str:
        """Emit `clientAdd` and remember the player, the way a real server knows its own roster."""
        self.players[slot] = (nickname, vapor_id)
        return self.push(
            {
                "type": "clientAdd",
                "player": slot,
                "nickname": nickname,
                "vaporId": vapor_id,
                "ip": f"{ip}:27272",
                "level": 9,
                "aceRank": 0,
                "demo": False,
            }
        )

    def remove_player(self, slot: int, reason: str = "Client left.") -> str | None:
        """Emit `clientRemove` for a player the server has."""
        if slot not in self.players:
            return None
        nickname, vapor_id = self.players.pop(slot)
        return self.push(
            {
                "type": "clientRemove",
                "player": slot,
                "nickname": nickname,
                "vaporId": vapor_id,
                "ip": "10.0.0.1:27272",
                "reason": reason,
                "message": "left",
            }
        )

    def say(self, slot: int, message: str) -> str:
        """A player talking. There is no team-chat variant in this game's log."""
        return self.push({"type": "chat", "player": slot, "message": message, "server": False})

    def ping_summary(self, pings: dict[int, int] | None = None) -> str:
        """The periodic ping report, which is the only list of who is connected this game offers."""
        if pings is None:
            pings = {slot: 40 + slot for slot in self.players}
        return self.push(
            {"type": "pingSummary", "pingByPlayer": {str(k): v for k, v in pings.items()}}
        )

    # -- the command file the server reads --------------------------------

    def poll(self) -> list[str]:
        """Read and execute everything appended since the last call. Returns our new commands."""
        if not self.command_path.exists():
            return []
        with self.command_path.open("r", encoding="utf-8") as handle:
            handle.seek(self._offset)
            new = handle.read()
            self._offset = handle.tell()

        ours: list[str] = []
        for raw in new.splitlines():
            if not raw.strip():
                continue
            match = COMMAND_LINE_RE.match(raw)
            if match is None:
                self.malformed.append(raw)
                continue
            port = int(match.group("port"))
            command = match.group("command")
            if port != self.port:
                self.for_other_ports.append((port, command))
                continue
            self.received.append(command)
            ours.append(command)
            self._execute(command)
        return ours

    def _execute(self, command: str) -> None:
        """Act on a command the way the server would, including writing to its own log."""
        verb, _, rest = command.partition(" ")
        if verb == "serverMessage":
            # Echoed back into the log with server=true. A bot that ignores that flag reads its own
            # announcements as a player talking -- and then answers them.
            self.push({"type": "chat", "player": -1, "message": rest, "server": True})
        elif verb == "serverWhisper":
            self.push({"type": "chat", "player": -1, "message": rest, "server": True})
        elif verb == "kick":
            slot = self._slot_by_name(rest.strip())
            if slot is not None:
                self.remove_player(slot, reason=KICK_REASON)
        elif verb == "addBan":
            parts = rest.split(" ", 3)
            if len(parts) >= 3:
                vapor_id, duration, unit = parts[0], parts[1], parts[2]
                reason = parts[3] if len(parts) > 3 else ""
                self.bans[vapor_id] = (duration, unit, reason)
        elif verb == "removeBan":
            self.bans.pop(rest.strip(), None)
        elif verb == "changeMap":
            self.push({"type": "mapChange", "map": rest.strip(), "mode": "ball"})

    def _slot_by_name(self, nickname: str) -> int | None:
        for slot, (name, _) in self.players.items():
            if name == nickname:
                return slot
        return None

    def wait_for_command(self, prefix: str) -> bool:
        """Whether any command received so far starts with ``prefix``. Poll first."""
        return any(command.startswith(prefix) for command in self.received)


def _demo() -> None:  # pragma: no cover - manual aid
    import tempfile

    directory = Path(tempfile.mkdtemp(prefix="b3-altitude-fake-"))
    server = FakeAltitudeServer(directory).start()
    server.add_player(0, "Courgette", "a8654321-123a-414e-c71a-123123123131")
    server.say(0, "hello")
    server.ping_summary()
    print(f"log file:     {server.log_path}")
    print(f"command file: {server.command_path}")
    print()
    print(server.log_path.read_text(encoding="utf-8"), end="")


if __name__ == "__main__":  # pragma: no cover
    _demo()
