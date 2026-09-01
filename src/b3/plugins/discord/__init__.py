"""Relays what happens on the server to a Discord channel, through a webhook.

**Outbound only, and on purpose.** Every Discord integration in this corner of the world is — the
community plugins for the classic bot, IW4MAdmin's YADB and IW4ToDiscord — and the one tool that does
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
rate limit within a minute. Lines are queued and flushed together, up to Discord's limits, and those
limits are read from what Discord answers rather than guessed at: a 429 carries `retry_after`, and
this plugin then stays quiet for exactly that long.

**Two shapes, because a channel is read by people.** `style: embeds` (the default) posts a moderation
event as a coloured card — an author line naming the server, the facts as labelled fields
("Banned Player", "By", "Reason"), the map as a thumbnail and the time along the bottom. A channel of those is *scanned*; a channel of
sentences has to be read. Chat, arrivals and map changes keep the sentence, because there is nothing
to label about "Bob: hello everyone" — which is also what keeps `templates` meaningful here: the
events with no fields are exactly the ones whose whole content is the operator's wording.
`style: lines` posts everything as plain text instead, for a compact channel or a webhook feeding
something that parses it.

**Nothing here can cost the bot anything.** The POST runs on a worker thread, every failure is
swallowed and logged, and the queue is bounded — a Discord outage must never stall the event loop or
stop a ban being applied. This is the bot's first outbound integration, so the shape of that is
deliberate: whatever the metrics endpoint does about timeouts and retries should look like this.

**Images.** Nothing is bundled and nothing is fetched. A map picture would mean hosting a screenshot
of every map of 38 titles, and a player avatar would mean a Steam API key and publishing identity to
a third party. Instead `map_image_url` is a template the operator points at their own hosting — and
that is the difference from the plugins in the wild, which hardcode a table of wiki URLs per game and
break the day somebody reorganises a wiki.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, duration_text
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Discord's own limits, from its webhook documentation: `content` is capped at 2000 characters, and
#: one request carries one message. Together those are the whole argument for batching.
MAX_CONTENT = 2000

#: And for embeds: ten per message, six thousand characters across all of them. A flush that has more
#: than ten events to report sends ten and keeps the rest for the next one, which is why the queue
#: survives a flush rather than being emptied by it.
MAX_EMBEDS = 10
MAX_EMBED_TOTAL = 6000

#: Room left for the "and N more" line a truncated batch ends with.
CONTENT_HEADROOM = 120

#: Seconds. Short, because this is a notification rather than a transaction, and long enough that a
#: server which has gone away does not hold a worker thread for a minute.
TIMEOUT = 10.0

#: How long to stay quiet after a rate limit that did not say how long to wait.
DEFAULT_BACKOFF = 30.0

#: The two shapes a message can take. Named rather than a boolean because a third (one embed holding
#: the whole batch as fields) is a plausible thing to want later.
STYLES = ("embeds", "lines")

DEFAULTS: dict[str, object] = {
    # The webhook, from Discord: Channel Settings -> Integrations -> Webhooks -> Copy URL. Empty
    # means this plugin does nothing at all, and says so once rather than per event.
    "webhook": "",
    # What the messages are posted as. Empty keeps whatever the webhook itself is named.
    "username": "",
    "avatar_url": "",
    # `embeds` for a coloured card per event, `lines` for plain text.
    "style": "embeds",
    # Text along the foot of every embed, beside the time. Ignored in `lines` style, which has
    # nowhere to put it.
    "footer": "",
    # The author line at the top of each card. Empty uses the server's own `sv_hostname`, falling
    # back to the title being played — no table of game names is hardcoded here, see `game_title`.
    "game_name": "",
    # A small icon beside it: your community's logo, on your own hosting.
    "icon_url": "",
    # Which events to relay. The moderation ones are on: they are what an admin channel is for.
    # Chat is off, deliberately — see the note about consent in this module's docstring.
    "bans": True,
    "kicks": True,
    "warnings": False,
    "joins": False,
    "leaves": False,
    "chat": False,
    "map_changes": True,
    # `!report`, which needs the `report` plugin loaded to produce anything. Off by default because
    # switching it on without that plugin is a configuration error rather than a quiet no-op — see
    # `check_config`.
    "reports": False,
    # Seconds between flushes. Everything since the last one goes in a single message.
    "flush_seconds": 10,
    # How many lines to hold while Discord is unreachable. Past this the oldest go, because the
    # alternative is a queue that grows for as long as the outage lasts.
    "max_queue": 200,
    # A picture for the map, if you host one: "https://example.com/maps/{map}.jpg". Empty means no
    # image, which is the default because nothing here ships pictures of maps.
    "map_image_url": "",
    # The picture to use when there is no map-specific one — a map you have not got a shot of, or
    # `map_image_url` left empty. Your own URL again: a placeholder shipped here would be a URL on
    # somebody else's hosting, pulled by every server running this bot, and broken the day it moves.
    "map_image_fallback": "",
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
    "report": "\N{POLICE CARS REVOLVING LIGHT} **{name}** was reported by {admin} - {reason}",
}

#: The heading on each embed, and the colour of its stripe. The colours do the work a reader actually
#: uses — nobody reads a moderation channel word by word, they look for the red ones — so they are
#: chosen to be told apart at a glance rather than to be pretty: one red for the bans, amber for the
#: reversible penalties, green for arrivals and good news, grey for the things that are only noise.
EMBED_STYLE: dict[str, tuple[str, int]] = {
    "ban": ("Ban", 0xC0392B),
    "tempban": ("Temporary ban", 0xD35400),
    "kick": ("Kick", 0xE67E22),
    "warn": ("Warning", 0xF1C40F),
    "unban": ("Unban", 0x27AE60),
    "join": ("Joined", 0x2ECC71),
    "leave": ("Left", 0x95A5A6),
    "chat": ("Chat", 0x5865F2),
    "map": ("Map", 0x3498DB),
    "report": ("Report", 0xFF0000),
}

#: What the player is called in the card's first field, per kind. A card that labels its facts —
#: "Kicked Player: boto", "By: SirReaDy" — is read at a glance, while a sentence has to be read.
#: The label names the action, so nothing else has to repeat it.
EMBED_LABELS: dict[str, str] = {
    "ban": "Banned Player",
    "tempban": "Banned Player",
    "kick": "Kicked Player",
    "warn": "Warned Player",
    "unban": "Unbanned Player",
    "report": "Reported Player",
}

#: Colour codes a Call of Duty or Quake 3 name is full of. They mean nothing in Discord and would be
#: read as literal text, so they go.
COLOUR_CODES = tuple(f"^{digit}" for digit in "0123456789")

#: Markdown a player could put in their own name to reformat the rest of a channel's line.
MARKDOWN = ("*", "_", "`", "~", "|")


@dataclass(frozen=True, slots=True)
class Relayed:
    """One thing that happened, rendered but not yet addressed to Discord.

    Held as a record rather than a string because the two styles need different parts of it: `lines`
    wants `text` and nothing else, `embeds` wants the colour that `kind` implies and the fields
    laid out beside the sentence. Rendering once into both would mean formatting every event twice
    and keeping the two in step by hand.
    """

    kind: str
    text: str
    #: The player this is about, and what they are called in the card's first field.
    who: str = ""
    #: Whoever did it, for the footer — "banned by Admin". "b3" when nobody typed it.
    by: str = ""
    #: name, value, inline — Discord's own shape for the fields after the first two.
    fields: tuple[tuple[str, str, bool], ...] = ()
    #: A picture for this event, if the operator hosts one. Only map changes have one today.
    image: str = ""
    #: Set on the synthetic line that reports dropped events, which has no event behind it.
    plain: bool = field(default=False)


class DiscordPlugin(Plugin):
    """Posts what happens on the server to one Discord webhook, batched."""

    requires_plugins = ("admin",)

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
        EventType.CLIENT_REPORT: "reports",
    }

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.templates = dict(TEMPLATES)
        #: Events waiting for the next flush.
        self.queue: list[Relayed] = []
        #: How many were dropped because the queue was full. Said out loud when it recovers, because
        #: a relay that quietly skipped an hour of bans would be worse than one that failed.
        self.dropped = 0
        #: Time before which nothing is sent, set by a rate limit Discord asked us to respect.
        self.quiet_until = 0.0

    # -- setup ---------------------------------------------------------------

    @classmethod
    def check_config(cls, config: object, configured: frozenset[str]) -> list[str]:
        """What is wrong with these settings, given the rest of the config.

        Both of these are the same kind of fault: a setting that reads as though it does something
        and does nothing at all. That is the failure an operator cannot see from in the game — the
        channel is simply quiet — so it is worth refusing to start over.
        """
        settings: dict[str, Any] = {}
        if isinstance(config, dict):
            settings = config.get("settings") or {}

        problems: list[str] = []
        if settings.get("reports") and "report" not in configured:
            problems.append(
                "settings.reports is on, but this server does not load the `report` plugin, so "
                "there is nothing to relay - add `- name: report` to the plugins list, or set "
                "reports: no"
            )
        style = str(settings.get("style", "")).strip().lower()
        if style and style not in STYLES:
            problems.append(f"settings.style is {style!r}; it must be one of {', '.join(STYLES)}")
        return problems

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

        for event_type, setting in self.GOVERNED_BY.items():
            if self.settings.get(setting):
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

    def style(self) -> str:
        """`embeds` or `lines`. An unreadable value falls back rather than raising per event.

        `check_config` refuses to start on a bad one, so reaching the fallback means somebody edited
        the file under a running bot.
        """
        chosen = str(self.settings.get("style") or "").strip().lower()
        return chosen if chosen in STYLES else str(DEFAULTS["style"])

    # -- what happened -------------------------------------------------------

    def on_event(self, event: Event) -> None:
        """Turn an event into a line, or ignore it. Never raises: this is a notifier."""
        try:
            relayed = self.render(event)
        except Exception as exc:  # noqa: BLE001 - a bad template must not cost the event
            log.warning("discord: %s could not be rendered (%s)", event.type.name, exc)
            return
        if relayed is not None:
            self.enqueue(relayed)

    def render(self, event: Event) -> Relayed | None:
        """One event as something postable, or None for an event this does not relay."""
        setting = self.GOVERNED_BY.get(event.type)
        if setting is None or not self.settings.get(setting):
            return None
        name = self.plain(event.client.name if event.client else "")
        admin = self.plain(self.admin_name(event))
        reason = self.plain(str(event.data or "")) or "no reason given"

        if event.type is EventType.CLIENT_BAN:
            return self._relayed(
                "ban", name=name, admin=admin, reason=reason, fields=(("Reason", reason, False),)
            )
        if event.type is EventType.CLIENT_BAN_TEMP:
            duration = self.duration(event)
            return self._relayed(
                "tempban",
                name=name,
                admin=admin,
                reason=reason,
                duration=duration,
                fields=(("For", duration, True), ("Reason", reason, False)),
            )
        if event.type is EventType.CLIENT_UNBAN:
            return self._relayed("unban", name=name, admin=admin)
        if event.type is EventType.CLIENT_KICK:
            return self._relayed(
                "kick", name=name, admin=admin, reason=reason, fields=(("Reason", reason, False),)
            )
        if event.type is EventType.CLIENT_WARN:
            return self._relayed(
                "warn", name=name, admin=admin, reason=reason, fields=(("Reason", reason, False),)
            )
        if event.type is EventType.CLIENT_REPORT:
            # The one event here a *player* raised, so the reporter is named as prominently as the
            # player named: an admin reading the channel has to decide about both.
            return self._relayed(
                "report", name=name, admin=admin, reason=reason, fields=(("Reason", reason, False),)
            )
        if event.type is EventType.CLIENT_JOIN:
            return self._relayed("join", name=name)
        if event.type is EventType.CLIENT_DISCONNECT:
            return self._relayed("leave", name=name)
        if event.type in (EventType.CLIENT_SAY, EventType.CLIENT_TEAM_SAY):
            return self.chat_line(name, str(event.data or ""))
        if event.type is EventType.GAME_MAP_CHANGE:
            return self.map_line(str(event.data or ""))
        return None

    def _relayed(
        self,
        kind: str,
        *,
        fields: tuple[tuple[str, str, bool], ...] = (),
        image: str = "",
        **values: str,
    ) -> Relayed:
        return Relayed(
            kind=kind,
            text=self.templates[kind].format(**values),
            who=values.get("name", ""),
            by=values.get("admin", ""),
            fields=fields,
            image=image,
        )

    def chat_line(self, name: str, said: str) -> Relayed | None:
        """A player's chat, unless it is a command.

        `!login <password>` is typed in the same place as chat and relaying it to a channel would be
        worse than relaying nothing at all.
        """
        text = self.plain(said)
        if not text or text.startswith(("!", "@", "&")):
            return None
        return self._relayed("chat", name=name, text=text)

    def map_line(self, map_name: str) -> Relayed:
        # `clean`, not `plain`: a map name comes from the server rather than from a player, and
        # escaping it would put backslashes through `mp_crash` — and through the URL below, which
        # would then not be a URL any more.
        map_name = self.clean(map_name)
        # Asked for by name rather than read off the game state: the map has just changed, and the
        # state may still hold the old one until the round-start cvars are applied.
        image = self.map_picture(map_name)
        relayed = self._relayed("map", map=map_name, image=image)
        if image and self.style() == "lines":
            # No embed to hang a picture on, so it goes in the text: Discord renders a bare image
            # URL by itself, which is the whole reason there is no embed to configure in this style.
            return Relayed(kind=relayed.kind, text=f"{relayed.text}\n{image}")
        return relayed

    def admin_name(self, event: Event) -> str:
        """Who did it: the admin is the event's `target`, and is often nobody at all."""
        who: Client | None = event.target
        return who.name if who is not None else "b3"

    def duration(self, event: Event) -> str:
        """How long the ban is, worded as the game words it.

        `duration_text` is what the in-game message uses, so a ban the server called "14 days" is
        not "20160" in the channel — and not "a while" either, which is what this said for every
        tempban until the event started carrying the figure.
        """
        minutes = event.extra.get("duration") if event.extra else None
        if minutes is None:
            return "a while"
        try:
            return duration_text(float(minutes))
        except (TypeError, ValueError):
            return str(minutes)

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

    def enqueue(self, relayed: Relayed) -> None:
        limit = max(1, as_int(self.settings.get("max_queue"), 200))
        self.queue.append(relayed)
        while len(self.queue) > limit:
            self.queue.pop(0)
            self.dropped += 1

    async def flush(self) -> None:
        """Post everything queued, as one message. The scheduled job."""
        if not self.queue or not self.webhook():
            return
        if self.console.clock.now() < self.quiet_until:
            return  # rate limited, and Discord said for how long

        pending, self.queue = self.queue, []
        if self.dropped:
            # First, not last. It reads better — "you missed 40 lines" belongs above the ones that
            # survived — and it is the only position that guarantees it goes out at all: a batch of
            # more than ten embeds is cut to Discord's limit, and a notice on the end would be the
            # part that got cut, every time, from the one message whose job is to say something
            # went missing.
            pending.insert(
                0,
                Relayed(
                    kind="dropped",
                    text=f"_({self.dropped} more line(s) dropped while Discord was unreachable)_",
                    plain=True,
                ),
            )
            self.dropped = 0

        payload, unsent = self.pack(pending)
        # What did not fit is not lost: it goes back to the front of the queue, ahead of whatever
        # arrives before the next flush.
        self.queue = unsent + self.queue

        retry_after = await asyncio.to_thread(self.post, self.webhook(), payload)
        if retry_after is not None:
            self.quiet_until = self.console.clock.now() + retry_after
            log.warning("discord: rate limited; not posting again for %.0fs", retry_after)

    def pack(self, pending: list[Relayed]) -> tuple[dict[str, Any], list[Relayed]]:
        """One request's body, and whatever did not fit in it."""
        if self.style() == "lines":
            content, unsent = self.pack_lines(pending)
            body: dict[str, Any] = {"content": content}
        else:
            embeds, unsent = self.pack_embeds(pending)
            body = {"embeds": embeds}
        if self.settings.get("username"):
            body["username"] = str(self.settings["username"])
        if self.settings.get("avatar_url"):
            body["avatar_url"] = str(self.settings["avatar_url"])
        return body, unsent

    def pack_lines(self, pending: list[Relayed]) -> tuple[str, list[Relayed]]:
        """As many lines as fit in one message, and the ones that do not."""
        content, unsent = "", []
        for index, relayed in enumerate(pending):
            candidate = f"{content}\n{relayed.text}" if content else relayed.text
            if len(candidate) > MAX_CONTENT - CONTENT_HEADROOM:
                unsent = pending[index:]
                break
            content = candidate
        if unsent:
            content = f"{content}\n_(+{len(unsent)} more)_"
        return content[:MAX_CONTENT], unsent

    def pack_embeds(self, pending: list[Relayed]) -> tuple[list[dict[str, Any]], list[Relayed]]:
        """Up to ten embeds and six thousand characters, which are Discord's two limits here."""
        embeds: list[dict[str, Any]] = []
        total = 0
        for index, relayed in enumerate(pending):
            embed = self.embed(relayed)
            size = len(json.dumps(embed, ensure_ascii=False))
            if len(embeds) >= MAX_EMBEDS or total + size > MAX_EMBED_TOTAL - CONTENT_HEADROOM:
                return embeds, pending[index:]
            embeds.append(embed)
            total += size
        return embeds, []

    def embed(self, relayed: Relayed) -> dict[str, Any]:
        """One event as a Discord embed, laid out as a card rather than a sentence.

        An author line naming the server, the facts as **labelled fields** — "Reported Player",
                "Server" — a thumbnail of the map, and "reported by X" with a timestamp along the bottom.
                A channel of those is scanned; a channel of sentences is read.

                A moderation event gets that layout. Chat, arrivals and map changes keep the sentence: there
                is nothing to label about "Bob: hello everyone", and a card would be three lines of chrome
                around four words. That is also what keeps `templates` meaningful in this style — the events
                with no fields are exactly the ones whose whole content is the operator's wording.
        """
        label, colour = EMBED_STYLE.get(relayed.kind, ("", 0x2F3136))
        subject = EMBED_LABELS.get(relayed.kind, "")
        embed: dict[str, Any] = {
            "color": colour,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        author = str(self.settings.get("game_name") or "") or self.game_title()
        if author and not relayed.plain:
            embed["author"] = {"name": author}
            if self.settings.get("icon_url"):
                embed["author"]["icon_url"] = str(self.settings["icon_url"])

        fields: list[dict[str, Any]] = []
        if subject and relayed.who:
            fields.append({"name": subject, "value": relayed.who, "inline": True})
            # Who did it, as a column rather than as footer text. It began in the footer, where
            # Discord prints it small and grey under everything else — and the first question asked
            # of a moderation channel after "who" is "who by", so it belongs at the same size as the
            # rest. "b3" here means nobody typed it: a ban list match, a warning that ran out.
            if relayed.by:
                fields.append({"name": "By", "value": relayed.by, "inline": True})
        else:
            # No labels to hang on it, so the sentence *is* the content.
            embed["description"] = relayed.text
            if label and not relayed.plain:
                embed["title"] = label
        fields += [
            {"name": name, "value": value or "-", "inline": inline}
            for name, value, inline in relayed.fields
        ]
        if fields:
            embed["fields"] = fields

        picture = relayed.image or self.map_picture()
        if picture:
            # A thumbnail, not an image: a small map shot in the corner leaves the facts the size
            # they should be. A map *change* is the one event that is about the picture, so that
            # one gets it full width.
            embed["thumbnail" if relayed.kind != "map" else "image"] = {"url": picture}

        # Only the operator's own line. Who did it used to live here and is a field now: Discord
        # prints a footer small and grey under everything else, and "who by" is not a footnote.
        footer = str(self.settings.get("footer") or "")
        if footer:
            embed["footer"] = {"text": footer}
        return embed

    def game_title(self) -> str:
        """What to print on the author line: the server's own name, else the title being played.

        No table of game names is hardcoded here. The plugins that carry one — "Call of Duty 4:
        Modern Warfare" and an icon URL per title — cover five games out of the thirty-eight this
        bot supports, so it would be wrong more often than right, and the URLs rot. `sv_hostname` is
        what the operator already named their server, and `game_name` overrides it.
        """
        game = getattr(self.console, "game", None)
        hostname = self.clean(str(getattr(game, "hostname", "") or ""))
        profile = getattr(self.console, "profile", None)
        return hostname or str(getattr(profile, "name", "") or "")

    def map_picture(self, map_name: str = "") -> str:
        """The picture for a map — the card's thumbnail, and a map change's full-width image.

        Falls back to `map_image_fallback` when there is no per-map URL to build: an operator with
        shots of ten maps and a rotation of thirty gets their placeholder on the other twenty,
        rather than a card that is a different shape depending on the map.
        """
        template = str(self.settings.get("map_image_url") or "")
        game = getattr(self.console, "game", None)
        current = self.clean(map_name or str(getattr(game, "map_name", "") or ""))
        if template and current:
            return template.format(map=current)
        return str(self.settings.get("map_image_fallback") or "")

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


__all__ = [
    "DEFAULTS",
    "EMBED_STYLE",
    "MAX_CONTENT",
    "MAX_EMBEDS",
    "TEMPLATES",
    "DiscordPlugin",
    "Relayed",
]
