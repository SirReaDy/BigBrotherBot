"""Alembic: the migrations apply cleanly, in order, and stamp the version."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from b3.storage.migrate import current_revision, head_revision, upgrade

HEAD = "0003"


def test_migrations_build_the_full_schema(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"

    assert head_revision(url) == HEAD
    assert current_revision(url) is None  # nothing applied yet

    upgrade(url, "head")

    assert current_revision(url) == HEAD
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"clients", "groups", "penalties", "aliases", "ipaliases", "data"} <= tables
    assert "alembic_version" in tables


def test_upgrading_an_existing_database_adds_server_id(tmp_path):
    """The case this migration exists for: a bot that has been running keeps its rows.

    A penalty written before the column existed must survive the upgrade and read as "no server
    stated" — not vanish, and not block the upgrade.
    """
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    upgrade(url, "0001")

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO clients (guid, pbid, ip, name, connections, group_bits, mask_level,"
                " auto_login, greeting, time_add, time_edit)"
                " VALUES ('G', '', '', 'Bob', 1, 0, 0, 0, '', 0, 0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO penalties (type, client_id, duration, inactive, keyword, reason,"
                " data, time_add, time_edit, time_expire)"
                " VALUES ('Ban', 1, 0, 0, '', 'cheating', '', 100, 100, -1)"
            )
        )

    upgrade(url, "head")

    assert current_revision(url) == HEAD
    columns = {c["name"] for c in inspect(engine).get_columns("penalties")}
    assert "server_id" in columns
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT reason, server_id FROM penalties")).all()
    assert rows == [("cheating", "")]


def test_a_legacy_md5_password_survives_the_widening(tmp_path):
    """0003 widens `clients.password` so a modern hash fits. What must *not* happen is the existing
    digests being touched: an imported classic database authenticates on them until each owner logs in
    once, and `login` replaces them then."""
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    upgrade(url, "0002")

    digest = "5f4dcc3b5aa765d61d8327deb882cf99"  # md5("password"), as the classic stored it
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO clients (guid, pbid, ip, name, connections, group_bits, mask_level,"
                " auto_login, greeting, password, time_add, time_edit)"
                " VALUES ('g', '', '', 'Bob', 1, 128, 0, 0, '', :pw, 0, 0)"
            ),
            {"pw": digest},
        )

    upgrade(url, "head")

    with engine.begin() as conn:
        assert conn.execute(text("SELECT password FROM clients")).scalar() == digest
        # And the column now holds a full modern hash, which is what it could not do before.
        modern = "pbkdf2_sha256$600000$" + "a" * 32 + "$" + "b" * 64
        conn.execute(text("UPDATE clients SET password = :pw"), {"pw": modern})
        assert conn.execute(text("SELECT password FROM clients")).scalar() == modern
