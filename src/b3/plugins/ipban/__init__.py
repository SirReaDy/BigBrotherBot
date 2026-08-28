"""Keeps out a banned player who has come back with a different identity.

A port of the classic `ipban` plugin, and it closes the obvious hole in a guid-based ban: a player who
is banned reinstalls, gets a new guid and walks straight back in. Their **address** has not changed, so
if it is the address of anybody holding a ban or a tempban, they are kicked again.

Kept from the classic: the exemption by level, and the shape of the idea — a check on the addresses
behind active bans, not a list of its own.

Changed, and the first is a fault with teeth:

* **An empty address matches nobody.** The classic asked the database for the addresses of banned
  clients and compared them with `client.ip in banned` — with neither side excluding the empty string.
  A banned player whose address was never recorded put `""` in that set, and then **every player the
  engine had not yet given an address to matched it**. On the Call of Duty and Quake 3 engines that is
  every player at the moment they authenticate, which is a bot that empties the server.
* **The addresses are one query, cached for a few seconds**, where the classic ran two SQL statements
  per connecting player and rebuilt the list each time. `Storage.banned_ips` is now part of the typed
  storage contract, so this plugin does not write SQL at all.
* **It also checks when an address arrives**, not only at authentication. On most of these engines the
  log line that authenticates a player carries no address: the status poll resolves one moments later
  (the same thing `banlist` found). The classic worked around this on Frostbite by listening for a
  PunkBuster event; `CLIENT_UPDATE` is the general answer.
"""

from __future__ import annotations

import logging

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: How long the set of banned addresses is reused before it is asked for again. Short, because a ban
#: issued now should keep out the same player's next connection; long enough that a server filling up
#: after a map change is one query rather than twenty.
CACHE_SECONDS = 15.0

DEFAULTS: dict[str, object] = {
    # Players at or below this level are checked. The classic's default is `user` (1), which is worth
    # keeping: a registered player who shares a household address with somebody banned is the case
    # this exists to *not* punish.
    "max_level": 1,
}

MESSAGES = {
    "ipban_kicked": "banned from this server",
}


class IpbanPlugin(Plugin):
    """Kicks a connecting player whose address is behind an active ban."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self._addresses: set[str] = set()
        self._read_at = 0.0

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_AUTH, self.on_seen)
        # And when an address turns up, which is not the same moment: a Call of Duty log line carries
        # none, so the check at authentication has nothing to compare.
        self.subscribe(EventType.CLIENT_UPDATE, self.on_seen)
        # A ban issued a second ago must keep out that player's next connection, so the cached set is
        # dropped when one is issued or lifted rather than waiting out its few seconds.
        for changed in (
            EventType.CLIENT_BAN,
            EventType.CLIENT_BAN_TEMP,
            EventType.CLIENT_UNBAN,
        ):
            self.subscribe(changed, self.on_bans_changed)
        log.info(
            "ipban: %d address(es) currently behind an active ban", len(self.banned_addresses())
        )

    def banned_addresses(self) -> set[str]:
        """The addresses behind active bans, re-read at most every `CACHE_SECONDS`."""
        now = self.console.clock.now()
        if not self._addresses or now - self._read_at >= CACHE_SECONDS:
            try:
                self._addresses = self.console.storage.banned_ips()
            except Exception as exc:  # noqa: BLE001 - a failed query must not refuse every player
                log.warning("ipban: could not read the banned addresses (%s)", exc)
                return self._addresses
            self._read_at = now
        return self._addresses

    def on_seen(self, event: Event) -> None:
        client = event.client
        if client is None:
            return
        self.check(client)

    def check(self, client: Client) -> bool:
        """Kick this player if their address is banned. True if they were kicked.

        The address is required to be non-empty on *this* side as well as in the set: a player the
        engine has not identified yet is not evidence of anything, and treating them as a match is how
        this plugin would empty a server rather than police it.
        """
        if not client.ip:
            return False
        if client.max_level() > as_level(self.settings.get("max_level"), 1):
            return False
        if client.ip not in self.banned_addresses():
            return False
        log.info(
            "ipban: %s is connecting from %s, which is behind an active ban", client.name, client.ip
        )
        self.console.kick(client, reason=self.message("ipban_kicked"))
        return True

    def on_bans_changed(self, event: Event) -> None:
        """A ban or an unban makes the cached set wrong, so it is dropped rather than aged out."""
        self._addresses = set()
        self._read_at = 0.0


__all__ = ["CACHE_SECONDS", "DEFAULTS", "MESSAGES", "IpbanPlugin"]
