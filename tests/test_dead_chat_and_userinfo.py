"""Two engine features the classic bot had and this one did not — §1.15.

**`sayDead`.** On several of these engines a dead player is shown only what other dead players are
saying. So a bot that answers a `!command` with a plain `say` answers into a channel the person who
asked cannot read, while everybody else reads it — the reply is not lost, it is delivered to exactly
the wrong set of people. There is no engine verb for "tell the dead"; the classic bot sent the same
private message to each of them, and so does this.

**`dumpuser`.** A Quake 3 `status` table carries a slot, a name and a ping, and no persistent id at
all: the id is stated once, in the `ClientUserinfo` line, when the player connects. A bot started
mid-match therefore cannot identify anybody already playing — cannot hold their level, cannot match
a ban — and before this it simply waited for them to reconnect.
"""

from __future__ import annotations

import pytest

from b3.config.schema import Config
from b3.core.events import Event, EventType
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.parsers.q3.parser import Q3Parser
from b3.parsers.q3.profiles import ET, IOURT42, Q3
from b3.runtime.bot import Bot

# -- who is dead ----------------------------------------------------------------


def _bot(game: str = "q3", rcon=None):  # noqa: ANN001, ANN202
    config = Config.model_validate(
        {"bot": {"database": "sqlite://"}, "server": {"game": game}},
    )
    return Bot(config, rcon=rcon)


def test_a_player_starts_alive() -> None:
    """The safe default: being wrong this way sends someone a message they can see."""
    assert Client(guid="A", name="Bob").alive


@pytest.mark.asyncio
async def test_being_killed_marks_the_victim_dead_not_the_killer() -> None:
    bot = _bot()
    killer = Client(guid="A", name="Killer", cid="1")
    victim = Client(guid="B", name="Victim", cid="2")
    bot.clients.add(killer)
    bot.clients.add(victim)

    await bot.bus.publish(Event(EventType.CLIENT_KILL, client=killer, target=victim))

    assert killer.alive
    assert not victim.alive


@pytest.mark.asyncio
async def test_a_suicide_names_one_player_and_that_player_is_the_victim() -> None:
    """Read only `target` and every self-inflicted death would leave the player marked alive."""
    bot = _bot()
    bob = Client(guid="A", name="Bob", cid="1")
    bot.clients.add(bob)

    await bot.bus.publish(Event(EventType.CLIENT_SUICIDE, client=bob))

    assert not bob.alive


@pytest.mark.asyncio
async def test_spawning_puts_a_player_back_in_play() -> None:
    bot = _bot()
    bob = Client(guid="A", name="Bob", cid="1", alive=False)
    bot.clients.add(bob)

    await bot.bus.publish(Event(EventType.CLIENT_SPAWN, client=bob))

    assert bob.alive


@pytest.mark.asyncio
async def test_a_new_round_puts_everybody_back_in_play() -> None:
    """Without this, someone killed in the last seconds of a round stays dead through the next one
    on any engine that does not report spawns."""
    bot = _bot()
    bob = Client(guid="A", name="Bob", cid="1", alive=False)
    bot.clients.add(bob)

    await bot.bus.publish(Event(EventType.GAME_ROUND_START, data={"mapname": "q3dm17"}))

    assert bob.alive


# -- saying it to them ----------------------------------------------------------


class RecordingRcon:
    def __init__(self, reply: str = "") -> None:
        self.sent: list[str] = []
        self.reply = reply

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        return self.reply

    def close(self) -> None:
        pass


def test_say_dead_reaches_the_dead_and_nobody_else() -> None:
    rcon = RecordingRcon()
    bot = _bot(rcon=rcon)
    alive = Client(guid="A", name="Alive", cid="1")
    dead = Client(guid="B", name="Dead", cid="2", alive=False)
    bot.clients.add(alive)
    bot.clients.add(dead)

    bot.say_dead("the bomb has been planted")

    assert len(rcon.sent) == 1
    assert "tell 2" in rcon.sent[0]
    assert "[DEAD]" in rcon.sent[0]


def test_say_dead_with_nobody_dead_sends_nothing() -> None:
    """Not "sends an empty message": a `tell` to nobody is still a command on the wire."""
    rcon = RecordingRcon()
    bot = _bot(rcon=rcon)
    bot.clients.add(Client(guid="A", name="Alive", cid="1"))

    bot.say_dead("nothing to report")

    assert rcon.sent == []


def test_smart_say_answers_a_live_player_out_loud() -> None:
    rcon = RecordingRcon()
    bot = _bot(rcon=rcon)
    bob = Client(guid="A", name="Bob", cid="1")
    bot.clients.add(bob)

    bot.smart_say(bob, "hello")

    assert any(cmd.startswith("say ") for cmd in rcon.sent)


def test_smart_say_answers_a_dead_player_where_they_can_read_it() -> None:
    rcon = RecordingRcon()
    bot = _bot(rcon=rcon)
    bob = Client(guid="A", name="Bob", cid="1", alive=False)
    bot.clients.add(bob)

    bot.smart_say(bob, "hello")

    assert not any(cmd.startswith("say ") for cmd in rcon.sent)
    assert any("tell 1" in cmd for cmd in rcon.sent)


def test_a_spectator_is_answered_the_same_way_as_a_dead_player() -> None:
    """Which is what the classic bot did, and for the same reason: they read the same channel."""
    rcon = RecordingRcon()
    bot = _bot(rcon=rcon)
    watcher = Client(guid="A", name="Watcher", cid="1", team="spec")
    bot.clients.add(watcher)

    bot.smart_say(watcher, "hello")

    assert not any(cmd.startswith("say ") for cmd in rcon.sent)


# -- dumpuser -------------------------------------------------------------------

DUMPUSER_REPLY = """userinfo
--------
ip                  62.235.246.103:27960
name                Shinki
rate                8000
cl_guid             8982B13A8DCEE4C77A32E6AC4DD7EEDF
snaps               20
"""


def test_a_dumpuser_reply_becomes_the_log_line_it_is_the_same_thing_as() -> None:
    parser = Q3Parser(Q3)
    line = parser.read_userinfo("3", DUMPUSER_REPLY)
    assert line is not None
    assert line.startswith("ClientUserinfo: 3 ")
    assert "\\cl_guid\\8982B13A8DCEE4C77A32E6AC4DD7EEDF" in line
    assert "\\name\\Shinki" in line


def test_a_value_with_spaces_in_it_survives() -> None:
    """The table is fixed-width, not whitespace-separated. Splitting on whitespace would keep "Bob"
    and throw away "the Builder", which is a different player as far as an alias search goes."""
    parser = Q3Parser(Q3)
    reply = "userinfo\n--------\nname                Bob the Builder\n"
    line = parser.read_userinfo("2", reply)
    assert line is not None
    assert "\\name\\Bob the Builder" in line


def test_an_empty_slot_is_reported_as_such_rather_than_parsed() -> None:
    """The engine answers with prose, not an error: "Player 5 is not on the server"."""
    parser = Q3Parser(Q3)
    assert parser.read_userinfo("5", "Player 5 is not on the server") is None
    assert parser.read_userinfo("5", "") is None


def test_the_reassembled_line_goes_through_the_ordinary_handler() -> None:
    """Which is the point of rebuilding a line rather than applying the fields here: the guid length
    check, the name truncation and the team mapping are not reimplemented."""
    parser = Q3Parser(IOURT42)
    line = parser.read_userinfo("3", DUMPUSER_REPLY)
    assert line is not None

    events = parser.parse_line(line)

    assert [e.type for e in events] == [EventType.CLIENT_UPDATE]
    client = events[0].client
    assert client is not None
    assert client.guid == "8982B13A8DCEE4C77A32E6AC4DD7EEDF"
    assert client.name == "Shinki"
    assert client.ip == "62.235.246.103"  # the port is dropped, as on the log path


def test_the_q3_family_asks_and_the_others_do_not() -> None:
    from b3.parsers.cod.profiles import ALL as COD_PROFILES

    assert Q3.userinfo_command == "dumpuser %(cid)s"
    assert ET.userinfo_command == "dumpuser %(cid)s"
    # Every Call of Duty status row carries the guid outright, so there is nothing left to ask.
    assert all(p.userinfo_command == "" for p in COD_PROFILES.values())


def test_a_player_with_no_id_in_the_status_table_is_asked_about() -> None:
    rcon = RecordingRcon(reply=DUMPUSER_REPLY)
    bot = _bot(rcon=rcon)

    filled = bot._identify([PlayerInfo(cid="3", name="Shinki", guid="", ip="")])

    assert "dumpuser 3" in rcon.sent
    assert filled[0].guid == "8982B13A8DCEE4C77A32E6AC4DD7EEDF"


def test_a_player_the_table_already_identifies_is_not_asked_about() -> None:
    rcon = RecordingRcon(reply=DUMPUSER_REPLY)
    bot = _bot(rcon=rcon)

    bot._identify([PlayerInfo(cid="3", name="Shinki", guid="ALREADYKNOWN", ip="")])

    assert rcon.sent == []


def test_a_slot_is_asked_about_once_not_once_per_sync() -> None:
    """A plain Enemy Territory server sets no `cl_guid` at all, so the answer never arrives. Asking
    every five minutes forever is a round trip per player per sync for nothing."""
    rcon = RecordingRcon(reply="Player 3 is not on the server")
    bot = _bot(rcon=rcon)
    players = [PlayerInfo(cid="3", name="Shinki", guid="", ip="")]

    bot._identify(players)
    bot._identify(players)
    bot._identify(players)

    assert rcon.sent.count("dumpuser 3") == 1


def test_the_slot_is_asked_about_again_once_somebody_else_is_in_it() -> None:
    rcon = RecordingRcon(reply="Player 3 is not on the server")
    bot = _bot(rcon=rcon)
    ghost = Client(guid="", name="Gone", cid="3")
    bot.clients.add(ghost)

    bot._identify([PlayerInfo(cid="3", name="Gone", guid="", ip="")])
    bot._drop_client(ghost)
    bot._identify([PlayerInfo(cid="3", name="Newcomer", guid="", ip="")])

    assert rcon.sent.count("dumpuser 3") == 2


# -- the Frozen Sand account name ------------------------------------------------------------


#: Captured verbatim from `test_iourt42.py:535` (`test_ioclient_with_authl_token`), a real 4.2
#: `ClientUserinfo` line. The one field that matters here is `authl`.
AUTHL_USERINFO = (
    r"2 \ip\11.22.33.44:27961\challenge\-284496317\qport\13492\protocol\68"
    r"\name\laCourge\racered\2\raceblue\2\rate\16000\ut_timenudge\0\cg_rgb\128 128 128"
    r"\cg_predictitems\0\cg_physics\1\cl_anonymous\0\sex\male\handicap\100\color2\5\color1\4"
    r"\team_headmodel\*james\team_model\james\headmodel\sarge\model\sarge\snaps\20"
    r"\cg_autoPickup\-1\gear\GLAORWA\authl\lacourge\authc\0\teamtask\0"
    r"\cl_guid\00000000011111111122222223333333\weapmodes\00000110220000020002"
)


def test_the_frozen_sand_account_name_is_read_off_the_userinfo() -> None:
    """Urban Terror puts the player's account name in `authl`, and we were discarding it.

    An account survives a new `cl_guid`, which is what makes it worth more than the id a ban is
    keyed on today — so it goes on `pbid`, the second-identity field PunkBuster already fills. This
    is the half of TODO §1.3's open Frozen Sand item that needs **no network at all**: the account
    *service* is still unported because nothing here can confirm it still answers, but the account
    *name* was arriving in every one of these lines. The classic reads it here too, and skips its
    own auth query when it is present.
    """
    parser = Q3Parser(IOURT42)

    parser.parse_line("777:16 ClientUserinfo: " + AUTHL_USERINFO)

    client = parser.clients.get_by_cid("2")
    assert client is not None
    assert client.pbid == "lacourge"
    assert client.guid == "00000000011111111122222223333333"  # the guid is still the guid
    assert client.name == "laCourge"


def test_a_userinfo_without_an_account_leaves_the_second_identity_alone() -> None:
    """Captured: `test_iourt42.py:522` — the same line without `authl`, which is the common case.

    A field that is absent must not blank one already known: PunkBuster fills the same field, and
    the two sources arrive at different moments.
    """
    parser = Q3Parser(IOURT42)
    parser.parse_line("777:16 ClientUserinfo: " + AUTHL_USERINFO)
    parser.parse_line(
        r"777:17 ClientUserinfo: 2 \ip\11.22.33.44:27961\name\laCourge"
        r"\cl_guid\00000000011111111122222223333333"
    )

    client = parser.clients.get_by_cid("2")
    assert client is not None
    assert client.pbid == "lacourge"


def test_a_password_in_the_userinfo_is_not_the_players_b3_password() -> None:
    """Captured: `test_iourt42.py:573` (`test_client_with_password_gamepassword`).

    A player who saves the server's join password in their UrT config sends a `password` field in
    every `ClientUserinfo`. In the classic that overwrote `Client.password` — the *bot's* login
    password — so the `login` plugin's credential was replaced by the server's. Nothing here reads
    the field, and authentication reloads the password from the stored record, so it cannot happen;
    this pins both halves of that.
    """
    parser = Q3Parser(IOURT42)

    parser.parse_line(
        r"777:16 ClientUserinfo: 15 \ip\1.2.3.4:27960\name\Zesco\password\some_password_here"
        r"\cl_guid\58D4069246865BB5A85F20FB60ED6F65"
    )

    client = parser.clients.get_by_cid("15")
    assert client is not None
    assert client.password is None  # not `some_password_here`
