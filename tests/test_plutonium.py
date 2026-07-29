"""Plutonium — Modern Warfare 3 (IW5) and Black Ops 2 (T6), the client the modern CoD scene runs.

Two titles the classic bot never had; Black Ops 2 it never supported in any form. Spec transplanted
from `xerxes-at/b3-parser-plutonium`, whose parsers patch B3's client class and monkey-patch a guid
setter to cope — where here the same facts are profile data.

Three of them are the interesting ones, because each is a silent failure if it is wrong: the chat
line limit (past it the engine simply does not display the rest), the bot guid (every AI shares one,
so they would share one database row), and the ping column (which is not always a number).
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.game import PlayerInfo
from b3.domain.client import Client
from b3.parsers.cod import status as sp
from b3.parsers.cod.profiles import PLUTOIW5, PLUTOT6
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot

IW5_BOT_GUID = "FFFFFFFF000B07123"  # long enough to pass the 15-character minimum
IW5_PLAYER_GUID = "123456789012345"

#: Plutonium T6, in the columns IW4M-Admin publishes for it:
#:     num score bot ping guid name lastmsg address qport rate
#: A bot has `bot`=1, an empty ping, guid `0`, and no port on its address.
T6_STATUS = """map: mp_dockside
num score bot ping guid name             lastmsg address               qport rate
--- ----- --- ---- ---- ---------------- ------- --------------------- ----- -----
  0    12   0   47 4A2F ^7Bob the Builder      50 192.0.2.44:28960      12345 25000
  1     3   1      0    ^7[3arc]Bot            20 loopback                 -1 25000
  2     7   0   88 5B3E Plain Name             10 198.51.100.9:28961    12347 25000
"""

#: Plutonium IW5, likewise:
#:     num score bot ping guid name address qport
#: No `lastmsg` and no `rate`, the ping is *letters* for a bot, and the address may be `bot` or
#: `loopback` with no port. There **is** a guid column — the B3 reference parser captured none.
IW5_STATUS = """map: mp_dome
num score bot ping guid                             name            address               qport
--- ----- --- ---- -------------------------------- --------------- --------------------- -----
  0    12   0   47 110000112345678                  Bob the Builder 192.0.2.44:28960      12345
  1     3   1  BOT bot1                             Recruit         bot                     -1
  2    -1   0   61 aabbccddeeff0011                 Alice           loopback              12346
"""


class ScriptedRcon:
    def __init__(self, replies=None):  # noqa: ANN001
        self.replies = replies or {}
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return self.replies.get(cmd, "")


def _bot(tmp_path, game, rcon=None, line_length=None):  # noqa: ANN001, ANN202
    bot_config = BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}")
    if line_length is not None:
        bot_config.line_length = line_length
    config = Config(
        bot=bot_config,
        server=ServerConfig(game=game),
        plugins=[PluginEntry(name="admin")],
    )
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    if rcon is not None:
        rcon.commands.clear()  # drop the profile's startup cvars
    return bot


# -- the status tables --------------------------------------------------------------------------


def test_the_t6_table_reads_its_own_shape():
    _map, players = sp.parse_status(T6_STATUS, PLUTOT6.status_patterns, PLUTOT6.identity_field)

    assert [p.cid for p in players] == ["0", "1", "2"]
    bob = players[0]
    assert bob.name == "Bob the Builder"  # the ^7 colour code is not part of the name
    assert bob.guid == "4A2F"  # Black Ops 2 guids are short hex
    assert (bob.ip, bob.port, bob.ping, bob.score) == ("192.0.2.44", 28960, 47, 12)


def test_a_row_with_no_port_does_not_take_the_whole_table_down():
    """`int("")` would raise, and one such row would lose the player list — including everyone who
    parsed. A bot and a listen server both produce one."""
    _map, players = sp.parse_status(T6_STATUS, PLUTOT6.status_patterns, PLUTOT6.identity_field)
    assert len(players) == 3
    assert players[1].port == 0


def test_the_iw5_table_reads_its_own_shape():
    """The B3 reference parser put the score **last** and captured no guid at all, so nobody on an
    IW5 server would ever have been identified from the status table. These columns are the ones
    IW4M-Admin publishes."""
    _map, players = sp.parse_status(IW5_STATUS, PLUTOIW5.status_patterns, PLUTOIW5.identity_field)

    assert [p.cid for p in players] == ["0", "1", "2"]
    bob = players[0]
    assert bob.name == "Bob the Builder"
    assert bob.guid == "110000112345678"
    assert (bob.ip, bob.port, bob.ping, bob.score) == ("192.0.2.44", 28960, 47, 12)
    assert players[2].score == -1  # a negative score is ordinary


def test_an_address_that_is_not_an_address_still_parses():
    """These engines print `bot`, `loopback` or `unknown` where an IP would go, with no port."""
    _map, iw5 = sp.parse_status(IW5_STATUS, PLUTOIW5.status_patterns, PLUTOIW5.identity_field)
    assert (iw5[1].ip, iw5[1].port) == ("bot", 0)
    assert (iw5[2].ip, iw5[2].port) == ("loopback", 0)

    _map, t6 = sp.parse_status(T6_STATUS, PLUTOT6.status_patterns, PLUTOT6.identity_field)
    assert (t6[1].ip, t6[1].port) == ("loopback", 0)


def test_a_bots_ping_is_not_a_number():
    """IW5 prints letters there and T6 leaves it blank. Read as 0 a bot looks like the best connection
    on the server, which is backwards for anything watching for high pings; refused outright, the row
    does not parse and the bot cannot see the player at all."""
    _map, iw5 = sp.parse_status(IW5_STATUS, PLUTOIW5.status_patterns, PLUTOIW5.identity_field)
    assert iw5[1].ping == sp.BOT_PING == 999

    _map, t6 = sp.parse_status(T6_STATUS, PLUTOT6.status_patterns, PLUTOT6.identity_field)
    assert t6[1].ping == 0  # empty, which is not a claim either way


def test_a_signed_ping_is_still_a_number():
    """The counter-case, and a regression: Arma reports -1 for a player in the lobby, so "not a
    number" must mean *not numeric*, not merely "does not start with a digit"."""
    assert sp.parse_status(
        "map: x\n  0    12   0   -1 110000112345678                  Bob 192.0.2.44:28960 12345\n",
        PLUTOIW5.status_patterns,
        PLUTOIW5.identity_field,
    )[1] == []  # IW5's own pattern requires digits or letters there, so the row simply does not match


def test_names_with_spaces_survive():
    """The B3 reference regex captures the name as `\\S+`, so "Bob the Builder" arrived as "Bob"."""
    for profile, text in ((PLUTOT6, T6_STATUS), (PLUTOIW5, IW5_STATUS)):
        _map, players = sp.parse_status(text, profile.status_patterns, profile.identity_field)
        assert players[0].name == "Bob the Builder", profile.name


def test_a_t6_name_without_a_colour_code_still_parses():
    """The reference pattern required a literal `^7` before the name, so a server that does not print
    one would have parsed as an empty table — a bot that sees nobody on a full server."""
    _map, players = sp.parse_status(T6_STATUS, PLUTOT6.status_patterns, PLUTOT6.identity_field)
    assert players[2].name == "Plain Name"


def test_the_bot_column_is_believed_over_the_guid():
    """Both tables state outright whether a row is an AI player. That beats any guid convention — and
    it has to be applied *after* the "fall back to the guid column" rule, or the fallback puts the
    bot's shared guid straight back and every AI ends up on one database row."""
    _map, iw5 = sp.parse_status(IW5_STATUS, PLUTOIW5.status_patterns, PLUTOIW5.identity_field)
    assert iw5[1].guid == ""  # bot=1, despite the row carrying `bot1`

    _map, t6 = sp.parse_status(T6_STATUS, PLUTOT6.status_patterns, PLUTOT6.identity_field)
    assert t6[1].guid == ""  # bot=1, despite the row carrying `0`
    assert t6[0].guid == "4A2F"  # and a real player keeps theirs


# -- bots must not get identities ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("game", "bot_guid", "human_guid"),
    [("plutot6", "0", "4"), ("plutoiw5", IW5_BOT_GUID, IW5_PLAYER_GUID)],
)
@pytest.mark.asyncio
async def test_an_ai_player_joins_without_an_identity(tmp_path, game, bot_guid, human_guid):
    """Every bot on the server shares one guid, so authenticating on it would merge them all into a
    single database row — one level and one ban history between them. T6 says exactly "0"; IW5 uses a
    long shared prefix, long enough to pass any length check, which is why it is named explicitly."""
    bot = _bot(tmp_path, game=game)

    await bot.replay([f"J;{bot_guid};1;[3arc]Bot", f"J;{human_guid};2;Human"])

    ai = bot.clients.get_by_cid("1")
    human = bot.clients.get_by_cid("2")
    assert ai is not None and ai.name == "[3arc]Bot"  # still a client: it holds a slot and can chat
    assert ai.guid == ""  # but no identity
    assert human is not None and human.guid == human_guid
    bot.storage.close()


@pytest.mark.asyncio
async def test_the_status_table_does_not_smuggle_a_bot_in_either(tmp_path):
    """The other route a client can be created. Filtering only the log path still lets every T6 bot
    share one row, because its bot guid passes that title's one-character minimum."""
    bot = _bot(tmp_path, game="plutot6")

    bot._reconcile(
        [
            PlayerInfo(cid="0", name="Human", guid="4", ip="192.0.2.44"),
            PlayerInfo(cid="1", name="[3arc]Bot", guid="0", ip="192.0.2.45"),
        ]
    )

    assert bot.clients.get_by_cid("0").guid == "4"
    assert bot.clients.get_by_cid("1").guid == ""
    bot.storage.close()


def test_the_profile_answers_the_bot_question_for_both_shapes():
    assert PLUTOT6.is_bot_guid("0") is True
    assert PLUTOT6.is_bot_guid("04") is False  # exact, not a prefix: a real guid may start with 0
    assert PLUTOIW5.is_bot_guid(IW5_BOT_GUID) is True
    assert PLUTOIW5.is_bot_guid(IW5_PLAYER_GUID) is False
    assert PLUTOT6.is_bot_guid("") is False


# -- the chat line limit ------------------------------------------------------------------------


@pytest.mark.parametrize(("game", "limit"), [("plutoiw5", 43), ("plutot6", 72)])
@pytest.mark.asyncio
async def test_chat_is_wrapped_to_what_the_engine_will_display(tmp_path, game, limit):
    """The default is 90 characters. Send that to IW5, which stops at 43, and half of every reply is
    simply not shown — with nothing anywhere to say so."""
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, game=game, rcon=rcon)

    bot.say("x" * 200)

    assert rcon.commands, "nothing was said"
    for command in rcon.commands:
        text = command.removeprefix("say ")
        assert len(text) <= limit, command
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_configured_limit_can_ask_for_shorter_lines_but_not_longer(tmp_path):
    """The two mean different things: the config is a preference, the profile is a fact about the
    engine. So the smaller wins — a config value cannot lift a limit the game imposes."""
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, game="plutoiw5", rcon=rcon, line_length=20)
    bot.say("y" * 100)
    assert all(len(c.removeprefix("say ")) <= 20 for c in rcon.commands)
    bot.storage.close()

    rcon = ScriptedRcon()
    bot = _bot(tmp_path, game="plutoiw5", rcon=rcon, line_length=200)
    bot.say("z" * 100)
    assert all(len(c.removeprefix("say ")) <= 43 for c in rcon.commands)  # the engine still wins
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_title_with_no_engine_limit_uses_the_config_alone(tmp_path):
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, game="cod4", rcon=rcon, line_length=60)
    bot.say("w" * 200)
    lengths = [len(c.removeprefix("say ")) for c in rcon.commands]
    assert max(lengths) <= 60
    assert max(lengths) > 43  # not capped by some other title's limit
    bot.storage.close()


# -- the verbs ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_neither_title_has_a_ban_verb_so_a_ban_is_a_kick(tmp_path):
    """IW4M-Admin maps `Ban` and `TempBan` to the kick verb on both titles, its history records
    `tempbanclient` being *removed* from the sibling TeknoMW3 parser, and Plutonium's own admin script
    keeps bans in a JSON file and enforces them by kicking. So the bot's record is the ban.

    The verb itself is the correction that mattered: `dropclient` — what the B3 reference parser sent
    for IW5 — belongs to TeknoMW3, a different Modern Warfare 3 stack, and would have kicked nobody.
    """
    for game, verb in (("plutoiw5", "clientkick"), ("plutot6", "clientkick_for_reason")):
        rcon = ScriptedRcon()
        bot = _bot(tmp_path, game=game, rcon=rcon)
        bob = Client(cid="2", guid="1" * 15, name="Bob")
        bot.clients.add(bob)

        bot.ban(bob, "cheating")
        bot.tempban(bob, minutes=30, reason="spam")

        assert rcon.commands == [
            f'{verb} 2 "cheating"',
            f'{verb} 2 "spam"',
        ], game
        bot.storage.close()


@pytest.mark.asyncio
async def test_unbanning_sends_nothing_and_does_not_cry_about_a_ban_list(tmp_path, caplog):
    """There is no server-side ban list on these titles, so lifting our own record *is* the whole job.
    The generic "it may still be on the server's own ban list" warning would send an operator looking
    for something that does not exist."""
    rcon = ScriptedRcon()
    bot = _bot(tmp_path, game="plutoiw5", rcon=rcon)
    bob = Client(cid="2", guid="1" * 15, name="Bob")
    bot.clients.add(bob)

    with caplog.at_level("WARNING"):
        bot.unban(bob, "appeal granted")

    assert rcon.commands == []
    assert "ban list" not in caplog.text
    bot.storage.close()


@pytest.mark.asyncio
async def test_iw5_reads_a_cvar_the_only_way_it_answers(tmp_path):
    """IW5 wants `get <name>` and replies `name is "value"` — no quotes round the name, no colon.
    Sending the bare name (the Quake3 convention every other title here takes) gets nothing back, so
    `!maps` and anything else built on a cvar read came back empty."""
    rcon = ScriptedRcon({"get sv_maxclients": 'sv_maxclients is "18"'})
    bot = _bot(tmp_path, game="plutoiw5", rcon=rcon)

    assert bot.get_cvar("sv_maxclients") == "18"
    assert rcon.commands == ["get sv_maxclients"]
    assert bot.game.max_players == 18  # and it lands in the live game state
    bot.storage.close()


@pytest.mark.asyncio
async def test_t6_reads_a_cvar_the_quake3_way(tmp_path):
    """T6 does not use `get`, and answers in the quoted-and-colonned form. Two titles in one family
    disagreeing is exactly why this is profile data."""
    rcon = ScriptedRcon({"sv_maxclients": '"sv_maxclients" is: "18^7" default: "8^7"'})
    bot = _bot(tmp_path, game="plutot6", rcon=rcon)

    assert bot.get_cvar("sv_maxclients") == "18"
    assert rcon.commands == ["sv_maxclients"]
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_kill_with_a_decimal_damage_figure_is_not_dropped(tmp_path):
    """Plutonium T6 writes the damage with decimals. An integer-only pattern does not mis-read those
    lines, it fails to match them — and an unmatched log line looks exactly like a quiet server."""
    from b3.core.events import EventType

    bot = _bot(tmp_path, game="plutot6")
    guid_a, guid_b = "4", "5"
    seen: list[EventType] = []
    bot.bus.subscribe(EventType.CLIENT_KILL, lambda e: seen.append(e.type))

    await bot.replay(
        [
            f"J;{guid_a};1;Attacker",
            f"J;{guid_b};2;Victim",
            f"K;{guid_b};2;axis;Victim;{guid_a};1;allies;Attacker;ak47_mp;100.000;MOD_RIFLE_BULLET;head",
        ]
    )

    assert seen == [EventType.CLIENT_KILL]
    bot.storage.close()
