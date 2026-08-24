"""Decides who may call a vote, and keeps a record of the ones that were called.

A port of the classic `callvote` plugin. A vote is the one power an ordinary player has over the whole
server, which is why it gets abused: `callvote kick` on whoever is winning, `callvote map` to whatever
empties the server. So each vote type gets a level, a vote from somebody below it is cancelled with a
word to the player, and every finished vote is written down — `!lastvote` is what an admin reads when
they arrive to "the map changed and nobody knows why".

Kept from the classic: per-type levels, the special map list (a level per map, for the maps that
empty a server), announcing the next map when a `cyclemap`/`g_nextmap` vote starts, `!veto` and
`!lastvote`, and doing nothing at all when too few players are present to vote — that last one is not
laziness, it is that a vote with one voter passes before the log line reporting it has been read.

**The `protect:` section is the vote protector from `poweradminhf`**, and it is here rather than in a
per-game plugin for the same reason `censorurt` became a `mute:` section on `censor`: it is a policy
about votes, and this is the plugin that already holds the running vote, the level table and the veto.
What it does is stop the oldest abuse on any server with votes — `callvote kick` on the admin who just
warned you. Where the engine can cancel a vote (Urban Terror) it is cancelled outright, which the
Homefront plugin could not do; where it cannot, the caller is warned and a **ban** that passes against
a protected player is lifted again. It is off by default: a plugin that starts punishing players for
votes an operator has allowed for years, because they upgraded, is exactly the kind of surprise this
project exists to avoid — `examples/plugin_callvote.yaml` says what to set for what the classic did.

Changed, and the first three are faults:

* **An unknown vote type no longer kills the handler.** `lv = self.callvoteminlevel[tp]` sat *outside*
  the `try` that was meant to catch it, so a vote type absent from the classic's fixed table of thirty
  raised `KeyError` before anything was checked: no level enforced, no record kept, nothing said. Any
  vote type a newer build, a mod, or an operator-extended votable cvar list allows did it, every time.
  A type nobody has named has `default_level` here, and the level table is policy rather than a
  vocabulary the plugin has to know in advance.
* **A vote's arguments no longer reach SQL.** The record was written with
  `INSERT INTO callvote VALUES (NULL, '%s', ...)` and the vote's arguments pasted in — a string a
  *player* chose, since `callvote map <anything>` is how it gets there. This owns a table through the
  ORM and there is no SQL in the plugin at all. (The same statement stored the literal text `None`
  for a vote with no arguments, because `'%s' % None` is `'None'`.)
* **A finished vote is never vetoed.** When the result line did not match the vote it was holding, the
  classic called `veto()` — a cancellation aimed at a vote that had already ended, and it dropped the
  record on the way past. The mismatch was also case-sensitive against a type it had lower-cased when
  storing, so a title reporting `Map` at the start and `map` at the end would have vetoed every vote
  it saw. Comparison is normalised here, and a mismatch is logged and recorded, not vetoed.
* **It is no longer Urban-Terror-only.** The classic declared `requiresParsers = ['iourt42',
  'iourt43']`, yet `CLIENT_CALLVOTE` is in the shared vocabulary and three families here report one
  (Urban Terror, Homefront, Altitude). What is genuinely UrT-specific is *cancelling* a vote, so that
  is a profile field (`veto_command`) the plugin asks about: where the engine has no verb, the history
  and the announcements still work and the level policy is disabled loudly at startup — because
  telling a player they may not call a vote which then passes anyway is worse than saying nothing.
* **`!lastvote` answers about this server.** The classic read the newest row in the table, so
  operators running several bots against one database were shown another server's vote.
* **Bots do not count as voters.** The "enough players to bother checking" count included AI, which
  inflates it on exactly the servers where it is wrong: one human among seven bots had their votes
  policed, and the veto arrived after the vote had passed.
* **A required level above every group is reported as a number** rather than raising. The classic
  looked for the lowest group at or above the level and called `.name` on the result, which is None
  when the config says `map: 101`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Integer, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.client import Client
from b3.domain.permissions import DEFAULT_GROUPS, level_for

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

#: Vote types whose argument is a map, and so the ones the special map list applies to. Both
#: spellings exist because Urban Terror has both: `map` loads it now, `g_nextmap` sets what follows.
MAP_VOTE_TYPES = ("map", "g_nextmap", "nextmap")

#: Vote types after which the next map is worth announcing — the question everybody asks when a
#: cyclemap vote appears is "to what?".
NEXT_MAP_VOTE_TYPES = ("cyclemap", "g_nextmap", "nextmap")

#: Vote types whose argument is a *player*, which is what the `protect:` section is about. Both
#: spellings of the Quake 3 one are here (`kick <name>` and `clientkick <slot>`) along with the two
#: Homefront reports.
PLAYER_VOTE_TYPES = ("kick", "clientkick", "ban", "kickban", "tempban", "mute")

#: Of those, the ones that leave something behind to undo. A kick cannot be taken back — the player
#: is already gone and can reconnect — so a passed kick vote is warned about and no more.
BAN_VOTE_TYPES = ("ban", "kickban", "tempban")

DEFAULTS: dict[str, object] = {
    # Level for `!veto` and `!lastvote`. The classic's default is mod for both.
    "min_level": 20,
    # Level required for a vote type nobody has named in `levels`. 0 — anybody — because that is what
    # the classic's config shipped for all thirty of its types, and because the alternative is a
    # plugin that silently forbids a vote the operator never thought about.
    "default_level": 0,
    # Below this many human voters nothing is checked and nothing is cancelled. Two, because with one
    # voter the vote has already passed by the time its log line is read, and a veto that arrives
    # after the map has changed only looks like the bot is broken.
    "min_voters": 2,
    # Announce the next map when a cyclemap/nextmap vote starts.
    "announce_next_map": True,
    # Write finished votes down. Off means `!lastvote` has nothing to answer with.
    "record": True,
}

PROTECT_DEFAULTS: dict[str, object] = {
    # Level at and above which a player may not be vote-kicked or vote-banned by somebody who does
    # not outrank them. **0 turns the whole section off, and that is the default** — the classic's
    # `poweradminhf` shipped this at `mod` on Homefront, but that plugin ran on one title and this one
    # runs on every family that reports a vote. Turning it on for everybody at an upgrade would start
    # punishing players for votes their operator has been allowing for years.
    "level": 0,
    # Warn whoever called the vote. The classic's own penalty, and its own two days: long enough that
    # a second attempt reaches the admin plugin's escalation ladder.
    "warn": True,
    "warn_minutes": 2880,
    # Lift a ban that passed against a protected player. Nothing to undo for a kick.
    "undo": True,
}

MESSAGES = {
    "callvote_denied": "you may not call a {type} vote — that needs {group}",
    "callvote_next_map": "next map: {map}",
    "callvote_vetoed": "the vote was cancelled",
    "callvote_veto_impossible": "this game has no way to cancel a vote",
    "callvote_none_running": "there is no vote running",
    "callvote_last": "last vote: {type} {args} by {name}, {ago} ago",
    "callvote_last_result": "it {outcome} {yes}:{no} with {voters} players on the server",
    "callvote_last_result_unknown": "it {outcome}; this game does not report the counts",
    "callvote_last_none": "no vote has been recorded on this server yet",
    "callvote_protected": "{name} is not somebody you may call a {type} vote against",
    "callvote_protected_undone": "the vote against {name} has been undone",
}


class Base(DeclarativeBase):
    """This plugin's own table. The core's schema is Alembic-managed and is not touched here."""


class CallvoteRecord(Base):
    """One finished vote.

    ``name`` is stored rather than joined from `clients`, for two reasons: that table belongs to the
    core's migrations and a plugin has no business reading it directly, and the name somebody was
    using when they called a vote is a fact about the vote — renaming later should not rewrite it.

    ``yes``/``no`` are nullable because not every engine states them. Homefront reports the yes count
    and a percentage and no more; a column of zeroes there would read as "nobody voted against", which
    is a different claim from "nobody counted".
    """

    __tablename__ = "callvote_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    #: Which game server this vote happened on, from `bot.server_id`. "" when unstated, as it is for
    #: an operator running one bot.
    server_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    client_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    type: Mapped[str] = mapped_column(String(32), default="")
    args: Mapped[str] = mapped_column(String(128), default="")
    voters: Mapped[int] = mapped_column(Integer, default=0)
    yes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    time_add: Mapped[int] = mapped_column(Integer, default=0, index=True)


@dataclass(slots=True)
class Callvote:
    """The vote currently running, as the bot understood it when it started."""

    client: Client
    type: str
    args: str
    started: float
    #: Humans who could vote when it started — the figure `min_voters` is compared against.
    voters: int
    #: Who the vote is *about*, where it is about somebody. Homefront names them in the vote line;
    #: on the Quake 3 engines the vote's own argument is the name or the slot, so it is resolved.
    target: Client | None = None
    #: Whether `protect:` judged this vote one that should not have been called. Kept on the vote
    #: rather than recomputed at the end, because by then the target may have been banned and gone.
    protected: bool = False


def parse_vote(event: Event) -> tuple[str, str]:
    """The vote type and its arguments, out of whichever shape the engine reports.

    Three families announce a vote and none of them agree on the payload, so this is read from the
    events themselves rather than from one engine's habit: Urban Terror puts the whole line in
    ``data`` (``"map ut4_casa"``), Altitude puts the joined arguments there and the list in
    ``extra["arguments"]``, and Homefront puts the *type* in ``data`` with the type and its target
    together in ``extra["arguments"]``. Splitting ``data`` handles the first two; the third needs the
    extra, or a `kick` vote loses the name of the player it is about.
    """
    text = str(event.data or "").strip()
    head, _, tail = text.partition(" ")
    vote_type = head.strip().lower()
    args = tail.strip()
    arguments = event.extra.get("arguments")
    if not args and isinstance(arguments, list) and arguments:
        rest = [str(a) for a in arguments]
        if rest and rest[0].strip().lower() == vote_type:
            rest = rest[1:]
        args = " ".join(part for part in rest if part).strip()
    return vote_type, args


def group_name_for(level: int) -> str:
    """What to call the level a vote needs: the lowest group that reaches it, else the number.

    The classic searched for that group and used `.name` on the result without checking there was
    one, so a config line like `map: 101` answered a player's denial with an `AttributeError`.
    """
    reaching = [group for group in DEFAULT_GROUPS if group.level >= level]
    if not reaching:
        return f"level {level}"
    return min(reaching, key=lambda group: group.level).name


class CallvotePlugin(Plugin):
    """Polices who may call a vote, and records the ones that finish."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.protect_settings: dict[str, object] = dict(PROTECT_DEFAULTS)
        #: Vote type -> the level needed to call it.
        self.levels: dict[str, int] = {}
        #: Map name -> the level needed to vote for it, whatever the level for `map` itself is.
        self.map_levels: dict[str, int] = {}
        self.current: Callvote | None = None
        self._engine: Engine | None = None
        self._can_veto = True

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.protect_settings = {**PROTECT_DEFAULTS, **(config.get("protect") or {})}
        level = level_for(self.protect_settings.get("level"))
        if level is None:
            log.error(
                "callvote: protect.level is %r, which is neither a group keyword nor a level 0-100; "
                "nobody is protected from a vote",
                self.protect_settings.get("level"),
            )
            level = 0
        self.protect_settings["level"] = level
        self.levels = self._level_table(config.get("levels"), "levels")
        self.map_levels = self._level_table(config.get("maps"), "maps")
        for name in ("veto", "lastvote"):
            registered = self.console.command_registry.get(name)
            if registered is not None:
                registered.min_level = as_int(self.settings.get("min_level"), 20)

    def _level_table(self, section: object, where: str) -> dict[str, int]:
        """Read a `name: group-or-number` mapping, refusing entries rather than guessing at them."""
        if not isinstance(section, dict):
            return {}
        table: dict[str, int] = {}
        for key, value in section.items():
            level = level_for(value)
            if level is None:
                log.error(
                    "callvote: %s.%s is %r, which is neither a group keyword nor a level 0-100; "
                    "that entry is ignored",
                    where,
                    key,
                    value,
                )
                continue
            table[str(key).strip().lower()] = level
        return table

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        if self.settings.get("record"):
            self._engine = self._open_storage()
        self.subscribe(EventType.CLIENT_CALLVOTE, self.on_callvote)
        self.subscribe(EventType.VOTE_PASSED, self.on_vote_finished)
        self.subscribe(EventType.VOTE_FAILED, self.on_vote_finished)
        # Asked once, at startup, because it decides whether half this plugin can work at all — and
        # an operator who has configured levels on an engine that cannot enforce them should be told
        # so now rather than wondering later why a guest keeps changing the map.
        self._can_veto = self.console.can_cancel_vote()
        if not self._can_veto:
            log.warning(
                "callvote: this game has no verb for cancelling a vote, so who may call one cannot "
                "be enforced. Votes are still announced and recorded, and `!veto` will say it "
                "cannot help"
            )

    def _open_storage(self) -> Engine | None:
        try:
            engine = self.storage_engine()
        except RuntimeError as exc:
            log.warning("callvote: %s; no vote can be recorded", exc)
            return None
        try:
            Base.metadata.create_all(engine)  # type: ignore[arg-type]
        except Exception as exc:  # noqa: BLE001 - a plugin must not stop the bot over its own table
            log.error("callvote: could not create its table (%s)", exc)
            return None
        return engine  # type: ignore[return-value]

    # -- a vote starting -----------------------------------------------------

    def on_callvote(self, event: Event) -> None:
        client = event.client
        if client is None:
            return
        vote_type, args = parse_vote(event)
        if not vote_type:
            log.warning("callvote: could not read a vote type out of %r", event.data)
            return
        voters = self.voters()
        self.current = Callvote(
            client=client,
            type=vote_type,
            args=args,
            started=self.console.clock.now(),
            voters=voters,
            target=self.vote_target(event, vote_type, args),
        )
        if self.protect(self.current):
            return
        if voters < as_int(self.settings.get("min_voters"), 2):
            # The classic's reasoning, and it is sound: with one voter the server has already acted
            # on the vote by the time its line reaches us, so a cancellation would arrive after the
            # map had changed and read as the bot misbehaving.
            log.debug("callvote: %s vote not checked — only %d voter(s)", vote_type, voters)
            return
        if self.refuse(client, vote_type, args):
            return
        if self.settings.get("announce_next_map") and vote_type in NEXT_MAP_VOTE_TYPES:
            following = self.console.get_next_map()
            if following:
                self.console.say(self.message("callvote_next_map", map=following))

    def vote_target(self, event: Event, vote_type: str, args: str) -> Client | None:
        """Who a vote is about, where it is about somebody.

        Homefront resolves the player itself and puts them on the event. The Quake 3 engines put the
        argument in the vote line — `clientkick 3`, `kick bob` — so it is looked up here, and an
        argument matching more than one player resolves to nobody: acting on a guess is worse than
        not acting, and the level policy above still applies either way.
        """
        target = event.target
        if isinstance(target, Client):
            return target
        if vote_type not in PLAYER_VOTE_TYPES or not args:
            return None
        found = self.console.find_clients(args.split()[0])
        return found[0] if len(found) == 1 else None

    def protect(self, vote: Callvote) -> bool:
        """Deal with a vote called against somebody who should not be voted against.

        True when the vote was cancelled outright, which is the best answer and needs an engine that
        can cancel one. Where it cannot — Homefront, where this feature comes from — the caller is
        warned now and a **ban** that passes is lifted when it does, which is the classic's own
        two-part answer to a problem it could not prevent.

        The rule is the classic's: the target has to be at or above `protect.level` **and** outrank
        the caller. An admin vote-kicking another admin of the same rank is a disagreement between
        two people who both have `!kick`, and not this plugin's business.
        """
        level = as_int(self.protect_settings.get("level"), 0)
        target = vote.target
        if not level or target is None or vote.type not in PLAYER_VOTE_TYPES:
            return False
        if target.max_level() < level or target.max_level() <= vote.client.max_level():
            return False
        vote.protected = True
        log.info(
            "callvote: %s called a %s vote against %s, who is protected (level %d)",
            vote.client.name,
            vote.type,
            target.name,
            target.max_level(),
        )
        self.console.tell(
            vote.client, self.message("callvote_protected", name=target.name, type=vote.type)
        )
        if self.protect_settings.get("warn"):
            self.console.warn(
                vote.client,
                reason=self.message("callvote_protected", name=target.name, type=vote.type),
                minutes=as_int(self.protect_settings.get("warn_minutes"), 2880),
            )
        if not self._can_veto:
            return False
        self.console.cancel_vote()
        self.current = None
        return True

    def undo_protected(self, vote: Callvote) -> None:
        """Lift a ban a protected player was voted into. A kick leaves nothing to undo."""
        target = vote.target
        if target is None or not self.protect_settings.get("undo"):
            return
        if vote.type not in BAN_VOTE_TYPES:
            log.info(
                "callvote: %s's %s vote against %s passed; a kick cannot be undone",
                vote.client.name,
                vote.type,
                target.name,
            )
            return
        log.warning(
            "callvote: lifting the ban %s was voted into by %s", target.name, vote.client.name
        )
        self.console.unban(
            target, reason=self.message("callvote_protected_undone", name=target.name)
        )
        self.console.say(self.message("callvote_protected_undone", name=target.name))

    def refuse(self, client: Client, vote_type: str, args: str) -> bool:
        """Cancel this vote if the player is not allowed it. True when it was refused."""
        needed = self.level_needed(vote_type, args)
        if client.max_level() >= needed:
            return False
        if not self._can_veto:
            # Nothing to enforce with. Said at info because it is the operator's configuration
            # meeting the engine's limits, not a fault, and the startup warning already explained it.
            log.info(
                "callvote: %s may not call a %s vote (needs %d) but this game cannot cancel one",
                client.name,
                vote_type,
                needed,
            )
            return False
        log.info(
            "callvote: cancelling %s's %s vote — level %d, needs %d",
            client.name,
            vote_type,
            client.max_level(),
            needed,
        )
        self.console.cancel_vote()
        self.current = None
        self.console.tell(
            client,
            self.message("callvote_denied", type=vote_type, group=group_name_for(needed)),
        )
        return True

    def level_needed(self, vote_type: str, args: str) -> int:
        """The level this exact vote needs.

        A map named in the special list replaces the level for the vote type rather than raising it,
        which is the classic's rule and the more useful one: an operator can both lock `map` down to
        admins and let everybody vote for the two maps that always fill the server.
        """
        if vote_type in MAP_VOTE_TYPES:
            named = self.map_levels.get(args.strip().lower())
            if named is not None:
                return named
        return self.levels.get(vote_type, as_int(self.settings.get("default_level"), 0))

    def voters(self) -> int:
        """Humans who could vote. Bots cannot, and counting them is how one player becomes eight."""
        return len(
            [
                client
                for client in self.console.clients.connected()
                if not client.is_bot and (client.team or "") != "spec"
            ]
        )

    # -- a vote finishing ----------------------------------------------------

    def on_vote_finished(self, event: Event) -> None:
        vote = self.current
        self.current = None
        if vote is None:
            log.debug("callvote: a vote finished that this plugin never saw start")
            return
        yes, no = self._counts(event)
        finished_type, finished_args = self._finished_vote(event)
        if finished_type and (finished_type != vote.type or finished_args != vote.args):
            # Logged, not vetoed. The classic cancelled the vote here, which cannot work — the vote
            # has finished — and threw away the record of what actually happened.
            log.warning(
                "callvote: the vote that finished (%s %s) is not the one that started (%s %s); "
                "recording what finished",
                finished_type,
                finished_args,
                vote.type,
                vote.args,
            )
            vote = Callvote(
                client=vote.client,
                type=finished_type,
                args=finished_args,
                started=vote.started,
                voters=vote.voters,
            )
        passed = event.type is EventType.VOTE_PASSED
        if passed and vote.protected:
            self.undo_protected(vote)
        self.record(vote, yes=yes, no=no, passed=passed)

    @staticmethod
    def _finished_vote(event: Event) -> tuple[str, str]:
        """The type and arguments named in a result line, normalised, or empty when it names none.

        Urban Terror repeats the vote in `data["what"]`; Homefront's result line says only whether it
        passed, so there is nothing to compare and nothing is claimed.
        """
        data = event.data
        what = data.get("what") if isinstance(data, dict) else None
        if not what:
            return "", ""
        head, _, tail = str(what).strip().partition(" ")
        return head.strip().lower(), tail.strip()

    @staticmethod
    def _counts(event: Event) -> tuple[int | None, int | None]:
        """Votes for and against, where the engine states them.

        None rather than 0 for a figure that was not reported: Homefront gives a yes count and a
        percentage, and writing 0 into the other column would state that nobody voted against.
        """
        data = event.data
        yes: Any = None
        no: Any = None
        if isinstance(data, dict):
            yes, no = data.get("yes"), data.get("no")
        if yes is None:
            yes = event.extra.get("yes_votes")
        if no is None:
            no = event.extra.get("no_votes")
        return (
            None if yes is None else as_int(yes, 0),
            None if no is None else as_int(no, 0),
        )

    def record(
        self, vote: Callvote, *, yes: int | None, no: int | None, passed: bool
    ) -> CallvoteRecord | None:
        if self._engine is None or vote.client.id is None:
            return None
        record = CallvoteRecord(
            server_id=self.server_id(),
            client_id=vote.client.id,
            name=vote.client.name,
            type=vote.type,
            args=vote.args,
            # Everybody on the server at the finish, not the voters at the start: a player who
            # joined a team to vote is a voter, and the classic recorded it the same way for the
            # same reason.
            voters=len(self.console.clients.connected()),
            yes=yes,
            no=no,
            passed=passed,
            time_add=int(vote.started),
        )
        with Session(self._engine) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            session.expunge(record)
        return record

    def server_id(self) -> str:
        """Which game server this is, for operators pointing several bots at one database.

        Read off the runtime the way `status` reads its config path — it is not on the `Console`
        port, and inventing a place for it there for one column would be the bigger change.
        """
        config = getattr(self.console, "config", None)
        return str(getattr(getattr(config, "bot", None), "server_id", "") or "")

    def last_vote(self) -> CallvoteRecord | None:
        if self._engine is None:
            return None
        with Session(self._engine) as session:
            return session.scalars(
                select(CallvoteRecord)
                .where(CallvoteRecord.server_id == self.server_id())
                .order_by(CallvoteRecord.time_add.desc(), CallvoteRecord.id.desc())
                .limit(1)
            ).first()

    # -- commands ------------------------------------------------------------

    @command("veto", level=20)
    def cmd_veto(self, ctx: CommandContext) -> None:
        """veto - cancel the vote that is running"""
        if not self._can_veto:
            ctx.reply(self.message("callvote_veto_impossible"))
            return
        if self.current is None:
            # The classic sent the verb regardless and said nothing either way, so an admin could
            # not tell "cancelled" from "there was nothing to cancel" from "this game cannot".
            ctx.reply(self.message("callvote_none_running"))
            return
        self.current = None
        self.console.cancel_vote()
        ctx.reply(self.message("callvote_vetoed"))

    @command("lastvote", level=20)
    def cmd_lastvote(self, ctx: CommandContext) -> None:
        """lastvote - what the last vote on this server was, and how it went"""
        record = self.last_vote()
        if record is None:
            ctx.reply(self.message("callvote_last_none"))
            return
        ago = int(max(0.0, self.console.clock.now() - record.time_add))
        ctx.reply(
            self.message(
                "callvote_last",
                type=record.type,
                args=record.args or "",
                name=record.name,
                ago=format_duration(ago),
            )
        )
        outcome = "passed" if record.passed else "failed"
        if record.yes is None and record.no is None:
            ctx.reply(self.message("callvote_last_result_unknown", outcome=outcome))
            return
        ctx.reply(
            self.message(
                "callvote_last_result",
                outcome=outcome,
                yes="?" if record.yes is None else record.yes,
                no="?" if record.no is None else record.no,
                voters=record.voters,
            )
        )


def format_duration(seconds: int) -> str:
    """ "45 seconds", "3 minutes", "2 hours" — the classic's shape, with its arithmetic fixed.

    It divided integers under Python 2, so `round(119 / 60)` was 1 and two minutes minus a second
    read as "1 minute"; and its plural test was `> 1`, so nought of anything was "0 second".
    """
    if seconds < 60:
        return f"{seconds} second{'' if seconds == 1 else 's'}"
    if seconds < 3600:
        minutes = round(seconds / 60)
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = round(seconds / 3600)
    return f"{hours} hour{'' if hours == 1 else 's'}"


__all__ = [
    "BAN_VOTE_TYPES",
    "DEFAULTS",
    "MAP_VOTE_TYPES",
    "MESSAGES",
    "NEXT_MAP_VOTE_TYPES",
    "PLAYER_VOTE_TYPES",
    "PROTECT_DEFAULTS",
    "Callvote",
    "CallvotePlugin",
    "CallvoteRecord",
    "format_duration",
    "group_name_for",
    "parse_vote",
]
