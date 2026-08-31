"""Two-phase player authentication — the single most important CoD-specific mechanism.

A CoD ``J;`` join line carries only guid/cid/name; the player's IP and PunkBuster id are NOT in
the log and only appear in the ``status`` / ``PB_SV_PList`` output, which lags the join. The legacy
code handled this with a ``_counter`` dict and self-rescheduling ``threading.Timer`` calls, aborted
by ``OnQ``. This rewrite models it as an explicit async state machine: after an initial delay, poll
a ``resolve`` callback until the player appears with usable data (or we give up), cancellable on
disconnect.

The state machine is game-agnostic. The CoD-specific validity rule (guid length, IP fallback) lives
in the ``resolve`` callback the parser supplies: it returns an :class:`AuthInfo` once the player is
resolvable, or ``None`` while not ready yet.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthInfo:
    cid: str
    guid: str | None = None
    pbid: str = ""
    ip: str = ""
    #: The same account as the *log* spells it, when the status table names it another way. CoD4X
    #: prints both; without this the resolver cannot tell that the row it found describes the player
    #: who just joined, and throws the answer away as somebody else's.
    alt_guid: str = ""


# resolve(cid) -> AuthInfo when the player is resolvable, else None (retry).
# May be async: the real resolver does an RCON round trip, which must not block the event loop.
Resolver = Callable[[str], "AuthInfo | None | Awaitable[AuthInfo | None]"]
# on_authed(info, attempt) — called once, when a player successfully resolves.
OnAuthed = Callable[[AuthInfo, int], None]
# sleep(seconds) — injectable so tests run without real delays.
SleepFn = Callable[[float], Awaitable[None]]


class AuthManager:
    def __init__(
        self,
        resolve: Resolver,
        on_authed: OnAuthed,
        *,
        max_attempts: int = 10,
        initial_delay: float = 2.0,
        retry_delay: float = 4.0,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._resolve = resolve
        self._on_authed = on_authed
        self._max_attempts = max_attempts
        self._initial_delay = initial_delay
        self._retry_delay = retry_delay
        self._sleep = sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def schedule(self, cid: str) -> None:
        """Begin (or restart) authentication for a joining player."""
        self.cancel(cid)
        self._tasks[cid] = asyncio.ensure_future(self._run(cid))

    def cancel(self, cid: str) -> None:
        """Abort a pending auth (player disconnected before resolving)."""
        task = self._tasks.pop(cid, None)
        if task is not None and not task.done():
            task.cancel()

    async def wait_all(self) -> None:
        """Await all in-flight auth tasks (used by tests / shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    @property
    def pending(self) -> set[str]:
        return set(self._tasks)

    async def _run(self, cid: str) -> None:
        try:
            await self._sleep(self._initial_delay)
            for attempt in range(1, self._max_attempts + 1):
                info = self._resolve(cid)
                if inspect.isawaitable(info):
                    info = await info
                if info is not None:
                    self._on_authed(info, attempt)
                    return
                if attempt < self._max_attempts:
                    await self._sleep(self._retry_delay)
            log.info("auth gave up for cid %s after %d attempts", cid, self._max_attempts)
        except asyncio.CancelledError:
            log.debug("auth cancelled for cid %s", cid)
            raise
        finally:
            self._tasks.pop(cid, None)
