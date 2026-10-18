"""Colour for the terminal: green for what is fine, yellow for what is worth a look, red for what is
broken - on Windows, Linux and macOS alike.

Colour is decoration on output that must read perfectly well without it, so it is **only ever added
for a person at a terminal**:

* not when the output is a pipe or a file - `b3 doctor > report.txt` and `b3 probe | clip` stay plain
  text, which is what somebody pastes into an issue;
* not when `NO_COLOR` is set (https://no-color.org), nor on a `TERM=dumb` terminal;
* forced on with `FORCE_COLOR`, for a CI log that renders it.

**Windows** needs one more step: its console understands these escape codes only once a program asks
for them (`ENABLE_VIRTUAL_TERMINAL_PROCESSING`, Windows 10 and later). Where that request is refused
- an older Windows, or a console that cannot - colour is simply off, rather than the screen filling
with `[32m`.

The codes are plain ASCII, so a cp1252 console that cannot print an em dash prints these fine.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TextIO

RESET = "\033[0m"
BOLD = "1"
DIM = "2"
RED = "31"
GREEN = "32"
YELLOW = "33"
CYAN = "36"

_decided: dict[int, bool] = {}


def _windows_ansi(stream: TextIO) -> bool:
    """Ask the Windows console to interpret escape codes. True if it agreed."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32 = ctypes.windll.kernel32
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_virtual_terminal_processing = 0x0004
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal_processing)
        )
    except Exception:  # noqa: BLE001 - no colour is the answer to anything going wrong here
        return False


def enabled(stream: TextIO | None = None) -> bool:
    """Whether to colour what is written to `stream` (stdout when not given)."""
    target = stream if stream is not None else sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        if not target.isatty():
            return False
    except (AttributeError, ValueError):  # a closed or replaced stream
        return False
    # Asked once per stream: the answer does not change while the program runs, and on Windows the
    # question is a system call.
    key = id(target)
    if key not in _decided:
        _decided[key] = _windows_ansi(target)
    return _decided[key]


def paint(text: str, *codes: str, stream: TextIO | None = None) -> str:
    """`text` in the given style when the stream is a colour terminal, and unchanged otherwise."""
    if not codes or not text or not enabled(stream):
        return text
    return f"\033[{';'.join(codes)}m{text}{RESET}"


def ok(text: str, stream: TextIO | None = None) -> str:
    return paint(text, GREEN, stream=stream)


def warn(text: str, stream: TextIO | None = None) -> str:
    return paint(text, YELLOW, stream=stream)


def fail(text: str, stream: TextIO | None = None) -> str:
    return paint(text, RED, BOLD, stream=stream)


def hint(text: str, stream: TextIO | None = None) -> str:
    """What the operator can type or do next - the `?`, a command to run."""
    return paint(text, CYAN, stream=stream)


def heading(text: str, stream: TextIO | None = None) -> str:
    return paint(text, BOLD, stream=stream)


def quiet(text: str, stream: TextIO | None = None) -> str:
    """Secondary: a default in brackets, a path the eye can skip."""
    return paint(text, DIM, stream=stream)


class LevelColours(logging.Formatter):
    """A log formatter that colours the level name - the part of a log line the eye looks for."""

    COLOURS = {
        logging.DEBUG: (DIM,),
        logging.WARNING: (YELLOW,),
        logging.ERROR: (RED, BOLD),
        logging.CRITICAL: (RED, BOLD),
    }

    def __init__(self, fmt: str, stream: TextIO | None = None) -> None:
        super().__init__(fmt)
        self._stream = stream

    def format(self, record: logging.LogRecord) -> str:
        codes = self.COLOURS.get(record.levelno)
        if not codes or not enabled(self._stream or sys.stderr):
            return super().format(record)
        original = record.levelname
        record.levelname = paint(original, *codes, stream=self._stream or sys.stderr)
        try:
            return super().format(record)
        finally:
            record.levelname = original


def configure_logging(level: int, fmt: str) -> None:
    """`logging.basicConfig`, with the level names coloured on a colour terminal."""
    handler = logging.StreamHandler()
    handler.setFormatter(LevelColours(fmt, sys.stderr))
    logging.basicConfig(level=level, handlers=[handler])


__all__ = [
    "LevelColours",
    "configure_logging",
    "enabled",
    "fail",
    "heading",
    "hint",
    "ok",
    "paint",
    "quiet",
    "warn",
]
