"""The `location` plugin — announcing where people are, and answering for it.

The classic's captured tests fix two things worth keeping: the exact distance between its two
fixtures (Rome to Mountain View, 10068.18 km, which is a regression test for the Haversine constants)
and the shape of each reply.

They also encode the fault. `getLocationDistance` returned `False` when it could not work a distance
out and a number when it could, and the caller tested `if not distance:` — so **0.0 km** was reported
as a failure. Two players in the same city is not exotic; on a country-level database every pair in
the same country has identical coordinates.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.geolocation import GeolocationPlugin, Location
from b3.plugins.location import LocationPlugin, distance_km

#: The classic's own two fixtures, which is where the 10068.18 comes from.
ROME = Location(
    country="Italy",
    country_code="IT",
    region="Lazio",
    city="Rome",
    latitude=41.9,
    longitude=12.4833,
)
MOUNTAIN_VIEW = Location(
    country="United States",
    country_code="US",
    region="California",
    city="Mountain View",
    latitude=37.386,
    longitude=-122.0838,
    isp="Google Inc.",
)
#: What a country-only database gives you: a name and no position at all.
GERMANY = Location(country="Germany", country_code="DE")


def _plugins(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    geo = GeolocationPlugin(console, {"settings": {}})
    geo.start()
    plugin = LocationPlugin(console, {"settings": settings})
    console.plugins = {"admin": admin, "geolocation": geo, "location": plugin}
    plugin.start()
    return plugin, geo


def _join(console, name, cid=None, bits=20):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"), name=name, cid=slot, id=int(slot), group_bits=bits
    )
    console.clients.add(client)
    console.register_client(name, client)
    return client


def _place(geo, client, location):  # noqa: ANN001, ANN202
    """Put a player on the map, as `geolocation` does when its database answers."""
    client.set_var(geo, "location", location)


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- the distance --------------------------------------------------------------------------------


def test_the_classics_own_distance():
    """Rome to Mountain View. The number is the regression test for the constants."""
    assert distance_km(ROME, MOUNTAIN_VIEW) == pytest.approx(10068.18, abs=0.01)


def test_the_distance_to_yourself_is_nothing_which_is_still_an_answer():
    """The fault: the classic's caller read 0.0 as "could not compute"."""
    assert distance_km(ROME, ROME) == 0.0


def test_no_coordinates_means_no_distance():
    assert distance_km(GERMANY, ROME) is None
    assert distance_km(ROME, GERMANY) is None


# -- announcing ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_arrival_is_announced_with_its_place(console):
    plugin, geo = _plugins(console, startup_silence=0)
    joe = _join(console, "Joe")
    _place(geo, joe, ROME)

    await console.bus.publish(Event(EventType.CLIENT_GEOLOCATION_SUCCESS, client=joe, data=ROME))

    assert console.said == ["Joe connected from Rome (Italy)"]


@pytest.mark.asyncio
async def test_a_country_only_database_still_says_something(console):
    _plugin, geo = _plugins(console, startup_silence=0)
    joe = _join(console, "Joe")
    _place(geo, joe, GERMANY)

    await console.bus.publish(Event(EventType.CLIENT_GEOLOCATION_SUCCESS, client=joe, data=GERMANY))

    assert console.said == ["Joe connected from Germany"]


@pytest.mark.asyncio
async def test_nothing_is_announced_just_after_the_bot_starts(console):
    """A bot restarted mid-match authenticates everybody at once."""
    _plugin, geo = _plugins(console, startup_silence=300)
    joe = _join(console, "Joe")
    _place(geo, joe, ROME)

    await console.bus.publish(Event(EventType.CLIENT_GEOLOCATION_SUCCESS, client=joe, data=ROME))
    assert console.said == []

    console.clock.advance(301)
    await console.bus.publish(Event(EventType.CLIENT_GEOLOCATION_SUCCESS, client=joe, data=ROME))
    assert console.said == ["Joe connected from Rome (Italy)"]


@pytest.mark.asyncio
async def test_the_announcement_can_be_switched_off(console):
    _plugin, geo = _plugins(console, announce=False, startup_silence=0)
    joe = _join(console, "Joe")
    _place(geo, joe, ROME)

    await console.bus.publish(Event(EventType.CLIENT_GEOLOCATION_SUCCESS, client=joe, data=ROME))

    assert console.said == []


# -- !locate -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locate_reports_the_place(console):
    _plugin, geo = _plugins(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill")
    _place(geo, bill, MOUNTAIN_VIEW)

    await _run(console, mike, "!locate Bill")

    assert _told(console, mike) == ["Bill is connected from Mountain View (United States)"]


@pytest.mark.asyncio
async def test_locate_on_somebody_who_has_not_been_placed(console):
    _plugin, _geo = _plugins(console)
    mike, _bill = _join(console, "Mike"), _join(console, "Bill")

    await _run(console, mike, "!locate Bill")

    assert _told(console, mike) == ["I do not know where Bill is connecting from"]


@pytest.mark.asyncio
async def test_locate_with_no_name(console):
    _plugin, _geo = _plugins(console)
    mike = _join(console, "Mike")

    await _run(console, mike, "!locate")

    assert _told(console, mike) == ["name a player: !locate <player>"]


# -- !distance -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distance_between_two_placed_players(console):
    _plugin, geo = _plugins(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill")
    _place(geo, mike, ROME)
    _place(geo, bill, MOUNTAIN_VIEW)

    await _run(console, mike, "!distance Bill")

    assert _told(console, mike) == ["Bill is 10068.18 km away from you"]


@pytest.mark.asyncio
async def test_two_players_in_the_same_place_are_nought_apart(console):
    """The fault, from the player's side: the classic answered "could not compute"."""
    _plugin, geo = _plugins(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill")
    _place(geo, mike, ROME)
    _place(geo, bill, ROME)

    await _run(console, mike, "!distance Bill")

    assert _told(console, mike) == ["Bill is 0.0 km away from you"]


@pytest.mark.asyncio
async def test_distance_to_yourself(console):
    _plugin, geo = _plugins(console)
    mike = _join(console, "Mike")
    _place(geo, mike, ROME)

    await _run(console, mike, "!distance Mike")

    assert _told(console, mike) == ["you are exactly where you are"]


@pytest.mark.asyncio
async def test_distance_with_a_country_only_database(console):
    """Both players are placed and neither has a position: that is a different failure from "who?"."""
    _plugin, geo = _plugins(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill")
    _place(geo, mike, GERMANY)
    _place(geo, bill, GERMANY)

    await _run(console, mike, "!distance Bill")

    assert _told(console, mike) == ["I cannot work out the distance to Bill"]


@pytest.mark.asyncio
async def test_distance_to_somebody_unplaced(console):
    _plugin, geo = _plugins(console)
    mike, _bill = _join(console, "Mike"), _join(console, "Bill")
    _place(geo, mike, ROME)

    await _run(console, mike, "!distance Bill")

    assert _told(console, mike) == ["I cannot work out the distance to Bill"]


# -- !isp ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_isp_reports_the_network(console):
    _plugin, geo = _plugins(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill")
    _place(geo, bill, MOUNTAIN_VIEW)

    await _run(console, mike, "!isp Bill")

    assert _told(console, mike) == ["Bill is on Google Inc."]


@pytest.mark.asyncio
async def test_isp_without_the_asn_database_says_it_does_not_know(console):
    """The classic printed the two characters `--`, which reads as an answer."""
    _plugin, geo = _plugins(console)
    mike, bill = _join(console, "Mike"), _join(console, "Bill")
    _place(geo, bill, ROME)  # placed, but no ISP: that needs a second database

    await _run(console, mike, "!isp Bill")

    assert _told(console, mike) == ["I do not know whose network Bill is on"]


@pytest.mark.asyncio
async def test_isp_needs_a_higher_level_than_the_other_two(console):
    """The classic's config: locate and distance for `user`, isp for `mod`."""
    plugin, _geo = _plugins(console)

    assert plugin.settings["locate_level"] == 1
    assert plugin.settings["isp_level"] == 20
    registered = console.command_registry.get("isp")
    assert registered is not None
    assert registered.min_level == 20


# -- without the provider ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_geolocation_gone_nobody_is_placed_and_nothing_raises(console):
    """The classic *enabled* its required plugins behind the operator's back, and assumed they existed."""
    plugin, _geo = _plugins(console)
    mike, _bill = _join(console, "Mike"), _join(console, "Bill")
    console.plugins.pop("geolocation")

    await _run(console, mike, "!locate Bill")

    assert plugin.provider() is None
    assert _told(console, mike) == ["I do not know where Bill is connecting from"]
