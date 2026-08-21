"""Frees a slot on a full server, so a member can get in.

A port of the classic `makeroom` plugin. On a popular server the people who pay for it are the ones
who cannot join it, so an admin types `!makeroom`, the newest player from the lowest group is told
why and removed, and the slot is held for a few seconds while a member takes it. Automation does the
same thing continuously: keep N slots free, kicking whoever arrives and does not belong to the
groups that are allowed in.

This is the bluntest plugin in the tree — its entire job is to remove somebody who has done nothing
wrong — so the interesting decisions are all about *who* and *when not to*.

Kept from the classic: the two commands, `non_member_level`, the announcement before the kick and
the delay after it, the slot-retention window with its thirty-second ceiling, automation with
`min_free_slots`, and a member arriving ending the retention early — which is the point of it.

Changed, and the first four are faults rather than preferences:

* **"The newest player" is now the newest *arrival*.** The classic sorted its candidates by
  `Client.timeAdd`, which is when the player's **database row** was created, not when they joined.
  On the servers this plugin exists for — the busy ones, full of regulars — that is the newest
  *account*: a player who first visited two years ago and walked in a minute ago sorted as the
  oldest, and somebody who registered last week and had been playing for three hours sorted as the
  newest and got kicked. Its own tests could not show it, because they set `timeAdd` by hand.
  `Client.connected_at` is the arrival, stamped in `ClientManager.add`.
* **Automation no longer crashes the moment it fires.** When the last player to connect was a
  member, the classic called `cmd_makeroom()` with no client, and every path out of that reaches
  `admin.message(...)` on None. The kick went out and the `AttributeError` was swallowed by the
  event dispatcher, so what an operator saw was automation that worked and a traceback in the log.
  Replying to the admin is separate from freeing the slot here, and there is nobody to reply to.
* **A bot is never the candidate.** The classic's list was every client, and an AI player's level is
  0 — the lowest group there is — so on any server running bots the "newest player from the lowest
  group" was a bot, every time. The engine then refills it, and the admin still cannot get in.
* **A masked admin is judged by their real level.** `getClientsByLevel` compared the *mask* when one
  was set, so a superadmin playing incognito was a non-member and got kicked to free a slot for
  somebody with less rank than them. A mask hides rank; it never removes it.
* **The retention window is decided before the kick, not after.** The classic snapshotted
  `_count_players()` immediately after asking for the kick and required a later join to exceed it. A
  kick is asynchronous — the player leaves when the server says so — so whether that snapshot
  included the player being removed was a race, and losing it meant the window silently held nothing.
  The population we intend to keep is computed up front instead.
* **The server is asked how many slots it has.** `total_slots` was a number the operator typed, and
  typing it wrong means automation that never fires or one that kicks on a server that is not full.
  `sv_maxclients` is already in `game.max_players`; config is now an override for the operator who
  is deliberately reserving slots, not the only source.
* **`!makeroom` on a server that is not full kicks nobody** (`only_when_full`, on by default). The
  classic obliged regardless, so a mistyped command cost a player their game for a slot nobody
  needed.
* **Nothing is announced until there is somebody to remove.** The classic said "making room for clan
  member" to the whole server, waited out the delay, and only then discovered there was no
  non-member to kick.
* **No threads.** The classic's delay was a `threading.Timer` per request, aimed at a client object,
  which fired whether or not that player was still on the server — so the kick could land on
  whoever had taken the slot in the meantime. A pending request is a deadline here, checked by one
  scheduled task, and the target is re-checked when it comes due.
* **A misconfigured automation section no longer deletes the command.** The classic reached into the
  admin plugin's command table and popped `makeroomauto` out of it. The command exists; asking for
  something it cannot do gets an answer saying so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_float, as_int
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: The ceiling the classic put on the retention window, and it is the right shape of number: a slot
#: held longer than this is a slot taken from everybody else on the strength of a member who may not
#: be coming.
MAX_RETAIN = 30

#: How long the announcement may precede the kick. The classic clamped neither end. Nothing is a
#: warning if it arrives with the kick, and a warning nobody is still reading is not one either.
MAX_DELAY = 30

#: Fewest slots a server can sensibly be said to have for this purpose — the classic's floor. One
#: slot means the only player who can ever be on the server is the one being kicked out of it.
MIN_SLOTS = 2

DEFAULTS: dict[str, object] = {
    # At or below this level a player may be kicked to free a slot. 2 is `reg` — the classic's
    # default, and the meaning of the plugin: the people who support the server keep their slot.
    "non_member_level": 2,
    # Seconds between the announcement and the kick, so the player being removed knows why. 0 sends
    # no announcement and kicks at once.
    "delay": 2,
    # Seconds to hold the freed slot for a member, 0-30. 0 gives it to whoever joins first.
    "retain_free_duration": 15,
    # Refuse `!makeroom` when the server already has a free slot. Off restores the classic's
    # behaviour of kicking somebody regardless.
    "only_when_full": True,
    # Keep `min_free_slots` free at all times, kicking arriving non-members. Off by default: this is
    # the setting that turns a moderation plugin into a door policy.
    "automation": False,
    # How many slots this server has. 0 asks the server (`sv_maxclients`); set it only to state a
    # smaller number than the truth, which is what reserving slots outside the bot looks like.
    "total_slots": 0,
    # How many of them automation keeps free.
    "min_free_slots": 1,
}

MESSAGES = {
    "makeroom_info": "making room for a member — please come back again",
    "makeroom_auto_info": "a slot is kept free for a member — please come back again",
    "makeroom_kick_announce": "kicking {name} to free a slot",
    "makeroom_kick_reason": "to make room for a server member",
    "makeroom_freed": "{name} was kicked to free a slot",
    "makeroom_freed_retained": "{name} was kicked to free a slot — a member has {seconds}s to take it",
    "makeroom_none": "there is nobody here I may kick to free a slot",
    "makeroom_in_progress": "a makeroom request is already running",
    "makeroom_retaining": "a slot is already being held for a member — {seconds}s left",
    "makeroom_not_full": "no need: {players} of {slots} slots are taken",
    "makeroom_auto_usage": "expecting on or off",
    "makeroom_auto_on": "makeroom automation is on, keeping {count} of {slots} slots free",
    "makeroom_auto_off": "makeroom automation is off",
    "makeroom_auto_unavailable": "this server has not said how many slots it has — "
    "set total_slots in the makeroom config",
}


@dataclass(slots=True)
class Pending:
    """A kick that has been announced and is waiting out the delay.

    ``target`` is the player automation picked — the one who just arrived — and None when the
    candidate should be chosen at the deadline instead, which is what `!makeroom` does so that a
    player who leaves during the delay is not still the answer.
    """

    due: float
    admin: Client | None = None
    target: Client | None = None


@dataclass(slots=True)
class Retain:
    """A freed slot being held open for a member."""

    until: float
    #: The population this plugin means the server to stay at or below. A non-member whose arrival
    #: pushes it above that is taking the slot that was freed, and is kicked.
    keep_at_or_below: int
    admin: Client | None = None


class MakeroomPlugin(Plugin):
    """Kicks the newest player from the lowest group, so a member can join a full server."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: Kicks that have been announced and are waiting out the delay. A list rather than the
        #: classic's single lock because automation can announce two at once — two players joining a
        #: full server inside the delay — and a single slot silently dropped the first of them,
        #: leaving somebody in the seat that had just been announced as going.
        self._pending: list[Pending] = []
        self._retain: Retain | None = None
        #: Said once rather than on every arrival: automation that cannot work is a config fault, and
        #: repeating it once a second is how a log becomes unreadable.
        self._warned_unusable = False

    # -- config --------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

        retain = as_int(self.settings.get("retain_free_duration"), 15)
        if retain < 0 or retain > MAX_RETAIN:
            log.warning(
                "makeroom: retain_free_duration %s is outside 0-%ds; using %d. A slot held longer "
                "than that is taken from everybody else for a member who may not arrive",
                retain,
                MAX_RETAIN,
                min(max(retain, 0), MAX_RETAIN),
            )
        self.settings["retain_free_duration"] = min(max(retain, 0), MAX_RETAIN)

        delay = as_float(self.settings.get("delay"), 2.0)
        if delay < 0 or delay > MAX_DELAY:
            log.warning(
                "makeroom: delay %s is outside 0-%ds; using %d", delay, MAX_DELAY, MAX_DELAY
            )
            delay = min(max(delay, 0.0), float(MAX_DELAY))
        self.settings["delay"] = delay

        configured = as_int(self.settings.get("total_slots"), 0)
        if configured and configured < MIN_SLOTS:
            log.warning(
                "makeroom: total_slots %s is below %d; asking the server instead",
                configured,
                MIN_SLOTS,
            )
            configured = 0
        self.settings["total_slots"] = configured

        keep = as_int(self.settings.get("min_free_slots"), 1)
        if keep < 0:
            log.warning("makeroom: min_free_slots cannot be negative; using 0")
            keep = 0
        if configured and keep >= configured:
            log.warning(
                "makeroom: min_free_slots %s is not less than total_slots %s — automation would "
                "kick every player who joins, so it stays off",
                keep,
                configured,
            )
            self.settings["automation"] = False
        self.settings["min_free_slots"] = keep

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        # Authentication rather than connection, because a player's level is not known before it —
        # and this plugin does nothing but compare levels.
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        self.schedule(self._check_deadlines, second="*", name="MakeroomPlugin.deadlines")

    def on_disable(self) -> None:
        """Forget the pending kick and the held slot. A plugin switched off must not kick later."""
        self._pending.clear()
        self._retain = None

    # -- commands ------------------------------------------------------------

    @command("makeroom", level=20, alias="mkr")
    def cmd_makeroom(self, ctx: CommandContext) -> None:
        """makeroom - free a slot by kicking the newest arrival from the lowest group"""
        held = self._held_slot()
        if held is not None:
            ctx.reply(
                self.message(
                    "makeroom_retaining", seconds=int(held.until - self.console.clock.now())
                )
            )
            return
        if self._pending:
            ctx.reply(self.message("makeroom_in_progress"))
            return

        slots = self.total_slots()
        players = self.population()
        if self.settings.get("only_when_full") and slots and players < slots:
            ctx.reply(self.message("makeroom_not_full", players=players, slots=slots))
            return

        # Asked before anything is announced: the classic told the whole server it was making room,
        # waited out the delay, and only then found there was nobody it was allowed to remove.
        if self.candidate(exclude=ctx.client) is None:
            ctx.reply(self.message("makeroom_none"))
            return

        delay = as_float(self.settings.get("delay"), 2.0)
        if delay <= 0:
            self.free_a_slot(ctx.client)
            return
        self.console.say(self.message("makeroom_info"))
        self._pending.append(Pending(due=self.console.clock.now() + delay, admin=ctx.client))

    @command("makeroomauto", level=60, alias="mrauto")
    def cmd_makeroomauto(self, ctx: CommandContext) -> None:
        """makeroomauto <on|off> - keep slots free without being asked each time"""
        wanted = ctx.args.strip().lower()
        if wanted not in ("on", "off"):
            ctx.reply(self.message("makeroom_auto_usage"))
            return
        if wanted == "off":
            self.settings["automation"] = False
            ctx.reply(self.message("makeroom_auto_off"))
            return
        slots = self.total_slots()
        if not slots:
            # The one thing automation cannot do without. Said here rather than at the next arrival,
            # where nobody is listening.
            ctx.reply(self.message("makeroom_auto_unavailable"))
            return
        keep = as_int(self.settings.get("min_free_slots"), 1)
        if keep >= slots:
            ctx.reply(self.message("makeroom_auto_unavailable"))
            return
        self.settings["automation"] = True
        self._warned_unusable = False
        ctx.reply(self.message("makeroom_auto_on", count=keep, slots=slots))

    # -- arrivals ------------------------------------------------------------

    def on_auth(self, event: Event) -> None:
        """A player has identified themselves: hold the door, or count the slots.

        Order matters and is the classic's: a member arriving ends the retention, because the slot
        was being held for exactly this. Only then is anybody kicked for taking it.
        """
        client = event.client
        if client is None:
            return
        if client.is_bot:
            # An AI player authenticates like anybody else (its guid is dropped, not its arrival),
            # and there is nothing useful to do about one: the slot it took is refilled the moment
            # it is kicked, so acting would mean announcing a kick on every bot the server cycles.
            # Only `bot_quota` frees that slot, and that is the operator's to set.
            return
        held = self._held_slot()
        if held is not None:
            if self.is_member(client):
                log.info("makeroom: %s took the slot that was being held", client.name)
                self._retain = None
                return
            if self.population() > held.keep_at_or_below:
                self.kick(client, held.admin)
            return
        if self.settings.get("automation"):
            self.check_free_slots(client)

    def check_free_slots(self, arrived: Client) -> None:
        """Keep `min_free_slots` free, at the cost of whoever just arrived or the lowest group."""
        slots = self.total_slots()
        keep = as_int(self.settings.get("min_free_slots"), 1)
        if not slots or keep >= slots:
            if not self._warned_unusable:
                self._warned_unusable = True
                log.warning(
                    "makeroom: automation is on but cannot run — %s. Nobody will be kicked for it",
                    "this server has not said how many slots it has (set total_slots)"
                    if not slots
                    else f"min_free_slots {keep} is not less than {slots} slots",
                )
            return
        players = self.population()
        if slots - players >= keep:
            return
        log.info("makeroom: %d of %d slots taken, keeping %d free", players, slots, keep)
        if self.is_member(arrived):
            # A member filled the last slot, so somebody else has to go. Nobody asked for this, so
            # there is nobody to report it to — which is where the classic raised on `None`.
            self.free_a_slot(None)
            return
        delay = as_float(self.settings.get("delay"), 2.0)
        if delay <= 0:
            self.kick(arrived, None)
            return
        self.console.say(self.message("makeroom_auto_info"))
        self._pending.append(Pending(due=self.console.clock.now() + delay, target=arrived))

    # -- doing it ------------------------------------------------------------

    def free_a_slot(self, admin: Client | None) -> None:
        """Kick the newest player from the lowest group, and hold their slot if asked to."""
        target = self.candidate(exclude=admin)
        if target is None:
            if admin is not None:
                self.console.tell(admin, self.message("makeroom_none"))
            else:
                log.info("makeroom: the server is full and there is nobody here I may kick")
            return
        # Read before the kick: a kick is a request to the server, and the player leaves when the
        # server says they have. Deciding the population we want from a count taken afterwards is
        # what made the classic's retention window a coin toss.
        keep_at_or_below = max(0, self.population() - 1)
        self.kick(target, admin)
        seconds = as_int(self.settings.get("retain_free_duration"), 15)
        if seconds > 0:
            self._retain = Retain(
                until=self.console.clock.now() + seconds,
                keep_at_or_below=keep_at_or_below,
                admin=admin,
            )
        if admin is None:
            return
        key = "makeroom_freed_retained" if seconds > 0 else "makeroom_freed"
        self.console.tell(admin, self.message(key, name=target.name, seconds=seconds))

    def kick(self, target: Client, admin: Client | None) -> None:
        """Say who is going and why, then remove them.

        The announcement is this plugin's, which is why the kick itself is quiet: the classic passed
        `silent=True` for the same reason, so that a player is not told twice.
        """
        self.console.say(self.message("makeroom_kick_announce", name=target.name))
        self.console.kick(target, reason=self.message("makeroom_kick_reason"), admin=admin)

    def _check_deadlines(self) -> None:
        now = self.console.clock.now()
        if self._retain is not None and now >= self._retain.until:
            self._retain = None
        due = [p for p in self._pending if now >= p.due]
        if not due:
            return
        self._pending = [p for p in self._pending if now < p.due]
        for pending in due:
            self._run_pending(pending)

    def _run_pending(self, pending: Pending) -> None:
        if pending.target is None:
            self.free_a_slot(pending.admin)
            return
        # Re-checked, because the classic's timer fired at a client object regardless: a player who
        # left during the delay was kicked in absentia, and a slot reused inside it meant the kick
        # landed on whoever had taken it.
        if self.console.clients.get_by_cid(pending.target.cid or "") is not pending.target:
            log.info("makeroom: %s left before the kick was due", pending.target.name)
            return
        if self.is_member(pending.target):
            log.info("makeroom: %s is a member now; not kicking them", pending.target.name)
            return
        self.kick(pending.target, pending.admin)

    # -- who and how many ----------------------------------------------------

    def candidate(self, exclude: Client | None = None) -> Client | None:
        """The player who would be kicked: lowest group first, newest arrival within it.

        Three exclusions, and each is a case the classic got wrong. A **bot** has level 0, so it was
        always the lowest group and always the answer — and the engine puts it straight back. A
        **masked admin** was judged by the mask rather than their level, so hiding rank got them
        kicked by somebody junior. And the **admin who asked** is never the answer to their own
        request, which the classic allowed once `non_member_level` was set above the command's own.
        """
        limit = as_int(self.settings.get("non_member_level"), 2)
        candidates = [
            client
            for client in self.console.clients.connected()
            if client is not exclude and not client.is_bot and client.max_level() <= limit
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda c: (c.max_level(), -c.connected_at))
        return candidates[0]

    def is_member(self, client: Client) -> bool:
        """Whether this player's slot is safe from this plugin."""
        return client.max_level() > as_int(self.settings.get("non_member_level"), 2)

    def population(self) -> int:
        """How many slots are taken.

        Bots count. They are not candidates — kicking one frees nothing — but they occupy a slot on
        every engine here, so a server whose last four slots hold AI is a full server.
        """
        return len(self.console.clients.connected())

    def total_slots(self) -> int:
        """How many slots this server has: what the operator said, else what the server says.

        Config wins when it is set because the only reason to state a number is to state a *smaller*
        one — slots reserved outside the bot, for a clan or for rcon. 0 from both means the question
        has no answer here, which is a thing automation has to be told rather than guess at.
        """
        configured = as_int(self.settings.get("total_slots"), 0)
        if configured:
            return configured
        return max(0, self.console.game.max_players)

    def _held_slot(self) -> Retain | None:
        """The live retention window, expiring it if it has run out."""
        if self._retain is None:
            return None
        if self.console.clock.now() >= self._retain.until:
            self._retain = None
        return self._retain


__all__ = [
    "DEFAULTS",
    "MAX_DELAY",
    "MAX_RETAIN",
    "MESSAGES",
    "MIN_SLOTS",
    "MakeroomPlugin",
    "Pending",
    "Retain",
]
