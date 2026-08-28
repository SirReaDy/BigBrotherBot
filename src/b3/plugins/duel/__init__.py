"""Keeps score between two players who have agreed to settle it.

A port of the classic `duel` plugin. One player challenges another, the other accepts, and from then
on every kill between the two of them is counted and both are told the score privately. It is the
smallest possible competitive feature and it is entirely social: nobody else on the server sees any of
it.

Kept from the classic: the three commands, the propose-then-accept handshake, a private score to each
player after every kill and at the end of a round, and several duels at once with the name required to
say which one you mean.

Changed, and the first is a fault:

* **A duel between two players on the same team never scored.** The classic counted `EVT_CLIENT_KILL`
  only, and two teammates shooting each other produce `EVT_CLIENT_KILL_TEAM` — so the most common
  duel there is, two people on a friendly-fire server settling something, silently kept a score of
  0:0. Gibs were missed for the same reason, which on Enemy Territory is most kills. All four are
  counted here.
* **A duel is one object in one place.** The classic stored each accepted duel in *both* players'
  variable dictionaries, keyed by the other player's `Client` object, which is why cancelling it
  needed two deletes wrapped in two `except KeyError`, why a disconnect had to walk every client on
  the server to find it, and why a departed player's object stayed referenced by whoever they had been
  duelling. The plugin owns a list.
* **A proposal can be turned down**, and expires. The classic's had no answer but silence and no end:
  a challenge sat in the challenger's dictionary until one of them left the server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Every way one player can kill another. The team-kill and gib spellings are the ones the classic
#: missed, and between them they are how most duels are actually fought: teammates on a friendly-fire
#: server, and every kill at all on Enemy Territory.
KILL_EVENTS = (
    EventType.CLIENT_KILL,
    EventType.CLIENT_KILL_TEAM,
    EventType.CLIENT_GIB,
    EventType.CLIENT_GIB_TEAM,
)

DEFAULTS: dict[str, object] = {
    # Level for all three commands. 1 (`user`) is the classic's, hardcoded there.
    "min_level": 1,
    # How long an unanswered challenge stands, in seconds. 0 means forever, which is what the classic
    # did — a proposal nobody answered sat there until somebody left the server.
    "proposal_timeout": 300,
    # How many duels one player may have running. The classic had no limit; a dozen duels means a
    # dozen private messages after every kill, which is a way to make the bot unusable.
    "max_duels": 3,
}

MESSAGES = {
    "duel_proposed": "you have challenged {name} — they have to accept it",
    "duel_challenged": "{name} challenges you to a duel: type !duel {handle} to accept",
    "duel_accepted_you": "you accepted {name}'s challenge",
    "duel_accepted_them": "{name} accepted your challenge",
    "duel_score": "duel: you {score} - {opponent_score} {name}",
    "duel_cancelled": "the duel with {name} is off",
    "duel_none": "you have no duel running",
    "duel_none_with": "you have no duel with {name}",
    "duel_which": "you have {count} duels running — say which, as !{command} <player>",
    "duel_yourself": "you cannot duel yourself",
    "duel_already": "you are already duelling {name}",
    "duel_too_many": "you already have {count} duels running",
    "duel_usage": "!duel <player> — challenge somebody, or accept their challenge",
    "duel_expired": "your challenge to {name} expired",
}


@dataclass
class Duel:
    """Two players and the score between them.

    ``accepted`` is the whole handshake: an unaccepted duel is a challenge, counts nothing and can
    expire. Scores are held here rather than keyed by client object — the classic used the `Client`
    instances as dictionary keys, which is what kept a departed player's object alive.
    """

    challenger: Client
    opponent: Client
    accepted: bool = False
    proposed_at: float = 0.0
    scores: dict[str, int] = field(default_factory=dict)

    def involves(self, client: Client) -> bool:
        return client is self.challenger or client is self.opponent

    def other(self, client: Client) -> Client:
        return self.opponent if client is self.challenger else self.challenger

    def side(self, client: Client) -> str:
        """A stable key for one side of this duel, whatever the player renames themselves to."""
        return "challenger" if client is self.challenger else "opponent"

    def score_of(self, client: Client) -> int:
        return self.scores.get(self.side(client), 0)

    def credit(self, client: Client) -> None:
        key = self.side(client)
        self.scores[key] = self.scores.get(key, 0) + 1

    def reset(self) -> None:
        self.scores = {}


class DuelPlugin(Plugin):
    """Challenges, and the score between two players who accepted one."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: Every duel and challenge on the server. One object per duel, held here rather than twice
        #: over in the two players' own storage.
        self.duels: list[Duel] = []

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        level = as_level(self.settings.get("min_level"), 1)
        for name in ("duel", "duelcancel", "duelreset"):
            registered = self.console.command_registry.get(name)
            if registered is not None:
                registered.min_level = level

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        for event_type in KILL_EVENTS:
            self.subscribe(event_type, self.on_kill)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.subscribe(EventType.GAME_ROUND_END, self.on_round_end)
        self.schedule(self._expire_proposals, second="*/30", name="DuelPlugin.expire")
        self.on_load_config()

    def on_disable(self) -> None:
        self.duels.clear()

    # -- the list ------------------------------------------------------------

    def duels_of(self, client: Client) -> list[Duel]:
        return [duel for duel in self.duels if duel.involves(client)]

    def duel_between(self, one: Client, other: Client) -> Duel | None:
        for duel in self.duels:
            if duel.involves(one) and duel.involves(other):
                return duel
        return None

    # -- handlers ------------------------------------------------------------

    def on_kill(self, event: Event) -> None:
        """A kill counts if it was between two people duelling each other."""
        killer, victim = event.client, event.target
        if killer is None or victim is None or killer is victim:
            return
        duel = self.duel_between(killer, victim)
        if duel is None or not duel.accepted:
            return
        duel.credit(killer)
        self.tell_score(duel, killer)
        self.tell_score(duel, victim)

    def on_disconnect(self, event: Event) -> None:
        """Both players are told the final score, and the duel goes."""
        client = event.client
        if client is None:
            return
        for duel in self.duels_of(client):
            other = duel.other(client)
            if duel.accepted:
                self.tell_score(duel, other)
            self.console.tell(other, self.message("duel_cancelled", name=client.name))
            self.duels.remove(duel)

    def on_round_end(self, event: Event) -> None:
        for duel in self.duels:
            if not duel.accepted:
                continue
            self.tell_score(duel, duel.challenger)
            self.tell_score(duel, duel.opponent)

    def _expire_proposals(self) -> None:
        """Drop challenges nobody answered. 0 keeps the classic's forever."""
        timeout = as_int(self.settings.get("proposal_timeout"), 300)
        if timeout <= 0:
            return
        now = self.console.clock.now()
        for duel in list(self.duels):
            if duel.accepted or now - duel.proposed_at < timeout:
                continue
            self.duels.remove(duel)
            self.console.tell(
                duel.challenger, self.message("duel_expired", name=duel.opponent.name)
            )

    def tell_score(self, duel: Duel, client: Client) -> None:
        other = duel.other(client)
        self.console.tell(
            client,
            self.message(
                "duel_score",
                score=duel.score_of(client),
                opponent_score=duel.score_of(other),
                name=other.name,
            ),
        )

    # -- commands ------------------------------------------------------------

    @command("duel", level=1)
    def cmd_duel(self, ctx: CommandContext) -> None:
        """duel <player> - challenge somebody, or accept their challenge"""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("duel_usage"))
            return
        opponent = self.resolve_client(ctx, handle)
        if opponent is None:
            return
        if opponent is ctx.client:
            ctx.reply(self.message("duel_yourself"))
            return

        existing = self.duel_between(ctx.client, opponent)
        if existing is not None:
            self._answer_existing(ctx, existing, opponent)
            return
        running = len([d for d in self.duels_of(ctx.client)])
        if running >= as_int(self.settings.get("max_duels"), 3):
            ctx.reply(self.message("duel_too_many", count=running))
            return
        self.duels.append(
            Duel(
                challenger=ctx.client,
                opponent=opponent,
                proposed_at=self.console.clock.now(),
            )
        )
        ctx.reply(self.message("duel_proposed", name=opponent.name))
        # The handle is offered as well as the name, because the reply has to be typeable: a name with
        # colour codes or spaces in it is not, and the slot always is.
        self.console.tell(
            opponent,
            self.message("duel_challenged", name=ctx.client.name, handle=ctx.client.cid or ""),
        )

    def _answer_existing(self, ctx: CommandContext, duel: Duel, opponent: Client) -> None:
        if duel.accepted:
            ctx.reply(self.message("duel_already", name=opponent.name))
            return
        if duel.challenger is ctx.client:
            # Their own challenge, still unanswered: say so and nudge the other player again.
            ctx.reply(self.message("duel_proposed", name=opponent.name))
            self.console.tell(
                opponent,
                self.message("duel_challenged", name=ctx.client.name, handle=ctx.client.cid or ""),
            )
            return
        duel.accepted = True
        duel.reset()
        ctx.reply(self.message("duel_accepted_you", name=opponent.name))
        self.console.tell(opponent, self.message("duel_accepted_them", name=ctx.client.name))
        self.tell_score(duel, ctx.client)
        self.tell_score(duel, opponent)

    @command("duelcancel", level=1)
    def cmd_duelcancel(self, ctx: CommandContext) -> None:
        """duelcancel [player] - call off a duel"""
        duel = self._pick(ctx, "duelcancel")
        if duel is None:
            return
        other = duel.other(ctx.client)
        self.duels.remove(duel)
        ctx.reply(self.message("duel_cancelled", name=other.name))
        self.console.tell(other, self.message("duel_cancelled", name=ctx.client.name))

    @command("duelreset", level=1)
    def cmd_duelreset(self, ctx: CommandContext) -> None:
        """duelreset [player] - set a duel's score back to nothing"""
        duel = self._pick(ctx, "duelreset")
        if duel is None:
            return
        duel.reset()
        self.tell_score(duel, duel.challenger)
        self.tell_score(duel, duel.opponent)

    def _pick(self, ctx: CommandContext, command_name: str) -> Duel | None:
        """The duel a command means: the only one, or the one with the player named."""
        mine = self.duels_of(ctx.client)
        if not mine:
            ctx.reply(self.message("duel_none"))
            return None
        handle = ctx.args.strip()
        if not handle:
            if len(mine) == 1:
                return mine[0]
            ctx.reply(self.message("duel_which", count=len(mine), command=command_name))
            return None
        opponent = self.resolve_client(ctx, handle)
        if opponent is None:
            return None
        duel = self.duel_between(ctx.client, opponent)
        if duel is None:
            ctx.reply(self.message("duel_none_with", name=opponent.name))
            return None
        return duel


__all__ = ["DEFAULTS", "KILL_EVENTS", "MESSAGES", "Duel", "DuelPlugin"]
