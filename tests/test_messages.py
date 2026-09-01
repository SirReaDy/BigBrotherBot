"""Message templates and game-chat line wrapping (the legacy getMessage / getWrap)."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest

from b3.core.messages import DEFAULT_MESSAGES, Messages


# -- lookup and formatting -------------------------------------------------


def test_defaults_are_used_when_nothing_is_configured():
    messages = Messages()
    assert messages.get("kicked", name="Bob", reason="spam") == "Bob was kicked (spam)"


def test_config_overrides_a_default():
    messages = Messages({"kicked": "^1{name}^7 got the boot: {reason}"})
    assert messages.get("kicked", name="Bob", reason="spam") == "^1Bob^7 got the boot: spam"
    # Untouched keys still come from the defaults.
    assert messages.get("iamgod_done") == DEFAULT_MESSAGES["iamgod_done"]


def test_an_operators_template_may_ignore_placeholders():
    messages = Messages({"kicked": "player removed"})
    assert messages.get("kicked", name="Bob", reason="spam") == "player removed"


def test_a_missing_placeholder_is_logged_not_raised(caplog):
    """A typo in someone's config must not take the command down mid-reply."""
    messages = Messages({"kicked": "{name} was kicked by {nonexistent}"})
    with caplog.at_level("WARNING"):
        result = messages.get("kicked", name="Bob", reason="spam")
    assert result == "{name} was kicked by {nonexistent}"  # verbatim, no exception
    assert "could not be formatted" in caplog.text


def test_an_unknown_key_is_visible_rather_than_silent(caplog):
    messages = Messages()
    with caplog.at_level("WARNING"):
        assert messages.get("no_such_message") == "[no_such_message]"
    assert "no message defined for" in caplog.text


def test_an_unused_config_key_is_flagged_at_startup(caplog):
    with caplog.at_level("WARNING"):
        Messages({"typoed_key": "hello"})
    assert "which no core command uses" in caplog.text


def test_template_returns_the_raw_string():
    messages = Messages({"kicked": "raw {name}"})
    assert messages.template("kicked") == "raw {name}"
    assert messages.template("iamgod_done") == DEFAULT_MESSAGES["iamgod_done"]


# -- wrapping --------------------------------------------------------------


def test_short_text_is_a_single_line():
    assert Messages().wrap("hello there") == ["hello there"]


def test_empty_text_produces_no_lines():
    assert Messages().wrap("") == []


def test_long_text_is_split_on_word_boundaries():
    messages = Messages(line_length=20)
    lines = messages.wrap("the quick brown fox jumps over the lazy dog")

    assert len(lines) > 1
    assert all(len(line) <= 20 for line in lines)
    # No word is broken apart.
    assert " ".join(lines).split() == "the quick brown fox jumps over the lazy dog".split()


def test_a_word_longer_than_the_limit_is_broken_rather_than_dropped():
    messages = Messages(line_length=10)
    lines = messages.wrap("x" * 25)
    assert "".join(lines) == "x" * 25
    assert all(len(line) <= 10 for line in lines)


def test_embedded_newlines_start_a_new_line():
    messages = Messages(line_length=90)
    assert messages.wrap("first\nsecond") == ["first", "second"]
    # A literal backslash-n, which is how the legacy config expressed a break.
    assert messages.wrap("first\\nsecond") == ["first", "second"]


def test_blank_paragraphs_are_dropped():
    assert Messages().wrap("first\n\n\nsecond") == ["first", "second"]


def test_continuation_lines_get_the_colour_prefix():
    messages = Messages(line_length=20, color_prefix="^3")
    lines = messages.wrap("the quick brown fox jumps over the lazy dog")

    assert not lines[0].startswith("^3")  # the first line keeps the caller's own colours
    assert all(line.startswith("^3") for line in lines[1:])


def test_no_prefix_is_added_to_a_single_line():
    messages = Messages(line_length=90, color_prefix="^3")
    assert messages.wrap("short") == ["short"]


@pytest.mark.parametrize("length", [1, 5, 8])
def test_a_silly_line_length_still_produces_output(length):
    """Guard the wrapper's minimum width so a bad config cannot make it loop or crash."""
    lines = Messages(line_length=length).wrap("some words here")
    assert lines and all(line for line in lines)


# -- plugin-supplied templates -----------------------------------------------------------------


def test_a_plugin_can_register_its_own_messages():
    """Without this a plugin's text is hardcoded English while the core's is customisable."""
    messages = Messages()
    messages.register_defaults({"chatlog_line": "[{when}] {name}: {text}"})

    assert messages.get("chatlog_line", when="now", name="Bob", text="hi") == "[now] Bob: hi"


def test_an_operator_override_beats_a_plugin_default():
    messages = Messages({"chatlog_line": "{name} said {text}"})
    messages.register_defaults({"chatlog_line": "[{when}] {name}: {text}"})

    assert messages.get("chatlog_line", when="now", name="Bob", text="hi") == "Bob said hi"


def test_a_plugin_cannot_shadow_a_core_message(caplog):
    messages = Messages()
    with caplog.at_level("WARNING"):
        messages.register_defaults({"kicked": "something else entirely"})

    assert messages.get("kicked", name="Bob", reason="spam") == "Bob was kicked (spam)"
    assert "would shadow a core message" in caplog.text


def test_two_plugins_registering_the_same_key_keep_the_first():
    messages = Messages()
    messages.register_defaults({"shared": "first"})
    messages.register_defaults({"shared": "second"})
    assert messages.get("shared") == "first"


def test_every_shipped_message_can_be_printed_by_the_game():
    """A message a console cannot encode is a message nobody reads.

    Found on a live CoD4X server: the greeting arrived as "player #2 ? here 5 times". The Call of
    Duty and Quake 3 families read and write **latin-1**, so the em dash these tables were written
    with — U+2014, which is not in latin-1 — reached the player as a question mark. Sixty-three
    shipped messages had one, across the core table and twenty-two plugins.

    Checked against latin-1 rather than ASCII because that is the narrowest encoding any supported
    title uses, and because an accented name in a message is fine while typographic punctuation is
    not.
    """
    import importlib
    import pkgutil

    import b3.plugins
    from b3.core.messages import DEFAULT_MESSAGES

    tables = {"core": DEFAULT_MESSAGES}
    for module in pkgutil.iter_modules(b3.plugins.__path__):
        plugin = importlib.import_module(f"b3.plugins.{module.name}")
        # **Every** module-level table of text, not just the one called MESSAGES. Checking that name
        # alone missed `spree.DEFAULT_KILLING_SPREES`, whose five-kill line carried an em dash all
        # the way to a live server: "is on a killing spree ? 5 kills in a row". A plugin's
        # announcements are messages whatever the constant holding them is called.
        for name in dir(plugin):
            if name.startswith("_") or name == "TEMPLATES":
                continue  # TEMPLATES goes to Discord, which is UTF-8 — see the discord plugin
            table = getattr(plugin, name)
            if isinstance(table, dict) and table:
                tables[f"{module.name}.{name}"] = table

    def strings(value: object) -> "Iterator[str]":
        """Every line in a table entry — some hold a tuple of them, one per situation."""
        if isinstance(value, str):
            yield value
        elif isinstance(value, (tuple, list)):
            for item in value:
                yield from strings(item)

    unprintable = [
        f"{owner}[{key!r}]: {char!r} in {text!r}"
        for owner, table in tables.items()
        for key, value in table.items()
        for text in strings(value)
        for char in text
        if ord(char) > 255
    ]
    assert not unprintable, "\n".join(unprintable)


def test_every_shipped_example_config_can_be_printed_by_the_game():
    """The operator's copy comes from `examples/`, so every message ships twice.

    Fixing a plugin default and leaving the example fixes nothing: `b3 init` copies these files, and
    what an operator has in front of them is what their server says. The spree em dash was in both,
    and it was the copy that reached the player.

    `plugin_discord.yaml` is exempt, and is the one file that should be: its templates go to
    Discord, which is UTF-8, and the emoji in them are the point.
    """
    import yaml

    root = pathlib.Path(__file__).resolve().parent.parent / "examples"
    # Only the sections whose values are *said in the game*. Comments are not loaded by yaml at all,
    # so the prose explaining each setting stays free to use whatever punctuation reads best.
    spoken = ("messages", "spamages", "warn_reasons", "sprees", "killing_sprees", "losing_sprees")
    unprintable: list[str] = []

    def walk(value: object, where: str, name: str) -> None:
        if isinstance(value, str):
            unprintable.extend(
                f"{name} {where}: {char!r} in {value!r}" for char in value if ord(char) > 255
            )
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{where}.{key}", name)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{where}[{index}]", name)

    for path in sorted(root.glob("*.yaml")):
        if path.name == "plugin_discord.yaml":
            continue
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            continue
        for section in spoken:
            if section in loaded:
                walk(loaded[section], section, path.name)

    assert not unprintable, "\n".join(unprintable)
