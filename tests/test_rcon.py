"""RCON: wire framing, retry policy, status cache, bytes<->str boundary."""

from __future__ import annotations

import select
import socket
import threading
import time

import pytest

from b3.core.clock import FakeClock
from b3.net.rcon import Quake3Dialect, Rcon, RconError, UdpRconTransport


class FakeTransport:
    """Records outgoing payloads, returns a canned reply, can fail a few times first."""

    def __init__(self, response: bytes = b"", fail_times: int = 0) -> None:
        self.response = response
        self.fail_times = fail_times
        self.payloads: list[bytes] = []
        self.calls = 0
        self.writes: list[bytes] = []

    def request(self, payload: bytes) -> bytes:
        self.calls += 1
        self.payloads.append(payload)
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RconError("transient")
        return self.response

    def send(self, payload: bytes) -> None:
        self.writes.append(payload)
        self.payloads.append(payload)

    def close(self) -> None:
        pass


class OldTransport:
    """A transport from before `send` existed, to prove the fallback path."""

    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def request(self, payload: bytes) -> bytes:
        self.payloads.append(payload)
        return b""

    def close(self) -> None:
        pass


# --- dialect --------------------------------------------------------------


def test_dialect_command_framing():
    d = Quake3Dialect()
    assert d.encode_command("pw", "say hi", "latin-1") == b'\xff\xff\xff\xffrcon "pw" say hi\n'


def test_dialect_query_framing():
    d = Quake3Dialect()
    assert d.encode_query("getstatus", "latin-1") == b"\xff\xff\xff\xffgetstatus\n"


def test_dialect_strips_reply_header():
    d = Quake3Dialect()
    raw = b"\xff\xff\xff\xffprint\nmap: mp_crash"
    assert d.strip_reply(raw, "latin-1") == "map: mp_crash"


def test_dialect_reply_without_header_untouched():
    d = Quake3Dialect()
    assert d.strip_reply(b"plain reply", "latin-1") == "plain reply"


# --- orchestrator ---------------------------------------------------------


def test_command_builds_payload_and_strips_reply():
    t = FakeTransport(response=b"\xff\xff\xff\xffprint\npong")
    rcon = Rcon(t, password="secret")
    reply = rcon.command("ping")
    assert reply == "pong"
    assert t.payloads[-1] == b'\xff\xff\xff\xffrcon "secret" ping\n'


def test_say_and_set_cvar_helpers():
    t = FakeTransport(response=b"")
    rcon = Rcon(t, password="pw")
    rcon.say("hello world")
    assert t.payloads[-1] == b'\xff\xff\xff\xffrcon "pw" say hello world\n'
    rcon.set_cvar("g_gametype", "dm")
    assert t.payloads[-1] == b'\xff\xff\xff\xffrcon "pw" set g_gametype "dm"\n'


def test_retry_then_succeed():
    t = FakeTransport(response=b"ok", fail_times=1)
    rcon = Rcon(t, password="pw", max_retries=2, retry_delay=0)
    assert rcon.command("status") == "ok"
    assert t.calls == 2  # first failed, second succeeded


def test_quit_is_never_retried():
    t = FakeTransport(fail_times=5)
    rcon = Rcon(t, password="pw", max_retries=3, retry_delay=0)
    with pytest.raises(RconError):
        rcon.command("quit")
    assert t.calls == 1  # no retry for quit/map


def test_map_change_is_never_retried():
    t = FakeTransport(fail_times=5)
    rcon = Rcon(t, password="pw", max_retries=3, retry_delay=0)
    with pytest.raises(RconError):
        rcon.command("map mp_crash")
    assert t.calls == 1


def test_status_cache():
    clock = FakeClock(start=1_000.0)
    t = FakeTransport(response=b"\xff\xff\xff\xffprint\nplayers")
    rcon = Rcon(t, password="pw", clock=clock, status_cache_ttl=5.0)

    assert rcon.get_status() == "players"
    assert rcon.get_status() == "players"
    assert t.calls == 1  # second call served from cache

    clock.advance(10)  # past TTL
    rcon.get_status()
    assert t.calls == 2  # cache expired -> queried again


def test_encoding_boundary_non_ascii():
    # A latin-1 player name with a byte > 127 must round-trip without raising.
    t = FakeTransport(response=b"\xff\xff\xff\xffprint\nRen\xe9")  # 'René' in latin-1
    rcon = Rcon(t, password="pw", encoding="latin-1")
    assert rcon.command("status") == "René"


# --- fire-and-forget vs round trip ----------------------------------------
#
# The distinction this section defends: a reply-waiting send costs the socket's settle window every
# time (there is no length prefix in this protocol, so "the reply has finished" only means "the
# socket went quiet"). Chat is the bot's most frequent traffic and needs no reply; penalties do.


def test_write_does_not_wait_for_a_reply():
    t = FakeTransport(response=b"\xff\xff\xff\xffprint\n")
    rcon = Rcon(t, password="pw")

    rcon.write("say hello")

    assert t.writes == [b'\xff\xff\xff\xffrcon "pw" say hello\n']
    assert t.calls == 0  # no round trip at all


def test_write_falls_back_to_a_round_trip_on_a_transport_without_send():
    t = OldTransport()
    Rcon(t, password="pw").write("say hello")
    assert t.payloads == [b'\xff\xff\xff\xffrcon "pw" say hello\n']


def test_command_still_waits_so_a_dead_server_is_reported():
    """Penalties keep this path: an admin must hear that their ban did not reach the server."""
    t = FakeTransport(fail_times=5)
    rcon = Rcon(t, password="pw", max_retries=2, retry_delay=0)
    with pytest.raises(RconError):
        rcon.command("banclient 2")


# --- the real UDP transport ------------------------------------------------
#
# Exercised against a local responder rather than mocked, because the bug these guard is about
# *socket state*: once fire-and-forget sends stop reading their replies, those replies queue up in
# the receive buffer and the next query would read one of them instead of its own answer.


class UdpResponder:
    """A one-thread UDP server that answers whatever it is sent."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.received: list[bytes] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.2)
        self.address = self._sock.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(8192)
            except (TimeoutError, OSError):
                continue
            self.received.append(data)
            for datagram in self.reply if isinstance(self.reply, list) else [self.reply]:
                self._sock.sendto(datagram, addr)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        self._sock.close()


@pytest.fixture
def responder():  # noqa: ANN201
    server = UdpResponder(b"\xff\xff\xff\xffprint\nSTALE")
    yield server
    server.close()


def _wait_for_reply(transport, timeout=2.0):  # noqa: ANN001, ANN202
    """Block until the transport's socket actually has a datagram waiting.

    Reaching for the private socket keeps this deterministic: the whole point of the test is what
    happens when a reply *has* already landed, so sleeping and hoping would test nothing on a slow
    machine and everything on a fast one.
    """
    return select.select([transport._sock], [], [], timeout)[0] != []


def test_an_ignored_reply_is_not_served_as_the_next_query_answer(responder):  # noqa: ANN001
    """Without the discard, `status` reads the leftover `say` reply — an empty player list.

    That is the dangerous shape of this bug: an empty status reply does not look like an error, it
    looks like an empty server, and the next sync would drop every connected player.
    """
    transport = UdpRconTransport(*responder.address, timeout=1.0, settle=0.05)
    try:
        transport.send(b"say hello")  # fire and forget: its reply is never read
        assert _wait_for_reply(transport), "responder did not answer the fire-and-forget send"

        responder.reply = b"\xff\xff\xff\xffprint\nmap: mp_crash"
        raw = transport.request(b"status")
        # Not just "the real reply is in there somewhere": the stale one must be *gone*. Left in,
        # it is prepended to the answer, and the parse silently yields whatever survives.
        assert b"STALE" not in raw
        assert raw == b"\xff\xff\xff\xffprint\nmap: mp_crash"
    finally:
        transport.close()


def test_a_reply_spanning_several_datagrams_is_joined(responder):  # noqa: ANN001
    responder.reply = [b"\xff\xff\xff\xffprint\nfirst ", b"second"]
    transport = UdpRconTransport(*responder.address, timeout=1.0, settle=0.3)
    try:
        assert transport.request(b"status") == b"\xff\xff\xff\xffprint\nfirst second"
    finally:
        transport.close()


def test_a_query_returns_as_soon_as_the_reply_settles(responder):  # noqa: ANN001
    """It used to wait the full timeout again after the reply had arrived — every single time."""
    transport = UdpRconTransport(*responder.address, timeout=5.0, settle=0.05)
    try:
        started = time.monotonic()
        transport.request(b"status")
        assert time.monotonic() - started < 1.0
    finally:
        transport.close()


def test_a_silent_server_still_times_out():
    """Nothing bound at the other end: the caller must be told, not left with an empty reply."""
    transport = UdpRconTransport("127.0.0.1", 1, timeout=0.1, settle=0.05)
    try:
        with pytest.raises(RconError):
            transport.request(b"status")
    finally:
        transport.close()
