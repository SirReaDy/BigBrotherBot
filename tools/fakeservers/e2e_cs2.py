"""Drive a real Bot against a fake Counter-Strike 2 server, end to end.

    python -m tools.fakeservers.e2e_cs2

CS2 shares Insurgency's grammar and both its transports, so this driver is not here to re-prove
those. It exists for the two things that are specific to this title, both of which a unit test can
assert *around* without ever exercising:

* **The identity fold.** The log writes ``[U:1:N]`` and ``status`` answers with a 17-digit Steam64.
  A unit test can check the arithmetic; only a running bot can show that a player built from a log
  line survives a `sync` that names them differently. If it does not, the sync drops the record and
  adopts a stranger -- every five minutes, for every player, with their level and bans going with it.
  That is silent: the roster stays the right size.

* **A "ban" that is only a kick.** This title has no dependable ban verb, so the record in this
  bot's database *is* the enforcement, re-applied when the player comes back. Whether that actually
  happens is a question about the runtime, not about a template, and getting it wrong means a banned
  player walks straight back in.

The fake is started with ``sourcemod=False``, because a CS2 server has no SourceMod and a driver run
against one that does would prove nothing about this title.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.events import Event, EventType
from b3.core.steamid import canonical, to_steam64
from b3.domain.client import PenaltyType
from b3.net.logsource import create_log_source
from b3.net.source import SourceRconClient
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot
from tools.fakeservers.source import FakeSourceServer

#: One account, and the three spellings of it this run has to reconcile.
COURGETTE_ACCOUNT = 2222222
COURGETTE_MODERN = f"[U:1:{COURGETTE_ACCOUNT}]"
COURGETTE_LEGACY = canonical(COURGETTE_MODERN)
COURGETTE_STEAM64 = to_steam64(COURGETTE_ACCOUNT)

BADGER_ACCOUNT = 3333333
BADGER_MODERN = f"[U:1:{BADGER_ACCOUNT}]"
BADGER_LEGACY = canonical(BADGER_MODERN)
BADGER_STEAM64 = to_steam64(BADGER_ACCOUNT)

failures: list[str] = []


def check(condition: bool, description: str) -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {description}")
    if not condition:
        failures.append(description)


async def main() -> int:  # noqa: PLR0915 - a linear script reads better than helpers here
    tmp = Path(tempfile.mkdtemp(prefix="b3-cs2-e2e-"))
    log_path = tmp / "console.log"
    log_path.write_text("", encoding="utf-8")

    server = FakeSourceServer(
        password="test",
        log_path=log_path,
        fragment=48,
        sourcemod=False,  # what a real CS2 server is: Source 2 has no SourceMod build
        hostname="Fake CS2 Server",
        map_name="de_dust2",
        maps=("de_dust2", "de_inferno", "de_mirage", "de_nuke", "cs_office"),
    ).start()
    try:
        client = SourceRconClient(*server.address, "test", timeout=3.0)
        config = Config(
            bot=BotConfig(database=f"sqlite:///{tmp / 'b3.sqlite'}", server_id="cs2_1"),
            server=ServerConfig(
                game="cs2",
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
        print(f"connected to the fake CS2 server on {server.address[0]}:{server.address[1]}")
        check(server.authed, "the RCON login handshake was accepted")
        check(
            not server.sent_command("sm version"),
            "SourceMod was not asked for, this title having none to find",
        )

        events: list[Event] = []
        for event_type in EventType:
            bot.bus.subscribe(event_type, lambda e: events.append(e))

        async def pump(rounds: int = 6) -> None:
            for _ in range(rounds):
                for line in source.read_lines():
                    await bot.feed_line(line)
                await asyncio.sleep(0.02)

        # -- players arrive, written the way the log writes them ------------
        server.load_map("de_dust2")
        server.add_player(
            "194", "courgette", COURGETTE_MODERN, team="CT", ip="11.222.111.222", ping=67
        )
        server.add_player(
            "195", "Killer Badger", BADGER_MODERN, team="CT", ip="11.222.111.223", ping=42
        )
        server.add_player("224", "Moe", team="TERRORIST")  # a bot: no ip, no ping
        await pump()

        print("\nthe identity fold:")
        courgette = bot.clients.get_by_cid("194")
        check(courgette is not None, "the player from the log is on the roster")
        check(
            courgette is not None and courgette.guid == COURGETTE_LEGACY,
            f"and is stored as {COURGETTE_LEGACY}, not as the {COURGETTE_MODERN} the log wrote",
        )

        # Now the server starts answering `status` with the *other* spelling, which is what CS2 does.
        with server._lock:  # noqa: SLF001 - the fake's state is the point of the fake
            for slot, steam64 in (("194", COURGETTE_STEAM64), ("195", BADGER_STEAM64)):
                name, _guid, team, ip, ping = server.players[slot]
                server.players[slot] = (name, steam64, team, ip, ping)

        before = bot.clients.get_by_cid("194")
        bot.sync()
        after = bot.clients.get_by_cid("194")
        check(
            after is not None and before is not None and after is before,
            "a sync naming them by Steam64 keeps the same client record, rather than replacing it",
        )
        check(
            after is not None and after.guid == COURGETTE_LEGACY,
            "and the guid is still the canonical one after the sync",
        )
        check(
            len([c for c in bot.clients.connected() if c.name == "courgette"]) == 1,
            "so the player exists once, not once per spelling",
        )
        moe = bot.clients.get_by_cid("224")
        check(
            moe is not None, "the bot player survived the sync too, its row shape being different"
        )
        check(moe is not None and moe.guid == "", "and still has no identity to share")

        # -- moderation, on a title with no ban verb ------------------------
        server.say_as("194", "!iamgod")
        await pump()
        check(bot.storage.has_superadmin(), "!iamgod bootstrapped a superadmin")

        server.say_as("194", "!kick Badger cheating")
        await pump()
        print("\nwhat a kick did:")
        check(
            any(c.startswith("kickid ") for c in server.received),
            "the native kickid verb was used, no sm_kick existing here",
        )
        check("195" not in server.players, "and the player is actually off the server")

        server.add_player(
            "195", "Killer Badger", BADGER_MODERN, team="CT", ip="11.222.111.223", ping=42
        )
        await pump()
        server.say_as("194", "!permban Badger cheating")
        await pump(10)

        print("\nwhat a ban did, on an engine that has no ban:")
        banned = bot.storage.get_client_by_guid(BADGER_LEGACY)
        check(banned is not None, f"the culprit is in the database, keyed on {BADGER_LEGACY}")
        if banned is not None:
            check(
                len(bot.storage.get_active_penalties(banned.require_id(), PenaltyType.BAN)) == 1,
                "the ban is recorded here, which is the whole of the enforcement",
            )
        check("195" not in server.players, "and the player was removed from the server")
        check(
            not server.bans,
            "nothing was written to the server's own ban list, because it has no reliable one",
        )

        # The test that matters for a ban that is only a kick: coming back.
        server.add_player(
            "195", "Killer Badger", BADGER_MODERN, team="CT", ip="11.222.111.223", ping=42
        )
        await pump(10)
        check(
            "195" not in server.players,
            "and on reconnecting they are kicked again, this bot's record being what enforces it",
        )

        # -- a private reply, which this engine cannot do privately ---------
        server.received.clear()
        server.say_as("194", "!help")
        await pump()
        said = [c for c in server.received if c.startswith("say ")]
        print("\nhow a reply gets out:")
        check(bool(said), "a reply was sent at all")
        check(
            any(c.startswith("say [courgette]") for c in said),
            "as a public say naming the player, there being no private verb on this title",
        )
        check(
            not any(c.startswith("sm_psay") for c in server.received),
            "and never as sm_psay, which would fail silently here",
        )

        # -- the map, which does have a native verb ------------------------
        print("\nand the map:")
        server.say_as("194", "!map inferno")  # partial, resolved against `maps *`
        await pump()
        check(server.map_name == "de_inferno", "changelevel loaded the map a partial name matched")

        # The other half of that: a map the server has not got is refused rather than sent. Worth a
        # check here because `changelevel` gets no reply, so sending a bad one fails silently.
        server.say_as("194", "!map de_notinstalled")
        await pump()
        check(
            server.map_name == "de_inferno",
            "and a map this server does not have was refused, not sent and lost",
        )

        bot.storage.close()
        client.close()
        source.close()
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
