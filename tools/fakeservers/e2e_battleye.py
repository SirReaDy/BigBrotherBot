"""Drive a real Bot against the fake BattlEye server, end to end.

The unit tests check the pieces; this checks the thing. It builds an actual :class:`b3.runtime.bot.Bot`
with the admin plugin and a real SQLite database, points it at a fake Arma server, and plays out a
session: two players connect and are identified, one bootstraps himself as superadmin, bans the other,
and the ban is checked on the wire *and* in the database.

    python -m tools.fakeservers.e2e_battleye

Exits non-zero and prints what failed if any step does not hold, so it can gate a release.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import EventType
from b3.domain.client import PenaltyType
from b3.net.battleye import BattleyeClient
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.battleye import FakeBattleyeServer

ADMIN_GUID = "80a5885ebe2420bab5e1581234567890"
BOB_GUID = "04b81a0bd914e7ba610ef31234567890"

#: What the server pushes, in order — a plausible few seconds on an Arma server.
SESSION = [
    "Player #0 Bravo17 (76.108.91.78:2304) connected",
    f"Verified GUID ({ADMIN_GUID}) of player #0 Bravo17",
    "Player #2 Bob (198.51.100.7:2304) connected",
    f"Verified GUID ({BOB_GUID}) of player #2 Bob",
    "(Global) Bravo17: !iamgod",
    "(Side) Bob: they are behind the ridge",
    "(Global) Bravo17: !permban Bob cheating",
]

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def main() -> int:
    server = FakeBattleyeServer(password="test", resend_unacked_after=99).start()
    tmp = tempfile.mkdtemp(prefix="b3-battleye-e2e-")
    try:
        client = BattleyeClient(*server.address, "test", timeout=2.0)
        config = Config(
            bot=BotConfig(
                database=f"sqlite:///{Path(tmp) / 'b3.sqlite'}",
                server_id="arma3_1",
            ),
            server=ServerConfig(game="arma3"),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=client)
        bot.add_plugin(AdminPlugin(bot), "admin")

        client.open()
        bot.start()
        print(f"connected to fake Arma server on {server.address[0]}:{server.address[1]}")

        seen: list[EventType] = []
        for event_type in EventType:
            bot.bus.subscribe(event_type, lambda e: seen.append(e.type))

        # Play the session out through the same path the live loop uses: push, then read_lines.
        for message in SESSION:
            server.push(message)
            for _ in range(20):
                lines = client.read_lines()
                if lines:
                    for line in lines:
                        await bot.feed_line(line)
                    break
                await asyncio.sleep(0.02)

        print("\nwhat happened:")
        check(EventType.CLIENT_JOIN in seen, "players joined on their verified GUID")
        check(seen.count(EventType.CLIENT_JOIN) == 2, "both players joined, once each")
        check(EventType.CLIENT_TEAM_SAY in seen, "side chat arrived as team chat")
        check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin")

        bob = bot.storage.get_client_by_guid(BOB_GUID)
        check(bob is not None, "Bob is in the database, keyed on his BattlEye GUID")
        if bob is not None:
            penalties = bot.storage.get_active_penalties(bob.id, PenaltyType.BAN)
            check(len(penalties) == 1, "the ban was recorded")
            check(
                bool(penalties) and penalties[0].server_id == "arma3_1",
                "the ban records which server issued it",
            )

        # Three commands, not one. `addBan` keys on the GUID, so it works for a player who is not
        # standing in a slot; the kick is separate because `addBan` does not remove anybody; and
        # `writeBans` is what makes the ban survive a restart of the game server.
        check(
            server.wait_for_command(f"addBan {BOB_GUID} 0 cheating"),
            "the ban reached the server as `addBan <guid> 0 <reason>`",
        )
        check(
            server.wait_for_command("kick 2 cheating"),
            "...and the player was kicked, which `addBan` does not do",
        )
        check(
            server.wait_for_command("writeBans"),
            "...and the ban list was saved, so it survives a server restart",
        )
        check(
            any(c.startswith("say ") for c in server.received),
            "the bot answered in game",
        )
        check(
            all("\n" not in c for c in server.received),
            "no command carried a newline",
        )

        print("\ncommands the bot sent:")
        for cmd in server.received:
            print(f"  {cmd}")

        # Paced chat: the reply to `!permban` is still queued, because an Arma server drops rapid
        # `say`s. Prove it is queued and not lost — the failure mode that pacing could introduce.
        for _ in range(30):
            client.read_lines()
            if len([c for c in server.received if c.startswith("say ")]) >= 2:
                break
            await asyncio.sleep(0.1)
        check(
            len([c for c in server.received if c.startswith("say ")]) >= 2,
            "chat held back by pacing is released, not dropped",
        )

        # And prove the connection survives a quiet spell rather than being dropped.
        client.read_lines()
        check(client.connected, "the connection is still up after the session")

        # `!unban` is a read-match-remove sequence on this engine, not one command.
        check(
            [t for t, _m, _r in server.guid_bans] == [BOB_GUID],
            "the ban is on the server's own list",
        )
        if bob is not None:
            bot.unban(bob, "appeal granted")
        check(server.guid_bans == [], "!unban cleared the server's ban list entry too")

        # Nothing to ask, so nothing should be asked: on an engine with no rotation cvar, `!maps`
        # used to frame an empty command — a keepalive — and stall for the whole rcon timeout.
        before = len(server.received)
        check(bot.get_maps() == [], "`!maps` answers nothing on an engine with no rotation")
        check(len(server.received) == before, "...and does not send a pointless command to ask")

        # Losing the server must not leave the bot deaf and silent about it.
        server.stop()
        client._last_heard -= 1000  # pretend the silence has gone on long enough
        client.read_lines()
        check(not client.connected, "a vanished server is noticed, not ignored")
        check(client._retry_at > 0, "and a reconnect is scheduled")

        bot.storage.close()
        client.close()
    finally:
        server.stop()

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED:")
        for description in failures:
            print(f"  - {description}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
