"""Authentication: ban enforcement on reconnect, and name/IP history recording.

These are the two behaviours the classic bot had that the rewrite was missing (see PARITY.md P0/P1).
Driven through a real Bot with real SQLAlchemy storage, because the point is the wiring.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.events import EventType
from b3.domain.client import NEVER_EXPIRES, Penalty, PenaltyType
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot

GBOB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
GADMIN = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class FakeRcon:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


def _bot(tmp_path, clock: FakeClock | None = None):
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="admin")],
    )
    rcon = FakeRcon()
    bot = Bot(config, rcon=rcon, clock=clock or FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    return bot, rcon


# -- P0: bans are enforced on reconnect ------------------------------------


@pytest.mark.asyncio
async def test_permanently_banned_player_is_rebanned_on_rejoin(tmp_path):
    bot, rcon = _bot(tmp_path)

    # Session 1: Bob joins and is banned.
    await bot.replay([f"J;{GADMIN};1;Admin", f"J;{GBOB};2;Bob", "say;x;1;Admin;!iamgod"])
    await bot.feed_line("say;x;1;Admin;!permban Bob cheating")
    bob = bot.storage.get_client_by_guid(GBOB)
    assert len(bot.storage.get_active_penalties(bob.id, PenaltyType.BAN)) == 1

    # Session 2: a fresh bot (server restarted, banlist gone) and Bob reconnects.
    bot2, rcon2 = _bot(tmp_path)
    events: list = []
    bot2.bus.subscribe(EventType.CLIENT_BAN, lambda e: events.append(e))
    await bot2.feed_line(f"J;{GBOB};7;Bob")

    # He is thrown out again, on his new slot id...
    assert "banclient 7" in rcon2.commands
    assert len(events) == 1
    # ...without a second penalty being recorded (that would inflate his ban history).
    assert len(bot2.storage.get_active_penalties(bob.id, PenaltyType.BAN)) == 1
    # ...and he is not authenticated, so he cannot issue commands.
    client = bot2.clients.get_by_cid("7")
    assert client.authed is False


@pytest.mark.asyncio
async def test_the_ban_is_reissued_once_not_per_line(tmp_path):
    """A rejected client must not re-trigger banclient for every buffered log line."""
    bot, _ = _bot(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!permban Bob cheating",
        ]
    )

    bot2, rcon2 = _bot(tmp_path)
    await bot2.replay(
        [f"J;{GBOB};9;Bob", "say;x;9;Bob;!help", "say;x;9;Bob;hello?", "say;x;9;Bob;!iamgod"]
    )
    assert rcon2.commands.count("banclient 9") == 1
    assert bot2.clients.get_by_cid("9").rejected is True


@pytest.mark.asyncio
async def test_banned_player_cannot_run_commands(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay([f"J;{GADMIN};1;Admin", "say;x;1;Admin;!iamgod"])

    # Make Bob a superadmin, then ban him: level must not save him.
    await bot.replay([f"J;{GBOB};2;Bob", "say;x;1;Admin;!permban Bob cheating"])

    bot2, rcon2 = _bot(tmp_path)
    await bot2.replay([f"J;{GBOB};2;Bob", "say;x;2;Bob;!help"])
    replies = [c for c in rcon2.commands if c.startswith("tell 2")]
    assert replies == []  # no command output at all for a banned client


@pytest.mark.asyncio
async def test_tempban_still_running_is_reapplied_with_remaining_time(tmp_path):
    clock = FakeClock()
    bot, _ = _bot(tmp_path, clock)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!tempban Bob 60m spam",
        ]
    )
    bob = bot.storage.get_client_by_guid(GBOB)
    assert len(bot.storage.get_active_penalties(bob.id, PenaltyType.TEMPBAN)) == 1

    bot2, rcon2 = _bot(tmp_path, clock)
    events: list = []
    bot2.bus.subscribe(EventType.CLIENT_BAN_TEMP, lambda e: events.append(e))
    await bot2.feed_line(f"J;{GBOB};3;Bob")

    assert "banclient 3" in rcon2.commands
    assert len(events) == 1
    assert bot2.clients.get_by_cid("3").authed is False


@pytest.mark.asyncio
async def test_expired_tempban_lets_the_player_back_in(tmp_path):
    clock = FakeClock()
    bot, _ = _bot(tmp_path, clock)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!tempban Bob 30m spam",
        ]
    )

    clock.advance(31 * 60)  # the tempban has run out

    bot2, rcon2 = _bot(tmp_path, clock)
    await bot2.feed_line(f"J;{GBOB};4;Bob")

    assert "banclient 4" not in rcon2.commands
    assert bot2.clients.get_by_cid("4").authed is True


@pytest.mark.asyncio
async def test_a_lifted_ban_is_not_reapplied(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!permban Bob cheating",
        ]
    )
    bob = bot.storage.get_client_by_guid(GBOB)
    bot.storage.disable_penalties(bob.id, PenaltyType.BAN)

    bot2, rcon2 = _bot(tmp_path)
    await bot2.feed_line(f"J;{GBOB};5;Bob")
    assert "banclient 5" not in rcon2.commands
    assert bot2.clients.get_by_cid("5").authed is True


@pytest.mark.asyncio
async def test_a_kick_is_not_a_ban(tmp_path):
    """Kicks are recorded with time_expire=-1 but must never block a reconnect."""
    bot, _ = _bot(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!kick Bob language",
        ]
    )
    bob = bot.storage.get_client_by_guid(GBOB)
    assert len(bot.storage.get_active_penalties(bob.id, PenaltyType.KICK)) == 1

    bot2, rcon2 = _bot(tmp_path)
    await bot2.feed_line(f"J;{GBOB};6;Bob")
    assert "banclient 6" not in rcon2.commands
    assert bot2.clients.get_by_cid("6").authed is True


@pytest.mark.asyncio
async def test_client_auth_event_is_published(tmp_path):
    """The classic bot fired EVT_CLIENT_AUTH; plugins rely on it as "identity is known now"."""
    bot, _ = _bot(tmp_path)
    authed: list = []
    bot.bus.subscribe(EventType.CLIENT_AUTH, lambda e: authed.append(e))

    await bot.feed_line(f"J;{GBOB};2;Bob")
    assert [e.client.name for e in authed] == ["Bob"]


@pytest.mark.asyncio
async def test_rejected_client_publishes_no_auth_event(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!permban Bob cheating",
        ]
    )

    bot2, _ = _bot(tmp_path)
    authed: list = []
    bot2.bus.subscribe(EventType.CLIENT_AUTH, lambda e: authed.append(e))
    await bot2.feed_line(f"J;{GBOB};9;Bob")
    assert authed == []


# -- P1: name and IP history are recorded ---------------------------------


@pytest.mark.asyncio
async def test_name_is_recorded_as_an_alias_on_join(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.feed_line(f"J;{GBOB};2;Bob")

    bob = bot.storage.get_client_by_guid(GBOB)
    assert [a.value for a in bot.storage.get_aliases(bob.id)] == ["Bob"]


@pytest.mark.asyncio
async def test_rejoining_bumps_num_used_rather_than_duplicating(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay([f"J;{GBOB};2;Bob", f"Q;{GBOB};2;Bob"])

    bot2, _ = _bot(tmp_path)
    await bot2.feed_line(f"J;{GBOB};2;Bob")

    bob = bot2.storage.get_client_by_guid(GBOB)
    aliases = bot2.storage.get_aliases(bob.id)
    assert len(aliases) == 1
    assert aliases[0].num_used == 2


@pytest.mark.asyncio
async def test_chatting_does_not_inflate_num_used(tmp_path):
    """num_used counts times a name was adopted — not every line the player says."""
    bot, _ = _bot(tmp_path)
    await bot.replay(
        [f"J;{GBOB};2;Bob", "say;x;2;Bob;hello", "say;x;2;Bob;anyone there", "say;x;2;Bob;!help"]
    )

    bob = bot.storage.get_client_by_guid(GBOB)
    aliases = bot.storage.get_aliases(bob.id)
    assert len(aliases) == 1
    assert aliases[0].num_used == 1


@pytest.mark.asyncio
async def test_a_new_name_is_added_to_the_history(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay([f"J;{GBOB};2;Bob", f"Q;{GBOB};2;Bob", f"J;{GBOB};2;RobertoTheGreat"])

    bob = bot.storage.get_client_by_guid(GBOB)
    assert {a.value for a in bot.storage.get_aliases(bob.id)} == {"Bob", "RobertoTheGreat"}


@pytest.mark.asyncio
async def test_ip_is_recorded_when_known(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.feed_line(f"J;{GBOB};2;Bob")

    # v1 join lines carry no IP; simulate the status-poll resolver having filled it in.
    client = bot.clients.get_by_cid("2")
    client.ip = "10.0.0.7"
    bot._record_history(client)

    bob = bot.storage.get_client_by_guid(GBOB)
    assert [ip.value for ip in bot.storage.get_ip_aliases(bob.id)] == ["10.0.0.7"]


@pytest.mark.asyncio
async def test_history_is_searchable_by_a_past_name(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay([f"J;{GBOB};2;Bob", f"Q;{GBOB};2;Bob", f"J;{GBOB};2;NewName"])

    # Found by the name they use now...
    assert [c.guid for c in bot.lookup_clients("NewName")] == [GBOB]
    # ...and by the one they used before.
    found = bot.storage.search_clients("Bob")
    assert [c.guid for c in found] == [GBOB]


@pytest.mark.asyncio
async def test_lookup_finds_an_offline_player_by_db_id(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay([f"J;{GBOB};2;Bob", f"Q;{GBOB};2;Bob"])
    bob = bot.storage.get_client_by_guid(GBOB)

    assert [c.id for c in bot.lookup_clients(f"@{bob.id}")] == [bob.id]
    assert bot.lookup_clients("@999999") == []
    assert bot.lookup_clients("@notanumber") == []
    assert bot.lookup_clients("  ") == []


# -- P1: unban ------------------------------------------------------------


@pytest.mark.asyncio
async def test_unban_lifts_penalties_and_tells_the_server(tmp_path):
    bot, rcon = _bot(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!permban Bob cheating",
        ]
    )
    bob = bot.storage.get_client_by_guid(GBOB)
    unbans: list = []
    bot.bus.subscribe(EventType.CLIENT_UNBAN, lambda e: unbans.append(e))

    bot.unban(bob, reason="appealed")
    await bot.bus.drain()

    assert "unbanuser Bob" in rcon.commands
    assert bot.storage.get_active_penalties(bob.id, PenaltyType.BAN) == []
    assert len(unbans) == 1
    # Soft-delete only: the row survives for the audit trail.
    with bot.storage._session_factory() as s:  # noqa: SLF001 - asserting the audit trail
        from b3.storage.models import PenaltyRow

        rows = s.query(PenaltyRow).filter(PenaltyRow.client_id == bob.id).all()
        assert [r.inactive for r in rows] == [1]


@pytest.mark.asyncio
async def test_unbanned_player_can_rejoin(tmp_path):
    bot, _ = _bot(tmp_path)
    await bot.replay(
        [
            f"J;{GADMIN};1;Admin",
            "say;x;1;Admin;!iamgod",
            f"J;{GBOB};2;Bob",
            "say;x;1;Admin;!permban Bob cheating",
        ]
    )
    bob = bot.storage.get_client_by_guid(GBOB)
    bot.unban(bob)

    bot2, rcon2 = _bot(tmp_path)
    await bot2.feed_line(f"J;{GBOB};3;Bob")
    assert "banclient 3" not in rcon2.commands
    assert bot2.clients.get_by_cid("3").authed is True


@pytest.mark.asyncio
async def test_ban_by_database_id_while_offline_is_enforced_on_return(tmp_path):
    """The case the game server's own banlist cannot cover."""
    bot, _ = _bot(tmp_path)
    await bot.replay([f"J;{GBOB};2;Bob", f"Q;{GBOB};2;Bob"])
    bob = bot.storage.get_client_by_guid(GBOB)

    bot.storage.add_penalty(
        Penalty(
            type=PenaltyType.BAN,
            client_id=bob.id,
            reason="reviewed demo",
            time_expire=NEVER_EXPIRES,
        )
    )

    bot2, rcon2 = _bot(tmp_path)
    await bot2.feed_line(f"J;{GBOB};8;Bob")
    assert "banclient 8" in rcon2.commands
    assert bot2.clients.get_by_cid("8").authed is False
