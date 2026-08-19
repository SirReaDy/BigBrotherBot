"""The `banlist` plugin — honouring ban lists somebody else maintains.

The IP range convention is the part worth pinning, and the classic bot's captured tests
(`tests/plugins/banlist/test_ip_banlist.py`) are where it is written down: an entry ending `.0`
covers the last octet, `.0.0` two octets, `.0.0.0` three, an entry with trailing junk
(`11.22.33.44:-1`, `22.33.44.11foo`) still names its address, and `11.22.33.4` must **not** match the
entry `11.22.33.44`. Every one of those cases is reproduced below against the same data.

Two of the classic's own faults are visible in those tests rather than in its code: commented entries
were skipped only because the regex happened to be anchored, and every lookup rebuilt a regex out of
the *player's own id* — escaped, but escaping was the only thing between a shared ban list and a
pattern compiled from data off the network.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.events import Event, EventType
from b3.domain.client import Client, PenaltyType
from b3.plugins.admin import AdminPlugin
from b3.plugins.banlist import BanlistPlugin, ip_matches, parse_entries

# The captured file content from the classic's own tests.
CAPTURED_STRICT = """\
11.22.33.44:-1
22.33.44.11foo
33.44.55.66
"""

CAPTURED_RANGES = """\
11.22.33.0:-1
22.33.44.0foo
33.44.55.0
44.55.0.0
55.0.0.0
"""

CAPTURED_COMMENTS = """\
//11.22.33.44:-1
# 22.33.44.11foo
//   33.44.55.66
12.12.12.12
"""


def _banlist(console, tmp_path, lists, **settings):  # noqa: ANN001, ANN202
    admin = AdminPlugin(console, None)
    admin.start()
    console.plugins = {"admin": admin}
    console.get_plugin = lambda name: console.plugins.get(name)  # noqa: ARG005
    plugin = BanlistPlugin(console, {"settings": settings, "lists": lists})
    plugin.start()
    return plugin


def _file(tmp_path, name, content):  # noqa: ANN001, ANN202
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _client(console, name="Bob", ip="", guid="", pbid="", bits=0, cid="2"):  # noqa: ANN001, ANN202
    client = Client(guid=guid, name=name, cid=cid, id=int(cid), ip=ip, pbid=pbid, group_bits=bits)
    console.clients.add(client)
    return client


async def _auth(console, client):  # noqa: ANN001, ANN202
    await console.bus.publish(Event(EventType.CLIENT_AUTH, client=client))


async def _run(console, client, text):  # noqa: ANN001, ANN202
    await CommandProcessor(console.command_registry, console).handle(client, text)


# -- reading a list file -------------------------------------------------------------------------


def test_an_entry_keeps_only_its_address():
    """The published lists carry a port, a nickname or a reason after the address."""
    assert parse_entries(CAPTURED_STRICT, "ip") == {"11.22.33.44", "22.33.44.11", "33.44.55.66"}


def test_commented_entries_are_dropped():
    """The classic skipped these by accident — its pattern was anchored at the start of the line, so
    `//11.22.33.44` failed to match rather than being understood as a comment."""
    assert parse_entries(CAPTURED_COMMENTS, "ip") == {"12.12.12.12"}


def test_a_guid_list_is_read_line_by_line():
    text = "# cheaters\nAAAA1111\nBBBB2222 some reason here\n\n"
    assert parse_entries(text, "guid") == {"aaaa1111", "bbbb2222"}


def test_a_rules_of_combat_list_is_read_from_its_attributes():
    text = '<banlist><ban BannedID="AAAA1111" /><ban BannedID="BBBB2222"/></banlist>'
    assert parse_entries(text, "roc") == {"aaaa1111", "bbbb2222"}


# -- the range convention ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("11.22.33.44", "11.22.33.44"),
        ("22.33.44.11", "22.33.44.11"),
        ("33.44.55.66", "33.44.55.66"),
        # The case that makes trailing junk matter: `11.22.33.4` is not `11.22.33.44`.
        ("11.22.33.4", None),
        ("22.33.44.1", None),
        ("33.44.55.6", None),
    ],
)
def test_an_exact_entry_matches_only_that_address(ip, expected):  # noqa: ANN001
    entries = parse_entries(CAPTURED_STRICT, "ip")
    assert ip_matches(ip, entries, force_range=False) == expected


@pytest.mark.parametrize(
    ("ip", "entry"),
    [
        ("11.22.33.44", "11.22.33.0"),
        ("11.22.33.4", "11.22.33.0"),
        ("22.33.44.11", "22.33.44.0"),
        ("33.44.55.6", "33.44.55.0"),
        ("44.55.0.1", "44.55.0.0"),
        ("44.55.1.255", "44.55.0.0"),
        ("44.55.255.255", "44.55.0.0"),
        ("55.0.0.1", "55.0.0.0"),
        ("55.41.25.15", "55.0.0.0"),
        ("55.255.255.255", "55.0.0.0"),
    ],
)
def test_an_entry_ending_in_zero_covers_the_range(ip, entry):  # noqa: ANN001
    """Straight from the captures. It is also why an entry for a real `.0` host cannot be written —
    a limit of the format the published lists use, not of this code."""
    entries = parse_entries(CAPTURED_RANGES, "ip")
    assert ip_matches(ip, entries, force_range=False) == entry


def test_a_neighbouring_network_is_not_covered():
    entries = parse_entries(CAPTURED_RANGES, "ip")
    assert ip_matches("11.22.34.44", entries, force_range=False) is None


@pytest.mark.parametrize(
    ("ip", "banned"),
    [("11.22.33.77", True), ("11.22.33.4", True), ("11.22.66.66", False)],
)
def test_force_ip_range_treats_every_entry_as_its_network(ip, banned):  # noqa: ANN001
    entries = parse_entries(CAPTURED_STRICT, "ip")
    assert (ip_matches(ip, entries, force_range=True) is not None) is banned


# -- acting on a match ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_listed_player_is_kicked_with_the_list_named(console, tmp_path):
    """Named, and out loud. The classic kicked with `silent=True`, so nobody on the server — the
    admins included — could see why somebody had vanished."""
    _banlist(
        console,
        tmp_path,
        [{"kind": "ip", "name": "shared", "file": _file(tmp_path, "ips.txt", CAPTURED_STRICT)}],
    )
    bob = _client(console, ip="11.22.33.44")

    await _auth(console, bob)

    assert [c.name for c, _reason, _admin in console.kicked] == ["Bob"]
    assert "shared" in console.kicked[0][1]


@pytest.mark.asyncio
async def test_a_player_who_is_not_listed_is_left_alone(console, tmp_path):
    _banlist(
        console,
        tmp_path,
        [{"kind": "ip", "file": _file(tmp_path, "ips.txt", CAPTURED_STRICT)}],
    )
    bob = _client(console, ip="99.99.99.99")

    await _auth(console, bob)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_a_guid_list_matches_regardless_of_case(console, tmp_path):
    _banlist(
        console,
        tmp_path,
        [{"kind": "guid", "file": _file(tmp_path, "guids.txt", "aaaa1111\n")}],
    )
    bob = _client(console, guid="AAAA1111")

    await _auth(console, bob)

    assert console.kicked


@pytest.mark.asyncio
async def test_a_punkbuster_list_matches_the_punkbuster_id(console, tmp_path):
    """A player's guid and their PunkBuster id are different things — see `b3.parsers.punkbuster` —
    so a pbid list must be looked up against the pbid and nothing else."""
    _banlist(
        console,
        tmp_path,
        [{"kind": "pbid", "file": _file(tmp_path, "pb.txt", "PBIDAAA\n")}],
    )
    bob = _client(console, guid="SOMETHINGELSE", pbid="pbidaaa")

    await _auth(console, bob)

    assert console.kicked


@pytest.mark.asyncio
async def test_a_rules_of_combat_list_bans_by_guid(console, tmp_path):
    _banlist(
        console,
        tmp_path,
        [{"kind": "roc", "file": _file(tmp_path, "roc.xml", '<b BannedID="AAAA1111"/>')}],
    )
    bob = _client(console, guid="AAAA1111")

    await _auth(console, bob)

    assert console.kicked


@pytest.mark.asyncio
async def test_a_whitelist_wins_and_stops_the_search(console, tmp_path):
    """The point of a whitelist: a list maintained by somebody else must not be able to remove one of
    your own regulars, and the only way to promise that is to stop looking once it matches."""
    _banlist(
        console,
        tmp_path,
        [
            {
                "kind": "ip",
                "name": "ours",
                "whitelist": True,
                "file": _file(tmp_path, "allow.txt", "11.22.33.44\n"),
            },
            {"kind": "ip", "name": "shared", "file": _file(tmp_path, "deny.txt", "11.22.33.44\n")},
        ],
    )
    bob = _client(console, ip="11.22.33.44")

    await _auth(console, bob)

    assert console.kicked == []


@pytest.mark.asyncio
async def test_an_immune_player_is_recorded_rather_than_kicked(console, tmp_path):
    """Somebody trusted turning up on a shared list is worth knowing about either way, so it becomes
    an admin note instead of nothing at all."""
    _banlist(
        console,
        tmp_path,
        [{"kind": "ip", "name": "shared", "file": _file(tmp_path, "ips.txt", "11.22.33.44\n")}],
        immunity_level=40,
    )
    bob = _client(console, ip="11.22.33.44", bits=16)  # admin, level 40

    await _auth(console, bob)

    assert console.kicked == []
    assert [c.name for c, _reason, _admin in console.noticed] == ["Bob"]


# -- lists that will not load --------------------------------------------------------------------


def test_a_list_whose_file_is_missing_is_loud_and_matches_nobody(console, tmp_path, caplog):
    """The classic raised per list and caught it around the loop, so a typo'd path left a plugin that
    looked healthy while enforcing one list fewer than the operator believed."""
    with caplog.at_level("ERROR"):
        plugin = _banlist(
            console, tmp_path, [{"kind": "ip", "name": "typo", "file": str(tmp_path / "nope.txt")}]
        )

    assert plugin.lists[0].entries == set()
    assert plugin.lists[0].loaded is False
    assert "nope.txt" in caplog.text


def test_a_list_with_no_kind_is_refused_by_name(console, tmp_path, caplog):
    with caplog.at_level("ERROR"):
        plugin = _banlist(console, tmp_path, [{"name": "x", "file": str(tmp_path / "a.txt")}])

    assert plugin.lists == []
    assert "not a list kind" in caplog.text


def test_a_list_with_neither_a_file_nor_a_url_is_refused(console, tmp_path, caplog):
    with caplog.at_level("ERROR"):
        plugin = _banlist(console, tmp_path, [{"kind": "ip", "name": "nowhere"}])

    assert plugin.lists == []
    assert "neither a file nor a url" in caplog.text


def test_no_lists_at_all_says_so_rather_than_looking_healthy(console, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        plugin = _banlist(console, tmp_path, [])

    assert plugin.lists == []
    assert "will do nothing" in caplog.text


# -- the commands --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_banlistinfo_says_what_is_loaded_and_from_where(console, tmp_path):
    _banlist(
        console,
        tmp_path,
        [{"kind": "ip", "name": "shared", "file": _file(tmp_path, "ips.txt", CAPTURED_STRICT)}],
    )
    admin = _client(console, name="Su", bits=128)

    await _run(console, admin, "!banlistinfo")

    reply = [text for who, text in console.told if who is admin][-1]
    assert "shared" in reply and "3 entries" in reply


@pytest.mark.asyncio
async def test_banlistcheck_rechecks_everybody_now(console, tmp_path):
    """Somebody already on the server when a list is updated has already authenticated, so a recheck
    is the only thing that will catch them."""
    path = _file(tmp_path, "ips.txt", "1.1.1.1\n")
    plugin = _banlist(console, tmp_path, [{"kind": "ip", "name": "shared", "file": path}])
    bob = _client(console, ip="11.22.33.44")
    await _auth(console, bob)
    assert console.kicked == []

    # The list changes under the bot, as a shared list does.
    (tmp_path / "ips.txt").write_text("11.22.33.44\n", encoding="utf-8")
    plugin.load(plugin.lists[0])
    admin = _client(console, name="Su", ip="9.9.9.9", bits=128, cid="3")

    await _run(console, admin, "!banlistcheck")

    assert [c.name for c, _reason, _admin in console.kicked] == ["Bob"]


# -- through a real bot --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_listed_player_joining_a_real_server_is_kicked_over_rcon(tmp_path):
    """The whole path: a Call of Duty join line, the roster giving the bot an IP, the list matching
    it, and the kick reaching the server."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.core.game import PlayerInfo
    from b3.runtime.bot import Bot

    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str) -> str:
            self.commands.append(cmd)
            if cmd.startswith("status"):
                return ""
            return ""

    guid = "b" * 32
    listfile = tmp_path / "ips.txt"
    listfile.write_text("11.22.33.0 a whole network of cheaters\n", encoding="utf-8")

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin"), PluginEntry(name="banlist")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    admin = AdminPlugin(bot, None)
    bot.add_plugin(admin, "admin")
    plugin = BanlistPlugin(
        bot, {"lists": [{"kind": "ip", "name": "shared", "file": str(listfile)}]}
    )
    bot.add_plugin(plugin, "banlist")
    bot.start()
    admin.start()
    plugin.start()
    rcon.commands.clear()

    # Authenticated from the log first, with no address — the log line does not carry one. Then the
    # roster poll resolves it, which is the moment the IP list can say anything at all.
    await bot.replay([f"J;{guid};2;Bob"])
    await bot.bus.drain()
    assert not any("kick" in c for c in rcon.commands)  # nothing to match on yet

    bot._reconcile([PlayerInfo(cid="2", name="Bob", guid=guid, ip="11.22.33.77")])
    await bot.bus.drain()

    assert any("kick" in c for c in rcon.commands)
    stored = bot.storage.get_client_by_guid(guid)
    assert stored is not None
    assert bot.storage.get_active_penalties(stored.require_id(), PenaltyType.KICK)
    bot.storage.close()
