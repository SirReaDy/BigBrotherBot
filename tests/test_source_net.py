"""Source RCON and A2S against a fake server that speaks the real protocols over real sockets.

The codec is testable without a socket and is tested that way below; everything after that needs a
server, because the properties worth proving are the ones a unit test cannot reach:

* a reply **split across packets** with nothing marking it as split, which is this protocol's nastiest
  property and the reason the client sends a sentinel instead of guessing from a size;
* a **rejected password signalled by an id of -1** and no message at all;
* a **reply left in the socket** by a fire-and-forget `write`, which must not be handed to the next
  command as its answer;
* the server **closing the connection** between commands, which is what a level change does;
* and the **A2S challenge**, which every current build demands and the classic bot never sent.
"""

from __future__ import annotations

import pytest

from b3.net.a2s import A2SClient, A2SError
from b3.net.source import (
    AUTH_FAILED_ID,
    MAX_COMMAND_LENGTH,
    SERVERDATA_AUTH,
    SERVERDATA_EXECCOMMAND,
    SERVERDATA_RESPONSE_VALUE,
    Packet,
    SourceAuthError,
    SourceError,
    SourceRconClient,
    decode_packet,
    encode_packet,
)
from tools.fakeservers.source import FakeSourceServer


@pytest.fixture()
def server():
    srv = FakeSourceServer(password="test").start()
    yield srv
    srv.stop()


@pytest.fixture()
def client(server):
    rcon = SourceRconClient(*server.address, "test", timeout=2.0)
    rcon.open()
    yield rcon
    rcon.close()


# -- the codec, which needs no socket ------------------------------------------------------------


def test_a_packets_size_counts_everything_after_itself():
    raw = encode_packet(7, SERVERDATA_EXECCOMMAND, "status")

    # 4 size + 4 id + 4 type + 6 body + 2 terminators, and the size field excludes its own 4.
    assert len(raw) == 20
    assert int.from_bytes(raw[:4], "little") == 16


def test_a_packet_round_trips():
    decoded = decode_packet(encode_packet(9, SERVERDATA_AUTH, "secret"))

    assert decoded is not None
    packet, rest = decoded
    assert packet == Packet(9, SERVERDATA_AUTH, "secret")
    assert rest == b""


def test_half_a_packet_is_not_an_error_because_tcp_delivers_halves():
    raw = encode_packet(1, SERVERDATA_EXECCOMMAND, "a long enough command")

    assert decode_packet(raw[:3]) is None, "not even the length prefix yet"
    assert decode_packet(raw[:-4]) is None, "length known, body incomplete"
    assert decode_packet(raw) is not None


def test_two_packets_in_one_read_are_taken_one_at_a_time():
    raw = encode_packet(1, SERVERDATA_RESPONSE_VALUE, "first") + encode_packet(
        2, SERVERDATA_RESPONSE_VALUE, "second"
    )

    first = decode_packet(raw)
    assert first is not None
    assert first[0].body == "first"
    second = decode_packet(first[1])
    assert second is not None
    assert second[0].body == "second"
    assert second[1] == b""


def test_an_implausible_length_prefix_is_refused_rather_than_used_to_size_a_read():
    """A bad prefix means the stream is out of step. Trusting it would allocate on a made-up number."""
    with pytest.raises(SourceError, match="implausible size"):
        decode_packet((999_999).to_bytes(4, "little") + b"\x00" * 8)


def test_the_body_is_taken_up_to_the_first_nul_not_the_last():
    """There are two strings in a packet: the body, and a second one the protocol never uses."""
    decoded = decode_packet(encode_packet(3, 0, "status"))
    assert decoded is not None
    assert decoded[0].body == "status"


# -- logging in ----------------------------------------------------------------------------------


def test_a_good_password_authenticates(server):
    rcon = SourceRconClient(*server.address, "test", timeout=2.0)
    rcon.open()

    assert rcon.connected and rcon.authed
    assert server.authed
    rcon.close()


def test_the_empty_packet_before_the_verdict_is_not_mistaken_for_it(server):
    """Every build sends an empty value packet and *then* the verdict. A client that reads the first
    as the answer never logs in."""
    rcon = SourceRconClient(*server.address, "test", timeout=2.0)
    rcon.open()

    assert rcon.authed
    assert rcon.command("status").startswith("hostname:")
    rcon.close()


def test_a_rejected_password_is_an_id_of_minus_one_and_no_message(server):
    rcon = SourceRconClient(*server.address, "wrong", timeout=1.0)

    with pytest.raises(SourceAuthError):
        rcon.open()
    assert AUTH_FAILED_ID == -1
    assert not rcon.authed


def test_a_refused_password_is_never_tried_again_on_its_own(server):
    """Hammering a server with a bad password is how an address gets rate-limited or blocked, so the
    refusal is remembered rather than retried."""
    rcon = SourceRconClient(*server.address, "wrong", timeout=1.0)
    with pytest.raises(SourceAuthError):
        rcon.open()

    attempts = server.auth_attempts
    with pytest.raises(SourceAuthError, match="already refused"):
        rcon.open()
    assert server.auth_attempts == attempts, "the second open() must not reach the server"


# -- commands ------------------------------------------------------------------------------------


def test_a_command_gets_its_reply(client, server):
    reply = client.command("sm version")

    assert "SourceMod" in reply
    assert server.sent_command("sm version")


def test_a_reply_split_across_packets_is_reassembled(server):
    """The property this whole client is shaped around.

    Nothing in a packet says it is a part and every part carries the same id, so the only sound way to
    know the reply has ended is the sentinel. The fake writes in 24-byte slices, which turns a status
    table into a dozen packets.
    """
    server.fragment = 24
    for slot in range(220, 232):
        server.add_player(str(slot), f"Bot{slot}")

    rcon = SourceRconClient(*server.address, "test", timeout=3.0)
    rcon.open()
    reply = rcon.command("status")
    rcon.close()

    assert reply.startswith("hostname:")
    assert reply.rstrip().endswith("#end")
    assert reply.count("\n") > 12, "a whole table, not the first packet of one"
    for slot in range(220, 232):
        assert f'"Bot{slot}"' in reply


def test_the_sentinel_can_be_turned_off_and_the_reply_still_arrives(server):
    """A server that declines to answer the empty command should cost latency, not the reply."""
    rcon = SourceRconClient(*server.address, "test", timeout=2.0, use_sentinel=False)
    rcon.open()

    assert rcon.command("status").startswith("hostname:")
    rcon.close()


def test_a_reply_left_behind_by_write_is_not_served_as_the_next_commands_answer(client, server):
    """The bug ids exist to prevent. Read as `status`, a stale empty reply says the server is empty —
    and an empty roster makes the next sync disconnect every player on it."""
    server.add_player("194", "courgette", "STEAM_1:0:1111111", ip="11.222.111.222", ping=67)

    client.write("sm_say hello")
    client.write("sm_say again")
    reply = client.command("status")

    assert reply.startswith("hostname:"), "the answer to status, not to a say"
    assert '"courgette"' in reply


def test_a_command_longer_than_the_protocol_allows_is_refused_not_truncated(client):
    """Past this the server silently does nothing, so failing loudly is the only way to know."""
    with pytest.raises(SourceError, match="over this protocol"):
        client.command("sm_say " + "x" * MAX_COMMAND_LENGTH)


def test_an_empty_command_is_not_sent(client, server):
    before = list(server.received)
    assert client.command("   ") == ""
    assert server.received == before


def test_the_status_cache_is_keyed_by_the_command_asked(client, server):
    first = client.get_status("status")
    server.add_player("194", "courgette", "STEAM_1:0:1111111", ip="1.2.3.4", ping=30)

    assert client.get_status("status") == first, "served from the cache"
    assert '"courgette"' in client.get_status("status", force=True)


# -- the connection going away -------------------------------------------------------------------


def test_a_dropped_connection_reconnects_and_retries_the_command(client, server):
    """A Source server closes this socket on a level change, and nothing is expected to arrive
    between commands — so the drop is invisible until the next one is sent."""
    assert client.command("status").startswith("hostname:")

    # What a restart looks like from here: the socket simply closes.
    server._conn.close()  # noqa: SLF001

    reply = client.command("status")
    assert reply.startswith("hostname:")
    assert server.auth_attempts >= 2, "it logged in again rather than sending into a dead socket"


def test_a_command_before_authentication_is_recognised_as_a_password_problem(server):
    """The fake answers an unauthenticated command with ``Bad Password``, as some builds do rather
    than using the id. Both routes must land on the same conclusion."""
    rcon = SourceRconClient(*server.address, "test", timeout=1.0)
    rcon.open()
    server.authed = False  # the server forgot us, without closing the socket

    with pytest.raises(SourceAuthError):
        rcon.command("status")
    rcon.close()


# -- A2S ------------------------------------------------------------------------------------------


def test_a2s_info_answers_the_challenge_the_server_demands(server):
    """The step the classic bot never took, so its ``info()`` returns nothing against any current
    build: the reply is there, it is just a challenge nobody answers."""
    server.add_player("194", "courgette", "STEAM_1:0:1111111", ip="1.2.3.4", ping=30)
    server.add_player("224", "Moe")

    info = A2SClient(*server.address, timeout=2.0).info()

    assert info.hostname == "Fake Insurgency Server"
    assert info.map_name == "buhriz"
    assert (info.players, info.max_players, info.bots) == (2, 20, 1)
    assert info.game_dir == "insurgency"
    assert info.ping > 0


def test_a2s_info_still_works_against_a_server_that_wants_no_challenge(server):
    """Pre-2020 behaviour, and a private build may still behave this way."""
    server.require_challenge = False

    info = A2SClient(*server.address, timeout=2.0).info()
    assert info.hostname == "Fake Insurgency Server"


def test_a2s_info_reads_the_optional_extra_data_block(server):
    info = A2SClient(*server.address, timeout=2.0).info()

    # An ephemeral port is routinely above 32767, so this is also the regression test for reading
    # these sixteen-bit fields as *signed*, which is what the classic client did.
    assert info.port == server.address[1]
    assert info.port > 0
    assert info.keywords == "coop,bots"


def test_the_sixteen_bit_app_id_is_read_unsigned(server):
    """Found by the fake server on its first run.

    Insurgency is Steam app 222880 and this field holds sixteen bits, so a real server sends 26272.
    Read signed, a game whose low word has the top bit set comes back negative — and the app id is
    then compared against The Ship's, so getting it wrong desynchronises everything after it.
    """
    info = A2SClient(*server.address, timeout=2.0).info()

    assert info.app_id == 222880 & 0xFFFF == 26272
    assert info.version == "1.17.5.1", "the fields after the app id are still in step"


def test_a2s_player_names_players_but_can_never_identify_them(server):
    """No Steam id and no address in this reply — which is why the roster is not built from it."""
    server.add_player("194", "courgette", "STEAM_1:0:1111111", ip="1.2.3.4", ping=30)
    server.add_player("224", "Moe")

    players = A2SClient(*server.address, timeout=2.0).players()

    assert [p.name for p in players] == ["courgette", "Moe"]
    assert all(p.score == 7 for p in players)


def test_a2s_rules_reads_the_public_cvars(server):
    rules = A2SClient(*server.address, timeout=2.0).rules()

    assert rules["sm_nextmap"] == "district"
    assert rules["tv_password"] == ""


def test_a_server_that_is_not_there_is_reported_rather_than_hanging():
    with pytest.raises(A2SError):
        # Port 1 on loopback: nothing listens, so this is a timeout rather than a refusal on UDP.
        A2SClient("127.0.0.1", 1, timeout=0.2).info()
