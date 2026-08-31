"""Spam control — warns players who flood chat.

A port of the classic `spamcontrol` plugin, whose good idea was to score spam rather than count it:
saying the same thing twice is worse than saying two different things, and a coloured repeat is
worse still. Points decay over time, so an ordinary talkative player never trips it while someone
pasting the same advert every second does, quickly.

Kept from the original: the point values, the decay, and `!spamins` for checking somebody's score.
Changed: the score decays *when read* from a timestamp rather than being swept by a background
thread, which means one less thread and a score that is correct the moment you ask for it.

**The radio is scored separately**, in a `radio:` section. That was a feature of its own in the classic
— `radio_spam_protection`, inside `poweradminurt`, and only inside its Urban Terror 4.2 subclass, so a
4.1 server never had it — but it is this plugin's job, in the same way `censorurt` turned out to be a
`mute:` section on `censor`. Two things about the radio make it not chat:

* **It is scored on the gap, not on the words.** A radio message is chosen from a fixed menu, so
  repeating one is ordinary and the content says almost nothing. Scoring it as chat — which this
  plugin did before the section existed — misses somebody cycling through five different calls a
  second and punishes two identical ones a minute apart.
* **It is answered with a mute, not a warning.** The radio is a menu of buttons: telling somebody to
  stop does not stop them, and they are not reading the chat a warning arrives in. Needs the engine's
  `mute` verb (`GameProfile.player_verbs`); where there is none, the section is refused at startup
  with a reason and radio spam is warned about like chat.
"""

from __future__ import annotations

import logging
import re

from b3.core.commands import CommandContext, command
from b3.core.events import Event, EventType
from b3.core.console import Console
from b3.core.plugin import Plugin
from b3.domain.client import Client
from b3.core.util import as_float, as_level

log = logging.getLogger(__name__)

#: Chat that starts with a colour code — the classic tell of a pasted advert.
COLOUR_PREFIX = re.compile(r"^\^[0-9]")

DEFAULTS: dict[str, object] = {
    "max_spamins": 10,  # points at which the player is warned
    "falloff_rate": 6.5,  # seconds per point of decay
    "mod_level": 20,  # at or above this level, chat is never scored
}

#: The radio's own scoring. Separate numbers because it is a different thing being measured: the
#: classic's `radio_spam_protection` used a decay more than three times faster than its chat one.
RADIO_DEFAULTS: dict[str, object] = {
    # Seconds per point of decay. Faster than chat's: five radio calls in five seconds is spam,
    # where five typed lines in five seconds is somebody with something to say.
    "falloff_rate": 2.0,
    # Score at which they are silenced.
    "max_spamins": 10,
    # Seconds of silence. 0 warns instead, as chat does. Seconds rather than `censor`'s minutes
    # because the radio is answered in seconds — the classic's own default here was five.
    "mute_seconds": 60,
}

MESSAGES = {
    "spam_warning": "do not spam, you have been warned",
    "spamins": "{name} has {points} spam point(s)",
    "radio_muted": "your radio is off for {seconds} seconds - stop spamming it",
}


class SpamcontrolPlugin(Plugin):
    """Scores chat and warns whoever floods it."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.radio = dict(RADIO_DEFAULTS)

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self._load_radio()

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        for event_type in (
            EventType.CLIENT_SAY,
            EventType.CLIENT_TEAM_SAY,
            EventType.CLIENT_PRIVATE_SAY,
        ):
            self.subscribe(event_type, self.on_chat)
        # Urban Terror's radio is flooded like a chat channel and scored unlike one — see `radio:`.
        self.subscribe(EventType.CLIENT_RADIO, self.on_radio)

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

    def score(self, client: Client, falloff: float | None = None) -> float:
        """The player's spam score right now, with decay applied.

        Decaying on read rather than on a timer means no background sweep, and no window where the
        stored number is stale.
        """
        points = float(client.get_var(self, "spam_points", 0.0))
        if points <= 0:
            return 0.0
        if falloff is None:
            falloff = as_float(self.settings.get("falloff_rate"), 6.5) or 6.5
        elapsed = self.console.clock.now() - float(client.get_var(self, "spam_time", 0.0))
        return max(0.0, points - elapsed / falloff)

    def add_points(self, client: Client, points: int, falloff: float | None = None) -> float:
        total = self.score(client, falloff) + points
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
        if client.max_level() >= as_level(self.settings.get("mod_level"), 20):
            return  # admins are trusted to know when to stop

        text = self.scored_text(event)
        total = self.add_points(client, self.points_for(client, text))
        client.set_var(self, "last_message", text)

        if total >= as_float(self.settings.get("max_spamins"), 10):
            client.set_var(self, "spam_points", 0.0)  # warned; start the count again
            self.console.warn(client, reason=self.message("spam_warning"), admin=None)

    # -- the radio ----------------------------------------------------------

    def _load_radio(self) -> None:
        """Read the `radio:` section, and refuse a mute the engine cannot carry out."""
        config = self.config if isinstance(self.config, dict) else {}
        self.radio = {**RADIO_DEFAULTS, **(config.get("radio") or {})}
        if as_float(self.radio.get("mute_seconds"), 0.0) > 0 and not self.console.supports_verb(
            "mute"
        ):
            log.warning(
                "spamcontrol: radio.mute_seconds is set but this game has no `mute` verb, so radio "
                "spam is warned about like chat instead"
            )
            self.radio["mute_seconds"] = 0

    def radio_points(self, client: Client, text: str) -> int:
        """What a radio call is worth, which is not what a line of chat is worth.

        A radio message's words are chosen from a fixed menu, so repeating one is ordinary and
        content says almost nothing — what marks out somebody abusing the radio is the *rate*. The
        classic scored it on the gap since the last call, and that is right; scoring it as chat
        (which this plugin did on its own) misses a player cycling through five different calls a
        second and punishes two identical ones a minute apart.
        """
        last = client.get_var(self, "radio_time")
        if not isinstance(last, int | float):
            return 0
        gap = self.console.clock.now() - float(last)
        points = 0
        if gap < 20:
            points += 1
        if gap < 2:
            points += 1
            if text == client.get_var(self, "last_message", ""):
                points += 3
        if gap < 1:
            points += 3
        return points

    def on_radio(self, event: Event) -> None:
        """Score a radio call, and silence whoever will not stop.

        Silence, not warn: the radio is a menu of buttons, so a warning telling somebody to stop
        does not stop them — and it is the one channel a warning cannot reach, because they are not
        reading chat, they are pressing keys. This is the whole of the classic's separate
        `radio_spam_protection` feature, which lived in `poweradminurt` and existed only there.
        """
        client = event.client
        if client is None or not event.data:
            return
        if client.max_level() >= as_level(self.settings.get("mod_level"), 20):
            return
        if self.console.muted_until(client) > self.console.clock.now():
            # Already silenced. The classic kept a second deadline of its own for this, computed
            # from the mute length rather than read from it, so the two could drift apart.
            return
        text = self.scored_text(event)
        falloff = as_float(self.radio.get("falloff_rate"), 2.0) or 2.0
        total = self.add_points(client, self.radio_points(client, text), falloff=falloff)
        client.set_var(self, "last_message", text)
        client.set_var(self, "radio_time", self.console.clock.now())
        if total < as_float(self.radio.get("max_spamins"), 10):
            return
        seconds = as_float(self.radio.get("mute_seconds"), 0.0)
        # Halved rather than cleared, as the classic had it: somebody who has just been silenced for
        # this is not starting from nothing when they come back.
        client.set_var(self, "spam_points", total / 2.0)
        if seconds > 0 and self.console.mute(client, seconds / 60.0):
            self.console.tell(client, self.message("radio_muted", seconds=int(seconds)))
            return
        client.set_var(self, "spam_points", 0.0)
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
