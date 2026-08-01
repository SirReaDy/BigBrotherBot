"""Drive a real Bot against the fake Frontlines: Fuel of War server, end to end.

    python -m tools.fakeservers.e2e_frontline

Plays out a plausible session: two players appear in the roster, one claims superadmin, an admin bans
the other for a while, the ban is lifted, and the map is changed. Checks the outcome on the wire, in the
database and in the server's own ban list. Exits non-zero if anything fails.

The parts worth a running server for, none of which a unit test would catch:

* **the roster reply as the event stream** — this engine writes no connect and no disconnect line, so a
  join really does have to come out of a diff, and a bot that treated `PLAYERLIST` as a status poll
  would never see anybody arrive;
* **the whole packet surviving a fragmented socket**, because that reply is one packet with newlines and
  tabs inside it, and it is the only thing carrying identity;
* **the MD5 challenge/response**, which the fake verifies rather than assumes, including the username —
  and a refusal that arrives as a closed connection with no message in it;
* **the reporting switches**, because until `CHATLOGGING TRUE` goes out nobody in the game can be heard
  at all, and a server that has been restarted has forgotten them again;
* **the keepalive**, since ten seconds of silence costs the connection.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import Event, EventType
from b3.domain.client import PenaltyType
from b3.net.frontline import FrontlineAuthError, FrontlineClient
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.frontline import FakeFrontlineServer

COURGETTE = "1561500"
BADGER = "1561501"

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def settled(
    predicate: "Callable[[], bool]", *, rounds: int = 60, delay: float = 0.05
) -> bool:
    """Wait for something the server's own thread has to do before asking whether it did it.

    Writing a command and the fake reading it are separate events, so asking straight after a write
    races another thread rather than testing anything.

    Returns the predicate's final value, so a caller still `check`s it and still fails if the wait
    ran out.
    """
    for _ in range(rounds):
        if predicate():
            return True
        await asyncio.sleep(delay)
    return predicate()


async def main() -> int:
    # `fragment=5` writes every packet in five-byte slices: the roster is the longest thing this
    # protocol sends and the only one that matters, so it is the one to break up.
    server = FakeFrontlineServer(password="test", fragment=5).start()
    tmp = tempfile.mkdtemp(prefix="b3-frontline-e2e-")
    try:
        # The refresh is driven by hand below so the roster diff can be watched a step at a time.
        client = FrontlineClient(
            *server.address, "test", timeout=0.5, ping_interval=0.0, playerlist_interval=0.0
        )
        config = Config(
            bot=BotConfig(database=f"sqlite:///{Path(tmp) / 'b3.sqlite'}", server_id="fl_1"),
            server=ServerConfig(game="frontline", rcon_user="admin"),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=client)
        bot.add_plugin(AdminPlugin(bot), "admin")

        client.open()
        bot.start()
        print(f"connected to the fake Frontline server on {server.address[0]}:{server.address[1]}")
        check(server.authed, "the MD5 challenge was answered (the password never crossed the wire)")
        check(client.server_version == "2", "and the server's RCON version was read")
        check(
            await settled(lambda: server.chat_logging and server.debug_logging),
            "the reporting switches were turned on -- without them nobody can be heard at all",
        )

        events: list[Event] = []
        for event_type in EventType:
            bot.bus.subscribe(event_type, lambda e: events.append(e))

        async def pump(rounds: int = 8) -> None:
            for _ in range(rounds):
                for line in client.read_lines():
                    await bot.feed_line(line)
                await asyncio.sleep(0.02)

        async def refresh() -> None:
            """Ask for the roster and feed the reply in. On a live bot the client does this on a
            three-second timer; here it is explicit so the diff can be watched."""
            client.write("PLAYERLIST")
            await pump()

        # -- the session ---------------------------------------------------
        server.add_player("1", "Courgette", COURGETTE, team="0", pb_hash="a" * 32)
        server.add_player("2", "Killer Badger", BADGER, team="0")
        await refresh()

        seen = [e.type for e in events]
        print("\nwhat happened:")
        check(
            seen.count(EventType.CLIENT_JOIN) == 2,
            f"both players joined -- out of a roster *diff*, there being no connect line ({seen})",
        )
        check(bot.game.map_name == "CQ-Gnaw", "the map came from the roster header")
        check(bot.game.max_players == 32, "and the slot count with it")

        events.clear()
        await refresh()
        check(
            not [e for e in events if e.type is EventType.CLIENT_JOIN],
            "a second identical roster reports nothing, so joins are not re-announced every 3s",
        )

        server.say_as("Courgette", "!iamgod")
        await pump()
        check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin, so chat works")

        # PunkBuster is the only source of an IP on this engine.
        courgette = bot.clients.get_by_cid("1")
        check(
            courgette is not None and courgette.ip == "192.168.0.1",
            "the IP came from PunkBuster's line, the only place this engine reports one",
        )

        server.say_as("Courgette", "!tempban Killer 2h teamkilling")
        await pump()
        await refresh()  # the ban's kick shows up as the roster losing a row

        badger = bot.storage.get_client_by_guid(BADGER)
        check(badger is not None, "the culprit is in the database, keyed on his ProfileID")
        if badger is not None:
            check(
                len(bot.storage.get_active_penalties(badger.require_id(), PenaltyType.TEMPBAN))
                == 1,
                "the tempban was recorded",
            )

        print("\ncommands the bot sent:")
        for command in server.received:
            print(f"  {command if not command.startswith('RESPONSE') else 'RESPONSE admin <md5>'}")

        bans = [c for c in server.received if c.upper().startswith("BAN ")]
        check(bool(bans), "the ban went out")
        check(
            all(f"ProfileID={BADGER}" in c for c in bans),
            "keyed on the ProfileID, because slot numbers are reused by the next player",
        )
        check(
            any("BanTime=120" in c for c in bans),
            f"with its duration in minutes, which is the unit this verb takes ({bans!r})",
        )
        check(BADGER in server.bans, "it is on the server's own ban list")
        check(bot.clients.get_by_cid("2") is None, "and he left the roster")
        check(
            any(c.upper().startswith("PLAYERSAY ") for c in server.received),
            "the bot answered privately, which this engine addresses by slot",
        )

        # The server announces its own bans, including ones the bot did not make.
        server_bans = [
            e for e in events if e.type is EventType.CUSTOM and e.extra.get("kind") == "server_ban"
        ]
        check(
            bool(server_bans),
            "the server's own ban confirmation was reported, so a console admin's ban is visible too",
        )

        # -- the rotation --------------------------------------------------
        client.write("MapList")
        client.write("GetNextMap")
        await pump()
        maps = bot.get_maps()
        check(
            maps == ["CQ-Gnaw", "FL-Oilfield", "CQ-Bridge", "FL-Harbor"],
            f"the rotation was read whole, `CQ-` maps included ({maps})",
        )
        check(
            bot.get_next_map() == "FL-Oilfield",
            "and the next map is what the server says it is, not what arithmetic guesses",
        )

        server.received.clear()
        events.clear()
        bot.change_map("FL-Harbor")
        await pump()
        await refresh()
        check(server.sent_command("ForceMapChange FL-Harbor"), "the map change went out")
        check(
            bot.game.map_name == "FL-Harbor", "and the new map was picked up from the next roster"
        )
        check(
            not bot.clients.connected(),
            "everybody was dropped through the loading screen, and the roster says so",
        )

        # -- lifting the ban -----------------------------------------------
        if badger is not None:
            bot.unban(badger, "appeal granted")
        await pump()
        check(BADGER not in server.bans, "!unban cleared the server's own ban list entry")

        events.clear()
        client.write(f"UNBAN ProfileID={BADGER}")  # already lifted: the server refuses
        await pump()
        refused = [
            e
            for e in events
            if e.type is EventType.CUSTOM and e.extra.get("kind") == "server_unban_failed"
        ]
        check(
            bool(refused),
            "an unban the server refuses is reported, rather than looking exactly like one that worked",
        )

        bot.storage.close()
        client.close()
    finally:
        server.stop()

    # -- the refusal that carries no message --------------------------------
    print("\nthe silent refusal:")
    strict = FakeFrontlineServer(password="test").start()
    try:
        wrong = FrontlineClient(*strict.address, "nope", timeout=0.3)
        try:
            wrong.open()
            check(False, "a wrong password is refused")
        except FrontlineAuthError:
            check(True, "a wrong password is refused -- and as an auth failure, not a network one")
        check(
            strict.rejected, "the server hung up rather than answering, which is all it ever does"
        )
    finally:
        strict.stop()

    print("\nand a wrong *user*, which looks identical from the outside:")
    named = FakeFrontlineServer(password="test", user="operator").start()
    try:
        wrong_user = FrontlineClient(*named.address, "test", user="admin", timeout=0.3)
        try:
            wrong_user.open()
            check(False, "a wrong user is refused")
        except FrontlineAuthError:
            check(True, "a wrong user is refused the same way, which is why doctor names both")
    finally:
        named.stop()

    # -- the ten-second hangup ----------------------------------------------
    print("\nthe hangup, and the keepalive that prevents it:")
    quiet_server = FakeFrontlineServer(password="test", idle_timeout=0.4).start()
    try:
        quiet = FrontlineClient(
            *quiet_server.address, "test", timeout=0.1, ping_interval=99, playerlist_interval=0
        )
        quiet.open()
        await asyncio.sleep(0.8)  # say nothing at all
        for _ in range(6):
            quiet.read_lines()
        check(quiet_server.dropped_for_silence, "a client that stops talking really is hung up on")
        quiet.close()
    finally:
        quiet_server.stop()

    patient = FakeFrontlineServer(password="test", idle_timeout=0.4).start()
    try:
        talkative = FrontlineClient(
            *patient.address, "test", timeout=0.05, ping_interval=0.1, playerlist_interval=0
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

    # -- and coming back, with the switches thrown again --------------------
    print("\nsurviving a restart:")
    restarting = FakeFrontlineServer(password="test").start()
    try:
        client = FrontlineClient(
            *restarting.address, "test", timeout=0.1, ping_interval=0, playerlist_interval=0.2
        )
        client.open()
        restarting.add_player("1", "Courgette", COURGETTE)
        for _ in range(6):
            client.read_lines()
            await asyncio.sleep(0.02)

        restarting.restart()
        lines: list[str] = []
        # Waits for the *last* of the three things a reconnect does, not the first. Logging in and
        # having the switches read are separate events -- the client writes them, and the server's
        # own thread reads them a moment later -- so stopping at `authed` and asking about the
        # switches straight away is a race. It happens to be won on Windows and lost on Linux, which
        # is exactly the kind of difference a driver should not be sensitive to.
        for _ in range(80):
            lines.extend(client.read_lines())
            if (
                client.authed
                and any("RECONNECTED" in line for line in lines)
                and restarting.chat_logging
            ):
                break
            client._retry_at = 0.0  # the back-off is real; a demo should not wait it out
            await asyncio.sleep(0.05)

        check(client.connected and client.authed, "a restarted server is reconnected to")
        check(
            any("RECONNECTED" in line for line in lines),
            "and the parser is told, so a roster nobody can vouch for is dropped",
        )
        check(
            restarting.chat_logging,
            "the reporting switches were thrown again -- a restarted server has forgotten them, and "
            "a bot that sent them only once would be deaf for the rest of its life",
        )
        client.close()
    finally:
        restarting.stop()

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
