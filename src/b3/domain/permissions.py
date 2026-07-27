"""Group / permission model.

This preserves a subtle but load-bearing piece of B3 domain knowledge exactly:

* A group's ``id`` is a **power-of-two membership bit**.
* A client's ``group_bits`` is the **bitwise OR** of the bits of every group it belongs to.
* A group's ``level`` (0-100) is the **permission ordinal** used by the command system.

So a client in ``admin`` (bit 16) and ``mod`` (bit 8) has ``group_bits == 24``, and its
effective level is the max level among its groups. Both meanings of the group PK are kept.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Group:
    id: int  # membership bit (power of two); 0 for guest
    keyword: str  # 'superadmin', 'admin', 'mod', ...
    name: str  # human-readable label
    level: int  # 0-100 permission ordinal


# The canonical seed groups, reproduced verbatim from the legacy b3.sql files.
# Order matters only for readability; lookups are by keyword / bit / level.
DEFAULT_GROUPS: tuple[Group, ...] = (
    Group(128, "superadmin", "Super Admin", 100),
    Group(64, "senioradmin", "Senior Admin", 80),
    Group(32, "fulladmin", "Full Admin", 60),
    Group(16, "admin", "Admin", 40),
    Group(8, "mod", "Moderator", 20),
    Group(2, "reg", "Regular", 2),
    Group(1, "user", "User", 1),
    Group(0, "guest", "Guest", 0),
)

_BY_KEYWORD = {g.keyword: g for g in DEFAULT_GROUPS}
_BY_LEVEL = {g.level: g for g in DEFAULT_GROUPS}


def group_by_keyword(keyword: str) -> Group | None:
    return _BY_KEYWORD.get(keyword)


def group_by_level(level: int) -> Group | None:
    return _BY_LEVEL.get(level)


def find_group(keyword: str, groups: tuple[Group, ...] = DEFAULT_GROUPS) -> Group | None:
    """Look a group up by keyword, case-insensitively, in a specific group set.

    Takes the set explicitly because an operator may have edited the ``groups`` table; the
    admin commands pass what storage returns rather than assuming the seeded defaults.
    """
    needle = keyword.strip().lower()
    return next((g for g in groups if g.keyword.lower() == needle), None)


def groups_from_bits(group_bits: int, groups: tuple[Group, ...] = DEFAULT_GROUPS) -> list[Group]:
    """Return the groups whose membership bit is set in ``group_bits``.

    The guest group (bit 0) is only returned when no other membership bit is set.
    """
    matched = [g for g in groups if g.id != 0 and (group_bits & g.id)]
    if matched:
        return matched
    guest = next((g for g in groups if g.id == 0), None)
    return [guest] if guest else []


def bits_from_groups(groups: list[Group]) -> int:
    """Bitwise-OR the membership bits of ``groups``."""
    bits = 0
    for g in groups:
        bits |= g.id
    return bits


def max_level(group_bits: int, groups: tuple[Group, ...] = DEFAULT_GROUPS) -> int:
    """The effective permission level for a client with the given ``group_bits``."""
    return max((g.level for g in groups_from_bits(group_bits, groups)), default=0)


def max_group(group_bits: int, groups: tuple[Group, ...] = DEFAULT_GROUPS) -> Group | None:
    """The highest-level group a client belongs to — what `!leveltest` names."""
    matched = groups_from_bits(group_bits, groups)
    return max(matched, key=lambda g: g.level) if matched else None
