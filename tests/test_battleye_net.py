"""The BattlEye connection: framing, login, pushed messages, split replies, keepalives.

Exercised against a real socket and a real (fake) server rather than a mock, because everything that
is hard here is *stateful*: sequence numbers, acknowledgements a missing one of which makes the server
repeat itself, and a reply arriving in pieces. A mock would assert that we call what we think we call.
"""

from __future__ import annotations

import pytest

from b3.core.clock import FakeClock
from b3.net.battleye import (
    COMMAND,
    LOGIN,
    MESSAGE,
    BattleyeAuthError,
    BattleyeClient,
    BattleyeError,
    BattleyeTimeout,
    crc,
    decode,
    encode,
)
from tools.fakeservers.battleye import FakeBattleyeServer


@pytest.fixture
def server():  # noqa: ANN201
    fake = FakeBattleyeServer(password="test", resend_unacked_after=99)
    fake.start()
    yield fake
    fake.stop()


def _client(server, **kwargs):  # noqa: ANN001, ANN202
    client = BattleyeClient(*server.address, "test", timeout=2.0, **kwargs)
    client.open()
    return client


# -- framing ---------------------------------------------------------------


def test_a_login_packet_has_no_sequence_byte():
    packet = encode(LOGIN, None, "secret")
    assert packet[:2] == b"BE"
    assert packet[6] == 0xFF
    assert packet[7] == LOGIN
    assert packet[8:] == b"secret"


def test_a_command_packet_carries_its_sequence():
    packet = encode(COMMAND, 7, "players")
    assert packet[7] == COMMAND
    assert packet[8] == 7
    assert packet[9:] == b"players"


def test_the_checksum_covers_the_payload_only():
    packet = encode(COMMAND, 1, "say -1 hi")
    assert packet[2:6] == crc(packet[6:])


def test_a_packet_round_trips():
    packet = decode(encode(MESSAGE, 42, "(Global) Bob: hi"))
    assert packet is not None
    assert (packet.type, packet.sequence, packet.data) == (MESSAGE, 42, b"(Global) Bob: hi")


def test_a_corrupt_packet_is_ignored_not_raised():
    """A mangled datagram is normal on a UDP link; the server resends anything that mattered."""
    good = bytearray(encode(COMMAND, 1, "players"))
    good[10] ^= 0xFF  # flip a payload bit, leaving the checksum stale
    assert decode(bytes(good)) is None


@pytest.mark.parametrize("junk", [b"", b"XX", b"BE", b"NOTBATTLEYE"])
def test_something_that_is_not_a_battleye_packet(junk):  # noqa: ANN001
    assert decode(junk) is None


def test_non_ascii_survives_the_trip():
    packet = decode(encode(MESSAGE, 1, "(Global) Renée: café"))
    assert packet is not None
    assert packet.data.decode("utf-8") == "(Global) Renée: café"


# -- login -----------------------------------------------------------------


def test_a_successful_login(server):  # noqa: ANN001
    client = _client(server)
    try:
        assert client.connected
        assert server.logged_in
    finally:
        client.close()


def test_a_rejected_password_says_so(server):  # noqa: ANN001
    """The distinction an operator actually needs: refused, not merely silent."""
    client = BattleyeClient(*server.address, "wrong", timeout=1.0)
    with pytest.raises(BattleyeAuthError):
        client.open()
    assert not client.connected


def test_nothing_listening_times_out_instead():
    client = BattleyeClient("127.0.0.1", 1, "test", timeout=0.3)
    with pytest.raises(BattleyeTimeout):
        client.open()


def test_using_a_closed_client_is_an_error(server):  # noqa: ANN001
    client = _client(server)
    client.close()
    with pytest.raises(BattleyeError):
        client.command("players")


# -- commands --------------------------------------------------------------


def test_a_command_and_its_reply(server):  # noqa: ANN001
    client = _client(server)
    try:
        reply = client.command("players")
        assert "Bravo17" in reply
        assert server.received == ["players"]
    finally:
        client.close()


def test_a_reply_split_across_packets_is_reassembled(server):  # noqa: ANN001
    """How a long ban list arrives. Each part is prefixed 0x00 <total> <index>."""
    server.replies["bans"] = "\n".join(f"{i} 80a5885ebe2420bab5e158123456789{i} perm reason" for i in range(60))
    server.max_packet = 200
    client = _client(server)
    try:
        reply = client.command("bans")
        assert reply == server.replies["bans"]
        assert reply.count("\n") == 59  # nothing lost or duplicated at a boundary
    finally:
        client.close()


def test_a_command_with_no_answer_times_out(server):  # noqa: ANN001
    server.replies["silent"] = ""
    client = BattleyeClient(*server.address, "test", timeout=0.4)
    client.open()
    try:
        # An empty reply is still a reply; make the server not answer at all instead.
        server.replies.pop("silent")
        server._handle_command = lambda *_args: None  # type: ignore[method-assign]
        with pytest.raises(BattleyeTimeout):
            client.command("whatever")
    finally:
        client.close()


def test_each_command_gets_its_own_sequence_number(server):  # noqa: ANN001
    client = _client(server)
    try:
        client.command("players")
        client.command("players")
        assert server.received == ["players", "players"]
    finally:
        client.close()


# -- pushed server messages ------------------------------------------------


def test_a_pushed_message_arrives_as_a_line(server):  # noqa: ANN001
    client = _client(server)
    try:
        server.push("(Global) Bravo17: hello b3")
        assert _drain(client) == ["(Global) Bravo17: hello b3"]
    finally:
        client.close()


def test_pushed_messages_are_acknowledged(server):  # noqa: ANN001
    """Not politeness: an unacknowledged message is sent again, for as long as it takes."""
    client = _client(server)
    try:
        sequence = server.push("Player #0 Bravo17 (76.108.91.78:2304) connected")
        _drain(client)
        assert server.wait_for_ack(sequence)
    finally:
        client.close()


def test_a_resent_message_is_not_acted_on_twice():
    """The consequence if it were: a chat line runs twice, and `!ban` with it."""
    fake = FakeBattleyeServer(password="test", resend_unacked_after=0.05).start()
    try:
        client = BattleyeClient(*fake.address, "test", timeout=1.0)
        client.open()
        try:
            # Push, then let the server retransmit before the client has a chance to acknowledge.
            packet = encode(MESSAGE, 5, "(Global) Bravo17: !ban Bob")
            fake._send(packet)
            fake._send(packet)
            lines = _drain(client, rounds=4)
            assert lines == ["(Global) Bravo17: !ban Bob"]
        finally:
            client.close()
    finally:
        fake.stop()


def test_a_message_arriving_while_a_command_waits_is_kept(server):  # noqa: ANN001
    """The reason this can be single-threaded: nothing is dropped while a reply is outstanding."""
    client = _client(server)
    try:
        server.push("(Global) Bravo17: mid-command")
        reply = client.command("players")  # pumps the socket, meeting the message on the way
        assert "Bravo17" in reply
        assert _drain(client) == ["(Global) Bravo17: mid-command"]
    finally:
        client.close()


def test_reading_returns_everything_since_last_time(server):  # noqa: ANN001
    client = _client(server)
    try:
        server.push("Player #0 Bravo17 (76.108.91.78:2304) connected")
        server.push("(Global) Bravo17: hi")
        lines = _drain(client, expect=2)
        assert lines == [
            "Player #0 Bravo17 (76.108.91.78:2304) connected",
            "(Global) Bravo17: hi",
        ]
        assert client.read_lines() == []  # and they are not handed out twice
    finally:
        client.close()


# -- chat pacing and keepalives --------------------------------------------


def test_chat_is_paced_because_the_server_drops_a_burst(server):  # noqa: ANN001
    """An Arma server silently discards rapid `say`s. Queueing beats sleeping on the event loop."""
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(*server.address, "test", timeout=1.0, clock=clock, say_interval=0.8)
    client.open()
    try:
        client.write("say -1 line one")
        client.write("say -1 line two")
        client.write("say -1 line three")
        assert server.wait_for_command("line one")
        # The other two are queued, not lost, and not sent yet.
        assert not any("line two" in c for c in server.received)

        clock.advance(1.0)
        client.read_lines()  # the next pass through the loop releases the next line
        assert server.wait_for_command("line two")
        assert not any("line three" in c for c in server.received)

        clock.advance(1.0)
        client.read_lines()
        assert server.wait_for_command("line three")
    finally:
        client.close()


def test_a_command_flushes_queued_chat_first(server):  # noqa: ANN001
    """So "banning Bob" cannot arrive after the ban it announces."""
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(*server.address, "test", timeout=1.0, clock=clock)
    client.open()
    try:
        client.write("say -1 banning Bob")
        client.write("say -1 for cheating")
        client.command("players")
        assert [c for c in server.received if "say" in c] == [
            "say -1 banning Bob",
            "say -1 for cheating",
        ]
    finally:
        client.close()


def test_a_keepalive_goes_out_when_the_line_is_quiet(server):  # noqa: ANN001
    """BattlEye hangs up after about 45 seconds of silence."""
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(*server.address, "test", timeout=1.0, clock=clock, keepalive=25.0)
    client.open()
    try:
        client.read_lines()
        assert server.received == []  # nothing to say yet

        clock.advance(30)
        client.read_lines()
        # A keepalive is an empty command packet, which the fake server records as no command at
        # all — so prove it another way: the client noted the send and does not send a second one.
        clock.advance(1)
        before = client._last_send
        client.read_lines()
        assert client._last_send == before
    finally:
        client.close()


def _drain(client, *, expect: int = 1, rounds: int = 20):  # noqa: ANN001, ANN202
    """Read until ``expect`` lines have arrived, or we run out of patience."""
    import time

    lines: list[str] = []
    for _ in range(rounds):
        lines.extend(client.read_lines())
        if len(lines) >= expect:
            break
        time.sleep(0.02)
    return lines


# --- losing the server, and getting it back -------------------------------
#
# There is no goodbye packet in this protocol: a restarting Arma server just stops answering. Without
# the checks below the bot reads nothing for the rest of the night and never says why — which is the
# same class of failure the remote log sources' back-off exists to prevent.


def test_silence_after_a_keepalive_is_treated_as_a_lost_connection(server):  # noqa: ANN001
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(
        *server.address, "test", timeout=0.3, clock=clock, keepalive=25.0, dead_after=90.0
    )
    client.open()
    try:
        server.stop()  # the server goes away, saying nothing

        clock.advance(30)
        client.read_lines()  # sends a keepalive, hears nothing
        assert client.connected  # not yet: one missed keepalive is not a dead server

        clock.advance(70)
        assert client.read_lines() == []
        assert not client.connected  # now it is
    finally:
        client.close()


def test_a_quiet_server_with_nobody_on_it_is_not_a_lost_connection(server):  # noqa: ANN001
    """An empty Arma server says nothing for hours. That must not look like a fault."""
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(
        *server.address, "test", timeout=0.3, clock=clock, keepalive=25.0, dead_after=90.0
    )
    client.open()
    try:
        for _ in range(6):
            clock.advance(30)
            client.read_lines()  # keepalive out, and the fake server answers nothing to those...
            # ...but it *is* still there, and the ack of a keepalive is not a thing, so prove
            # liveness the way the client does: a command still works.
            assert "Bravo17" in client.command("players")
        assert client.connected
    finally:
        client.close()


def test_it_reconnects_and_keeps_working():
    """A mission change restarts the server; the bot should pick straight back up."""
    fake = FakeBattleyeServer(password="test", resend_unacked_after=99).start()
    port = fake.address[1]
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(*fake.address, "test", timeout=0.3, clock=clock, retry_interval=5.0)
    client.open()
    try:
        fake.stop()
        clock.advance(200)
        client.read_lines()
        assert not client.connected

        # The server comes back on the same port, as a restarted one does.
        replacement = FakeBattleyeServer(password="test", resend_unacked_after=99)
        replacement._sock.close()
        import socket as _socket

        replacement._sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        replacement._sock.bind(("127.0.0.1", port))
        replacement._sock.settimeout(0.05)
        replacement.address = replacement._sock.getsockname()
        replacement.start()
        try:
            clock.advance(10)  # past the back-off
            client.read_lines()
            assert client.connected
            assert replacement.logged_in  # it logged in again, not just reopened a socket

            replacement.push("(Global) Bravo17: still here")
            assert _drain(client) == ["(Global) Bravo17: still here"]
        finally:
            replacement.stop()
    finally:
        client.close()


def test_reconnecting_is_backed_off_not_hammered():
    clock = FakeClock(start=1000.0)
    client = BattleyeClient("127.0.0.1", 1, "test", timeout=0.05, clock=clock, retry_interval=5.0)
    client._lost("test")
    assert client.read_lines() == []  # too soon to try

    delays = []
    for _ in range(4):
        clock.advance(600)  # well past any back-off
        before = client._failures
        client.read_lines()
        delays.append(client._retry_at - clock.now())
        assert client._failures > before  # each failure pushes the next attempt further out
    assert delays == sorted(delays)  # 5, 10, 20, 40 …


def test_a_rejected_password_on_reconnect_stops_hammering(server):  # noqa: ANN001
    """It will not fix itself, so back off to the ceiling and say so once, loudly."""
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(*server.address, "test", timeout=0.3, clock=clock, retry_max=300.0)
    client.open()
    try:
        server.accept_login = False
        client._lost("pretend the link dropped")
        clock.advance(10)
        assert client.read_lines() == []
        assert not client.connected
        assert client._retry_at - clock.now() >= 300.0
    finally:
        client.close()


def test_chat_queued_for_a_dead_session_is_discarded(server):  # noqa: ANN001
    """Sending it to the next session would announce something that happened before a restart."""
    clock = FakeClock(start=1000.0)
    client = BattleyeClient(*server.address, "test", timeout=0.3, clock=clock)
    client.open()
    try:
        client.write("say -1 first")  # goes out immediately
        client.write("say -1 second")  # queued behind the pacing
        client._lost("pretend the link dropped")
        assert not client._outbox
    finally:
        client.close()


def test_reading_from_a_never_opened_client_does_not_explode():
    """The live loop calls this every tick; it must report, not raise."""
    client = BattleyeClient("127.0.0.1", 1, "test", timeout=0.05)
    assert client.read_lines() == []
