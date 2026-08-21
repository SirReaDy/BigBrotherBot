"""In-memory registry of currently-connected clients, keyed by in-game slot id (cid).

A deliberately small replacement for the legacy ``Clients`` dict-subclass (which mixed in lazy
lookup indexes, SQL escaping, and event side effects). Persistence and lookup-by-guid live in the
storage layer; this only tracks who is on the server right now.
"""

from __future__ import annotations

from b3.core.clock import Clock, SystemClock
from b3.domain.client import Client


class ClientManager:
    def __init__(self, clock: Clock | None = None) -> None:
        self._by_cid: dict[str, Client] = {}
        #: Only used to stamp arrivals (see :meth:`add`). Injected so a test with a `FakeClock`
        #: gets the same deterministic times here as everywhere else.
        self._clock: Clock = clock or SystemClock()

    def get_by_cid(self, cid: str) -> Client | None:
        return self._by_cid.get(cid)

    def add(self, client: Client) -> Client:
        """Track a client, stamping when they arrived if nothing has yet.

        This is the one funnel: all eleven places a parser or the roster reconcile creates a client
        end up here, which is why the arrival time is taken here rather than in each of them. An
        already-stamped client keeps its original time — `add` is also how a parser re-registers a
        client it already knows, and that is not a new arrival.
        """
        if client.cid is None:
            raise ValueError("client must have a cid to be tracked")
        if not client.connected_at:
            client.connected_at = self._clock.now()
        self._by_cid[client.cid] = client
        return client

    def remove(self, cid: str) -> Client | None:
        return self._by_cid.pop(cid, None)

    def connected(self) -> list[Client]:
        return list(self._by_cid.values())

    def clear(self) -> None:
        self._by_cid.clear()

    def __len__(self) -> int:
        return len(self._by_cid)

    def __contains__(self, cid: object) -> bool:
        return cid in self._by_cid
