"""Map naming and map resolution.

Map ids are awkward to type (`Thrust_Oilrig`, `MP_Subway`, `fl-harbor`) and sending one the server
does not have fails silently, since `change_map` gets no reply. `!map` therefore resolves what was
typed against the rotation, by id or by display name.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.core.util import match_names
from b3.domain.client import Client
from b3.parsers.frostbite.profiles import BF3, BF4, BFBC2, MOH
from b3.plugins.admin import AdminPlugin

BF3_ROTATION = ["MP_001", "MP_Subway", "XP1_002"]


def _setup(console):  # noqa: ANN001, ANN202
    plugin = AdminPlugin(console)
    plugin.register_commands()
    # `!map` waits before changing so its announcement can be read (see `cmd_map`). That wait is
    # real seconds, and four tests in this file change a map: the pause has one test of its own
    # below, and everything else here is about which map was chosen rather than when.
    plugin.settings["map_announce_pause"] = 0
    return plugin, CommandProcessor(console.command_registry, console)


def _admin() -> Client:
    return Client(guid="A", name="Admin", group_bits=128, cid="0", id=1)


def _frostbite(console) -> None:  # noqa: ANN001
    console.maps = list(BF3_ROTATION)
    console.map_names = dict(BF3.map_names)


# -- the matcher -----------------------------------------------------------------------------


def test_an_exact_match_is_never_ambiguous_with_a_longer_one():
    """ "Operation Metro" must resolve even when "Operation Metro 2014" is in the same rotation."""
    options = [("XP0_Metro", "Operation Metro 2014"), ("MP_Subway", "Operation Metro")]

    assert match_names("Operation Metro", options) == ["MP_Subway"]


def test_a_prefix_beats_a_substring():
    options = [("MP_Prison", "Operation Locker"), ("MP_Naval", "Paracel Storm")]

    assert match_names("oper", options) == ["MP_Prison"]


def test_a_substring_can_match_several():
    options = [("XP0_Metro", "Operation Metro 2014"), ("MP_Subway", "Operation Metro")]

    assert match_names("metro", options) == ["XP0_Metro", "MP_Subway"]


def test_a_typo_still_resolves():
    options = [("MP_Damage", "Lancang Dam"), ("MP_Naval", "Paracel Storm")]

    assert match_names("lancang dan", options) == ["MP_Damage"]


def test_nothing_close_matches_nothing():
    assert match_names("zzzz", [("MP_Damage", "Lancang Dam")]) == []


def test_an_empty_handle_matches_nothing():
    assert match_names("   ", [("MP_Damage", "Lancang Dam")]) == []


# -- the map name tables ---------------------------------------------------------------------


def test_a_frostbite_2_id_becomes_a_name_people_use():
    assert BF3.map_display("MP_Subway") == "Operation Metro"
    assert BF4.map_display("XP4_WlkrFtry") == "Giants Of Karelia"


def test_the_lookup_is_case_insensitive():
    assert BF3.map_display("mp_001") == BF3.map_display("MP_001") == "Grand Bazaar"


def test_frostbite_1_matches_the_level_path_by_prefix():
    """Bad Company 2 reports a path with a gametype suffix, which an exact table would miss."""
    assert BFBC2.map_display("Levels/MP_001") == "Panama Canal"
    assert BFBC2.map_display("Levels/MP_001_RUSH") == "Panama Canal"


def test_the_longest_prefix_wins():
    """On Medal of Honor `mp_01_elimination` is a different map from `mp_01`, not a mode of it."""
    assert MOH.map_display("levels/mp_01_elimination") == "Bagram Hanger"
    assert MOH.map_display("levels/mp_01") == "Mazar-i-Sharif Airfield"


def test_an_unknown_id_comes_back_as_itself():
    assert BF3.map_display("MP_Something_New") == "MP_Something_New"


def test_a_title_with_no_table_is_untouched():
    from b3.parsers.q3.profiles import Q3

    assert Q3.map_display("q3dm17") == "q3dm17"


# -- !map ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_partial_name_reaches_the_right_map(console):
    _, proc = _setup(console)
    _frostbite(console)

    await proc.handle(_admin(), "!map metro")

    assert console.map_changes == ["MP_Subway"]


@pytest.mark.asyncio
async def test_the_display_name_can_be_typed(console):
    """An admin can type the name shown in game rather than the engine id."""
    _, proc = _setup(console)
    _frostbite(console)

    await proc.handle(_admin(), "!map grand bazaar")

    assert console.map_changes == ["MP_001"]


@pytest.mark.asyncio
async def test_the_announcement_uses_the_display_name(console):
    _, proc = _setup(console)
    _frostbite(console)

    await proc.handle(_admin(), "!map metro")

    assert "Operation Metro" in console.said[-1]


@pytest.mark.asyncio
async def test_a_map_that_is_not_in_the_rotation_is_refused(console):
    _, proc = _setup(console)
    _frostbite(console)

    await proc.handle(_admin(), "!map oilrig")

    assert console.map_changes == []
    assert "no map in the rotation" in console.told[0][1]


@pytest.mark.asyncio
async def test_a_short_rotation_is_listed_when_nothing_matches(console):
    _, proc = _setup(console)
    _frostbite(console)

    await proc.handle(_admin(), "!map oilrig")

    assert "Grand Bazaar" in console.told[-1][1]


@pytest.mark.asyncio
async def test_an_ambiguous_partial_name_is_refused(console):
    _, proc = _setup(console)
    console.maps = ["MP_Subway", "XP0_Metro"]
    console.map_names = dict(BF4.map_names) | {"mp_subway": "Operation Metro"}

    await proc.handle(_admin(), "!map metro")

    assert console.map_changes == []
    assert "2 maps match" in console.told[-1][1]


@pytest.mark.asyncio
async def test_an_engine_whose_rotation_cannot_be_read_sends_what_was_typed(console):
    """With nothing to match against there is no basis for refusing the name."""
    _, proc = _setup(console)
    console.maps = []

    await proc.handle(_admin(), "!map mp_crossfire")

    assert console.map_changes == ["mp_crossfire"]


@pytest.mark.asyncio
async def test_maps_and_nextmap_print_display_names(console):
    _, proc = _setup(console)
    _frostbite(console)
    console.next_map = "XP1_002"

    await proc.handle(_admin(), "!maps")
    assert "Operation Metro" in console.told[-1][1]

    await proc.handle(_admin(), "!nextmap")
    assert "Gulf of Oman" in console.told[-1][1]


@pytest.mark.asyncio
async def test_the_announcement_gets_a_moment_before_the_map_changes(console, monkeypatch):
    """A level change takes effect at once, so an unpaused announcement is never read.

    Found on a live CoD4X server: the `say` and the `InitGame` landed in the same second, so nobody
    saw the line on the map it was about - and Call of Duty replays recent chat once a client has
    loaded, which put "changing map to shipment" in front of somebody already standing in shipment.
    """
    plugin, proc = _setup(console)
    plugin.settings["map_announce_pause"] = 2
    console.maps = ["mp_crash"]
    when_it_slept = []

    async def fake_sleep(seconds):  # noqa: ANN001, ANN202
        # What had happened by the time it waited: the announcement, and not the map change.
        when_it_slept.append((seconds, list(console.said), list(console.map_changes)))

    monkeypatch.setattr("b3.plugins.admin.asyncio.sleep", fake_sleep)

    await proc.handle(_admin(), "!map mp_crash")

    assert len(when_it_slept) == 1, "it waited once"
    seconds, said, changed = when_it_slept[0]
    assert seconds == 2
    assert said and "mp_crash" in said[-1], "the announcement went first"
    assert changed == [], "and the map had not changed yet"
    assert console.map_changes == ["mp_crash"], "then it did"


@pytest.mark.asyncio
async def test_a_pause_of_zero_changes_the_map_at_once(console, monkeypatch):
    """For an operator who would rather have the map than the courtesy."""
    plugin, proc = _setup(console)
    plugin.settings["map_announce_pause"] = 0
    console.maps = ["mp_crash"]
    slept = []
    monkeypatch.setattr("b3.plugins.admin.asyncio.sleep", lambda s: slept.append(s))

    await proc.handle(_admin(), "!map mp_crash")

    assert slept == [], "no wait at all, not a wait of zero"
    assert console.map_changes == ["mp_crash"]
