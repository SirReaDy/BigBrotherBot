"""Altitude's "RCON": a file the game server reads commands out of.

There is no admin socket on this engine. The server tails a text file, and anything with write
access to it can drive the server by appending lines framed like this::

    <game server port>,console,<command>

Two consequences shape everything here.

**Nothing ever replies.** There is no request/response, so :meth:`AltitudeCommandFile.command`
returns an empty string always, and the bot cannot ask this engine anything: no player list, no
cvars, no map name. What the bot knows comes from the log instead — see
:class:`b3.parsers.altitude.parser.AltParser`, which keeps the roster the server reports there. The
profile therefore leaves ``status_command`` and ``rotation_cvar`` empty, which is what stops the
runtime asking a question that would be silently appended to this file as a bogus command.

**The file is a queue the server never truncates.** It remembers how far it has read, so everything
still in the file gets executed again the next time the server starts. A leftover ``kick Bob`` or
``addBan`` from a previous run is therefore live ammunition: it fires at whoever holds that name or
id later. So this class empties the file at **both** ends — on open and on close:

* *on close*, which is what the classic parser did (in its ``shutdown``), and
* *on open*, which it did not. A bot that is killed rather than stopped cleanly never runs its
  shutdown, and the classic bot's file kept its commands for the game server to find. Clearing on
  open costs at most a command written in the last instant before the crash, and the bot's own
  database is the authority on bans anyway — an unsent one is re-applied on reconnect. An old
  penalty landing on the wrong player has no such recovery.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

#: The only channel the game reads console commands from.
CHANNEL = "console"


class AltitudeCommandFile:
    """Write commands to an Altitude server's command file. Satisfies the bot's ``RconClient``.

    ``port`` is the game server's own port, and it is not decoration: one Altitude installation runs
    several servers against **one shared command file**, and the prefix is what says which of them a
    command is for. Get it wrong and the command silently runs on somebody else's server, or on none.
    """

    def __init__(self, path: str | Path, port: int, encoding: str = "utf-8") -> None:
        self.path = Path(path)
        self.port = port
        self.encoding = encoding

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Create the file if it is missing, and empty it. See the module docstring for why."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        leftover = ""
        if self.path.exists():
            leftover = self.path.read_text(encoding=self.encoding, errors="replace").strip()
        self.clear()
        if leftover:
            # Worth a line in the log: it means the previous run did not stop cleanly, and it says
            # exactly what the game server would otherwise have replayed.
            log.warning(
                "%s still held %d command(s) from a previous run; discarded rather than left for "
                "the game server to replay",
                self.path,
                len(leftover.splitlines()),
            )

    def clear(self) -> None:
        """Truncate the command file."""
        self.path.write_text("", encoding=self.encoding)

    def close(self) -> None:
        """Empty the file so the game server does not re-run this session's commands on restart."""
        try:
            self.clear()
        except OSError as exc:  # pragma: no cover - a vanished directory at shutdown
            log.warning("could not clear %s: %s", self.path, exc)

    # -- sending -----------------------------------------------------------

    def command(self, cmd: str) -> str:
        """Queue ``cmd`` for the server. Always returns "" — this engine answers nothing."""
        self.write(cmd)
        return ""

    def write(self, cmd: str) -> None:
        """Append ``cmd`` to the file, framed for our port.

        A ``cmd`` containing newlines is written as **several commands**, one per line. That is not
        an invention: the file is a list of newline-separated commands, so a newline already means
        "and then this". It is how a profile expresses a penalty this engine needs two verbs for —
        ``addBan`` does not remove the player who is already connected, so a ban is a ban *and* a
        kick. Every value interpolated into a template has been through
        :func:`b3.core.util.sanitize_rcon_value`, which strips newlines, so only the template itself
        can put one here.
        """
        lines = [part.strip() for part in cmd.splitlines()]
        lines = [part for part in lines if part]
        if not lines:
            # An empty command would be written as a bare "27276,console," -- a line the server has
            # to parse and can do nothing with. Refusing is not hiding an error: no caller means it.
            log.debug("altitude: refusing to queue an empty command")
            return
        payload = "".join(f"{self.port},{CHANNEL},{line}\n" for line in lines)
        # Opened per write rather than held open, deliberately. The game server is *tailing* this
        # file, so a buffered handle would delay every command by however long the buffer takes to
        # fill -- the same failure that made a locally-tailed game log arrive in bursts. Closing
        # each time also leaves the operator free to rotate or delete the file while the bot runs.
        with self.path.open("a", encoding=self.encoding) as handle:
            handle.write(payload)
        for line in lines:
            log.debug("altitude command: %s", line)


__all__ = ["CHANNEL", "AltitudeCommandFile"]
