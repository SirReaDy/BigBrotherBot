"""Homefront's transport: length-prefixed TCP, a SHA1 login, and a server that hangs up if ignored.

Three of its properties are the kind that only bite in production, so each has a test here and is
exercised again end to end by `tools/fakeservers/e2e_homefront.py`: the framing is **asymmetric**
(six bytes of header out, seven back), the login is a hash in an unusual format, and **ten seconds of
silence costs the connection**.
"""

from __future__ import annotations

import json
import time
from struct import pack

import pytest

from b3.net.homefront import (
    CHANNEL_CHATTER,
    CHANNEL_SERVER,
    CLIENT_TRANSMISSION,
    SERVER_TRANSMISSION,
    HomefrontAuthError,
    HomefrontClient,
    HomefrontError,
    decode,
    encode,
    login_hash,
)
from tools.fakeservers.homefront import FakeHomefrontServer, expected_login

BOB_ID = "76561197963239765"


def _server_frame(channel: int, data: str) -> bytes:
    """A packet shaped the way the *server* sends them: type, channel, length, payload."""
    payload = data.encode("utf-8")
    return SERVER_TRANSMISSION + pack(">B", channel) + pack(">i", len(payload)) + payload


# -- the codec ----------------------------------------------------------------------------------


def test_a_client_packet_has_no_channel_byte():
    """The asymmetry, stated as a test because it is the one thing that silently ruins everything: a
    codec that writes a channel byte here puts every field of every reply one place out."""
    framed = encode(CLIENT_TRANSMISSION, "PING")
    assert framed[:2] == b"CT"
    assert framed[2:6] == pack(">i", 4)  # the length, immediately after the type
    assert framed[6:] == b"PING"
    assert len(framed) == 6 + 4


def test_a_server_packet_does_have_one():
    channel, data, rest = decode(_server_frame(CHANNEL_SERVER, "AUTH: true"))
    assert (channel, data, rest) == (CHANNEL_SERVER, "AUTH: true", b"")


def test_two_packets_in_one_read_are_both_recovered():
    buffer = _server_frame(CHANNEL_SERVER, "HELLO: 1.0") + _server_frame(CHANNEL_CHATTER, "hi")
    first = decode(buffer)
    assert first is not None
    channel, data, rest = first
    assert (channel, data) == (CHANNEL_SERVER, "HELLO: 1.0")
    second = decode(rest)
    assert second is not None
    assert (second[0], second[1]) == (CHANNEL_CHATTER, "hi")


@pytest.mark.parametrize("cut", [0, 1, 6, 7, 8])
def test_half_a_packet_is_not_a_packet(cut):
    """TCP is a stream: a read can hand over any fraction of a message."""
    whole = _server_frame(CHANNEL_SERVER, "LOGIN: Bob 76561197963239765")
    assert decode(whole[:cut]) is None


def test_a_message_may_contain_newlines():
    """The player-list reply is newline-separated *inside* one message, which is why the transport
    cannot use newlines as a delimiter of its own — and why messages reach the parser as JSON."""
    row = "76561197963239765\n1\nB3bot\nBob\n7\n2"
    decoded = decode(_server_frame(CHANNEL_SERVER, f"PLAYER: {row}"))
    assert decoded is not None
    assert decoded[1] == f"PLAYER: {row}"


def test_an_impossible_length_is_refused_rather_than_trusted():
    """There is no framing marker and no checksum in this protocol, so there is nothing to
    resynchronise on. Dropping the connection is recoverable; reading from the wrong offset is not."""
    bad = SERVER_TRANSMISSION + pack(">B", 4) + pack(">i", 99_999_999) + b"x"
    with pytest.raises(HomefrontError):
        decode(bad)


def test_the_login_hash_is_uppercase_hex_in_pairs():
    """An unusual format, and neither an unspaced nor a lowercase digest is accepted by the game."""
    assert login_hash("test") == expected_login("test")
    assert login_hash("test").count(" ") == 19  # 20 pairs of a 40-character digest
    assert login_hash("test") == login_hash("test").upper()
    assert "test" not in login_hash("test")  # the password itself never appears


# -- against the fake server ---------------------------------------------------------------------


def test_it_logs_in_and_reads_the_servers_version():
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "test", timeout=0.5, playerlist_interval=0)
        client.open()

        assert client.authed is True
        assert server.authed is True
        assert client.server_version == "1.0.0.0"
        # What crossed the wire was the hash, not the password.
        assert server.received == [f'PASS: "{expected_login("test")}"']
        client.close()
    finally:
        server.stop()


def test_a_wrong_password_is_refused_and_says_so():
    server = FakeHomefrontServer(password="right", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "wrong", timeout=0.3, playerlist_interval=0)
        with pytest.raises(HomefrontAuthError):
            client.open()
        assert server.authed is False
    finally:
        server.stop()


def test_commands_are_ignored_until_the_login_is_accepted():
    """A real server does that, and a fake which obeyed anyway would hide a login the bot never
    completed — the bot would look fine and change nothing."""
    server = FakeHomefrontServer(password="right", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "wrong", timeout=0.3)
        with pytest.raises(HomefrontAuthError):
            client.open()
        assert server.players == {}
    finally:
        server.stop()


def test_every_message_is_delivered_exactly_once():
    """The bug this pins: `read_lines` handed back the same lines on the next call as well, because
    the buffer it drained was the same list object it then extended. One read looked perfect."""
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "test", timeout=0.3, playerlist_interval=0)
        client.open()
        server.add_player(BOB_ID, "Bob")

        first = _drain(client)
        second = client.read_lines()

        assert any("LOGIN: Bob" in line for line in first)
        assert second == []  # not the same lines a second time
        client.close()
    finally:
        server.stop()


def test_a_fragmented_stream_is_reassembled():
    """The server can write a reply in seven-byte slices; a client that assumed whole messages per
    read would see nothing but garbage. Proved rather than assumed, as with Frostbite."""
    server = FakeHomefrontServer(password="test", idle_timeout=0, fragment=7).start()
    try:
        client = HomefrontClient(*server.address, "test", timeout=0.5, playerlist_interval=0)
        client.open()
        server.push("PLAYER: %s\n1\nB3bot\nBob the Builder\n7\n2" % BOB_ID)

        lines = _drain(client)

        assert any("Bob the Builder" in line for line in lines)
        client.close()
    finally:
        server.stop()


def test_messages_reach_the_parser_as_channel_and_data():
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "test", timeout=0.3, playerlist_interval=0)
        client.open()
        server.push("BROADCAST: Bob says: hello", CHANNEL_CHATTER)

        lines = _drain(client)

        assert json.loads(lines[-1]) == [CHANNEL_CHATTER, "BROADCAST: Bob says: hello"]
        client.close()
    finally:
        server.stop()


def test_the_handshake_and_the_keepalive_are_kept_off_the_parser():
    """HELLO, AUTH and PONG belong to the connection. A parser that had to know about them would be
    a parser that knows about sockets."""
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(
            *server.address, "test", timeout=0.3, playerlist_interval=0, ping_interval=0
        )
        client.open()
        lines = _drain(client)

        assert not any("AUTH" in line or "HELLO" in line or "PONG" in line for line in lines)
        assert server.pings > 0  # the ping went out
        client.close()
    finally:
        server.stop()


def test_silence_really_does_cost_the_connection():
    """Ten seconds on a real server. A bot that does not ping is a bot that works for ten seconds."""
    server = FakeHomefrontServer(password="test", idle_timeout=0.3).start()
    try:
        client = HomefrontClient(
            *server.address, "test", timeout=0.1, playerlist_interval=0, ping_interval=99
        )
        client.open()
        time.sleep(0.6)
        for _ in range(4):
            client.read_lines()

        assert server.dropped_for_silence is True
    finally:
        server.stop()


def test_and_the_keepalive_prevents_it():
    server = FakeHomefrontServer(password="test", idle_timeout=0.3).start()
    try:
        client = HomefrontClient(
            *server.address, "test", timeout=0.05, playerlist_interval=0, ping_interval=0.05
        )
        client.open()
        deadline = time.time() + 0.7
        while time.time() < deadline:
            client.read_lines()

        assert server.dropped_for_silence is False
        assert server.pings >= 2
        client.close()
    finally:
        server.stop()


def test_the_roster_is_asked_for_periodically_because_it_cannot_be_asked_for_once():
    """`RETRIEVE PLAYERLIST` is answered by pushed messages, so somebody has to keep asking. The
    classic parser used a cron thread; this comes off the pump, which needs no thread."""
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(
            *server.address, "test", timeout=0.05, playerlist_interval=0.05, ping_interval=99
        )
        client.open()
        server.add_player(BOB_ID, "Bob")
        deadline = time.time() + 0.5
        while time.time() < deadline:
            client.read_lines()

        assert server.sent_command("RETRIEVE PLAYERLIST")
        client.close()
    finally:
        server.stop()


def test_an_empty_command_is_not_sent():
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "test", timeout=0.3, playerlist_interval=0)
        client.open()
        server.received.clear()
        client.command("   ")

        assert server.received == []
        client.close()
    finally:
        server.stop()


def test_a_command_gets_no_reply_because_there_is_none_to_get():
    """Answers on this engine arrive as pushed messages, not as returns."""
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "test", timeout=0.3, playerlist_interval=0)
        client.open()

        assert client.command("adminsay hello") == ""
        client.close()
    finally:
        server.stop()


def test_the_client_notices_a_server_that_went_away():
    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    client = HomefrontClient(*server.address, "test", timeout=0.1, playerlist_interval=0)
    client.open()
    assert client.connected is True

    server.stop()
    for _ in range(5):
        client.read_lines()

    assert client.connected is False


def _drain(client: HomefrontClient, rounds: int = 6) -> list[str]:
    """Read until the server has nothing more to say, and return everything it said."""
    lines: list[str] = []
    for _ in range(rounds):
        lines.extend(client.read_lines())
        time.sleep(0.02)
    return lines


# -- and coming back after the server hangs up ---------------------------------------------------


def test_it_reconnects_after_the_server_goes_away():
    """The bug this pins was BattlEye's once: the client noticed the connection had gone and then
    reported nothing for ever, so the bot looked healthy and was deaf. This engine closes the socket
    on purpose — a restart, a map change, ten seconds of quiet — so it matters more here, not less.
    """
    from b3.net.homefront import CHANNEL_CLIENT_NOTICE, RECONNECTED_NOTICE

    server = FakeHomefrontServer(password="test", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "test", timeout=0.1, playerlist_interval=0)
        client.open()
        assert client.connected is True

        server.stop()
        for _ in range(5):
            client.read_lines()
        assert client.connected is False

        server.restart()  # the same address comes back, as a restarted game server does
        client._retry_at = 0.0  # and do not wait out the back-off in a test
        lines = _drain(client, rounds=10)

        assert client.connected is True
        assert client.authed is True
        # It says so, so the parser can throw away a roster it can no longer trust.
        assert json.dumps([CHANNEL_CLIENT_NOTICE, RECONNECTED_NOTICE]) in lines
        # ...and asks who is actually playing rather than waiting for the next interval.
        assert server.sent_command("RETRIEVE PLAYERLIST")
        client.close()
    finally:
        server.stop()


def test_a_reconnect_that_is_rejected_backs_off_hard_and_says_why():
    """A wrong password will not fix itself, and hammering a server with one is how an address gets
    blocked. Straight to the ceiling, and one loud line rather than a repeated whisper."""
    from b3.net.homefront import RETRY_MAX

    server = FakeHomefrontServer(password="right", idle_timeout=0).start()
    try:
        client = HomefrontClient(*server.address, "right", timeout=0.1, playerlist_interval=0)
        client.open()
        client.password = "wrong"  # as if it had been changed on the server
        client.connected = False
        client._retry_at = 0.0

        assert client._try_reconnect() is False
        assert client._retry_at - time.monotonic() > RETRY_MAX / 2
    finally:
        server.stop()


def test_the_back_off_grows_between_failed_attempts():
    client = HomefrontClient("127.0.0.1", 1, "test", timeout=0.05)  # nothing is listening there
    client.connected = False

    delays = []
    for _ in range(3):
        client._retry_at = 0.0
        assert client._try_reconnect() is False
        delays.append(client._retry_at - time.monotonic())

    assert delays[0] < delays[1] < delays[2]
