"""Urban Terror jump servers — timed runs, records per way, and the demo of each one.

A port of the classic `jumper`, and the last of the classic tree's plugins. A jump map is an obstacle
course rather than a fight: the server times a player from the first pad to the last one and reports
it, a map has several *ways* through it, and what the players are there for is the leaderboard. So
this plugin keeps every run that beat the runner's own best, names the ways, announces a new map
record, and — with `urtserversidedemo` loaded — records a demo of each attempt and throws it away
again unless the run was worth keeping.

`!record` is your own best times on this map, `!maprecord` the best anybody has, `!topruns` the top
three per way, `!delrecord` removes somebody's, `!setway` gives a way a name instead of a number, and
`!mapinfo` says who built the map and when.

Changed from the classic:

* **A player's typing no longer reaches SQL.** Every one of its twelve queries was a `%`-formatted
  string, and two of them take text a *player* chose: `!setway 1 <name>` went into
  `INSERT INTO jumpways VALUES (NULL, '%s', '%d', '%s')`. This owns two tables through the ORM and
  there is no SQL in the plugin at all. (The same shape as `callvote`'s fault, and found the same
  way.)
* **`!topruns` raised on the case it was written to handle.** Its warning for a record whose player
  is missing from the clients table read `r1['client_id']` — but `r1` comes from
  `SELECT DISTINCT way_id`, which has no such column, so the branch meant to survive a deleted
  player raised `KeyError` instead.
* **Nothing sleeps and nothing is a thread.** The classic welcomed players onto a new map from a
  `threading.Timer(30)`, and fetched the map catalogue over HTTP **on the bot's own thread** at every
  map change and inside `!mapinfo`. The greeting is a deadline on this plugin's scheduled pass, and
  the fetch is a scheduled coroutine handing the blocking call to a worker thread — the shape
  `banlist` already uses for the same job.
* **Dates are formatted portably.** `datetime.strptime(d, '%Y-%m-%d').strftime('%s')` — `%s` is not a
  documented format code; it works on glibc and raises or prints nonsense elsewhere, so every
  Windows operator lost the release date and the map greeting with it.
* **`!setway` names a command that exists.** Its usage line said `!help jmpsetway`, a name from an
  older release, so the one message a player sees when they get the syntax wrong sent them to a
  command the bot does not have.
* **The automatic map cycle is guarded in both directions.** `skip_standard_maps` cycles away from a
  stock map, with a five-in-a-row limit so a server whose whole rotation is stock maps does not spin;
  the classic applied that limit on a map change but *not* when the plugin was enabled, which is the
  one moment an operator is watching.
* **A run is credited to a player the database knows.** Records are keyed on the client's database
  id, and a player who has not authenticated yet has none — the classic wrote `None` into the column
  and produced rows nothing could ever read back.

**Not ported: `!map`, `!maps` and `!setnextmap`.** The classic *replaced* the admin plugin's versions
of all three — it read their level and alias out of the command table, unregistered them, and
registered its own — to make them skip the stock maps. Two plugins owning one command name is the
thing the command registry is meant to prevent, and the reason the classic wanted it is thinner than
it looks: a jump server's rotation is jump maps, and this plugin cycles away from a stock map within
seconds of it loading. `!maps` and `!map` are the admin plugin's here, and they work.

**The demo half needs `urtserversidedemo`.** It is asked for by name, and where it is absent the runs
are still timed and recorded — only the demos are missing, which is said once at startup rather than
left to be discovered. Deleting a demo needs the bot to be on the *same machine* as the game server,
since the file is written by the server; where it is not, the demo stays on disk and that is reported
once rather than every run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Integer, String, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, match_names
from b3.domain.client import Client
from b3.domain.permissions import level_for

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

#: The maps Urban Terror ships. A jump server runs none of them, so `skip_standard_maps` cycles off
#: one that loads — usually because somebody voted for it. The classic's own list, verbatim.
STANDARD_MAPS = frozenset(
    {
        "ut4_abbey",
        "ut4_abbeyctf",
        "ut4_algiers",
        "ut4_ambush",
        "ut4_austria",
        "ut4_bohemia",
        "ut4_casa",
        "ut4_cascade",
        "ut4_commune",
        "ut4_company",
        "ut4_crossing",
        "ut4_docks",
        "ut4_dressingroom",
        "ut4_eagle",
        "ut4_elgin",
        "ut4_firingrange",
        "ut4_ghosttown_rc4",
        "ut4_harbortown",
        "ut4_herring",
        "ut4_horror",
        "ut4_kingdom",
        "ut4_kingpin",
        "ut4_mandolin",
        "ut4_maya",
        "ut4_oildepot",
        "ut4_prague",
        "ut4_prague_v2",
        "ut4_raiders",
        "ut4_ramelle",
        "ut4_ricochet",
        "ut4_riyadh",
        "ut4_sanc",
        "ut4_snoppis",
        "ut4_suburbs",
        "ut4_subway",
        "ut4_swim",
        "ut4_thingley",
        "ut4_tombs",
        "ut4_toxic",
        "ut4_tunis",
        "ut4_turnpike",
        "ut4_uptown",
    }
)

#: How many stock maps **in a row** the plugin will cycle past before leaving one alone. The classic's
#: figure and its reasoning: a server whose whole rotation is stock maps would otherwise cycle for
#: ever, which is worse than playing one. A burst limit rather than a total — the count starts again
#: once a map is played, since refusing to cycle again *ever* would strand a jump server on them.
MAX_CYCLES = 5

#: Seconds after a map loads before the greeting goes out — long enough for players to have finished
#: connecting and read the map name themselves. The classic's `Timer(30.0)`, as a deadline.
WELCOME_SECONDS = 30.0

#: How long to wait on the map catalogue, and how often to refresh it. The classic asked at every map
#: change with a four-second timeout, on the bot's thread.
CATALOGUE_TIMEOUT = 10.0
CATALOGUE_HOURS = 6

#: How many top runs per way `!topruns` lists. The classic's `LIMIT 3`.
TOP_RUNS = 3

#: `!setway <id> <name>` — the id is a number and the name is the rest of the line.
SETWAY_RE = re.compile(r"^(?P<way_id>\d+)\s+(?P<way_name>\S.*)$")

DEFAULTS: dict[str, object] = {
    # Record a demo of every attempt, keeping it only where the run set a record. Needs
    # `urtserversidedemo` loaded; without it this is switched off with a word in the log.
    "demo_record": True,
    # Cycle off a map Urban Terror ships, which on a jump server is a map nobody wanted.
    "skip_standard_maps": True,
    # Level needed to delete somebody else's records. The classic's default of senioradmin.
    "min_level_delete": 80,
    # Where the map catalogue comes from — who built a map, when, how many ways it has. The classic
    # hardcoded this URL; it is a setting so an operator can point it somewhere else or, by emptying
    # it, keep the bot from talking to anybody. `!mapinfo` then says it has nothing to answer with.
    "catalogue_url": "http://api.urtjumpers.com/?key=B3urtjumpersplugin&liste=maps&format=json",
}

MESSAGES = {
    "jumper_record_none": "no record for {name} on {map}",
    "jumper_record_header": "records for {name} on {map}:",
    "jumper_record_line": "[{way}] {time} since {date}",
    "jumper_map_record_none": "no record on {map}",
    "jumper_map_record_header": "map records on {map}:",
    "jumper_map_record_line": "[{way}] {name} with {time}",
    "jumper_top_header": "top runs on {map}:",
    "jumper_top_line": "[{way}] #{place} {name} with {time}",
    "jumper_map_record": "{name} set a new map record: {time}",
    "jumper_personal_record": "a new personal record on {map}: {time}",
    "jumper_personal_failed": "{time} — your best on this way is {best}, keep trying",
    "jumper_deleted": "removed {count} record(s) for {name} on {map}",
    "jumper_delete_denied": "you may not delete {name}'s records",
    "jumper_setway_usage": "!setway <way> <name>",
    "jumper_setway": "way {way} on {map} is called {name}",
    "jumper_map_unknown": "no map like {map} — try {maps}",
    "jumper_welcome": "welcome to {map}",
    "jumper_welcome_author": "{map} was built by {author}, {date} — !mapinfo for more",
    "jumper_info_none": "nothing is known about {map}",
    "jumper_info_unavailable": "the map catalogue could not be read",
    "jumper_info_author": "{map} was built by {author}",
    "jumper_info_author_unknown": "nobody knows who built {map}",
    "jumper_info_released": "released {date}",
    "jumper_info_ways": "{ways} way(s), {jumps} jump(s)",
    "jumper_info_level": "difficulty {level}/100",
}


class Base(DeclarativeBase):
    """This plugin's own tables. The core's schema is Alembic-managed and is not touched here."""


class JumpRunRecord(Base):
    """One player's best time on one way of one map.

    Deliberately **not** stamped with a `server_id`, unlike `callvote`'s history: a community running
    two jump servers against one database wants one leaderboard, and the map name is what a record is
    about. `!lastvote` had the opposite need — it answers about *this* server.
    """

    __tablename__ = "jumper_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(Integer, index=True)
    mapname: Mapped[str] = mapped_column(String(64), index=True)
    way_id: Mapped[int] = mapped_column(Integer, index=True)
    #: The time itself, in **milliseconds**, which is what the engine reports.
    way_time: Mapped[int] = mapped_column(Integer)
    time_add: Mapped[int] = mapped_column(Integer, default=0)
    time_edit: Mapped[int] = mapped_column(Integer, default=0)
    #: The demo the server wrote for this run, where one was kept. Empty rather than NULL: "there is
    #: no demo" and "the column has never been written" are the same thing to every reader here.
    demo: Mapped[str] = mapped_column(String(160), default="")


class JumpWay(Base):
    """A name for a way, so a leaderboard can say "the roof route" instead of "way 3"."""

    __tablename__ = "jumper_ways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mapname: Mapped[str] = mapped_column(String(64), index=True)
    way_id: Mapped[int] = mapped_column(Integer, index=True)
    way_name: Mapped[str] = mapped_column(String(64), default="")


@dataclass(slots=True)
class Run:
    """A run in progress: who, which way, and the demo the server is writing for it."""

    client: Client
    mapname: str
    way_id: int
    demo: str = ""


@dataclass(slots=True)
class Best:
    """What the database held for this player and way before the run that just finished."""

    record: JumpRunRecord | None
    map_best: int | None


def format_time(msec: int) -> str:
    """A run time as players read it — `1:02:03.456`. The classic's own shape."""
    seconds, msec = divmod(max(0, msec), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:01d}:{minutes:02d}:{seconds:02d}.{msec:03d}"


def format_date(epoch: int) -> str:
    """A date as players read it. `strftime('%s')` in the classic, which is not a format code."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%a, %d %b %Y")


def parse_release_date(text: str) -> int:
    """`2011-07-16` from the catalogue, as an epoch. 0 when it is not a date at all.

    The classic did this with `strptime(...).strftime('%s')`: `%s` is a glibc extension, so on
    Windows it either raised or produced the literal `%s`, and the release date — and the map
    greeting that quotes it — was lost for every operator not on Linux.
    """
    try:
        return int(
            datetime.strptime(text.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        )
    except (TypeError, ValueError):
        return 0


class JumperPlugin(Plugin):
    """Timed runs on Urban Terror jump maps: the records, the ways, and a demo of each attempt."""

    requires_plugins = ("admin",)
    requires_parsers = ("iourt42", "iourt43")
    load_after = ("urtserversidedemo",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self._engine: Engine | None = None
        #: Runs in progress, by client. A player can only be on one way at a time.
        self.runs: list[Run] = []
        #: The map catalogue, keyed by map name in lower case. Empty until the first fetch answers.
        self.catalogue: dict[str, dict[str, Any]] = {}
        #: When to greet the players on a new map, and which map that was.
        self._welcome_at = 0.0
        self._welcome_map = ""
        #: Stock maps cycled past in a row, so a rotation full of them cannot spin for ever.
        self._cycles = 0
        #: Said once rather than once per run, since it is a fact about the installation.
        self._warned_about_demo_paths = False

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self._engine = self._open_storage()
        if self.settings.get("demo_record") and self.demo_plugin() is None:
            log.warning(
                "jumper: demo_record is on but the urtserversidedemo plugin is not loaded, so no "
                "demo of a jump run can be recorded"
            )
        self.schedule(self._run_pending, second="*", name="JumperPlugin.pending")
        self.schedule(
            self._refresh_catalogue, hour=f"*/{CATALOGUE_HOURS}", name="JumperPlugin.catalogue"
        )
        self.subscribe(EventType.CLIENT_JUMP_RUN_START, self.on_run_start)
        self.subscribe(EventType.CLIENT_JUMP_RUN_STOP, self.on_run_stop)
        self.subscribe(EventType.CLIENT_JUMP_RUN_CANCEL, self.on_run_cancel)
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_team_change)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.subscribe(EventType.GAME_ROUND_START, self.on_map_start)
        # Anything the server was already recording is stopped: the classic did this too, and for a
        # good reason — a demo started before the bot came up has a filename nothing here knows, so
        # it could never be attached to a run or deleted.
        demos = self.demo_plugin()
        if demos is not None:
            demos.stop_all()

    def on_disable(self) -> None:
        """Every run in progress is abandoned, and its demo with it."""
        for run in list(self.runs):
            self._cancel(run)

    def _open_storage(self) -> Engine | None:
        try:
            engine = self.storage_engine()
        except RuntimeError as exc:
            log.error("jumper: %s; no run can be recorded", exc)
            return None
        try:
            Base.metadata.create_all(engine)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop the bot over its own table
            log.error("jumper: could not create its tables (%s)", exc)
            return None
        return engine  # type: ignore[return-value]

    def demo_plugin(self) -> Any:
        """The `urtserversidedemo` plugin, if the operator loaded it.

        Asked for each time rather than held: it can be enabled and disabled at runtime (`!plugin`),
        and the classic tracked that with two event handlers and a flag that could disagree with it.
        """
        if not self.settings.get("demo_record"):
            return None
        plugin = self.console.get_plugin("urtserversidedemo")
        if plugin is None or not getattr(plugin, "is_enabled", bool)():
            return None
        return plugin

    # -- the run -------------------------------------------------------------

    def run_of(self, client: Client) -> Run | None:
        return next((run for run in self.runs if run.client is client), None)

    def on_run_start(self, event: Event) -> None:
        """A player set off. Any run they had going is abandoned, demo and all."""
        client = event.client
        data = event.data if isinstance(event.data, dict) else {}
        if client is None:
            return
        previous = self.run_of(client)
        if previous is not None:
            self._cancel(previous)
        run = Run(
            client=client,
            mapname=self._mapname(),
            way_id=as_int(data.get("way_id"), 0),
        )
        demos = self.demo_plugin()
        if demos is not None:
            started = demos.record(client)
            run.demo = started.filename
            if not run.demo:
                log.warning(
                    "jumper: no demo of %s's run on way %d: %s",
                    client.name,
                    run.way_id,
                    started.reply or "the server said nothing",
                )
        self.runs.append(run)

    def on_run_cancel(self, event: Event) -> None:
        client = event.client
        if client is None:
            return
        run = self.run_of(client)
        if run is not None:
            self._cancel(run)

    def on_run_stop(self, event: Event) -> None:
        """A run finished. This is where a record is made, or not."""
        client = event.client
        data = event.data if isinstance(event.data, dict) else {}
        if client is None:
            return
        run = self.run_of(client)
        if run is None:
            # A run whose start this bot never saw — it was under way when the bot connected. There
            # is no demo and no way to know when it began, but the *time* is the server's and stands.
            run = Run(client=client, mapname=self._mapname(), way_id=as_int(data.get("way_id"), 0))
        else:
            self.runs.remove(run)
        way_time = as_int(data.get("way_time"), 0)
        self._stop_demo(run)
        if way_time <= 0 or client.id is None:
            # The classic wrote `None` into `client_id` for a player who had not authenticated, which
            # produces rows nothing can read back.
            self._unlink_demo(run)
            return
        self._finish(run, way_time)

    def _finish(self, run: Run, way_time: int) -> None:
        """Record the run if it beat the player's own best, and say what happened."""
        best = self._best_for(run)
        if best.record is not None and way_time >= best.record.way_time:
            self.console.tell(
                run.client,
                self.message(
                    "jumper_personal_failed",
                    time=format_time(way_time),
                    best=format_time(best.record.way_time),
                ),
            )
            self._unlink_demo(run)
            return
        # The previous demo is the one to delete now: this run replaces it.
        if best.record is not None and best.record.demo:
            self._unlink(best.record.demo)
        self._store(run, way_time, replacing=best.record)
        if best.map_best is None or way_time < best.map_best:
            self.console.say_big(
                self.message("jumper_map_record", name=run.client.name, time=format_time(way_time))
            )
            return
        self.console.tell(
            run.client,
            self.message(
                "jumper_personal_record", map=self._display(run.mapname), time=format_time(way_time)
            ),
        )

    def _cancel(self, run: Run) -> None:
        """Give up on a run: stop its demo and delete it, since it recorded nothing."""
        if run in self.runs:
            self.runs.remove(run)
        self._stop_demo(run)
        self._unlink_demo(run)

    def _stop_demo(self, run: Run) -> None:
        demos = self.demo_plugin()
        if demos is not None and run.demo:
            demos.stop(run.client)

    def _unlink_demo(self, run: Run) -> None:
        if run.demo:
            self._unlink(run.demo)
            run.demo = ""

    # -- the records ---------------------------------------------------------

    def _best_for(self, run: Run) -> Best:
        """This player's row for the way, and the best time anybody has on it."""
        if self._engine is None or run.client.id is None:
            return Best(record=None, map_best=None)
        with Session(self._engine) as session:
            record = session.scalars(
                select(JumpRunRecord).where(
                    JumpRunRecord.client_id == run.client.id,
                    JumpRunRecord.mapname == run.mapname,
                    JumpRunRecord.way_id == run.way_id,
                )
            ).first()
            if record is not None:
                session.expunge(record)
            map_best = session.scalars(
                select(JumpRunRecord.way_time)
                .where(
                    JumpRunRecord.mapname == run.mapname,
                    JumpRunRecord.way_id == run.way_id,
                )
                .order_by(JumpRunRecord.way_time.asc())
                .limit(1)
            ).first()
        return Best(record=record, map_best=map_best)

    def _store(self, run: Run, way_time: int, replacing: JumpRunRecord | None) -> None:
        if self._engine is None or run.client.id is None:
            return
        now = int(self.console.clock.now())
        with Session(self._engine) as session:
            if replacing is not None:
                fresh = session.get(JumpRunRecord, replacing.id)
                if fresh is not None:
                    fresh.way_time = way_time
                    fresh.time_edit = now
                    fresh.demo = run.demo
                    session.commit()
                    return
            session.add(
                JumpRunRecord(
                    client_id=run.client.id,
                    mapname=run.mapname,
                    way_id=run.way_id,
                    way_time=way_time,
                    time_add=now,
                    time_edit=now,
                    demo=run.demo,
                )
            )
            session.commit()

    def records_of(self, client: Client, mapname: str) -> list[JumpRunRecord]:
        """One player's records on a map, by way."""
        if self._engine is None or client.id is None:
            return []
        with Session(self._engine) as session:
            rows = list(
                session.scalars(
                    select(JumpRunRecord)
                    .where(
                        JumpRunRecord.client_id == client.id,
                        JumpRunRecord.mapname == mapname,
                    )
                    .order_by(JumpRunRecord.way_id.asc())
                )
            )
            for row in rows:
                session.expunge(row)
        return rows

    def map_records(self, mapname: str) -> list[JumpRunRecord]:
        """The best run on each way of a map, one row per way."""
        return [rows[0] for rows in self._by_way(mapname, limit=1) if rows]

    def top_runs(self, mapname: str) -> list[list[JumpRunRecord]]:
        """The top few runs on each way, best first."""
        return self._by_way(mapname, limit=TOP_RUNS)

    def _by_way(self, mapname: str, limit: int) -> list[list[JumpRunRecord]]:
        if self._engine is None:
            return []
        with Session(self._engine) as session:
            ways = list(
                session.scalars(
                    select(JumpRunRecord.way_id)
                    .where(JumpRunRecord.mapname == mapname)
                    .distinct()
                    .order_by(JumpRunRecord.way_id.asc())
                )
            )
            out: list[list[JumpRunRecord]] = []
            for way_id in ways:
                rows = list(
                    session.scalars(
                        select(JumpRunRecord)
                        .where(
                            JumpRunRecord.mapname == mapname,
                            JumpRunRecord.way_id == way_id,
                        )
                        .order_by(JumpRunRecord.way_time.asc())
                        .limit(limit)
                    )
                )
                for row in rows:
                    session.expunge(row)
                out.append(rows)
        return out

    def way_names(self, mapname: str) -> dict[int, str]:
        if self._engine is None:
            return {}
        with Session(self._engine) as session:
            return {
                row.way_id: row.way_name
                for row in session.scalars(select(JumpWay).where(JumpWay.mapname == mapname))
            }

    def _way_label(self, mapname: str, way_id: int, names: dict[int, str] | None = None) -> str:
        named = (names if names is not None else self.way_names(mapname)).get(way_id, "")
        return named or str(way_id)

    def _holder_name(self, client_id: int) -> str:
        """Who a record belongs to, by database id.

        A record outlives a session, so this is a database lookup rather than a look at who is on the
        server. The classic asked the admin plugin to resolve `@<id>` and, where the row was gone,
        went to log a warning that raised `KeyError` on a column its query had not selected.
        """
        found = self.console.lookup_clients(f"@{client_id}")
        return found[0].name if found else f"@{client_id}"

    # -- commands ------------------------------------------------------------

    @command("record", level=0)
    def cmd_record(self, ctx: CommandContext) -> None:
        """record [<player>] [<map>] - the best times somebody has set on a map"""
        target, mapname = self._who_and_where(ctx)
        if mapname is None:
            return
        records = self.records_of(target, mapname)
        if not records:
            ctx.reply(
                self.message("jumper_record_none", name=target.name, map=self._display(mapname))
            )
            return
        names = self.way_names(mapname)
        if len(records) > 1:
            ctx.reply(
                self.message("jumper_record_header", name=target.name, map=self._display(mapname))
            )
        for record in records:
            ctx.reply(
                self.message(
                    "jumper_record_line",
                    way=self._way_label(mapname, record.way_id, names),
                    time=format_time(record.way_time),
                    date=format_date(record.time_edit),
                )
            )

    @command("maprecord", level=0)
    def cmd_maprecord(self, ctx: CommandContext) -> None:
        """maprecord [<map>] - the best run on each way of a map"""
        mapname = self._where(ctx, ctx.args.strip())
        if mapname is None:
            return
        records = self.map_records(mapname)
        if not records:
            ctx.reply(self.message("jumper_map_record_none", map=self._display(mapname)))
            return
        names = self.way_names(mapname)
        if len(records) > 1:
            ctx.reply(self.message("jumper_map_record_header", map=self._display(mapname)))
        for record in records:
            ctx.reply(
                self.message(
                    "jumper_map_record_line",
                    way=self._way_label(mapname, record.way_id, names),
                    name=self._holder_name(record.client_id),
                    time=format_time(record.way_time),
                )
            )

    @command("topruns", level=0)
    def cmd_topruns(self, ctx: CommandContext) -> None:
        """topruns [<map>] - the top three runs on each way of a map"""
        mapname = self._where(ctx, ctx.args.strip())
        if mapname is None:
            return
        ways = self.top_runs(mapname)
        if not ways:
            ctx.reply(self.message("jumper_map_record_none", map=self._display(mapname)))
            return
        names = self.way_names(mapname)
        ctx.reply(self.message("jumper_top_header", map=self._display(mapname)))
        for rows in ways:
            for place, record in enumerate(rows, start=1):
                ctx.reply(
                    self.message(
                        "jumper_top_line",
                        way=self._way_label(mapname, record.way_id, names),
                        place=place,
                        name=self._holder_name(record.client_id),
                        time=format_time(record.way_time),
                    )
                )

    @command("delrecord", level=0)
    def cmd_delrecord(self, ctx: CommandContext) -> None:
        """delrecord [<player>] [<map>] - remove somebody's records on a map"""
        target, mapname = self._who_and_where(ctx)
        if mapname is None:
            return
        if target is not ctx.client and not self._may_delete(ctx, target):
            ctx.reply(self.message("jumper_delete_denied", name=target.name))
            return
        records = self.records_of(target, mapname)
        if not records:
            ctx.reply(
                self.message("jumper_record_none", name=target.name, map=self._display(mapname))
            )
            return
        for record in records:
            if record.demo:
                self._unlink(record.demo)
        if self._engine is not None and target.id is not None:
            with Session(self._engine) as session:
                session.execute(
                    delete(JumpRunRecord).where(
                        JumpRunRecord.client_id == target.id,
                        JumpRunRecord.mapname == mapname,
                    )
                )
                session.commit()
        log.info(
            "jumper: %s removed %d record(s) for %s on %s",
            ctx.client.name,
            len(records),
            target.name,
            mapname,
        )
        ctx.reply(
            self.message(
                "jumper_deleted",
                count=len(records),
                name=target.name,
                map=self._display(mapname),
            )
        )

    def _may_delete(self, ctx: CommandContext, target: Client) -> bool:
        needed = level_for(self.settings.get("min_level_delete"))
        if needed is None:
            log.warning(
                "jumper: min_level_delete is %r, which is neither a group keyword nor a level "
                "0-100; using senioradmin",
                self.settings.get("min_level_delete"),
            )
            needed = 80
        return ctx.client.max_level() >= needed and ctx.client.max_level() >= target.max_level()

    @command("setway", level=80)
    def cmd_setway(self, ctx: CommandContext) -> None:
        """setway <way> <name> - give a way on this map a name instead of a number"""
        match = SETWAY_RE.match(ctx.args.strip())
        if match is None:
            # The classic's usage line named `!help jmpsetway`, a command from an older release.
            ctx.reply(self.message("jumper_setway_usage"))
            return
        way_id = int(match["way_id"])
        way_name = match["way_name"].strip()
        mapname = self._mapname()
        if self._engine is None:
            return
        with Session(self._engine) as session:
            existing = session.scalars(
                select(JumpWay).where(JumpWay.mapname == mapname, JumpWay.way_id == way_id)
            ).first()
            if existing is None:
                # Through the ORM, because this name is a *player's* typing: the classic pasted it
                # into `INSERT INTO jumpways VALUES (NULL, '%s', '%d', '%s')`.
                session.add(JumpWay(mapname=mapname, way_id=way_id, way_name=way_name))
            else:
                existing.way_name = way_name
            session.commit()
        ctx.reply(
            self.message("jumper_setway", way=way_id, map=self._display(mapname), name=way_name)
        )

    @command("mapinfo", level=0)
    def cmd_mapinfo(self, ctx: CommandContext) -> None:
        """mapinfo [<map>] - who built a map, when, and how hard it is"""
        if not self.catalogue:
            ctx.reply(self.message("jumper_info_unavailable"))
            return
        wanted = ctx.args.strip().lower() or self._mapname().lower()
        entry = self.catalogue.get(wanted)
        if entry is None:
            found = [name for name in self.catalogue if wanted in name]
            if len(found) != 1:
                listed = ", ".join(sorted(found)[:5]) or "nothing like it"
                ctx.reply(self.message("jumper_map_unknown", map=wanted, maps=listed))
                return
            entry = self.catalogue[found[0]]
            wanted = found[0]
        name = str(entry.get("nom") or wanted)
        author = str(entry.get("mapper") or "")
        ctx.reply(
            self.message("jumper_info_author", map=name, author=author)
            if author
            else self.message("jumper_info_author_unknown", map=name)
        )
        released = parse_release_date(str(entry.get("mdate") or ""))
        if released:
            ctx.reply(self.message("jumper_info_released", date=format_date(released)))
        ways = as_int(entry.get("nway"), 0)
        jumps = as_int(entry.get("njump"), 0)
        if ways or jumps:
            ctx.reply(self.message("jumper_info_ways", ways=ways, jumps=jumps))
        level = as_int(entry.get("level"), 0)
        if level > 0:
            ctx.reply(self.message("jumper_info_level", level=level))

    # -- who and where a command is about ------------------------------------

    def _who_and_where(self, ctx: CommandContext) -> tuple[Client, str | None]:
        """`[<player>] [<map>]`, the shape three of these commands take."""
        parts = ctx.args.split(None, 1)
        if not parts:
            return ctx.client, self._mapname()
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return ctx.client, None
        if len(parts) < 2:
            return target, self._mapname()
        return target, self._where(ctx, parts[1])

    def _where(self, ctx: CommandContext, wanted: str) -> str | None:
        """A map name an admin typed, matched against the rotation. None once it has been refused."""
        if not wanted:
            return self._mapname()
        maps = self.console.get_maps()
        if not maps:
            return wanted.lower()
        found = match_names(wanted, [(m, self._display(m)) for m in maps])
        if len(found) != 1:
            listed = ", ".join(self._display(m) for m in (found or maps)[:5])
            ctx.reply(self.message("jumper_map_unknown", map=wanted, maps=listed))
            return None
        return found[0].lower()

    def _mapname(self) -> str:
        return (self.console.game.map_name or "").lower()

    def _display(self, mapname: str) -> str:
        return self.console.map_display(mapname)

    # -- events on the map ---------------------------------------------------

    def on_map_start(self, event: Event) -> None:
        """A new map: every run in progress is over, and a stock map gets cycled off."""
        for run in list(self.runs):
            self._cancel(run)
        mapname = self._mapname()
        data = event.data if isinstance(event.data, dict) else {}
        if isinstance(data.get("mapname"), str):
            mapname = str(data["mapname"]).lower()
        if self._cycle_if_standard(mapname):
            return
        self._cycles = 0
        self._welcome_map = mapname
        self._welcome_at = self.console.clock.now() + WELCOME_SECONDS

    def _cycle_if_standard(self, mapname: str) -> bool:
        """Move off a map Urban Terror ships. True when the server was asked to cycle.

        The classic applied its endless-loop limit on a map change but not when the plugin was
        enabled, which is the one moment somebody is watching the log.
        """
        if not self.settings.get("skip_standard_maps") or mapname not in STANDARD_MAPS:
            return False
        if self._cycles >= MAX_CYCLES:
            log.warning(
                "jumper: %s is a stock map, but %d have been cycled past in a row already, so it "
                "will be played",
                mapname,
                self._cycles,
            )
            return False
        self._cycles += 1
        log.info("jumper: %s is a stock map; cycling", mapname)
        self.console.apply_server_verb("cyclemap")
        return True

    def on_enable(self) -> None:
        self._cycle_if_standard(self._mapname())

    def on_team_change(self, event: Event) -> None:
        """Going to the spectators abandons a run, which is what the classic did too."""
        client = event.client
        if client is None or (client.team or "").strip().lower() != "spec":
            return
        run = self.run_of(client)
        if run is not None:
            self._cancel(run)

    def on_disconnect(self, event: Event) -> None:
        client = event.client
        if client is None:
            return
        run = self.run_of(client)
        if run is not None:
            # No `stop` verb: the player is gone and the server has stopped recording them itself.
            self.runs.remove(run)
            self._unlink_demo(run)

    # -- the map catalogue ---------------------------------------------------

    async def _refresh_catalogue(self) -> None:
        """Fetch the map catalogue, off the bot's thread.

        The classic called `requests.get` **on the bot's own thread** at every map change and inside
        `!mapinfo`, so a slow or dead endpoint stopped the bot answering for as long as it took.
        """
        url = str(self.settings.get("catalogue_url") or "").strip()
        if not url:
            return
        catalogue = await asyncio.to_thread(self._download_catalogue, url)
        if catalogue:
            self.catalogue = catalogue
            log.info("jumper: the map catalogue holds %d map(s)", len(catalogue))

    def _download_catalogue(self, url: str) -> dict[str, dict[str, Any]]:
        """Blocking, and called through a worker thread. Empty on any failure."""
        import json
        from urllib.error import URLError
        from urllib.request import Request, urlopen

        try:
            request = Request(url, headers={"User-Agent": "b3ng"})  # noqa: S310 - operator's url
            with urlopen(request, timeout=CATALOGUE_TIMEOUT) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (URLError, OSError, ValueError) as exc:
            log.warning("jumper: could not read the map catalogue from %s (%s)", url, exc)
            return {}
        if not isinstance(payload, list):
            log.warning("jumper: the map catalogue at %s is not a list of maps", url)
            return {}
        catalogue: dict[str, dict[str, Any]] = {}
        for entry in payload:
            if isinstance(entry, dict) and entry.get("pk3"):
                catalogue[str(entry["pk3"]).lower()] = entry
        return catalogue

    # -- the one scheduled pass ----------------------------------------------

    def _run_pending(self) -> None:
        """The greeting a new map owes its players. The classic's `threading.Timer(30)`."""
        if not self._welcome_at or self.console.clock.now() < self._welcome_at:
            return
        mapname, self._welcome_map = self._welcome_map, ""
        self._welcome_at = 0.0
        self.console.say(self.message("jumper_welcome", map=self._display(mapname)))
        entry = self.catalogue.get(mapname.lower())
        if entry is None:
            return
        author = str(entry.get("mapper") or "")
        released = parse_release_date(str(entry.get("mdate") or ""))
        if author and released:
            self.console.say(
                self.message(
                    "jumper_welcome_author",
                    map=self._display(mapname),
                    author=author,
                    date=format_date(released),
                )
            )

    # -- demo files ----------------------------------------------------------

    def _unlink(self, demo: str) -> bool:
        """Delete a demo the game server wrote. False when it could not be found or removed.

        The file belongs to the *game server*, so this only works where the bot runs on the same
        machine — which the classic assumed silently. Said once here, because it is a fact about the
        installation and not about this run.
        """
        path = self._demo_path(demo)
        if path is None:
            if not self._warned_about_demo_paths:
                self._warned_about_demo_paths = True
                log.warning(
                    "jumper: cannot find %s on this machine, so jump-run demos will not be deleted "
                    "— the game server writes them, and this bot is either elsewhere or cannot read "
                    "fs_basepath/fs_homepath",
                    demo,
                )
            return False
        try:
            path.unlink()
        except OSError as exc:
            log.warning("jumper: could not remove %s (%s)", path, exc)
            return False
        log.debug("jumper: removed %s", path)
        return True

    def _demo_path(self, demo: str) -> Path | None:
        """Where a demo the server named might be on this machine, if anywhere.

        `fs_basepath` and `fs_homepath` are the two places a Quake 3 server writes, and `fs_game` is
        the mod directory inside them. Read as cvars where the bot has not already learned them.
        """
        game = self._cvar("fs_game")
        for root in (self._cvar("fs_basepath"), self._cvar("fs_homepath")):
            if not root:
                continue
            candidate = Path(os.path.normpath(os.path.join(root, game, demo)))
            if candidate.is_file():
                return candidate
        return None

    def _cvar(self, name: str) -> str:
        """A cvar, from what the bot already knows or by asking. "" when the server will not say.

        Read where it is needed rather than at startup: a cvar read in `on_startup` that the server
        does not answer is what took the classic's `poweradminurt` down with all forty-nine of its
        commands.
        """
        known = self.console.game.cvars.get(name)
        if known:
            return str(known).rstrip("/")
        value = self.console.get_cvar(name)
        return str(value).rstrip("/") if value else ""


__all__ = [
    "CATALOGUE_HOURS",
    "CATALOGUE_TIMEOUT",
    "DEFAULTS",
    "MAX_CYCLES",
    "MESSAGES",
    "STANDARD_MAPS",
    "TOP_RUNS",
    "WELCOME_SECONDS",
    "Best",
    "JumpRunRecord",
    "JumpWay",
    "JumperPlugin",
    "Run",
    "format_date",
    "format_time",
    "parse_release_date",
]
