"""Keeping the IP-to-country database current — the part that touches the network.

The database ships with the bot so `geolocation` works on a fresh install, and `b3 init` replaces it
with the current month's file. Everything here is about the second half being **safe to run inside a
command somebody is waiting on**: every failure is survivable, nothing retries in a loop, and the
bundled copy is what an install falls back to.

No test here reaches DB-IP. The download is exercised against bytes this file makes up, which is what
lets the failures — a 404, a truncated body, something that is not a database — be tested at all.
"""

from __future__ import annotations

import datetime
import gzip
from pathlib import Path

import pytest

from b3.core import geoipdata

REAL = geoipdata.BUNDLED_PATH


def test_the_database_is_actually_shipped():
    """The whole promise: an install can place a player before it has downloaded anything."""
    assert REAL.is_file(), f"{REAL} is missing from the package"
    assert REAL.stat().st_size > 1_000_000, "a country database is megabytes, not a stub"


def test_the_shipped_database_is_readable_and_answers():
    maxminddb = pytest.importorskip("maxminddb")
    with maxminddb.open_database(str(REAL)) as reader:
        assert reader.metadata().database_type.startswith("DBIP")
        record = reader.get("8.8.8.8")
    assert record is not None
    assert record["country"]["iso_code"] == "US"


def test_the_url_is_the_month(monkeypatch):
    """One variable, which is what makes a new file findable without an index or an account."""
    assert geoipdata.url_for(2026, 8).endswith("dbip-country-lite-2026-08.mmdb.gz")
    assert geoipdata.url_for(2026, 12).endswith("dbip-country-lite-2026-12.mmdb.gz")


def test_january_looks_back_into_last_year():
    assert geoipdata.previous_month(2026, 1) == (2025, 12)


def _served(monkeypatch, bodies: dict[str, bytes]):
    """Answer only the URLs named; everything else is a miss, as a 404 would be."""
    asked: list[str] = []

    def fake_fetch(url: str, *, timeout: float = 0.0) -> bytes | None:
        asked.append(url)
        return bodies.get(url)

    monkeypatch.setattr(geoipdata, "fetch", fake_fetch)
    return asked


def test_the_current_month_is_downloaded(tmp_path, monkeypatch):
    today = datetime.date(2026, 8, 20)
    url = geoipdata.url_for(2026, 8)
    asked = _served(monkeypatch, {url: gzip.compress(REAL.read_bytes())})

    built = geoipdata.download(tmp_path / "db.mmdb", today=today)

    assert built is not None
    assert asked == [url], "one request, and no retry loop"
    assert (tmp_path / "db.mmdb").is_file()


def test_the_first_days_of_a_month_fall_back_to_the_one_before(tmp_path, monkeypatch):
    """The new file does not appear at midnight on the 1st. Without this, every instance created in
    the first days of a month would download nothing and say it could not reach DB-IP."""
    today = datetime.date(2026, 9, 2)
    last_month = geoipdata.url_for(2026, 8)
    asked = _served(monkeypatch, {last_month: gzip.compress(REAL.read_bytes())})

    built = geoipdata.download(tmp_path / "db.mmdb", today=today)

    assert built is not None
    assert asked == [geoipdata.url_for(2026, 9), last_month], "this month first, then last month"


def test_a_server_that_cannot_be_reached_leaves_what_was_there(tmp_path, monkeypatch):
    """The failure an operator behind a firewall gets, and it must not cost them the database."""
    destination = tmp_path / "db.mmdb"
    destination.write_bytes(REAL.read_bytes())
    _served(monkeypatch, {})

    changed, said = geoipdata.refresh(destination, bundled=REAL)

    assert changed is False
    assert "could not reach" in said
    assert destination.read_bytes() == REAL.read_bytes(), "the old database is still a database"


def test_something_that_is_not_a_database_is_refused(tmp_path, monkeypatch):
    """A redirect to an error page gzips just as well as a database does."""
    destination = tmp_path / "db.mmdb"
    destination.write_bytes(b"the database that was already here")
    url = geoipdata.url_for(*geoipdata.current_month())
    _served(monkeypatch, {url: gzip.compress(b"<html>not found</html>")})

    built = geoipdata.download(destination)

    assert built is None
    assert destination.read_bytes() == b"the database that was already here"
    assert not list(tmp_path.glob("*.part")), "and the half-written file is cleaned up"


def test_a_body_too_large_is_not_unpacked(monkeypatch):
    """A gzip bomb, or a redirect to something enormous."""
    monkeypatch.setattr(geoipdata, "MAX_DOWNLOAD", 16)

    class Response:
        def read(self, size: int) -> bytes:
            return b"x" * size

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(geoipdata.urllib.request, "urlopen", lambda *a, **k: Response())
    assert geoipdata.fetch("https://example.invalid/x") is None


def test_an_interrupted_write_cannot_replace_a_working_database(tmp_path, monkeypatch):
    """Written whole and moved into place, so there is no moment where the file is half a database."""
    destination = tmp_path / "db.mmdb"
    destination.write_bytes(REAL.read_bytes())
    url = geoipdata.url_for(*geoipdata.current_month())
    _served(monkeypatch, {url: b"not gzip at all"})

    assert geoipdata.download(destination) is None
    assert geoipdata.build_date(destination) is not None, "still the database it was"


def test_refresh_says_what_happened_in_words_an_operator_can_act_on(tmp_path, monkeypatch):
    url = geoipdata.url_for(*geoipdata.current_month())
    _served(monkeypatch, {url: gzip.compress(REAL.read_bytes())})

    changed, said = geoipdata.refresh(tmp_path / "db.mmdb", bundled=REAL)

    assert changed is True
    assert "database" in said and any(ch.isdigit() for ch in said), said


def test_the_build_date_of_something_unreadable_is_unknown_rather_than_an_error(tmp_path):
    junk = tmp_path / "not.mmdb"
    junk.write_bytes(b"nope")
    assert geoipdata.build_date(junk) is None
    assert geoipdata.build_date(Path(tmp_path / "missing.mmdb")) is None
