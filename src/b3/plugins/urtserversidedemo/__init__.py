"""Urban Terror's server-side demos — recording a player from the server, not from their machine.

A port of the classic `urtserversidedemo`. A 4.2 server can record a demo of one player itself, which
is the only kind of evidence an admin can collect about somebody they suspect: a client-side demo
needs the player's cooperation. `!startserverdemo <player>` starts one, `!startserverdemo all`
records everybody and keeps recording whoever arrives next, and `!stopserverdemo` does the reverse.

**The reply is the point.** `startserverdemo` answers with the filename it has begun writing, and
without reading that nothing can say whether recording started, nor find the file again — which is
what `jumper` needs when it throws away the demo of a run that set no record. That is why this plugin
exists as a service as well as two commands: `record(client)` and `stop(client)` are what another
plugin calls, and they return the filename or `None`.

The engine has four answers and each means something different: *recording X to <file>* (started),
*X is already being recorded* (nothing to do), *X is not active* (they are connected but not on a
team yet, so try again when they join), and *No player …* (a slot with nobody in it).

Changed from the classic:

* **Nothing on shutdown ever ran.** `onExit` and `onStop` are defined and were **never registered**
  as handlers, so when the bot stopped, every demo the server had been asked for kept recording — and
  `!startserverdemo all` stayed in force on the server with nothing left to stop it. And the method
  they would have called could not have worked: `for guid, stopper in self._auto_stop_timers:`
  iterates a **dict**, which yields its keys, so unpacking a GUID string into two names raises
  `ValueError`. Two faults in one path, each of which hid the other.
* **A reply the author had not seen killed the feature for that player.** `_try_to_start_demo` ended
  in `raise AssertionError("unexpected response: %r")`, inside a thread — so any fifth answer from
  the server (a build without the feature, a name with a newline in it) killed the thread silently,
  left the player in the "waiting to start" table for ever, and told the admin nothing. Every reply
  is reported here, and an unrecognised one is passed to the admin verbatim, since the server's own
  words are more use than "it did not work".
* **An auto-stopping demo could raise instead of stopping.** The timer's callback did
  `del self._auto_stop_timers[guid]` unconditionally, and a player who disconnected first had already
  had that entry deleted — `KeyError` in the timer thread, so the demo was never stopped.
* **No threads and no timers.** The classic ran a `Thread` per demo request whose whole job was to
  wait for the player to join a team, plus a `threading.Timer` per timed demo. Both are deadlines on
  this plugin's one scheduled pass; the retry also happens the moment the player joins, which is
  what the thread was waiting for.
* **`all` means the same thing to both commands.** `!startserverdemo` compared `data == 'all'` — so
  `!startserverdemo ALL` looked for a player called "ALL" — while `!stopserverdemo` compared only the
  first word, so `!stopserverdemo all please` stopped everything. Both read the first word,
  case-insensitively.
* **The verbs are the title's, not this plugin's.** The classic wrote `startserverdemo %s` itself and
  guarded it by asking the server `cmdlist startserverdemo` at startup. They are
  `GameProfile.player_verbs` here (`record_demo`, `record_stop`) and `server_verbs` (`record_all`,
  `record_stop_all`), declared on 4.2 and 4.3 and **not on 4.1**, which predates the feature — so the
  plugin asks the profile rather than the server, and refuses to start with a reason where the title
  has no such verb. A 4.2 build compiled without it answers the first attempt with a complaint, and
  that answer reaches the admin who typed the command.
* **A demo can be given a length.** The classic could only do that from two third-party plugins
  (`haxbusterurt` and `follow`, both external and neither ported), so its `demo_duration` settings
  were unreachable for everybody else. `!startserverdemo <player> [<minutes>]` is the same machinery
  with a way to ask for it.

**Not ported.** The `haxbusterurt` and `follow` sections: both plugins live outside the classic tree,
neither exists here, and their events are not in this bot's vocabulary. What they wanted — "record
this player for a few minutes because something flagged them" — is `record(client, minutes=…)`, which
any plugin can call.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: The engine's four answers, as patterns. Captured from the classic's own tests
#: (`tests/plugins/urtserversidedemo/`), which is where the filename shape comes from: a demo is
#: `dm_68` on 4.2 and `urtdemo` on later builds.
STARTED_RE = re.compile(
    r"^startserverdemo:\s+recording\s+(?P<name>.+?)\s+to\s+(?P<file>.+\.(?:dm_68|urtdemo))\s*$",
    re.IGNORECASE,
)
ALREADY_RE = re.compile(r"is already being recorded\s*$", re.IGNORECASE)
NOT_ACTIVE_RE = re.compile(r"is not active\s*$", re.IGNORECASE)
NO_PLAYER_RE = re.compile(r"^\s*no player", re.IGNORECASE)

#: How long a player who is connected but not yet on a team is waited for before the attempt is given
#: up on. The classic waited for ever — its thread sat on an event until the player joined or left —
#: which is fine for a thread and not fine for a table that has to be cleaned up.
WAIT_SECONDS = 300.0

#: How often a waiting attempt is retried by the scheduled pass. A join is what actually triggers it;
#: this is the backstop for a player who was already on a team when something odd happened.
RETRY_SECONDS = 20.0

DEFAULTS: dict[str, object] = {
    # Minutes a demo runs for when `!startserverdemo <player>` names no length. 0 means "until
    # somebody stops it", which is what the classic's command did and the sane default: a demo an
    # admin started because they are watching somebody should not stop while they are still watching.
    "default_minutes": 0,
}

MESSAGES = {
    "ussd_usage": "!{command} <player|all> [<minutes>]",
    "ussd_started": "recording {name} to {file}",
    "ussd_started_for": "recording {name} to {file}, stopping in {minutes} minute(s)",
    "ussd_already": "{name} is already being recorded",
    "ussd_waiting": "{name} has not joined a team yet - recording will start when they do",
    "ussd_refused": "the server would not record {name}: {reply}",
    "ussd_stopped": "stopped recording {name}",
    "ussd_stop_refused": "the server said: {reply}",
    "ussd_all_started": "recording every player, and everybody who joins",
    "ussd_all_stopped": "stopped recording everybody",
    "ussd_unsupported": "this server has no server-side demo recording",
}


@dataclass(slots=True)
class Waiting:
    """A demo asked for on a player who is not on a team yet.

    The classic's `DemoStarter` thread, as data: it waited on an event for the player to join, which
    is a thread doing nothing but hold a stack.
    """

    client: Client
    admin: Client | None
    minutes: int
    give_up_at: float
    next_try: float


@dataclass(slots=True)
class Recording:
    """A demo the server is writing, and when to tell it to stop."""

    client: Client
    filename: str
    stop_at: float = 0.0
    admin: Client | None = None


@dataclass(slots=True)
class Started:
    """What `record` found out: the filename, or why there is none."""

    filename: str = ""
    already: bool = False
    waiting: bool = False
    reply: str = ""

    def ok(self) -> bool:
        return bool(self.filename)


@dataclass(slots=True)
class Demos:
    """Everything in flight, so that neither table can outlive the players in it."""

    recording: list[Recording] = field(default_factory=list)
    waiting: list[Waiting] = field(default_factory=list)


class UrtserversidedemoPlugin(Plugin):
    """Server-side demo recording, as two commands and a service other plugins call."""

    requires_plugins = ("admin",)
    requires_parsers = ("iourt41", "iourt42", "iourt43")

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.demos = Demos()
        #: Whether every arrival is recorded. Kept across a disable/enable, as the classic did: an
        #: operator who switches the plugin off for a minute has not changed their mind about it.
        self.recording_all = False

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        if not self.console.supports_verb("record_demo"):
            # 4.1 predates server-side demos, so the profile says so and this stops here rather than
            # offering two commands that could only ever answer "unknown command". The classic asked
            # the *server* (`cmdlist startserverdemo`); the title's own profile answers it for free.
            # `mark_disabled` rather than `disable`, because nothing has started: the second would
            # run `on_disable`, which would try to stop demos on a server that cannot record any.
            self.mark_disabled("this title has no server-side demo recording")
            log.warning("urtserversidedemo: %s", self.disabled_reason)
            return
        self.schedule(self._run_pending, second="*", name="UrtserversidedemoPlugin.pending")
        self.subscribe(EventType.CLIENT_JOIN, self.on_join)
        self.subscribe(EventType.CLIENT_TEAM_CHANGE, self.on_join)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)

    def on_disable(self) -> None:
        """Stop every demo. The classic defined this and never registered it, so nothing happened.

        `recording_all` is deliberately *not* forgotten: the classic restored it on enable and that is
        right — switching the plugin off for a minute is not a decision about what to record.
        """
        was_recording_all = self.recording_all
        self.stop_all()
        self.demos.waiting.clear()
        self.recording_all = was_recording_all

    def on_enable(self) -> None:
        if self.recording_all:
            self.record_all()

    # -- events --------------------------------------------------------------

    def on_join(self, event: Event) -> None:
        """A player reaching a team is what a waiting attempt was waiting for."""
        client = event.client
        if client is None:
            return
        if self.recording_all and not self._recording_of(client):
            self.record(client)
            return
        for entry in [w for w in self.demos.waiting if w.client is client]:
            self._retry(entry)

    def on_disconnect(self, event: Event) -> None:
        """Forget both tables for this player. The server stops the demo itself when they leave."""
        client = event.client
        if client is None:
            return
        self.demos.waiting = [w for w in self.demos.waiting if w.client is not client]
        self.demos.recording = [r for r in self.demos.recording if r.client is not client]

    # -- the service other plugins call --------------------------------------

    def record(self, client: Client, minutes: int = 0, admin: Client | None = None) -> Started:
        """Ask the server to record ``client``, and say what came back.

        This is what `jumper` calls: the filename it answers with is the only way to find that demo
        again. A player who is connected but not on a team yet is put on a list and tried again when
        they join, which is what the classic's thread-per-request was for.
        """
        reply = self.console.ask_verb("record_demo", client)
        if reply is None:
            return Started(reply="this title has no server-side demo recording")
        match = STARTED_RE.match(reply.strip())
        if match:
            filename = match["file"]
            self._remember(client, filename, minutes, admin)
            log.info(
                "urtserversidedemo: recording %s (%s, %s) to %s",
                client.name,
                client.guid,
                client.ip or "no address",
                filename,
            )
            return Started(filename=filename)
        if ALREADY_RE.search(reply):
            return Started(already=True, reply=reply.strip())
        if NOT_ACTIVE_RE.search(reply):
            self._wait_for(client, minutes, admin)
            return Started(waiting=True, reply=reply.strip())
        if NO_PLAYER_RE.match(reply):
            # A slot with nobody in it: there is nothing to wait for, unlike "not active".
            log.info("urtserversidedemo: the server has no player in %s's slot", client.name)
            return Started(reply=reply.strip())
        # Anything else, including a reply nobody has seen before. The classic raised `AssertionError`
        # here, inside a thread, so the attempt vanished without a word to anybody.
        log.warning("urtserversidedemo: %s was not recorded: %s", client.name, reply.strip())
        return Started(reply=reply.strip())

    def stop(self, client: Client, admin: Client | None = None) -> str:
        """Stop recording ``client`` and return what the server said."""
        self.demos.waiting = [w for w in self.demos.waiting if w.client is not client]
        self.demos.recording = [r for r in self.demos.recording if r.client is not client]
        reply = self.console.ask_verb("record_stop", client)
        if reply is None:
            return ""
        log.info("urtserversidedemo: stopped recording %s: %s", client.name, reply.strip())
        return reply.strip()

    def record_all(self, admin: Client | None = None) -> str:
        self.recording_all = True
        reply = self.console.ask_server_verb("record_all") or ""
        log.info("urtserversidedemo: recording every player: %s", reply.strip() or "no reply")
        return reply.strip()

    def stop_all(self, admin: Client | None = None) -> str:
        self.recording_all = False
        self.demos.recording.clear()
        reply = self.console.ask_server_verb("record_stop_all") or ""
        log.info("urtserversidedemo: stopped recording everybody: %s", reply.strip() or "no reply")
        return reply.strip()

    def filename_for(self, client: Client) -> str:
        """The file the server is writing for this player, or "" if it is not recording them."""
        recording = self._recording_of(client)
        return recording.filename if recording is not None else ""

    # -- the tables ----------------------------------------------------------

    def _recording_of(self, client: Client) -> Recording | None:
        return next((r for r in self.demos.recording if r.client is client), None)

    def _remember(
        self, client: Client, filename: str, minutes: int, admin: Client | None
    ) -> Recording:
        self.demos.waiting = [w for w in self.demos.waiting if w.client is not client]
        self.demos.recording = [r for r in self.demos.recording if r.client is not client]
        stop_at = self.console.clock.now() + minutes * 60 if minutes > 0 else 0.0
        recording = Recording(client=client, filename=filename, stop_at=stop_at, admin=admin)
        self.demos.recording.append(recording)
        return recording

    def _wait_for(self, client: Client, minutes: int, admin: Client | None) -> None:
        now = self.console.clock.now()
        self.demos.waiting = [w for w in self.demos.waiting if w.client is not client]
        self.demos.waiting.append(
            Waiting(
                client=client,
                admin=admin,
                minutes=minutes,
                give_up_at=now + WAIT_SECONDS,
                next_try=now + RETRY_SECONDS,
            )
        )

    def _retry(self, entry: Waiting) -> None:
        """One more attempt for somebody who was not on a team last time."""
        self.demos.waiting = [w for w in self.demos.waiting if w is not entry]
        started = self.record(entry.client, minutes=entry.minutes, admin=entry.admin)
        if entry.admin is None:
            return
        if started.ok():
            self.console.tell(
                entry.admin,
                self.message("ussd_started", name=entry.client.name, file=started.filename),
            )
        elif not started.waiting:
            self.console.tell(
                entry.admin,
                self.message("ussd_refused", name=entry.client.name, reply=started.reply),
            )

    def _run_pending(self) -> None:
        """The two deadlines this plugin owes. No thread and no timer.

        A demo with a length stops when it falls due; an attempt waiting on a player who never joins
        a team is given up on rather than kept for the life of the bot.
        """
        now = self.console.clock.now()
        for recording in list(self.demos.recording):
            if recording.stop_at and now >= recording.stop_at:
                # Removed by `stop`, which is the fix for the classic's `KeyError`: its timer deleted
                # the entry itself, and a player who had already disconnected had none to delete.
                reply = self.stop(recording.client)
                if recording.admin is not None:
                    self.console.tell(
                        recording.admin,
                        self.message("ussd_stopped", name=recording.client.name)
                        if reply
                        else self.message("ussd_stop_refused", reply=reply or "nothing"),
                    )
        for entry in list(self.demos.waiting):
            if now >= entry.give_up_at:
                self.demos.waiting.remove(entry)
                log.info(
                    "urtserversidedemo: giving up on recording %s, who never joined a team",
                    entry.client.name,
                )
                continue
            if now >= entry.next_try:
                entry.next_try = now + RETRY_SECONDS
                self._retry(entry)

    # -- commands ------------------------------------------------------------

    @command("startserverdemo", level=20, alias="startdemo")
    def cmd_startserverdemo(self, ctx: CommandContext) -> None:
        """startserverdemo <player|all> [<minutes>] - record a player from the server"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("ussd_usage", command=ctx.command.name))
            return
        # `data == 'all'` in the classic, so `!startserverdemo ALL` looked for a player called "ALL"
        # while `!stopserverdemo` read only the first word and accepted anything after it.
        if parts[0].lower() == "all":
            reply = self.record_all(admin=ctx.client)
            ctx.reply(reply or self.message("ussd_all_started"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        minutes = as_int(parts[1], -1) if len(parts) > 1 else self._default_minutes()
        if minutes < 0:
            ctx.reply(self.message("ussd_usage", command=ctx.command.name))
            return
        started = self.record(target, minutes=minutes, admin=ctx.client)
        if started.ok():
            key = "ussd_started_for" if minutes > 0 else "ussd_started"
            ctx.reply(self.message(key, name=target.name, file=started.filename, minutes=minutes))
            return
        if started.already:
            ctx.reply(self.message("ussd_already", name=target.name))
            return
        if started.waiting:
            ctx.reply(self.message("ussd_waiting", name=target.name))
            return
        # The server's own words, which are more use than "it did not work" — and where the classic
        # raised an AssertionError inside a thread instead.
        ctx.reply(self.message("ussd_refused", name=target.name, reply=started.reply))

    @command("stopserverdemo", level=20, alias="stopdemo")
    def cmd_stopserverdemo(self, ctx: CommandContext) -> None:
        """stopserverdemo <player|all> - stop recording"""
        parts = ctx.args.split()
        if not parts:
            ctx.reply(self.message("ussd_usage", command=ctx.command.name))
            return
        if parts[0].lower() == "all":
            reply = self.stop_all(admin=ctx.client)
            ctx.reply(reply or self.message("ussd_all_stopped"))
            return
        target = self.resolve_client(ctx, parts[0])
        if target is None:
            return
        reply = self.stop(target, admin=ctx.client)
        ctx.reply(reply or self.message("ussd_stopped", name=target.name))

    def _default_minutes(self) -> int:
        return max(0, as_int(self.settings.get("default_minutes"), 0))


__all__ = [
    "DEFAULTS",
    "MESSAGES",
    "RETRY_SECONDS",
    "STARTED_RE",
    "WAIT_SECONDS",
    "Demos",
    "Recording",
    "Started",
    "UrtserversidedemoPlugin",
    "Waiting",
]
