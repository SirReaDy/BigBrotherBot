"""Works out where a player is connecting from, and tells the rest of the bot.

A port of the classic `geolocation` plugin: it resolves an address to a country (and a city, where the
database has one) and publishes the result, so `location`, `geowelcome` and `countryfilter` can act on
it. It answers no commands and says nothing in game itself.

The whole of the port is in **where the answer comes from**, which is the one thing the classic got
wrong in a way that has only got worse with time.

It carried four backends and used them in a fixed order: `ip-api.com`, then `telize.com`, then
`freegeoip.net`, then a local MaxMind database — which was tried **last**. Two of those three web
services have since shut down, so on a current network every arriving player cost two doomed HTTP
requests, each with its own timeout, before anything useful was tried. The local database was also
*shipped inside the repository* as `GeoIP.dat`: MaxMind's legacy format, which stopped being updated in
2018 and whose downloads were withdrawn in 2019. Porting that file would mean shipping data that
answers with the wrong country for whole ranges.

So: **one source, a local MaxMind-format database the operator points at.** Two vendors publish one —
DB-IP's "IP to Country Lite" (a monthly `.mmdb`, no account, CC-BY) and MaxMind's GeoLite2 (a free
account and a licence key, not redistributable). Either works; the reader is the same.

What that buys, beyond being the only option that still works:

* **No player's address leaves the machine.** The classic sent every arriving player's IP to a company
  neither the operator nor the player chose, on plain HTTP.
* **No thread.** A memory-mapped database read takes microseconds, so there is nothing to move off the
  event loop — where the classic started a thread per player per event, including on every
  `CLIENT_UPDATE`, which is once more per address resolution.
* **No network failure mode at all**: no timeouts, no rate limits, nothing to be down.

Kept from the classic: resolving on authentication and again when an address becomes known, not
re-resolving somebody already placed, the ASCII folding of place names (a Quake 3 console cannot render
`Córdoba`, and a name it cannot render is worse than one spelled plainly), and the two events.
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Protocol

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Which language's place names to prefer out of a record's `names` table. English, because that is the
#: only one every one of these databases carries.
NAME_LANGUAGE = "en"

#: The database this ships with, named the way any other path setting would be. `@b3` is the
#: installed package directory, so this resolves wherever b3 is installed and needs nothing from the
#: operator: geolocation works on a fresh install, which is the whole reason for carrying a copy.
#: `b3 init` replaces it with a current download when it can reach DB-IP, because a bundled file is
#: only ever as fresh as the release that carried it.
BUNDLED_DATABASE = "@b3/data/dbip-country-lite.mmdb"

#: Where `b3 init` puts the current download: `~/.b3`, shared by every instance on this machine.
#: Preferred over the bundled copy when it is there, which is what makes the refresh worth doing —
#: and shared rather than per-instance because every server answers the same question about the same
#: addresses, so a copy each would be the same 8 MB several times over.
SHARED_DATABASE = "@home/dbip-country-lite.mmdb"

DEFAULTS: dict[str, object] = {
    # Path to a MaxMind-format database (`.mmdb`). The bundled DB-IP country file by default; set it
    # to a GeoLite2-City to get cities, or to "" to switch this plugin off entirely.
    "database": SHARED_DATABASE,
    # Optional second database naming the network's operator — GeoLite2-ASN is the one that has it.
    # Without it `isp` is simply unknown, which is a truthful answer.
    "asn_database": "",
    # Fold place names to plain ASCII. On by default and for a real reason: the Quake 3 and Call of
    # Duty consoles cannot render anything else, and a row of question marks is worse than "Cordoba".
    "ascii_only": True,
}


@dataclass(frozen=True, slots=True)
class Location:
    """Where an address is, as far as the database goes.

    Every field is optional and independently so, because the databases differ in what they carry: a
    country-only database (DB-IP Lite, GeoLite2-Country) fills in two of these, a city database most of
    them, and the ASN database only `isp`. The classic's object had the same shape; what it did not have
    was any way to tell "the database does not say" from "there is nothing there".
    """

    country: str = ""
    country_code: str = ""
    region: str = ""
    region_code: str = ""
    city: str = ""
    postcode: str = ""
    timezone: str = ""
    latitude: float | None = None
    longitude: float | None = None
    isp: str = ""

    def __bool__(self) -> bool:
        """True when the database said anything at all about this address."""
        return bool(self.country or self.country_code or self.city or self.isp)

    def describe(self) -> str:
        """A human-readable place: "Cordoba (Argentina)", "Argentina", or "somewhere unknown"."""
        if self.city and self.country:
            return f"{self.city} ({self.country})"
        return self.city or self.country or self.country_code or ""


class Reader(Protocol):
    """The part of `maxminddb.Reader` this plugin uses."""

    def get(self, ip: str) -> Any: ...
    def close(self) -> None: ...


def _ascii(text: str) -> str:
    """`Córdoba` -> `Cordoba`: decompose, drop the combining marks, keep what a console can draw."""
    decomposed = unicodedata.normalize("NFKD", text)
    return (
        "".join(ch for ch in decomposed if not unicodedata.combining(ch))
        .encode("ascii", "ignore")
        .decode("ascii")
    )


def _name(section: Any) -> str:
    """The English name out of a record's `names` table, when there is one."""
    if not isinstance(section, dict):
        return ""
    names = section.get("names")
    if isinstance(names, dict):
        value = names.get(NAME_LANGUAGE) or next(iter(names.values()), "")
        return str(value or "")
    return ""


def location_from_record(record: Any, *, ascii_only: bool = True) -> Location:
    """Read a GeoIP2-shaped record into a `Location`, taking whatever it happens to contain.

    Both vendors' files use this shape, and they carry different subsets of it: DB-IP's country-only
    database has `country` and `continent`, GeoLite2-City adds `city`, `subdivisions`, `location` and
    `postal`. Everything is looked up defensively rather than indexed, because a record is data from a
    file the operator supplied and a missing key is not an error.
    """
    if not isinstance(record, dict):
        return Location()

    def section(key: str) -> dict[str, Any]:
        found = record.get(key)
        return found if isinstance(found, dict) else {}

    country = section("country")
    subdivisions = record.get("subdivisions")
    region_record: dict[str, Any] = (
        subdivisions[0]
        if isinstance(subdivisions, list) and subdivisions and isinstance(subdivisions[0], dict)
        else {}
    )
    position = section("location")
    postal = section("postal")

    def text(value: object) -> str:
        rendered = str(value or "")
        return _ascii(rendered) if ascii_only else rendered

    def number(value: object) -> float | None:
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )

    return Location(
        country=text(_name(country)),
        country_code=str(country.get("iso_code") or "").upper(),
        region=text(_name(region_record)),
        region_code=str(region_record.get("iso_code") or "").upper(),
        city=text(_name(record.get("city"))),
        postcode=str(postal.get("code") or ""),
        timezone=str(position.get("time_zone") or ""),
        latitude=number(position.get("latitude")),
        longitude=number(position.get("longitude")),
    )


def isp_from_record(record: Any, *, ascii_only: bool = True) -> str:
    """The network operator's name out of an ASN database record."""
    if not isinstance(record, dict):
        return ""
    name = str(
        record.get("autonomous_system_organization")
        or record.get("isp")
        or record.get("organization")
        or ""
    )
    return _ascii(name) if ascii_only else name


class GeolocationPlugin(Plugin):
    """Resolves a player's address against a local database and publishes the result."""

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: Set from the config at startup, or injected by a test. None means every lookup is skipped.
        self.reader: Reader | None = None
        self.asn_reader: Reader | None = None

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.reader = self.reader or self.open_database(self.database_path())
        self.asn_reader = self.asn_reader or self.open_database(
            str(self.settings.get("asn_database") or ""), what="ASN database"
        )
        # Both moments, and the second is the one that matters on Call of Duty and Quake 3: the line
        # that authenticates a player carries no address at all.
        self.subscribe(EventType.CLIENT_AUTH, self.on_seen)
        self.subscribe(EventType.CLIENT_UPDATE, self.on_seen)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        if self.reader is None:
            log.warning(
                "geolocation: no database is loaded, so nobody will be placed. Point `database` at a "
                "MaxMind-format .mmdb file — DB-IP's IP to Country Lite needs no account"
            )

    def database_path(self) -> str:
        """Which database to read: this instance's own copy, or the one b3 ships with.

        `b3 init` downloads the current month's file into the instance directory, and that is the one
        to prefer — the bundled copy is only ever as fresh as the release that carried it, and a
        database that has gone stale answers with the country an address used to be in rather than
        admitting it does not know. An instance created before this existed, or one whose download
        could not reach DB-IP, has no such file and falls back to the bundled copy, which is the
        whole reason for carrying one.

        A `database` an operator has set themselves is used as written, with no fallback: they named
        a file, and quietly reading a different one would be worse than saying it is missing.
        """
        configured = str(self.settings.get("database") or "")
        if configured != DEFAULTS["database"]:
            return configured
        if self.resolve_path(configured).is_file():
            return configured
        return BUNDLED_DATABASE

    def open_database(self, path: str, what: str = "database") -> Reader | None:
        """Open an `.mmdb`, or explain once why not. Never raises: this is an optional feature."""
        if not path:
            return None
        # `@conf/GeoLite2-City.mmdb` is how an instance names a file that lives beside its own
        # config, and it is the convention the rest of this bot's paths use. Reported unresolved,
        # the error names a directory called `@conf` that nobody has.
        resolved = self.resolve_path(path)
        if not resolved.is_file():
            log.error(
                "geolocation: %s %r does not exist; nothing will be looked up in it",
                what,
                str(resolved),
            )
            return None
        path = str(resolved)
        try:
            import maxminddb
        except ImportError:
            log.error(
                "geolocation: the `maxminddb` package is not installed, so %r cannot be read. It "
                "is a dependency of this bot, so something has gone wrong with the install: "
                "`pip install --force-reinstall b3ng`",
                path,
            )
            return None
        try:
            reader: Reader = maxminddb.open_database(path)
        except Exception as exc:  # noqa: BLE001 - a bad file must not stop the bot from starting
            log.error("geolocation: %r could not be opened as a MaxMind database (%s)", path, exc)
            return None
        log.info("geolocation: reading %s from %s", what, path)
        return reader

    def on_disable(self) -> None:
        for reader in (self.reader, self.asn_reader):
            if reader is not None:
                try:
                    reader.close()
                except Exception:  # noqa: BLE001 - closing a file must not raise into the loop
                    log.debug("geolocation: a database did not close cleanly", exc_info=True)
        self.reader = None
        self.asn_reader = None

    # -- looking up ----------------------------------------------------------

    def location_of(self, client: Client) -> Location | None:
        """Where this player is, if they have been placed. The public API of this plugin.

        `location`, `geowelcome` and `countryfilter` ask this rather than reading a field off the
        client, which is what the classic did — it set `client.location` on the domain object, so the
        core carried a field only one plugin family understood.
        """
        found = client.get_var(self, "location")
        return found if isinstance(found, Location) else None

    def on_seen(self, event: Event) -> None:
        client = event.client
        if client is None or not client.ip:
            return
        if self.location_of(client) is not None:
            return  # already placed; an address that resolved once does not change under us
        self.locate(client)

    def on_disconnect(self, event: Event) -> None:
        if event.client is not None:
            event.client.del_var(self, "location")

    def locate(self, client: Client) -> Location | None:
        """Look this player up and publish the outcome. Returns the location, or None."""
        if self.reader is None:
            return None
        ascii_only = bool(self.settings.get("ascii_only", True))
        try:
            record = self.reader.get(client.ip)
        except Exception as exc:  # noqa: BLE001 - a malformed address or record is not a bot failure
            log.warning("geolocation: could not look up %s (%s)", client.ip, exc)
            record = None
        location = location_from_record(record, ascii_only=ascii_only)
        if self.asn_reader is not None and location:
            try:
                isp = isp_from_record(self.asn_reader.get(client.ip), ascii_only=ascii_only)
            except Exception as exc:  # noqa: BLE001 - the second database is optional in every sense
                log.warning(
                    "geolocation: could not read the ASN database for %s (%s)", client.ip, exc
                )
            else:
                location = replace(location, isp=isp)
        if not location:
            log.debug("geolocation: %s is not in the database", client.ip)
            self.console.bus.publish_soon(
                Event(EventType.CLIENT_GEOLOCATION_FAILURE, client=client)
            )
            return None
        client.set_var(self, "location", location)
        log.debug("geolocation: %s is connecting from %s", client.name, location.describe())
        self.console.bus.publish_soon(
            Event(EventType.CLIENT_GEOLOCATION_SUCCESS, client=client, data=location)
        )
        return location


__all__ = [
    "DEFAULTS",
    "NAME_LANGUAGE",
    "GeolocationPlugin",
    "Location",
    "Reader",
    "isp_from_record",
    "location_from_record",
]
