"""Message templates and game-chat line wrapping (the legacy getMessage / getWrap)."""

from __future__ import annotations

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
