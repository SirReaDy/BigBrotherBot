"""Greets players as they arrive, with what the bot already knows about them.

A port of the classic `welcome` plugin. Three messages sent to the player — first visit, returning and
unregistered, returning and registered — and two announced to the server, plus the player's own
greeting if they have set one with `!greeting`. What makes it worth having is that it is the moment a
new player finds out the server has a bot at all: the first-visit message is where `!help` gets
mentioned, and the returning one is where an unregistered player is told about `!register`.

Kept from the classic: the message set, the `newb_connections` idea (a returning player is announced
only while they are still new), the delay before speaking, and the `min_gap` that stops a player who
reconnects twice in a minute being greeted twice.

Changed, and each for a reason:

* **No thread per arrival.** The classic started a `threading.Timer` for every authentication, so a
  full server refilling after a map change was a dozen threads sleeping in parallel. Here an arrival
  is a due time in a queue, checked once a second by the plugin's own scheduled task.
* **The five-minute startup silence is measured from this plugin starting**, not from a console
  uptime value. Same purpose — a bot restarted mid-match authenticates everybody at once, and
  welcoming all of them is a wall of text nobody reads — and no core support needed for it.
* **A greeting too long is refused, at the length the column actually is.** The classic checked 255
  characters against a `varchar(128)`, so a greeting between the two was accepted, saved and silently
  truncated by the database.
* **Placeholders are validated when the greeting is set**, and named in the refusal. The classic
  rewrote `$name` into `%(name)s` and let a bad one raise at greeting time — which is to say in front
  of the whole server, once, on somebody else's arrival.

**`geowelcome` is folded in here rather than ported.** The classic had a second plugin for "welcome,
but name the country": a **subclass of this one** that re-implemented the whole greeting flow to add
two messages, and disabled `welcome` at startup if it found it loaded. Its copy of the message defaults
had already drifted from the originals. What it actually adds is two message variants, so that is what
it is: `announce_first_geo` and `announce_user_geo`, used instead of the plain ones when `geolocation`
has placed the player, with `{place}` available to every message here. Nothing needs configuring and
`welcome` behaves exactly as before on a server with no geolocation at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, as_level
from b3.domain.client import Client
from b3.domain.permissions import max_group

log = logging.getLogger(__name__)

#: The delay range the classic allowed, and it is a sensible one: under fifteen seconds the player is
#: still loading the map and will never see it, and past ninety they have stopped reading chat.
MIN_DELAY = 15
MAX_DELAY = 90

#: How long the greeting may be. Not a taste judgement: `clients.greeting` is `varchar(128)`, so
#: anything longer is truncated by the database rather than refused.
MAX_GREETING = 128

#: Placeholders a player may use in their own greeting. Deliberately short — a greeting is read by
#: everybody on the server, and the classic's list is the same four.
GREETING_FIELDS = ("name", "level", "group", "connections")

DEFAULTS: dict[str, object] = {
    # Which of the five messages to send. All on, as the classic had them.
    "welcome_first": True,  # privately, on somebody's first ever visit
    "welcome_newb": True,  # privately, to a returning player who has not registered
    "welcome_user": True,  # privately, to a returning player who has
    "announce_first": True,  # to the server, on a first visit
    "announce_user": True,  # to the server, for a returning player who is still new
    "show_greeting": True,  # to the server: the player's own line, if they have set one
    # Above this many visits a returning player is no longer announced to everybody. A regular does
    # not need announcing every time they connect.
    "newb_connections": 15,
    # Seconds after connecting before the greeting goes out, so it lands after the map has loaded.
    "delay": 30,
    # Do not greet the same player again within this many seconds. A player who drops and reconnects
    # is the case: they have just read it.
    "min_gap": 3600,
    # Silence for this long after the bot starts. A bot restarted mid-match authenticates everybody
    # at once, and greeting a full server is a wall of text that teaches nobody anything.
    "startup_silence": 300,
    # Level for `!greeting`. The classic's default is mod, which is worth keeping: a greeting is
    # said to everybody, and anybody who has just arrived should not be able to write one.
    "greeting_level": 20,
}

MESSAGES = {
    "welcome_first": "welcome {name}, this must be your first visit — you are player #{id}. "
    "Type !help for help",
    "welcome_newb": "welcome back {name} [@{id}], last seen {last_visit}. Type !register to "
    "register, !help for help",
    "welcome_user": "welcome back {name} [@{id}], last seen {last_visit} — you are a {group}, "
    "here {connections} times",
    "announce_first": "everyone welcome {name}, player #{id}, to the server",
    "announce_user": "everyone welcome back {name}, player #{id} — here {connections} times",
    # Used instead of the two above when `geolocation` has placed the player. This is the whole of
    # what the classic bot's separate `geowelcome` plugin did — see the note in this module's
    # docstring about why it is two messages here rather than a plugin.
    "announce_first_geo": "everyone welcome {name} from {place}, player #{id}, to the server",
    "announce_user_geo": "everyone welcome back {name} from {place}, player #{id} — here "
    "{connections} times",
    "greeting_announce": "{name} joined: {greeting}",
    "greeting_none": "you have no greeting set",
    "greeting_yours": "your greeting is: {greeting}",
    "greeting_changed": "greeting changed to: {greeting}",
    "greeting_cleared": "greeting cleared",
    "greeting_too_long": "that greeting is {length} characters; the limit is {limit}",
    "greeting_bad": "a greeting cannot use {{{field}}} — try {fields}",
    # A player the database has never dated. "Unknown" is a truthful answer and the classic's own.
    "welcome_unknown_visit": "unknown",
}


@dataclass(frozen=True, slots=True)
class Pending:
    """Somebody who has arrived and is waiting to be greeted."""

    client: Client
    due: float


class WelcomePlugin(Plugin):
    """Greets arrivals, and lets players set a line of their own."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self._pending: list[Pending] = []
        #: When this plugin started, for the startup silence. Set in `on_startup` rather than read
        #: from the console, because "how long have I been running" is a question only this plugin
        #: asks and the answer it wants is "since I could have seen these players arrive".
        self._started_at = 0.0

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        delay = as_int(self.settings.get("delay"), 30)
        if not MIN_DELAY <= delay <= MAX_DELAY:
            log.warning(
                "welcome: delay %s is outside %d-%d seconds; using %d — under %d the player is "
                "still loading and past %d they have stopped reading chat",
                delay,
                MIN_DELAY,
                MAX_DELAY,
                DEFAULTS["delay"],
                MIN_DELAY,
                MAX_DELAY,
            )
            self.settings["delay"] = DEFAULTS["delay"]

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self._started_at = self.console.clock.now()
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        # One task for every arrival, rather than the classic's thread each: the delay is a due time,
        # and nothing has to be cancelled when a player leaves before it comes round.
        self.schedule(self._greet_due, second="*", name="WelcomePlugin.greetings")
        registered = self.console.command_registry.get("greeting")
        if registered is not None:  # pragma: no branch - registration is the framework's job
            registered.min_level = as_level(self.settings.get("greeting_level"), 20)

    # -- arrivals ------------------------------------------------------------

    def on_auth(self, event: Event) -> None:
        """Queue a greeting, unless this is the moment not to send one.

        Four refusals, and they are all about not talking over something: a player with no database
        record has nothing to be greeted about, the bot has only just started (so everybody on the
        server is authenticating at once), the player has been greeted recently, or they have already
        left.
        """
        client = event.client
        if client is None or client.id is None or client.cid is None:
            return
        if self.console.clock.now() - self._started_at < as_int(
            self.settings.get("startup_silence"), 300
        ):
            log.debug("welcome: not greeting %s — the bot has only just started", client.name)
            return
        gap = as_int(self.settings.get("min_gap"), 3600)
        if client.last_visit and self.console.clock.epoch() - client.last_visit < gap:
            log.debug("welcome: %s was greeted within the last %ds", client.name, gap)
            return
        due = self.console.clock.now() + as_int(self.settings.get("delay"), 30)
        self._pending = [p for p in self._pending if p.client.cid != client.cid]
        self._pending.append(Pending(client=client, due=due))

    def on_disconnect(self, event: Event) -> None:
        """Somebody who left before their greeting was due does not get one.

        The classic could not do this: its timer had already been handed the client, so it welcomed
        players who were no longer there — a message to a slot that may by then hold somebody else.
        """
        client = event.client
        if client is None or client.cid is None:
            return
        self._pending = [p for p in self._pending if p.client.cid != client.cid]

    def _greet_due(self) -> None:
        now = self.console.clock.now()
        due = [p for p in self._pending if p.due <= now]
        if not due:
            return
        self._pending = [p for p in self._pending if p.due > now]
        for pending in due:
            client = pending.client
            # Checked again here, not only on disconnect: on engines that never report a departure
            # the roster poll is what removes a player, and that can land inside the delay.
            if self.console.clients.get_by_cid(client.cid or "") is not client:
                continue
            self.welcome(client)

    def welcome(self, client: Client) -> None:
        """Say the right things about one player.

        A returning player is one the database has seen before (`connections >= 2`), and a
        *registered* one is anybody whose level is above guest — which is what `!register` gives them.
        The distinction is the whole point of having two messages: one of them advertises `!register`
        and the other would be nagging somebody who has already done it.
        """
        values = self._values(client)
        first_visit = client.connections < 2
        if first_visit:
            if self.settings.get("welcome_first"):
                self.console.tell(client, self.message("welcome_first", **values))
            if self.settings.get("announce_first"):
                self.console.say(
                    self.message(self._announce_key("announce_first", values), **values)
                )
        else:
            registered = client.display_level() > 0
            if registered and self.settings.get("welcome_user"):
                self.console.tell(client, self.message("welcome_user", **values))
            elif not registered and self.settings.get("welcome_newb"):
                self.console.tell(client, self.message("welcome_newb", **values))
            still_new = client.connections < as_int(self.settings.get("newb_connections"), 15)
            if still_new and self.settings.get("announce_user"):
                self.console.say(
                    self.message(self._announce_key("announce_user", values), **values)
                )
        if self.settings.get("show_greeting") and client.greeting:
            self.console.say(
                self.message("greeting_announce", greeting=self._render(client), **values)
            )

    def place_of(self, client: Client) -> str:
        """Where this player is, if `geolocation` is loaded and has placed them; "" otherwise.

        Asked of the plugin rather than read off the client, and asked *optionally*: `welcome` works
        exactly as before on a server with no geolocation at all, which is the point of folding the
        classic's `geowelcome` in here instead of porting it.
        """
        provider = self.console.get_plugin("geolocation")
        location_of = getattr(provider, "location_of", None)
        if location_of is None:
            return ""
        place = location_of(client)
        described = getattr(place, "describe", None)
        return str(described()) if described is not None else ""

    def _announce_key(self, key: str, values: dict[str, object]) -> str:
        """`announce_user`, or `announce_user_geo` when we know where they are.

        Decided at the moment of speaking, which is what makes this need no coordination at all:
        `geolocation` resolves on authentication and this greeting is delayed by half a minute, so by
        the time it goes out the answer is either known or it never will be. The classic's `geowelcome`
        instead subscribed to the geolocation events and started its own timer from them, which is why
        it had to be a copy of this whole plugin.
        """
        return f"{key}_geo" if values.get("place") else key

    def _values(self, client: Client) -> dict[str, object]:
        """The fields every welcome message can use."""
        group = max_group(client.group_bits)
        return {
            "name": client.name,
            "id": client.id if client.id is not None else "?",
            "connections": client.connections,
            "level": client.display_level(),
            "group": group.name if group is not None else "guest",
            # Empty unless `geolocation` is loaded and has placed them. `{place}` is available to
            # every message here, so an operator can name the country in a private greeting too.
            "place": self.place_of(client),
            "last_visit": (
                self.console.format_time(client.last_visit)
                if client.last_visit
                else self.message("welcome_unknown_visit")
            ),
        }

    def _render(self, client: Client) -> str:
        """Fill in a player's own greeting.

        Never raises: the placeholders were checked when the greeting was set, but a greeting written
        by an older version — or imported from a classic database, where they are `%(name)s` — could
        still be anything, and a bad one must cost that player their greeting rather than the
        announcement of somebody's arrival.
        """
        try:
            return client.greeting.format(**self._values(client))
        except (KeyError, IndexError, ValueError) as exc:
            log.warning(
                "welcome: %s has a greeting that cannot be filled in (%s)", client.name, exc
            )
            return client.greeting

    # -- the command ---------------------------------------------------------

    @command(level=20)
    def cmd_greeting(self, ctx: CommandContext) -> None:
        """greeting [text|none] - set the line said when you join ({name}, {group}, {connections})"""
        text = ctx.args.strip()
        client = ctx.client
        if not text:
            if client.greeting:
                ctx.reply(self.message("greeting_yours", greeting=client.greeting))
            else:
                ctx.reply(self.message("greeting_none"))
            return
        if text.lower() == "none":
            client.greeting = ""
            self.console.storage.save_client(client)
            ctx.reply(self.message("greeting_cleared"))
            return
        if len(text) > MAX_GREETING:
            # Named rather than truncated: a greeting silently cut off mid-sentence looks like the
            # bot mangling it, which is how the classic's 255-against-a-128-column check read.
            ctx.reply(self.message("greeting_too_long", length=len(text), limit=MAX_GREETING))
            return
        bad = self._unknown_field(text, client)
        if bad is not None:
            ctx.reply(
                self.message(
                    "greeting_bad",
                    field=bad,
                    fields=", ".join(f"{{{name}}}" for name in GREETING_FIELDS),
                )
            )
            return
        client.greeting = text
        self.console.storage.save_client(client)
        ctx.reply(self.message("greeting_changed", greeting=self._render(client)))

    def _unknown_field(self, text: str, client: Client) -> str | None:
        """The placeholder that will not work, or None. Checked now, in front of one person.

        The classic validated by *rendering* at greeting time, so a greeting with a typo in it failed
        on somebody else's arrival — a message the player who wrote it would probably never see.
        """
        try:
            text.format(**self._values(client))
        except KeyError as exc:
            return str(exc.args[0]) if exc.args else "?"
        except (IndexError, ValueError):
            return "?"
        return None


__all__ = [
    "DEFAULTS",
    "GREETING_FIELDS",
    "MAX_GREETING",
    "MESSAGES",
    "Pending",
    "WelcomePlugin",
]
