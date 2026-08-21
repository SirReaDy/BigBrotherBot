"""Engine verbs: things a server can do to one player that this bot has no portable concept of.

`GameProfile.player_verbs` plus `Console.supports_verb` / `Console.apply_verb` are the honest half of
the classic bot's `inflictCustomPenalty`. The other half — inventing a sixth *penalty type* in the
database — was refused (TODO §4.5), and rightly: a slap is not a penalty, it is a thing the server can
be asked to do.

What the classic got wrong is that it never asked whether the server *could*. `inflictCustomPenalty`
sent a command into the dark and reported nothing back, so a plugin could offer "slap" on a title with
no such verb and the operator would watch a configured penalty do nothing at all. Every test here is
about that: the question, the answer, and the refusal.
"""

from __future__ import annotations

import pytest

from dataclasses import replace

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.domain.client import Client
from b3.parsers.q3.profiles import IOURT42
from b3.runtime.bot import Bot


class Rcon:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


def _bot(tmp_path, game="iourt42"):  # noqa: ANN001, ANN202
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game=game),
        plugins=[PluginEntry(name="admin")],
    )
    rcon = Rcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    bot.start()
    rcon.commands.clear()
    return bot, rcon


def _client(name="Bob", cid="3"):  # noqa: ANN001, ANN202
    return Client(guid="b" * 32, name=name, cid=cid, id=1)


# -- asking -------------------------------------------------------------------


def test_urban_terror_declares_its_own_verbs():
    """Taken from the classic `iourt42` parser's command table, which is where they were hiding."""
    assert set(IOURT42.player_verbs) == {"slap", "nuke", "kill", "mute"}
    assert IOURT42.player_verbs["kill"] == "smite %(cid)s"  # the engine's own spelling


def test_a_title_with_none_says_so(tmp_path):
    """Every family here but Urban Terror, which is why a plugin has to ask before it offers."""
    bot, _rcon = _bot(tmp_path, game="cod4")

    assert bot.supports_verb("slap") is False
    assert bot.apply_verb("slap", _client()) is False
    bot.storage.close()


def test_a_title_with_the_verb_says_so(tmp_path):
    bot, _rcon = _bot(tmp_path)

    assert bot.supports_verb("slap") is True
    assert bot.supports_verb("teleport") is False  # not declared, so no
    bot.storage.close()


# -- applying -----------------------------------------------------------------


def test_a_slap_reaches_the_server_as_the_engines_verb(tmp_path):
    bot, rcon = _bot(tmp_path)

    assert bot.apply_verb("slap", _client(cid="3")) is True

    assert rcon.commands == ["slap 3"]
    bot.storage.close()


def test_kill_is_the_engines_word_for_it_not_ours(tmp_path):
    """Urban Terror's verb is `smite`. A plugin asks for `kill` and the profile knows the spelling."""
    bot, rcon = _bot(tmp_path)

    bot.apply_verb("kill", _client(cid="7"))

    assert rcon.commands == ["smite 7"]
    bot.storage.close()


def test_a_verb_with_an_argument(tmp_path):
    """`mute %(cid)s %(seconds)s` — and 0 seconds is how the classic's `censorurt` un-muted."""
    bot, rcon = _bot(tmp_path)

    bot.apply_verb("mute", _client(cid="4"), seconds="60")
    bot.apply_verb("mute", _client(cid="4"), seconds="0")

    assert rcon.commands == ["mute 4 60", "mute 4 0"]
    bot.storage.close()


def test_an_argument_nobody_supplied_is_refused_rather_than_half_sent(tmp_path):
    """§1.10's rule: a verb that cannot be filled in is not sent at all.

    `mute 4` without the seconds is a different command on some builds, and a plugin that thinks it
    muted somebody who is still talking is worse than one that reports it could not.
    """
    bot, rcon = _bot(tmp_path)

    assert bot.apply_verb("mute", _client(cid="4")) is False

    assert rcon.commands == []
    bot.storage.close()


def test_a_players_name_cannot_break_out_of_the_command(tmp_path):
    """`%(name)s` is a string the *player* chose, so it is sanitised like everything else.

    A Quake 3 console splits its command buffer on `;` and reads `"` as opening a quoted token, so a
    player called `bob"; quit` is how one command becomes two.
    """
    bot, rcon = _bot(tmp_path)
    bot.profile = replace(bot.profile, player_verbs={"scold": 'say "%(name)s, behave"'})

    bot.apply_verb("scold", _client(name='bob"; quit', cid="2"))

    assert rcon.commands == ['say "bob quit, behave"']
    assert ";" not in rcon.commands[0]
    bot.storage.close()


@pytest.mark.asyncio
async def test_a_verb_is_available_to_a_plugin_through_the_console(tmp_path):
    """The seam a plugin actually uses: ask, then apply. `spawnkill` is the first caller."""
    bot, rcon = _bot(tmp_path)
    bob = _client()
    bot.clients.add(bob)

    if bot.supports_verb("nuke"):
        bot.apply_verb("nuke", bob)

    assert rcon.commands == ["nuke 3"]
    bot.storage.close()
