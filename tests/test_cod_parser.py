"""CoD4 parser: line grammar + the preserved kill/chat/auth quirks."""

from __future__ import annotations

from b3.core.events import EventType
from b3.parsers.cod.parser import CodParser, KillData
from b3.parsers.cod.profiles import COD4, COD5

GUID = "0123456789abcdef0123456789abcdef"  # 32 hex chars (valid cod4 guid)
GUID2 = "fedcba9876543210fedcba9876543210"


def parser() -> CodParser:
    return CodParser(COD4)


def one(p: CodParser, line: str):
    events = p.parse_line(line)
    assert len(events) == 1, f"expected 1 event, got {events!r}"
    return events[0]


def test_kill_enemy():
    p = parser()
    ev = one(p, f"K;{GUID};3;allies;Victim;{GUID2};5;axis;Killer;mp5_mp;100;MOD_RIFLE;chest")
    assert ev.type is EventType.CLIENT_KILL
    assert ev.client.cid == "5" and ev.client.name == "Killer"
    assert ev.target.cid == "3" and ev.target.name == "Victim"
    assert isinstance(ev.data, KillData)
    assert ev.data.weapon == "mp5_mp"
    assert ev.data.damage == 100


def test_teamkill():
    p = parser()
    ev = one(p, f"K;{GUID};3;allies;Victim;{GUID2};5;allies;Killer;mp5_mp;100;MOD_RIFLE;chest")
    assert ev.type is EventType.CLIENT_KILL_TEAM


def test_briefcase_bomb_is_not_teamkill():
    p = parser()
    # Same team, but the objective explosion must NOT be a team kill (preserved cod4 quirk).
    ev = one(
        p,
        f"K;{GUID};3;allies;Victim;{GUID2};5;allies;Killer;briefcase_bomb_mp;100;MOD_EXPLOSIVE;none",
    )
    assert ev.type is EventType.CLIENT_KILL


def test_cod4_omits_attacker_team_not_teamkill():
    p = parser()
    # Attacker team empty (cod4 omits it) -> cannot be a team kill even though victim is allies.
    ev = one(p, f"K;{GUID};3;allies;Victim;{GUID2};5;;Killer;mp5_mp;100;MOD_RIFLE;chest")
    assert ev.type is EventType.CLIENT_KILL


def test_world_death_is_suicide():
    p = parser()
    ev = one(p, f"K;{GUID};3;allies;Victim;;-1;world;;fall;100;MOD_FALLING;none")
    assert ev.type is EventType.CLIENT_SUICIDE
    assert ev.client is ev.target
    assert ev.client.cid == "3"


def test_self_kill_is_suicide():
    p = parser()
    ev = one(p, f"K;{GUID};3;allies;V;{GUID};3;allies;V;grenade;100;MOD_SPLASH;none")
    assert ev.type is EventType.CLIENT_SUICIDE


def test_say_strips_control_byte():
    p = parser()
    ev = one(p, "say;abc;3;PlayerName;\x15hello world")
    assert ev.type is EventType.CLIENT_SAY
    assert ev.data == "hello world"  # 0x15 prefix removed
    assert ev.client.name == "PlayerName"


def test_sayteam():
    p = parser()
    ev = one(p, "sayteam;abc;3;PlayerName;go B")
    assert ev.type is EventType.CLIENT_TEAM_SAY
    assert ev.data == "go B"


def test_join_valid_guid_and_registers_client():
    p = parser()
    ev = one(p, f"J;{GUID};4;NewPlayer")
    assert ev.type is EventType.CLIENT_JOIN
    assert ev.client.cid == "4"
    assert ev.client.guid == GUID  # valid 32-char guid kept
    assert p.clients.get_by_cid("4") is not None


def test_join_short_guid_rejected():
    p = parser()
    ev = one(p, "J;short;4;NewPlayer")
    assert ev.client.guid == ""  # partial guid discarded


def test_quit_removes_client():
    p = parser()
    p.parse_line(f"J;{GUID};4;NewPlayer")
    assert "4" in p.clients
    ev = one(p, f"Q;{GUID};4;NewPlayer")
    assert ev.type is EventType.CLIENT_DISCONNECT
    assert "4" not in p.clients


def test_init_game_round_start():
    p = parser()
    ev = one(p, r"InitGame: \g_gametype\dm\mapname\mp_crash\sv_maxclients\32")
    assert ev.type is EventType.GAME_ROUND_START
    assert ev.data["mapname"] == "mp_crash"
    assert ev.data["g_gametype"] == "dm"


def test_leading_timestamp_is_stripped():
    p = parser()
    ev = one(p, f"  3:24 K;{GUID};3;allies;Victim;{GUID2};5;axis;Killer;mp5_mp;100;MOD;chest")
    assert ev.type is EventType.CLIENT_KILL


def test_unmatched_line_yields_no_events():
    p = parser()
    assert p.parse_line("some random unparseable garbage") == []
    assert p.parse_line("") == []


# -- line types the first pass missed -------------------------------------------------------
#
# Found by auditing our grammar against the classic cod.py: it handles D, A, JT, tell, Item and
# ExitLevel, and we handled none of them. Two of those events had subscribers already — censor
# and chatlogger both listen for private messages — so they were quietly never firing.


def test_damage_between_enemies():
    p = parser()
    events = p.parse_line(
        "D;GUIDBOB;2;allies;Bob;GUIDADM;1;axis;Admin;mp5_mp;40;MOD_PISTOL_BULLET;chest"
    )
    assert [e.type for e in events] == [EventType.CLIENT_DAMAGE]
    assert events[0].client.name == "Admin"  # the one dealing it
    assert events[0].target.name == "Bob"
    assert events[0].data.damage == 40
    assert events[0].data.hit_location == "chest"


def test_damage_between_teammates_is_classified_as_team_damage():
    """What a team-kill plugin actually watches — it acts long before anyone dies."""
    p = parser()
    events = p.parse_line(
        "D;GUIDBOB;2;allies;Bob;GUIDADM;1;allies;Admin;mp5_mp;40;MOD_PISTOL_BULLET;chest"
    )
    assert [e.type for e in events] == [EventType.CLIENT_DAMAGE_TEAM]


def test_damage_to_yourself():
    p = parser()
    events = p.parse_line(
        "D;GUIDBOB;2;allies;Bob;GUIDBOB;2;allies;Bob;grenade_mp;99;MOD_GRENADE;none"
    )
    assert [e.type for e in events] == [EventType.CLIENT_DAMAGE_SELF]


def test_damage_from_the_world_has_nobody_to_blame():
    """Falling damage has no attacker, so there is no event — as classically."""
    p = parser()
    assert p.parse_line(
        "D;GUIDBOB;2;allies;Bob;;-1;world;;none;25;MOD_FALLING;none"
    ) == []


def test_decimal_damage_values_are_accepted():
    """The engine writes these with decimals; int() alone would raise on every one."""
    p = parser()
    events = p.parse_line(
        "D;GUIDBOB;2;allies;Bob;GUIDADM;1;axis;Admin;mp5_mp;33.500;MOD_RIFLE;head"
    )
    assert events[0].data.damage == 33


def test_an_objective_action():
    p = parser()
    events = p.parse_line("A;GUIDADM;1;axis;Admin;bomb_plant")
    assert [e.type for e in events] == [EventType.CLIENT_ACTION]
    assert events[0].data == "bomb_plant"
    assert events[0].client.team == "red"  # axis, canonicalised


def test_a_team_change_line():
    p = parser()
    events = p.parse_line("JT;GUIDBOB;2;allies;Bob;")  # note the trailing semicolon
    assert [e.type for e in events] == [EventType.CLIENT_TEAM_CHANGE]
    assert p.clients.get_by_cid("2").team == "blue"


def test_a_private_message_names_both_ends():
    """CLIENT_PRIVATE_SAY had subscribers (censor, chatlogger) and nothing ever emitted it."""
    p = parser()
    events = p.parse_line("tell;GUIDADM;1;Admin;GUIDBOB;2;Bob;meet me at B")
    assert [e.type for e in events] == [EventType.CLIENT_PRIVATE_SAY]
    assert events[0].client.name == "Admin"
    assert events[0].target.name == "Bob"
    assert events[0].data == "meet me at B"


def test_a_private_message_strips_the_control_byte():
    p = parser()
    events = p.parse_line("tell;GUIDADM;1;Admin;GUIDBOB;2;Bob;\x15hidden prefix")
    assert events[0].data == "hidden prefix"


def test_an_item_pickup():
    p = parser()
    events = p.parse_line("Item;GUIDADM;1;Admin;weapon_ak47")
    assert [e.type for e in events] == [EventType.CLIENT_ITEM_PICKUP]
    assert events[0].data == "weapon_ak47"


def test_the_end_of_a_map():
    p = parser()
    events = p.parse_line("ExitLevel: executed")
    assert [e.type for e in events] == [EventType.GAME_EXIT]


# -- Treyarch's per-action lines (cod5) ---------------------------------------------------------


def cod5_parser() -> CodParser:
    return CodParser(COD5)


def test_cod5_reports_an_objective_action():
    p = cod5_parser()
    events = p.parse_line(f"bp;{GUID};3;allies;Bomber")
    assert [e.type for e in events] == [EventType.CLIENT_ACTION]
    assert events[0].data == "bomb_planted"  # not the bare "bp" the classic bot published
    assert events[0].client.cid == "3"


def test_cod5_reports_actor_damage_from_dogs_and_ai():
    """The line World at War servers lost entirely before `action_map` existed."""
    p = cod5_parser()
    events = p.parse_line(f"ad;{GUID};3;allies;Handler;dog_mp;25;MOD_MELEE;torso")
    assert [e.type for e in events] == [EventType.CLIENT_ACTION]
    assert events[0].data == "actor_damage"


def test_the_same_line_on_cod4_produces_nothing():
    """cod4 puts its actions in `A;`, so these two letters are not the bot's to interpret."""
    assert parser().parse_line(f"ad;{GUID};3;allies;Handler;dog_mp;25;MOD_MELEE;torso") == []


def test_an_action_line_does_not_rename_the_player_to_their_team():
    """The classic parser matched these with its chat pattern, which lined `team` up with `name`."""
    p = cod5_parser()
    p.parse_line(f"J;{GUID};3;Handler")
    p.parse_line(f"ft;{GUID};3;allies;Handler")
    assert p.clients.get_by_cid("3").name == "Handler"
    assert p.clients.get_by_cid("3").team == "blue"


def test_a_team_change_line_is_not_swallowed_by_the_action_pattern():
    """`JT` is two letters too; the router must still reach the team-change handler."""
    p = cod5_parser()
    events = p.parse_line(f"JT;{GUID};4;axis;Rambo;")
    assert [e.type for e in events] == [EventType.CLIENT_TEAM_CHANGE]


def test_an_unmapped_two_letter_line_is_ignored_not_guessed():
    assert cod5_parser().parse_line(f"zz;{GUID};3;allies;Nobody") == []
