"""The ready-up a match starts with, as a state machine and nothing else.

Three of the classic bot's plugins carried a `MatchManager` class — `poweradminbfbc2`,
`poweradminmoh` and `poweradminhf` — and all three were the same forty lines: `!match on` asks
everybody to type `!ready`, nags whoever has not, and when they all have, counts down from ten and
restarts the round. Each copy had its own `threading.Timer` chain, its own uncancelled timer on
`!match off`, and its own way of getting the announcements out.

What is here is the part that is genuinely identical: whose turn it is to be waited for, when the
next nag is due, and where the countdown has got to. It holds **no text and sends nothing**, which
is what lets one implementation serve titles that announce differently — Bad Company 2 puts the
countdown on the screen with `admin.yell` where the 2010 Medal of Honor has no yell at all and says
it in chat. The plugin asks :meth:`ReadyUp.tick` once a second, is told what just happened, and does
the announcing itself.

Two things the classic got wrong are decisions here rather than accidents:

* **A countdown with nobody on the server does not start.** The classic looped over the connected
  players, found none unready, concluded that everybody was ready and counted an empty server down
  into a round restart. Nobody is not everybody.
* **Readiness is asked of the players who are here now**, each pass, so a player who arrives during
  the ready-up is waited for and one who leaves is not. The classic's dictionary of ready states was
  keyed at `!match on` and never revisited, so a player who joined late was ignored — and on the
  titles where readiness lived in a client variable, a reconnect made somebody ready again.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from b3.core.clock import Clock
from b3.domain.client import Client

#: Seconds between "we are waiting for you" rounds, and how long the first one waits. The classic's
#: figure, and it is the right shape: long enough to type `!ready`, short enough to keep a match
#: moving.
NAG_SECONDS = 10.0

#: Where the countdown starts, and how long a step lasts. The classic's first step was 0.9 seconds
#: and the rest a full second, for no reason anybody wrote down; one second throughout.
COUNTDOWN_FROM = 10
COUNTDOWN_STEP = 1.0


class Step(Enum):
    """What a pass of :meth:`ReadyUp.tick` found, for the plugin to announce."""

    #: Somebody is not ready. `Tick.waiting` says who.
    WAITING = "waiting"
    #: Everybody is, and the countdown begins with the next pass.
    READY = "ready"
    #: A countdown number, in `Tick.count`.
    COUNT = "count"
    #: The countdown reached zero: start the match.
    START = "start"


@dataclass(slots=True)
class Tick:
    """One thing that happened, handed back for the caller to put into words."""

    step: Step
    waiting: tuple[Client, ...] = ()
    count: int = 0


@dataclass(slots=True)
class ReadyUp:
    """Who is ready, who is being waited for, and how far the countdown has got.

    Drive it from the plugin's own scheduled pass: :meth:`begin` when a match is called,
    :meth:`toggle` when somebody types `!ready`, :meth:`tick` every second, :meth:`cancel` when the
    match is called off or the plugin is disabled.
    """

    clock: Clock
    nag_seconds: float = NAG_SECONDS
    countdown_from: int = COUNTDOWN_FROM
    countdown_step: float = COUNTDOWN_STEP
    _ready: list[Client] = field(default_factory=list)
    _due: float = 0.0
    _count: int | None = None
    _running: bool = False

    # -- state ---------------------------------------------------------------

    def running(self) -> bool:
        """Whether a ready-up is in progress at all."""
        return self._running

    def counting_down(self) -> bool:
        """Whether the countdown has started, after which nobody may un-ready themselves."""
        return self._count is not None

    def is_ready(self, client: Client) -> bool:
        return client in self._ready

    def waiting_for(self, players: Iterable[Client]) -> list[Client]:
        """Which of ``players`` have not said they are ready."""
        return [player for player in players if player not in self._ready]

    # -- driving it ----------------------------------------------------------

    def begin(self) -> None:
        """Start a ready-up. Everybody counts as not ready, whatever an earlier one thought."""
        self._ready.clear()
        self._count = None
        self._running = True
        self._due = self.clock.now() + self.nag_seconds

    def cancel(self) -> None:
        """Stop, wherever it had got to. Nothing is announced and no match starts."""
        self._ready.clear()
        self._count = None
        self._running = False
        self._due = 0.0

    def toggle(self, client: Client) -> bool | None:
        """Flip one player's ready state and return it — ``None`` once the countdown has started.

        Refusing after the countdown is the classic's rule and a sound one: a player who un-readies
        at four seconds stops a match everybody else is waiting for.
        """
        if not self._running or self.counting_down():
            return None
        if client in self._ready:
            self._ready.remove(client)
            return False
        self._ready.append(client)
        return True

    def tick(self, players: Iterable[Client]) -> Tick | None:
        """What has happened since the last pass, or ``None`` if nothing is due yet."""
        if not self._running or self.clock.now() < self._due:
            return None
        if self._count is not None:
            return self._count_down()
        here = list(players)
        waiting = self.waiting_for(here)
        if not here or waiting:
            # Nobody is not everybody: an empty server is still being waited for, not ready.
            self._due = self.clock.now() + self.nag_seconds
            return Tick(Step.WAITING, waiting=tuple(waiting))
        self._count = self.countdown_from
        self._due = self.clock.now() + self.countdown_step
        return Tick(Step.READY)

    def _count_down(self) -> Tick:
        remaining = self._count or 0
        if remaining <= 0:
            self.cancel()
            return Tick(Step.START)
        self._count = remaining - 1
        self._due = self.clock.now() + self.countdown_step
        return Tick(Step.COUNT, count=remaining)


__all__ = [
    "COUNTDOWN_FROM",
    "COUNTDOWN_STEP",
    "NAG_SECONDS",
    "ReadyUp",
    "Step",
    "Tick",
]
