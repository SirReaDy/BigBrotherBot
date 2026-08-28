"""Keeps whole networks off the server.

A port of the classic `netblocker` plugin. An operator lists the networks a player may not connect
from — a hosting range that only ever brings bots, a school's address block, a country's proxy
provider — and anybody arriving from one is removed. It is the blunt instrument you reach for when the
same nuisance keeps coming back on a new account from the same place.

Kept from the classic: what it does, one setting for the list and one for the level that is exempt
from it.

Changed:

* **The 1,091-line vendored CIDR library is gone.** The original shipped a copy of a third-party
  `netblock` package to do address arithmetic, from an era when Python had none. The standard library
  has had `ipaddress` since 3.3, and it is better: `1.2.3.0/24`, a bare address, a dashed range and
  **IPv6** all work, where `netblock.convert` understood dotted-quad IPv4 and nothing else.
* **A player whose address is not known yet is not a match.** The classic converted `client.ip`
  whatever it held and compared the result — the same shape of fault that made `ipban` kick every
  player on the Call of Duty and Quake 3 engines, where the log line that authenticates somebody
  carries no address at all.
* **...and on those engines it now checks when the address *arrives*.** Authentication was the only
  moment the classic looked, so on CoD and Quake 3 it was looking before there was anything to see —
  the address comes from the status poll a moment later. `CLIENT_UPDATE` is the other half, exactly as
  `banlist` and `ipban` do it.
* **One event, not a branch on the engine.** The classic listened for a PunkBuster connection event on
  Frostbite and `EVT_CLIENT_AUTH` everywhere else; both arrive here as an authentication or an update
  with an address on it, so there is nothing per-title left.
* **A bad entry in the list is refused by name**, and the rest of the list still loads. A typo used to
  raise inside the check, once per connecting player.
"""

from __future__ import annotations

import ipaddress
import logging

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: What separates the two ends of a range in the config, as the classic's library accepted.
RANGE_SEPARATOR = "-"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

DEFAULTS: dict[str, object] = {
    # At or below this level a player is subject to the list. 1 (`user`) is the classic's default,
    # which means a registered player is already exempt — deliberately: this list is aimed at people
    # nobody knows, and a regular sharing a blocked range should not be caught by it.
    "max_level": 1,
}

MESSAGES = {
    "netblocker_kicked": "connections from your network are not accepted here",
}


def parse_blocks(entries: object) -> list[Network]:
    """Turn the configured list into networks, refusing entries one at a time.

    Four spellings, and all of them are things an operator will write: `10.0.0.0/8`,
    `192.168.1.1`, `1.2.3.4 - 1.2.3.20`, and any of those in IPv6. A range that is not a clean
    network boundary becomes the several networks that cover it, which is what makes the dashed form
    exact rather than approximate.
    """
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, (list, tuple)):
        if entries is not None:
            log.error("netblocker: `blocks` must be a list of networks; nothing is blocked")
        return []
    blocks: list[Network] = []
    for entry in entries:
        text = str(entry).strip()
        if not text:
            continue
        blocks.extend(_networks_for(text))
    return blocks


def _networks_for(text: str) -> list[Network]:
    if RANGE_SEPARATOR in text:
        first, _, last = text.partition(RANGE_SEPARATOR)
        try:
            return list(
                ipaddress.summarize_address_range(
                    ipaddress.ip_address(first.strip()), ipaddress.ip_address(last.strip())
                )
            )
        except (ValueError, TypeError) as exc:
            log.error("netblocker: %r is not a usable address range (%s); ignored", text, exc)
            return []
    try:
        # `strict=False` so a host address with a prefix (`1.2.3.4/24`) is read as the network it is
        # in rather than refused. An operator writing that means the network, every time.
        return [ipaddress.ip_network(text, strict=False)]
    except ValueError as exc:
        log.error("netblocker: %r is not a usable network (%s); ignored", text, exc)
        return []


class NetblockerPlugin(Plugin):
    """Removes players connecting from a listed network."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.blocks: list[Network] = []

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.blocks = parse_blocks(self.settings.get("blocks"))

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_AUTH, self.on_seen)
        # The other half, and the reason this plugin did nothing on two whole families: on Call of
        # Duty and Quake 3 the line that authenticates a player carries no address, so the only
        # moment worth checking is when one arrives.
        self.subscribe(EventType.CLIENT_UPDATE, self.on_seen)
        if not self.blocks:
            log.warning("netblocker: no networks are configured, so nothing will be blocked")
        else:
            log.info("netblocker: %d network(s) blocked", len(self.blocks))

    # -- checking ------------------------------------------------------------

    def on_seen(self, event: Event) -> None:
        if event.client is not None:
            self.check(event.client)

    def check(self, client: Client) -> bool:
        """Kick this player if they are connecting from a listed network. True if they were."""
        if not self.blocks or not client.ip:
            return False
        if client.max_level() > as_level(self.settings.get("max_level"), 1):
            return False
        try:
            address = ipaddress.ip_address(client.ip.strip())
        except ValueError:
            # Not an address at all. Said once per occurrence rather than acted on: an engine that
            # reports something unexpected here is a parser question, not grounds for a kick.
            log.warning("netblocker: %r is not an address I can read", client.ip)
            return False
        for network in self.blocks:
            if address in network:
                log.info(
                    "netblocker: %s is connecting from %s, inside the blocked network %s",
                    client.name,
                    client.ip,
                    network,
                )
                self.console.kick(client, reason=self.message("netblocker_kicked"))
                return True
        return False


__all__ = [
    "DEFAULTS",
    "MESSAGES",
    "RANGE_SEPARATOR",
    "Network",
    "NetblockerPlugin",
    "parse_blocks",
]
