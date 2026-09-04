"""`!map` and the arguments some engines take with it — §1.15 and §2.2.

Two engines here want more than a map name, and they were both losing what an admin typed:

* **Insurgency** takes ``changelevel <map> <gamemode>``. We sent ``changelevel market`` and dropped
  the mode, so the admin got the right map in whatever mode the server happened to be set to.
* **Frostbite** took ``admin.runNextRound "<map>"`` — the Frostbite *1* rotate verb, which takes no
  argument at all, on all six titles. So `!map` advanced the round and loaded whatever was next; the
  map name went nowhere and nothing said so.

Both are the failure this codebase keeps finding: not an error, just silence. So the assertions here
are about what reaches the server, not about the call being made.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.domain.client import Client
from b3.core.clock import FakeClock
from b3.net.frostbite import (
    SAY_INTERVAL,
    SAY_INTERVAL_FROSTBITE1,
    FrostbiteClient,
    parse_map_list,
)
from b3.parsers.frostbite.profiles import BF3, BFBC2, LEGACY_MAPLIST, MOH
from b3.parsers.profile import GameProfile
from b3.parsers.source.profiles import CS2, INSURGENCY
from b3.plugins.admin import AdminPlugin
from tools.fakeservers.frostbite import FakeFrostbiteServer

# -- parsing what the admin typed -----------------------------------------------


def test_a_title_with_no_extra_arguments_takes_the_whole_line_as_the_map() -> None:
    """Which it must: plenty of map names contain a space, and most engines take only a name."""
    profile = GameProfile(name="cod4")
    request = profile.parse_map_request("mp crossfire")
    assert request.name == "mp crossfire"
    assert request.extras == {}
    assert request.surplus == ()


def test_insurgency_reads_the_gamemode_after_the_map() -> None:
    request = INSURGENCY.parse_map_request("market push")
    assert request.name == "market"
    assert request.extras == {"gamemode": "push"}


def test_insurgency_still_accepts_a_bare_map_name() -> None:
    request = INSURGENCY.parse_map_request("market")
    assert request.name == "market"
    assert request.extras == {}


def test_frostbite_reads_a_gamemode_and_a_round_count_after_a_comma() -> None:
    request = BF3.parse_map_request("Grand Bazaar, RushLarge0, 3")
    # The comma is what makes a two-word map name possible, which is why this family uses one.
    assert request.name == "Grand Bazaar"
    assert request.extras == {"gamemode": "RushLarge0", "rounds": "3"}


def test_an_omitted_middle_argument_is_left_alone_rather_than_sent_blank() -> None:
    """`!map metro,,2` means "this map, this many rounds, leave the mode as it is"."""
    request = BF3.parse_map_request("metro,,2")
    assert request.extras == {"rounds": "2"}
    assert "gamemode" not in request.extras


def test_typing_more_arguments_than_the_engine_takes_is_reported_not_swallowed() -> None:
    request = INSURGENCY.parse_map_request("market push nonsense")
    assert request.surplus == ("nonsense",)


def test_the_usage_line_uses_the_engine_s_own_separator() -> None:
    # Telling a Source admin to type a comma would be a usage line that does not work.
    assert INSURGENCY.map_usage() == " [gamemode]"
    assert BF3.map_usage() == ", [gamemode], [rounds]"
    assert GameProfile(name="cod4").map_usage() == ""


def test_the_frostbite_1_titles_offer_no_extras_because_their_map_list_holds_none() -> None:
    """Bad Company 2 and Medal of Honor keep a flat list of level names, with nowhere to put a mode."""
    for profile in (BFBC2, MOH):
        assert profile.map_arguments == ()
        assert profile.parse_map_request("Levels/MP_001").name == "Levels/MP_001"


def test_cs2_takes_a_bare_map_name() -> None:
    """Its gamemode is a pair of cvars, not an argument to `changelevel` — unlike Insurgency's."""
    assert CS2.map_arguments == ()


# -- rendering the command ------------------------------------------------------


class RecordingRcon:
    """Just enough of an RCON client to see what would go out."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        return ""

    def close(self) -> None:
        pass


def _bot(game: str, rcon: RecordingRcon):
    from b3.config.schema import Config
    from b3.runtime.bot import Bot

    config = Config.model_validate(
        {
            "bot": {"database": "sqlite://"},
            "server": {"game": game, "host": "127.0.0.1", "port": 27015, "rcon_password": "x"},
        }
    )
    return Bot(config, rcon=rcon)


def test_insurgency_sends_the_gamemode_it_was_given() -> None:
    rcon = RecordingRcon()
    _bot("insurgency", rcon).change_map("market", {"gamemode": "push"})
    assert rcon.sent == ["changelevel market push"]


def test_insurgency_with_no_gamemode_sends_exactly_what_it_used_to() -> None:
    """The trailing placeholder must not leave a dangling argument behind it."""
    rcon = RecordingRcon()
    _bot("insurgency", rcon).change_map("market")
    assert rcon.sent == ["changelevel market"]


def test_a_title_with_no_extras_is_rendered_positionally_exactly_as_before() -> None:
    rcon = RecordingRcon()
    _bot("cod4", rcon).change_map("mp_crossfire")
    assert rcon.sent == ["map mp_crossfire"]


# -- through the command, which is where an admin meets it ----------------------


def _setup(console):
    plugin = AdminPlugin(console)
    plugin.register_commands()
    # No wait between `!map`'s announcement and the change: this file is about what reaches the
    # server, and the pause has its own tests in `test_maps.py`.
    plugin.settings["map_announce_pause"] = 0
    return plugin, CommandProcessor(console.command_registry, console)


def _admin() -> Client:
    return Client(guid="A", name="Admin", group_bits=128, cid="0", id=1)


@pytest.mark.asyncio
async def test_the_gamemode_an_admin_types_reaches_the_server(console) -> None:
    console.map_profile = INSURGENCY
    console.maps = ["market", "district"]
    _, proc = _setup(console)

    await proc.handle(_admin(), "!map market push")

    assert console.map_changes == ["market"]
    assert console.map_extras == [{"gamemode": "push"}]


@pytest.mark.asyncio
async def test_a_frostbite_map_with_a_space_in_it_survives_the_comma_form(console) -> None:
    console.map_profile = BF3
    console.maps = ["MP_Subway"]
    console.map_names = dict(BF3.map_names)
    _, proc = _setup(console)

    await proc.handle(_admin(), "!map operation metro, RushLarge0, 3")

    assert console.map_changes == ["MP_Subway"]
    assert console.map_extras == [{"gamemode": "RushLarge0", "rounds": "3"}]


@pytest.mark.asyncio
async def test_too_many_arguments_gets_the_usage_line_rather_than_a_wrong_map(console) -> None:
    console.map_profile = INSURGENCY
    console.maps = ["market"]
    _, proc = _setup(console)

    await proc.handle(_admin(), "!map market push and then some")

    assert console.map_changes == []
    assert any("map <name> [gamemode]" in text for _, text in console.told)


@pytest.mark.asyncio
async def test_an_engine_with_no_extras_is_unaffected(console) -> None:
    """A map name with a space in it still works everywhere else, which is the thing not to break."""
    console.maps = ["mp_crossfire"]
    _, proc = _setup(console)

    await proc.handle(_admin(), "!map mp_crossfire")

    assert console.map_changes == ["mp_crossfire"]
    assert console.map_extras == [{}]


# -- the Frostbite map list -----------------------------------------------------


def test_a_frostbite_2_map_list_states_its_own_stride() -> None:
    """DICE documented the words-per-map field as future-proofing, so it is read, not assumed."""
    words = ["2", "3", "MP_001", "ConquestLarge0", "2", "MP_011", "RushLarge0", "3"]
    assert parse_map_list(words) == ["MP_001", "MP_011"]


def test_a_map_list_with_four_words_per_entry_still_reads_the_names() -> None:
    """The case the stride field exists for: a patch adds a word and nothing here should notice."""
    words = ["2", "4", "MP_001", "Conquest", "2", "extra", "MP_011", "Rush", "3", "extra"]
    assert parse_map_list(words) == ["MP_001", "MP_011"]


def test_a_frostbite_1_map_list_is_a_flat_list_of_level_names() -> None:
    words = ["Levels/MP_001", "Levels/MP_002"]
    assert parse_map_list(words) == ["Levels/MP_001", "Levels/MP_002"]


def test_an_empty_page_reads_as_no_maps_rather_than_raising() -> None:
    assert parse_map_list(["0", "3"]) == []
    assert parse_map_list([]) == []


def test_the_two_generations_are_told_apart_by_name() -> None:
    assert LEGACY_MAPLIST == {"bfbc2", "moh"}


def test_every_frostbite_title_uses_its_own_rotate_verb() -> None:
    """Sending a title a verb it has not got is an unknown command, so `!maprotate` did nothing.

    There are **three** spellings across the six titles, not two. Frostbite 2 says
    `mapList.runNextRound`; the 2010 Medal of Honor says `admin.runNextRound`; and Bad Company 2 says
    `admin.runNextLevel` — that generation's two titles do not agree with each other. Every one of
    the six carried the Frostbite 2 spelling first, and then both Frostbite 1 titles carried Medal of
    Honor's, which is what `poweradminbfbc2` turned up: `!map` on Bad Company 2 inserted the map,
    pointed the server at it, and then sent the one command that would have loaded it to a title that
    does not take it.
    """
    assert BFBC2.rotate_command == "admin.runNextLevel"
    assert MOH.rotate_command == "admin.runNextRound"
    assert BF3.rotate_command == "mapList.runNextRound"


def test_frostbite_has_no_map_template_because_it_cannot_have_one() -> None:
    """Four commands, one of whose arguments is arithmetic over a reply. See FrostbiteClient."""
    assert BF3.map_template == ""


# -- the client, against the fake server ----------------------------------------


@pytest.fixture()
def frostbite_server():
    server = FakeFrostbiteServer(password="test", resend_unacked_after=99)
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _client(server, **kwargs):
    client = FrostbiteClient(*server.address, "test", timeout=2.0, **kwargs)
    client.open()
    return client


def test_the_rotation_is_paged_through_to_the_end(frostbite_server) -> None:
    """The fake pages two at a time, so a client that asked once would come back short."""
    frostbite_server.map_page_size = 2
    client = _client(frostbite_server)
    try:
        assert client.get_maps() == ["MP_001", "MP_011", "MP_013"]
    finally:
        client.close()


def test_changing_the_map_actually_changes_it(frostbite_server) -> None:
    """The assertion that the old code could not have passed: not "a command was sent" but "the
    server is now playing the map that was asked for"."""
    client = _client(frostbite_server)
    try:
        client.change_map("MP_007", {"gamemode": "RushLarge0", "rounds": "3"})
    finally:
        client.close()
    assert frostbite_server.current_map() == "MP_007"
    assert ("MP_007", "RushLarge0", "3") in frostbite_server.map_list


def test_a_map_change_with_no_mode_keeps_the_one_being_played(frostbite_server) -> None:
    """An empty gamemode is refused by the server outright, so it has to be filled in from somewhere."""
    client = _client(frostbite_server)
    try:
        client.change_map("MP_007")
    finally:
        client.close()
    assert frostbite_server.current_map() == "MP_007"
    added = [entry for entry in frostbite_server.map_list if entry[0] == "MP_007"]
    assert added and added[0][1] == "ConquestLarge0"  # what MP_001 was being played as


# -- chat pacing ----------------------------------------------------------------
#
# §1.15's say queue. BattlEye already had one, because an Arma server *drops* a burst of `say`s.
# Frostbite loses nothing on the wire — every `admin.say` is acknowledged and TCP will not reorder
# them — but the game shows each line for a moment, so a burst replaces the earlier ones before
# anybody can read them and a wrapped four-line reply arrives as its last line. The classic paced it
# for that reason, at 0.8s on Frostbite 2 and a full 2s on Frostbite 1.


def test_chat_is_paced_so_a_wrapped_reply_can_be_read(frostbite_server) -> None:
    clock = FakeClock(start=1000.0)
    client = _client(frostbite_server, clock=clock, say_interval=0.8)
    try:
        client.write('admin.say "line one" all')
        client.write('admin.say "line two" all')
        client.write('admin.say "line three" all')

        assert frostbite_server.wait_for_command("line one")
        # The rest are queued, not lost, and not sent yet.
        assert not any("line two" in " ".join(w) for w in frostbite_server.received)

        clock.advance(1.0)
        client.read_lines()  # the next pass through the loop releases the next line
        assert frostbite_server.wait_for_command("line two")
        assert not any("line three" in " ".join(w) for w in frostbite_server.received)

        clock.advance(1.0)
        client.read_lines()
        assert frostbite_server.wait_for_command("line three")
    finally:
        client.close()


def test_a_real_command_flushes_queued_chat_first(frostbite_server) -> None:
    """So "banning Bob" cannot arrive after the ban it announces."""
    clock = FakeClock(start=1000.0)
    client = _client(frostbite_server, clock=clock, say_interval=0.8)
    try:
        client.write('admin.say "banning Bob" all')
        client.write('admin.say "for cheating" all')
        client.command("version")
    finally:
        client.close()

    verbs = [" ".join(w) for w in frostbite_server.received]
    said = [i for i, c in enumerate(verbs) if "banning Bob" in c or "for cheating" in c]
    asked = [i for i, c in enumerate(verbs) if c.startswith("version")]
    assert len(said) == 2
    assert max(said) < max(asked)


def test_the_two_generations_are_paced_at_their_own_rate() -> None:
    """0.8s against a full 2s — the classic's two `_message_delay` values, and the same flag that
    already says which map list this server keeps decides it."""
    fast = FrostbiteClient("127.0.0.1", 1, "x")
    slow = FrostbiteClient("127.0.0.1", 1, "x", legacy_maplist=True)

    assert fast._say_interval == SAY_INTERVAL
    assert slow._say_interval == SAY_INTERVAL_FROSTBITE1
    # And an operator's explicit figure still wins over either default.
    assert FrostbiteClient("127.0.0.1", 1, "x", say_interval=0.0)._say_interval == 0.0


def test_a_frostbite_1_map_change_uses_the_flat_list_verbs(frostbite_server) -> None:
    frostbite_server.legacy_maplist = True
    client = _client(frostbite_server, legacy_maplist=True, rotate_command=MOH.rotate_command)
    try:
        client.change_map("Levels/MP_007")
    finally:
        client.close()
    verbs = [words[0] for words in frostbite_server.received]
    assert "mapList.insert" in verbs
    assert "admin.runNextRound" in verbs
    # `mapList.add` is Frostbite 2's, and this generation does not have it.
    assert "mapList.add" not in verbs
    assert frostbite_server.current_map() == "Levels/MP_007"


def test_the_last_step_of_a_map_change_is_the_titles_own_rotate_verb(frostbite_server) -> None:
    """The two Frostbite 1 titles differ here, and the client was hardcoded to Medal of Honor's.

    So on Bad Company 2 the map was inserted into the rotation and pointed at, and then the command
    that loads it was one the title has not got — a `!map` that did nothing until the round happened
    to end on its own.
    """
    frostbite_server.legacy_maplist = True
    client = _client(frostbite_server, legacy_maplist=True, rotate_command=BFBC2.rotate_command)
    try:
        client.change_map("Levels/MP_007")
    finally:
        client.close()
    verbs = [words[0] for words in frostbite_server.received]
    assert "admin.runNextLevel" in verbs
    assert "admin.runNextRound" not in verbs
    assert frostbite_server.current_map() == "Levels/MP_007"
