"""Drive a real Bot against the fake Ravaged server, end to end.

    python -m tools.fakeservers.e2e_ravaged

Plays out a plausible round: two players connect, one claims superadmin, a team kill happens, the
admin bans the culprit for a while and then lifts it, and the map is changed. Checks the outcome on
the wire, in the database and in the server's own ban list. Exits non-zero if anything fails.

The parts worth a running server for, none of which a unit test would catch:

* the **two directions having different shapes** — bare lines out, `(<size>)payload` back — through a
  real socket, with the fake writing in small slices so the reassembly is real rather than lucky;
* **replies and events sharing one stream**, so a `!kick` and a kill can be in flight together and
  neither is mistaken for the other;
* the **bare verb echo**, because a bot that expects its whole command back never sees the answer to
  anything with an argument — which is most of them;
* the **ban counted in days**, the only engine here that does, and its `%(days)s` conversion;
* the **two-command map change**, since `addmap` alone would look like it worked and change nothing;
* and the **blacklist**, which is what a wrong password turns into if a bot retries it.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import Event, EventType
from b3.domain.client import PenaltyType
from b3.net.ravaged import RavagedAuthError, RavagedBlacklistedError, RavagedClient
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.ravaged import FakeRavagedServer

COURGETTE = "12312312312312312"
BADGER = "70000000000000005"

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def main() -> int:
    # `fragment=5` makes the server write every frame in five-byte slices: a length-prefixed stream
    # that is only ever delivered whole is not being tested.
    server = FakeRavagedServer(password="test", fragment=5).start()
    tmp = tempfile.mkdtemp(prefix="b3-ravaged-e2e-")
    try:
        # The roster refresh is off for the session and exercised on its own below, so it cannot race
        # the assertions about what the bot sent.
        client = RavagedClient(*server.address, "test", timeout=1.0, playerlist_interval=0)
        config = Config(
            bot=BotConfig(database=f"sqlite:///{Path(tmp) / 'b3.sqlite'}", server_id="rav_1"),
            server=ServerConfig(game="ravaged"),
            plugins=[PluginEntry(name="admin")],
        )
        bot = Bot(config, rcon=client)
        bot.add_plugin(AdminPlugin(bot), "admin")

        client.open()
        bot.start()
        print(f"connected to the fake Ravaged server on {server.address[0]}:{server.address[1]}")
        check(server.authed, "the two-step login was accepted (only the SHA1 crossed the wire)")
        check(
            server.sent_command("testrcon"),
            "and commands were proved to work rather than assumed from the password",
        )

        events: list[Event] = []
        for event_type in EventType:
            bot.bus.subscribe(event_type, lambda e: events.append(e))

        async def pump(rounds: int = 6) -> None:
            for _ in range(rounds):
                for line in client.read_lines():
                    await bot.feed_line(line)
                await asyncio.sleep(0.02)

        # -- the round -----------------------------------------------------
        server.load_map("CTR_Canyon")
        server.add_player(COURGETTE, "courgette", team="1")
        server.add_player(BADGER, "Killer Badger", team="1")
        await pump()

        server.say_as(COURGETTE, "courgette", "!iamgod")
        await pump()

        # A team kill: same team, and the weapon unquoted, which is one of the two forms the real
        # server uses.
        server.push(
            f'"Killer Badger<{BADGER}><1>" killed "courgette<{COURGETTE}><1>" '
            "with R_DmgType_SniperPrimary"
        )
        await pump()

        server.say_as(COURGETTE, "courgette", "!tempban Killer 2h teamkilling")
        await pump()
        await pump()  # the kickban and the disconnect it causes come back

        seen = [e.type for e in events]
        print("\nwhat happened:")
        check(seen.count(EventType.CLIENT_JOIN) == 2, "both players joined on their Steam id")
        check(bot.game.map_name == "CTR_Canyon", "the map was recorded from the log line")
        check(
            bot.game.cvars.get("g_gametype") == "CTR",
            "and the gametype was read out of the map name, there being nowhere else to get it",
        )
        check(EventType.CLIENT_KILL_TEAM in seen, "the team kill was classified as one")
        check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin")

        badger = bot.storage.get_client_by_guid(BADGER)
        check(badger is not None, "the culprit is in the database, keyed on his Steam id")
        if badger is not None:
            check(
                len(bot.storage.get_active_penalties(badger.require_id(), PenaltyType.TEMPBAN)) == 1,
                "the tempban was recorded",
            )

        print("\ncommands the bot sent:")
        for command in server.received:
            print(f"  {command if not command.startswith('PASS') else 'PASS=<sha1>'}")

        kickbans = [c for c in server.received if c.startswith("kickban")]
        check(bool(kickbans), "the ban went out as a kickban")
        check(
            all(BADGER in c for c in kickbans),
            "by Steam id, so it holds across a name change",
        )
        check(
            any(c.rstrip().endswith("0.0833") for c in kickbans),
            f"and in **days**, the only unit this engine's verb takes ({kickbans!r})",
        )
        check(BADGER in server.bans, "it is on the server's own ban list")
        check(bot.clients.get_by_cid(BADGER) is None, "and he left the roster")
        check(
            any(c.startswith("playersay") or c.startswith("say") for c in server.received),
            "the bot answered in game, in the colour this engine wants",
        )

        # -- the roster, which is a reply the parser reads ------------------
        server.received.clear()
        client.playerlist_interval = 0.05
        await asyncio.sleep(0.1)
        await pump()
        players = bot.get_players()
        check(
            [p.cid for p in players] == [COURGETTE],
            f"the roster came back from getplayerlist ({[p.name for p in players]})",
        )
        check(
            all(p.ping > 0 for p in players),
            "with pings, which appear in no log line and can only be asked for",
        )
        client.playerlist_interval = 0

        # -- the map list and the map change -------------------------------
        maps = bot.get_maps()
        check(
            maps[:2] == ["CTR_Canyon", "CTR_Derelict"],
            f"the rotation reads in order, current map first ({maps[:3]})",
        )
        check(
            bot.get_next_map() == "CTR_Derelict",
            "so the next map is the one after it",
        )

        server.received.clear()
        bot.change_map("Thrust_Oilrig")
        await pump(2)
        check(
            server.sent_command("addmap Thrust_Oilrig") and server.sent_command("nextmap"),
            "changing map takes **both** commands: addmap alone would change nothing until the "
            "round ended on its own",
        )
        check(
            server.maps[0] == "Thrust_Oilrig",
            f"and the server really is on the new map ({server.maps[0]})",
        )

        # -- lifting the ban ------------------------------------------------
        if badger is not None:
            bot.unban(badger, "appeal granted")
        await pump()
        check(BADGER not in server.bans, "!unban cleared the server's own ban list entry")

        bot.storage.close()
        client.close()
    finally:
        server.stop()

    # -- and the thing this protocol will not forgive -----------------------
    print("\nthe blacklist:")
    strict = FakeRavagedServer(password="test", max_attempts=3).start()
    try:
        wrong = RavagedClient(*strict.address, "nope", timeout=0.3)
        try:
            wrong.open()
            check(False, "a wrong password is refused")
        except RavagedBlacklistedError:
            check(False, "one attempt should not be enough to be blacklisted")
        except RavagedAuthError:
            check(True, "a wrong password is refused")
        check(
            len([c for c in strict.received if c.startswith("PASS=")]) == 1,
            "and tried exactly once -- a retry here is a step towards a lockout, not a second chance",
        )
        check(not strict.blacklisted, "so the address is still allowed to connect")
    finally:
        strict.stop()

    print("\nand once it has happened:")
    banned = FakeRavagedServer(password="test").start()
    try:
        banned.blacklisted = True
        client = RavagedClient(*banned.address, "test", timeout=0.3)
        try:
            client.open()
            check(False, "a blacklisted address is told so")
        except RavagedBlacklistedError:
            check(True, "a blacklisted address is told so, and not as a mere password problem")
        except RavagedAuthError:
            check(False, "a blacklist must be distinguishable from a refusal")
    finally:
        banned.stop()

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
