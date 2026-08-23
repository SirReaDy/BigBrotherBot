"""Group bitmask + level model — the load-bearing domain rule."""

from __future__ import annotations

from b3.domain.permissions import (
    DEFAULT_GROUPS,
    bits_from_groups,
    group_by_keyword,
    groups_from_bits,
    level_for,
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


# -- what an operator wrote, as a level ----------------------------------------------------------


def test_a_level_comes_from_a_keyword_or_a_number():
    assert level_for("mod") == 20
    assert level_for("SeniorAdmin") == 80
    assert level_for(" admin ") == 40
    assert level_for("40") == 40
    assert level_for(40) == 40


def test_anything_else_is_none_so_the_caller_can_name_it():
    """Four plugins read a level out of config and each has to say which word it did not know."""
    assert level_for("moderator") is None
    assert level_for("101") is None
    assert level_for(101) is None
    assert level_for("") is None


def test_a_bool_is_refused_rather_than_counted_as_a_level():
    """YAML reads a bare `yes`/`no` as one, and neither was ever meant as "superadmin" or "guest"."""
    assert level_for(True) is None
    assert level_for(False) is None
