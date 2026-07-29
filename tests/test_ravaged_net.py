"""Ravaged's admin protocol: text length prefixes, a two-step login, and a server that blacklists.

Four properties here are the kind that only bite in production, so each has a test and each is
exercised again end to end by `tools/fakeservers/e2e_ravaged.py`:

* the two directions are **not** the same shape — bare lines out, `(<size>)payload` back;
* a reply and a pushed event travel identically and are told apart by *content*;
* the server echoes the **bare verb** when it answers, and the login verdict is the body of a
  `pass:` reply rather than a message of its own;
* **repeated bad passwords earn a blacklist**, not a refusal — so a failed login is never retried.
"""

from __future__ import annotations

import time

import pytest

from b3.net.ravaged import (
    BLACKLISTED,
    HANDSHAKE_COMMAND,
    LOGIN_SUCCESS,
    NOT_SUPERUSER,
    UNKNOWN_COMMAND,
    RavagedAuthError,
    RavagedBlacklistedError,
    RavagedClient,
    RavagedError,
    decode,
    is_event,
    login_hash,
    reply_body,
    reply_name,
)
from tools.fakeservers.ravaged import FakeRavagedServer, expected_login

COURGETTE = "12312312312312312"


@pytest.fixture
def server():
    fake = FakeRavagedServer(password="test").start()
    yield fake
    fake.stop()


def _client(fake: FakeRavagedServer, **kwargs) -> RavagedClient:
    kwargs.setdefault("playerlist_interval", 0.0)  # off unless a test is about it
    return RavagedClient(fake.address[0], fake.address[1], "test", timeout=0.5, **kwargs)


# -- the codec ----------------------------------------------------------------------------------


def test_a_frame_is_a_decimal_length_in_brackets():
    assert decode(b"(5)hello") == ("hello", b"")


def test_an_incomplete_frame_waits_rather_than_guessing():
    assert decode(b"(10)hel") is None
    assert decode(b"(10") is None
    assert decode(b"") is None


def test_two_frames_in_one_read_are_both_recovered():
    payload, rest = decode(b"(2)hi(3)bye")
    assert payload == "hi"
    assert decode(rest) == ("bye", b"")


def test_junk_before_the_bracket_is_discarded():
    """The classic client's rule, and the only recovery this framing has: there is no checksum, so
    the opening bracket is the sole landmark once the stream is out of step."""
    assert decode(b"garbage(2)hi") == ("hi", b"")


def test_a_header_that_is_not_a_length_is_a_protocol_error():
    with pytest.raises(RavagedError):
        decode(b"(abc)hi")


def test_an_implausible_length_is_refused_rather_than_buffered_forever():
    with pytest.raises(RavagedError):
        decode(b"(99999999)x")


def test_utf8_is_measured_in_bytes_not_characters():
    raw = "café".encode()
    assert decode(f"({len(raw)})".encode() + raw) == ("café", b"")


# -- telling a reply from an event --------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "(127.0.0.1:3508 has connected remotely)",
        "RCon:(Admin127.0.0.1:3508 has disconnected from RCon)",
        '"courgette<12312312312312312><0>"disconnected',
        "Round started",
    ],
)
def test_these_are_events(payload):
    assert is_event(payload)


@pytest.mark.parametrize(
    "payload",
    [
        "getplayerlist:1 players:\ncourgette 21 pts 4:8 38ms steamid: 12312312312312312\n",
        "kick:",
        "pass:Login success as admin",
        NOT_SUPERUSER,
    ],
)
def test_these_are_replies(payload):
    assert not is_event(payload)


def test_a_chat_line_containing_a_colon_is_still_an_event():
    """The ambiguity this classification has to survive: a player can type a colon."""
    assert is_event('"courgette<12312312312312312><1>" say "hi: there"')


# -- what the server echoes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sent", "echoed"),
    [
        ("getplayerlist", "getplayerlist"),
        ('kick 12312312312312312 "spam"', "kick"),
        ("getmaplist false", "getmaplist"),
        ("LOGIN=admin", "login"),
        ("PASS=DEADBEEF", "pass"),
    ],
)
def test_the_echoed_name_is_the_bare_verb(sent, echoed):
    """Not the whole command. See the module docstring: the classic client's reply pattern rejects a
    name containing a space, so a full echo could never have been matched to its command."""
    assert reply_name(sent) == echoed


def test_the_login_verdict_is_read_out_of_a_reply_or_bare():
    assert reply_body("pass:Login success as admin") == "Login success as admin"
    assert reply_body("Login success as admin") == "Login success as admin"


def test_the_password_is_uppercase_sha1_with_no_spaces_in_it():
    """Unlike Homefront's, which is space-separated pairs. Same algorithm, different presentation."""
    digest = login_hash("test")
    assert digest == digest.upper() and " " not in digest
    assert digest == expected_login("test")


# -- the login ----------------------------------------------------------------------------------


def test_a_good_password_logs_in_and_proves_commands_work(server):
    client = _client(server)
    client.open()
    try:
        assert client.authed
        assert server.sent_command("LOGIN=admin")
        assert server.sent_command(f"PASS={expected_login('test')}")
        assert server.sent_command(HANDSHAKE_COMMAND), (
            "an accepted password is not proof that commands are carried"
        )
    finally:
        client.close()


def test_the_user_step_going_unanswered_is_expected_rather_than_a_fault(server):
    """The real server frequently does not answer `LOGIN=`. The classic client waited out a timeout
    there and carried on, so a client that insists on an answer never gets in."""
    client = _client(server)
    client.open()
    client.close()
    assert "LOGIN=admin" in server.received


def test_a_wrong_password_is_refused_and_never_retried(server):
    """The retry is what matters here: a second attempt on a real server is a step towards a
    blacklist, so being wrong once must not become being locked out."""
    client = RavagedClient(server.address[0], server.address[1], "wrong", timeout=0.5)
    with pytest.raises(RavagedAuthError):
        client.open()

    assert len([c for c in server.received if c.startswith("PASS=")]) == 1
    assert not server.blacklisted
    assert not client.connected


def test_being_blacklisted_is_reported_as_its_own_kind_of_failure(server):
    """Because the answer differs: a refusal wants the password fixed, a blacklist wants the bot to
    stop connecting until the server forgets."""
    server.blacklisted = True
    client = RavagedClient(server.address[0], server.address[1], "wrong", timeout=0.5)
    with pytest.raises(RavagedBlacklistedError) as excinfo:
        client.open()
    assert "blacklisted" in str(excinfo.value)


def test_a_blacklist_is_not_mistaken_for_a_plain_refusal(server):
    """`RavagedBlacklistedError` is a subclass, so code catching the general case still works — but
    anything that wants to tell them apart can."""
    assert issubclass(RavagedBlacklistedError, RavagedAuthError)
    server.blacklisted = True
    client = RavagedClient(server.address[0], server.address[1], "wrong", timeout=0.5)
    with pytest.raises(RavagedAuthError):
        client.open()


def test_repeated_failures_are_what_earn_the_blacklist(server):
    """The server rule this client is written around, driven by hand: **attempts** are what count, so
    a bot that retried a rejection would walk itself into a lockout in three tries.

    Sent on one socket because the real server, like this fake, takes one admin connection at a time —
    which is also why a supervisor restarting the bot in a loop is the dangerous shape here.
    """
    import socket

    server.max_attempts = 3
    sock = socket.create_connection(server.address, timeout=1.0)
    try:
        for _ in range(3):
            sock.sendall(f"PASS={login_hash('wrong')}\n".encode())
            time.sleep(0.05)
        assert server.wait_for(lambda: server.blacklisted)
    finally:
        sock.close()


def test_a_login_that_is_accepted_but_cannot_carry_commands_is_refused(server):
    """The handshake exists for the case where the password is right and the connection is useless.
    Modelled by a server that answers `testrcon` with something other than the refusal."""
    original = server._execute

    def broken(command: str) -> None:
        if command == HANDSHAKE_COMMAND:
            server._reply(command, "who knows")
            return
        original(command)

    server._execute = broken  # type: ignore[method-assign]
    client = _client(server)
    with pytest.raises(RavagedError) as excinfo:
        client.open()
    assert "cannot be trusted" in str(excinfo.value)


def test_an_unreachable_server_names_the_address(server):
    client = RavagedClient("127.0.0.1", 1, "test", timeout=0.2)
    with pytest.raises(RavagedError) as excinfo:
        client.open()
    assert "127.0.0.1:1" in str(excinfo.value)


# -- commands -----------------------------------------------------------------------------------


def test_a_reply_is_matched_to_the_command_that_asked_for_it(server):
    server.add_player(COURGETTE, "courgette")
    client = _client(server)
    client.open()
    try:
        reply = client.command("getplayerlist")
        assert reply.startswith("1 players:")
        assert "steamid: 12312312312312312" in reply
    finally:
        client.close()


def test_a_multi_word_command_gets_its_reply(server):
    """The case a full-command echo would break: the server answers `kick:`, not `kick 123 "x":`."""
    server.add_player(COURGETTE, "courgette")
    client = _client(server)
    client.open()
    try:
        client.command(f'kick {COURGETTE} "spam"')
        assert server.wait_for(lambda: COURGETTE not in server.players)
    finally:
        client.close()


def test_events_arriving_while_a_reply_is_awaited_are_kept(server):
    """A kill that happens during a `!status` is still a kill. Dropping them would make the bot lose
    events in proportion to how much an admin used it."""
    client = _client(server)
    client.open()
    try:
        server.push("Round started")
        server.add_player(COURGETTE, "courgette")
        client.command("getplayerlist")
        lines = client.read_lines()
        for _ in range(10):
            if any("entered the game" in line for line in lines):
                break
            time.sleep(0.05)
            lines += client.read_lines()
        assert any("Round started" in line for line in lines)
        assert any("entered the game" in line for line in lines)
    finally:
        client.close()


def test_a_command_sent_before_the_login_is_reported_not_swallowed(server):
    client = _client(server)
    # Connect by hand, skipping the login, which is the state a half-open connection is in.
    import socket

    client._sock = socket.create_connection(server.address, timeout=0.5)
    client.connected = True
    with pytest.raises(RavagedError) as excinfo:
        client.command("getplayerlist")
    assert NOT_SUPERUSER in str(excinfo.value)
    client.close()


def test_an_empty_command_is_not_sent(server):
    client = _client(server)
    client.open()
    try:
        before = len(server.received)
        assert client.command("   ") == ""
        client.write("")
        assert len(server.received) == before
    finally:
        client.close()


def test_a_command_on_a_closed_connection_raises_rather_than_returning_nothing(server):
    client = _client(server)
    with pytest.raises(RavagedError):
        client.command("getplayerlist")


def test_a_reply_that_never_comes_gives_up_and_says_so(server, caplog):
    """No reply is not the same as an empty reply, and a bot that cannot tell them apart reports a
    server as empty when it is merely quiet."""
    client = _client(server, command_timeout=0.3)
    client.open()
    try:
        server._execute = lambda command: None  # type: ignore[method-assign]
        assert client.command("getplayerlist") == ""
        assert "no reply" in caplog.text
    finally:
        client.close()


# -- reading the stream -------------------------------------------------------------------------


def test_pushed_events_come_out_as_lines(server):
    client = _client(server)
    client.open()
    try:
        server.push("Round started")
        assert server.wait_for(lambda: "Round started" in client.read_lines())
    finally:
        client.close()


def test_replies_do_not_come_out_as_lines(server):
    """Otherwise the parser would see `testrcon:Command not found…` as a game-log line, and every
    command the bot sends would turn into an unrecognised line in the log."""
    client = _client(server)
    client.open()
    try:
        client.command("getmaplist false")
        time.sleep(0.1)
        lines = client.read_lines()
        assert not any("getmaplist" in line for line in lines)
        assert not any(UNKNOWN_COMMAND in line for line in lines)
    finally:
        client.close()


def test_a_fragmented_stream_is_reassembled(server):
    """Three bytes at a time — the framing is length-prefixed, and TCP has no obligation to deliver
    a frame in one read."""
    server.fragment = 3
    client = _client(server)
    client.open()
    try:
        server.push('"courgette<12312312312312312><1>" say "<FONT COLOR=\'#FF0000\'> hi"')
        assert server.wait_for(
            lambda: any("say" in line for line in client.read_lines()), timeout=4
        )
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


def test_reading_a_dead_connection_still_hands_over_what_was_buffered(server):
    """Events read just before the connection dropped are as real as any others — a disconnect that
    was already on the wire still has to reach the roster, or the player stays connected forever."""
    client = _client(server)
    client.open()
    try:
        server.push('"courgette<12312312312312312><1>"disconnected')
        assert server.wait_for(lambda: bool(client.read_lines()) or not client.connected)
        client._pending = ["Round started"]
        client.connected = False
        assert client.read_lines() == ["Round started"]
    finally:
        client.close()


# -- the roster refresh -------------------------------------------------------------------------


def test_the_roster_is_asked_for_on_an_interval_and_pushed_into_the_stream(server):
    """The engine answers questions, but scores and pings appear nowhere in the log — so the client
    asks on a timer and hands the reply over as though the server had volunteered it. The parser then
    reads it with the same router it reads everything else."""
    server.add_player(COURGETTE, "courgette")
    client = _client(server, playerlist_interval=0.2)
    client.open()
    try:
        client.read_lines()  # drains the join lines, and starts the clock
        time.sleep(0.25)
        lines = client.read_lines()
        assert any(line.startswith("1 players:") for line in lines)
    finally:
        client.close()


def test_the_refresh_does_not_fire_on_every_read(server):
    server.add_player(COURGETTE, "courgette")
    client = _client(server, playerlist_interval=30.0)
    client.open()
    try:
        before = len([c for c in server.received if c == "getplayerlist"])
        for _ in range(3):
            client.read_lines()
        after = len([c for c in server.received if c == "getplayerlist"])
        assert after == before
    finally:
        client.close()


def test_the_refresh_can_be_turned_off(server):
    client = _client(server, playerlist_interval=0.0)
    client.open()
    try:
        time.sleep(0.1)
        client.read_lines()
        assert not any(c == "getplayerlist" for c in server.received)
    finally:
        client.close()


def test_events_seen_while_refreshing_stay_in_front_of_the_roster(server):
    """Order matters: a disconnect reported *after* a roster that still listed the player would look
    like they were still there."""
    server.add_player(COURGETTE, "courgette")
    client = _client(server, playerlist_interval=0.15)
    client.open()
    try:
        client.read_lines()
        client._pending = ['"courgette<12312312312312312><1>"disconnected']
        time.sleep(0.2)
        lines = client.read_lines()
        disconnect = next(i for i, line in enumerate(lines) if "disconnected" in line)
        roster = next(i for i, line in enumerate(lines) if line.startswith("1 players:"))
        assert disconnect < roster
    finally:
        client.close()


def test_a_failed_refresh_does_not_take_the_read_down_with_it(server):
    """A refresh is a convenience. If it fails the events still have to be delivered, because those
    are the bot's actual input."""
    client = _client(server, playerlist_interval=0.1)
    client.open()
    try:
        client.read_lines()
        server._execute = lambda command: None  # type: ignore[method-assign]
        client.command_timeout = 0.2
        time.sleep(0.15)
        server.push("Round started")
        found = False
        for _ in range(20):
            if "Round started" in client.read_lines():
                found = True
                break
            time.sleep(0.05)
        assert found
    finally:
        client.close()


# -- doctor -------------------------------------------------------------------------------------


def _doctor_config(tmp_path, server: FakeRavagedServer, password: str = "test"):
    from b3.config.schema import BotConfig, Config, ServerConfig

    return Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="ravaged",
            host=server.address[0],
            port=server.address[1],
            rcon_password=password,
            rcon_timeout=0.5,
        ),
    )


def _rcon_check(config, tmp_path):
    from b3.core.doctor import run_checks

    return next(c for c in run_checks(config, tmp_path) if c.name == "rcon")


def test_doctor_logs_in_and_says_commands_are_answered(server, tmp_path):
    from b3.core.doctor import Status

    check = _rcon_check(_doctor_config(tmp_path, server), tmp_path)
    assert check.status is Status.OK


def test_doctor_warns_about_the_blacklist_before_the_operator_retries(server, tmp_path):
    """The whole point of a separate hint for this family: the natural response to "wrong password"
    is to try again, and on this engine trying again is how an address gets locked out."""
    from b3.core.doctor import Status

    check = _rcon_check(_doctor_config(tmp_path, server, password="wrong"), tmp_path)
    assert check.status is Status.FAIL
    assert "blacklisted" in (check.hint or "")


def test_doctor_tells_a_blacklisted_operator_to_stop_connecting(server, tmp_path):
    from b3.core.doctor import Status

    server.blacklisted = True
    check = _rcon_check(_doctor_config(tmp_path, server, password="wrong"), tmp_path)
    assert check.status is Status.FAIL
    assert "wait for the server to forget" in (check.hint or "")


def test_doctor_points_at_the_admin_port_when_nothing_answers(tmp_path):
    from b3.config.schema import BotConfig, Config, ServerConfig
    from b3.core.doctor import Status

    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="ravaged", host="127.0.0.1", port=1, rcon_password="test", rcon_timeout=0.2
        ),
    )
    check = _rcon_check(config, tmp_path)
    assert check.status is Status.FAIL
    assert "13550" in (check.hint or ""), "the admin port is not the game port, and differs by more"


# -- the fake's own claims ----------------------------------------------------------------------


def test_the_fake_speaks_the_login_verdict_as_a_reply(server):
    """Stated as a test so the fake cannot quietly drift back to sending it bare — which is the
    shape that made this family's login look fine while being unable to log into a real server."""
    client = _client(server)
    client.open()
    client.close()
    # Nothing to assert on the wire after the fact, so assert the property directly.
    assert server._reply_name("PASS=DEADBEEF") == "pass"
    assert is_event(f"pass:{LOGIN_SUCCESS}admin") is False
    assert is_event(BLACKLISTED) is True, "and bare, it would be read as an event"
