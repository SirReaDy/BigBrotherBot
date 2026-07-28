"""Drive a real Bot against the fake Altitude server, end to end.

    python -m tools.fakeservers.e2e_altitude

Plays out a plausible few minutes on an Altitude server — players connect, one claims superadmin,
another is banned and the ban is then lifted — and checks the outcome in the database, in the file
the bot writes commands to, and in the server's own ban list. Exits non-zero if anything fails.

The parts worth having a running server for, none of which a unit test would catch:

* every command is framed for **our** port, so a neighbouring server sharing the command file never
  executes it, and nothing malformed is ever written;
* a ban is **two** commands (the ban list does not remove the player who is already connected), and
  both arrive;
* the bot's own announcements come **back** through the log with ``server: true`` and are not read as
  a player talking — otherwise the bot answers itself;
* a log line from another server on the same machine is ignored;
* the command file is **empty** when the bot stops, so a server restarting cannot replay this
  session's kicks and bans against whoever holds those names later.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import Event, EventType
from b3.domain.client import PenaltyType
from b3.net.altitude import AltitudeCommandFile
from b3.net.logsource import FileLogSource
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.altitude import NOBODY, FakeAltitudeServer

PORT = 27276
NEIGHBOUR_PORT = 27277

ADMIN_ID = "a8654321-123a-414e-c71a-123123123131"
BOB_ID = "d6545616-17a3-4044-a74b-121231321321"

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="b3-altitude-e2e-"))
    server = FakeAltitudeServer(tmp / "server", port=PORT).start()

    rcon = AltitudeCommandFile(server.command_path, port=PORT)
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp / 'b3.sqlite'}", server_id="altitude_1"),
        server=ServerConfig(
            game="altitude",
            port=PORT,
            game_log=str(server.log_path),
            command_file=str(server.command_path),
        ),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, rcon=rcon)
    bot.add_plugin(AdminPlugin(bot), "admin")

    # The real log source, from the start of the file, so the JSON/encoding/tail path is exercised
    # rather than assumed.
    source = FileLogSource(str(server.log_path), encoding="utf-8", from_start=True)
    source.open()
    rcon.open()
    bot.start()
    print(f"reading {server.log_path}")
    print(f"writing {server.command_path}")

    events: list[Event] = []
    for event_type in EventType:
        bot.bus.subscribe(event_type, lambda e: events.append(e))

    def seen_types() -> list[EventType]:
        return [event.type for event in events]

    async def pump() -> None:
        """Give both halves a turn: the bot reads the log, the server reads the command file."""
        for _ in range(3):
            for line in source.read_lines():
                await bot.feed_line(line)
            server.poll()
            await asyncio.sleep(0.01)

    # -- the session ---------------------------------------------------------
    server.add_player(0, "Courgette", ADMIN_ID)
    server.add_player(1, "Bob", BOB_ID)
    server.add_player(2, "Rookie AI", NOBODY)  # a bot player: no identity
    server.push({"type": "mapChange", "map": "ball_cave", "mode": "ball"})
    server.push({"type": "teamChange", "player": 0, "team": 3})
    server.push({"type": "teamChange", "player": 1, "team": 4})
    await pump()

    server.say(0, "!iamgod")
    await pump()

    # A kill, and a plane flown into the ground (player -1 is the world, not a client).
    server.push({"type": "kill", "player": 1, "victim": 0, "source": "missile", "xp": 10})
    server.push({"type": "kill", "player": -1, "victim": 1, "source": "plane", "xp": 0})
    server.ping_summary({0: 42, 1: 87, 2: 0})
    await pump()

    # A neighbouring server on the same machine, writing into the same log.
    server.push({"type": "chat", "player": 9, "message": "!permban Courgette", "server": False},
                port=NEIGHBOUR_PORT)
    await pump()

    server.say(0, "!permban Bob cheating")
    await pump()
    await pump()  # the fake's kick lands in the log, which the bot then reads

    # The echo, made to look like an order: if the `server` flag were ignored, this would ban the
    # admin who is standing there. Nothing should happen at all.
    server.push({"type": "chat", "player": -1, "message": "!permban Courgette", "server": True})
    await pump()

    print("\nwhat happened:")
    seen = seen_types()
    check(seen.count(EventType.CLIENT_JOIN) == 3, "all three connections joined on clientAdd")
    check(bot.game.map_name == "ball_cave", "the map change was recorded (classic said 'warmup')")
    check(bot.game.gametype == "ball", "and so was the mode")
    check(EventType.CLIENT_KILL in seen, "the kill was published")
    check(EventType.CLIENT_SUICIDE in seen, "and the world kill became a suicide, not a WORLD client")
    check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin")

    admin = bot.storage.get_client_by_guid(ADMIN_ID)
    check(admin is not None, "the admin is in the database, keyed on their vapor id")
    check(
        bot.storage.get_client_by_guid(NOBODY) is None,
        "the bot player was NOT stored: the all-zero vapor id is not an identity",
    )

    bob = bot.storage.get_client_by_guid(BOB_ID)
    check(bob is not None, "Bob is in the database")
    if bob is not None:
        check(
            len(bot.storage.get_active_penalties(bob.require_id(), PenaltyType.BAN)) == 1,
            "the ban was recorded",
        )

    print("\ncommands the bot wrote:")
    for command in server.received:
        print(f"  {command}")

    check(
        server.wait_for_command(f"addBan {BOB_ID} 1 forever"),
        "the ban went out by vapor id, so it survives a name change",
    )
    check(server.wait_for_command("kick Bob"), "and a kick followed it: addBan alone leaves them in")
    check(BOB_ID in server.bans, "it is on the server's own ban list")
    check(1 not in server.players, "the fake executed the kick, so Bob is off the server")
    check(server.malformed == [], "nothing malformed was ever written to the command file")
    check(
        server.for_other_ports == [],
        "and nothing was addressed to another server sharing that file",
    )
    check(
        any(c.startswith("serverWhisper Courgette ") for c in server.received),
        "the bot answered the admin by name, which is the only handle whisper takes",
    )
    check(
        not any("!permban Courgette" in c for c in server.received),
        "the neighbouring server's chat line was ignored, not acted on",
    )

    # The echo: the bot's own output comes back through the log, and must never be read as a player
    # talking. The last one was written to look like an order.
    echoes = [
        event
        for event in events
        if event.type is EventType.CUSTOM and event.extra.get("kind") == "server_message"
    ]
    check(len(echoes) >= 2, f"the bot's own output came back as CUSTOM ({len(echoes)} lines)")
    check(
        not any(
            event.type is EventType.CLIENT_SAY and str(event.data).startswith("!permban Courgette")
            for event in events
        ),
        "and an echo that looked like a command was not obeyed",
    )
    if admin is not None:
        check(
            bot.storage.get_active_penalties(admin.require_id(), PenaltyType.BAN) == [],
            "so the admin was never banned by the bot's own announcement",
        )
    check(
        bot.clients.get_by_cid("1") is None,
        "Bob left the roster when the server reported the kick",
    )

    players = bot.get_players()
    check(
        {p.cid for p in players} == {"0", "2"},
        "the roster is what the log says, since this engine cannot be asked",
    )
    check(
        any(p.ping == 42 for p in players),
        "and pings come from the last ping report",
    )

    if bob is not None:
        bot.unban(bob, "appeal granted")
        server.poll()
    check(BOB_ID not in server.bans, "!unban cleared the server's own ban list entry")

    # -- shutdown, and the reason this fake exists ---------------------------
    source.close()
    rcon.close()
    replayed = server.restart()
    check(
        replayed == [],
        "the command file was left empty, so a restarting server replays nothing",
    )

    bot.storage.close()

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
