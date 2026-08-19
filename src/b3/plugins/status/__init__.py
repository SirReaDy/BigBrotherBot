"""Publishes what is happening on the server, for something outside the bot to read.

A port of the classic `status` plugin. Every minute it writes the server's state and its player list
somewhere a web page can find: a file on disk, a file uploaded over FTP, and/or two tables in the
database. Communities used it to put "who is playing right now" on their site, and that is still the
job — the bot knows the roster, the map, the scores and the levels, and nothing else does.

Kept from the classic: the interval, the local-file and FTP destinations, the two database tables, the
field names of its XML (so a status page written for the classic bot keeps working), and the empty
document written on shutdown — without which a page shows the last players forever and the server
looks busy while it is off.

Changed, and each for a reason:

* **JSON is the default and XML is the compatibility option.** The classic only wrote XML, because in
  2005 that is what a PHP page parsed. `format: xml` still writes the same element and attribute names
  as the classic did, for a page that already exists.
* **The database tables are created once, not dropped and rebuilt at every startup.** The classic ran
  `DROP TABLE` then `CREATE TABLE` on boot and `TRUNCATE` on every update, in raw SQL, with the table
  names substituted into it from config. Here they are ordinary plugin-owned tables (see
  `Plugin.storage_engine`), emptied per update with a delete.
* **A destination that cannot be written is reported once, not once a minute.** A status file on a full
  disk, or an FTP host that is down, otherwise fills the log with the same line 1,440 times a day.
* **Uploads happen on a worker thread**, so a slow or hanging FTP server cannot stall the bot. The
  classic did the upload inline on its cron thread, which is the same thing when that thread is what
  the rest of the plugin runs on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from sqlalchemy import Integer, String, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.permissions import max_group

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

#: How long an upload may take before it is treated as a failure.
UPLOAD_TIMEOUT = 20.0

DEFAULTS: dict[str, object] = {
    # Seconds between updates. A minute is the classic's figure and a sensible one: this is a page
    # somebody refreshes, not a live feed.
    "interval": 60,
    # Where to write it. A filesystem path (the `@conf` / `@b3` / `@home` tokens work), or an
    # `ftp://user:password@host/path/status.json` URL for a site on another machine.
    "output_file": "@conf/status.json",
    # `json` or `xml`. XML uses the classic bot's element and attribute names, so a status page
    # written for it keeps working.
    "format": "json",
    # Also write the state into two tables of this plugin's own, for a page that reads the database
    # rather than a file. Off by default: a file is simpler and does not grow a schema.
    "save_to_database": False,
    # Include player addresses. Off by default, and that is a change from the classic, which published
    # every player's IP to whatever could read the file — usually a public web page.
    "include_ip": False,
}


class Base(DeclarativeBase):
    """This plugin's own tables. The core's schema is Alembic-managed and is not touched here."""


class ServerStatus(Base):
    """One row per named value about the server — the classic's `current_svars`."""

    __tablename__ = "status_server"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255), default="")


class ClientStatus(Base):
    """One row per connected player — the classic's `current_clients`."""

    __tablename__ = "status_clients"

    cid: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    level: Mapped[int] = mapped_column(Integer, default=0)
    database_id: Mapped[int] = mapped_column(Integer, default=0)
    connections: Mapped[int] = mapped_column(Integer, default=0)
    guid: Mapped[str] = mapped_column(String(64), default="")
    pbid: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    team: Mapped[str] = mapped_column(String(16), default="")
    score: Mapped[int] = mapped_column(Integer, default=0)
    ping: Mapped[int] = mapped_column(Integer, default=0)
    alive: Mapped[int] = mapped_column(Integer, default=1)
    is_bot: Mapped[int] = mapped_column(Integer, default=0)


@dataclass
class Destination:
    """Where the status goes, and whether saying so has already failed once.

    The failure flag is the whole reason this is an object: a status file on a full disk would
    otherwise log the same line every minute, 1,440 times a day, and bury everything else.
    """

    path: Path | None = None
    url: str = ""
    failing: bool = False

    def describe(self) -> str:
        if self.url:
            parts = urlsplit(self.url)
            host = parts.hostname or "?"
            return f"{parts.scheme}://{host}{parts.path}"  # never the credentials
        return str(self.path)


@dataclass
class Snapshot:
    """What the bot knows right now, in the shape both writers render."""

    taken_at: str
    server: dict[str, Any] = field(default_factory=dict)
    clients: list[dict[str, Any]] = field(default_factory=list)


class StatusPlugin(Plugin):
    """Writes the server's state and roster where something else can read it."""

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.destination = Destination()
        self._engine: Engine | None = None

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.destination = self._destination(str(self.settings.get("output_file") or ""))
        if str(self.settings.get("format", "json")).lower() not in ("json", "xml"):
            log.error(
                "status: format %r is neither json nor xml; writing json",
                self.settings.get("format"),
            )
            self.settings["format"] = "json"

    def _destination(self, value: str) -> Destination:
        """A local path or an upload URL. The classic accepted both in one setting, and so does this."""
        if value.startswith(("ftp://", "ftps://")):
            return Destination(url=value)
        from b3.config.loader import resolve_path_token

        config_path = getattr(self.console, "config_path", None)
        conf_dir = Path(config_path).parent if config_path else None
        return Destination(path=Path(resolve_path_token(value, conf_dir)))

    def on_startup(self) -> None:
        if self.settings.get("save_to_database"):
            self._engine = self._open_storage()
        interval = max(5, as_int(self.settings.get("interval"), 60))
        # A coroutine rather than a plain function, because writing the file and uploading it are
        # blocking work handed to a worker: the event loop must not wait on an FTP handshake.
        #
        # The schedule is a crontab, so an interval under a minute is expressed in seconds and one
        # above it in minutes. A floor of five seconds because each update reads the server's player
        # table, and asking a game server that often is a cost with no reader for it.
        if interval < 60:
            self.schedule(self.publish, second=f"*/{interval}", name="StatusPlugin.publish")
        else:
            minutes = max(1, interval // 60)
            self.schedule(
                self.publish, second="0", minute=f"*/{minutes}", name="StatusPlugin.publish"
            )
        # The last thing written should say the server is empty, or a page shows the players who were
        # here when the bot stopped and the server looks busy while it is off.
        self.subscribe(EventType.STOP, self.on_stop)
        log.info(
            "status: publishing every %ds as %s to %s",
            interval,
            self.settings.get("format"),
            self.destination.describe(),
        )

    def _open_storage(self) -> Engine | None:
        try:
            engine = self.storage_engine()
        except RuntimeError as exc:
            log.warning("status: %s; nothing will be written to the database", exc)
            return None
        try:
            Base.metadata.create_all(engine)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop the bot over its own tables
            log.error("status: could not create its tables (%s)", exc)
            return None
        return engine  # type: ignore[return-value]

    # -- gathering -----------------------------------------------------------

    def snapshot(self, scores: dict[str, tuple[int, int]] | None = None) -> Snapshot:
        """Everything worth publishing, taken from the client list and the game state.

        ``scores`` maps a slot to (score, ping) and comes from the server's own table, which is the
        only place either exists. Without it the roster is still published — a page that shows who is
        on the server with no scores is worth far more than no page.
        """
        game = self.console.game
        now = self.console.clock.now()
        clients = self.console.clients.connected()
        include_ip = bool(self.settings.get("include_ip"))
        snapshot = Snapshot(taken_at=self.console.format_time(now))
        snapshot.server = {
            "name": game.hostname,
            "game": getattr(getattr(self.console, "profile", None), "name", ""),
            "gametype": game.gametype,
            "map": self.console.map_display(game.map_name) if game.map_name else "",
            "map_id": game.map_name,
            "rounds": game.rounds,
            "max_players": game.max_players,
            "players": len(clients),
            "version": game.version,
            "map_uptime": int(game.map_uptime(now)),
            "round_uptime": int(game.round_uptime(now)),
        }
        for client in clients:
            score, ping = (scores or {}).get(client.cid or "", (0, 0))
            group = max_group(client.group_bits)
            snapshot.clients.append(
                {
                    "cid": client.cid or "",
                    "name": client.name,
                    # The *displayed* level, so a masked admin is masked here too: this file is
                    # usually a public web page, and `!mask` exists to hide rank from players.
                    "level": client.display_level(),
                    "group": group.keyword if group else "guest",
                    "database_id": client.id or 0,
                    "connections": client.connections,
                    "guid": client.guid,
                    "pbid": client.pbid,
                    "ip": client.ip if include_ip else "",
                    "team": client.team or "",
                    "score": score,
                    "ping": ping,
                    "alive": client.alive,
                    "bot": client.is_bot,
                }
            )
        return snapshot

    def _scores(self) -> dict[str, tuple[int, int]]:
        """Scores and pings from the server's own player table, or nothing if it will not answer."""
        try:
            return {p.cid: (p.score, p.ping) for p in self.console.get_players()}
        except Exception as exc:  # noqa: BLE001 - a status file is not worth an exception
            log.debug("status: could not read the player table (%s)", exc)
            return {}

    # -- rendering -----------------------------------------------------------

    def render(self, snapshot: Snapshot) -> str:
        if str(self.settings.get("format", "json")).lower() == "xml":
            return self.render_xml(snapshot)
        return json.dumps(
            {"time": snapshot.taken_at, "server": snapshot.server, "clients": snapshot.clients},
            indent=2,
        )

    def render_xml(self, snapshot: Snapshot) -> str:
        """The classic bot's document, element for element, so an existing status page still parses it.

        `B3Status/Game` with its attributes and `B3Status/Clients/Client` per player. The names are
        CamelCase because that is what the classic wrote and what those pages read; nothing else here
        uses that convention.
        """
        root = ElementTree.Element("B3Status", {"Time": snapshot.taken_at})
        game = ElementTree.SubElement(
            root,
            "Game",
            {
                "Name": str(snapshot.server.get("game", "")),
                "Type": str(snapshot.server.get("gametype", "")),
                "Map": str(snapshot.server.get("map", "")),
                "Rounds": str(snapshot.server.get("rounds", "")),
                "MapTime": str(snapshot.server.get("map_uptime", "")),
                "RoundTime": str(snapshot.server.get("round_uptime", "")),
                "OnlinePlayers": str(snapshot.server.get("players", 0)),
            },
        )
        for key, value in snapshot.server.items():
            ElementTree.SubElement(game, "Data", {"Name": str(key), "Value": str(value)})
        clients = ElementTree.SubElement(root, "Clients", {"Total": str(len(snapshot.clients))})
        for entry in snapshot.clients:
            ElementTree.SubElement(
                clients,
                "Client",
                {
                    "CID": str(entry["cid"]),
                    "Name": str(entry["name"]),
                    "Level": str(entry["level"]),
                    "DBID": str(entry["database_id"]),
                    "Connections": str(entry["connections"]),
                    "GUID": str(entry["guid"]),
                    "PBID": str(entry["pbid"]),
                    "IP": str(entry["ip"]),
                    "Team": str(entry["team"]),
                    "Score": str(entry["score"]),
                    "Ping": str(entry["ping"]),
                },
            )
        ElementTree.indent(root, space="  ")
        return ElementTree.tostring(root, encoding="unicode") + "\n"

    # -- publishing ----------------------------------------------------------

    async def publish(self) -> None:
        """Take a snapshot and write it wherever it goes. The scheduled job."""
        scores = await asyncio.to_thread(self._scores)
        snapshot = self.snapshot(scores)
        text = self.render(snapshot)
        await asyncio.to_thread(self._write, text)
        if self._engine is not None:
            await asyncio.to_thread(self._save, snapshot)

    def on_stop(self, event: Event) -> None:
        """Write an empty status as the bot goes down.

        Synchronous on purpose: the loop is stopping, so there is nobody left to await a worker. A
        local file is one write; an upload is skipped, because a socket handshake during shutdown is
        how a bot comes to take thirty seconds to stop.
        """
        empty = Snapshot(taken_at=self.console.format_time(self.console.clock.now()))
        empty.server = {"players": 0, "name": self.console.game.hostname, "running": False}
        if self.destination.path is not None:
            self._write(self.render(empty))
        if self._engine is not None:
            self._save(empty)

    def _write(self, text: str) -> None:
        """Write the rendered status to its destination, complaining at most once per outage."""
        try:
            if self.destination.url:
                self._upload(text)
            elif self.destination.path is not None:
                self.destination.path.parent.mkdir(parents=True, exist_ok=True)
                self.destination.path.write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - a status page is never worth stopping the bot
            if not self.destination.failing:
                self.destination.failing = True
                log.error(
                    "status: cannot write to %s (%s); this will not be repeated until it works",
                    self.destination.describe(),
                    exc,
                )
            return
        if self.destination.failing:
            self.destination.failing = False
            log.info("status: %s is writable again", self.destination.describe())

    def _upload(self, text: str) -> None:
        """Put the file on an FTP server — a site that lives on another machine.

        Blocking, and called through a worker thread. The credentials are in the URL because that is
        where the classic put them and where operators' configs already have them; nothing here logs
        the URL without stripping them first (`Destination.describe`).
        """
        from ftplib import FTP, FTP_TLS
        from io import BytesIO

        parts = urlsplit(self.destination.url)
        host, port = parts.hostname or "", parts.port or 21
        user = unquote(parts.username or "anonymous")
        password = unquote(parts.password or "")
        remote = parts.path or "/status.json"
        client = FTP_TLS() if parts.scheme == "ftps" else FTP()
        client.connect(host, port, timeout=UPLOAD_TIMEOUT)
        try:
            client.login(user, password)
            if isinstance(client, FTP_TLS):
                client.prot_p()
            directory, _, filename = remote.rpartition("/")
            if directory:
                client.cwd(directory)
            client.storbinary(f"STOR {filename}", BytesIO(text.encode("utf-8")))
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001 - the file is already sent; a rude goodbye is fine
                client.close()

    def _save(self, snapshot: Snapshot) -> None:
        """Replace the two tables' contents with this snapshot.

        Deleted and re-inserted rather than the classic's `DROP TABLE` + `CREATE TABLE` on every
        startup and `TRUNCATE` on every update — all of it raw SQL with table names substituted in
        from config.
        """
        if self._engine is None:
            return
        try:
            with Session(self._engine) as session:
                session.execute(delete(ServerStatus))
                session.execute(delete(ClientStatus))
                for key, value in snapshot.server.items():
                    session.add(ServerStatus(name=str(key), value=str(value)))
                for entry in snapshot.clients:
                    session.add(
                        ClientStatus(
                            cid=str(entry["cid"]),
                            name=str(entry["name"]),
                            level=int(entry["level"]),
                            database_id=int(entry["database_id"]),
                            connections=int(entry["connections"]),
                            guid=str(entry["guid"]),
                            pbid=str(entry["pbid"]),
                            ip=str(entry["ip"]),
                            team=str(entry["team"]),
                            score=int(entry["score"]),
                            ping=int(entry["ping"]),
                            alive=int(bool(entry["alive"])),
                            is_bot=int(bool(entry["bot"])),
                        )
                    )
                session.commit()
        except Exception as exc:  # noqa: BLE001 - same rule as the file: never fatal
            log.error("status: could not write its tables (%s)", exc)


__all__ = [
    "DEFAULTS",
    "Base",
    "ClientStatus",
    "Destination",
    "ServerStatus",
    "Snapshot",
    "StatusPlugin",
]
