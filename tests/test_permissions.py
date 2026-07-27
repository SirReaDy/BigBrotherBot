"""Group bitmask + level model — the load-bearing domain rule."""

from __future__ import annotations

from b3.domain.permissions import (
    DEFAULT_GROUPS,
    bits_from_groups,
    group_by_keyword,
    groups_from_bits,
    max_level,
)


def test_seed_groups_present():
    assert len(DEFAULT_GROUPS) == 8
    assert group_by_keyword("superadmin").level == 100
    assert group_by_keyword("guest").id == 0


def test_group_bits_is_bitwise_or():
    admin = group_by_keyword("admin")  # bit 16
    mod = group_by_keyword("mod")  # bit 8
    assert bits_from_groups([admin, mod]) == 24


def test_groups_from_bits_multi_membership():
    groups = groups_from_bits(24)  # admin + mod
    keywords = {g.keyword for g in groups}
    assert keywords == {"admin", "mod"}


def test_max_level_is_highest_group():
    assert max_level(24) == 40  # admin(40) beats mod(20)
    assert max_level(128) == 100  # superadmin


def test_zero_bits_is_guest():
    groups = groups_from_bits(0)
    assert len(groups) == 1
    assert groups[0].keyword == "guest"
    assert max_level(0) == 0
