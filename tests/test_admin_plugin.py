"""Admin plugin commands, driven through the core command processor."""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.domain.client import NEVER_EXPIRES, Alias, Client, IpAlias, Penalty, PenaltyType
from b3.plugins.admin import AdminPlugin


def _setup(console, *, superadmin_exists=False):
    console.storage._superadmin = superadmin_exists
    plugin = AdminPlugin(console)
    plugin.register_commands()
    proc = CommandProcessor(console.command_registry, console)
    return plugin, proc


def _admin() -> Client:
    return Client(guid="A", name="Admin", group_bits=64)  # senioradmin, level 80


def _admin_level(level: int) -> Client:
    """A client whose effective level is exactly `level` (16=admin/40, 8=mod/20)."""
    bits = {20: 8, 40: 16, 60: 32, 80: 64, 100: 128}[level]
    return Client(guid="L", name="Lvl", group_bits=bits)


@pytest.mark.asyncio
async def test_kick(console):
    _, proc = _setup(console)
    target = Client(guid="T", name="Bob", cid="4")
    console.register_client("Bob", target)

    issuer = _admin()
    await proc.handle(issuer, "!kick Bob spawn killing")
    assert console.kicked == [(target, "spawn killing", issuer)]


@pytest.mark.asyncio
async def test_ban_and_tempban(console):
    _, proc = _setup(console)
    target = Client(guid="T", name="Bob", cid="4")
    console.register_client("Bob", target)

    await proc.handle(_admin(), "!ban Bob cheating")
    assert console.banned[-1][0] is target
    assert console.banned[-1][1] == "cheating"

    await proc.handle(_admin(), "!tempban Bob 2h aimbot")
    client, minutes, reason, _admin_c = console.tempbanned[-1]
    assert client is target
    assert minutes == 120  # 2h -> 120 min
    assert reason == "aimbot"


@pytest.mark.asyncio
async def test_tempban_bad_duration(console):
    _, proc = _setup(console)
    console.register_client("Bob", Client(guid="T", name="Bob", cid="4"))
    await proc.handle(_admin(), "!tempban Bob soon")
    assert console.tempbanned == []
    assert "invalid duration" in console.told[-1][1]


@pytest.mark.asyncio
async def test_kick_unknown_player(console):
    _, proc = _setup(console)
    await proc.handle(_admin(), "!kick Ghost")
    assert console.kicked == []
    assert "no player found" in console.told[-1][1]


@pytest.mark.asyncio
async def test_permission_denied_for_low_level(console):
    _, proc = _setup(console)
    console.register_client("Bob", Client(guid="T", name="Bob", cid="4"))
    guest = Client(guid="G", name="Guest", group_bits=0)  # level 0 < 40 needed for ban
    await proc.handle(guest, "!ban Bob")
    assert console.banned == []
    assert "sufficient access" in console.told[-1][1]


@pytest.mark.asyncio
async def test_alias_kick(console):
    _, proc = _setup(console)
    target = Client(guid="T", name="Bob", cid="4")
    console.register_client("Bob", target)
    await proc.handle(_admin(), "!k Bob spam")  # alias for kick
    assert console.kicked and console.kicked[-1][0] is target


@pytest.mark.asyncio
async def test_iamgod_bootstrap(console):
    _, proc = _setup(console, superadmin_exists=False)
    issuer = Client(guid="First", name="First")  # level 0
    await proc.handle(issuer, "!iamgod")
    assert issuer.max_level() == 100  # now superadmin
    assert issuer in console.storage.saved
    assert "now superadmin" in console.told[-1][1]


@pytest.mark.asyncio
async def test_iamgod_disabled_when_superadmin_exists(console):
    _, proc = _setup(console, superadmin_exists=True)
    issuer = Client(guid="Late", name="Late")
    await proc.handle(issuer, "!iamgod")
    assert issuer.max_level() == 0  # unchanged
    assert "already a superadmin" in console.told[-1][1]


@pytest.mark.asyncio
async def test_help_lists_one_group_and_says_where_the_rest_are(console):
    """Sixty command names is several wrapped lines in a chat window that holds four."""
    _, proc = _setup(console)
    await proc.handle(_admin(), "!help")
    listing = console.told[-2][1]
    tail = console.told[-1][1]

    assert "iamgod" in listing, "guest commands are what !help on its own answers with"
    assert "kick" not in listing, "and an admin command is not one of them"
    assert "!help admin" in tail, "the tail is how you find the rest"


@pytest.mark.asyncio
async def test_help_for_a_group_lists_what_that_group_adds(console):
    _, proc = _setup(console)
    await proc.handle(_admin(), "!help admin")
    listing = console.told[-2][1]
    assert "kick" in listing and "ban" in listing
    assert "iamgod" not in listing, "a guest command is not listed again under admin"


@pytest.mark.asyncio
async def test_help_never_lists_a_command_the_caller_cannot_run(console):
    """The list is drawn from `usable_by`, so asking about a group above you shows nothing of it."""
    _, proc = _setup(console)
    guest = Client(guid="G", name="Guest")
    await proc.handle(guest, "!help superadmin")
    assert "no superadmin commands" in console.told[-1][1]


@pytest.mark.asyncio
async def test_help_for_one_command_answers_with_its_usage(console):
    _, proc = _setup(console)
    await proc.handle(_admin(), "!help kick")
    assert "kick <player>" in console.told[-1][1]


@pytest.mark.asyncio
async def test_help_for_a_word_that_is_neither_says_so_and_points_somewhere(console):
    _, proc = _setup(console)
    await proc.handle(_admin(), "!help moderator")
    reply = console.told[-1][1]
    assert "moderator" in reply and "mod" in reply


# -- penalty inspection / lifting ------------------------------------------


def _superadmin() -> Client:
    return Client(guid="S", name="Boss", group_bits=128, id=1)  # level 100


def _banned_bob(console, *, type_=PenaltyType.BAN, expire=NEVER_EXPIRES, reason="cheating"):
    bob = Client(guid="T", name="Bob", cid="4", id=7)
    console.register_client("Bob", bob)
    console.storage.clients_by_id[7] = bob
    console.storage.add_penalty(
        Penalty(type=type_, client_id=7, reason=reason, time_expire=expire, duration=60)
    )
    return bob


@pytest.mark.asyncio
async def test_unban_lifts_the_ban(console):
    _, proc = _setup(console)
    bob = _banned_bob(console)

    issuer = _superadmin()
    await proc.handle(issuer, "!unban Bob appealed")

    assert console.unbanned == [(bob, "appealed", issuer)]
    assert console.storage.get_active_penalties(7, PenaltyType.BAN) == []
    assert "unbanned" in console.told[-1][1]


@pytest.mark.asyncio
async def test_unban_reports_when_there_is_nothing_to_lift(console):
    _, proc = _setup(console)
    bob = Client(guid="T", name="Bob", cid="4", id=7)
    console.register_client("Bob", bob)

    await proc.handle(_superadmin(), "!unban Bob")
    assert console.unbanned == []
    assert "no active ban" in console.told[-1][1]


@pytest.mark.asyncio
async def test_unban_requires_full_admin(console):
    _, proc = _setup(console)
    _banned_bob(console)
    await proc.handle(_admin_level(40), "!unban Bob")  # admin, below fulladmin(60)
    assert console.unbanned == []
    assert "sufficient access" in console.told[-1][1]


@pytest.mark.asyncio
async def test_unban_of_an_offline_player_by_db_id(console):
    _, proc = _setup(console)
    bob = _banned_bob(console)
    console.register_lookup("@7", [bob])

    await proc.handle(_superadmin(), "!unban @7")
    assert console.unbanned and console.unbanned[-1][0] is bob


@pytest.mark.asyncio
async def test_ambiguous_target_asks_for_a_db_id(console):
    _, proc = _setup(console)
    one = Client(guid="A", name="Bobby", id=11)
    two = Client(guid="B", name="Bobcat", id=12)
    console.register_lookup("Bob", [one, two])

    await proc.handle(_superadmin(), "!unban Bob")
    reply = console.told[-1][1]
    assert "2 players match" in reply and "@11" in reply and "@12" in reply
    assert console.unbanned == []


@pytest.mark.asyncio
async def test_baninfo_describes_a_permanent_ban(console):
    _, proc = _setup(console)
    _banned_bob(console, reason="aimbot")
    await proc.handle(_admin(), "!baninfo Bob")
    assert "permanent ban" in console.told[-1][1]
    assert "aimbot" in console.told[-1][1]


@pytest.mark.asyncio
async def test_baninfo_describes_a_tempban(console):
    _, proc = _setup(console)
    _banned_bob(console, type_=PenaltyType.TEMPBAN, expire=9_999_999_999, reason="spam")
    await proc.handle(_admin(), "!bi Bob")
    reply = console.told[-1][1]
    assert "tempban" in reply and "1 hour" in reply  # not "60 min" — read it aloud


@pytest.mark.asyncio
async def test_baninfo_on_a_clean_player(console):
    _, proc = _setup(console)
    bob = Client(guid="T", name="Bob", cid="4", id=7)
    console.register_client("Bob", bob)
    await proc.handle(_admin(), "!baninfo Bob")
    assert "is not banned" in console.told[-1][1]


@pytest.mark.asyncio
async def test_warns_and_warnclear(console):
    _, proc = _setup(console)
    bob = Client(guid="T", name="Bob", cid="4", id=7)
    console.register_client("Bob", bob)
    for reason in ("language", "spam"):
        console.storage.add_penalty(Penalty(type=PenaltyType.WARNING, client_id=7, reason=reason))

    await proc.handle(_admin(), "!warns Bob")
    reply = console.told[-1][1]
    assert "2 warning(s)" in reply and "language" in reply and "spam" in reply

    await proc.handle(_superadmin(), "!wc Bob")
    assert "cleared 2 warning(s)" in console.told[-1][1]
    assert console.storage.get_active_penalties(7, PenaltyType.WARNING) == []


@pytest.mark.asyncio
async def test_warns_on_a_clean_player(console):
    _, proc = _setup(console)
    bob = Client(guid="T", name="Bob", cid="4", id=7)
    console.register_client("Bob", bob)
    await proc.handle(_admin(), "!warns Bob")
    assert "no active warnings" in console.told[-1][1]


# -- identity history ------------------------------------------------------


@pytest.mark.asyncio
async def test_aliases_lists_past_names_excluding_the_current_one(console):
    _, proc = _setup(console)
    bob = Client(guid="T", name="Bob", cid="4", id=7)
    console.register_client("Bob", bob)
    for name in ("Bob", "Robert", "Bobby"):
        console.storage.add_alias(Alias(value=name, client_id=7))

    await proc.handle(_admin(), "!aliases Bob")
    reply = console.told[-1][1]
    assert "Robert" in reply and "Bobby" in reply
    assert reply.count("Bob,") == 0  # the current name is not repeated as an alias


@pytest.mark.asyncio
async def test_aliases_when_there_are_none(console):
    _, proc = _setup(console)
    bob = Client(guid="T", name="Bob", cid="4", id=7)
    console.register_client("Bob", bob)
    console.storage.add_alias(Alias(value="Bob", client_id=7))

    await proc.handle(_admin(), "!alias Bob")
    assert "no known aliases" in console.told[-1][1]


@pytest.mark.asyncio
async def test_clientinfo_shows_identity_and_history(console):
    _, proc = _setup(console)
    bob = Client(guid="THEGUID", name="Bob", cid="4", id=7, ip="10.0.0.5", connections=3)
    console.register_client("Bob", bob)
    console.storage.add_alias(Alias(value="Robert", client_id=7))
    console.storage.add_ip_alias(IpAlias(value="10.0.0.5", client_id=7))

    await proc.handle(_superadmin(), "!clientinfo Bob")
    reply = console.told[-1][1]
    assert "@7" in reply and "THEGUID" in reply and "10.0.0.5" in reply
    assert "connections=3" in reply and "aliases=1" in reply and "ips=1" in reply


@pytest.mark.asyncio
async def test_admins_lists_connected_admins_by_level(console):
    _, proc = _setup(console)
    boss = Client(guid="S", name="Boss", cid="1", group_bits=128)  # 100
    mod = Client(guid="M", name="Mod", cid="2", group_bits=8)  # 20
    punter = Client(guid="P", name="Punter", cid="3")  # 0
    for c in (boss, mod, punter):
        console.clients.add(c)

    await proc.handle(_admin(), "!admins")
    reply = console.told[-1][1]
    assert reply.index("Boss") < reply.index("Mod")  # highest level first
    assert "Punter" not in reply


@pytest.mark.asyncio
async def test_admins_when_none_are_connected(console):
    _, proc = _setup(console)
    console.clients.add(Client(guid="P", name="Punter", cid="3"))
    await proc.handle(_admin(), "!admins")
    assert "no admins are currently connected" in console.told[-1][1]
