"""Keeps players on VPNs, proxies and Tor off the server.

The ban evader's tool of choice is a VPN: banned, they switch it on and come back with a new address a
minute later, and `ipban` and `netblocker` are matching an address that is no longer theirs. This
plugin asks a different question — not "is this address banned" but "is this somebody's home
connection at all" — and answers it in two layers, cheapest first.

**Layer one: lists, on this machine.** Public lists of VPN ranges (X4BNet's `lists_vpn`) and Tor exit
nodes (the Tor Project's own bulk list) are downloaded once a day, kept on disk so a restart has them
with no network, and searched with a binary search — so a check is microseconds and no player's
address leaves the machine. On top of those, an operator's own `blocks` (written the way `netblocker`
takes them) and `asns`, whole providers by AS number, read from the ASN database `geolocation` already
loads.

**Layer two: an online lookup, off by default.** proxycheck.io catches what no public list does —
residential proxies, the VPN range that appeared last week — but it means sending players' addresses
to a company, which `geolocation` was ported specifically to stop doing. So it is the operator's
decision, made by switching on `lookup`. Answers are remembered for `cache_hours`, so a regular costs
one query every few days rather than one a join, and a lookup that fails or times out **lets the player
in**: a dead API must not empty the server.

**Exemptions, because everybody behind a flagged address is caught.** Registered players are exempt by
default (`max_level`), and `allow` / `allow_guids` cover the regular who genuinely needs a VPN. `action:
watch` reports what would have been kicked to the admins in game and kicks nobody, which is how to find
out what the lists would catch on your server before trusting them with it.

Checked at authentication and again when an address arrives, for the same reason as `netblocker`: on
Call of Duty and Quake 3 the line that authenticates a player carries no address.
"""

from __future__ import annotations

import asyncio
import bisect
import hashlib
import ipaddress
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_float, as_int, as_level, as_word
from b3.domain.client import Client
from b3.plugins.netblocker import Network, parse_blocks

log = logging.getLogger(__name__)

#: The lists fetched when an operator names none. VPN ranges in both address families, and every
#: current Tor exit. X4BNet also publish `output/datacenter/ipv4.txt`, every hosting provider there is,
#: which catches more and is left out on purpose: cloud gaming (GeForce NOW and the like) arrives from
#: a datacenter, and so do a lot of perfectly ordinary players behind a company's network.
DEFAULT_LISTS = (
    "https://raw.githubusercontent.com/X4BNet/lists_vpn/main/output/vpn/ipv4.txt",
    "https://raw.githubusercontent.com/X4BNet/lists_vpn/main/output/vpn/ipv6.txt",
    "https://check.torproject.org/torbulkexitlist",
)

#: proxycheck.io's v3 endpoint. `tag=0` asks them not to store the query against the account.
LOOKUP_URL = "https://proxycheck.io/v3/{ip}?{query}"

#: The `detections` flags that mean "not somebody's own connection". `hosting` is a separate setting,
#: for the reason the datacenter list is not a default. `anonymous` is not one of them: it is a risk
#: score rather than a finding, and it answers true for Google's 8.8.8.8 with every other flag false.
FLAGGED_DETECTIONS = ("proxy", "vpn", "tor")

#: Seconds before a download or a lookup is given up on. A lookup holds nobody up — the player is in
#: the game while it runs — but a thread stuck for minutes is a thread nobody gets back.
DOWNLOAD_TIMEOUT = 30
LOOKUP_TIMEOUT = 5

ACTIONS = ("kick", "watch")

DEFAULTS: dict[str, object] = {
    # `kick` removes a flagged player; `watch` only tells the admins who would have been.
    "action": "kick",
    # At or below this level a player is checked. 1 (`user`) exempts everybody who has registered.
    "max_level": 1,
    "lists": list(DEFAULT_LISTS),
    # How often the lists are downloaded again. They change daily; more often gains nothing.
    "refresh_hours": 24,
    # Where the downloaded lists are kept between restarts. Shared by every instance on the machine.
    "cache_dir": "@home/vpnblocker",
    # Networks of your own, in any spelling `netblocker` accepts.
    "blocks": [],
    # Whole providers by AS number (`AS9009` or `9009`). Needs `geolocation` and its ASN database.
    "asns": [],
    # Networks and players that are never flagged, whatever the lists say.
    "allow": [],
    "allow_guids": [],
    # The online lookup. Off: it sends each unknown player's address to proxycheck.io.
    "lookup": False,
    "api_key": "",
    # Also flag addresses the lookup says belong to a hosting provider. See `DEFAULT_LISTS`.
    "block_hosting": False,
    "cache_hours": 72,
    # Tell the admins in game when somebody is flagged, at or above this level.
    "tell_admins": True,
    "admin_level": 20,
}

MESSAGES = {
    "vpnblocker_kicked": "VPN and proxy connections are not accepted here",
    "vpnblocker_admins": "{name} is connecting through {source} ({action})",
    "vpnblocker_clean": "{name} ({ip}) is not on any VPN or proxy list",
    "vpnblocker_flagged": "{name} ({ip}) is flagged: {source}",
    "vpnblocker_exempt": "{name} is exempt from the VPN check",
    "vpnblocker_unknown": "{name} has no address yet",
    "vpnblocker_usage": "usage: !vpncheck <player>",
}


@dataclass(frozen=True, slots=True)
class Verdict:
    """Why an address was flagged, in words an admin can act on: "Tor exit list", "AS9009"."""

    source: str


class NetworkSet:
    """A great many networks, searchable in O(log n).

    The VPN lists run to tens of thousands of ranges, and a linear `address in network` over all of
    them on every join is the kind of thing that is fine until it is not. Each network becomes an
    integer interval, overlapping intervals are merged, and a lookup is one bisection per address
    family.
    """

    def __init__(self, networks: Iterable[Network] = ()) -> None:
        self._starts: dict[int, list[int]] = {4: [], 6: []}
        self._ends: dict[int, list[int]] = {4: [], 6: []}
        by_version: dict[int, list[tuple[int, int]]] = {4: [], 6: []}
        for network in networks:
            by_version[network.version].append(
                (int(network.network_address), int(network.broadcast_address))
            )
        for version, intervals in by_version.items():
            intervals.sort()
            for start, end in intervals:
                ends = self._ends[version]
                if ends and start <= ends[-1] + 1:
                    ends[-1] = max(ends[-1], end)
                else:
                    self._starts[version].append(start)
                    ends.append(end)

    def __len__(self) -> int:
        return len(self._starts[4]) + len(self._starts[6])

    def __contains__(self, address: object) -> bool:
        if not isinstance(address, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return False
        starts = self._starts[address.version]
        index = bisect.bisect_right(starts, int(address)) - 1
        return index >= 0 and int(address) <= self._ends[address.version][index]


def parse_list(text: str) -> list[Network]:
    """A downloaded list: one network or address a line, `#` comments and blank lines ignored."""
    entries = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    return parse_blocks([entry for entry in entries if entry])


def parse_asns(value: object) -> set[int]:
    """`AS9009`, `as9009`, `9009` and `9009` the number all mean the same provider."""
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return set()
    found: set[int] = set()
    for entry in value:
        text = str(entry).strip().upper().removeprefix("AS")
        if text.isdigit():
            found.add(int(text))
        else:
            log.error("vpnblocker: %r is not an AS number; ignored", entry)
    return found


def verdict_from_lookup(payload: Any, ip: str, *, block_hosting: bool = False) -> Verdict | None:
    """Read a proxycheck.io v3 answer. None when it is clean *or* unreadable — a failure lets in."""
    if not isinstance(payload, dict) or payload.get("status") not in ("ok", "warning"):
        return None
    record = payload.get(ip)
    if not isinstance(record, dict):
        return None
    detections = record.get("detections")
    if not isinstance(detections, dict):
        return None
    flags = [flag for flag in FLAGGED_DETECTIONS if detections.get(flag) is True]
    if block_hosting and detections.get("hosting") is True:
        flags.append("hosting")
    if not flags:
        return None
    operator = record.get("operator")
    name = operator.get("name") if isinstance(operator, dict) else ""
    described = f"{flags[0]} ({name})" if name else flags[0]
    return Verdict(source=f"proxycheck: {described}")


class VpnblockerPlugin(Plugin):
    """Removes players connecting through a VPN, a proxy or Tor."""

    requires_plugins = ("admin",)
    # Not required — only `asns` needs it — but when it is there its ASN database must be open first.
    load_after = ("geolocation",)

    @classmethod
    def check_config(cls, config: object, configured: frozenset[str]) -> list[str]:
        settings: dict[str, Any] = {}
        if isinstance(config, dict):
            settings = config.get("settings") or {}
        problems: list[str] = []
        action = as_word(settings.get("action", DEFAULTS["action"]), "kick")
        if action not in ACTIONS:
            problems.append(
                f"settings.action is {action!r}; it must be one of {', '.join(ACTIONS)}"
            )
        if settings.get("asns") and "geolocation" not in configured:
            problems.append(
                "settings.asns is set, but this server does not load the `geolocation` plugin, "
                "whose ASN database is what says which provider an address belongs to - add "
                "`- name: geolocation` to the plugins list, or remove asns"
            )
        return problems

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.blocks = NetworkSet()
        self.allow = NetworkSet()
        self.asns: set[int] = set()
        #: One set per configured list URL, so a list that fails to download keeps its last copy.
        self.lists: dict[str, NetworkSet] = {}
        #: Lookup answers by address: (when it was asked, the verdict or None for clean).
        self.lookups: dict[str, tuple[float, Verdict | None]] = {}
        #: Lookups still out, by address, so a second player on the same one waits for the first.
        self._pending: dict[str, asyncio.Future[Verdict | None]] = {}

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.blocks = NetworkSet(parse_blocks(self.settings.get("blocks")))
        self.allow = NetworkSet(parse_blocks(self.settings.get("allow")))
        self.asns = parse_asns(self.settings.get("asns"))

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        for url in self.list_urls():
            cached = self.cache_file(url)
            if cached.is_file():
                self.lists[url] = NetworkSet(parse_list(cached.read_text(errors="replace")))
        self.subscribe(EventType.CLIENT_AUTH, self.on_seen)
        self.subscribe(EventType.CLIENT_UPDATE, self.on_seen)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        if self.list_urls():
            # Once now, so a first start does not wait a day for its lists, then on the interval.
            self.schedule(self.refresh, second="*", one_shot=True, name="VpnblockerPlugin.first")
            # A crontab, so an interval of a day or more is once a day, at a quiet hour.
            hours = max(1, as_int(self.settings.get("refresh_hours"), 24))
            self.schedule(
                self.refresh,
                minute=0,
                hour=f"*/{hours}" if hours < 24 else 4,
                name="VpnblockerPlugin.refresh",
            )
        if self.lookup_enabled():
            log.info("vpnblocker: online lookups are on; unknown addresses go to proxycheck.io")
        log.info(
            "vpnblocker: %d cached range(s) from %d list(s), %d of your own, %d provider(s); "
            "action is %s",
            sum(len(found) for found in self.lists.values()),
            len(self.lists),
            len(self.blocks),
            len(self.asns),
            self.action(),
        )

    def list_urls(self) -> list[str]:
        lists = self.settings.get("lists")
        if isinstance(lists, str):
            lists = [lists]
        if not isinstance(lists, (list, tuple)):
            return []
        return [str(url).strip() for url in lists if str(url).strip()]

    def cache_file(self, url: str) -> Path:
        """Where one list is kept. Named by a hash of its URL, because two lists share a filename."""
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self.resolve_path(str(self.settings.get("cache_dir") or DEFAULTS["cache_dir"])) / (
            f"{digest}.txt"
        )

    def action(self) -> str:
        chosen = as_word(self.settings.get("action"), "kick")
        return chosen if chosen in ACTIONS else "kick"

    def lookup_enabled(self) -> bool:
        return bool(self.settings.get("lookup"))

    # -- the lists -----------------------------------------------------------

    async def refresh(self) -> None:
        """Download every list, off the bot's thread. A list that fails keeps the copy it had."""
        for url in self.list_urls():
            text = await asyncio.to_thread(self._download, url)
            if text is None:
                continue
            networks = parse_list(text)
            if not networks:
                log.warning("vpnblocker: %s held no usable networks; keeping the last copy", url)
                continue
            self.lists[url] = NetworkSet(networks)
            try:
                cached = self.cache_file(url)
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_text(text)
            except OSError as exc:
                log.warning("vpnblocker: could not keep a copy of %s (%s)", url, exc)
            log.info("vpnblocker: %d range(s) from %s", len(self.lists[url]), url)

    def _download(self, url: str) -> str | None:
        """Blocking, and called through a worker thread. None on any failure."""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "b3ng"})  # noqa: S310
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310
                body: bytes = response.read()
                return body.decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("vpnblocker: could not download %s (%s)", url, exc)
            return None

    # -- checking ------------------------------------------------------------

    def on_disconnect(self, event: Event) -> None:
        if event.client is not None:
            event.client.del_var(self, "checked")

    async def on_seen(self, event: Event) -> None:
        client = event.client
        if client is None or not client.ip:
            return
        ip = client.ip.strip()
        if client.get_var(self, "checked") == ip:
            return  # `CLIENT_UPDATE` arrives again and again; one check per address is enough
        client.set_var(self, "checked", ip)
        if self.exempt(client):
            return
        verdict = await self.verdict(ip)
        if verdict is None:
            return
        # A lookup takes a moment, and in it the player may have left and somebody else taken the
        # slot, or the address may have changed. Only the player who was checked is acted on.
        current = self.console.clients.get_by_cid(client.cid or "")
        if current is client and (client.ip or "").strip() == ip:
            self.act(client, verdict)

    def exempt(self, client: Client) -> bool:
        if client.max_level() > as_level(self.settings.get("max_level"), 1):
            return True
        guids = self.settings.get("allow_guids") or []
        return isinstance(guids, (list, tuple)) and client.guid in {str(g) for g in guids}

    async def verdict(self, ip: str) -> Verdict | None:
        """Why this address is flagged, or None. The lists first, the lookup only if they are silent."""
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            log.warning("vpnblocker: %r is not an address I can read", ip)
            return None
        # A dual-stack server reports an IPv4 player as `::ffff:1.2.3.4`, which is in none of the IPv4
        # lists as written. Read as the address it is, or every list is one spelling away from empty.
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if not address.is_global or address in self.allow:
            return None
        found = self.local_verdict(address)
        if found is None and self.lookup_enabled():
            found = await self.lookup(str(address))
        return found

    def local_verdict(
        self, address: ipaddress.IPv4Address | ipaddress.IPv6Address
    ) -> Verdict | None:
        if address in self.blocks:
            return Verdict(source="your own block list")
        for url, networks in self.lists.items():
            if address in networks:
                return Verdict(source=self.describe_list(url))
        asn = self.asn_of(str(address))
        if asn is not None and asn in self.asns:
            return Verdict(source=f"AS{asn}")
        return None

    @staticmethod
    def describe_list(url: str) -> str:
        if "torproject" in url:
            return "the Tor exit list"
        if "lists_vpn" in url:
            return "the VPN list"
        return urllib.parse.urlsplit(url).netloc or url

    def asn_of(self, ip: str) -> int | None:
        if not self.asns:
            return None
        geolocation = self.console.get_plugin("geolocation")
        reader = getattr(geolocation, "asn_reader", None)
        if reader is None:
            return None
        try:
            record = reader.get(ip)
        except Exception:  # noqa: BLE001 - an unreadable record is "unknown", not a fault
            return None
        number = record.get("autonomous_system_number") if isinstance(record, dict) else None
        return number if isinstance(number, int) else None

    async def lookup(self, ip: str) -> Verdict | None:
        """Ask proxycheck.io, or answer from what it said last time."""
        now = self.console.clock.now()
        ttl = as_float(self.settings.get("cache_hours"), 72) * 3600
        remembered = self.lookups.get(ip)
        if remembered is not None and now - remembered[0] < ttl:
            return remembered[1]
        # Two players on one address at once share one lookup and both wait for it. Answering the
        # second with None while the first was still out would let a pair of evaders in together.
        pending = self._pending.get(ip)
        if pending is None:
            pending = asyncio.ensure_future(self._ask(ip, now))
            self._pending[ip] = pending
            pending.add_done_callback(lambda _done: self._pending.pop(ip, None))
        return await asyncio.shield(pending)

    async def _ask(self, ip: str, now: float) -> Verdict | None:
        payload = await asyncio.to_thread(self._query, ip)
        if payload is None:
            return None  # not remembered: a failure is not an answer, so the next join asks again
        found = verdict_from_lookup(
            payload, ip, block_hosting=bool(self.settings.get("block_hosting"))
        )
        self.lookups[ip] = (now, found)
        return found

    def _query(self, ip: str) -> Any:
        """Blocking, and called through a worker thread. None on any failure."""
        params = {"tag": "0"}
        key = str(self.settings.get("api_key") or "").strip()
        if key:
            params["key"] = key
        url = LOOKUP_URL.format(ip=urllib.parse.quote(ip), query=urllib.parse.urlencode(params))
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "b3ng"})  # noqa: S310
            with urllib.request.urlopen(request, timeout=LOOKUP_TIMEOUT) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("vpnblocker: the lookup for %s failed (%s); letting them in", ip, exc)
            return None
        if isinstance(payload, dict) and payload.get("status") in ("denied", "error"):
            log.warning("vpnblocker: proxycheck.io refused a lookup: %s", payload.get("message"))
            return None
        return payload

    def act(self, client: Client, verdict: Verdict) -> None:
        action = self.action()
        log.info("vpnblocker: %s (%s) is on %s; %s", client.name, client.ip, verdict.source, action)
        if self.settings.get("tell_admins"):
            line = self.message(
                "vpnblocker_admins",
                name=client.name,
                source=verdict.source,
                action="kicked" if action == "kick" else "watching",
            )
            level = as_level(self.settings.get("admin_level"), 20)
            for admin in self.console.clients.connected():
                if admin is not client and admin.max_level() >= level:
                    self.console.tell(admin, line)
        if action == "kick":
            self.console.kick(client, reason=self.message("vpnblocker_kicked"))

    # -- commands ------------------------------------------------------------

    @command("vpncheck", level=20)
    async def cmd_vpncheck(self, ctx: CommandContext) -> None:
        """vpncheck <player> - whether somebody is on a VPN, and which list says so"""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("vpnblocker_usage"))
            return
        target = self.resolve_client(ctx, handle)
        if target is None:
            return
        if not target.ip:
            ctx.reply(self.message("vpnblocker_unknown", name=target.name))
            return
        if self.exempt(target):
            ctx.reply(self.message("vpnblocker_exempt", name=target.name))
            return
        found = await self.verdict(target.ip.strip())
        if found is None:
            ctx.reply(self.message("vpnblocker_clean", name=target.name, ip=target.ip))
        else:
            ctx.reply(
                self.message(
                    "vpnblocker_flagged", name=target.name, ip=target.ip, source=found.source
                )
            )


__all__ = [
    "DEFAULTS",
    "DEFAULT_LISTS",
    "MESSAGES",
    "NetworkSet",
    "Verdict",
    "VpnblockerPlugin",
    "parse_asns",
    "parse_list",
    "verdict_from_lookup",
]
