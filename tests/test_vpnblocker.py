"""The `vpnblocker` plugin — keeping VPNs, proxies and Tor off the server.

Nothing here touches the network: the two blocking calls, `_download` and `_query`, are replaced on
the instance, so what is tested is everything around them — what a list may contain, which layer
answers, who is exempt, what a failed lookup does (lets the player in), and that a verdict is
remembered rather than paid for again on the next join.
"""

from __future__ import annotations

import ipaddress

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.vpnblocker import (
    NetworkSet,
    VpnblockerPlugin,
    parse_asns,
    parse_list,
    verdict_from_lookup,
)

VPN_LIST = "https://example.com/lists_vpn/output/vpn/ipv4.txt"
TOR_LIST = "https://check.torproject.org/torbulkexitlist"

#: proxycheck.io's v3 answer for a Tor exit, trimmed to the parts this plugin reads.
TOR_ANSWER = {
    "status": "ok",
    "185.220.101.1": {
        "detections": {"proxy": True, "vpn": False, "tor": False, "hosting": False},
        "operator": {"name": "TOR"},
    },
}
CLEAN_ANSWER = {
    "status": "ok",
    "8.8.4.4": {"detections": {"proxy": False, "vpn": False, "tor": False, "hosting": True}},
}


def _plugin(console, tmp_path, lists=None, downloads=None, answers=None, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    settings.setdefault("lists", [])
    if lists is not None:
        settings["lists"] = lists
    settings.setdefault("cache_dir", str(tmp_path / "cache"))
    plugin = VpnblockerPlugin(console, {"settings": settings})
    plugin.queries = []  # type: ignore[attr-defined]

    def query(ip):  # noqa: ANN001, ANN202
        plugin.queries.append(ip)  # type: ignore[attr-defined]
        return (answers or {}).get(ip)

    plugin._download = lambda url: (downloads or {}).get(url)  # type: ignore[method-assign]
    plugin._query = query  # type: ignore[method-assign]
    plugin.start()
    console.plugins["vpnblocker"] = plugin
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
    console.register_client(name, client)
    return client


async def _auth(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))


async def _update(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_UPDATE, client=client))


def _kicked(console):  # noqa: ANN001, ANN202
    return [client.name for client, _reason, _admin in console.kicked]


def _told(console, client):  # noqa: ANN001, ANN202
    return [text for who, text in console.told if who is client]


# -- the pieces ----------------------------------------------------------------------------------


def test_a_list_takes_networks_addresses_and_comments():
    networks = parse_list("# a header\n2.26.157.0/24\n\n171.25.193.25  # tor\nnonsense\n")

    assert [str(n) for n in networks] == ["2.26.157.0/24", "171.25.193.25/32"]


def test_a_network_set_finds_what_is_inside_and_nothing_else():
    found = NetworkSet(parse_list("2.26.157.0/24\n2.26.158.0/24\n2001:550:1d05::/48"))

    assert ipaddress.ip_address("2.26.157.1") in found
    assert ipaddress.ip_address("2.26.158.255") in found
    assert ipaddress.ip_address("2001:550:1d05::1") in found
    assert ipaddress.ip_address("2.26.159.0") not in found
    assert ipaddress.ip_address("2.26.156.255") not in found
    assert len(found) == 2  # the two adjacent /24s merge into one range


def test_overlapping_networks_merge_without_losing_the_wider_one():
    found = NetworkSet(parse_list("1.0.0.0/8\n1.2.3.0/24"))

    assert ipaddress.ip_address("1.200.0.1") in found
    assert len(found) == 1


def test_every_spelling_of_an_as_number():
    assert parse_asns(["AS9009", "as60729", 13335, "16509"]) == {9009, 60729, 13335, 16509}
    assert parse_asns("AS9009") == {9009}
    assert parse_asns(["Mullvad"]) == set()


def test_a_lookup_answer_names_what_it_found():
    verdict = verdict_from_lookup(TOR_ANSWER, "185.220.101.1")

    assert verdict is not None
    assert verdict.source == "proxycheck: proxy (TOR)"


def test_hosting_is_only_flagged_when_asked_for():
    assert verdict_from_lookup(CLEAN_ANSWER, "8.8.4.4") is None
    assert verdict_from_lookup(CLEAN_ANSWER, "8.8.4.4", block_hosting=True) is not None


def test_an_answer_that_cannot_be_read_flags_nobody():
    assert verdict_from_lookup({"status": "denied"}, "1.1.1.1") is None
    assert verdict_from_lookup({"status": "ok"}, "1.1.1.1") is None
    assert verdict_from_lookup(None, "1.1.1.1") is None


# -- the lists -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_player_on_a_downloaded_list_is_removed(console, tmp_path):
    plugin = _plugin(console, tmp_path, lists=[VPN_LIST], downloads={VPN_LIST: "2.26.157.0/24\n"})
    await plugin.refresh()
    joe = _join(console, "Joe", ip="2.26.157.9")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_a_player_on_no_list_is_left_alone(console, tmp_path):
    plugin = _plugin(console, tmp_path, lists=[VPN_LIST], downloads={VPN_LIST: "2.26.157.0/24\n"})
    await plugin.refresh()
    joe = _join(console, "Joe", ip="8.8.8.8")

    await _auth(console, joe)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_restart_has_the_lists_without_the_network(console, tmp_path):
    """The copy on disk is what a bot starting with no connection has to work with."""
    first = _plugin(console, tmp_path, lists=[TOR_LIST], downloads={TOR_LIST: "171.25.193.25\n"})
    await first.refresh()

    second = _plugin(console, tmp_path, lists=[TOR_LIST])  # downloads nothing
    joe = _join(console, "Joe", ip="171.25.193.25")
    await second.on_seen(Event(EventType.CLIENT_AUTH, client=joe))

    assert _kicked(console) == ["Joe"]
    assert console.kicked[0][0] is joe


@pytest.mark.asyncio
async def test_a_failed_download_keeps_the_last_copy(console, tmp_path):
    plugin = _plugin(console, tmp_path, lists=[VPN_LIST], downloads={VPN_LIST: "2.26.157.0/24\n"})
    await plugin.refresh()
    plugin._download = lambda url: None  # type: ignore[method-assign]
    await plugin.refresh()

    assert ipaddress.ip_address("2.26.157.9") in plugin.lists[VPN_LIST]


@pytest.mark.asyncio
async def test_your_own_blocks_need_no_download(console, tmp_path):
    _plugin(console, tmp_path, blocks=["45.0.0.0/8"])
    joe = _join(console, "Joe", ip="45.1.2.3")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_a_whole_provider_by_its_as_number(console, tmp_path):
    class AsnReader:
        def get(self, ip):  # noqa: ANN001, ANN202
            return {"autonomous_system_number": 9009} if ip == "5.6.7.8" else None

    class Geolocation:
        asn_reader = AsnReader()

    plugin = _plugin(console, tmp_path, asns=["AS9009"])
    console.plugins["geolocation"] = Geolocation()
    joe = _join(console, "Joe", ip="5.6.7.8")
    ann = _join(console, "Ann", ip="5.6.7.9")

    await _auth(console, joe)
    await _auth(console, ann)

    assert _kicked(console) == ["Joe"]
    assert plugin.lists == {}


# -- who is not checked --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_registered_player_is_exempt_by_default(console, tmp_path):
    _plugin(console, tmp_path, blocks=["45.0.0.0/8"])
    regular = _join(console, "Regular", ip="45.1.2.3", bits=2)

    await _auth(console, regular)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_allowed_network_or_player_is_never_flagged(console, tmp_path):
    _plugin(
        console,
        tmp_path,
        blocks=["45.0.0.0/8"],
        allow=["45.9.0.0/16"],
        allow_guids=["guid2".ljust(32, "0")],
    )
    office = _join(console, "Office", ip="45.9.1.1")
    friend = _join(console, "Friend", ip="45.1.1.1")

    await _auth(console, office)
    await _auth(console, friend)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_private_address_is_nobodys_vpn(console, tmp_path):
    """A LAN player arrives from 192.168.x.x; no list has it and no lookup should be spent on it."""
    plugin = _plugin(console, tmp_path, lookup=True)
    joe = _join(console, "Joe", ip="192.168.1.20")

    await _auth(console, joe)

    assert console.kicked == []
    assert plugin.queries == []


@pytest.mark.asyncio
async def test_no_address_yet_is_checked_when_it_arrives(console, tmp_path):
    """On CoD and Quake 3 authentication carries no address; the update a moment later does."""
    _plugin(console, tmp_path, blocks=["45.0.0.0/8"])
    joe = _join(console, "Joe")

    await _auth(console, joe)
    assert console.kicked == []

    joe.ip = "45.1.2.3"
    await _update(console, joe)

    assert _kicked(console) == ["Joe"]


# -- the lookup ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_lookup_is_off_unless_switched_on(console, tmp_path):
    plugin = _plugin(console, tmp_path, answers={"185.220.101.1": TOR_ANSWER})
    joe = _join(console, "Joe", ip="185.220.101.1")

    await _auth(console, joe)

    assert console.kicked == []
    assert plugin.queries == []


@pytest.mark.asyncio
async def test_the_lookup_catches_what_the_lists_do_not(console, tmp_path):
    _plugin(console, tmp_path, lookup=True, answers={"185.220.101.1": TOR_ANSWER})
    joe = _join(console, "Joe", ip="185.220.101.1")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_the_lists_answer_first_and_the_lookup_is_not_spent(console, tmp_path):
    plugin = _plugin(console, tmp_path, lookup=True, blocks=["185.220.101.0/24"], answers={})
    joe = _join(console, "Joe", ip="185.220.101.1")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]
    assert plugin.queries == []


@pytest.mark.asyncio
async def test_a_failed_lookup_lets_the_player_in_and_is_asked_again(console, tmp_path):
    plugin = _plugin(console, tmp_path, lookup=True, answers={})  # every query fails
    joe = _join(console, "Joe", ip="8.8.4.4")

    await _auth(console, joe)
    assert console.kicked == []

    await plugin.verdict("8.8.4.4")
    assert plugin.queries == ["8.8.4.4", "8.8.4.4"]  # a failure is not remembered


@pytest.mark.asyncio
async def test_an_answer_is_remembered_until_it_expires(console, tmp_path):
    plugin = _plugin(
        console, tmp_path, lookup=True, cache_hours=1, answers={"8.8.4.4": CLEAN_ANSWER}
    )

    await plugin.verdict("8.8.4.4")
    await plugin.verdict("8.8.4.4")
    assert plugin.queries == ["8.8.4.4"]

    console.clock.advance(3601)
    await plugin.verdict("8.8.4.4")
    assert plugin.queries == ["8.8.4.4", "8.8.4.4"]


@pytest.mark.asyncio
async def test_repeated_updates_cost_one_check(console, tmp_path):
    """`CLIENT_UPDATE` arrives on every status poll; the check is per address, not per event."""
    plugin = _plugin(console, tmp_path, lookup=True, answers={"8.8.4.4": CLEAN_ANSWER})
    plugin.settings["cache_hours"] = 0  # so only the per-player memory can be what saves a query
    joe = _join(console, "Joe", ip="8.8.4.4")

    for _ in range(5):
        await _update(console, joe)

    assert plugin.queries == ["8.8.4.4"]


# -- what happens to a flagged player ------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_mode_tells_the_admins_and_kicks_nobody(console, tmp_path):
    _plugin(console, tmp_path, blocks=["45.0.0.0/8"], action="watch")
    admin = _join(console, "Admin", ip="8.8.8.8", bits=16)
    joe = _join(console, "Joe", ip="45.1.2.3")

    await _auth(console, joe)

    assert console.kicked == []
    assert _told(console, admin) == ["Joe is connecting through your own block list (watching)"]


@pytest.mark.asyncio
async def test_a_kick_is_reported_to_the_admins_too(console, tmp_path):
    _plugin(console, tmp_path, blocks=["45.0.0.0/8"])
    admin = _join(console, "Admin", ip="8.8.8.8", bits=16)
    joe = _join(console, "Joe", ip="45.1.2.3")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]
    assert _told(console, admin) == ["Joe is connecting through your own block list (kicked)"]


def test_a_misspelt_action_is_refused_at_startup():
    problems = VpnblockerPlugin.check_config({"settings": {"action": "ban"}}, frozenset())

    assert problems and "settings.action" in problems[0]


def test_asns_without_geolocation_is_refused_at_startup():
    problems = VpnblockerPlugin.check_config({"settings": {"asns": [9009]}}, frozenset({"admin"}))

    assert problems and "geolocation" in problems[0]
    assert not VpnblockerPlugin.check_config(
        {"settings": {"asns": [9009]}}, frozenset({"admin", "geolocation"})
    )


# -- !vpncheck -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vpncheck_says_which_list_flagged_somebody(console, tmp_path):
    _plugin(console, tmp_path, blocks=["45.0.0.0/8"], action="watch", tell_admins=False)
    admin = _join(console, "Admin", ip="8.8.8.8", bits=16)
    _join(console, "Joe", ip="45.1.2.3")
    _join(console, "Ann", ip="8.8.4.4")

    await CommandProcessor(console.command_registry, console).handle(admin, "!vpncheck Joe")
    await CommandProcessor(console.command_registry, console).handle(admin, "!vpncheck Ann")

    assert _told(console, admin) == [
        "Joe (45.1.2.3) is flagged: your own block list",
        "Ann (8.8.4.4) is not on any VPN or proxy list",
    ]


def test_anonymous_on_its_own_is_not_a_finding():
    """proxycheck.io answers `anonymous: true` for Google's 8.8.8.8 with every other flag false."""
    answer = {"status": "ok", "8.8.8.8": {"detections": {"proxy": False, "anonymous": True}}}

    assert verdict_from_lookup(answer, "8.8.8.8") is None
