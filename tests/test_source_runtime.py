"""The runtime seams the Source family needed, and the three faults the end-to-end run found.

Every test here is a regression test for something that was wrong and green: the parser tests passed,
`mypy` and `ruff` passed, and the bot still could not ban anybody properly. What caught them was
driving a real Bot against a real socket — `tools/fakeservers/e2e_insurgency.py`.

1. **`sm_addban` does not remove the player.** It writes the ban list and nothing else, so the ban was
   recorded, the server's own list had the entry, and the culprit went on playing until the map ended.
   A penalty verb here is *two* commands.
2. **`sm_unban` takes an id, not a name.** The template used `%(target)s`, which is the player's name,
   so lifting a ban left the server's entry in place — and the player stayed banned by an entry nobody
   remembered making.
3. **SourceMod has to be checked for.** Without it the bot connects, authenticates, reports itself
   healthy, and silently cannot kick, ban or reply to anybody.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, ServerConfig
from b3.core.doctor import Status, run_checks
from b3.domain.client import Client
from b3.parsers.profile import RequiredMod
from b3.parsers.source.profiles import INSURGENCY
from b3.runtime.bot import Bot, MissingServerModError


class FakeRcon:
    """Records what was sent, and answers `sm version` like a server with SourceMod."""

    def __init__(self, *, sourcemod: bool = True, reply: str = "") -> None:
        self.sent: list[str] = []
        self.sourcemod = sourcemod
        self.reply = reply

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        if cmd.strip() == "sm version":
            return " SourceMod Version: 1.10\n" if self.sourcemod else 'Unknown command "sm"\n'
        return self.reply

    def close(self) -> None:
        pass


def _bot(tmp_path, rcon: FakeRcon) -> Bot:
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game="insurgency"),
    )
    bot = Bot(config, rcon=rcon)
    bot.storage.connect()
    return bot


def _connected(bot: Bot, cid: str = "194", guid: str = "STEAM_1:0:1111111") -> Client:
    client = Client(cid=cid, guid=guid, name="courgette")
    bot.clients.add(client)
    bot.storage.save_client(client)
    return client


# -- a penalty that takes two commands -----------------------------------------------------------


def test_a_ban_both_writes_the_ban_list_and_removes_the_player(tmp_path):
    """`sm_addban` alone bans somebody and leaves them playing. This is the bug, in one test."""
    rcon = FakeRcon()
    bot = _bot(tmp_path, rcon)
    client = _connected(bot)

    bot.ban(client, "cheating")

    sent = [c for c in rcon.sent if c != "sm version"]
    assert any(c.startswith("sm_addban") for c in sent), f"no ban was written: {sent}"
    assert any(c.startswith("sm_kick") for c in sent), (
        f"the player was banned and left on the server: {sent}"
    )
    bot.storage.close()


def test_a_tempban_does_the_same_and_carries_the_minutes(tmp_path):
    rcon = FakeRcon()
    bot = _bot(tmp_path, rcon)
    client = _connected(bot)

    bot.tempban(client, 120, "teamkilling")

    sent = [c for c in rcon.sent if c != "sm version"]
    addban = next(c for c in sent if c.startswith("sm_addban"))
    assert "120" in addban
    assert "STEAM_1:0:1111111" in addban
    assert any(c.startswith("sm_kick") for c in sent)
    bot.storage.close()


def test_banning_someone_who_is_not_on_the_server_skips_the_kick(tmp_path):
    """The same template has to serve an offline ban: there is no slot to kick, and naming one would
    send the literal string "None" as a slot id."""
    rcon = FakeRcon()
    bot = _bot(tmp_path, rcon)
    offline = Client(guid="STEAM_1:0:3333333", name="ghost")  # cid is None
    bot.storage.save_client(offline)

    bot.ban(offline, "evading")

    sent = [c for c in rcon.sent if c != "sm version"]
    assert any(c.startswith("sm_addban") for c in sent), "an offline ban is still written"
    assert not any("None" in c for c in sent), f"a slot was invented: {sent}"
    assert not any(c.startswith("sm_kick") for c in sent)
    bot.storage.close()


def test_the_multi_command_split_does_not_disturb_a_single_command_engine(tmp_path):
    """Every other family's penalty templates are one command, and must stay one command."""
    rcon = FakeRcon()
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'cod.sqlite'}"),
        server=ServerConfig(game="cod4"),
    )
    bot = Bot(config, rcon=rcon)
    bot.storage.connect()
    client = _connected(bot, guid="a" * 32)

    bot.kick(client, "spam")
    assert rcon.sent == ["clientkick 194"]
    bot.storage.close()


# -- lifting a ban -------------------------------------------------------------------------------


def test_an_unban_names_the_steam_id_because_the_verb_will_not_take_a_name(tmp_path):
    rcon = FakeRcon()
    bot = _bot(tmp_path, rcon)
    client = _connected(bot)

    bot.unban(client, "appeal granted")

    unbans = [c for c in rcon.sent if c.startswith("sm_unban")]
    assert unbans == ['sm_unban "STEAM_1:0:1111111"']
    assert "courgette" not in unbans[0], "the name would not lift anything"
    bot.storage.close()


def test_an_unban_is_not_sent_at_all_for_a_player_with_no_steam_id(tmp_path):
    """A bot, or a player the engine never identified. Our own record is lifted either way."""
    rcon = FakeRcon()
    bot = _bot(tmp_path, rcon)
    anonymous = Client(cid="224", name="Moe")
    bot.clients.add(anonymous)
    bot.storage.save_client(anonymous)

    bot.unban(anonymous, "nothing to lift")

    assert not [c for c in rcon.sent if c.startswith("sm_unban")]
    bot.storage.close()


# -- the mod the engine cannot work without ------------------------------------------------------


def test_a_server_with_sourcemod_starts(tmp_path):
    rcon = FakeRcon(sourcemod=True)
    bot = _bot(tmp_path, rcon)

    bot.check_required_mod()  # does not raise
    assert "sm version" in rcon.sent
    bot.storage.close()


def test_a_server_without_sourcemod_refuses_to_start(tmp_path):
    rcon = FakeRcon(sourcemod=False)
    bot = _bot(tmp_path, rcon)

    with pytest.raises(MissingServerModError, match="SourceMod"):
        bot.check_required_mod()
    bot.storage.close()


def test_a_server_that_cannot_be_asked_is_not_reported_as_missing_the_mod(tmp_path):
    """A silent server is a wrong host, port or password — a different job from installing something.

    Reporting the second as the first sends an operator to install what they already have.
    """

    class Unreachable(FakeRcon):
        def command(self, cmd: str) -> str:
            raise OSError("connection refused")

    bot = _bot(tmp_path, Unreachable())
    bot.check_required_mod()  # warns, does not raise
    bot.storage.close()


def test_a_family_that_needs_no_mod_asks_nothing(tmp_path):
    rcon = FakeRcon()
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'cod.sqlite'}"),
        server=ServerConfig(game="cod4"),
    )
    bot = Bot(config, rcon=rcon)
    bot.storage.connect()

    bot.check_required_mod()
    assert rcon.sent == []
    bot.storage.close()


def test_the_required_mod_is_data_on_the_profile_not_a_check_in_a_parser():
    assert INSURGENCY.required_mod == RequiredMod(
        name="SourceMod", command="sm version", expect="SourceMod"
    )


# -- rotating the map in two steps ---------------------------------------------------------------


def test_rotating_the_map_uses_the_next_map_cvar_when_there_is_no_rotate_verb(tmp_path):
    """This engine has no `map_rotate`. The classic read `sm_nextmap` and loaded it, which is two
    steps, and a bot that only warned would leave `!maprotate` doing nothing on a game that can."""
    rcon = FakeRcon(reply='"sm_nextmap" = "district" ( def. "" )')
    bot = _bot(tmp_path, rcon)

    bot.rotate_map()

    assert any(c == "changelevel district" for c in rcon.sent), rcon.sent
    bot.storage.close()


def test_an_unanswered_next_map_cvar_does_not_fall_back_to_guessing_from_the_map_list(tmp_path):
    """`maps *` is everything installed, in filesystem order. A map picked out of that reads to an
    admin as a fact about the rotation."""
    rcon = FakeRcon(reply="")
    bot = _bot(tmp_path, rcon)

    assert bot.get_next_map() is None
    bot.storage.close()


# -- doctor --------------------------------------------------------------------------------------


def test_doctor_says_the_password_worked_and_sourcemod_is_there(tmp_path):
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="insurgency", rcon_password="test", game_log=str(tmp_path / "c.log")
        ),
    )
    (tmp_path / "c.log").write_text("L 08/26/2012 - 03:22:35: Log file started\n", encoding="utf-8")

    checks = run_checks(config, rcon_factory=lambda: FakeRcon(sourcemod=True))
    rcon = next(c for c in checks if c.name == "rcon")

    assert rcon.status is Status.OK


def test_doctor_fails_when_the_password_works_and_sourcemod_is_missing(tmp_path):
    """The check that matters on this family: authenticating proves nothing about being able to act."""
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(
            game="insurgency", rcon_password="test", game_log=str(tmp_path / "c.log")
        ),
    )
    (tmp_path / "c.log").write_text("L 08/26/2012 - 03:22:35: Log file started\n", encoding="utf-8")

    checks = run_checks(config, rcon_factory=lambda: FakeRcon(sourcemod=False))
    rcon = next(c for c in checks if c.name == "rcon")

    assert rcon.status is Status.FAIL
    assert "SourceMod" in (rcon.detail + (rcon.hint or ""))
