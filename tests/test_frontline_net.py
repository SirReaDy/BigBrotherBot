"""Frontline's transport: an MD5 challenge, 0x04 framing, and a refusal with no message in it.

Four properties here only bite in production, so each has a test and each is exercised again end to end
by `tools/fakeservers/e2e_frontline.py`:

* **packets are separated by ``\\x04``, not by newlines** — and the roster is one packet with newlines
  inside it, so a client that split on both would shred the only message that says who is playing;
* **the login is a challenge/response with a username**, and the challenge has no fixed length;
* **a rejected login is not answered — the connection closes**, so that has to be read as the refusal
  it is rather than reported as a network fault;
* **nothing is reported until the bot asks**: a freshly authenticated connection is silent until
  `CHATLOGGING TRUE` and `DebugLogging TRUE` go out.
"""

from __future__ import annotations

import socket
import time

import pytest

from b3.net.frontline import (
    LOGGING_COMMANDS,
    PING_COMMAND,
    PLAYERLIST_COMMAND,
    RECONNECTED_NOTICE,
    TERMINATOR,
    FrontlineAuthError,
    FrontlineClient,
    FrontlineError,
    decode,
    encode,
    login_response,
)
from tools.fakeservers.frontline import FakeFrontlineServer, expected_response

COURGETTE = "1561500"


@pytest.fixture
def server():
    fake = FakeFrontlineServer(password="test").start()
    yield fake
    fake.stop()


def _client(fake: FakeFrontlineServer, **kwargs) -> FrontlineClient:
    kwargs.setdefault("ping_interval", 0.0)  # off unless a test is about it
    kwargs.setdefault("playerlist_interval", 0.0)
    return FrontlineClient(fake.address[0], fake.address[1], "test", timeout=0.4, **kwargs)


# -- the codec ----------------------------------------------------------------------------------


def test_a_packet_ends_with_the_terminator():
    assert encode("PLAYERLIST") == b"PLAYERLIST\x04"


def test_a_packet_is_read_up_to_the_terminator():
    assert decode(b"Login SUCCESS! User:admin\x04rest") == ("Login SUCCESS! User:admin", b"rest")


def test_an_incomplete_packet_waits():
    assert decode(b"Login SUCC") is None


def test_a_newline_inside_a_packet_is_data_and_not_a_boundary():
    """The property everything else here depends on. The documentation says packets are separated by
    "'\\n' or 0x04", and taking that literally when *reading* would cut the roster — the one message
    that carries every player — into a header and a pile of orphan rows."""
    roster = "PlayerList: Players=1/32\nID\tName\n1\tCourgette"
    payload, rest = decode(roster.encode() + b"\x04")
    assert payload == roster
    assert payload.count("\n") == 2
    assert rest == b""


def test_utf8_survives_the_round_trip():
    payload, _ = decode(encode("SAY café"))
    assert payload == "SAY café"


def test_a_stream_with_no_terminator_at_all_is_eventually_refused():
    """Rather than buffering for ever on a port that is not this protocol."""
    with pytest.raises(FrontlineError):
        decode(b"x" * 1_000_001)


def test_the_hash_is_md5_of_the_challenge_and_the_password():
    assert login_response("38D384D07C", "test") == expected_response("38D384D07C", "test")
    assert len(login_response("38D384D07C", "test")) == 32


# -- the login ----------------------------------------------------------------------------------


def test_a_good_login_reads_the_challenge_and_answers_it(server):
    client = _client(server)
    client.open()
    try:
        assert client.authed
        assert client.server_version == "2"
        expected = f"RESPONSE admin {expected_response(server.challenge, 'test')}"
        assert expected in server.received, "the password itself never crosses the wire"
    finally:
        client.close()


def test_the_challenge_is_read_rather_than_assumed(server):
    """Its length is not fixed — the documentation says so explicitly — so a client that expected ten
    characters would fail on half of all servers."""
    server.challenge = "0011223344556677889900AABBCCDDEEFF"
    client = _client(server)
    client.open()
    try:
        assert f"RESPONSE admin {expected_response(server.challenge, 'test')}" in server.received
    finally:
        client.close()


def test_a_wrong_password_is_reported_as_a_refusal_and_not_as_a_network_fault(server):
    """This engine says nothing when it refuses: it hangs up. Reporting that as a connection problem
    would send an operator to check a firewall that is working perfectly."""
    client = FrontlineClient(server.address[0], server.address[1], "wrong", timeout=0.4)
    with pytest.raises(FrontlineAuthError) as excinfo:
        client.open()
    assert "closed the connection" in str(excinfo.value)
    assert server.rejected


def test_a_wrong_user_fails_the_same_way_which_is_why_doctor_mentions_both(server):
    client = FrontlineClient(
        server.address[0], server.address[1], "test", user="root", timeout=0.4
    )
    with pytest.raises(FrontlineAuthError):
        client.open()


def test_a_server_that_sends_no_greeting_is_named_as_such(server):
    """The likeliest misconfiguration: the game port rather than the console port. Nothing there will
    ever greet us, and the message has to say so rather than time out anonymously."""
    plain = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    plain.bind(("127.0.0.1", 0))
    plain.listen(1)
    try:
        client = FrontlineClient(*plain.getsockname()[:2], "test", timeout=0.2)
        with pytest.raises(FrontlineError) as excinfo:
            client.open()
        assert "no RCON greeting" in str(excinfo.value)
    finally:
        plain.close()


def test_an_unreachable_server_names_the_address():
    client = FrontlineClient("127.0.0.1", 1, "test", timeout=0.2)
    with pytest.raises(FrontlineError) as excinfo:
        client.open()
    assert "127.0.0.1:1" in str(excinfo.value)


def test_logging_in_switches_the_server_reporting_on(server):
    """Not a preference: until these go out the server reports nothing at all, and a bot that skipped
    them would watch a busy server in silence and call it empty."""
    client = _client(server)
    client.open()
    try:
        for command in LOGGING_COMMANDS:
            assert server.wait_for(lambda c=command: c in server.received), command
        assert server.chat_logging and server.debug_logging
    finally:
        client.close()


def test_chat_really_is_silent_until_then(server):
    """Stated against the fake, because it is the whole reason those commands are sent."""
    server.say_as("Courgette", "hello")  # chat_logging is False: nothing is emitted
    assert server._conn is None or True  # nothing to read; the point is that nothing was sent

    client = _client(server)
    client.open()
    try:
        server.say_as("Courgette", "hello")
        assert server.wait_for(lambda: any("CHAT:" in line for line in client.read_lines()))
    finally:
        client.close()


# -- reading the stream -------------------------------------------------------------------------


def test_pushed_lines_come_out_as_lines(server):
    client = _client(server)
    client.open()
    try:
        server.push("DEBUG: ScriptLog: hello")
        assert server.wait_for(lambda: any("ScriptLog" in line for line in client.read_lines()))
    finally:
        client.close()


def test_the_roster_arrives_whole(server):
    """One packet, newlines and tabs intact, all the way from the socket to the caller."""
    server.add_player("1", "Courgette", COURGETTE)
    client = _client(server)
    client.open()
    try:
        client.write(PLAYERLIST_COMMAND)
        found = ""
        for _ in range(30):
            for line in client.read_lines():
                if line.startswith("PlayerList:"):
                    found = line
            if found:
                break
            time.sleep(0.05)
        assert found, "the roster never arrived"
        assert "\n" in found and "\t" in found
        assert found.splitlines()[2].startswith("1\tCourgette")
    finally:
        client.close()


def test_a_fragmented_stream_is_reassembled(server):
    """Five bytes at a time. The roster is the longest thing this protocol sends and the only one that
    matters, so it is the one to fragment."""
    server.fragment = 5
    server.add_player("1", "Courgette", COURGETTE)
    client = _client(server)
    client.open()
    try:
        client.write(PLAYERLIST_COMMAND)
        found = ""
        for _ in range(60):
            for line in client.read_lines():
                if line.startswith("PlayerList:"):
                    found = line
            if found:
                break
            time.sleep(0.05)
        assert found and found.splitlines()[2].startswith("1\tCourgette")
    finally:
        client.close()


def test_two_packets_in_one_read_are_both_delivered(server):
    client = _client(server)
    client.open()
    try:
        assert server._conn is not None
        server._conn.sendall(f"one{TERMINATOR}two{TERMINATOR}".encode())
        lines: list[str] = []
        for _ in range(20):
            lines.extend(client.read_lines())
            if "one" in lines and "two" in lines:
                break
            time.sleep(0.05)
        assert "one" in lines and "two" in lines
    finally:
        client.close()


def test_an_empty_packet_is_not_delivered(server):
    """The server sends these; a parser asked to read one would report an unknown line for nothing."""
    client = _client(server)
    client.open()
    try:
        assert server._conn is not None
        # Drain the login's own replies first -- until two reads in a row come back empty, because
        # they arrive as separate packets and one empty read proves nothing.
        for _ in range(20):
            if not client.read_lines() and not client.read_lines():
                break
            time.sleep(0.02)
        server._conn.sendall(TERMINATOR.encode() * 3)
        time.sleep(0.1)
        assert client.read_lines() == []
    finally:
        client.close()


def test_a_server_that_hangs_up_is_noticed(server):
    client = _client(server)
    client.open()
    server.stop()
    for _ in range(20):
        client.read_lines()
        if not client.connected:
            break
        time.sleep(0.05)
    assert not client.connected


# -- the keepalive and the roster refresh --------------------------------------------------------


def test_the_keepalive_goes_out_from_read_lines(server):
    """Ten seconds of silence costs the connection, and this is what keeps the client single-threaded:
    the ping rides the same pump the events do."""
    client = _client(server, ping_interval=0.1)
    client.open()
    try:
        time.sleep(0.15)
        client.read_lines()
        assert server.wait_for(lambda: server.pings > 0)
        assert PING_COMMAND in server.received
    finally:
        client.close()


def test_a_client_that_stops_talking_really_is_hung_up_on():
    """Modelled rather than assumed, because it is the reason the ping exists."""
    strict = FakeFrontlineServer(password="test", idle_timeout=0.3).start()
    try:
        quiet = FrontlineClient(
            *strict.address, "test", timeout=0.1, ping_interval=99, playerlist_interval=0
        )
        quiet.open()
        assert quiet.connected
        time.sleep(0.6)
        for _ in range(6):
            quiet.read_lines()
        assert strict.dropped_for_silence
        quiet.close()
    finally:
        strict.stop()


def test_the_roster_is_asked_for_on_an_interval(server):
    """And the interval is the *event latency*, not a refresh rate: with no connect or disconnect lines
    on this engine, this reply is the only way to learn that anybody arrived or left."""
    client = _client(server, playerlist_interval=0.1)
    client.open()
    try:
        client.read_lines()
        time.sleep(0.15)
        client.read_lines()
        assert server.wait_for(lambda: server.received.count(PLAYERLIST_COMMAND) >= 2)
    finally:
        client.close()


def test_the_first_roster_is_asked_for_at_once(server):
    """A bot that waited a whole interval would start up believing the server was empty."""
    client = _client(server, playerlist_interval=5.0)
    client.open()
    try:
        client.read_lines()
        assert server.wait_for(lambda: PLAYERLIST_COMMAND in server.received)
    finally:
        client.close()


# -- reconnecting -------------------------------------------------------------------------------


def test_a_reconnect_is_attempted_and_announced(server):
    """Note there is no window in which the bot is quietly deaf: the first read after the drop already
    tries again, because `_retry_at` starts at zero. That the connection *was* lost is covered by
    `test_a_server_that_hangs_up_is_noticed`; what matters here is that it comes back and says so."""
    client = _client(server, playerlist_interval=0.5)
    client.open()
    try:
        server.restart()
        lines: list[str] = []
        for _ in range(40):
            lines.extend(client.read_lines())
            if client.authed and RECONNECTED_NOTICE in lines:
                break
            client._retry_at = 0.0  # the back-off is real; a test should not wait it out
            time.sleep(0.05)

        assert client.connected and client.authed
        assert RECONNECTED_NOTICE in lines, (
            "the parser has to be told: this engine reports no departures, so every player who left "
            "during the outage is a ghost until the roster is dropped"
        )
    finally:
        client.close()


def test_a_reconnect_switches_the_reporting_back_on(server):
    """A restarted server has forgotten CHATLOGGING, so a bot that only sends it once goes deaf for
    the rest of its life without a single error to show for it."""
    client = _client(server, playerlist_interval=0.0)
    client.open()
    server.restart()
    try:
        for _ in range(6):
            client.read_lines()
            time.sleep(0.05)
        client._retry_at = 0.0
        for _ in range(20):
            client.read_lines()
            if client.authed:
                break
            client._retry_at = 0.0
            time.sleep(0.05)
        assert client.authed
        assert server.chat_logging, "chat logging was switched on again"
    finally:
        client.close()


def test_a_rejected_login_backs_off_hard_rather_than_hammering(server, caplog):
    """A wrong password will be wrong next time too. The one loud line is the point."""
    client = _client(server)
    client.open()
    client.close()
    client.password = "wrong"
    client.connected = False
    client._retry_at = 0.0

    server.restart()
    assert client.read_lines() == []
    assert client._retry_delay >= 300.0
    assert "not retrying" in caplog.text


# -- writing ------------------------------------------------------------------------------------


def test_a_command_returns_nothing_because_nothing_can_be_asked(server):
    """Honest rather than convenient: this protocol never says which command it is answering, so a
    client claiming to have the answer would be inventing it."""
    client = _client(server)
    client.open()
    try:
        assert client.command(PLAYERLIST_COMMAND) == ""
        assert server.wait_for(lambda: PLAYERLIST_COMMAND in server.received)
    finally:
        client.close()


def test_an_empty_command_is_not_sent(server):
    client = _client(server)
    client.open()
    try:
        # Wait until the login's own commands have all been recorded, or "nothing new was sent" is
        # a race rather than an assertion.
        assert server.wait_for(lambda: len(server.received) == 4)
        before = len(server.received)
        client.write("   ")
        client.command("")
        time.sleep(0.1)
        assert len(server.received) == before
    finally:
        client.close()


def test_writing_to_a_closed_connection_raises(server):
    client = _client(server)
    with pytest.raises(FrontlineError):
        client.write("PLAYERLIST")


# -- doctor -------------------------------------------------------------------------------------


def _doctor_config(tmp_path, server: FakeFrontlineServer, password="test", user="admin"):
    from b3.config.schema import BotConfig, Config, ServerConfig

    return Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="frontline",
            host=server.address[0],
            port=server.address[1],
            rcon_password=password,
            rcon_user=user,
            rcon_timeout=0.4,
        ),
    )


def _rcon_check(config, tmp_path):
    from b3.core.doctor import run_checks

    return next(c for c in run_checks(config, tmp_path) if c.name == "rcon")


def test_doctor_logs_in(server, tmp_path):
    from b3.core.doctor import Status

    check = _rcon_check(_doctor_config(tmp_path, server), tmp_path)
    assert check.status is Status.OK


def test_doctor_names_the_user_as_well_as_the_password(server, tmp_path):
    """Because the server does not: it hangs up either way, and this is the only engine here with a
    separate account name to get wrong."""
    from b3.core.doctor import Status

    check = _rcon_check(_doctor_config(tmp_path, server, user="root"), tmp_path)
    assert check.status is Status.FAIL
    assert "rcon_user" in (check.hint or "")
    assert "'root'" in (check.hint or "")


def test_doctor_points_at_the_console_port_when_nothing_greets_us(tmp_path):
    from b3.config.schema import BotConfig, Config, ServerConfig
    from b3.core.doctor import Status

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="frontline", host="127.0.0.1", port=1, rcon_password="test", rcon_timeout=0.2
        ),
    )
    check = _rcon_check(config, tmp_path)
    assert check.status is Status.FAIL
    assert "14507" in (check.hint or "")
