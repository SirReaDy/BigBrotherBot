"""The `countryfilter` plugin — deciding who may play by where they are.

No captured tests exist. The interesting part is the truth table (Apache's two orderings, with `all` in
either list) and one fault that would empty a server: the classic tested list membership with
`self.cf_deny_from.find(cc)`, and `str.find("")` is 0 — so a player whose record carried **no country
code** was "in" the deny list and got kicked, on any server with a deny list at all.
"""

from __future__ import annotations

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.countryfilter import ALLOW_DENY, CountryfilterPlugin, codes
from b3.plugins.geolocation import GeolocationPlugin, Location

GERMANY = Location(country="Germany", country_code="DE")
CHINA = Location(country="China", country_code="CN")
#: An address the database placed but could not name a country for — a new range, satellite, carrier
#: NAT. The classic's success event fires for a record like this.
NOWHERE = Location(city="Somewhere")


def _plugin(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    geo = GeolocationPlugin(console, {"settings": {}})
    geo.start()
    plugin = CountryfilterPlugin(console, {"settings": settings})
    console.plugins = {"admin": admin, "geolocation": geo, "countryfilter": plugin}
    plugin.start()
    return plugin


def _join(console, name, ip="", bits=0, cid=None):  # noqa: ANN001, ANN202
    slot = cid or str(len(console.clients.connected()) + 1)
    client = Client(
        guid=f"guid{slot}".ljust(32, "0"),
        name=name,
        cid=slot,
        id=int(slot),
        group_bits=bits,
        ip=ip,
    )
    console.clients.add(client)
    return client


async def _arrives(console, client, place):  # noqa: ANN001, ANN202
    await console.bus.publish(
        Event(EventType.CLIENT_GEOLOCATION_SUCCESS, client=client, data=place)
    )


def _kicked(console):  # noqa: ANN001, ANN202
    return [client.name for client, _reason, _admin in console.kicked]


# -- reading the lists ---------------------------------------------------------------------------


def test_a_list_of_codes_in_either_spelling():
    """Operators have the second one already: the classic's config held one string."""
    assert codes(["de", "CN"]) == {"DE", "CN"}
    assert codes("CN, RU") == {"CN", "RU"}
    assert codes("all") == {"ALL"}
    assert codes([]) == set()
    assert codes(None) == set()
    assert codes(42) == set()


# -- the truth table -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deny_allow_lets_everybody_in_by_default(console):
    _plugin(console, startup_silence=0)
    joe = _join(console, "Joe")

    await _arrives(console, joe, CHINA)

    assert console.kicked == []
    assert console.said == ["Joe is connecting from China"]


@pytest.mark.asyncio
async def test_deny_allow_with_a_blocklist(console):
    _plugin(console, deny_from=["CN"], startup_silence=0)
    joe, ann = _join(console, "Joe"), _join(console, "Ann")

    await _arrives(console, joe, CHINA)
    await _arrives(console, ann, GERMANY)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_deny_all_with_an_exception(console):
    """`deny_from: all` plus `allow_from: DE` is a whitelist written the other way round."""
    _plugin(console, deny_from=["all"], allow_from=["DE"], startup_silence=0)
    joe, ann = _join(console, "Joe"), _join(console, "Ann")

    await _arrives(console, joe, CHINA)
    await _arrives(console, ann, GERMANY)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_allow_deny_is_a_whitelist(console):
    _plugin(console, order=ALLOW_DENY, allow_from=["DE"], startup_silence=0)
    joe, ann = _join(console, "Joe"), _join(console, "Ann")

    await _arrives(console, joe, CHINA)
    await _arrives(console, ann, GERMANY)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_allow_deny_denies_what_is_in_both_lists(console):
    _plugin(console, order=ALLOW_DENY, allow_from=["DE", "CN"], deny_from=["CN"], startup_silence=0)
    joe = _join(console, "Joe")

    await _arrives(console, joe, CHINA)

    assert _kicked(console) == ["Joe"]


def test_a_junk_order_falls_back_to_the_classics_default(console):
    plugin = _plugin(console, order="sideways")

    assert plugin.settings["order"] == "deny,allow"


def test_the_order_may_be_written_with_a_space(console):
    plugin = _plugin(console, order="allow, deny")

    assert plugin.settings["order"] == ALLOW_DENY


def test_codes_are_compared_not_searched_for(console):
    """`find` also meant the config's *format* changed the behaviour: `CNRU` matched `NR`."""
    plugin = _plugin(console, deny_from="CNRU")

    assert plugin.country_allowed("NR") is True
    assert plugin.country_allowed("CNRU") is False  # what that config actually says


# -- the fault -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_player_with_no_country_code_is_not_denied(console):
    """`str.find("")` is 0, so an unplaceable address was in every list the classic checked."""
    _plugin(console, deny_from=["CN", "RU"], startup_silence=0)
    joe = _join(console, "Joe")

    await _arrives(console, joe, NOWHERE)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_player_with_no_country_code_is_not_allowed_either(console):
    """The other half of the same rule: it is not a country, so it is in neither list."""
    _plugin(console, order=ALLOW_DENY, allow_from=["DE"], startup_silence=0)
    joe = _join(console, "Joe")

    await _arrives(console, joe, NOWHERE)

    assert _kicked(console) == ["Joe"]


# -- exemptions ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_registered_player_is_exempt(console):
    _plugin(console, deny_from=["CN"], max_level=1, startup_silence=0)
    reg = _join(console, "Reg", bits=2)

    await _arrives(console, reg, CHINA)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_exempt_address(console):
    _plugin(console, deny_from=["CN"], exempt_ips=["8.8.8.8"], startup_silence=0)
    joe = _join(console, "Joe", ip="8.8.8.8")

    await _arrives(console, joe, CHINA)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_exempt_name_works_and_is_spoofable_by_design(console):
    """Ported because operators use it; the config says plainly that a name is not proof of anything.

    Colour codes are stripped from both sides, because `^1Bob` and `Bob` are one name in the server.
    """
    _plugin(console, deny_from=["CN"], exempt_names=["Bob"], startup_silence=0)
    bob = _join(console, "^1B^2o^3b")

    await _arrives(console, bob, CHINA)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_blocked_address_beats_the_country_lists(console):
    _plugin(console, allow_from=["all"], blocked_ips=["10.0.0.1"], startup_silence=0)
    joe = _join(console, "Joe", ip="10.0.0.1")

    await _arrives(console, joe, GERMANY)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_an_exemption_beats_a_blocked_address(console):
    """The classic's order, and the useful one."""
    _plugin(console, blocked_ips=["10.0.0.1"], exempt_ips=["10.0.0.1"], startup_silence=0)
    joe = _join(console, "Joe", ip="10.0.0.1")

    await _arrives(console, joe, GERMANY)

    assert console.kicked == []


# -- what it says --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_announcements_can_be_switched_off(console):
    _plugin(
        console,
        deny_from=["CN"],
        announce_accepted=False,
        announce_rejected=False,
        startup_silence=0,
    )
    joe, ann = _join(console, "Joe"), _join(console, "Ann")

    await _arrives(console, joe, CHINA)
    await _arrives(console, ann, GERMANY)

    assert console.said == []
    assert _kicked(console) == ["Joe"]  # still removed, just quietly


@pytest.mark.asyncio
async def test_countries_that_arrive_constantly_can_be_kept_quiet(console):
    _plugin(console, quiet_countries=["DE"], startup_silence=0)
    joe, ann = _join(console, "Joe"), _join(console, "Ann")

    await _arrives(console, ann, GERMANY)
    await _arrives(console, joe, CHINA)

    assert console.said == ["Joe is connecting from China"]


@pytest.mark.asyncio
async def test_the_startup_silence_covers_both_announcements(console):
    """The classic checked it before announcing an accepted player and not a rejected one, so a bot
    restarting on a full server was silent about who it let in and loud about who it removed."""
    _plugin(console, deny_from=["CN"], startup_silence=300)
    joe, ann = _join(console, "Joe"), _join(console, "Ann")

    await _arrives(console, joe, CHINA)
    await _arrives(console, ann, GERMANY)
    assert console.said == []
    assert _kicked(console) == ["Joe"]  # the filter still applies; only the announcement waits

    console.clock.advance(301)
    sue = _join(console, "Sue")
    await _arrives(console, sue, GERMANY)
    assert console.said == ["Sue is connecting from Germany"]


@pytest.mark.asyncio
async def test_a_country_the_database_could_not_name_is_still_announced_as_something(console):
    _plugin(console, order=ALLOW_DENY, allow_from=["DE"], startup_silence=0)
    joe = _join(console, "Joe")

    await _arrives(console, joe, NOWHERE)

    assert console.said == [
        "Joe was refused: this server does not accept players from an unknown country"
    ]
