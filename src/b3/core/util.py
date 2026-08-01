"""Small shared helpers."""

from __future__ import annotations

import difflib
import logging
import re
from collections.abc import Sequence
from datetime import datetime, tzinfo

log = logging.getLogger(__name__)

#: How close a difflib ratio has to be before a guess is offered at all.
FUZZY_CUTOFF = 0.6


def match_names(wanted: str, options: Sequence[tuple[str, str]]) -> list[str]:
    """Resolve what somebody typed against ``(value, label)`` pairs; returns the matching values.

    Four steps, narrowest first — exact, prefix, substring, then a difflib ratio — and the first
    step that matches anything wins. That keeps an exact answer from being ambiguous with a longer
    one: "metro" resolves to a map of that name even when "metro 2014" is also in the rotation.

    Both halves of each pair are searched, so a caller can offer an id and a display name for the
    same thing and accept either. Duplicates are collapsed, first occurrence winning, so results
    come back in the caller's order.
    """
    needle = wanted.strip().lower()
    if not needle:
        return []
    pairs = [(value, [text.lower() for text in (value, label) if text]) for value, label in options]

    exact = [value for value, texts in pairs if needle in texts]
    if exact:
        return list(dict.fromkeys(exact))
    prefixed = [value for value, texts in pairs if any(t.startswith(needle) for t in texts)]
    if prefixed:
        return list(dict.fromkeys(prefixed))
    contained = [value for value, texts in pairs if any(needle in t for t in texts)]
    if contained:
        return list(dict.fromkeys(contained))

    close = set(
        difflib.get_close_matches(
            needle, [t for _, texts in pairs for t in texts], n=5, cutoff=FUZZY_CUTOFF
        )
    )
    return list(dict.fromkeys(v for v, texts in pairs if close.intersection(texts)))


#: How `!time`, `!seen` and `!lookup` render a timestamp. The classic bot's `formatTime` used the
#: locale's `%c`, which is unreadable in a game chat line; this is short, sortable and unambiguous.
TIME_FORMAT = "%Y-%m-%d %H:%M"


def format_time(epoch: float, tz: tzinfo | None = None) -> str:
    """Render an epoch timestamp in the bot's configured zone — the legacy ``formatTime``."""
    return datetime.fromtimestamp(epoch, tz).strftime(TIME_FORMAT)


def duration_text(minutes: float) -> str:
    """Human-readable duration, e.g. ``90`` -> ``1.5 hours`` (the legacy ``minutesStr``)."""
    minutes = max(0.0, minutes)
    if minutes < 1:
        return f"{int(minutes * 60)} seconds"
    if minutes < 60:
        return f"{_trim(minutes)} minute{'' if minutes == 1 else 's'}"
    hours = minutes / 60
    if hours < 24:
        return f"{_trim(hours)} hour{'' if hours == 1 else 's'}"
    days = hours / 24
    if days < 365:
        return f"{_trim(days)} day{'' if days == 1 else 's'}"
    return f"{_trim(days / 365)} years"


def _trim(value: float) -> str:
    """Drop a trailing ``.0`` so durations read as '2 hours', not '2.0 hours'."""
    return f"{value:.1f}".removesuffix(".0")


def as_int(value: object, default: int) -> int:
    """Read a config value as an int, falling back to ``default`` and saying so.

    Settings arrive from YAML as whatever the operator typed, so a plugin reading
    ``int(settings["max_ping"])`` crashes the bot at startup if that line says ``5oo``. Falling back
    keeps the server moderated; logging it means the typo is findable, which a silent default is
    not.
    """
    if isinstance(value, bool):  # bool is an int subclass, and `True` is not a count
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(float(value))  # "30" and 30.0 both mean 30
        except ValueError:
            pass
    log.warning("config value %r is not a whole number; using %r", value, default)
    return default


def as_float(value: object, default: float) -> float:
    """Read a config value as a float. See :func:`as_int`."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    log.warning("config value %r is not a number; using %r", value, default)
    return default


#: Characters that must never reach a game console. A Quake3-family engine splits its command
#: buffer on newlines and on `;`, and a `"` opens a quoted token that swallows the rest of the line
#: — so any of them inside a value can end the command the bot meant to send and begin another one.
#: Whether an *rcon* command reaches the shared command buffer (and so honours `;`) depends on the
#: engine, which is exactly why all three are removed rather than reasoned about per title.
_RCON_UNSAFE_RE = re.compile(r'[\x00-\x1f\x7f;"]+')

#: Cap for a substituted value. Long enough for any real ban reason, short enough that the command
#: still fits in one datagram alongside the verb and the password.
MAX_RCON_VALUE = 128


def sanitize_rcon_value(value: object, max_length: int | None = MAX_RCON_VALUE) -> str:
    """Make a value safe to substitute into an RCON command.

    Applied to everything player- or admin-supplied that ends up on a command line: ban reasons,
    player names, guids and chat output. Control characters and command separators become spaces,
    runs of whitespace collapse, and the result is capped — so a reason typed as ``hax"; quit``
    cannot end the ban command and start another one.
    """
    text = _RCON_UNSAFE_RE.sub(" ", str(value))
    text = " ".join(text.split())
    if max_length is not None and len(text) > max_length:
        text = text[:max_length].rstrip()
    return text


def parse_duration(text: str) -> int:
    """Parse a human duration into minutes.

    Accepts a bare number (minutes) or a suffixed value: ``m`` minutes, ``h`` hours, ``d`` days,
    ``w`` weeks. Raises ``ValueError`` on anything else.
    """
    s = text.strip().lower()
    if not s:
        raise ValueError("empty duration")
    unit = s[-1]
    factors = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}
    if unit in factors:
        return int(float(s[:-1]) * factors[unit])
    return int(float(s))  # bare number == minutes
