"""Drive a real Bot against the fake Source server, end to end.

    python -m tools.fakeservers.e2e_insurgency

Plays out a plausible coop round: a human and two bots join, the human claims superadmin, a team kill
happens, the admin bans the culprit and then lifts it, and the map is changed. Checks the outcome on
the wire, in the database and in the server's own ban list. Exits non-zero if anything fails.

The parts worth a running server for, none of which a unit test would catch:

* the **log and the commands travelling separately** — a file tailed for events, a TCP session for
  commands — which is the shape every Call of Duty title has and no other family here combines with a
  stateful login;
* a **reply split across packets** on a real socket, with the fake writing in small slices, so the
  sentinel that ends a reply is doing real work;
* the **status table's two row shapes** through the whole runtime, because a roster that omits the
  bots is one that makes the next sync disconnect every bot on the server;
* the **SourceMod requirement**, checked against a server that has it and against one that does not,
  since on this engine a bot without it starts and silently does nothing;
* and the **A2S query**, which is the only way to learn the hostname and the player counts here.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import Event, EventType
from b3.domain.client import PenaltyType
from b3.net.a2s import A2SClient
from b3.net.logsource import create_log_source
from b3.net.source import SourceRconClient
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot, MissingServerModError
from tools.fakeservers.source import FakeSourceServer

COURGETTE = "STEAM_1:0:1111111"
BADGER = "STEAM_1:0:2222222"

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def main() -> int:  # noqa: PLR0915 - a linear script reads better than helpers here
    tmp = Path(tempfile.mkdtemp(prefix="b3-insurgency-e2e-"))
    log_path = tmp / "console.log"
    log_path.write_text("", encoding="utf-8")

    # `fragment=48` makes the server answer in 48-byte packets: a reply that is only ever delivered
    # whole is not being tested, and this protocol says nothing about a reply being split.
    server = FakeSourceServer(password="test", log_path=log_path, fragment=48).start()
    try:
        client = SourceRconClient(*server.address, "test", timeout=3.0)
        config = Config(
            bot=BotConfig(database=f"sqlite:///{tmp / 'b3.sqlite'}", server_id="ins_1"),
            server=ServerConfig(
                game="insurgency",
                host=server.address[0],
                port=server.address[1],
                game_log=str(log_path),
            ),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=client)
        bot.add_plugin(AdminPlugin(bot), "admin")

        client.open()
        source = create_log_source(str(log_path), encoding="utf-8")
        source.open()
        bot.start()
        print(f"connected to the fake Source server on {server.address[0]}:{server.address[1]}")
        check(server.authed, "the RCON login handshake was accepted")
        check(
            server.sent_command("sm version"),
            "and SourceMod was checked for, because without it nothing this bot says arrives",
        )

        events: list[Event] = []
        for event_type in EventType:
            bot.bus.subscribe(event_type, lambda e: events.append(e))

        async def pump(rounds: int = 6) -> None:
            for _ in range(rounds):
                for line in source.read_lines():
                    await bot.feed_line(line)
                await asyncio.sleep(0.02)

        # -- the round -----------------------------------------------------
        server.load_map("buhriz")
        server.add_player("194", "courgette", COURGETTE, ip="11.222.111.222", ping=67)
        server.add_player("195", "Killer Badger", BADGER, ip="11.222.111.223", ping=42)
        server.add_player("224", "Moe")  # a bot, and the reason the table is two-shaped
        await pump()

        server.say_as("194", "!iamgod")
        await pump()

        # A team kill: both on Security, and a headshot, which is the only hit location this engine
        # reports at all.
        server.kill("195", "194", "m4a1", headshot=True)
        await pump()

        server.say_as("194", "!tempban Badger 2h teamkilling")
        await pump()
        await pump()  # the sm_addban and the disconnect it causes come back through the log

        seen = [e.type for e in events]
        print("\nwhat happened:")
        check(seen.count(EventType.CLIENT_JOIN) == 3, "all three players joined")
        check(bot.game.map_name == "buhriz", "the map was recorded from the log")
        check(EventType.CLIENT_KILL_TEAM in seen, "the team kill was classified as one")
        check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin")

        moe = bot.clients.get_by_cid("224")
        check(moe is not None, "the bot player is on the roster")
        check(moe is not None and moe.guid == "", "and has no identity, so bots cannot share a record")

        badger = bot.storage.get_client_by_guid(BADGER)
        check(badger is not None, "the culprit is in the database, keyed on his Steam id")
        if badger is not None:
            check(
                len(bot.storage.get_active_penalties(badger.require_id(), PenaltyType.TEMPBAN)) == 1,
                "the tempban was recorded",
            )

        print("\ncommands the bot sent:")
        for command in server.received:
            print(f"  {command}")

        bans = [c for c in server.received if c.startswith("sm_addban")]
        check(bool(bans), "the ban went out through SourceMod, the only verb this engine has for one")
        check(all(BADGER in c for c in bans), "by Steam id, so it holds across a name change")
        check(BADGER in server.bans, "it is on the server's own ban list")
        check(bot.clients.get_by_cid("195") is None, "and he left the roster")
        check(
            any(c.startswith(("sm_say", "sm_psay", "sm_hsay")) for c in server.received),
            "the bot answered in game through SourceMod",
        )

        # -- the roster, split across packets on the way back ---------------
        players = bot.get_players()
        check(
            sorted(p.cid for p in players) == ["194", "224"],
            f"the roster came back whole from a split reply ({[p.name for p in players]})",
        )
        check(
            any(p.guid == "BOT" for p in players),
            "including the AI row, which has no ping, rate or address at all -- a roster without it "
            "makes the next sync disconnect every bot on the server",
        )
        human = next((p for p in players if p.cid == "194"), None)
        check(human is not None and human.ip == "11.222.111.222", "the human's address was read")
        check(human is not None and human.ping == 67, "and their ping")

        # -- reconciliation, the thing a partial roster would break ---------
        before = len(bot.clients.connected())
        bot.sync()
        check(
            len(bot.clients.connected()) == before,
            f"a sync against the real table drops nobody ({before} before, "
            f"{len(bot.clients.connected())} after)",
        )

        # -- maps -----------------------------------------------------------
        maps = bot.get_maps()
        check(maps[:3] == ["buhriz", "district", "sinjar"], f"`maps *` lists what is installed ({maps[:3]})")
        check(
            bot.get_next_map() == "district",
            "and the next map comes from sm_nextmap rather than from that list's order, which is "
            "not a rotation",
        )

        server.received.clear()
        bot.change_map("market")
        await pump(3)
        check(server.sent_command("changelevel market"), "the map change went out as changelevel")
        check(server.map_name == "market", f"and the server is on it ({server.map_name})")

        # -- the query protocol, on the same port over UDP -------------------
        info = A2SClient(*server.address, timeout=2.0).info()
        check(info.hostname == server.hostname, f"A2S named the server ({info.hostname!r})")
        check(info.map_name == "market", "and agreed with it about the map")
        check(info.bots == 1, f"and counted the bots separately ({info.bots})")

        # -- lifting the ban -------------------------------------------------
        if badger is not None:
            bot.unban(badger, "appeal granted")
        await pump(2)
        check(BADGER not in server.bans, "!unban cleared the server's own ban list entry")

        bot.storage.close()
        source.close()
        client.close()
    finally:
        server.stop()

    # -- and the thing this engine will not work without ----------------------
    print("\nwithout SourceMod:")
    stock = FakeSourceServer(password="test", sourcemod=False, log_path=tmp / "stock.log").start()
    try:
        client = SourceRconClient(*stock.address, "test", timeout=1.0)
        client.open()
        config = Config(
            bot=BotConfig(database=f"sqlite:///{tmp / 'stock.sqlite'}", server_id="ins_2"),
            server=ServerConfig(game="insurgency", host=stock.address[0], port=stock.address[1]),
        )
        try:
            Bot(config, rcon=client).start()
            check(False, "a server without SourceMod is refused rather than silently useless")
        except MissingServerModError as exc:
            check(True, "a server without SourceMod is refused rather than silently useless")
            print(f"    -> {exc}")
        client.close()
    finally:
        stock.stop()

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
