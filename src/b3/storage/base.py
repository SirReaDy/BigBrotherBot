"""The Storage contract.

In the legacy code this was a ~30-method abstract ``Storage`` class — one of the cleanest seams
in the whole project (the rest of B3 reached the DB almost exclusively via ``console.storage.*``).
We keep that seam as a typing ``Protocol`` so alternative backends can be dropped in, and so the
runtime depends on the interface rather than a concrete database.

Scoped to what v1 (CoD4 + admin) needs; it will grow as later phases require.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from b3.domain.client import Alias, Client, IpAlias, Penalty, PenaltyType
from b3.domain.permissions import Group


@runtime_checkable
class Storage(Protocol):
    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> None: ...
    def close(self) -> None: ...

    # -- clients -----------------------------------------------------------
    def get_client_by_id(self, client_id: int) -> Client | None: ...
    def get_client_by_guid(self, guid: str) -> Client | None: ...
    def save_client(self, client: Client) -> Client:
        """Insert or update by guid/id; returns the client with its ``id`` populated."""
        ...

    def count_clients(self) -> int: ...
    def search_clients(self, term: str, limit: int = 5) -> list[Client]:
        """Find stored clients by current name or recorded alias, most recently seen first."""
        ...

    # -- groups ------------------------------------------------------------
    def get_groups(self) -> list[Group]: ...
    def has_superadmin(self) -> bool:
        """True if any client is in the superadmin group (drives !iamgod bootstrap)."""
        ...

    # -- penalties ---------------------------------------------------------
    def add_penalty(self, penalty: Penalty) -> Penalty: ...
    def get_active_penalties(
        self, client_id: int, type_: PenaltyType | None = None
    ) -> list[Penalty]:
        """Penalties still in force (see :meth:`Penalty.is_active`), most recent first."""
        ...

    def disable_penalties(self, client_id: int, type_: PenaltyType | None = None) -> int:
        """Soft-delete (lift) matching penalties by setting inactive=1. Returns count."""
        ...

    def disable_penalty(self, penalty_id: int | None) -> bool:
        """Lift one penalty by id (soft delete). Returns False if there was nothing to lift."""
        ...

    def get_recent_penalties(
        self, types: tuple[PenaltyType, ...] | None = None, limit: int = 5
    ) -> list[Penalty]:
        """The most recently issued penalties still in force, newest first (drives `!lastbans`)."""
        ...

    def banned_ips(self) -> set[str]:
        """Addresses of clients holding a ban or tempban that is still in force.

        One query rather than a row per player, because the caller — `ipban` — asks the same question
        of everybody who connects. **Empty addresses are excluded**: a banned player whose address was
        never recorded would otherwise put `""` in this set, and every player the engine has not yet
        given an address to would match it. On the Call of Duty and Quake 3 engines that is every
        player at the moment they authenticate.
        """
        ...

    # -- aliases -----------------------------------------------------------
    def add_alias(self, alias: Alias) -> Alias: ...
    def get_aliases(self, client_id: int) -> list[Alias]: ...
    def add_ip_alias(self, ip_alias: IpAlias) -> IpAlias: ...
    def get_ip_aliases(self, client_id: int) -> list[IpAlias]: ...
