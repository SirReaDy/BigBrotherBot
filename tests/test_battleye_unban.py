"""Lifting a BattlEye ban, which no single command can express.

`removeBan` takes a **row number in the ban list**, not a player id, so an unban is read-match-remove.
Two things make that worth testing carefully rather than trusting: the GUID and IP lists are numbered
*separately*, so an index taken from the wrong one lifts a stranger's ban; and removing a row
renumbers the rows after it.
"""

from __future__ import annotations

import pytest

from b3.net.battleye import BattleyeClient
from b3.parsers.battleye.status import parse_bans
from tools.fakeservers.battleye import FakeBattleyeServer

GUID = "80a5885ebe2420bab5e1581234567890"
GUID2 = "04b81a0bd914e7ba610ef31234567890"


@pytest.fixture
def server():  # noqa: ANN201
    fake = FakeBattleyeServer(
        password="test",
        resend_unacked_after=99,
        guid_bans=[(GUID, "perm", "cheating"), (GUID2, "120", "abuse")],
        ip_bans=[("198.51.100.7", "perm", "proxy")],
    )
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def client(server):  # noqa: ANN001, ANN201
    conn = BattleyeClient(*server.address, "test", timeout=2.0)
    conn.open()
    yield conn
    conn.close()


# -- reading the list ------------------------------------------------------


def test_the_two_sections_are_numbered_separately():
    """Both lists start at 0. Confusing them is how the wrong person gets unbanned."""
    bans = parse_bans(
        "GUID Bans:\n"
        "[#] [GUID] [Minutes left] [Reason]\n"
        "----------\n"
        f"0  {GUID} perm cheating\n"
        "\n"
        "IP Bans:\n"
        "[#] [IP Address] [Minutes left] [Reason]\n"
        "----------\n"
        "0  198.51.100.7 perm proxy\n"
    )
    assert [(b.index, b.kind, b.target) for b in bans] == [
        (0, "guid", GUID),
        (0, "ip", "198.51.100.7"),
    ]


def test_a_permanent_ban_and_a_timed_one():
    bans = parse_bans(f"GUID Bans:\n0  {GUID} perm forever\n1  {GUID2} 120 two hours\n")
    assert bans[0].minutes is None
    assert bans[1].minutes == 120
    assert bans[1].reason == "two hours"


def test_a_ban_with_no_reason():
    (ban,) = parse_bans(f"GUID Bans:\n0  {GUID} perm\n")
    assert ban.reason == ""


def test_headers_dashes_and_totals_are_skipped():
    assert parse_bans("GUID Bans:\n[#] [GUID] [Minutes left] [Reason]\n---\n(0 bans)\n") == []


def test_a_truncated_reply_yields_what_it_can():
    """It arrives over UDP; a cut-short reply must not lose the rows that did arrive."""
    bans = parse_bans(f"GUID Bans:\n0  {GUID} perm cheating\n1  04b81a0bd914e7ba6")
    assert [b.target for b in bans] == [GUID]


# -- lifting one -----------------------------------------------------------


def test_removing_a_ban_by_guid(client, server):  # noqa: ANN001
    assert client.remove_ban(GUID) is True
    assert [t for t, _m, _r in server.guid_bans] == [GUID2]  # and only that one went


def test_the_ip_list_is_left_alone(client, server):  # noqa: ANN001
    """Row 0 exists in both lists. Only the GUID one is ours to touch."""
    client.remove_ban(GUID)
    assert server.ip_bans == [("198.51.100.7", "perm", "proxy")]


def test_removing_a_ban_that_is_not_there_is_success_not_failure(client):  # noqa: ANN001
    """The caller asked for "not banned", and that is already the case."""
    assert client.remove_ban("f" * 32) is True


def test_several_entries_for_one_player_all_go(client, server):  # noqa: ANN001
    """Removing a row renumbers the rest, so these are removed highest-index first."""
    server.guid_bans.append((GUID, "60", "again"))
    server.guid_bans.append((GUID, "perm", "and again"))
    assert client.remove_ban(GUID) is True
    assert [t for t, _m, _r in server.guid_bans] == [GUID2]


def test_a_removal_that_does_not_take_is_reported_as_failure(client, server):  # noqa: ANN001
    """Verified by re-reading the list: claiming success unchecked is how a ban quietly survives."""
    server._remove_ban = lambda _argument: "Ban not found"  # type: ignore[method-assign]
    assert client.remove_ban(GUID) is False


def test_an_empty_guid_is_refused(client):  # noqa: ANN001
    """A player with no id would otherwise match the first row with an empty target."""
    assert client.remove_ban("") is False


# -- through the bot -------------------------------------------------------


def test_unban_through_the_console_reaches_the_server(server, tmp_path):  # noqa: ANN001
    """`!unban` on Arma: the profile has no template, so the Bot asks the client to do it."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.domain.client import Client, Penalty, PenaltyType
    from b3.plugins.admin import AdminPlugin
    from b3.runtime.bot import Bot

    conn = BattleyeClient(*server.address, "test", timeout=2.0)
    conn.open()
    try:
        config = Config(
            bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
            server=ServerConfig(game="arma3"),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=conn)
        bot.add_plugin(AdminPlugin(bot), "admin")
        bot.start()

        bob = bot.storage.save_client(Client(guid=GUID, name="Bravo17", cid="0"))
        bot.storage.add_penalty(
            Penalty(type=PenaltyType.BAN, client_id=bob.require_id(), reason="cheating")
        )
        bot.clients.add(bob)

        bot.unban(bob, "appeal granted")

        assert bot.storage.get_active_penalties(bob.require_id(), PenaltyType.BAN) == []
        assert [t for t, _m, _r in server.guid_bans] == [GUID2]  # the server's own list too
        bot.storage.close()
    finally:
        conn.close()


def test_a_ban_and_then_an_unban_round_trip(server, tmp_path):  # noqa: ANN001
    """The whole point: what the bot bans, the bot can lift — server-side included."""
    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.domain.client import Client, PenaltyType
    from b3.plugins.admin import AdminPlugin
    from b3.runtime.bot import Bot

    server.guid_bans.clear()
    # The server knows which id is in which slot — that is how `ban <slot>` reaches the right one.
    # Normally it learns that from the identity message it pushes; here nobody connected, so say so.
    server.slot_guids["0"] = GUID
    conn = BattleyeClient(*server.address, "test", timeout=2.0)
    conn.open()
    try:
        config = Config(
            bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
            server=ServerConfig(game="arma3"),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=conn)
        bot.add_plugin(AdminPlugin(bot), "admin")
        bot.start()

        bob = Client(guid=GUID, name="Bravo17", cid="0")
        bot.clients.add(bob)
        bot.ban(bob, "cheating")
        assert server.wait_for_command(f"addBan {GUID} 0 cheating")
        assert server.wait_for_command("kick 0 cheating")
        assert [t for t, _m, _r in server.guid_bans] == [GUID]

        # And it survives the server being restarted, which is the whole job of `writeBans`.
        assert server.wait_for_command("writeBans")
        server.restart()
        assert [t for t, _m, _r in server.guid_bans] == [GUID]

        bot.unban(bob, "appeal granted")
        assert server.guid_bans == []
        # The unban has to be saved too, or the restart brings the ban back and nothing lifts it
        # a second time: the bot's own database now records the penalty as already gone.
        server.restart()
        assert server.guid_bans == []
        assert bot.storage.get_active_penalties(bob.require_id(), PenaltyType.BAN) == []
        bot.storage.close()
    finally:
        conn.close()
