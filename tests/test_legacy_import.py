"""Legacy DB import: id preservation, orphan handling, domain-value fidelity."""

from __future__ import annotations

import sqlite3

import pytest

from b3.core.clock import FakeClock
from b3.domain.client import PenaltyType
from b3.legacy import import_legacy_database
from b3.storage.store import SqlAlchemyStorage

LEGACY_SCHEMA = """
CREATE TABLE clients(id INTEGER PRIMARY KEY, ip TEXT, connections INT, guid TEXT, pbid TEXT,
    name TEXT, auto_login INT, mask_level INT, group_bits INT, greeting TEXT,
    time_add INT, time_edit INT, password TEXT, login TEXT);
CREATE TABLE groups(id INTEGER PRIMARY KEY, name TEXT, keyword TEXT, level INT,
    time_add INT, time_edit INT);
CREATE TABLE penalties(id INTEGER PRIMARY KEY, type TEXT, client_id INT, admin_id INT,
    duration INT, inactive INT, keyword TEXT, reason TEXT, data TEXT,
    time_add INT, time_edit INT, time_expire INT);
CREATE TABLE aliases(id INTEGER PRIMARY KEY, num_used INT, alias TEXT, client_id INT,
    time_add INT, time_edit INT);
CREATE TABLE ipaliases(id INTEGER PRIMARY KEY, num_used INT, ip TEXT, client_id INT,
    time_add INT, time_edit INT);
CREATE TABLE data(data_key TEXT PRIMARY KEY, data_value TEXT);
"""


def _make_legacy_db(path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO clients VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                1,
                "192.0.2.4",
                5,
                "GUIDADMIN",
                "PBADMIN",
                "Admin",
                0,
                0,
                128,
                "",
                1000,
                1000,
                None,
                None,
            ),
            (2, "198.51.100.8", 3, "GUIDBOB", "", "Bob", 0, 0, 0, "", 1001, 1001, None, None),
        ],
    )
    conn.execute("INSERT INTO groups VALUES (128,'Super Admin','superadmin',100,0,0)")
    conn.executemany(
        "INSERT INTO penalties VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "Ban", 1, 2, 0, 0, "", "banned by bob", "", 1000, 1000, -1),  # valid
            (
                2,
                "Ban",
                2,
                0,
                0,
                0,
                "",
                "system ban",
                "",
                1001,
                1001,
                -1,
            ),  # admin 0 -> null (silent)
            (
                3,
                "Ban",
                99,
                1,
                0,
                0,
                "",
                "orphan client",
                "",
                1002,
                1002,
                -1,
            ),  # orphan client -> skip
            (
                4,
                "Ban",
                2,
                77,
                0,
                0,
                "",
                "dangling admin",
                "",
                1003,
                1003,
                -1,
            ),  # admin 77 gone -> null
        ],
    )
    conn.executemany(
        "INSERT INTO aliases VALUES (?,?,?,?,?,?)",
        [
            (1, 3, "Admin", 1, 1000, 1000),  # valid
            (2, 1, "Ghost", 99, 1002, 1002),  # orphan -> skip
        ],
    )
    conn.execute("INSERT INTO ipaliases VALUES (1,2,'198.51.100.8',2,1001,1001)")
    conn.executemany(
        "INSERT INTO data VALUES (?,?)",
        [("foo", "bar"), ("schema_version", "1.10.1")],
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


@pytest.fixture()
def target(tmp_path):
    store = SqlAlchemyStorage(f"sqlite:///{tmp_path / 'new.sqlite'}", clock=FakeClock())
    yield store
    store.close()


def test_import_counts_and_orphan_handling(tmp_path, target):
    source_url = _make_legacy_db(tmp_path / "legacy.sqlite")

    report = import_legacy_database(source_url, target)

    assert report.clients == 2
    assert report.penalties == 3  # ids 1, 2, 4 imported
    assert report.penalties_skipped_orphan == 1  # id 3 (client 99 doesn't exist)
    assert (
        report.penalties_admin_nulled == 1
    )  # id 4 (admin 77 doesn't exist); id 2's admin 0 is silent
    assert report.aliases == 1
    assert report.aliases_skipped_orphan == 1  # alias for client 99
    assert report.ipaliases == 1
    assert report.data == 1  # 'foo' only; schema_version skipped


def test_import_preserves_domain_values(tmp_path, target):
    source_url = _make_legacy_db(tmp_path / "legacy.sqlite")
    import_legacy_database(source_url, target)

    # Client ids preserved, group bitmask intact -> superadmin recognized.
    admin = target.get_client_by_id(1)
    assert admin is not None
    assert admin.name == "Admin"
    assert admin.group_bits == 128
    assert target.has_superadmin() is True

    bob = target.get_client_by_guid("GUIDBOB")
    assert bob is not None and bob.id == 2

    # The valid penalty keeps its admin; the system penalty got its admin nulled.
    admin_pens = target.get_active_penalties(1, PenaltyType.BAN)
    assert len(admin_pens) == 1 and admin_pens[0].admin_id == 2

    # Bob has two bans (system + dangling-admin); both had their admin reference nulled.
    bob_pens = target.get_active_penalties(2, PenaltyType.BAN)
    assert len(bob_pens) == 2
    assert all(p.admin_id is None for p in bob_pens)

    # Aliases / ip-aliases carried over with their references.
    assert [a.value for a in target.get_aliases(1)] == ["Admin"]
    assert [a.value for a in target.get_ip_aliases(2)] == ["198.51.100.8"]
