"""Declarative line-pattern registry.

Replaces the legacy ``getLineParts`` + ``On<Capwords>(action)`` reflection dispatch (the source of
the ``parseLine``/``parse_line`` dead-code bug) with an explicit, ordered registry: a handler method
is decorated with :func:`handles` and the regex + handler live together. Patterns are matched in
**definition order, first match wins** — preserving the order-sensitivity of the CoD grammar.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any


def handles(pattern: str, flags: int = 0) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a parser method as the handler for log lines matching ``pattern``."""
    compiled = re.compile(pattern, flags)

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func._line_pattern = compiled  # type: ignore[attr-defined]
        return func

    return decorator


class LineRouter:
    """Ordered (regex -> bound handler) table built from an object's decorated methods."""

    def __init__(self, patterns: list[tuple[re.Pattern[str], Callable[..., Any]]]) -> None:
        self._patterns = patterns

    @classmethod
    def from_instance(cls, obj: object) -> "LineRouter":
        patterns: list[tuple[re.Pattern[str], Callable[..., Any]]] = []
        # Walk the MRO base-first so inherited patterns precede subclass ones, and within each
        # class use definition order (class __dict__ preserves insertion order on 3.7+).
        for klass in reversed(type(obj).__mro__):
            for name, member in vars(klass).items():
                pattern = getattr(member, "_line_pattern", None)
                if pattern is not None:
                    patterns.append((pattern, getattr(obj, name)))
        return cls(patterns)

    def match(self, line: str) -> tuple[re.Match[str], Callable[..., Any]] | None:
        for regex, handler in self._patterns:
            m = regex.match(line)
            if m is not None:
                return m, handler
        return None

    def __len__(self) -> int:
        return len(self._patterns)
