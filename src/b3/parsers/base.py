"""Parser base class.

The lifted parser *contract*: turn a raw game-log line into zero or more typed events. The base
owns the dispatch pipeline (timestamp strip + route through the :class:`LineRouter`); subclasses
declare their grammar with :func:`b3.parsers.registry.handles` and implement game operations.
"""

from __future__ import annotations

import re
from abc import ABC

from b3.core.clients import ClientManager
from b3.core.events import Event
from b3.parsers.profile import GameProfile
from b3.parsers.registry import LineRouter


class Parser(ABC):
    # Leading game-log timestamp (e.g. "  123:45 "). Overridable per game.
    _timestamp_re = re.compile(r"^\s*\d+:\d+\s?")

    def __init__(self, profile: GameProfile, clients: ClientManager | None = None) -> None:
        # The profile belongs here rather than in each engine's parser: every parser has one, and
        # `b3.parsers.games.parser_for` builds whichever the configured title names — which it can
        # only do if they all take the same two arguments.
        self.profile = profile
        # NB: use an explicit None check — an empty ClientManager is falsy (__len__ == 0),
        # so `clients or ClientManager()` would wrongly discard a shared-but-empty manager.
        self.clients = clients if clients is not None else ClientManager()
        self._router = LineRouter.from_instance(self)

    def parse_line(self, line: str) -> list[Event]:
        """Route one log line to its handler and return the events it produced."""
        line = self._timestamp_re.sub("", line.strip(), count=1)
        if not line:
            return []
        matched = self._router.match(line)
        if matched is None:
            return []
        match, handler = matched
        result = handler(match)
        if result is None:
            return []
        if isinstance(result, Event):
            return [result]
        return list(result)
