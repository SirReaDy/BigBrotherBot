"""In-memory registry of currently-connected clients, keyed by in-game slot id (cid).

A deliberately small replacement for the legacy ``Clients`` dict-subclass (which mixed in lazy
lookup indexes, SQL escaping, and event side effects). Persistence and lookup-by-guid live in the
storage layer; this only tracks who is on the server right now.
"""

from __future__ import annotations

from b3.domain.client import Client


class ClientManager:
    def __init__(self) -> None:
        self._by_cid: dict[str, Client] = {}

    def get_by_cid(self, cid: str) -> Client | None:
        return self._by_cid.get(cid)

    def add(self, client: Client) -> Client:
        if client.cid is None:
            raise ValueError("client must have a cid to be tracked")
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
