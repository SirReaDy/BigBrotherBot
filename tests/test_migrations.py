"""Alembic: the migrations apply cleanly, in order, stamp the version — and *say* where they are.

A fresh install is always right, because `connect()` creates the schema and stamps it. An upgraded one
is the problem this file's second half is about: the code moves to a revision the database has not
applied, and the only symptom is whatever the new column was for failing oddly. With several bots
sharing one database — the deployment this project recommends — one forgotten `b3 db upgrade` affects
every server at once.
"""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from b3.storage.migrate import (
    current_revision,
    head_revision,
    schema_state,
    upgrade,
    upgrade_reporting,
)

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


# -- where a database is, and whether anybody is told ---------------------------------------------


def test_a_database_at_head_says_it_is_current(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    upgrade(url, "head")

    state = schema_state(url)

    assert state.current == HEAD and state.head == HEAD
    assert state.current_ok is True
    assert (state.behind, state.ahead, state.unknown, state.unstamped) == (False,) * 4
    assert state.describe() == f"at {HEAD} (current)"


def test_a_database_left_behind_names_every_revision_it_is_missing(tmp_path):
    """The case the whole entry is about: an install that was upgraded and never migrated."""
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    upgrade(url, "0001")

    state = schema_state(url)

    assert state.behind is True
    assert state.pending == ("0002", "0003")  # oldest first, walked by Alembic rather than sorted
    assert state.current_ok is False
    assert "0001" in state.describe() and "0003" in state.describe()


def test_an_upgrade_says_what_it_applied(tmp_path):
    """`command.upgrade` succeeds silently whether it applied five migrations or none, which is
    indistinguishable from doing nothing."""
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    upgrade(url, "0001")

    result = upgrade_reporting(url, "head")

    assert (result.before, result.after) == ("0001", HEAD)
    assert result.applied == ("0002", "0003")
    assert result.changed is True
    assert "0001 -> 0003" in result.describe()


def test_an_upgrade_with_nothing_to_do_says_that_instead(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    upgrade(url, "head")

    result = upgrade_reporting(url, "head")

    assert result.changed is False
    assert "nothing to apply" in result.describe()


def test_a_database_ahead_of_this_code_is_not_reported_as_behind(tmp_path):
    """A shared database a newer bot has already migrated. Telling this operator to run `db upgrade`
    would be telling them to do something that cannot help."""
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"
    upgrade(url, "head")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '9999'"))
    engine.dispose()

    state = schema_state(url)

    assert state.ahead is True
    assert state.behind is False
    assert "newer than this code" in state.describe()


def test_a_database_with_no_stamp_is_its_own_case(tmp_path):
    """An empty file, or a schema somebody built by hand. Not the same as being behind."""
    url = f"sqlite:///{tmp_path / 'm.sqlite'}"

    state = schema_state(url)

    assert state.unstamped is True
    assert state.behind is False
    assert "no revision stamp" in state.describe()


def test_a_database_that_cannot_be_read_is_an_answer_rather_than_a_crash(tmp_path):
    """An unreachable database must not be reported as needing an upgrade: that sends an operator to
    run a command which will fail for an entirely different reason."""
    state = schema_state("postgresql+psycopg://nobody@127.0.0.1:1/nothing")

    assert state.unknown is True
    assert state.behind is False
    assert state.describe().startswith("could not be read")
