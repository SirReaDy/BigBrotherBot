"""The bot's message prefixes — the classic's `msgPrefix`, `pmPrefix` and `deadPrefix`.

`bot.prefix` was declared in the config schema, documented as working, given the classic's own
default (`^2(b3)^7:`) — and **read by nothing**. An operator who set it saw no change in game. This
is what it does now that it is wired.

Why it matters beyond the broken promise: without it the bot's messages are indistinguishable from
player chat on a busy server, which is the whole reason the classic had one. A player needs to be
able to tell "you were warned for teamkilling" from something another player typed.

**The prefix is part of the line-wrapping budget**, not added afterwards. That is the one thing here
with a trap in it: a prefix bolted on after wrapping pushes the first line past the engine's chat
limit, and on Call of Duty that limit is 65 characters and the engine drops the overflow rather than
wrapping it — so the first line of every reply would be cut off mid-sentence.
"""

from __future__ import annotations

import pytest

from b3.config.schema import Config
from b3.core.messages import Messages
from b3.domain.client import Client
from b3.parsers.source.profiles import B3_SAY
from b3.runtime.bot import Bot

PREFIX = "^2(b3)^7:"
PM = "^8[pm]^7"


class RecordingRcon:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        return ""

    def close(self) -> None:
        pass


def _bot(rcon: RecordingRcon, game: str = "cod4", **bot_config: object) -> Bot:
    config = Config.model_validate(
        {
            "bot": {"database": "sqlite://", **bot_config},
            "server": {"game": game},
        },
    )
    return Bot(config, rcon=rcon)


# -- composing the prefixes -----------------------------------------------------


def test_prefixes_are_joined_with_single_spaces():
    """The classic `prefixText`: each non-empty prefix, then the text."""
    messages = Messages(prefix="P")
    assert messages.prefixed("hello") == "P hello"
    assert messages.prefixed("hello", "Q") == "P Q hello"


def test_an_empty_prefix_is_skipped_rather_than_leaving_a_gap():
    assert Messages(prefix="").prefixed("hello") == "hello"
    assert Messages(prefix="P").prefixed("hello", "") == "P hello"


def test_empty_text_stays_empty_rather_than_becoming_a_lone_prefix():
    """An announcement with nothing in it should send nothing, not "(b3):"."""
    assert Messages(prefix="P").prefixed("") == ""
    assert Messages(prefix="P").wrap("") == []


# -- the wrap budget, which is the whole reason this lives in Messages ----------


def test_the_prefix_is_inside_the_line_limit_not_added_to_it():
    messages = Messages(line_length=30, prefix="^2(b3)^7:")

    lines = messages.wrap("the quick brown fox jumps over the lazy dog")

    assert all(len(line) <= 30 for line in lines), lines
    assert lines[0].startswith("^2(b3)^7:")


def test_a_prefix_only_appears_once_however_many_lines_the_text_takes():
    """As in the classic: prefixed once, then wrapped — not prefixed per line."""
    messages = Messages(line_length=30, prefix="P")

    lines = messages.wrap("the quick brown fox jumps over the lazy dog and keeps going")

    assert len(lines) > 1
    assert lines[0].startswith("P ")
    assert not any(line.startswith("P ") for line in lines[1:])


def test_a_long_prefix_does_not_produce_an_empty_first_line():
    """A guard on the arithmetic rather than a real configuration: whatever the operator sets, the
    text still has to come out."""
    messages = Messages(line_length=20, prefix="^1[a very long server tag]^7:")

    lines = messages.wrap("hello")

    assert "hello" in " ".join(lines)


# -- what actually goes out ------------------------------------------------------


def test_a_broadcast_carries_the_prefix():
    rcon = RecordingRcon()
    _bot(rcon).say("round starting")
    assert rcon.sent == [f"say {PREFIX} round starting"]


def test_a_private_reply_carries_the_pm_marker_on_top():
    """So a player can tell a reply meant for them from a broadcast that happens to name them."""
    rcon = RecordingRcon()
    bot = _bot(rcon)

    bot.tell(Client(guid="G", name="Bob", cid="2"), "you were warned")

    assert rcon.sent == [f"tell 2 {PREFIX} {PM} you were warned"]


def test_dead_chat_carries_its_own_marker_and_not_the_pm_one():
    """It is dead *chat*, not a private message — the classic drew the same distinction."""
    rcon = RecordingRcon()
    bot = _bot(rcon, dead_prefix="[DEAD]^7")
    bot.clients.add(Client(guid="G", name="Bob", cid="2", alive=False))

    bot.say_dead("the bomb was planted")

    assert rcon.sent == [f"tell 2 {PREFIX} [DEAD]^7 the bomb was planted"]
    assert PM not in rcon.sent[0]


def test_a_centre_screen_announcement_is_prefixed_too():
    rcon = RecordingRcon()
    _bot(rcon).say_big("round over")
    assert rcon.sent == [f"say {PREFIX} round over"]


def test_an_operator_can_change_it():
    rcon = RecordingRcon()
    _bot(rcon, prefix="^1[MyServer]^7").say("hello")
    assert rcon.sent == ["say ^1[MyServer]^7 hello"]


def test_an_operator_can_turn_it_off_entirely():
    rcon = RecordingRcon()
    _bot(rcon, prefix="", pm_prefix="").say("hello")
    assert rcon.sent == ["say hello"]


def test_the_defaults_are_the_classic_bot_s_own_values():
    """Not invented here: `msgPrefix` came from config with this default, and these are the two
    literals from `b3/parser.py`."""
    config = Config.model_validate({"bot": {}, "server": {}})
    assert config.bot.prefix == "^2(b3)^7:"
    assert config.bot.pm_prefix == "^8[pm]^7"
    assert config.bot.dead_prefix == "[DEAD]^7"


# -- the one title that switches it off ------------------------------------------


def test_b3_say_turns_the_prefix_off_as_the_classic_did():
    """A mod that draws the bot's messages distinctly itself makes the bot's own marker clutter —
    and clutter that costs part of the line budget."""
    assert B3_SAY.overrides["prefix_messages"] is False


def test_installing_b3_say_stops_the_prefix_going_out():
    plugin_list = '01 "B3 Say" (1.0.0) by Courgette\n'

    class SmRcon(RecordingRcon):
        def command(self, cmd: str) -> str:
            self.sent.append(cmd)
            return plugin_list if cmd == "sm plugins list" else ""

    rcon = SmRcon()
    bot = _bot(rcon, game="insurgency")
    bot.apply_optional_mods()
    rcon.sent.clear()

    bot.say("hello")

    assert rcon.sent == ["b3_say hello"]


def test_without_it_the_stock_verb_is_prefixed():
    rcon = RecordingRcon()
    bot = _bot(rcon, game="insurgency")
    bot.apply_optional_mods()  # the fake answers nothing, so no mod is found
    rcon.sent.clear()

    bot.say("hello")

    assert rcon.sent == [f"sm_say {PREFIX} hello"]


@pytest.mark.parametrize("game", ["cod4", "q3", "bf3", "insurgency", "cs2"])
def test_every_family_prefixes_by_default(game: str):
    rcon = RecordingRcon()
    bot = _bot(rcon, game=game)
    assert bot.messages.prefix == PREFIX
