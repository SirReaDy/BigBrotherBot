"""Live server state — what the bot knows about the match in progress.

The classic bot had ``b3/game.py``: a ``Game`` object holding the current map, gametype, round
timings and a cvar cache, updated from ``InitGame``/``ExitLevel`` log lines. The rewrite had no
counterpart at all, so nothing could answer "what map are we on?" without asking the server.

Same idea here, minus the legacy quirks: attributes are typed and explicit rather than created on
the fly by ``setCvar`` (which used to let ``game.g_gametype`` spring into existence and silently
shadow ``game.gameType``), and elapsed times are computed from an injected clock rather than
``time.time()`` calls scattered through the parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class PlayerInfo:
    """One row of the server's live ``status`` table.

    This is what the log lines cannot tell us: the player's **IP** (which is why authentication
    needs a status poll at all), plus ping and score.
    """

    cid: str
    name: str = ""
    #: The player's persistent identity for this engine — on CoD4X that is the Steam64 id.
    guid: str = ""
    #: The raw Steam64 id when the engine reports one (CoD4X with `sv_usesteam64id 1`).
    steam_id: str = ""
    ip: str = ""
    port: int = 0
    ping: int = 0
    score: int = 0


@dataclass(slots=True)
class Game:
    """State of the match currently being played."""

    map_name: str = ""
    gametype: str = ""
    rounds: int = 0
    #: Epoch seconds when the current map / round started; None until the first one is seen.
    map_start: float | None = None
    round_start: float | None = None
    #: Server cvars as last read (``mapname``, ``sv_maxclients``, …). Populated on demand.
    cvars: dict[str, str] = field(default_factory=dict)

    def start_map(self, cvars: dict[str, str], now: float) -> None:
        """Record a new map from the cvar dump on an ``InitGame`` line."""
        self.cvars.update(cvars)
        self.map_name = cvars.get("mapname", self.map_name)
        self.gametype = cvars.get("g_gametype", self.gametype)
        self.map_start = now
        self.round_start = now
        self.rounds = 1

    def start_round(self, now: float) -> None:
        self.rounds += 1
        self.round_start = now

    def map_uptime(self, now: float) -> float:
        """Seconds since the current map started (0 if we never saw it start)."""
        return 0.0 if self.map_start is None else max(0.0, now - self.map_start)

    def round_uptime(self, now: float) -> float:
        return 0.0 if self.round_start is None else max(0.0, now - self.round_start)
