"""Server-side mods that are not required, but give the bot better verbs — §2.2.

SourceMod's **"B3 Say"** is the case. It is a plugin written for the classic bot, and where it is
installed `b3_say`/`b3_hsay`/`b3_psay` draw the bot's messages far better on screen than the stock
`sm_` verbs. The classic parser asked `sm plugins list` at connect and switched templates when it saw
the name. Ours always used the stock verbs, which is correct on a plain SourceMod server and simply
misses the improvement on one that has the plugin.

The mechanism is the interesting half, and it is why this sat open: a `GameProfile` is **frozen**, on
purpose, so nothing loaded at runtime can edit a title. The profile therefore declares what it would
*become*, and the runtime swaps the whole object in once — rather than the classic's arrangement,
where the parser reached into itself and rewrote a template in flight.
"""

from __future__ import annotations

import pytest

from b3.config.schema import Config
from b3.parsers.profile import GameProfile, OptionalMod
from b3.parsers.source.parser import SourceParser
from b3.parsers.source.profiles import B3_SAY, CS2, INSURGENCY
from b3.runtime.bot import Bot

PLUGIN_LIST = """  Listing 3 plugins:
01 "Admin File Reader" (1.10.0.6502) by AlliedModders LLC
02 "B3 Say" (1.0.0) by Courgette
03 "Basic Commands" (1.10.0.6502) by AlliedModders LLC
"""

WITHOUT_B3_SAY = """  Listing 2 plugins:
01 "Admin File Reader" (1.10.0.6502) by AlliedModders LLC
02 "Basic Commands" (1.10.0.6502) by AlliedModders LLC
"""


class ScriptedRcon:
    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.replies = replies or {}
        self.sent: list[str] = []

    def command(self, cmd: str) -> str:
        self.sent.append(cmd)
        for prefix, reply in self.replies.items():
            if cmd.startswith(prefix):
                return reply
        return ""

    def close(self) -> None:
        pass


def _bot(game: str, rcon: ScriptedRcon) -> Bot:
    config = Config.model_validate(
        {"bot": {"database": "sqlite://"}, "server": {"game": game}},
    )
    return Bot(config, rcon=rcon)


# -- reading the listing --------------------------------------------------------


def test_the_plugin_names_are_read_out_of_the_listing() -> None:
    names = SourceParser(INSURGENCY).read_installed_mods(PLUGIN_LIST)
    assert names == ["Admin File Reader", "B3 Say", "Basic Commands"]


def test_a_name_with_a_space_survives() -> None:
    """Which is the whole reason the pattern reads the quotes: split on whitespace and every plugin
    here becomes "Admin", "B3" and "Basic"."""
    assert "B3 Say" in SourceParser(INSURGENCY).read_installed_mods(PLUGIN_LIST)


def test_a_server_without_sourcemod_lists_nothing() -> None:
    parser = SourceParser(INSURGENCY)
    assert parser.read_installed_mods('Unknown command "sm"') == []
    assert parser.read_installed_mods("") == []


# -- acting on it ---------------------------------------------------------------


def test_the_better_verbs_are_taken_when_the_plugin_is_there() -> None:
    bot = _bot("insurgency", ScriptedRcon({"sm plugins list": PLUGIN_LIST}))

    bot.apply_optional_mods()

    assert bot.profile.say_template == "b3_say %s"
    assert bot.profile.saybig_template == "b3_hsay %s"
    assert bot.profile.tell_template == 'b3_psay #%(guid)s "%(text)s"'


def test_the_stock_verbs_stay_when_it_is_not() -> None:
    bot = _bot("insurgency", ScriptedRcon({"sm plugins list": WITHOUT_B3_SAY}))

    bot.apply_optional_mods()

    assert bot.profile.say_template == "sm_say %s"
    assert bot.profile is INSURGENCY  # not even rebuilt


def test_the_parser_is_given_the_same_profile_not_a_copy() -> None:
    """Two profiles that disagree would be worse than either of them being wrong: the parser reads
    its own for guid rules and team names, and a copy that missed this swap is a second answer."""
    bot = _bot("insurgency", ScriptedRcon({"sm plugins list": PLUGIN_LIST}))

    bot.apply_optional_mods()

    assert bot.parser.profile is bot.profile


def test_what_it_actually_sends_changes() -> None:
    """The templates are the mechanism; this is the outcome an operator would notice."""
    rcon = ScriptedRcon({"sm plugins list": PLUGIN_LIST})
    bot = _bot("insurgency", rcon)
    bot.apply_optional_mods()
    rcon.sent.clear()

    bot.say("hello")

    assert rcon.sent == ["b3_say hello"]


def test_a_server_that_will_not_answer_keeps_the_stock_verbs() -> None:
    class Broken(ScriptedRcon):
        def command(self, cmd: str) -> str:
            raise OSError("connection reset")

    bot = _bot("insurgency", Broken())
    bot.apply_optional_mods()  # does not raise
    assert bot.profile.say_template == "sm_say %s"


def test_the_listing_is_asked_for_once_however_many_mods_are_declared() -> None:
    rcon = ScriptedRcon({"sm plugins list": PLUGIN_LIST})
    bot = _bot("insurgency", rcon)
    bot.profile = replace_optional_mods(
        bot.profile,
        (
            B3_SAY,
            OptionalMod(name="Something Else", command="sm plugins list", overrides={}),
        ),
    )

    bot.apply_optional_mods()

    assert rcon.sent.count("sm plugins list") == 1


def replace_optional_mods(profile: GameProfile, mods: tuple[OptionalMod, ...]) -> GameProfile:
    from dataclasses import replace

    return replace(profile, optional_mods=mods)


def test_an_override_naming_a_field_that_does_not_exist_is_refused() -> None:
    """Otherwise a typo is a no-op that looks exactly like a working feature — which is the failure
    shape this codebase keeps finding, so it is worth one check at the moment it would happen."""
    rcon = ScriptedRcon({"sm plugins list": PLUGIN_LIST})
    bot = _bot("insurgency", rcon)
    bot.profile = replace_optional_mods(
        bot.profile,
        (
            OptionalMod(
                name="B3 Say",
                command="sm plugins list",
                overrides={"say_tempIate": "b3_say %s"},  # capital I, not an l
            ),
        ),
    )

    with pytest.raises(ValueError, match="say_tempIate"):
        bot.apply_optional_mods()


# -- who declares it ------------------------------------------------------------


def test_insurgency_offers_it_and_cs2_cannot() -> None:
    """SourceMod has no CS2 build at all, so there is no `sm plugins list` to ask and no plugin to
    find. Declaring one there would be a round trip at every startup for a certain "no"."""
    assert INSURGENCY.optional_mods == (B3_SAY,)
    assert CS2.optional_mods == ()


def test_it_is_optional_and_sourcemod_is_not() -> None:
    """The distinction the two classes exist to draw: without SourceMod the bot cannot ban anybody,
    so it refuses to start. Without B3 Say it looks slightly worse and says nothing."""
    assert INSURGENCY.required_mod is not None
    assert B3_SAY.command == "sm plugins list"
