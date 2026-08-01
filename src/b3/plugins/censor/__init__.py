"""Censor — penalises bad language in chat and in player names.

A port of the classic `censor` plugin. Its shape is worth keeping: a list of bad words, each either
a plain word or a regular expression, each able to carry its own penalty, with a default penalty for
anything that does not. Admins above a level are not checked, and very short words are skipped so
that "ass" does not fire on "class" — the classic bot's own `ignore_length`.

Two things are deliberately different. Patterns are compiled **once at startup** and a bad one is
reported and skipped rather than taking the plugin down mid-sentence (the XML version happily
accepted a broken regex and then raised on every chat line). And a plain `word` is matched on word
boundaries **in chat** rather than as a substring, which is what stops the Scunthorpe problem the
original was famous for — while bad *names* are still matched anywhere, because "xXcheaterXx" has
no word boundaries to find.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from b3.core.events import Event, EventType
from b3.core.console import Console
from b3.core.plugin import Plugin
from b3.core.util import as_int, parse_duration
from b3.domain.client import Client

log = logging.getLogger(__name__)

DEFAULTS: dict[str, object] = {
    "max_level": 40,  # above this level, nobody is checked
    "ignore_length": 3,  # words this short or shorter are not checked at all
}

#: What a badword entry may ask for when it matches.
PENALTIES = ("warning", "kick", "tempban", "ban")

MESSAGES = {
    "censor_chat": "watch your language",
    "censor_name": "your name is not acceptable here",
}


@dataclass(frozen=True, slots=True)
class BadWord:
    name: str
    pattern: "re.Pattern[str]"
    penalty: str
    reason: str
    minutes: int


class CensorPlugin(Plugin):
    """Watches chat and names for configured bad words."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.badwords: list[BadWord] = []
        self.badnames: list[BadWord] = []

    # -- config -------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        default = config.get("default_penalty") or {}
        self.badwords = self._compile(config.get("badwords") or [], default, "censor_chat")
        # Names are matched as substrings: word boundaries are right for a sentence, but nobody
        # writes a nickname with spaces, and "xXcheaterXx" is exactly what a bad name looks like.
        self.badnames = self._compile(
            config.get("badnames") or [], default, "censor_name", boundaries=False
        )
        log.info("censor: %d bad word(s), %d bad name(s)", len(self.badwords), len(self.badnames))

    def _compile(
        self,
        entries: list[dict[str, object]],
        default: dict[str, object],
        reason_key: str,
        *,
        boundaries: bool = True,
    ) -> list[BadWord]:
        """Turn config entries into compiled matchers, skipping (and naming) any that are broken."""
        compiled: list[BadWord] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                log.warning("censor: entry %d is not a mapping; ignored", index)
                continue
            name = str(entry.get("name") or entry.get("word") or entry.get("regexp") or index)
            try:
                pattern = self._pattern(entry, boundaries)
            except re.error as exc:
                log.warning("censor: %r is not a valid regular expression (%s); ignored", name, exc)
                continue
            if pattern is None:
                log.warning("censor: %r has neither `word` nor `regexp`; ignored", name)
                continue
            penalty = str(entry.get("penalty") or default.get("penalty") or "warning").lower()
            if penalty not in PENALTIES:
                log.warning(
                    "censor: %r asks for unknown penalty %r; warning instead", name, penalty
                )
                penalty = "warning"
            raw_duration = entry.get("duration") or default.get("duration") or 0
            try:
                minutes = parse_duration(str(raw_duration)) if raw_duration else 0
            except ValueError:
                log.warning(
                    "censor: %r has an invalid duration %r; ignoring it", name, raw_duration
                )
                minutes = 0
            compiled.append(
                BadWord(
                    name=name,
                    pattern=pattern,
                    penalty=penalty,
                    reason=str(entry.get("reason") or default.get("reason") or "")
                    or None
                    or self.message(reason_key),
                    minutes=minutes,
                )
            )
        return compiled

    @staticmethod
    def _pattern(entry: dict[str, object], boundaries: bool = True) -> "re.Pattern[str] | None":
        if entry.get("regexp"):
            return re.compile(str(entry["regexp"]), re.IGNORECASE)
        if entry.get("word"):
            # Word boundaries in chat, so "ass" does not fire on "class" — the classic bot's
            # oldest bug. Names get no boundaries; see on_load_config.
            word = re.escape(str(entry["word"]))
            return re.compile(rf"\b{word}\b" if boundaries else word, re.IGNORECASE)
        return None

    # -- lifecycle ----------------------------------------------------------

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_SAY, self.on_chat)
        self.subscribe(EventType.CLIENT_TEAM_SAY, self.on_chat)
        self.subscribe(EventType.CLIENT_NAME_CHANGE, self.on_name)
        self.subscribe(EventType.CLIENT_AUTH, self.on_name)
        # Not CLIENT_RADIO, though spamcontrol does subscribe to it: a radio call's text and
        # location are both chosen by the engine from fixed menus, so a player cannot put anything
        # in one to censor.

    # -- handlers -----------------------------------------------------------

    def _exempt(self, client: Client | None) -> bool:
        return client is None or client.max_level() > as_int(self.settings.get("max_level"), 40)

    def find_match(self, text: str, words: list[BadWord]) -> BadWord | None:
        """The first bad word this text trips, or None. Short text is skipped entirely."""
        if len(text.strip()) <= as_int(self.settings.get("ignore_length"), 3):
            return None
        return next((bad for bad in words if bad.pattern.search(text)), None)

    def on_chat(self, event: Event) -> None:
        client = event.client
        if client is None or self._exempt(client) or not event.data:
            return
        match = self.find_match(str(event.data), self.badwords)
        if match is not None:
            self._punish(client, match)

    def on_name(self, event: Event) -> None:
        client = event.client
        if client is None or self._exempt(client) or not client.name:
            return
        match = self.find_match(client.name, self.badnames)
        if match is not None:
            self._punish(client, match)

    def _punish(self, client: Client, bad: BadWord) -> None:
        log.info("censor: %s tripped %r -> %s", client.name, bad.name, bad.penalty)
        if bad.penalty == "warning":
            self.console.warn(client, reason=bad.reason, minutes=bad.minutes)
        elif bad.penalty == "kick":
            self.console.kick(client, reason=bad.reason)
        elif bad.penalty == "tempban":
            self.console.tempban(client, max(1, bad.minutes), reason=bad.reason)
        else:
            self.console.ban(client, reason=bad.reason)
