"""The Frostbite connection: word-list framing, TCP reassembly, the login challenge, pushed events.

Against a real socket and a real (fake) server, because the hard parts here are all stateful or
stream-shaped: a reply can arrive in pieces, a login is a challenge-response, and an event has to be
acknowledged or it comes again.
"""

from __future__ import annotations

import json
import time

import pytest

from b3.core.clock import FakeClock
from b3.net.frostbite import (
    FROM_SERVER,
    HEADER_SIZE,
    IS_RESPONSE,
    FrostbiteAuthError,
    FrostbiteClient,
    FrostbiteCommandError,
    FrostbiteError,
    FrostbiteTimeout,
    decode,
    encode,
    packet_length,
    password_hash,
    split_command,
)
from tools.fakeservers.frostbite import SALT_HEX, FakeFrostbiteServer


@pytest.fixture
def server():  # noqa: ANN201
    fake = FakeFrostbiteServer(password="test", resend_unacked_after=99)
    fake.start()
    yield fake
    fake.stop()


def _client(server, **kwargs):  # noqa: ANN001, ANN202
    client = FrostbiteClient(*server.address, "test", timeout=2.0, **kwargs)
    client.open()
    return client


def _drain(client, *, expect: int = 1, rounds: int = 25):  # noqa: ANN001, ANN202
    lines: list[str] = []
    for _ in range(rounds):
        lines.extend(client.read_lines())
        if len(lines) >= expect:
            break
        time.sleep(0.02)
    return lines


# -- framing ---------------------------------------------------------------


def test_a_packet_is_a_word_list():
    packet = encode(["admin.say", "hello there", "all"], 3)
    decoded = decode(packet)
    assert decoded.words == ["admin.say", "hello there", "all"]
    assert decoded.sequence == 3
    assert not decoded.from_server
    assert not decoded.is_response


def test_the_flags_live_in_the_top_bits_of_the_sequence():
    """One field carries three things, which is the trap: mask before comparing."""
    event = decode(encode(["player.onChat"], 5, from_server=True))
    assert event.from_server and not event.is_response and event.sequence == 5

    reply = decode(encode(["OK"], 5, from_server=True, response=True))
    assert reply.from_server and reply.is_response and reply.sequence == 5

    header = int.from_bytes(encode(["OK"], 5, from_server=True, response=True)[:4], "little")
    assert header & FROM_SERVER and header & IS_RESPONSE


def test_a_high_sequence_number_does_not_bleed_into_the_flags():
    decoded = decode(encode(["x"], 0x3FFFFFFF))
    assert decoded.sequence == 0x3FFFFFFF
    assert not decoded.from_server and not decoded.is_response


def test_the_size_field_counts_the_header():
    packet = encode(["version"], 0)
    assert packet_length(packet) == len(packet)
    assert len(packet) == HEADER_SIZE + 4 + len("version") + 1


def test_an_empty_word_survives():
    """A kill with no killer arrives as an empty first word — dropping it would shift every field."""
    assert decode(encode(["player.onKill", "", "Bob", "DamageArea", "false"], 0)).words == [
        "player.onKill",
        "",
        "Bob",
        "DamageArea",
        "false",
    ]


def test_non_ascii_words_round_trip():
    assert decode(encode(["player.onChat", "Renée", "café ☕"], 0)).words[1:] == [
        "Renée",
        "café ☕",
    ]


def test_a_partial_packet_is_not_yet_a_packet():
    packet = encode(["version"], 0)
    assert packet_length(packet[:4]) is None  # too short to know
    assert packet_length(packet[:-2]) is None  # known length, not all here
    assert packet_length(packet) == len(packet)


def test_a_nonsensical_size_is_refused():
    """There are no framing markers to resynchronise on, so this has to be fatal, not skipped."""
    with pytest.raises(FrostbiteError):
        packet_length(b"\x00\x00\x00\x00\x01\x00\x00\x00")


# -- the login challenge ---------------------------------------------------


def test_the_password_hash_is_md5_of_salt_then_password():
    assert password_hash(bytes.fromhex("AABB"), "secret") == password_hash(b"\xaa\xbb", "secret")
    assert password_hash(b"\xaa\xbb", "secret").isupper()
    assert len(password_hash(b"\xaa\xbb", "secret")) == 32


def test_a_successful_login_also_asks_for_events(server):  # noqa: ANN001
    """Events are opt-in: without this the connection is silent and the bot looks broken."""
    client = _client(server)
    try:
        assert server.logged_in
        assert server.events_enabled
    finally:
        client.close()


def test_the_password_never_crosses_the_wire(server):  # noqa: ANN001
    client = _client(server)
    try:
        sent = [" ".join(words) for words in server.received]
        assert not any("test" in line for line in sent)
        assert any(SALT_HEX not in line and "login.hashed" in line for line in sent)
    finally:
        client.close()


def test_a_wrong_password_is_reported_as_such(server):  # noqa: ANN001
    client = FrostbiteClient(*server.address, "wrong", timeout=1.0)
    with pytest.raises(FrostbiteAuthError):
        client.open()
    assert not client.connected


def test_nothing_listening_fails_at_startup():
    client = FrostbiteClient("127.0.0.1", 1, "test", timeout=0.3)
    with pytest.raises(FrostbiteTimeout):
        client.open()


# -- commands --------------------------------------------------------------


def test_a_command_and_its_reply(server):  # noqa: ANN001
    client = _client(server)
    try:
        assert client.command("version") == "BF3 1234567"
    finally:
        client.close()


def test_a_quoted_value_stays_one_word(server):  # noqa: ANN001
    """The bot composes command *strings*; this engine takes word lists. Quoting is the bridge."""
    client = _client(server)
    try:
        client.command('admin.say "hello there everyone" all')
        assert ["admin.say", "hello there everyone", "all"] in server.received
    finally:
        client.close()


@pytest.mark.parametrize(
    ("composed", "words"),
    [
        ('admin.say "one two" all', ["admin.say", "one two", "all"]),
        ("admin.kickPlayer Bob idle", ["admin.kickPlayer", "Bob", "idle"]),
        (
            'banList.add guid "EA_1" perm "no reason given"',
            ["banList.add", "guid", "EA_1", "perm", "no reason given"],
        ),
        ('admin.say "" all', ["admin.say", "", "all"]),
    ],
)
def test_splitting_a_composed_command(composed, words):  # noqa: ANN001
    assert split_command(composed) == words


def test_an_unbalanced_quote_does_not_take_the_bot_down():
    assert split_command('admin.say "oops all') == ["admin.say", '"oops', "all"]


def test_a_refused_command_names_the_reason(server):  # noqa: ANN001
    """`CommandDisallowedOnRanked` is more use to an admin than "command failed"."""
    client = _client(server)
    try:
        server.replies.pop("version", None)
        server._answer = lambda words: ["CommandDisallowedOnRanked"]  # type: ignore[method-assign]
        with pytest.raises(FrostbiteCommandError) as caught:
            client.command("admin.shutDown")
        assert "CommandDisallowedOnRanked" in str(caught.value)
    finally:
        client.close()


def test_a_reply_arriving_in_pieces_is_reassembled():
    """TCP is a stream: a read can hand over half a packet. Only the size field says where it ends."""
    fake = FakeFrostbiteServer(password="test", chunk_size=7, resend_unacked_after=99).start()
    try:
        client = FrostbiteClient(*fake.address, "test", timeout=3.0)
        client.open()  # the login itself already arrives in 7-byte slices
        try:
            players = client.get_players()
            assert [p.name for p in players] == ["Bravo17", "Bob"]
        finally:
            client.close()
    finally:
        fake.stop()


def test_several_packets_in_one_read_are_all_handled(server):  # noqa: ANN001
    """The other half of stream framing: two events can arrive in a single recv."""
    client = _client(server)
    try:
        server.push(["player.onChat", "Bravo17", "one", "all"])
        server.push(["player.onChat", "Bravo17", "two", "all"])
        lines = _drain(client, expect=2)
        assert [json.loads(line)[2] for line in lines] == ["one", "two"]
    finally:
        client.close()


# -- the player block ------------------------------------------------------


def test_the_player_block_is_read_by_field_name(server):  # noqa: ANN001
    client = _client(server)
    try:
        players = client.get_players()
        assert [p.name for p in players] == ["Bravo17", "Bob"]
        assert players[0].cid == "Bravo17"  # the name *is* the handle on this engine
        assert players[0].guid == "EA_00000000000000000000000000000001"
        assert players[0].score == 1500
    finally:
        client.close()


def test_a_server_that_adds_a_column_does_not_break_it(server):  # noqa: ANN001
    """The block carries its own schema precisely so this is safe. Read names, not positions."""
    server.replies["admin.listPlayers"] = [
        "4",
        "name",
        "guid",
        "score",
        "somethingNew",
        "1",
        "Bravo17",
        "EA_1234567890123456",
        "99",
        "whatever",
    ]
    client = _client(server)
    try:
        (player,) = client.get_players()
        assert (player.name, player.guid, player.score) == ("Bravo17", "EA_1234567890123456", 99)
    finally:
        client.close()


# -- pushed events ---------------------------------------------------------


def test_an_event_arrives_as_a_json_word_list(server):  # noqa: ANN001
    """A word may contain any character, so no delimiter is safe. JSON is reversible."""
    client = _client(server)
    try:
        server.push(["player.onChat", "Bravo17", "words with spaces, and a \\t tab", "all"])
        (line,) = _drain(client)
        assert json.loads(line) == [
            "player.onChat",
            "Bravo17",
            "words with spaces, and a \\t tab",
            "all",
        ]
    finally:
        client.close()


def test_events_are_acknowledged(server):  # noqa: ANN001
    """Unacknowledged, the server sends it again — and the bot acts on it twice."""
    client = _client(server)
    try:
        sequence = server.push(["player.onLeave", "Bob"])
        _drain(client)
        assert server.wait_for_ack(sequence)
    finally:
        client.close()


def test_an_event_arriving_while_a_command_waits_is_kept(server):  # noqa: ANN001
    client = _client(server)
    try:
        server.push(["player.onChat", "Bravo17", "mid-command", "all"])
        assert client.command("version") == "BF3 1234567"
        (line,) = _drain(client)
        assert json.loads(line)[2] == "mid-command"
    finally:
        client.close()


def test_reading_hands_each_event_out_once(server):  # noqa: ANN001
    client = _client(server)
    try:
        server.push(["player.onChat", "Bravo17", "once", "all"])
        assert len(_drain(client)) == 1
        assert client.read_lines() == []
    finally:
        client.close()


# -- losing the server -----------------------------------------------------


def test_a_closed_connection_is_noticed(server):  # noqa: ANN001
    """A Battlefield server restarts between maps; the bot must not go quietly deaf."""
    client = _client(server)
    try:
        server.stop()
        for _ in range(10):
            client.read_lines()
            if not client.connected:
                break
            time.sleep(0.05)
        assert not client.connected
    finally:
        client.close()


def test_it_reconnects_when_the_server_comes_back():
    fake = FakeFrostbiteServer(password="test", resend_unacked_after=99).start()
    port = fake.address[1]
    clock = FakeClock(start=1000.0)
    client = FrostbiteClient(*fake.address, "test", timeout=0.5, clock=clock)
    client.open()
    try:
        fake.stop()
        for _ in range(10):
            client.read_lines()
            if not client.connected:
                break
            clock.advance(200)
        assert not client.connected

        replacement = FakeFrostbiteServer(password="test", resend_unacked_after=99)
        replacement._listener.close()
        import socket as _socket

        replacement._listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        replacement._listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        replacement._listener.bind(("127.0.0.1", port))
        replacement._listener.listen(1)
        replacement._listener.settimeout(0.05)
        replacement.address = replacement._listener.getsockname()
        replacement.start()
        try:
            clock.advance(600)
            for _ in range(20):
                client.read_lines()
                if client.connected:
                    break
                time.sleep(0.05)
            assert client.connected
            assert replacement.logged_in  # a real login, not just a reopened socket
            assert replacement.events_enabled  # and it asked to be sent events again
        finally:
            replacement.stop()
    finally:
        client.close()


def test_a_rejected_password_on_reconnect_stops_hammering(server):  # noqa: ANN001
    clock = FakeClock(start=1000.0)
    client = FrostbiteClient(*server.address, "test", timeout=0.5, clock=clock, retry_max=300.0)
    client.open()
    try:
        server.accept_login = False
        client._lost("pretend the link dropped")
        clock.advance(10)
        assert client.read_lines() == []
        assert client._retry_at - clock.now() >= 300.0
    finally:
        client.close()


def test_reading_from_a_never_opened_client_does_not_explode():
    client = FrostbiteClient("127.0.0.1", 1, "test", timeout=0.05)
    assert client.read_lines() == []
