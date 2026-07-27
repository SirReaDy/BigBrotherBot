"""SQLAlchemy storage: schema/seed, client upsert, superadmin bootstrap, penalties, aliases."""

from __future__ import annotations

import pytest

from b3.core.clock import FakeClock
from b3.domain.client import Alias, Client, IpAlias, Penalty, PenaltyType
from b3.storage.store import SUPERADMIN_BIT, SqlAlchemyStorage


@pytest.fixture()
def store(tmp_path):
    clock = FakeClock(start=1_000.0)
    url = f"sqlite:///{tmp_path / 'test.sqlite'}"
    s = SqlAlchemyStorage(url, clock=clock)
    s.connect()
    s._fake_clock = clock  # expose for tests that need to advance time
    yield s
    s.close()


def test_connect_seeds_groups(store):
    groups = store.get_groups()
    assert len(groups) == 8
    assert {g.keyword for g in groups} >= {"superadmin", "admin", "guest"}


def test_save_client_insert_then_update(store):
    c = store.save_client(Client(guid="GUID1", name="Joe"))
    assert c.id is not None
    first_id = c.id

    fetched = store.get_client_by_guid("GUID1")
    assert fetched is not None
    assert fetched.name == "Joe"

    fetched.name = "Joseph"
    updated = store.save_client(fetched)
    assert updated.id == first_id  # same row, not a duplicate
    assert store.count_clients() == 1
    assert store.get_client_by_guid("GUID1").name == "Joseph"


def test_iamgod_superadmin_bootstrap(store):
    # Fresh DB: no superadmin yet -> !iamgod would be available.
    assert store.has_superadmin() is False

    c = store.save_client(Client(guid="ADMINGUID", name="Boss"))
    c.group_bits = SUPERADMIN_BIT
    store.save_client(c)

    # Now a superadmin exists -> !iamgod locks itself out.
    assert store.has_superadmin() is True


def test_penalties_add_get_disable(store):
    c = store.save_client(Client(guid="G", name="Target"))
    store.add_penalty(Penalty(type=PenaltyType.BAN, client_id=c.id, reason="cheating"))

    active = store.get_active_penalties(c.id)
    assert len(active) == 1
    assert active[0].type is PenaltyType.BAN
    assert active[0].reason == "cheating"

    # Soft delete (lift) — row stays, just flagged inactive.
    lifted = store.disable_penalties(c.id)
    assert lifted == 1
    assert store.get_active_penalties(c.id) == []


def test_tempban_expiry_uses_clock(store):
    c = store.save_client(Client(guid="G2"))
    # Expires at t=1060; clock currently at 1000.
    store.add_penalty(Penalty(type=PenaltyType.TEMPBAN, client_id=c.id, time_expire=1_060))
    assert len(store.get_active_penalties(c.id)) == 1

    store._fake_clock.advance(120)  # -> t=1120, past expiry
    assert store.get_active_penalties(c.id) == []


def test_alias_increments_num_used(store):
    c = store.save_client(Client(guid="G3"))
    store.add_alias(Alias(value="Joe", client_id=c.id))
    store.add_alias(Alias(value="Joe", client_id=c.id))  # same name again
    aliases = store.get_aliases(c.id)
    assert len(aliases) == 1
    assert aliases[0].num_used == 2


def test_ip_alias_tracking(store):
    c = store.save_client(Client(guid="G4"))
    store.add_ip_alias(IpAlias(value="192.0.2.4", client_id=c.id))
    store.add_ip_alias(IpAlias(value="192.0.2.4", client_id=c.id))
    store.add_ip_alias(IpAlias(value="198.51.100.8", client_id=c.id))
    ips = store.get_ip_aliases(c.id)
    assert len(ips) == 2
    by_ip = {a.value: a.num_used for a in ips}
    assert by_ip == {"192.0.2.4": 2, "198.51.100.8": 1}


def test_active_penalties_are_returned_most_recent_first(store):
    """Callers take [0] as "the ban in force" — legacy getClientLastPenalty ordering."""
    c = store.save_client(Client(guid="G", name="Bob"))
    store._fake_clock.advance(10)
    store.add_penalty(Penalty(type=PenaltyType.BAN, client_id=c.id, reason="first"))
    store._fake_clock.advance(10)
    store.add_penalty(Penalty(type=PenaltyType.BAN, client_id=c.id, reason="second"))

    penalties = store.get_active_penalties(c.id, PenaltyType.BAN)
    assert [p.reason for p in penalties] == ["second", "first"]


def test_search_clients_matches_current_name(store):
    store.save_client(Client(guid="G1", name="Bobby"))
    store.save_client(Client(guid="G2", name="Alice"))

    assert [c.name for c in store.search_clients("bob")] == ["Bobby"]
    assert store.search_clients("nobody") == []
    assert store.search_clients("") == []


def test_search_clients_matches_a_past_alias(store):
    c = store.save_client(Client(guid="G1", name="NewName"))
    store.add_alias(Alias(value="OldName", client_id=c.id))

    assert [x.guid for x in store.search_clients("oldname")] == ["G1"]


def test_search_clients_is_limited_and_most_recent_first(store):
    for i in range(7):
        store._fake_clock.advance(10)
        store.save_client(Client(guid=f"G{i}", name=f"Player{i}"))

    found = store.search_clients("Player")
    assert len(found) == 5  # legacy getClientsMatching returned 5
    assert found[0].name == "Player6"  # most recently seen first
    assert len(store.search_clients("Player", limit=2)) == 2


def test_get_recent_penalties_across_clients(store):
    a = store.save_client(Client(guid="A", name="Ann"))
    b = store.save_client(Client(guid="B", name="Bob"))
    store.add_penalty(Penalty(type=PenaltyType.BAN, client_id=a.id, reason="first"))
    store.add_penalty(Penalty(type=PenaltyType.WARNING, client_id=a.id, reason="noise"))
    store.add_penalty(Penalty(type=PenaltyType.TEMPBAN, client_id=b.id, reason="second",
                              time_expire=2_000_000_000))

    bans = store.get_recent_penalties((PenaltyType.BAN, PenaltyType.TEMPBAN), limit=5)

    assert [p.reason for p in bans] == ["second", "first"]  # newest first, warning excluded


def test_get_recent_penalties_skips_lifted_and_expired(store):
    c = store.save_client(Client(guid="C", name="Cal"))
    lifted = store.add_penalty(Penalty(type=PenaltyType.BAN, client_id=c.id, reason="lifted"))
    store.add_penalty(Penalty(type=PenaltyType.TEMPBAN, client_id=c.id, reason="expired",
                              time_expire=1))
    store.disable_penalty(lifted.id)

    assert store.get_recent_penalties((PenaltyType.BAN, PenaltyType.TEMPBAN)) == []


def test_disable_penalty_lifts_exactly_one(store):
    c = store.save_client(Client(guid="D", name="Dee"))
    first = store.add_penalty(Penalty(type=PenaltyType.WARNING, client_id=c.id, reason="one"))
    store.add_penalty(Penalty(type=PenaltyType.WARNING, client_id=c.id, reason="two"))

    assert store.disable_penalty(first.id) is True
    assert store.disable_penalty(first.id) is False  # already lifted
    assert store.disable_penalty(None) is False

    remaining = store.get_active_penalties(c.id, PenaltyType.WARNING)
    assert [p.reason for p in remaining] == ["two"]


# -- which server issued a penalty -----------------------------------------------------------
#
# Only meaningful when several bots share one database, which is a setup the classic bot allowed and
# this one keeps. It is attribution, not scoping: enforcement still reads every server's penalties,
# because a shared ban list that only applied where it was typed would defeat the point.


def test_a_penalty_records_the_server_that_issued_it(store):  # noqa: ANN001
    client = store.save_client(Client(guid="G1", name="Bob"))
    store.add_penalty(
        Penalty(type=PenaltyType.BAN, client_id=client.id, reason="cheating", server_id="cod4_1")
    )

    (penalty,) = store.get_active_penalties(client.id)
    assert penalty.server_id == "cod4_1"


def test_a_penalty_with_no_server_stated_reads_as_empty(store):  # noqa: ANN001
    """A single-server install never sets `bot.server_id`, and legacy imports never had one."""
    client = store.save_client(Client(guid="G2", name="Ann"))
    store.add_penalty(Penalty(type=PenaltyType.BAN, client_id=client.id, reason="x"))

    assert store.get_active_penalties(client.id)[0].server_id == ""


def test_recent_penalties_span_every_server_by_default(store):  # noqa: ANN001
    bob = store.save_client(Client(guid="G3", name="Bob"))
    ann = store.save_client(Client(guid="G4", name="Ann"))
    store.add_penalty(
        Penalty(type=PenaltyType.BAN, client_id=bob.id, reason="one", server_id="cod4_1")
    )
    store.add_penalty(
        Penalty(type=PenaltyType.BAN, client_id=ann.id, reason="two", server_id="cod4_2")
    )

    everything = store.get_recent_penalties(limit=10)
    assert {p.server_id for p in everything} == {"cod4_1", "cod4_2"}

    just_one = store.get_recent_penalties(limit=10, server_id="cod4_2")
    assert [p.reason for p in just_one] == ["two"]


def test_a_ban_from_another_server_is_still_in_force(store):  # noqa: ANN001
    """The whole reason to share a database: the ban follows the player between servers."""
    bob = store.save_client(Client(guid="G5", name="Bob"))
    store.add_penalty(
        Penalty(type=PenaltyType.BAN, client_id=bob.id, reason="cheating", server_id="cod4_1")
    )

    # A second bot, `server_id="cod4_2"`, asks the same question it always asks.
    assert len(store.get_active_penalties(bob.id, PenaltyType.BAN)) == 1
