"""Colour on a terminal, and nothing but the text anywhere else.

The rule that matters is the second half: `b3 doctor > report.txt` and `b3 probe | clip` are how a
report reaches an issue tracker, and escape codes in a pasted report are noise somebody has to edit
out by hand.
"""

from __future__ import annotations

import io
import logging

import pytest

from b3.core import style


class Terminal(io.StringIO):
    """A stream that says it is a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):  # noqa: ANN001, ANN202
    for name in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)
    # Not a real Windows console, so there is nothing to switch on: say it agreed.
    monkeypatch.setattr(style, "_windows_ansi", lambda stream: True)  # noqa: ARG005
    style._decided.clear()


def test_a_terminal_gets_colour():
    assert style.ok("fine", Terminal()) == "\033[32mfine\033[0m"
    assert style.fail("broken", Terminal()) == "\033[31;1mbroken\033[0m"


def test_a_pipe_or_a_file_gets_the_text_alone():
    assert style.ok("fine", io.StringIO()) == "fine"


def test_no_color_turns_it_off(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert style.warn("look", Terminal()) == "look"


def test_force_color_turns_it_on_even_into_a_pipe(monkeypatch):
    """For a CI log that renders colour, where the output is never a terminal."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert style.warn("look", io.StringIO()) == "\033[33mlook\033[0m"


def test_a_dumb_terminal_gets_none(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert style.hint("type ?", Terminal()) == "type ?"


def test_a_windows_console_that_refuses_gets_none(monkeypatch):
    """Older Windows: the console will not interpret the codes, so they would print as `[32m`."""
    monkeypatch.setattr(style, "_windows_ansi", lambda stream: False)  # noqa: ARG005
    assert style.ok("fine", Terminal()) == "fine"


def test_log_level_names_are_coloured_on_a_terminal_only():
    record = logging.LogRecord("b3", logging.ERROR, __file__, 1, "rcon is down", None, None)

    coloured = style.LevelColours("%(levelname)s %(message)s", Terminal()).format(record)
    plain = style.LevelColours("%(levelname)s %(message)s", io.StringIO()).format(record)

    assert coloured == "\033[31;1mERROR\033[0m rcon is down"
    assert plain == "ERROR rcon is down"
    assert record.levelname == "ERROR"  # put back for any other handler


def test_doctor_colours_inside_the_brackets_so_the_columns_still_line_up(monkeypatch, capsys):
    """Escape codes take no space on screen but do in a string: outside a fixed-width field they
    would shift every column after them."""
    from b3 import cli
    from b3.core import doctor

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setattr(
        doctor,
        "run_checks",
        lambda *a, **kw: [  # noqa: ARG005
            doctor.Check("game", doctor.Status.OK, "cod4x"),
            doctor.Check("game_log", doctor.Status.FAIL, "missing", "point it at the log"),
        ],
    )

    assert cli._run_doctor(object(), None) == 1  # type: ignore[arg-type]

    out = capsys.readouterr().out
    assert "[\033[32m  ok  \033[0m]" in out
    assert "[\033[31;1m FAIL \033[0m]" in out
    assert "\033[36m-> point it at the log\033[0m" in out


def test_a_probe_report_is_coloured_by_what_each_line_is(monkeypatch):
    from b3.core.probe import colour

    monkeypatch.setenv("FORCE_COLOR", "1")
    assert colour("FAILED: TimeoutError: timed out").startswith("\033[31")
    assert colour("  ! 0  12  47 ?? Admin").startswith("\033[31")
    assert colour("UNMATCHED 0").startswith("\033[32")
    assert colour("UNMATCHED 3").startswith("\033[33")
    assert colour("raw reply (120 bytes):") == "raw reply (120 bytes):"
