"""Removes players who have stopped playing but not stopped occupying a slot.

A port of the classic `afk` plugin, and the good judgement in the original is what it *doesn't* do:
it never sweeps the server on a timer. Nobody is checked until there is a reason to think somebody is
away — a player has died several times in a row without doing anything, or another player has said
"afk" in chat. Then the suspect is asked privately whether they are there, the server is told they
have a few seconds to answer, and only silence gets them kicked.

That shape is worth keeping exactly. An AFK kicker that sweeps is a kicker that eventually removes
somebody who was reloading, defending a corner, or on a slow computer loading the map.

Kept from the classic: the two triggers, the fifteen-second throttle on the chat trigger, the
"last chance" delay, `min_ingame_humans` (never empty the server through this), immunity by level,
and clearing every activity record at a round or map change so a loading player is safe.

Changed, and each for a reason:

* **No timer per suspect.** The classic held a `WeakKeyDictionary` of `threading.Timer` objects and
  had to cancel them on activity, on disconnect, on a round change and on being disabled. A suspect
  is a deadline here, and one scheduled task reads the clock.
* **Bots are excluded by a flag rather than by a guess.** `Client.is_bot` is set where the AI's guid
  is recognised and dropped; before that this plugin would have had to infer it from an empty guid,
  which is also what an unidentified *person* looks like on a plain Quake 3 server.
* **A player with no activity recorded yet is never kicked**, as in the classic — but the reason is
  now stated where it is relied on: it is what makes a bot restart, a map change and a fresh join all
  safe without a special case for each.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Every event that means "this player is still there". Wide on purpose: the cost of missing one is
#: kicking somebody who is playing, and the cost of an extra one is a check not happening.
ACTIVITY_EVENTS = (
    EventType.CLIENT_CONNECT,
    EventType.CLIENT_AUTH,
    EventType.CLIENT_JOIN,
    EventType.CLIENT_SPAWN,
    EventType.CLIENT_TEAM_CHANGE,
    EventType.CLIENT_NAME_CHANGE,
    EventType.CLIENT_SAY,
    EventType.CLIENT_TEAM_SAY,
    EventType.CLIENT_SQUAD_SAY,
    EventType.CLIENT_PRIVATE_SAY,
    EventType.CLIENT_RADIO,
    EventType.CLIENT_CALLVOTE,
    EventType.CLIENT_VOTE,
    EventType.CLIENT_KILL,
    EventType.CLIENT_KILL_TEAM,
    EventType.CLIENT_GIB,
    EventType.CLIENT_GIB_TEAM,
    EventType.CLIENT_GIB_SELF,
    EventType.CLIENT_DAMAGE,
    EventType.CLIENT_DAMAGE_SELF,
    EventType.CLIENT_DAMAGE_TEAM,
    EventType.CLIENT_ACTION,
    EventType.CLIENT_ASSIST,
    EventType.CLIENT_ITEM_PICKUP,
    EventType.CLIENT_FLAG_CAPTURE_TIME,
)

#: Events that mean the game has paused around everybody: a round starting or ending, a warmup, a new
#: map. All activity records are dropped at each, because a player loading a map is not away.
BREAK_EVENTS = (
    EventType.GAME_ROUND_START,
    EventType.GAME_ROUND_END,
    EventType.GAME_WARMUP,
    EventType.GAME_MAP_CHANGE,
)

#: The word in chat that starts a sweep, and how often that may happen. The throttle is what stops an
#: argument about who is AFK from checking the whole server on every line of it.
CHAT_TRIGGER = "afk"
CHAT_TRIGGER_THROTTLE = 15.0

#: Bounds the classic enforced, and they are the sensible ones: under thirty seconds of inactivity is
#: an ordinary firefight, and a last chance shorter than fifteen seconds is not a chance.
MIN_INACTIVITY = 30
MIN_LAST_CHANCE = 15
MAX_LAST_CHANCE = 60

DEFAULTS: dict[str, object] = {
    # Deaths in a row with no activity in between before a player is checked. 0 = only ever check
    # when somebody says "afk" in chat.
    "consecutive_deaths": 3,
    # Seconds of silence that make a checked player a suspect.
    "inactivity_threshold": 50,
    # Seconds a suspect has to prove they are there.
    "last_chance_delay": 20,
    # Never take the server below this many playing humans through an AFK kick. An empty server is a
    # worse outcome than an idle player in it.
    "min_ingame_humans": 1,
    # At or above this level, nobody is asked. 100 keeps it to superadmins, as the classic did.
    "immunity_level": 100,
}

MESSAGES = {
    "afk_are_you_there": "are you AFK? Say something or you will be kicked",
    "afk_suspected": "{name} may be AFK - kicking in {seconds}s unless they say something",
    "afk_still_here": "you are not AFK, then - thanks",
    "afk_kick_reason": "AFK for too long",
}


@dataclass
class Activity:
    """What this plugin knows about one player.

    ``last_seen`` is None until they have done something the bot noticed, and that is load-bearing:
    a player with no record is never kicked, which is what makes a bot restart, a map change and a
    fresh join safe without three special cases.
    """

    last_seen: float | None = None
    #: Deaths since they last did anything.
    deaths: int = 0
    #: When their last chance runs out, or 0 when they are not a suspect.
    answer_by: float = 0.0


class AfkPlugin(Plugin):
    """Asks players who look absent whether they are, and removes the ones who do not answer."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self._last_sweep = 0.0

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.settings["inactivity_threshold"] = self._floor(
            "inactivity_threshold", MIN_INACTIVITY, "an ordinary firefight is longer than that"
        )
        delay = as_int(self.settings.get("last_chance_delay"), 20)
        if delay < MIN_LAST_CHANCE or delay > MAX_LAST_CHANCE:
            log.warning(
                "afk: last_chance_delay %s is outside %d-%d seconds; using %d. Shorter is not a "
                "chance, and longer leaves a slot occupied while the bot waits",
                delay,
                MIN_LAST_CHANCE,
                MAX_LAST_CHANCE,
                DEFAULTS["last_chance_delay"],
            )
            self.settings["last_chance_delay"] = DEFAULTS["last_chance_delay"]
        for key in ("consecutive_deaths", "min_ingame_humans"):
            if as_int(self.settings.get(key), 0) < 0:
                log.warning("afk: %s cannot be negative; using 0", key)
                self.settings[key] = 0

    def _floor(self, key: str, minimum: int, why: str) -> int:
        value = as_int(self.settings.get(key), as_int(DEFAULTS.get(key), minimum))
        if value < minimum:
            log.warning("afk: %s cannot be less than %ds — %s", key, minimum, why)
            return minimum
        return value

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        for event_type in ACTIVITY_EVENTS:
            self.subscribe(event_type, self.on_activity)
        for event_type in BREAK_EVENTS:
            self.subscribe(event_type, self.on_break)
        # A death is activity for the killer and a strike against the victim, so it is handled after
        # `on_activity` has already credited whoever did it.
        self.subscribe(EventType.CLIENT_KILL, self.on_death)
        self.subscribe(EventType.CLIENT_SUICIDE, self.on_death)
        self.subscribe(EventType.CLIENT_SAY, self.on_chat)
        self.subscribe(EventType.CLIENT_TEAM_SAY, self.on_chat)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.schedule(self._check_deadlines, second="*", name="AfkPlugin.deadlines")

    def on_disable(self) -> None:
        """Forget every suspect. A plugin switched off mid-question must not answer it later."""
        for client in self.console.clients.connected():
            self.activity(client).answer_by = 0.0

    # -- the record ----------------------------------------------------------

    def activity(self, client: Client) -> Activity:
        record = client.get_var(self, "afk")
        if not isinstance(record, Activity):
            record = Activity()
            client.set_var(self, "afk", record)
        return record

    def playing_humans(self) -> int:
        """People — not bots, not spectators — currently in the game."""
        return len(
            [
                c
                for c in self.console.clients.connected()
                if not c.is_bot and (c.team or "") != "spec"
            ]
        )

    # -- handlers ------------------------------------------------------------

    def on_activity(self, event: Event) -> None:
        """Anything a player does clears their record, and answers the question if one was asked."""
        client = event.client
        if client is None:
            return
        record = self.activity(client)
        if record.answer_by:
            record.answer_by = 0.0
            self.console.tell(client, self.message("afk_still_here"))
        record.last_seen = self.console.clock.now()
        record.deaths = 0

    def on_death(self, event: Event) -> None:
        """Count a death against the victim, and check them once they have collected enough.

        A suicide is not counted: it is something the player *did*, so `on_activity` has already
        credited them with being there. The classic drew the same line, and it matters — a player
        killing themselves repeatedly is annoying, not absent.
        """
        victim = event.target if event.target is not None else event.client
        if victim is None or victim.cid is None:
            return
        if event.client is not None and event.client.cid == victim.cid:
            return  # a suicide, already counted as activity
        threshold = as_int(self.settings.get("consecutive_deaths"), 3)
        record = self.activity(victim)
        record.deaths += 1
        if threshold <= 0 or record.deaths < threshold:
            return
        if self.playing_humans() <= as_int(self.settings.get("min_ingame_humans"), 1):
            return  # too few people here to be removing any
        self.check(victim)

    def on_chat(self, event: Event) -> None:
        """Somebody said "afk". Look at everybody — but not more often than every fifteen seconds.

        The trigger is a word rather than a command on purpose: what players actually type is "bob is
        afk", not `!afk bob`. The throttle is what stops an argument about it sweeping the server on
        every line.
        """
        text = str(event.data or "").lower()
        if CHAT_TRIGGER not in text:
            return
        now = self.console.clock.now()
        if now - self._last_sweep < CHAT_TRIGGER_THROTTLE:
            return
        self._last_sweep = now
        for client in list(self.console.clients.connected()):
            self.check(client)

    def on_break(self, event: Event) -> None:
        """A round change, a warmup or a new map: forget everything.

        Without this the first players to load a new map are the ones with no activity since before
        it, which is to say the slowest computers on the server get kicked for being slow.
        """
        for client in self.console.clients.connected():
            record = self.activity(client)
            record.last_seen = None
            record.deaths = 0
            record.answer_by = 0.0

    def on_disconnect(self, event: Event) -> None:
        client = event.client
        if client is None:
            return
        client.del_var(self, "afk")

    # -- checking ------------------------------------------------------------

    def check(self, client: Client) -> None:
        """Ask this player whether they are there, if they look as though they are not."""
        if self.is_inactive(client) and not self.activity(client).answer_by:
            self.ask(client)

    def is_inactive(self, client: Client) -> bool:
        """Whether this player looks absent. Four reasons it answers no, and each is a real case.

        A bot cannot answer a question. A spectator is *supposed* to be doing nothing. An immune
        player is exempt by rank. And a player with nothing recorded has not been seen doing anything
        **yet** — a fresh join, a map change, a bot that has only just started — where kicking on no
        evidence is the one outcome worse than an idle slot.
        """
        if client.is_bot:
            return False
        if (client.team or "") == "spec":
            return False
        if client.max_level() >= as_level(self.settings.get("immunity_level"), 100):
            return False
        record = self.activity(client)
        if record.last_seen is None:
            return False
        idle = self.console.clock.now() - record.last_seen
        return idle > as_int(self.settings.get("inactivity_threshold"), 50)

    def ask(self, client: Client) -> None:
        """Ask privately, announce publicly, and start the clock.

        Both messages, because they do different jobs: the private one is the only one the suspect
        will see if they are looking at the screen, and the public one tells everybody else why a
        player is about to disappear — which is what stops it looking like an admin being arbitrary.
        """
        seconds = as_int(self.settings.get("last_chance_delay"), 20)
        self.activity(client).answer_by = self.console.clock.now() + seconds
        self.console.tell(client, self.message("afk_are_you_there"))
        self.console.say(self.message("afk_suspected", name=client.name, seconds=seconds))

    def _check_deadlines(self) -> None:
        now = self.console.clock.now()
        for client in list(self.console.clients.connected()):
            record = self.activity(client)
            if record.answer_by and now >= record.answer_by:
                record.answer_by = 0.0
                self.kick_if_still_absent(client)

    def kick_if_still_absent(self, client: Client) -> None:
        """Kick, unless they answered or the server can no longer spare them.

        The population is checked *again* here rather than only when the question was asked: people
        leave, and a server that has emptied out in the twenty seconds since is a server where the
        remaining idle player is the only reason anybody else can find a game at all.
        """
        if self.playing_humans() <= as_int(self.settings.get("min_ingame_humans"), 1):
            log.info("afk: not kicking %s — too few players left on the server", client.name)
            return
        if not self.is_inactive(client):
            return
        self.console.kick(client, reason=self.message("afk_kick_reason"))


__all__ = [
    "ACTIVITY_EVENTS",
    "BREAK_EVENTS",
    "CHAT_TRIGGER",
    "DEFAULTS",
    "MESSAGES",
    "Activity",
    "AfkPlugin",
]
