"""Group management (PARITY P2): putgroup/ungroup, regulars, register, leveltest, masking —
and the "you may not act on your equals and betters" rule the group commands share with kick/ban.

Driven through the core command processor, like the rest of the admin-plugin tests, so the levels
declared on each command are exercised too.
"""

from __future__ import annotations

import pytest

from b3.core.commands import CommandProcessor
from b3.domain.client import Client
from b3.domain.permissions import group_by_keyword
from b3.plugins.admin import AdminPlugin

BITS = {"guest": 0, "user": 1, "reg": 2, "mod": 8, "admin": 16, "senioradmin": 64, "superadmin": 128}


def _setup(console):
    plugin = AdminPlugin(console)
    plugin.register_commands()
    return plugin, CommandProcessor(console.command_registry, console)


def _client(name: str, keyword: str = "guest", *, cid: str = "1", id_: int | None = 1) -> Client:
    return Client(guid=name[0].upper(), name=name, cid=cid, id=id_, group_bits=BITS[keyword])


def _last_told(console) -> str:
    return console.told[-1][1]


# -- putgroup / ungroup --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_putgroup_sets_the_group_and_persists_it(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Boss", "superadmin", cid="1", id_=1), "!putgroup Bob mod")

    assert bob.group_bits == BITS["mod"]
    assert bob.max_level() == 20
    assert console.storage.saved[-1] is bob
    assert "put in group Moderator" in _last_told(console)


@pytest.mark.asyncio
async def test_putgroup_replaces_rather_than_accumulates(console):
    """`!putgroup bob user` on an admin must demote him, not silently do nothing."""
    _, proc = _setup(console)
    bob = _client("Bob", "admin", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Boss", "superadmin"), "!putgroup Bob user")

    assert bob.group_bits == BITS["user"]
    assert bob.max_level() == 1


@pytest.mark.asyncio
async def test_putgroup_cannot_reach_your_own_level(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    # A senioradmin (80) may not create another senioradmin.
    await proc.handle(_client("Senior", "senioradmin"), "!putgroup Bob senioradmin")

    assert bob.group_bits == BITS["user"]
    assert "beyond your reach" in _last_told(console)


@pytest.mark.asyncio
async def test_superadmin_may_reach_any_group(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Boss", "superadmin"), "!putgroup Bob senioradmin")
    assert bob.group_bits == BITS["senioradmin"]


@pytest.mark.asyncio
async def test_putgroup_cannot_demote_someone_at_or_above_your_level(console):
    """The privilege hole the classic bot left open: putgroup ignored the *target's* level."""
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="4", id_=7)
    console.register_client("Boss", boss)

    await proc.handle(_client("Senior", "senioradmin", cid="1", id_=1), "!putgroup Boss user")

    assert boss.group_bits == BITS["superadmin"]
    assert "at or above your level" in _last_told(console)


@pytest.mark.asyncio
async def test_putgroup_unknown_group_lists_the_known_ones(console):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))

    await proc.handle(_client("Boss", "superadmin"), "!putgroup Bob wizard")

    reply = _last_told(console)
    assert "no such group: 'wizard'" in reply
    assert "senioradmin" in reply  # the reply tells you what you could have typed


@pytest.mark.asyncio
async def test_putgroup_already_in_group(console):
    _, proc = _setup(console)
    bob = _client("Bob", "mod", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Boss", "superadmin"), "!putgroup Bob mod")
    assert "already in group Moderator" in _last_told(console)


@pytest.mark.asyncio
async def test_putgroup_needs_both_arguments(console):
    _, proc = _setup(console)
    console.register_client("Bob", _client("Bob", "user", cid="4", id_=7))
    await proc.handle(_client("Boss", "superadmin"), "!putgroup Bob")
    assert "usage: putgroup <player> <group>" in _last_told(console)


@pytest.mark.asyncio
async def test_ungroup_removes_only_that_group(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    bob.add_group(group_by_keyword("mod"))  # user + mod
    console.register_client("Bob", bob)

    await proc.handle(_client("Boss", "superadmin"), "!ungroup Bob mod")

    assert bob.group_bits == BITS["user"]
    assert "removed from group Moderator" in _last_told(console)


@pytest.mark.asyncio
async def test_ungroup_when_not_in_the_group(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Boss", "superadmin"), "!ungroup Bob mod")
    assert bob.group_bits == BITS["user"]
    assert "is not in group Moderator" in _last_told(console)


@pytest.mark.asyncio
async def test_putgroup_works_on_a_player_who_has_left(console):
    """Promoting someone who just disconnected is the common case; @id reaches them."""
    _, proc = _setup(console)
    offline = _client("Gone", "user", cid=None, id_=42)
    console.register_lookup("@42", [offline])

    await proc.handle(_client("Boss", "superadmin"), "!putgroup @42 admin")

    assert offline.group_bits == BITS["admin"]
    assert console.storage.saved[-1] is offline


# -- makereg / unreg / regulars / register --------------------------------------------------


@pytest.mark.asyncio
async def test_makereg_and_unreg(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    senior = _client("Senior", "senioradmin", cid="1", id_=1)

    await proc.handle(senior, "!makereg Bob")
    assert bob.group_bits == BITS["reg"]

    await proc.handle(senior, "!unreg Bob")
    assert bob.group_bits == BITS["user"]  # demoted to plain user, not stripped to nothing
    assert "removed from group Regular" in _last_told(console)


@pytest.mark.asyncio
async def test_makereg_refuses_to_demote_a_higher_group(console):
    _, proc = _setup(console)
    bob = _client("Bob", "mod", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Senior", "senioradmin"), "!makereg Bob")

    assert bob.group_bits == BITS["mod"]
    assert "higher-level group" in _last_told(console)


@pytest.mark.asyncio
async def test_unreg_when_not_a_regular(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Senior", "senioradmin"), "!unreg Bob")
    assert bob.group_bits == BITS["user"]
    assert "is not in group Regular" in _last_told(console)


@pytest.mark.asyncio
async def test_makereg_has_the_mr_alias(console):
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)
    await proc.handle(_client("Senior", "senioradmin"), "!mr Bob")
    assert bob.group_bits == BITS["reg"]


@pytest.mark.asyncio
async def test_regulars_lists_only_regulars(console):
    _, proc = _setup(console)
    for cid, (name, keyword) in enumerate(
        [("Reg1", "reg"), ("Reg2", "reg"), ("Newbie", "user"), ("Boss", "superadmin")]
    ):
        console.clients.add(_client(name, keyword, cid=str(cid), id_=cid + 1))

    await proc.handle(_client("Reg1", "reg", cid="0"), "!regulars")

    reply = _last_told(console)
    assert "Reg1" in reply and "Reg2" in reply
    assert "Newbie" not in reply and "Boss" not in reply


@pytest.mark.asyncio
async def test_regulars_none_connected(console):
    _, proc = _setup(console)
    await proc.handle(_client("Boss", "superadmin"), "!regulars")
    assert "no regular players" in _last_told(console)


@pytest.mark.asyncio
async def test_register_self_promotes_a_guest_and_announces(console):
    _, proc = _setup(console)
    newbie = _client("Newbie", "guest", cid="4", id_=7)

    await proc.handle(newbie, "!register")

    assert newbie.group_bits == BITS["user"]
    assert console.storage.saved[-1] is newbie
    assert "you are now a member of the group User" in _last_told(console)
    assert console.said == ["Newbie put in group User"]


@pytest.mark.asyncio
async def test_register_is_a_no_op_for_someone_already_ranked(console):
    _, proc = _setup(console)
    mod = _client("Mod", "mod", cid="4", id_=7)

    await proc.handle(mod, "!register")

    assert mod.group_bits == BITS["mod"]
    assert "already in a higher-level group" in _last_told(console)
    assert console.said == []


# -- leveltest / regtest / admintest ---------------------------------------------------------


@pytest.mark.asyncio
async def test_leveltest_on_yourself_shows_your_real_group(console):
    _, proc = _setup(console)
    await proc.handle(_client("Mod", "mod", id_=3), "!leveltest")
    assert _last_told(console) == "Mod [@3] is Moderator [20]"


@pytest.mark.asyncio
async def test_leveltest_on_another_player(console):
    _, proc = _setup(console)
    bob = _client("Bob", "reg", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Mod", "mod"), "!lt Bob")
    assert _last_told(console) == "Bob [@7] is Regular [2]"


@pytest.mark.asyncio
async def test_leveltest_on_an_ungrouped_player(console):
    _, proc = _setup(console)
    bob = _client("Bob", "guest", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Mod", "mod"), "!leveltest Bob")
    assert "not in any group" in _last_told(console)


@pytest.mark.asyncio
async def test_regtest_and_admintest_report_your_own_standing(console):
    _, proc = _setup(console)
    admin = _client("Boss", "admin", id_=5)

    await proc.handle(admin, "!regtest")
    assert _last_told(console) == "Boss [@5] is Admin [40]"

    await proc.handle(admin, "!admintest")
    assert _last_told(console) == "Boss [@5] is Admin [40]"


@pytest.mark.asyncio
async def test_admintest_is_out_of_reach_for_a_regular(console):
    _, proc = _setup(console)
    await proc.handle(_client("Reg", "reg"), "!admintest")
    assert "sufficient access" in _last_told(console)


# -- masking -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mask_hides_your_level_without_changing_your_powers(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="1", id_=1)

    await proc.handle(boss, "!mask user")

    assert boss.mask_level == 1
    assert boss.max_level() == 100  # still a superadmin where it counts
    assert boss.display_level() == 1
    assert console.storage.saved[-1] is boss
    assert console.told[-1] == (boss, "masked as User")


@pytest.mark.asyncio
async def test_masked_admin_disappears_from_the_admins_list(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="1", id_=1)
    mod = _client("Mod", "mod", cid="2", id_=2)
    console.clients.add(boss)
    console.clients.add(mod)

    await proc.handle(mod, "!admins")
    assert "Boss" in _last_told(console)

    await proc.handle(boss, "!mask user")
    await proc.handle(mod, "!admins")
    reply = _last_told(console)
    assert "Boss" not in reply
    assert "Mod [20]" in reply


@pytest.mark.asyncio
async def test_leveltest_of_a_masked_player_shows_the_mask(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="1", id_=1)
    boss.mask_level = 2
    console.register_client("Boss", boss)

    await proc.handle(_client("Mod", "mod", cid="2", id_=2), "!leveltest Boss")
    assert _last_told(console) == "Boss [@1] is Regular [2]"


@pytest.mark.asyncio
async def test_unmask_restores_the_real_level(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="1", id_=1)
    boss.mask_level = 1

    await proc.handle(boss, "!unmask")

    assert boss.mask_level == 0
    assert console.told[-1] == (boss, "un-masked")


@pytest.mark.asyncio
async def test_mask_another_player(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="1", id_=1)
    mod = _client("Mod", "mod", cid="2", id_=2)
    console.register_client("Mod", mod)

    await proc.handle(boss, "!mask reg Mod")

    assert mod.mask_level == 2
    assert console.told[-2] == (boss, "masked Mod as Regular")  # the admin is told
    assert console.told[-1] == (mod, "masked as Regular")  # and so is the player


@pytest.mark.asyncio
async def test_masking_needs_superadmin(console):
    _, proc = _setup(console)
    await proc.handle(_client("Senior", "senioradmin"), "!mask user")
    assert "sufficient access" in _last_told(console)


# -- the shared "equals and betters" guard ----------------------------------------------------


@pytest.mark.asyncio
async def test_you_cannot_kick_someone_at_your_own_level(console):
    _, proc = _setup(console)
    other = _client("Other", "admin", cid="4", id_=7)
    console.register_client("Other", other)

    await proc.handle(_client("Admin", "admin", cid="1", id_=1), "!kick Other")

    assert console.kicked == []
    assert "at or above your level" in _last_told(console)


@pytest.mark.asyncio
async def test_you_cannot_ban_someone_above_you(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="4", id_=7)
    console.register_client("Boss", boss)

    await proc.handle(_client("Admin", "admin", cid="1", id_=1), "!ban Boss")

    assert console.banned == []


@pytest.mark.asyncio
async def test_a_masked_target_gets_the_vaguer_refusal(console):
    """The refusal must not read like 'you just tried to kick a superadmin'."""
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="4", id_=7)
    boss.mask_level = 1
    console.register_client("Boss", boss)

    await proc.handle(_client("Admin", "admin", cid="1", id_=1), "!kick Boss")

    assert console.kicked == []
    assert "masked higher-level player" in _last_told(console)


@pytest.mark.asyncio
async def test_you_cannot_ban_yourself(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="1", id_=1)
    console.register_client("Boss", boss)

    await proc.handle(boss, "!ban Boss")

    assert console.banned == []
    assert "cannot do that to yourself" in _last_told(console)


@pytest.mark.asyncio
async def test_warn_and_tempban_are_guarded_too(console):
    _, proc = _setup(console)
    boss = _client("Boss", "superadmin", cid="4", id_=7)
    console.register_client("Boss", boss)
    admin = _client("Admin", "admin", cid="1", id_=1)

    await proc.handle(admin, "!warn Boss")
    await proc.handle(admin, "!tempban Boss 1h")

    assert console.warned == []
    assert console.tempbanned == []


# -- custom group tables ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_keywords_come_from_storage_not_the_defaults(console):
    """An operator who renamed a group in the database gets what they configured."""
    from b3.domain.permissions import Group

    console.storage.groups = [*console.storage.groups, Group(4, "vip", "VIP Player", 10)]
    _, proc = _setup(console)
    bob = _client("Bob", "user", cid="4", id_=7)
    console.register_client("Bob", bob)

    await proc.handle(_client("Boss", "superadmin"), "!putgroup Bob vip")

    assert bob.group_bits == 4
    assert "put in group VIP Player" in _last_told(console)
