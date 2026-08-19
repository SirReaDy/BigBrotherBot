"""The `status` plugin — publishing the roster and the server state for something else to read.

The classic plugin has almost no behavioural tests (its own suite covers config parsing only), so
these are written from what it produced and from what a status page needs. Three things are worth
pinning: the XML keeps the classic's element and attribute names, because pages written against it
still exist; the shutdown write says the server is empty, without which a page shows the last players
forever; and a destination that cannot be written complains once rather than once a minute.

One deliberate difference is tested too: a masked admin is masked here. This file is usually a public
web page, and `!mask` exists to hide rank from players.
"""

from __future__ import annotations

import json
from xml.etree import ElementTree

import pytest

from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.plugins.status import StatusPlugin


def _status(console, tmp_path, **settings):  # noqa: ANN001, ANN202
    settings.setdefault("output_file", str(tmp_path / "status.json"))
    plugin = StatusPlugin(console, {"settings": settings})
    plugin.start()
    return plugin


def _players(console, *names, team="red", bits=0):  # noqa: ANN001, ANN202
    made = []
    for index, name in enumerate(names, start=1):
        client = Client(
            guid=name[0].upper() * 4,
            name=name,
            cid=str(index),
            id=index,
            group_bits=bits,
            connections=3,
        )
        client.team = team
        console.clients.add(client)
        made.append(client)
    return made


# -- what is in a snapshot -----------------------------------------------------------------------


def test_the_snapshot_holds_the_server_and_the_roster(console, tmp_path):
    plugin = _status(console, tmp_path)
    console.game.map_name = "mp_vacant"
    console.game.gametype = "dm"
    console.game.hostname = "Test Server"
    _players(console, "Bob", "Ann")

    snapshot = plugin.snapshot()

    assert snapshot.server["name"] == "Test Server"
    assert snapshot.server["map"] == "mp_vacant"
    assert snapshot.server["players"] == 2
    assert [c["name"] for c in snapshot.clients] == ["Bob", "Ann"]


def test_scores_and_pings_come_from_the_servers_own_table(console, tmp_path):
    """They exist nowhere else: a log line does not carry a score."""
    plugin = _status(console, tmp_path)
    _players(console, "Bob")
    console.players = [PlayerInfo(cid="1", name="Bob", score=42, ping=57)]

    snapshot = plugin.snapshot(plugin._scores())

    assert (snapshot.clients[0]["score"], snapshot.clients[0]["ping"]) == (42, 57)


def test_a_server_that_will_not_answer_still_gets_a_roster(console, tmp_path):
    """A page showing who is playing with no scores is worth far more than no page."""
    plugin = _status(console, tmp_path)
    _players(console, "Bob")

    def explode() -> list[PlayerInfo]:
        raise OSError("no rcon here")

    console.get_players = explode

    snapshot = plugin.snapshot(plugin._scores())

    assert [c["name"] for c in snapshot.clients] == ["Bob"]
    assert snapshot.clients[0]["score"] == 0


def test_a_masked_admin_is_masked_in_the_file_too(console, tmp_path):
    """`!mask` hides rank from players, and this file is usually a public web page."""
    plugin = _status(console, tmp_path)
    (admin,) = _players(console, "Su", bits=128)
    admin.mask_level = 0
    assert plugin.snapshot().clients[0]["level"] == 100

    admin.mask_level = 2
    assert plugin.snapshot().clients[0]["level"] == 2


def test_addresses_are_left_out_unless_asked_for(console, tmp_path):
    """The classic published every player's IP to whatever could read the file."""
    plugin = _status(console, tmp_path)
    (bob,) = _players(console, "Bob")
    bob.ip = "11.22.33.44"

    assert plugin.snapshot().clients[0]["ip"] == ""

    plugin.settings["include_ip"] = True
    assert plugin.snapshot().clients[0]["ip"] == "11.22.33.44"


# -- rendering -----------------------------------------------------------------------------------


def test_json_is_the_default_and_parses(console, tmp_path):
    plugin = _status(console, tmp_path)
    console.game.map_name = "mp_vacant"
    _players(console, "Bob")

    parsed = json.loads(plugin.render(plugin.snapshot()))

    assert parsed["server"]["map"] == "mp_vacant"
    assert parsed["clients"][0]["name"] == "Bob"
    assert parsed["time"]


def test_the_xml_keeps_the_classic_bots_names(console, tmp_path):
    """`B3Status/Game` and `B3Status/Clients/Client`, with CamelCase attributes — because status pages
    written for the classic bot parse exactly those."""
    plugin = _status(console, tmp_path, format="xml")
    console.game.map_name = "mp_vacant"
    _players(console, "Bob")

    root = ElementTree.fromstring(plugin.render(plugin.snapshot()))

    assert root.tag == "B3Status"
    assert root.attrib["Time"]
    assert root.find("Game").attrib["Map"] == "mp_vacant"
    assert root.find("Game").attrib["OnlinePlayers"] == "1"
    client = root.find("Clients/Client")
    assert client.attrib["Name"] == "Bob"
    assert client.attrib["CID"] == "1"
    assert root.find("Clients").attrib["Total"] == "1"


def test_a_format_nobody_recognises_falls_back_to_json(console, tmp_path, caplog):
    with caplog.at_level("ERROR"):
        plugin = _status(console, tmp_path, format="yaml")

    assert plugin.settings["format"] == "json"
    assert "neither json nor xml" in caplog.text


# -- writing -------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publishing_writes_the_file(console, tmp_path):
    target = tmp_path / "sub" / "status.json"
    plugin = _status(console, tmp_path, output_file=str(target))
    _players(console, "Bob")

    await plugin.publish()

    assert json.loads(target.read_text(encoding="utf-8"))["clients"][0]["name"] == "Bob"


def test_a_destination_that_cannot_be_written_complains_once(console, tmp_path, caplog):
    """1,440 identical lines a day would bury everything else in the log."""
    plugin = _status(console, tmp_path, output_file=str(tmp_path / "status.json"))
    plugin.destination.path = tmp_path  # a directory: writing to it fails

    with caplog.at_level("ERROR"):
        plugin._write("{}")
        plugin._write("{}")

    assert caplog.text.count("cannot write to") == 1
    assert plugin.destination.failing is True


def test_and_says_when_it_works_again(console, tmp_path, caplog):
    plugin = _status(console, tmp_path, output_file=str(tmp_path / "status.json"))
    plugin.destination.path = tmp_path
    plugin._write("{}")

    plugin.destination.path = tmp_path / "status.json"
    with caplog.at_level("INFO"):
        plugin._write("{}")

    assert "writable again" in caplog.text
    assert plugin.destination.failing is False


def test_the_last_write_says_the_server_is_empty(console, tmp_path):
    """Without it a page shows the players who were here when the bot stopped, and the server looks
    busy while it is off."""
    target = tmp_path / "status.json"
    plugin = _status(console, tmp_path, output_file=str(target))
    _players(console, "Bob", "Ann")

    plugin.on_stop(Event(EventType.STOP, data="shutdown"))

    parsed = json.loads(target.read_text(encoding="utf-8"))
    assert parsed["server"]["players"] == 0
    assert parsed["server"]["running"] is False
    assert parsed["clients"] == []


def test_an_ftp_destination_is_recognised_and_never_logged_with_its_password(console, tmp_path):
    plugin = _status(console, tmp_path, output_file="ftp://bob:hunter2@example.com/www/status.json")

    described = plugin.destination.describe()

    assert plugin.destination.url  # it is an upload, not a path
    assert described == "ftp://example.com/www/status.json"
    assert "hunter2" not in described


# -- the database tables -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tables_are_written_when_asked_for(tmp_path):
    """Plugin-owned tables, created once — where the classic dropped and recreated them at every
    startup and truncated them on every update, in raw SQL with the names interpolated from config."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
    from b3.core.clock import FakeClock
    from b3.plugins.status import ClientStatus, ServerStatus
    from b3.runtime.bot import Bot

    class Rcon:
        def command(self, cmd: str) -> str:
            return ""

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="cod4"),
        plugins=[PluginEntry(name="status")],
    )
    bot = Bot(config, rcon=Rcon(), clock=FakeClock())
    plugin = StatusPlugin(
        bot,
        {
            "settings": {
                "output_file": str(tmp_path / "status.json"),
                "save_to_database": True,
            }
        },
    )
    bot.add_plugin(plugin, "status")
    bot.start()
    plugin.start()

    await bot.replay([f"J;{'b' * 32};2;Bob"])
    await bot.bus.drain()
    await plugin.publish()

    with Session(plugin._engine) as session:
        server = {row.name: row.value for row in session.scalars(select(ServerStatus)).all()}
        clients = session.scalars(select(ClientStatus)).all()
    assert server["players"] == "1"
    assert [c.name for c in clients] == ["Bob"]

    # A second update replaces the rows rather than adding to them.
    await plugin.publish()
    with Session(plugin._engine) as session:
        assert len(session.scalars(select(ClientStatus)).all()) == 1
    bot.storage.close()


def test_without_an_engine_the_tables_are_skipped_and_said_so(console, tmp_path, caplog):
    with caplog.at_level("WARNING"):
        plugin = _status(console, tmp_path, save_to_database=True)

    assert plugin._engine is None
    assert "nothing will be written to the database" in caplog.text
