"""Spam control — warns players who flood chat.

A port of the classic `spamcontrol` plugin, whose good idea was to score spam rather than count it:
saying the same thing twice is worse than saying two different things, and a coloured repeat is
worse still. Points decay over time, so an ordinary talkative player never trips it while someone
pasting the same advert every second does, quickly.

Kept from the original: the point values, the decay, and `!spamins` for checking somebody's score.
Changed: the score decays *when read* from a timestamp rather than being swept by a background
thread, which means one less thread and a score that is correct the moment you ask for it.
"""

from __future__ import annotations

import logging
import re

from b3.core.commands import CommandContext, command
from b3.core.events import Event, EventType
from b3.core.console import Console
from b3.core.plugin import Plugin
from b3.domain.client import Client
from b3.core.util import as_float, as_int

log = logging.getLogger(__name__)

#: Chat that starts with a colour code — the classic tell of a pasted advert.
COLOUR_PREFIX = re.compile(r"^\^[0-9]")

DEFAULTS: dict[str, object] = {
    "max_spamins": 10,  # points at which the player is warned
    "falloff_rate": 6.5,  # seconds per point of decay
    "mod_level": 20,  # at or above this level, chat is never scored
}

MESSAGES = {
    "spam_warning": "do not spam, you have been warned",
    "spamins": "{name} has {points} spam point(s)",
}


class SpamcontrolPlugin(Plugin):
    """Scores chat and warns whoever floods it."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        for event_type in (
            EventType.CLIENT_SAY,
            EventType.CLIENT_TEAM_SAY,
            EventType.CLIENT_PRIVATE_SAY,
            # Urban Terror's radio is a chat channel and is flooded like one.
            EventType.CLIENT_RADIO,
        ):
            self.subscribe(event_type, self.on_chat)

    # -- scoring ------------------------------------------------------------

    def points_for(self, client: Client, text: str) -> int:
        """The classic bot's weighting: repetition is the thing worth punishing."""
        last = client.get_var(self, "last_message", "")
        coloured = bool(COLOUR_PREFIX.match(text))
        repeated = text == last

        if repeated and coloured:
            points = 5
        elif repeated:
            points = 3
        elif coloured or text.startswith("QUICKMESSAGE_"):
            points = 2
        else:
            points = 1
        if text[:1] == "!":
            points += 1  # command spam counts double-ish, as it did classically
        return points

    def score(self, client: Client) -> float:
        """The player's spam score right now, with decay applied.

        Decaying on read rather than on a timer means no background sweep, and no window where the
        stored number is stale.
        """
        points = float(client.get_var(self, "spam_points", 0.0))
        if points <= 0:
            return 0.0
        falloff = as_float(self.settings.get("falloff_rate"), 6.5) or 6.5
        elapsed = self.console.clock.now() - float(client.get_var(self, "spam_time", 0.0))
        return max(0.0, points - elapsed / falloff)

    def add_points(self, client: Client, points: int) -> float:
        total = self.score(client) + points
        client.set_var(self, "spam_points", total)
        client.set_var(self, "spam_time", self.console.clock.now())
        return total

    # -- handlers -----------------------------------------------------------

    def scored_text(self, event: Event) -> str:
        """What to measure. Chat is a line of text; a radio call is a dict of fields.

        A radio call's location changes as the player moves, so it is left out: including it would
        make the same message sent twice from different places look like two different messages,
        and repetition is what this plugin scores most heavily.
        """
        data = event.data
        if isinstance(data, dict):
            group, msg = data.get("msg_group"), data.get("msg_id")
            return f"radio:{group}:{msg}:{data.get('text', '')}"
        return str(data)

    def on_chat(self, event: Event) -> None:
        client = event.client
        if client is None or not event.data:
            return
        if client.max_level() >= as_int(self.settings.get("mod_level"), 20):
            return  # admins are trusted to know when to stop

        text = self.scored_text(event)
        total = self.add_points(client, self.points_for(client, text))
        client.set_var(self, "last_message", text)

        if total >= as_float(self.settings.get("max_spamins"), 10):
            client.set_var(self, "spam_points", 0.0)  # warned; start the count again
            self.console.warn(client, reason=self.message("spam_warning"), admin=None)

    # -- commands -----------------------------------------------------------

    @command(level=20)
    def cmd_spamins(self, ctx: CommandContext) -> None:
        """spamins [player] - show how close somebody is to a spam warning"""
        target = ctx.client
        handle = ctx.args.strip()
        if handle:
            found = self.resolve_client(ctx, handle)
            if found is None:
                return
            target = found
        ctx.reply(self.message("spamins", name=target.name, points=round(self.score(target), 1)))
