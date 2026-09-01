"""Relays what happens on the server to a Discord channel, through a webhook.

**Outbound only, and on purpose.** Every Discord integration in this corner of the world is — the
classic bot's own `b3-plugin-discord`, IW4MAdmin's YADB and IW4ToDiscord — and the one tool that does
both directions (DiscordSRV, for Minecraft) shows what the other half costs: a bot token, a gateway
websocket that must stay up, reconnect and heartbeat, and a dependency. This bot is one asyncio loop
reading one game server, and a permanent second socket is not a small addition to it.

The deeper reason is not the transport. **A Discord message has no `Client`**, so before `!ban` could
be typed there, somebody has to decide how a Discord account maps to a b3 identity and a level —
otherwise the channel's permissions *are* the admin system, and a stolen Discord account is a stolen
server. And the want behind it, "administer the server without being in the game", is what the web
API and dashboard are for. Building it twice, once through Discord's gateway, is the expensive way.

**On privacy, which is why this exists while `translator` was dropped.** That plugin sent every chat
line to a third party the operator had no relationship with, through an undocumented endpoint, by
default. This is the opposite on all three counts: the webhook is the operator's own, it points at
their own Discord, and nothing is sent until they paste a URL into their config. Relaying your own
server to your own community is the feature. Chat relaying is still **off by default**, because a
player talking in a game they are playing has not agreed to being quoted somewhere else.

**Batching is not an optimisation.** Discord's webhook endpoint sends one message per request — there
is no way to post several at once — and a busy server's chat at one request per line would hit the
rate limit within a minute. Lines are queued and flushed together, up to Discord's 2000-character
limit, and the limit is read from what Discord answers rather than guessed at: a 429 carries
`retry_after`, and this plugin then stays quiet for exactly that long.

**Nothing here can cost the bot anything.** The POST runs on a worker thread, every failure is
swallowed and logged, and the queue is bounded — a Discord outage must never stall the event loop or
stop a ban being applied. This is the bot's first outbound integration, so the shape of that is
deliberate: whatever the metrics endpoint does about timeouts and retries should look like this.

**Images.** Nothing is bundled and nothing is fetched. A map picture would mean hosting a screenshot
of every map of 38 titles, and a player avatar would mean a Steam API key and publishing identity to
a third party. Instead `map_image_url` is a template the operator points at their own hosting, and
Discord renders a bare image URL by itself — so an operator who has pictures gets them, and one who
does not is not carrying a broken link.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Discord's own limits, from its webhook documentation: `content` is capped at 2000 characters, and
#: one request carries one message. Together those are the whole argument for batching.
MAX_CONTENT = 2000

#: Room left for the "and N more" line a truncated batch ends with.
CONTENT_HEADROOM = 120

#: Seconds. Short, because this is a notification rather than a transaction, and long enough that a
#: server which has gone away does not hold a worker thread for a minute.
TIMEOUT = 10.0

#: How long to stay quiet after a rate limit that did not say how long to wait.
DEFAULT_BACKOFF = 30.0

DEFAULTS: dict[str, object] = {
    # The webhook, from Discord: Channel Settings -> Integrations -> Webhooks -> Copy URL. Empty
    # means this plugin does nothing at all, and says so once rather than per event.
    "webhook": "",
    # What the messages are posted as. Empty keeps whatever the webhook itself is named.
    "username": "",
    "avatar_url": "",
    # Which events to relay. The moderation ones are on: they are what an admin channel is for.
    # Chat is off, deliberately — see the note about consent in this module's docstring.
    "bans": True,
    "kicks": True,
    "warnings": False,
    "joins": False,
    "leaves": False,
    "chat": False,
    "map_changes": True,
    # Seconds between flushes. Everything since the last one goes in a single message.
    "flush_seconds": 10,
    # How many lines to hold while Discord is unreachable. Past this the oldest go, because the
    # alternative is a queue that grows for as long as the outage lasts.
    "max_queue": 200,
    # A picture for the map, if you host one: "https://example.com/maps/{map}.jpg". Discord renders
    # a bare image URL on its own, so no embed is needed. Empty means no image, which is the default
    # because nothing here ships pictures of maps.
    "map_image_url": "",
}

#: The lines themselves. **Not called MESSAGES on purpose**: that name is the in-game message
#: contract, and every one of those has to survive a latin-1 game console. These go to Discord, which
#: is UTF-8 and renders markdown, so they may hold anything.
TEMPLATES = {
    "ban": "\N{HAMMER} **{name}** was banned by {admin} - {reason}",
    "tempban": "\N{HOURGLASS} **{name}** was banned for {duration} by {admin} - {reason}",
    "unban": "\N{DOVE OF PEACE} **{name}** was unbanned by {admin}",
    "kick": "\N{WOMANS BOOTS} **{name}** was kicked by {admin} - {reason}",
    "warn": "\N{WARNING SIGN} **{name}** was warned by {admin} - {reason}",
    "join": "\N{BLACK RIGHTWARDS ARROW} {name} joined",
    "leave": "\N{LEFTWARDS BLACK ARROW} {name} left",
    "chat": "{name}: {text}",
    "map": "\N{WORLD MAP} map is now **{map}**",
}

#: Colour codes a Call of Duty or Quake 3 name is full of. They mean nothing in Discord and would be
#: read as literal text, so they go.
COLOUR_CODES = tuple(f"^{digit}" for digit in "0123456789")

#: Markdown a player could put in their own name to reformat the rest of a channel's line.
MARKDOWN = ("*", "_", "`", "~", "|")


class DiscordPlugin(Plugin):
    """Posts what happens on the server to one Discord webhook, batched."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.templates = dict(TEMPLATES)
        #: Lines waiting for the next flush.
        self.queue: list[str] = []
        #: How many were dropped because the queue was full. Said out loud when it recovers, because
        #: a relay that quietly skipped an hour of bans would be worse than one that failed.
        self.dropped = 0
        #: Time before which nothing is sent, set by a rate limit Discord asked us to respect.
        self.quiet_until = 0.0

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.templates = {**TEMPLATES, **(config.get("templates") or {})}

    def on_startup(self) -> None:
        if not self.webhook():
            log.warning(
                "discord: no webhook is configured, so nothing will be relayed. Paste one from "
                "Channel Settings -> Integrations -> Webhooks into `webhook`"
            )
            return

        wanted = {
            "bans": (EventType.CLIENT_BAN, EventType.CLIENT_BAN_TEMP, EventType.CLIENT_UNBAN),
            "kicks": (EventType.CLIENT_KICK,),
            "warnings": (EventType.CLIENT_WARN,),
            "joins": (EventType.CLIENT_JOIN,),
            "leaves": (EventType.CLIENT_DISCONNECT,),
            "chat": (EventType.CLIENT_SAY, EventType.CLIENT_TEAM_SAY),
            "map_changes": (EventType.GAME_MAP_CHANGE,),
        }
        for setting, types in wanted.items():
            if not self.settings.get(setting):
                continue
            for event_type in types:
                self.subscribe(event_type, self.on_event)

        interval = max(1, as_int(self.settings.get("flush_seconds"), 10))
        # A crontab, as every scheduled job here is, so an interval under a minute is seconds.
        if interval < 60:
            self.schedule(self.flush, second=f"*/{interval}", name="DiscordPlugin.flush")
        else:
            self.schedule(
                self.flush, minute=f"*/{max(1, interval // 60)}", name="DiscordPlugin.flush"
            )

    def webhook(self) -> str:
        return str(self.settings.get("webhook") or "").strip()

    # -- what happened -------------------------------------------------------

    def on_event(self, event: Event) -> None:
        """Turn an event into a line, or ignore it. Never raises: this is a notifier."""
        try:
            line = self.render(event)
        except Exception as exc:  # noqa: BLE001 - a bad template must not cost the event
            log.warning("discord: %s could not be rendered (%s)", event.type.name, exc)
            return
        if line:
            self.enqueue(line)

    #: Which setting governs which event. Checked in `render` as well as at subscription time: a
    #: switched-off relay that still formatted the line would be one refactor away from sending it.
    GOVERNED_BY = {
        EventType.CLIENT_BAN: "bans",
        EventType.CLIENT_BAN_TEMP: "bans",
        EventType.CLIENT_UNBAN: "bans",
        EventType.CLIENT_KICK: "kicks",
        EventType.CLIENT_WARN: "warnings",
        EventType.CLIENT_JOIN: "joins",
        EventType.CLIENT_DISCONNECT: "leaves",
        EventType.CLIENT_SAY: "chat",
        EventType.CLIENT_TEAM_SAY: "chat",
        EventType.GAME_MAP_CHANGE: "map_changes",
    }

    def render(self, event: Event) -> str:
        """The line for one event, or "" for an event this does not relay."""
        setting = self.GOVERNED_BY.get(event.type)
        if setting is None or not self.settings.get(setting):
            return ""
        name = self.plain(event.client.name if event.client else "")
        admin = self.plain(self.admin_name(event))
        reason = self.plain(str(event.data or "")) or "no reason given"

        if event.type is EventType.CLIENT_BAN:
            return self.templates["ban"].format(name=name, admin=admin, reason=reason)
        if event.type is EventType.CLIENT_BAN_TEMP:
            return self.templates["tempban"].format(
                name=name, admin=admin, reason=reason, duration=self.duration(event)
            )
        if event.type is EventType.CLIENT_UNBAN:
            return self.templates["unban"].format(name=name, admin=admin)
        if event.type is EventType.CLIENT_KICK:
            return self.templates["kick"].format(name=name, admin=admin, reason=reason)
        if event.type is EventType.CLIENT_WARN:
            return self.templates["warn"].format(name=name, admin=admin, reason=reason)
        if event.type is EventType.CLIENT_JOIN:
            return self.templates["join"].format(name=name)
        if event.type is EventType.CLIENT_DISCONNECT:
            return self.templates["leave"].format(name=name)
        if event.type in (EventType.CLIENT_SAY, EventType.CLIENT_TEAM_SAY):
            return self.chat_line(name, str(event.data or ""))
        if event.type is EventType.GAME_MAP_CHANGE:
            return self.map_line(str(event.data or ""))
        return ""

    def chat_line(self, name: str, said: str) -> str:
        """A player's chat, unless it is a command.

        `!login <password>` is typed in the same place as chat and relaying it to a channel would be
        worse than relaying nothing at all.
        """
        text = self.plain(said)
        if not text or text.startswith(("!", "@", "&")):
            return ""
        return self.templates["chat"].format(name=name, text=text)

    def map_line(self, map_name: str) -> str:
        # `clean`, not `plain`: a map name comes from the server rather than from a player, and
        # escaping it would put backslashes through `mp_crash` — and through the URL below, which
        # would then not be a URL any more.
        map_name = self.clean(map_name)
        line = self.templates["map"].format(map=map_name)
        picture = str(self.settings.get("map_image_url") or "")
        if picture and map_name:
            # A bare URL, which Discord renders as a picture by itself: no embed to build, and
            # nothing to go wrong for the operator who has no pictures.
            line = f"{line}\n{picture.format(map=map_name)}"
        return line

    def admin_name(self, event: Event) -> str:
        """Who did it: the admin is the event's `target`, and is often nobody at all."""
        who: Client | None = event.target
        return who.name if who is not None else "b3"

    def duration(self, event: Event) -> str:
        minutes = event.extra.get("duration") if event.extra else None
        return "a while" if minutes is None else str(minutes)

    @staticmethod
    def clean(text: str) -> str:
        """Text from the server: colour codes gone, and unable to mention anybody.

        A zero-width space rather than a backslash, because a backslash does not reliably stop
        Discord parsing `@everyone` while an invisible character between the `@` and the word does —
        and the word still reads normally to a person.
        """
        for code in COLOUR_CODES:
            text = text.replace(code, "")
        return text.replace("@", "@​").strip()

    @classmethod
    def plain(cls, text: str) -> str:
        """Text a **player** chose: cleaned, and unable to reformat the rest of the channel.

        A player called `**hi**` should appear as they are named rather than emboldening everything
        after them. Their name is the one part of these lines somebody else writes, which is why it
        gets the escaping and a map name does not.
        """
        text = cls.clean(text)
        for char in MARKDOWN:
            text = text.replace(char, "\\" + char)
        return text

    # -- sending -------------------------------------------------------------

    def enqueue(self, line: str) -> None:
        limit = max(1, as_int(self.settings.get("max_queue"), 200))
        self.queue.append(line)
        while len(self.queue) > limit:
            self.queue.pop(0)
            self.dropped += 1

    async def flush(self) -> None:
        """Post everything queued, as one message. The scheduled job."""
        if not self.queue or not self.webhook():
            return
        if self.console.clock.now() < self.quiet_until:
            return  # rate limited, and Discord said for how long

        lines, self.queue = self.queue, []
        if self.dropped:
            lines.append(f"_({self.dropped} more line(s) dropped while Discord was unreachable)_")
            self.dropped = 0

        content, unsent = self.pack(lines)
        # What did not fit is not lost: it goes back to the front of the queue, ahead of whatever
        # arrives before the next flush.
        self.queue = unsent + self.queue

        retry_after = await asyncio.to_thread(self.post, self.webhook(), self.payload(content))
        if retry_after is not None:
            self.quiet_until = self.console.clock.now() + retry_after
            log.warning("discord: rate limited; not posting again for %.0fs", retry_after)

    def payload(self, content: str) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if self.settings.get("username"):
            body["username"] = str(self.settings["username"])
        if self.settings.get("avatar_url"):
            body["avatar_url"] = str(self.settings["avatar_url"])
        return body

    def pack(self, lines: list[str]) -> tuple[str, list[str]]:
        """As many lines as fit in one message, and the ones that do not."""
        content, unsent = "", []
        for index, line in enumerate(lines):
            candidate = f"{content}\n{line}" if content else line
            if len(candidate) > MAX_CONTENT - CONTENT_HEADROOM:
                unsent = lines[index:]
                break
            content = candidate
        if unsent:
            content = f"{content}\n_(+{len(unsent)} more)_"
        return content[:MAX_CONTENT], unsent

    def post(self, url: str, payload: dict[str, Any]) -> float | None:
        """Send one message. Returns how long to stay quiet if rate limited, else None.

        Runs on a worker thread and swallows everything: Discord being down, slow or angry is not a
        reason for a ban to fail or for the event loop to wait on anybody.
        """
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "b3"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT):  # noqa: S310
                return None
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                return self.retry_after(exc)
            log.warning("discord: the webhook answered %s %s", exc.code, exc.reason)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("discord: could not reach the webhook (%s)", exc)
        return None

    @staticmethod
    def retry_after(exc: urllib.error.HTTPError) -> float:
        """How long Discord asked us to wait, out of the 429 it answered with.

        It says so twice — `retry_after` in the body and a `Retry-After` header — and neither is
        guaranteed to be there, so both are tried before falling back to a fixed wait. Reading what
        the server says beats hardcoding a rate: Discord's published limits are per-bucket and it
        does not promise a number for webhooks.
        """
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(body, dict) and "retry_after" in body:
                return max(0.0, float(body["retry_after"]))
        except (ValueError, OSError):
            pass
        header = exc.headers.get("Retry-After") if exc.headers else None
        try:
            return max(0.0, float(header)) if header else DEFAULT_BACKOFF
        except (TypeError, ValueError):
            return DEFAULT_BACKOFF


__all__ = ["DEFAULTS", "MAX_CONTENT", "TEMPLATES", "DiscordPlugin"]
