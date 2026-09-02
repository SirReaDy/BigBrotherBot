"""Asynchronous, instance-scoped event bus.

Replaces the legacy dispatch machinery — a bounded ``queue.Queue`` drained by a single
hand-spawned thread, paced with ``time.sleep(0.001)`` to avoid ordering races, with a
10-second event-expiry drop and events silently discarded when no handler was registered.

Here dispatch is a plain ``asyncio`` coroutine: handlers (sync or async) run in
registration order; a handler may raise :class:`VetoEvent` to stop propagation; handler
exceptions are logged and swallowed so one bad plugin cannot kill the bus. The bus is an
ordinary object — no module singletons, no global registry.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from b3.core.events import Event, EventType, VetoEvent

log = logging.getLogger(__name__)

# A handler takes an Event and returns either None (sync) or an awaitable (async).
Handler = Callable[[Event], None | Awaitable[None]]
# Optional gate: return False to skip this subscription (e.g. plugin disabled).
EnabledPredicate = Callable[[], bool]


@dataclass(slots=True)
class Subscription:
    event_type: EventType
    handler: Handler
    is_enabled: EnabledPredicate | None = None
    label: str = ""

    def enabled(self) -> bool:
        return self.is_enabled is None or self.is_enabled()


class EventBus:
    """In-process publish/subscribe over :class:`EventType`."""

    def __init__(self) -> None:
        self._subs: dict[EventType, list[Subscription]] = {}
        self._background: set[asyncio.Task[bool]] = set()

    # -- subscription ------------------------------------------------------

    def subscribe(
        self,
        event_type: EventType,
        handler: Handler,
        *,
        is_enabled: EnabledPredicate | None = None,
        label: str = "",
    ) -> Subscription:
        """Register ``handler`` for ``event_type``. Returns the Subscription so it can
        be passed to :meth:`unsubscribe` later (plugin unload)."""
        sub = Subscription(
            event_type, handler, is_enabled, label or getattr(handler, "__qualname__", "")
        )
        self._subs.setdefault(event_type, []).append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        subs = self._subs.get(sub.event_type)
        if subs and sub in subs:
            subs.remove(sub)

    def handler_count(self, event_type: EventType) -> int:
        return len(self._subs.get(event_type, ()))

    # -- dispatch ----------------------------------------------------------

    async def publish(self, event: Event) -> bool:
        """Dispatch ``event`` to all enabled handlers in registration order.

        Returns ``True`` if propagation completed, ``False`` if a handler vetoed it.
        Unlike the legacy bus, an event with no handlers is simply a no-op (not an error).
        """
        for sub in list(self._subs.get(event.type, ())):
            if not sub.enabled():
                continue
            try:
                result = sub.handler(event)
                if inspect.isawaitable(result):
                    await result
            except VetoEvent:
                log.debug("event %s vetoed by %s", event.type.name, sub.label)
                return False
            except Exception:  # noqa: BLE001 - one bad handler must not kill the bus
                log.exception("handler %s raised on %s", sub.label, event.type.name)
        return True

    def publish_soon(self, event: Event) -> asyncio.Task[bool] | None:
        """Fire-and-forget publish from within a running loop.

        The task is tracked so :meth:`drain` can await it — this lets a synchronous action
        (e.g. ``Console.ban``) emit a follow-up event that is guaranteed delivered before the
        current line's processing completes.

        The bus is asyncio, not thread-safe, so this only works on the loop thread. Called from a
        worker thread it returns None and says so loudly, rather than leaving an un-awaited
        coroutine behind for the interpreter to complain about at some unrelated moment.
        """
        coro = self.publish(event)
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:
            coro.close()
            log.error(
                "publish_soon(%s) called off the event loop; the event was dropped",
                event.type.name,
            )
            return None
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def drain(self) -> None:
        """Await all outstanding fire-and-forget publishes (including ones they spawn).

        Finished tasks are dropped here rather than left to their done-callback, and that is the
        whole of why this is not a two-line loop. `publish_soon` removes a task with
        `add_done_callback(self._background.discard)`, and a done-callback is *queued* on the loop
        rather than run when the task completes - while awaiting an already-finished future takes
        asyncio's fast path and does not yield. So a set holding one finished task whose callback has
        not been run yet never empties, and `while self._background` spins on it forever, burning a
        core and printing nothing.

        Whether the window is hit is pure timing, which is what made it look like somebody else's
        bug: it hung `pytest (ubuntu-latest, py3.12)` in CI while 3.11, 3.13 and Windows passed.
        """
        while True:
            tracked = list(self._background)
            for task in tracked:
                if task.done():
                    self._background.discard(task)
            pending = [task for task in tracked if not task.done()]
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)
