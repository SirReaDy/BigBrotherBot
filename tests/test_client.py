"""Client + Penalty domain behaviour."""

from __future__ import annotations

from b3.domain.client import NEVER_EXPIRES, Client, Penalty, PenaltyType
from b3.domain.permissions import group_by_keyword


def test_client_max_level_from_group_bits():
    c = Client(guid="abc", group_bits=24)  # admin + mod
    assert c.max_level() == 40


def test_client_group_membership():
    c = Client(guid="abc")
    admin = group_by_keyword("admin")
    assert not c.in_group(admin)
    c.add_group(admin)
    assert c.in_group(admin)
    assert c.group_bits == admin.id
    c.remove_group(admin)
    assert not c.in_group(admin)


def test_client_guest_when_no_bits():
    c = Client(guid="abc", group_bits=0)
    assert c.in_group(group_by_keyword("guest"))
    assert c.max_level() == 0


def test_penalty_permanent_is_active():
    p = Penalty(type=PenaltyType.BAN, client_id=1, time_expire=NEVER_EXPIRES)
    assert p.is_active(now_epoch=9_999_999_999)


def test_penalty_tempban_expiry():
    p = Penalty(type=PenaltyType.TEMPBAN, client_id=1, time_expire=1_000)
    assert p.is_active(now_epoch=500)
    assert not p.is_active(now_epoch=2_000)


def test_penalty_inactive_is_not_active():
    p = Penalty(type=PenaltyType.BAN, client_id=1, inactive=True, time_expire=NEVER_EXPIRES)
    assert not p.is_active(now_epoch=0)


def test_plugin_scoped_vars():
    c = Client(guid="abc")
    plugin_a = object()
    plugin_b = object()
    c.set_var(plugin_a, "streak", 5)
    c.set_var(plugin_b, "streak", 99)
    assert c.get_var(plugin_a, "streak") == 5
    assert c.get_var(plugin_b, "streak") == 99
    assert c.get_var(plugin_a, "missing", default=0) == 0
    c.del_var(plugin_a, "streak")
    assert c.get_var(plugin_a, "streak") is None
