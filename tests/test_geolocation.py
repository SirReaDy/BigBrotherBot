"""The `geolocation` plugin — where a player is connecting from.

There are no captured tests for this one, and the classic's own design is what these tests are about.
It had four backends tried in a fixed order — `ip-api.com`, `telize.com`, `freegeoip.net`, then a local
MaxMind database **last** — of which two have since shut down, so on a current network every arriving
player cost two doomed HTTP requests before anything useful was tried. The local database it shipped in
the repository was MaxMind's legacy format, discontinued in 2018.

So the tests here are about a local database read: the record shapes the two current vendors publish
(they differ, and both have to work), and what happens when there is no database, no library, or no
answer for an address. The reader is a Protocol with one method, so none of this needs `maxminddb`
installed — which is the point of it being optional.
"""

from __future__ import annotations

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.geolocation import (
    GeolocationPlugin,
    Location,
    isp_from_record,
    location_from_record,
)

#: A GeoLite2-City record, trimmed to the keys this reads. Real shape, from MaxMind's own docs.
CITY_RECORD = {
    "city": {"names": {"en": "Córdoba"}},
    "continent": {"code": "SA", "names": {"en": "South America"}},
    "country": {"iso_code": "AR", "names": {"en": "Argentina"}},
    "location": {
        "latitude": -31.4135,
        "longitude": -64.1811,
        "time_zone": "America/Argentina/Cordoba",
    },
    "postal": {"code": "5000"},
    "subdivisions": [{"iso_code": "X", "names": {"en": "Cordoba Province"}}],
}

#: DB-IP's "IP to Country Lite" — the file that needs no account. Country and continent only.
COUNTRY_RECORD = {
    "continent": {"code": "EU", "names": {"en": "Europe"}},
    "country": {"iso_code": "DE", "names": {"en": "Germany"}},
}


class FakeReader:
    """One method, as `maxminddb.Reader` has for this purpose."""

    def __init__(self, answers: dict[str, object] | None = None, raises: bool = False) -> None:
        self.answers = answers or {}
        self.raises = raises
        self.closed = False
        self.asked: list[str] = []

    def get(self, ip: str) -> object:
        self.asked.append(ip)
        if self.raises:
            raise ValueError(f"{ip} is not an IP address")
        return self.answers.get(ip)

    def close(self) -> None:
        self.closed = True


def _plugin(console, reader=None, asn_reader=None, **settings):  # noqa: ANN001, ANN202
    plugin = GeolocationPlugin(console, {"settings": settings})
    plugin.reader = reader
    plugin.asn_reader = asn_reader
    plugin.start()
    return plugin


def _join(console, name, ip="", cid=None):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), ip=ip)
    console.clients.add(client)
    return client


async def _auth(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))


# -- reading a record ----------------------------------------------------------------------------


def test_a_city_database_record():
    place = location_from_record(CITY_RECORD)

    assert place.country == "Argentina"
    assert place.country_code == "AR"
    assert place.city == "Cordoba"  # folded to ASCII
    assert place.region == "Cordoba Province"
    assert place.region_code == "X"
    assert place.postcode == "5000"
    assert place.timezone == "America/Argentina/Cordoba"
    assert place.latitude == pytest.approx(-31.4135)
    assert place.longitude == pytest.approx(-64.1811)


def test_a_country_only_database_record():
    """DB-IP Lite is the file an operator can have without an account, and it says less."""
    place = location_from_record(COUNTRY_RECORD)

    assert place.country == "Germany"
    assert place.country_code == "DE"
    assert place.city == ""
    assert place.latitude is None
    assert bool(place) is True


def test_place_names_can_keep_their_accents():
    """Off for a reason: the Quake 3 and Call of Duty consoles cannot render anything but ASCII, and
    a row of question marks is worse than "Cordoba"."""
    assert location_from_record(CITY_RECORD, ascii_only=False).city == "Córdoba"


def test_a_record_that_is_not_a_record():
    assert bool(location_from_record(None)) is False
    assert bool(location_from_record("nonsense")) is False
    assert bool(location_from_record({})) is False


def test_a_record_missing_the_parts_this_reads():
    """A file the operator supplied is data, not a contract: a missing key is not an error."""
    place = location_from_record({"country": "Germany", "subdivisions": ["x"], "location": 3})

    assert bool(place) is False


def test_an_asn_record():
    assert isp_from_record({"autonomous_system_organization": "Deutsche Telekom AG"}) == (
        "Deutsche Telekom AG"
    )
    assert isp_from_record({}) == ""
    assert isp_from_record(None) == ""


def test_what_a_location_reads_as():
    assert Location(city="Cordoba", country="Argentina").describe() == "Cordoba (Argentina)"
    assert Location(country="Germany").describe() == "Germany"
    assert Location(country_code="DE").describe() == "DE"
    assert Location().describe() == ""


# -- placing a player ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_arriving_player_is_placed_and_the_answer_published(console):
    plugin = _plugin(console, reader=FakeReader({"1.2.3.4": COUNTRY_RECORD}))
    seen: list[Event] = []
    console.bus.subscribe(EventType.CLIENT_GEOLOCATION_SUCCESS, lambda event: seen.append(event))
    joe = _join(console, "Joe", ip="1.2.3.4")

    await _auth(console, joe)
    await console.bus.drain()

    placed = plugin.location_of(joe)
    assert placed is not None
    assert placed.country == "Germany"
    assert len(seen) == 1
    assert seen[0].data is placed


@pytest.mark.asyncio
async def test_an_address_the_database_does_not_know(console):
    plugin = _plugin(console, reader=FakeReader({}))
    failures: list[Event] = []
    console.bus.subscribe(
        EventType.CLIENT_GEOLOCATION_FAILURE, lambda event: failures.append(event)
    )
    joe = _join(console, "Joe", ip="10.0.0.1")

    await _auth(console, joe)
    await console.bus.drain()

    assert plugin.location_of(joe) is None
    assert len(failures) == 1


@pytest.mark.asyncio
async def test_a_player_with_no_address_yet_is_not_looked_up(console):
    """On Call of Duty and Quake 3 the line that authenticates a player carries no address."""
    reader = FakeReader({})
    _plugin(console, reader=reader)
    joe = _join(console, "Joe", ip="")

    await _auth(console, joe)

    assert reader.asked == []


@pytest.mark.asyncio
async def test_the_address_arriving_later_is_looked_up_then(console):
    plugin = _plugin(console, reader=FakeReader({"1.2.3.4": COUNTRY_RECORD}))
    joe = _join(console, "Joe", ip="")
    await _auth(console, joe)

    joe.ip = "1.2.3.4"
    await console.bus.publish(Event(EventType.CLIENT_UPDATE, client=joe))

    assert plugin.location_of(joe) is not None


@pytest.mark.asyncio
async def test_somebody_already_placed_is_not_looked_up_again(console):
    reader = FakeReader({"1.2.3.4": COUNTRY_RECORD})
    _plugin(console, reader=reader)
    joe = _join(console, "Joe", ip="1.2.3.4")

    await _auth(console, joe)
    await console.bus.publish(Event(EventType.CLIENT_UPDATE, client=joe))
    await console.bus.publish(Event(EventType.CLIENT_UPDATE, client=joe))

    assert reader.asked == ["1.2.3.4"]


@pytest.mark.asyncio
async def test_leaving_forgets_where_they_were(console):
    plugin = _plugin(console, reader=FakeReader({"1.2.3.4": COUNTRY_RECORD}))
    joe = _join(console, "Joe", ip="1.2.3.4")
    await _auth(console, joe)

    await console.bus.publish(Event(EventType.CLIENT_DISCONNECT, client=joe))

    assert plugin.location_of(joe) is None


@pytest.mark.asyncio
async def test_the_isp_comes_from_the_second_database(console):
    plugin = _plugin(
        console,
        reader=FakeReader({"1.2.3.4": COUNTRY_RECORD}),
        asn_reader=FakeReader({"1.2.3.4": {"autonomous_system_organization": "Telekom"}}),
    )
    joe = _join(console, "Joe", ip="1.2.3.4")

    await _auth(console, joe)

    placed = plugin.location_of(joe)
    assert placed is not None
    assert placed.isp == "Telekom"
    assert placed.country == "Germany"


@pytest.mark.asyncio
async def test_without_an_asn_database_the_isp_is_simply_unknown(console):
    plugin = _plugin(console, reader=FakeReader({"1.2.3.4": COUNTRY_RECORD}))
    joe = _join(console, "Joe", ip="1.2.3.4")

    await _auth(console, joe)

    placed = plugin.location_of(joe)
    assert placed is not None
    assert placed.isp == ""


# -- when it cannot work -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_no_database_nothing_happens_and_nothing_breaks(console):
    plugin = _plugin(console, database="")  # explicit: a bundled database ships with the bot now
    joe = _join(console, "Joe", ip="1.2.3.4")

    await _auth(console, joe)

    assert plugin.location_of(joe) is None


@pytest.mark.asyncio
async def test_a_reader_that_raises_does_not_take_the_handler_with_it(console):
    """A malformed address, or a corrupt record, is not a reason for the bot to stop moderating."""
    plugin = _plugin(console, reader=FakeReader(raises=True))
    joe = _join(console, "Joe", ip="not-an-address")

    await _auth(console, joe)

    assert plugin.location_of(joe) is None


def test_a_database_path_that_does_not_exist_is_reported_not_raised(console):
    plugin = GeolocationPlugin(console, {"settings": {"database": "/nowhere/at/all.mmdb"}})
    plugin.start()

    assert plugin.reader is None


def test_an_empty_database_setting_switches_the_plugin_off(console):
    """The way to have no geolocation, now that a database ships with the bot."""
    plugin = GeolocationPlugin(console, {"settings": {"database": ""}})
    plugin.start()

    assert plugin.reader is None
    assert plugin.asn_reader is None


def test_disabling_closes_the_files(console):
    reader, asn = FakeReader(), FakeReader()
    plugin = _plugin(console, reader=reader, asn_reader=asn)

    plugin.disable()

    assert reader.closed is True
    assert asn.closed is True
    assert plugin.reader is None


def test_a_database_named_relative_to_the_config_is_found(console, tmp_path, caplog):
    """`@conf/GeoLite2-City.mmdb` is how an instance names a file beside its own config.

    It is the convention every other path in this bot uses — banlist's lists, the status file, the
    sqlite database — and this plugin was the one that did not honour it. An operator who followed it
    was told `database '@conf/GeoLite2-City.mmdb' does not exist`, which sends them looking for a
    directory called `@conf`.
    """
    conf = tmp_path / "instance"
    conf.mkdir()
    (conf / "GeoLite2-City.mmdb").write_bytes(b"a real file, if not a real database")
    console.config_path = conf / "b3.yaml"

    plugin = GeolocationPlugin(console, {"settings": {"database": "@conf/GeoLite2-City.mmdb"}})
    with caplog.at_level("ERROR"):
        plugin.open_database("@conf/GeoLite2-City.mmdb")

    assert "does not exist" not in caplog.text, "the file is beside the config, where it was named"
    assert "@conf" not in caplog.text, (
        "and the token is never reported back as though it were a path"
    )


def test_a_fresh_install_can_place_a_player_with_nothing_configured(console):
    """The reason a database ships with this bot at all.

    The classic bot's answer to "where is this player" was three web services, two of which have since
    shut down; ours is a file, and a file nobody has is not an answer either. So one is carried, and
    the default setting names it.
    """
    plugin = GeolocationPlugin(console, {"settings": {}})
    plugin.start()

    assert plugin.reader is not None, "a fresh install places players without being configured"
    record = plugin.reader.get("8.8.8.8")
    assert record is not None, "and the database it carries answers"


def test_the_instance_copy_is_preferred_over_the_bundled_one(tmp_path, console):
    """`b3 init` downloads the current month's file; without this it would never be read.

    The bundled copy is only ever as fresh as the release that carried it, and a stale database does
    not say "I do not know" — it says the country the address used to be in.
    """
    from b3.plugins.geolocation import BUNDLED_DATABASE, INSTANCE_DATABASE

    conf = tmp_path / "instance"
    conf.mkdir()
    console.config_path = conf / "b3.yaml"
    plugin = GeolocationPlugin(console, {"settings": {}})
    plugin.on_load_config()

    assert plugin.database_path() == BUNDLED_DATABASE, "nothing downloaded yet"

    (conf / "dbip-country-lite.mmdb").write_bytes(b"a file where init would put one")
    assert plugin.database_path() == INSTANCE_DATABASE, "the downloaded one wins once it is there"


def test_a_database_an_operator_named_is_never_silently_swapped(tmp_path, console):
    """No fallback for a path somebody wrote themselves: they named a file, and reading a different
    one would be worse than saying theirs is missing."""
    console.config_path = tmp_path / "b3.yaml"
    plugin = GeolocationPlugin(console, {"settings": {"database": "@conf/GeoLite2-City.mmdb"}})
    plugin.on_load_config()

    assert plugin.database_path() == "@conf/GeoLite2-City.mmdb"
