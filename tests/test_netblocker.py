"""The `netblocker` plugin — keeping whole networks off the server.

No captured tests exist for this one; what the classic shipped instead was a copy of a third-party
CIDR library and its test suite, 1,091 lines of address arithmetic that the standard library has done
since Python 3.3. So these tests are about the two things that actually decide whether the plugin
works: what an operator can write in the list, and *when* the address is known.

That second one is the fault. The classic checked at authentication only, and on Call of Duty and
Quake 3 the line that authenticates a player carries no address — so it blocked nobody there. It also
compared whatever `client.ip` held, which is the shape of bug that made `ipban` kick everybody.
"""

from __future__ import annotations

import pytest

from b3.core.events import Event, EventType
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.plugins.netblocker import NetblockerPlugin, parse_blocks


def _plugin(console, blocks=None, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    if blocks is not None:
        settings["blocks"] = blocks
    plugin = NetblockerPlugin(console, {"settings": settings})
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


async def _auth(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))


async def _update(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_UPDATE, client=client))


def _kicked(console):  # noqa: ANN001, ANN202
    return [client.name for client, _reason, _admin in console.kicked]


# -- what an operator can write ------------------------------------------------------------------


def test_the_four_spellings_an_operator_will_use():
    """CIDR, a bare address, a dashed range, and IPv6 — the last of which the classic could not do."""
    assert [str(n) for n in parse_blocks(["10.0.0.0/8"])] == ["10.0.0.0/8"]
    assert [str(n) for n in parse_blocks(["192.168.1.1"])] == ["192.168.1.1/32"]
    assert [str(n) for n in parse_blocks(["2001:db8::/32"])] == ["2001:db8::/32"]
    assert [str(n) for n in parse_blocks(["1.2.3.5 - 1.2.3.9"])] == [
        "1.2.3.5/32",
        "1.2.3.6/31",
        "1.2.3.8/31",
    ]


def test_a_host_address_with_a_prefix_means_the_network_it_is_in():
    """An operator writing `1.2.3.4/24` means that /24. The alternative is refusing them on a
    technicality."""
    assert [str(n) for n in parse_blocks(["1.2.3.4/24"])] == ["1.2.3.0/24"]


def test_one_bad_entry_does_not_take_the_list_with_it():
    """A typo used to raise inside the check, once per connecting player."""
    blocks = parse_blocks(["10.0.0.0/8", "10.0.0.0/33", "not-an-address", "192.168.0.0/16"])

    assert [str(n) for n in blocks] == ["10.0.0.0/8", "192.168.0.0/16"]


def test_a_range_the_wrong_way_round_is_refused():
    assert parse_blocks(["1.2.3.9 - 1.2.3.5"]) == []


def test_a_list_that_is_not_a_list_blocks_nothing():
    assert parse_blocks("10.0.0.0/8") == parse_blocks(["10.0.0.0/8"])  # one entry is allowed
    assert parse_blocks({"10.0.0.0/8": True}) == []
    assert parse_blocks(None) == []


# -- who gets kicked -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_player_inside_a_blocked_network_is_removed(console):
    _plugin(console, blocks=["10.0.0.0/8"])
    joe = _join(console, "Joe", ip="10.1.2.3")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_a_player_outside_every_block_is_left_alone(console):
    _plugin(console, blocks=["10.0.0.0/8", "192.168.0.0/16"])
    joe = _join(console, "Joe", ip="8.8.8.8")

    await _auth(console, joe)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_ipv6_address_is_matched(console):
    _plugin(console, blocks=["2001:db8::/32"])
    joe = _join(console, "Joe", ip="2001:db8:dead:beef::1")

    await _auth(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_an_ipv4_address_is_not_inside_an_ipv6_block(console):
    _plugin(console, blocks=["::/0"])
    joe = _join(console, "Joe", ip="8.8.8.8")

    await _auth(console, joe)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_player_with_no_known_address_is_not_a_match(console):
    """The `ipban` lesson: on CoD and Quake 3 an authenticating player has no address yet, and
    treating that as a match is how a plugin empties a server instead of policing it."""
    _plugin(console, blocks=["10.0.0.0/8"])
    joe = _join(console, "Joe", ip="")

    await _auth(console, joe)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_the_address_arriving_later_is_still_checked(console):
    """The other half of the same fault: the classic looked once, before there was anything to see."""
    _plugin(console, blocks=["10.0.0.0/8"])
    joe = _join(console, "Joe", ip="")
    await _auth(console, joe)
    assert console.kicked == []

    joe.ip = "10.4.5.6"  # the status poll resolved it
    await _update(console, joe)

    assert _kicked(console) == ["Joe"]


@pytest.mark.asyncio
async def test_a_player_above_the_level_is_exempt(console):
    """A regular sharing a blocked range is not who this list is aimed at."""
    _plugin(console, blocks=["10.0.0.0/8"], max_level=1)
    regular = _join(console, "Reg", ip="10.1.2.3", bits=2)

    await _auth(console, regular)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_unreadable_address_is_reported_and_not_acted_on(console):
    """An engine reporting something unexpected is a parser question, not grounds for a kick."""
    _plugin(console, blocks=["10.0.0.0/8"])
    joe = _join(console, "Joe", ip="not.an.address")

    await _auth(console, joe)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_empty_list_blocks_nobody(console):
    plugin = _plugin(console, blocks=[])
    joe = _join(console, "Joe", ip="10.1.2.3")

    await _auth(console, joe)

    assert plugin.blocks == []
    assert console.kicked == []


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_real_call_of_duty_player_is_blocked_when_the_poll_finds_their_address(tmp_path):
    """The engine this plugin never worked on, end to end.

    A `J;` line authenticates the player and carries no address; the address arrives from the status
    table, which is what publishes `CLIENT_UPDATE`. The kick has to come from that second moment.
    """
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.core.game import PlayerInfo
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            return ""

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="netblocker")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = NetblockerPlugin(bot, {"settings": {"blocks": ["10.0.0.0/8"]}})
    bot.add_plugin(plugin, "netblocker")
    bot.start()
    admin.start()
    plugin.start()

    joe = "j" * 32
    await bot.replay([f"J;{joe};1;Joe"])
    await bot.bus.drain()
    client = bot.clients.get_by_cid("1")
    assert client is not None
    assert client.ip == ""  # the log line said nothing about where they are
    rcon.commands.clear()

    bot._reconcile([PlayerInfo(cid="1", name="Joe", guid=joe, ip="10.9.9.9")])
    await bot.bus.drain()

    assert any("kick" in c.lower() for c in rcon.commands), rcon.commands
    bot.storage.close()
