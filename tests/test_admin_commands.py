"""The rest of the classic command surface: lookup and listing, bulk penalties, warning
inspection, configured rules/spam, and the bot-lifecycle commands.

Driven through the core command processor so each command's declared level is exercised too.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor, command
from b3.core.game import PlayerInfo
from b3.core.plugin import Plugin
from b3.domain.client import NEVER_EXPIRES, Client, Penalty, PenaltyType
from b3.plugins.admin import AdminPlugin

BITS = {
    "guest": 0,
    "user": 1,
    "reg": 2,
    "mod": 8,
    "admin": 16,
    "fulladmin": 32,
    "senioradmin": 64,
    "superadmin": 128,
}

ADMIN_CONFIG = {
    "settings": {"ban_duration": 0},
    "spamages": {
        "rule1": "Rule #1: no cheating",
        "rule2": "Rule #2: no stacking",
        "vent": "voice chat: vent.example.com",
    },
    "warn_reasons": {
        "lang": "3h, watch your language",
        "cuss": "/lang",
        "stack": "/spam#rule2",
        "afk": "away from your keyboard",
        "loop": "/loop",
    },
}


def _setup(console, config=None):
    plugin = AdminPlugin(console, config if config is not None else ADMIN_CONFIG)
    plugin.start()  # start(), not register_commands(): we want on_load_config to run
    return plugin, CommandProcessor(console.command_registry, console)


def _client(name, keyword="guest", *, cid="1", id_=1):  # noqa: ANN001, ANN202
    return Client(guid=name[0].upper(), name=name, cid=cid, id=id_, group_bits=BITS[keyword])


def _boss() -> Client:
    return _client("Boss", "superadmin", cid="0", id_=1)


def _last(console) -> str:
    return console.told[-1][1]


# -- reason keywords -------------------------------------------------------------------------


def test_reason_keyword_expands_with_its_duration(console):
    plugin, _ = _setup(console)
    assert plugin.resolve_reason("lang") == (180, "watch your language")


def test_reason_keyword_without_a_duration(console):
    plugin, _ = _setup(console)
    assert plugin.resolve_reason("afk") == (0, "away from your keyboard")


def test_reason_keyword_can_point_at_another(console):
    plugin, _ = _setup(console)
    assert plugin.resolve_reason("cuss") == (180, "watch your language")


def test_reason_keyword_can_point_at_a_spam_message(console):
    plugin, _ = _setup(console)
    assert plugin.resolve_reason("stack") == (0, "Rule #2: no stacking")


def test_a_self_referencing_reason_does_not_hang(console):
    plugin, _ = _setup(console)
    assert plugin.resolve_reason("loop") == (0, "loop")


def test_free_text_reasons_are_left_alone(console):
    plugin, _ = _setup(console)
    assert plugin.resolve_reason("being a nuisance") == (0, "being a nuisance")
    assert plugin.resolve_reason("unknownkeyword") == (0, "unknownkeyword")


@pytest.mark.asyncio
async def test_warn_uses_the_keyword_text_and_duration(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_boss(), "!warn Bob lang")

    target, reason, _admin = console.warned[-1]
    assert target is bob
    assert reason == "watch your language"  # the keyword was expanded, not stored raw


# -- ban duration ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ban_duration_zero_makes_ban_permanent(console):
    _, proc = _setup(console)  # ADMIN_CONFIG pins ban_duration: 0
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_boss(), "!ban Bob cheating")

    assert console.banned[-1][0] is bob
    assert console.tempbanned == []


@pytest.mark.asyncio
async def test_ban_is_a_14_day_tempban_out_of_the_box(console):
    """The classic default, which we keep: `!ban` is temporary, `!permban` is forever."""
    _, proc = _setup(console, {})  # no settings at all -> the shipped defaults
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_boss(), "!ban Bob cheating")

    assert console.banned == []
    assert console.tempbanned[-1][1] == 14 * 24 * 60


@pytest.mark.asyncio
async def test_permban_is_permanent_even_with_a_ban_duration(console):
    _, proc = _setup(console, {"settings": {"ban_duration": "14d"}})
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_boss(), "!pb Bob cheating")

    assert console.banned[-1][0] is bob
    assert console.tempbanned == []


# -- bulk actions ----------------------------------------------------------------------------


def _crowd(console) -> None:
    console.clients.add(_client("clanBob", "user", cid="1", id_=2))
    console.clients.add(_client("clanSue", "user", cid="2", id_=3))
    console.clients.add(_client("Solo", "user", cid="3", id_=4))
    console.clients.add(_client("clanMod", "senioradmin", cid="4", id_=5))


@pytest.mark.asyncio
async def test_kickall_hits_every_match(console):
    _, proc = _setup(console)
    _crowd(console)

    await proc.handle(_boss(), "!kickall clan spamming")

    assert sorted(c.name for c, _r, _a in console.kicked) == ["clanBob", "clanMod", "clanSue"]
    assert all(reason == "spamming" for _c, reason, _a in console.kicked)
    assert "kicked 3 player(s)" in _last(console)


@pytest.mark.asyncio
async def test_bulk_commands_skip_players_out_of_your_reach(console):
    _, proc = _setup(console)
    _crowd(console)
    senior = _client("Senior", "senioradmin", cid="9", id_=9)

    await proc.handle(senior, "!kickall clan spamming")

    kicked = sorted(c.name for c, _r, _a in console.kicked)
    assert kicked == ["clanBob", "clanSue"]  # clanMod is a senioradmin too


@pytest.mark.asyncio
async def test_bulk_command_cannot_hit_yourself(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="0", id_=1)
    console.clients.add(boss)
    console.clients.add(_client("Bossy", "user", cid="1", id_=2))

    await proc.handle(boss, "!kickall Boss")

    assert [c.name for c, _r, _a in console.kicked] == ["Bossy"]


@pytest.mark.asyncio
async def test_banall_and_spankall(console):
    _, proc = _setup(console)
    _crowd(console)

    await proc.handle(_boss(), "!ball clanB")
    assert [c.name for c, _r, _a in console.banned] == ["clanBob"]

    await proc.handle(_boss(), "!sall clanS")
    assert [c.name for c, _r, _a in console.kicked] == ["clanSue"]
    assert any("SPANK" in s.upper() or "spanked" in s for s in console.said)


@pytest.mark.asyncio
async def test_bulk_command_with_no_matches(console):
    _, proc = _setup(console)
    _crowd(console)
    await proc.handle(_boss(), "!kickall nobody")
    assert console.kicked == []
    assert "no players match" in _last(console)


@pytest.mark.asyncio
async def test_spank_kicks_loudly(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_boss(), "!sp Bob camping")

    assert console.kicked[-1][0] is bob
    assert "Bob was spanked by Boss" in console.said


# -- warnings --------------------------------------------------------------------------------


def _warn(
    console, client_id: int, reason: str, *, id_: int, expire: int = NEVER_EXPIRES
) -> Penalty:
    penalty = Penalty(
        type=PenaltyType.WARNING, client_id=client_id, reason=reason, id=id_, time_expire=expire
    )
    console.storage.penalties.append(penalty)
    return penalty


@pytest.mark.asyncio
async def test_warninfo_reports_the_count_and_latest_reason(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    _warn(console, 7, "spawn camping", id_=1)
    _warn(console, 7, "language", id_=2)

    await proc.handle(_boss(), "!wi Bob")

    reply = _last(console)
    assert "Bob has 2 warning(s)" in reply


@pytest.mark.asyncio
async def test_warninfo_shows_when_a_warning_expires(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    _warn(console, 7, "language", id_=1, expire=console.clock.epoch() + 3600)

    await proc.handle(_boss(), "!wi Bob")

    assert "expires in 1 hour" in _last(console)


@pytest.mark.asyncio
async def test_warninfo_with_no_warnings(console):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))
    await proc.handle(_boss(), "!warninfo Bob")
    assert "no active warnings" in _last(console)


@pytest.mark.asyncio
async def test_warnremove_lifts_only_the_latest(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    first = _warn(console, 7, "spawn camping", id_=1)
    latest = _warn(console, 7, "language", id_=2)

    await proc.handle(_boss(), "!wr Bob")

    assert latest.inactive is True
    assert first.inactive is False
    assert "language" in _last(console)


@pytest.mark.asyncio
async def test_warntest_shows_the_expansion(console):
    _, proc = _setup(console)
    await proc.handle(_boss(), "!wt lang")
    assert "watch your language" in _last(console)
    assert "3 hours" in _last(console)


@pytest.mark.asyncio
async def test_clear_wipes_one_players_warnings(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    warning = _warn(console, 7, "language", id_=1)

    await proc.handle(_boss(), "!clear Bob")

    assert warning.inactive is True
    assert "cleared Bob's warnings" in console.said[-1]


@pytest.mark.asyncio
async def test_clear_with_no_argument_wipes_everyone(console):
    _, proc = _setup(console)
    console.clients.add(_client("Bob", "user", cid="1", id_=7))
    console.clients.add(_client("Sue", "user", cid="2", id_=8))
    a = _warn(console, 7, "language", id_=1)
    b = _warn(console, 8, "camping", id_=2)

    await proc.handle(_boss(), "!kiss")

    assert a.inactive and b.inactive
    assert "cleared everyone's warnings" in console.said[-1]


# -- lookup and listing ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_finds_stored_players(console):
    _, proc = _setup(console)
    gone = Client(guid="G", name="Gone", id=42, time_edit=1_700_000_000)
    console.register_lookup("Gone", [gone])

    await proc.handle(_boss(), "!l Gone")

    assert "@42 Gone" in _last(console)


@pytest.mark.asyncio
async def test_lookup_with_no_match(console):
    _, proc = _setup(console)
    await proc.handle(_boss(), "!lookup nobody")
    assert "no stored player matches" in _last(console)


@pytest.mark.asyncio
async def test_find_locates_a_connected_player(console):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))
    await proc.handle(_boss(), "!find Bob")
    assert "found Bob in slot 4" in _last(console)


@pytest.mark.asyncio
async def test_seen_reports_when_a_player_was_last_around(console):
    _, proc = _setup(console)
    gone = Client(guid="G", name="Gone", id=42, time_edit=1_700_000_000)
    console.register_lookup("Gone", [gone])

    await proc.handle(_boss(), "!seen Gone")

    assert "Gone was last seen 2023-11-1" in _last(console)


@pytest.mark.asyncio
async def test_list_and_longlist(console):
    _, proc = _setup(console)
    console.clients.add(_client("Bob", "mod", cid="4", id_=7))
    console.players = [PlayerInfo(cid="4", name="Bob", ping=42)]

    await proc.handle(_boss(), "!list")
    assert "[4] Bob" in _last(console)

    await proc.handle(_boss(), "!longlist")
    assert _last(console) == "[4] Bob @7 level 20 ping 42"


@pytest.mark.asyncio
async def test_longlist_still_works_when_the_server_cannot_be_reached(console):
    _, proc = _setup(console)
    console.clients.add(_client("Bob", "mod", cid="4", id_=7))

    def boom():
        raise OSError("rcon down")

    console.get_players = boom

    await proc.handle(_boss(), "!longlist")
    assert "ping ?" in _last(console)


@pytest.mark.asyncio
async def test_list_with_nobody_connected(console):
    _, proc = _setup(console)
    await proc.handle(_boss(), "!list")
    assert "nobody is connected" in _last(console)


@pytest.mark.asyncio
async def test_lastbans_lists_the_bans_in_force(console):
    _, proc = _setup(console)
    bob = Client(guid="B", name="Bob", id=7)
    console.storage.clients_by_id[7] = bob
    console.storage.penalties.append(
        Penalty(type=PenaltyType.BAN, client_id=7, reason="cheating", id=1)
    )

    await proc.handle(_boss(), "!lbans")

    assert "Bob" in _last(console) and "cheating" in _last(console)


@pytest.mark.asyncio
async def test_lastbans_with_nothing_in_force(console):
    _, proc = _setup(console)
    await proc.handle(_boss(), "!lastbans")
    assert "no bans are currently in force" in _last(console)


# -- output ------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_say_and_scream(console):
    _, proc = _setup(console)
    await proc.handle(_boss(), "!say hello everyone")
    assert console.said[-1] == "hello everyone"

    await proc.handle(_boss(), "!scream round starting")
    assert console.said_big[-1] == "round starting"


@pytest.mark.asyncio
async def test_poke_names_the_player(console):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))
    await proc.handle(_boss(), "!poke Bob")
    assert "Bob!" in console.said[-1]


@pytest.mark.asyncio
async def test_notice_records_a_note(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_boss(), "!notice Bob helpful player")

    assert console.noticed[-1][:2] == (bob, "helpful player")
    assert "notice added to Bob" in _last(console)


@pytest.mark.asyncio
async def test_rules_says_the_configured_rules_in_order(console):
    _, proc = _setup(console)
    await proc.handle(_client("Newbie", "guest", cid="5", id_=6), "!rules")
    said = [text for _c, text in console.told]
    assert said == ["Rule #1: no cheating", "Rule #2: no stacking"]


@pytest.mark.asyncio
async def test_rules_with_none_configured(console):
    _, proc = _setup(console, {})
    await proc.handle(_boss(), "!r")
    assert "no rules are configured" in _last(console)


@pytest.mark.asyncio
async def test_spam_and_spams(console):
    _, proc = _setup(console)

    await proc.handle(_boss(), "!s vent")
    assert console.said[-1] == "voice chat: vent.example.com"

    await proc.handle(_boss(), "!spams")
    assert "rule1, rule2, vent" in _last(console)

    await proc.handle(_boss(), "!spam nope")
    assert "no spam message called 'nope'" in _last(console)


@pytest.mark.asyncio
async def test_time_and_b3(console):
    _, proc = _setup(console)

    await proc.handle(_boss(), "!time")
    assert "server time:" in _last(console)

    await proc.handle(_boss(), "!b3")
    assert "Big Brother Bot" in _last(console)


# -- lifecycle ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_and_resume(console):
    _, proc = _setup(console)

    await proc.handle(_boss(), "!pause 30m")
    assert console.paused_minutes[-1] == 30
    assert "not acting on the game for 30 minutes" in console.said[-1]

    await proc.handle(_boss(), "!pause 0")
    assert console.paused_minutes[-1] == 0
    assert "back on duty" in console.said[-1]


@pytest.mark.asyncio
async def test_pause_rejects_nonsense(console):
    _, proc = _setup(console)
    await proc.handle(_boss(), "!pause soon")
    assert console.paused_minutes == []
    assert "invalid duration" in _last(console)


@pytest.mark.asyncio
async def test_rebuild_resyncs(console):
    _, proc = _setup(console)
    console.clients.add(_client("Bob", "user", cid="4", id_=7))
    await proc.handle(_boss(), "!rebuild")
    assert "1 player(s)" in _last(console)


@pytest.mark.asyncio
async def test_die_and_restart_set_the_exit_code(console):
    _, proc = _setup(console)

    await proc.handle(_boss(), "!die")
    assert console.exit_code == 0

    await proc.handle(_boss(), "!restart")
    assert console.exit_code == 221  # the classic restart code


@pytest.mark.asyncio
async def test_reconfig_reloads(console):
    _, proc = _setup(console)
    await proc.handle(_boss(), "!reconfig")
    assert console.reloads == 1


@pytest.mark.asyncio
async def test_runas_runs_with_the_targets_level_not_yours(console):
    """An admin checking what an ordinary player can do must not lend them their own powers."""
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    console.register_client("Sue", _client("Sue", "user", cid="5", id_=8))

    await proc.handle(_boss(), "!su Bob !kick Sue")

    assert console.kicked == []  # Bob is level 1; kick needs 40
    assert console.told[-1][0] is bob  # and Bob is the one told he cannot


@pytest.mark.asyncio
async def test_runas_needs_a_command(console):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))
    await proc.handle(_boss(), "!runas Bob")
    assert "usage: runas" in _last(console)


# -- levels ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "too_low", "high_enough"),
    [
        ("!die", "senioradmin", "superadmin"),
        ("!reconfig", "senioradmin", "superadmin"),
        ("!pause 1", "admin", "senioradmin"),
        ("!lastbans", "mod", "admin"),
        ("!kick Bob", "mod", "admin"),
        ("!ban Bob", "admin", "senioradmin"),
        ("!seen Bob", "user", "reg"),
        ("!time", "guest", "user"),
    ],
)
@pytest.mark.asyncio
async def test_commands_sit_at_the_classic_levels(console, command, too_low, high_enough):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "guest", cid="4", id_=7))

    await proc.handle(_client("Low", too_low, cid="8", id_=80), command)
    assert "sufficient access" in _last(console)

    console.told.clear()  # some commands answer with `say`, so don't read a stale reply
    await proc.handle(_client("High", high_enough, cid="9", id_=90), command)
    assert not any("sufficient access" in text for _c, text in console.told)


# -- warning escalation ---------------------------------------------------------------------------

ESCALATION_CONFIG = {
    **ADMIN_CONFIG,
    "warn": {
        "delay": 0,  # no rate limit, so a test can warn repeatedly
        "alert_at": 3,
        "grace": 25,
        "kick_at": 5,
        "tempban_at": 6,
        "tempban_duration": "1d",
        "max_duration": "1d",
        "duration_divider": 30,
    },
}


async def _warn_times(proc, console, target_name: str, times: int) -> None:
    for _ in range(times):
        await proc.handle(_boss(), f"!warn {target_name} lang")  # 3h each
        await console.bus.drain()


@pytest.mark.asyncio
async def test_each_warning_is_announced_with_its_running_count(console):
    _, proc = _setup(console, ESCALATION_CONFIG)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await _warn_times(proc, console, "Bob", 2)

    assert "WARNING [1]: Bob, watch your language" in console.said
    assert "WARNING [2]: Bob, watch your language" in console.said


@pytest.mark.asyncio
async def test_the_third_warning_alerts_but_does_not_punish_yet(console):
    _, proc = _setup(console, ESCALATION_CONFIG)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await _warn_times(proc, console, "Bob", 3)

    assert any("ALERT: Bob will be banned" in s for s in console.said)
    assert console.tempbanned == []  # the grace period has not run out


@pytest.mark.asyncio
async def test_clearing_the_warnings_inside_the_grace_period_cancels_the_ban(console):
    plugin, proc = _setup(console, ESCALATION_CONFIG)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    await _warn_times(proc, console, "Bob", 3)

    await proc.handle(_boss(), "!clear Bob")
    console.clock.advance(30)
    plugin._check_pending_kicks()

    assert console.tempbanned == []  # an admin stepped in; nothing happens


@pytest.mark.asyncio
async def test_warnings_left_alone_turn_into_a_tempban_when_the_grace_runs_out(console):
    plugin, proc = _setup(console, ESCALATION_CONFIG)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    console.clients.add(bob)
    await _warn_times(proc, console, "Bob", 3)

    console.clock.advance(30)
    plugin._check_pending_kicks()

    target, minutes, reason, _admin = console.tempbanned[-1]
    assert target is bob
    assert minutes == 18  # three 3h warnings = 540 min, / 30 = 18
    # The ban says what they were last warned for, not just "too many warnings".
    assert reason == "too many warnings: watch your language"


@pytest.mark.asyncio
async def test_the_fifth_warning_bans_at_once_with_no_grace(console):
    _, proc = _setup(console, ESCALATION_CONFIG)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await _warn_times(proc, console, "Bob", 5)

    assert console.tempbanned != []
    assert console.tempbanned[-1][1] == 30  # five 3h warnings = 900 min, / 30


@pytest.mark.asyncio
async def test_past_the_tempban_threshold_the_flat_duration_applies(console):
    _, proc = _setup(console, ESCALATION_CONFIG)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await _warn_times(proc, console, "Bob", 7)

    assert console.tempbanned[-1][1] == 24 * 60  # tempban_duration, not the growing sum


@pytest.mark.asyncio
async def test_the_computed_ban_is_capped(console):
    config = {**ESCALATION_CONFIG, "warn": {**ESCALATION_CONFIG["warn"], "max_duration": "10m"}}
    _, proc = _setup(console, config)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await _warn_times(proc, console, "Bob", 5)

    assert console.tempbanned[-1][1] == 10


@pytest.mark.asyncio
async def test_a_warning_from_another_plugin_counts_towards_the_kick(console):
    """Escalation hangs off the event, so a censor/tk plugin's warning escalates too."""
    _, proc = _setup(console, ESCALATION_CONFIG)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    await _warn_times(proc, console, "Bob", 4)

    console.warn(bob, "swearing", minutes=180)  # not via !warn — straight at the Console port
    await console.bus.drain()

    assert console.tempbanned != []


@pytest.mark.asyncio
async def test_warn_delay_stops_admins_piling_on(console):
    config = {**ESCALATION_CONFIG, "warn": {**ESCALATION_CONFIG["warn"], "delay": 15}}
    _, proc = _setup(console, config)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_boss(), "!warn Bob lang")
    await proc.handle(_boss(), "!warn Bob lang")
    await console.bus.drain()

    assert len(console.warned) == 1
    assert "only one warning per 15 seconds" in _last(console)

    console.clock.advance(16)
    await proc.handle(_boss(), "!warn Bob lang")
    assert len(console.warned) == 2


@pytest.mark.asyncio
async def test_pm_global_tells_the_player_instead_of_announcing(console):
    config = {**ESCALATION_CONFIG, "warn": {**ESCALATION_CONFIG["warn"], "pm_global": True}}
    _, proc = _setup(console, config)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await _warn_times(proc, console, "Bob", 1)

    assert console.said == []
    assert any("WARNING [1]" in text for _c, text in console.told)


# -- policy settings the classic bot had ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_junior_admin_cannot_hand_out_a_ten_year_tempban(console):
    """`long_tempban_level` — the cap the classic bot put on `!tempban` for junior admins."""
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    admin = _client("Admin", "admin", cid="8", id_=80)  # level 40, below long_tempban_level

    await proc.handle(admin, "!tempban Bob 30d spam")
    assert console.tempbanned == []
    assert "may not ban for longer than 3 hours" in _last(console)

    await proc.handle(admin, "!tempban Bob 2h spam")
    assert console.tempbanned[-1][1] == 120  # inside the cap, so it goes through


@pytest.mark.asyncio
async def test_a_senior_admin_is_not_capped(console):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))

    await proc.handle(_client("Senior", "senioradmin", cid="8", id_=80), "!tempban Bob 30d spam")

    assert console.tempbanned[-1][1] == 30 * 24 * 60


@pytest.mark.asyncio
async def test_noreason_level_makes_a_reason_compulsory(console):
    config = {**ADMIN_CONFIG, "settings": {"noreason_level": 100}}
    _, proc = _setup(console, config)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    senior = _client("Senior", "senioradmin", cid="8", id_=80)

    await proc.handle(senior, "!kick Bob")
    assert console.kicked == []
    assert "must supply a reason" in _last(console)

    await proc.handle(senior, "!kick Bob spawn killing")
    assert console.kicked != []

    await proc.handle(_boss(), "!kick Bob")  # a superadmin is above the threshold
    assert len(console.kicked) == 2


@pytest.mark.asyncio
async def test_a_reason_is_required_by_default_below_superadmin(console):
    """The classic `noreason_level`, which defaults to superadmin."""
    _, proc = _setup(console, {})  # the shipped defaults
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))

    await proc.handle(_client("Senior", "senioradmin", cid="9", id_=9), "!kick Bob")
    assert console.kicked == []
    assert "must supply a reason" in _last(console)

    await proc.handle(_boss(), "!kick Bob")  # a superadmin is exempt
    assert console.kicked != []


@pytest.mark.asyncio
async def test_admins_level_is_configurable(console):
    config = {**ADMIN_CONFIG, "settings": {"admins_level": 60}}
    _, proc = _setup(console, config)
    console.clients.add(_client("Mod", "mod", cid="1", id_=2))  # level 20
    console.clients.add(_client("Full", "fulladmin", cid="2", id_=3))  # level 60

    await proc.handle(_boss(), "!admins")

    reply = _last(console)
    assert "Full" in reply and "Mod" not in reply


@pytest.mark.asyncio
async def test_registration_announcement_can_be_turned_off(console):
    config = {**ADMIN_CONFIG, "settings": {"announce_registration": False}}
    _, proc = _setup(console, config)

    await proc.handle(_client("Newbie", "guest", cid="4", id_=7), "!register")

    assert console.said == []
    assert "you are now a member" in _last(console)


# -- !plugin: runtime enable/disable ---------------------------------------------------------
#
# The loader has always treated "disabled" as inert-but-loaded, precisely so a plugin can be turned
# on later; this is the in-game way to say so. It is level 100 because enabling a plugin runs
# third-party code with full database and RCON access.


class _Spare(Plugin):
    """A second plugin to switch on and off."""

    def __init__(self, console, config=None) -> None:  # noqa: ANN001
        super().__init__(console, config)
        self.enables = 0
        self.disables = 0

    def on_enable(self) -> None:
        self.enables += 1

    def on_disable(self) -> None:
        self.disables += 1

    @command(level=0)
    def cmd_spare(self, ctx) -> None:  # noqa: ANN001
        """spare - test command"""
        ctx.reply("spare")


def _with_spare(console, *, disabled=True):  # noqa: ANN001, ANN202
    plugin, proc = _setup(console)
    spare = _Spare(console)
    if disabled:
        spare.mark_disabled("disabled in the config")
    else:
        spare.start()
    console.plugins = {"admin": plugin, "spare": spare}
    return plugin, proc, spare


@pytest.mark.asyncio
async def test_plugin_list_shows_which_are_off(console):
    _plugin, proc, _spare = _with_spare(console)
    await proc.handle(_boss(), "!plugin list")
    assert _last(console) == "plugins: admin, spare (off)"


@pytest.mark.asyncio
async def test_plugin_list_is_the_default_action(console):
    _plugin, proc, _spare = _with_spare(console)
    await proc.handle(_boss(), "!plugin")
    assert "plugins:" in _last(console)


@pytest.mark.asyncio
async def test_enabling_a_plugin_runs_its_deferred_startup(console):
    """The payoff: a plugin disabled in the config becomes usable without restarting the bot."""
    _plugin, proc, spare = _with_spare(console)
    assert console.command_registry.get("spare") is None  # inert: no commands registered

    await proc.handle(_boss(), "!plugin enable spare")

    assert _last(console) == "plugin 'spare' enabled"
    assert spare.is_enabled() and spare.is_started() and spare.enables == 1
    assert console.command_registry.get("spare") is not None


@pytest.mark.asyncio
async def test_disabling_a_plugin_silences_it(console):
    _plugin, proc, spare = _with_spare(console, disabled=False)
    await proc.handle(_boss(), "!plugin disable spare")

    assert _last(console) == "plugin 'spare' disabled"
    assert not spare.is_enabled() and spare.disables == 1
    # Its command is still registered but no longer available, which is what hides it from !help.
    assert console.command_registry.get("spare").is_available() is False


@pytest.mark.asyncio
async def test_the_admin_plugin_cannot_disable_itself(console):
    """Otherwise `!plugin` switches off the only way back on, and every penalty command with it."""
    plugin, proc, _spare = _with_spare(console)
    await proc.handle(_boss(), "!plugin disable admin")

    assert "cannot be disabled" in _last(console)
    assert plugin.is_enabled()


@pytest.mark.asyncio
async def test_enabling_something_already_on_says_so(console):
    _plugin, proc, _spare = _with_spare(console, disabled=False)
    await proc.handle(_boss(), "!plugin enable spare")
    assert _last(console) == "plugin 'spare' is already enabled"


@pytest.mark.asyncio
async def test_an_unknown_plugin_name(console):
    _plugin, proc, _spare = _with_spare(console)
    await proc.handle(_boss(), "!plugin enable nosuch")
    assert _last(console) == "no plugin called 'nosuch' is loaded"


@pytest.mark.asyncio
async def test_plugin_info_reports_state_and_why(console):
    _plugin, proc, _spare = _with_spare(console)
    await proc.handle(_boss(), "!plugin info spare")
    assert _last(console) == "spare: disabled (disabled in the config), 0 command(s)"


@pytest.mark.asyncio
async def test_plugin_info_counts_a_running_plugins_commands(console):
    _plugin, proc, _spare = _with_spare(console, disabled=False)
    await proc.handle(_boss(), "!plugin info spare")
    assert _last(console) == "spare: enabled, 1 command(s)"


@pytest.mark.asyncio
async def test_an_ordinary_admin_cannot_touch_plugins(console):
    _plugin, proc, spare = _with_spare(console)
    await proc.handle(_client("Mod", "senioradmin", cid="2", id_=2), "!plugin enable spare")
    assert not spare.is_enabled()


# -- !punkbuster: the command three plugins each had their own copy of -------------------------


@pytest.mark.asyncio
async def test_a_line_for_punkbuster_is_handed_over_and_its_answer_shown(console):
    """The classic's copies of this — one per Frostbite plugin — threw PunkBuster's reply away, so an
    admin could not tell a command it had run from one it had not understood."""
    _plugin, proc = _setup(console)
    console.punkbuster_replies["pb_sv_plist"] = "Player List: [Slot #] ..."

    await proc.handle(_boss(), "!punkbuster pb_sv_plist")

    assert console.punkbuster_sent == ["pb_sv_plist"]
    assert "Player List" in _last(console)


@pytest.mark.asyncio
async def test_punkbuster_on_a_server_without_it_says_so(console):
    """Rather than sending a line into the dark, which is what a plugin holding the verb's spelling
    itself could only do."""
    _plugin, proc = _setup(console)
    console.punkbuster = False

    await proc.handle(_boss(), "!punkbuster pb_sv_plist")

    assert console.punkbuster_sent == []
    assert "not running PunkBuster" in _last(console)


@pytest.mark.asyncio
async def test_punkbuster_with_nothing_to_send_says_how_to_use_it(console):
    _plugin, proc = _setup(console)

    await proc.handle(_boss(), "!punkbuster")

    assert console.punkbuster_sent == []
    assert "punkbuster <command>" in _last(console)


@pytest.mark.asyncio
async def test_only_a_superadmin_may_talk_to_punkbuster(console):
    """`PB_SV_*` includes the ban list and the anti-cheat's own settings."""
    _plugin, proc = _setup(console)

    await proc.handle(_client("Senior", "senioradmin", cid="2", id_=2), "!punkbuster pb_sv_plist")

    assert console.punkbuster_sent == []
