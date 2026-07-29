"""Drive a real Bot against the fake Homefront server, end to end.

    python -m tools.fakeservers.e2e_homefront

Plays out a plausible few minutes: two players connect, one claims superadmin, a team kill happens,
the admin bans the culprit and the ban is then lifted. Checks the outcome on the wire, in the database
and in the server's own ban list. Exits non-zero if anything fails.

The parts worth a running server for, none of which a unit test would catch:

* the **asymmetric framing** — six bytes of header out, seven back — end to end through a real socket;
* the **SHA1 login**, which the fake verifies rather than assumes: an unspaced or lowercase digest is
  rejected here exactly as the game rejects it;
* the **keepalive**, because the server hangs up after ten seconds of silence and this fake really
  does that;
* a **fragmented stream**, because the fake can write its replies in small slices, which is the only
  way to know the reassembly is real rather than lucky;
* the **chatter channel repeating itself**, including the bot's own announcements, which a bot that
  reads them as player speech answers forever.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import Event, EventType
from b3.domain.client import PenaltyType
from b3.net.homefront import HomefrontClient
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.homefront import FakeHomefrontServer

ADMIN_ID = "76561197963239764"
BOB_ID = "76561197963239765"

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def main() -> int:
    # `fragment=7` makes the server write every reply in seven-byte slices: the reassembly has to be
    # real. `idle_timeout=0` disables the ten-second hangup for the session, which is exercised on its
    # own at the end rather than racing the rest of the run.
    server = FakeHomefrontServer(password="test", fragment=7, idle_timeout=0).start()
    tmp = tempfile.mkdtemp(prefix="b3-homefront-e2e-")
    try:
        client = HomefrontClient(*server.address, "test", timeout=1.0, playerlist_interval=0)
        config = Config(
            bot=BotConfig(database=f"sqlite:///{Path(tmp) / 'b3.sqlite'}", server_id="hf_1"),
            server=ServerConfig(game="homefront"),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=client)
        bot.add_plugin(AdminPlugin(bot), "admin")

        client.open()
        bot.start()
        print(f"connected to the fake Homefront server on {server.address[0]}:{server.address[1]}")
        check(server.authed, "the SHA1 login was accepted (the password never crossed the wire)")
        check(client.server_version == "1.0.0.0", "and the server's HELLO was read")

        events: list[Event] = []
        for event_type in EventType:
            bot.bus.subscribe(event_type, lambda e: events.append(e))

        async def pump(rounds: int = 6) -> None:
            for _ in range(rounds):
                for line in client.read_lines():
                    await bot.feed_line(line)
                await asyncio.sleep(0.02)

        # -- the session ---------------------------------------------------
        server.add_player(ADMIN_ID, "Courgette", team="1")
        server.add_player(BOB_ID, "Bob the Builder", team="1")
        server.push("CHANGE LEVEL: fl-harbor")
        await pump()

        server.say_as("Courgette", "!iamgod")
        await pump()

        # A team kill: same team, and the victim resolves by *name* while the killer resolves by id --
        # this engine puts either form in the same field.
        server.push(f"KILL: {BOB_ID} EXP_Frag Courgette")
        server.push("PLAYERPING: %s 48" % ADMIN_ID)
        await pump()

        # "Bob" rather than his full name: an unquoted name with spaces in it would be read as a
        # name plus a reason, which is the admin plugin working correctly and not what is under test.
        server.say_as("Courgette", "!permban Bob cheating")
        await pump()
        await pump()  # the server's BAN ADDED and LOGOUT come back

        seen = [e.type for e in events]
        print("\nwhat happened:")
        check(seen.count(EventType.CLIENT_JOIN) == 2, "both players joined on their Steam id")
        check(bot.game.map_name == "fl-harbor", "the level change was recorded")
        check(EventType.CLIENT_KILL_TEAM in seen, "the team kill was classified as one")
        check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin")

        bob = bot.storage.get_client_by_guid(BOB_ID)
        check(bob is not None, "Bob is in the database, keyed on his Steam id")
        if bob is not None:
            check(
                len(bot.storage.get_active_penalties(bob.require_id(), PenaltyType.BAN)) == 1,
                "the ban was recorded",
            )

        print("\ncommands the bot sent:")
        for command in server.received:
            print(f"  {command if not command.startswith('PASS') else 'PASS: <sha1>'}")

        check(
            server.sent_command(f'admin kickban "{BOB_ID}"'),
            "the ban went out by Steam id, so it holds across a name change",
        )
        check(
            any('"Courgette"' in c for c in server.received if c.startswith("admin kickban")),
            "and it names the admin who issued it, which this engine's verb requires",
        )
        check(BOB_ID in server.bans, "it is on the server's own ban list")
        check(bot.clients.get_by_cid("Bob the Builder") is None, "and Bob left the roster")
        # Either verb counts: this reply went out as a private message, because `!iamgod` and
        # `!permban` answer the admin quietly rather than announcing to the server.
        check(
            server.sent_command("adminsay ") or server.sent_command("adminpm "),
            "the bot answered in game",
        )
        check(server.malformed == [], "every packet the bot sent was framed correctly")

        # The echo: the bot's own announcements come back on the chatter channel. They must not be
        # read as somebody talking, or the bot answers itself.
        echoes = [
            e for e in events if e.type is EventType.CUSTOM and e.extra.get("kind") == "server_say"
        ]
        check(bool(echoes), f"the server's chatter repeats were reported as custom ({len(echoes)})")
        check(
            not any(
                e.type is EventType.CLIENT_SAY and str(e.data).startswith("Server:") for e in events
            ),
            "and none of them was read as a player speaking",
        )

        # The roster, which cannot be asked for synchronously.
        server.received.clear()
        client.command("RETRIEVE PLAYERLIST")
        await pump()
        players = bot.get_players()
        check(
            [p.cid for p in players] == ["Courgette"],
            "the roster comes from the pushed PLAYER messages",
        )
        check(any(p.ping == 48 for p in players), "and pings from the pushed PLAYERPING messages")

        # Lifting it.
        if bob is not None:
            bot.unban(bob, "appeal granted")
        await pump()
        check(BOB_ID not in server.bans, "!unban cleared the server's own ban list entry")

        bot.storage.close()
        client.close()
    finally:
        server.stop()

    # -- and the thing this protocol will not forgive ----------------------
    print("\nthe ten-second hangup:")
    strict = FakeHomefrontServer(password="test", idle_timeout=0.4).start()
    try:
        # ping_interval far beyond the test's window, so this client really does stay silent.
        quiet = HomefrontClient(*strict.address, "test", timeout=0.2, ping_interval=99)
        quiet.open()
        check(quiet.connected, "a fresh connection is up")
        await asyncio.sleep(0.8)  # say nothing at all
        quiet.read_lines()
        quiet.read_lines()
        check(strict.dropped_for_silence, "a client that stops talking really is hung up on")
    finally:
        strict.stop()

    # -- and coming back from it -------------------------------------------
    print("\nsurviving a restart:")
    restarting = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(
            *restarting.address, "test", timeout=0.1, playerlist_interval=0, ping_interval=99
        )
        client.open()
        restarting.add_player(ADMIN_ID, "Courgette")
        for _ in range(6):
            client.read_lines()
            await asyncio.sleep(0.02)

        restarting.stop()
        for _ in range(4):
            client.read_lines()
        check(client.connected is False, "a server that goes away is noticed")

        restarting.restart()
        client._retry_at = 0.0  # the back-off is real; a test should not wait it out
        lines: list[str] = []
        for _ in range(10):
            lines.extend(client.read_lines())
            await asyncio.sleep(0.02)

        check(client.connected and client.authed, "and it reconnects and logs in again")
        check(
            any("RECONNECTED" in line for line in lines),
            "telling the parser, so a roster nobody can vouch for is dropped",
        )
        check(
            restarting.sent_command("RETRIEVE PLAYERLIST"),
            "and asking who is actually playing rather than waiting for the interval",
        )
        client.close()
    finally:
        restarting.stop()

    # And with the keepalive doing its job, the same server keeps the connection.
    patient = FakeHomefrontServer(password="test", idle_timeout=0.4).start()
    try:
        talkative = HomefrontClient(
            *patient.address,
            "test",
            timeout=0.05,
            playerlist_interval=0,
            # Under the fake's patience, which is what the real client's default 4s is to the real
            # server's 10s. The interval is a parameter precisely so this can be scaled down.
            ping_interval=0.1,
        )
        talkative.open()
        for _ in range(30):
            talkative.read_lines()
            await asyncio.sleep(0.03)
        check(
            not patient.dropped_for_silence and patient.pings > 0,
            f"but the keepalive keeps it alive ({patient.pings} pings sent)",
        )
        talkative.close()
    finally:
        patient.stop()

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
