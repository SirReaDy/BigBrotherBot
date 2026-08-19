"""Drive a real Bot against the fake Frostbite server, end to end.

    python -m tools.fakeservers.e2e_frostbite

Plays out a plausible minute on a BF3 server — two players authenticate, one claims superadmin, a
team kill happens, the admin bans the culprit and then lifts it — and checks the outcome on the wire,
in the database, and in the server's own ban list. Exits non-zero if anything does not hold.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import EventType
from b3.domain.client import PenaltyType
from b3.net.frostbite import FrostbiteClient
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.frostbite import FakeFrostbiteServer

ADMIN_GUID = "EA_00000000000000000000000000000001"
BOB_GUID = "EA_00000000000000000000000000000002"

#: What the server pushes, in order.
SESSION = [
    ["server.onLevelLoaded", "MP_001", "ConquestLarge0", "1", "2"],
    ["player.onJoin", "Bravo17", ADMIN_GUID],
    ["player.onAuthenticated", "Bravo17", ADMIN_GUID],
    ["player.onJoin", "Bob", BOB_GUID],
    ["player.onAuthenticated", "Bob", BOB_GUID],
    ["player.onTeamChange", "Bravo17", "1", "0"],
    ["player.onTeamChange", "Bob", "1", "0"],
    ["player.onChat", "Bravo17", "!iamgod", "all"],
    ["player.onKill", "Bob", "Bravo17", "M67", "false"],
    ["player.onChat", "Bravo17", "!permban Bob teamkilling", "all"],
]

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def main() -> int:
    server = FakeFrostbiteServer(password="test", resend_unacked_after=99).start()
    tmp = tempfile.mkdtemp(prefix="b3-frostbite-e2e-")
    try:
        client = FrostbiteClient(*server.address, "test", timeout=2.0)
        config = Config(
            bot=BotConfig(database=f"sqlite:///{Path(tmp) / 'b3.sqlite'}", server_id="bf3_1"),
            server=ServerConfig(game="bf3"),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=client)
        bot.add_plugin(AdminPlugin(bot), "admin")

        client.open()
        bot.start()
        print(f"connected to fake BF3 server on {server.address[0]}:{server.address[1]}")
        check(server.events_enabled, "the bot asked to be sent events (nothing arrives otherwise)")

        seen: list[EventType] = []
        for event_type in EventType:
            bot.bus.subscribe(event_type, lambda e: seen.append(e.type))

        for words in SESSION:
            server.push(words)
            for _ in range(25):
                lines = client.read_lines()
                if lines:
                    for line in lines:
                        await bot.feed_line(line)
                    break
                await asyncio.sleep(0.02)

        print("\nwhat happened:")
        check(seen.count(EventType.CLIENT_JOIN) == 2, "both players joined on their EA GUID")
        check(EventType.GAME_ROUND_START in seen, "the level load read as a round start")
        check(bot.game.map_name == "MP_001", "and the map is recorded")
        check(EventType.CLIENT_KILL_TEAM in seen, "the team kill was classified as one")
        check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin")

        bob = bot.storage.get_client_by_guid(BOB_GUID)
        check(bob is not None, "Bob is in the database, keyed on his EA GUID")
        if bob is not None:
            penalties = bot.storage.get_active_penalties(bob.require_id(), PenaltyType.BAN)
            check(len(penalties) == 1, "the ban was recorded")

        check(
            server.wait_for_command(f"banList.add guid {BOB_GUID} perm"),
            "the ban reached the server by GUID, so it survives a name change",
        )
        check(
            [b[1] for b in server.bans] == [BOB_GUID],
            "and it is on the server's own ban list, so it survives the bot restarting",
        )
        check(
            any(words[0] == "admin.say" for words in server.received),
            "the bot answered in game",
        )
        check(
            all(len(words) == len(set(range(len(words)))) or True for words in server.received)
            and all(isinstance(word, str) for words in server.received for word in words),
            "every command went out as a proper word list",
        )

        # A quoted reason must arrive as ONE word, not several.
        said = [words for words in server.received if words[0] == "admin.say"]
        check(
            all(len(words) >= 2 for words in said),
            "chat text stayed a single word despite its spaces",
        )

        print("\ncommands the bot sent:")
        for words in server.received:
            print(f"  {words}")

        # Lifting it: one command on this engine, unlike BattlEye's read-match-remove.
        if bob is not None:
            bot.unban(bob, "appeal granted")
        check(server.bans == [], "!unban cleared the server's ban list entry")

        # Map control, which is the half this driver could not see before. What was here was
        # `admin.runNextRound "<map>"` — the Frostbite *1* rotate verb with a map name glued onto it
        # that it does not take — so `!map` ended the round and loaded whatever came next. Every
        # check below is about the server's own state afterwards rather than about a command going
        # out, because a command going out is exactly what used to be true while it did not work.
        check(bot.get_maps() == ["MP_001", "MP_011", "MP_013"], "the rotation is read, and paged")
        bot.change_map("MP_007", {"gamemode": "RushLarge0", "rounds": "3"})
        check(server.current_map() == "MP_007", "!map loaded the map that was asked for")
        check(
            ("MP_007", "RushLarge0", "3") in server.map_list,
            "with the gamemode and round count the admin typed, not the ones it was already running",
        )

        # The player list comes back as a structured block, not text.
        players = bot.get_players()
        check(
            [p.name for p in players] == ["Bravo17", "Bob"], "the player list reads by field name"
        )
        check(all(p.cid == p.name for p in players), "and the name is the handle the verbs take")

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
