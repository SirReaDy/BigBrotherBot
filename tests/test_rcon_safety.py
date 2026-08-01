"""What the bot is allowed to put on a game console's command line.

A ban reason is typed by an admin and a player name is chosen by the *player*; both end up
substituted into an RCON command. A Quake3-family engine splits its command buffer on newlines and
on `;`, and treats `"` as opening a quoted token — so an unsanitised value can end the command the
bot meant to send and start a different one. These tests are the fence.
"""

from __future__ import annotations

import pytest

from b3.config.schema import BotConfig, Config, PluginEntry, ServerConfig
from b3.core.clock import FakeClock
from b3.core.util import MAX_RCON_VALUE, sanitize_rcon_value
from b3.domain.client import Client
from b3.plugins.admin import AdminPlugin
from b3.runtime.bot import Bot

STEAM_BOB = "76561198000000002"


class RecordingRcon:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def command(self, cmd: str) -> str:
        self.commands.append(cmd)
        return ""


def _bot(tmp_path, game="cod4x"):  # noqa: ANN001, ANN202
    config = Config(
        bot=BotConfig(database=f"sqlite:///{tmp_path / 'b3.sqlite'}"),
        server=ServerConfig(game=game),
        plugins=[PluginEntry(name="admin")],
    )
    rcon = RecordingRcon()
    bot = Bot(config, rcon=rcon, clock=FakeClock())
    bot.add_plugin(AdminPlugin(bot), "admin")
    bot.start()
    rcon.commands.clear()  # drop the profile's startup cvars
    return bot, rcon


# -- the helper itself -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("plain reason", "plain reason"),
        ('hax"; quit', "hax quit"),  # the quote and the separator both go
        ("line one\nquit", "line one quit"),  # the newline the engine splits on
        ("a\r\nb", "a b"),
        ("tabs\tand   spaces", "tabs and spaces"),  # runs collapse
        ("nul\x00byte", "nul byte"),
        ("  padded  ", "padded"),
        ("^1colours^7 stay", "^1colours^7 stay"),  # harmless, and admins expect them
    ],
)
def test_sanitize_rcon_value(raw, expected):  # noqa: ANN001
    assert sanitize_rcon_value(raw) == expected


def test_sanitize_rcon_value_caps_length():
    assert len(sanitize_rcon_value("x" * 500)) == MAX_RCON_VALUE


def test_sanitize_rcon_value_can_skip_the_cap():
    """Chat output passes None: `Messages.wrap` has already bounded the line."""
    assert len(sanitize_rcon_value("x" * 500, None)) == 500


# -- through the real command path -----------------------------------------


def test_a_hostile_reason_produces_one_single_line_command(tmp_path):
    """CoD4X puts the reason on the command line, which is what makes this reachable."""
    bot, rcon = _bot(tmp_path)
    bob = Client(cid="2", guid=STEAM_BOB, name="Bob")
    bot.clients.add(bob)

    bot.ban(bob, reason='cheating"; quit\nmap mp_crash')

    assert rcon.commands == ["permban 2 cheating quit map mp_crash"]
    assert "\n" not in rcon.commands[0]


def test_a_hostile_player_name_cannot_alter_the_command(tmp_path):
    """The name is chosen by the player. A Quake3 infostring will happily carry a `;` in one."""
    bot, rcon = _bot(tmp_path, game="cod4")  # stock CoD4's unban takes the *name*
    bob = Client(cid="2", guid="b" * 32, name='Bob"; quit')
    bot.clients.add(bob)

    bot.unban(bob, reason="appeal granted")

    assert rcon.commands == ["unbanuser Bob quit"]


def test_a_hostile_name_in_an_announcement_cannot_alter_the_command(tmp_path):
    """Almost every announcement interpolates a name, so `say` is a live path too."""
    bot, rcon = _bot(tmp_path, game="cod4")

    bot.say('Bob"; quit was kicked')

    assert rcon.commands == ["say Bob quit was kicked"]


def test_set_cvar_value_cannot_close_its_own_quoting(tmp_path):
    bot, rcon = _bot(tmp_path, game="cod4")

    bot.set_cvar("sv_hostname", 'mine" ; quit')

    assert rcon.commands == ['set sv_hostname "mine quit"']


def test_a_reason_that_sanitises_to_nothing_still_reads_sensibly(tmp_path):
    bot, rcon = _bot(tmp_path)
    bob = Client(cid="2", guid=STEAM_BOB, name="Bob")
    bot.clients.add(bob)

    bot.ban(bob, reason=';;;"""')

    assert rcon.commands == ["permban 2 no reason given"]


# -- a command that cannot be filled in must not be sent -------------------


def test_a_ban_keyed_on_a_guid_is_not_sent_for_a_player_who_has_none(tmp_path, caplog):
    """Substituting an empty guid sends a syntactically valid command with a missing argument, which
    a server either rejects or applies to nothing while answering as though it worked. The player is
    removed with the kick verb instead, and the log says the ban cannot be enforced on their return.

    Reachable on every family whose ban verb takes an id: CoD4X's `unban <guid>`, Frostbite's
    `banList.add guid <id>`, Altitude's `addBan <vaporId>` — and a client the engine has not
    identified yet is ordinary, not exotic.
    """
    bot, rcon = _bot(tmp_path, game="altitude")  # its ban verb is keyed on the vapor id
    nobody = Client(cid="3", guid="", name="Unidentified")
    bot.clients.add(nobody)

    with caplog.at_level("WARNING"):
        bot.ban(nobody, "cheating")

    assert rcon.commands == ["kick Unidentified"]  # the kick verb, not a malformed addBan
    assert "no guid" in caplog.text
    assert "cannot be re-applied" in caplog.text  # and it is honest about what was lost


def test_the_same_holds_for_a_tempban_and_an_unban(tmp_path):
    bot, rcon = _bot(tmp_path, game="altitude")
    nobody = Client(cid="3", guid="", name="Unidentified")
    bot.clients.add(nobody)

    bot.tempban(nobody, minutes=30, reason="spam")
    assert rcon.commands == ["kick Unidentified"]

    rcon.commands.clear()
    bot.unban(nobody, "appeal granted")
    assert rcon.commands == []  # nothing to send; our own record is lifted either way


def test_a_ban_keyed_on_the_slot_is_unaffected(tmp_path):
    """Stock CoD4 bans by slot, so a missing guid does not stop the command being well formed."""
    bot, rcon = _bot(tmp_path, game="cod4")
    nobody = Client(cid="4", guid="", name="Unidentified")
    bot.clients.add(nobody)

    bot.ban(nobody, "cheating")

    assert rcon.commands == ["banclient 4"]
