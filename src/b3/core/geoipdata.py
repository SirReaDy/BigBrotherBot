"""Keeping the IP-to-country database current, without an account or a scheduled job.

A copy ships with this bot (`b3/data/dbip-country-lite.mmdb`) so `geolocation` works the moment it is
installed. That copy is only ever as fresh as the release that carried it, and **a stale answer here
is a confident wrong one**: an address that changed provider is reported with the country it used to
be in, and nothing anywhere says so. The 2015 database the classic bot shipped placed Cloudflare's
`1.1.1.1` in Australia for years.

DB-IP publish a new file monthly at a predictable URL, free and with no account, so `b3 init` fetches
the current one into the new instance. Three properties make that safe to do from a command an
operator is waiting on:

* **it is optional** — every failure returns None and says why; `init` carries on with the bundled
  copy, because a bot that would not start over a geolocation database would be a worse bot;
* **it is bounded** — one request, a short timeout, and no retry loop; and
* **it is skipped when there is nothing to gain**, which is the common case: the file is monthly, so
  a second `b3 init` in the same month downloads nothing.
"""

from __future__ import annotations

import datetime
import gzip
import logging
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

#: Where DB-IP publish. The month is the only variable, which is what makes this checkable without an
#: index to parse or an API to authenticate against.
URL_TEMPLATE = "https://download.db-ip.com/free/dbip-country-lite-{year:04d}-{month:02d}.mmdb.gz"

#: The name the database is given wherever it is written.
FILENAME = "dbip-country-lite.mmdb"

#: The copy that ships with this bot, which is what an install has before it downloads anything and
#: what it falls back to when it cannot reach DB-IP.
BUNDLED_PATH = Path(__file__).resolve().parent.parent / "data" / FILENAME

#: Where a downloaded database goes: **once per machine, not once per server**. Every instance on a
#: box answers the same question about the same addresses, so a copy each would be the same 8 MB
#: several times over, refreshed on several different days. `~/.b3` is also writable without root and
#: survives upgrading the package, which the directory the bundled copy lives in is not and does not.
SHARED_PATH = Path("~/.b3").expanduser() / FILENAME

#: Seconds. Short: this runs inside `b3 init`, where somebody is watching a prompt.
TIMEOUT = 30.0

#: Refuse a body larger than this. A country database is ~4 MB compressed; ten times that is a
#: redirect to something else, and unpacking it would be the surprise.
MAX_DOWNLOAD = 64 * 1024 * 1024


def current_month(today: datetime.date | None = None) -> tuple[int, int]:
    return ((today or datetime.date.today()).year, (today or datetime.date.today()).month)


def previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def url_for(year: int, month: int) -> str:
    return URL_TEMPLATE.format(year=year, month=month)


def build_date(path: Path) -> datetime.date | None:
    """When the database at `path` was built, or None if it cannot be read as one."""
    try:
        import maxminddb
    except ImportError:
        return None
    try:
        with maxminddb.open_database(str(path)) as reader:
            built = reader.metadata().build_epoch
    except Exception as exc:  # noqa: BLE001 - an unreadable file is not a crash, it is "unknown"
        log.debug("cannot read %s as a database: %s", path, exc)
        return None
    return datetime.datetime.fromtimestamp(built, datetime.UTC).date()


def fetch(url: str, *, timeout: float = TIMEOUT) -> bytes | None:
    """The gzipped body at `url`, or None with the reason logged. Never raises."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "b3"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body: bytes = response.read(MAX_DOWNLOAD + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("geoip: %s could not be fetched: %s", url, exc)
        return None
    if len(body) > MAX_DOWNLOAD:
        log.warning("geoip: %s is larger than %d bytes; not unpacking it", url, MAX_DOWNLOAD)
        return None
    return body


def download(
    destination: Path, *, today: datetime.date | None = None, timeout: float = TIMEOUT
) -> datetime.date | None:
    """Fetch the current month's database into `destination`. Returns its build date, or None.

    The current month is tried first and the one before it second, because the new file does not
    appear at midnight on the 1st: for the first days of a month the current URL is a 404 while last
    month's is perfectly good data.
    """
    year, month = current_month(today)
    for attempt in ((year, month), previous_month(year, month)):
        url = url_for(*attempt)
        body = fetch(url, timeout=timeout)
        if body is None:
            continue
        try:
            raw = gzip.decompress(body)
        except (OSError, EOFError) as exc:
            log.warning("geoip: %s did not unpack: %s", url, exc)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Written whole and then moved, so an interrupted download cannot leave a half a database
        # where a working one used to be.
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(raw)
        built = build_date(temporary)
        if built is None:
            log.warning("geoip: what %s served is not a database this bot can read", url)
            temporary.unlink(missing_ok=True)
            continue
        temporary.replace(destination)
        log.debug("geoip: wrote %s, built %s", destination, built)
        return built
    return None


def refresh(destination: Path, bundled: Path | None = None, **kwargs: object) -> tuple[bool, str]:
    """Update `destination` if a newer database is published. Returns (changed, what happened).

    The message is written to be printed at somebody: `init` shows it, and every outcome — including
    every failure — is one an operator can act on or safely ignore.
    """
    have = build_date(destination) if destination.exists() else None
    if have is None and bundled is not None:
        have = build_date(bundled)

    built = download(destination, **kwargs)  # type: ignore[arg-type]
    if built is None:
        if have is not None:
            return False, (
                f"could not reach DB-IP; keeping the database built {have:%Y-%m-%d}, which still "
                "answers for every address allocated before then"
            )
        return False, "could not reach DB-IP, and there is no database here to fall back on"
    if have is not None and built <= have:
        return True, f"the IP-to-country database is current, built {built:%Y-%m-%d}"
    return True, f"downloaded the IP-to-country database, built {built:%Y-%m-%d}"


__all__ = [
    "BUNDLED_PATH",
    "SHARED_PATH",
    "FILENAME",
    "URL_TEMPLATE",
    "build_date",
    "download",
    "fetch",
    "refresh",
    "url_for",
]
