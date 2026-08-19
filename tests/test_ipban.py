"""The `ipban` plugin — keeping out a banned player who came back with a new identity.

A guid ban is evaded by reinstalling. The address usually is not, so this checks a connecting player's
address against the addresses behind active bans.

The test that matters most is the empty one. The classic compared `client.ip in banned` with neither
side excluding `""`, so a banned player whose address was never recorded put an empty string in that set
— and then every player the engine had not yet given an address to matched it. On Call of Duty and
Quake 3 that is *every* player at the moment they authenticate, so the plugin would have emptied the
server instead of policing it.
"""

from __future__ import annotations

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client, NEVER_EXPIRES, Penalty, PenaltyType
from b3.plugins.admin import AdminPlugin
from b3.plugins.ipban import CACHE_SECONDS, IpbanPlugin


def _ipban(console, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = IpbanPlugin(console, {"settings": settings} if settings else None)
    plugin.start()
    return plugin


def _client(console, name="Bob", cid="2", id_=2, ip="", bits=0):  # noqa: ANN001, ANN202
    client = Client(guid=name[0].upper() * 4, name=name, cid=cid, id=id_, ip=ip, group_bits=bits)
    console.clients.add(client)
    console.storage.save_client(client)
    return client


def _ban(console, client, type_=PenaltyType.BAN, expire=NEVER_EXPIRES):  # noqa: ANN001, ANN202
    console.storage.add_penalty(
        Penalty(
            type=type_,
            client_id=client.require_id(),
            id=len(console.storage.penalties) + 1,
            reason="cheating",
            time_expire=expire,
        )
    )


async def _auth(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))


# -- the check -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_new_guid_from_a_banned_address_is_kicked(console):
    """The whole point: the ban is evaded by reinstalling, the address is not."""
    _ipban(console)
    banned = _client(console, name="Cheat", cid="1", id_=1, ip="11.22.33.44")
    _ban(console, banned)

    returning = _client(console, name="Innocent", cid="3", id_=3, ip="11.22.33.44")
    await _auth(console, returning)

    assert [c.name for c, _r, _a in console.kicked] == ["Innocent"]


@pytest.mark.asyncio
async def test_a_tempban_counts_while_it_lasts(console):
    _ipban(console)
    banned = _client(console, name="Cheat", cid="1", id_=1, ip="11.22.33.44")
    console.storage.now_epoch = 1_000
    _ban(console, banned, type_=PenaltyType.TEMPBAN, expire=2_000)

    returning = _client(console, name="Other", cid="3", id_=3, ip="11.22.33.44")
    await _auth(console, returning)

    assert console.kicked


@pytest.mark.asyncio
async def test_an_expired_tempban_does_not(console):
    _ipban(console)
    banned = _client(console, name="Cheat", cid="1", id_=1, ip="11.22.33.44")
    console.storage.now_epoch = 3_000
    _ban(console, banned, type_=PenaltyType.TEMPBAN, expire=2_000)

    returning = _client(console, name="Other", cid="3", id_=3, ip="11.22.33.44")
    await _auth(console, returning)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_different_address_is_left_alone(console):
    _ipban(console)
    banned = _client(console, name="Cheat", cid="1", id_=1, ip="11.22.33.44")
    _ban(console, banned)

    other = _client(console, name="Other", cid="3", id_=3, ip="99.99.99.99")
    await _auth(console, other)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_player_with_no_address_yet_is_never_kicked(console):
    """The classic's fault, and it had teeth: on the Call of Duty and Quake 3 engines the log line that
    authenticates a player carries no address at all, so `""` describes everybody for a moment."""
    _ipban(console)
    banned = _client(console, name="Cheat", cid="1", id_=1, ip="")  # banned, address never recorded
    _ban(console, banned)

    arriving = _client(console, name="Innocent", cid="3", id_=3, ip="")
    await _auth(console, arriving)

    assert console.kicked == []
    assert "" not in console.storage.banned_ips()


@pytest.mark.asyncio
async def test_an_address_arriving_later_is_still_checked(console):
    """Which is the other half of the same fact: the address turns up after authentication, from the
    status poll, and that is the moment this plugin can say anything."""
    _ipban(console)
    banned = _client(console, name="Cheat", cid="1", id_=1, ip="11.22.33.44")
    _ban(console, banned)

    arriving = _client(console, name="Innocent", cid="3", id_=3, ip="")
    await _auth(console, arriving)
    assert console.kicked == []

    arriving.ip = "11.22.33.44"
    await console.bus.publish(Event(EventType.CLIENT_UPDATE, client=arriving))

    assert [c.name for c, _r, _a in console.kicked] == ["Innocent"]


@pytest.mark.asyncio
async def test_a_registered_player_is_exempt_by_default(console):
    """A household address shared with somebody banned is the case this exists not to punish."""
    _ipban(console, max_level=1)
    banned = _client(console, name="Cheat", cid="1", id_=1, ip="11.22.33.44")
    _ban(console, banned)

    regular = _client(console, name="Reg", cid="3", id_=3, ip="11.22.33.44", bits=2)  # level 2
    await _auth(console, regular)

    assert console.kicked == []


# -- the cache -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_addresses_are_read_once_for_a_burst_of_arrivals(console):
    """A server refilling after a map change is one query, not one per player — the classic ran two
    SQL statements for each of them."""
    plugin = _ipban(console)
    calls = {"n": 0}
    real = console.storage.banned_ips

    def counted() -> set[str]:
        calls["n"] += 1
        return real()

    console.storage.banned_ips = counted
    plugin._addresses = {"1.1.1.1"}  # primed, so the first call is not forced
    plugin._read_at = console.clock.now()

    for index in range(5):
        await _auth(console, _client(console, name=f"P{index}", cid=str(index + 5), id_=index + 5))

    assert calls["n"] == 0  # nothing re-read inside the window

    console.clock.advance(CACHE_SECONDS + 1)
    await _auth(console, _client(console, name="Late", cid="20", id_=20, ip="9.9.9.9"))
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_a_fresh_ban_takes_effect_at_once(console):
    """Waiting out the cache would let the banned player's next connection through, which is exactly
    the connection this plugin exists for."""
    plugin = _ipban(console)
    plugin._addresses = set()
    plugin._read_at = console.clock.now()

    banned = _client(console, name="Cheat", cid="1", id_=1, ip="11.22.33.44")
    _ban(console, banned)
    await console.bus.publish(Event(EventType.CLIENT_BAN, client=banned, data="cheating"))

    returning = _client(console, name="Other", cid="3", id_=3, ip="11.22.33.44")
    await _auth(console, returning)

    assert console.kicked


def test_a_database_that_will_not_answer_refuses_nobody(console, caplog):
    """A failed query must not turn into a server nobody can join."""
    plugin = _ipban(console)

    def explode() -> set[str]:
        raise OSError("database is away")

    console.storage.banned_ips = explode
    plugin._addresses = set()
    plugin._read_at = 0.0

    with caplog.at_level("WARNING"):
        assert plugin.banned_addresses() == set()

    assert "could not read the banned addresses" in caplog.text


# -- the storage query itself ---------------------------------------------------------------------


def test_banned_ips_excludes_expired_lifted_and_empty(tmp_path):
    """Against a real database, because the query is the plugin's whole input."""
    from b3.core.clock import FakeClock
    from b3.storage.store import SqlAlchemyStorage

    clock = FakeClock()
    storage = SqlAlchemyStorage(f"sqlite:///{tmp_path / 'b3.sqlite'}", clock=clock)
    storage.connect()

    def saved(name: str, ip: str) -> Client:
        return storage.save_client(Client(guid=name * 8, name=name, ip=ip))

    permanent = saved("perm", "1.1.1.1")
    temporary = saved("temp", "2.2.2.2")
    expired = saved("gone", "3.3.3.3")
    lifted = saved("free", "4.4.4.4")
    unknown = saved("anon", "")

    now = clock.epoch()
    storage.add_penalty(Penalty(type=PenaltyType.BAN, client_id=permanent.require_id()))
    storage.add_penalty(
        Penalty(type=PenaltyType.TEMPBAN, client_id=temporary.require_id(), time_expire=now + 3600)
    )
    storage.add_penalty(
        Penalty(type=PenaltyType.TEMPBAN, client_id=expired.require_id(), time_expire=now - 10)
    )
    storage.add_penalty(Penalty(type=PenaltyType.BAN, client_id=lifted.require_id()))
    storage.disable_penalties(lifted.require_id())
    storage.add_penalty(Penalty(type=PenaltyType.BAN, client_id=unknown.require_id()))
    storage.add_penalty(Penalty(type=PenaltyType.KICK, client_id=saved("kick", "5.5.5.5").id or 0))

    assert storage.banned_ips() == {"1.1.1.1", "2.2.2.2"}
    storage.close()
