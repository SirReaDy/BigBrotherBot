"""Announces killing sprees, and the deaths that end them.

A port of the classic `spree` plugin. Every fifth kill without dying gets a line to the whole server,
whoever ends it gets the credit, and a player who has died seven times in a row gets told to keep at
it. It is the cheapest thing in this tree to implement and one of the most noticed: a spree
announcement is the server talking about a player by name, which is most of what makes a server feel
like a place.

Kept from the classic: the two tables (a message per kill count, a message per death count), the
start/end message pairing, `{player}` and `{victim}` in the operator's own text, resetting at the end
of a map, and the exact order the announcements come out in - a killer's own losing streak ending is
announced before the spree they just ended, which is what the captured tests pin.

Changed, and the first two are faults:

* **A spree now ends when the player dies, not only when they are shot.** The classic listened for
  `EVT_CLIENT_KILL` alone, so on the engines that report a gib as its own event a spree never started
  at all, and a player could keep one alive indefinitely by walking off a ledge - a suicide, a team
  kill and a gib were all invisible to it. All four kinds of death count here, and only an honest
  kill adds to a spree: being team-killed ends yours, and team-killing somebody does not extend the
  killer's.
* **The map reset works on more than three titles.** It hung off `EVT_GAME_EXIT`, which only the Call
  of Duty, Quake 3 and Source parsers ever emit - so on Frostbite, Homefront, Altitude, Ravaged and
  Frontline `reset_spree: yes` was a setting that did nothing, and a spree carried across maps until
  the player left. `reset_on` reads the map change as well, and can be told to reset per round for the
  titles where a round is the unit that matters.
* **A message table with one bad line no longer loses the good ones.** `int(kills)` on a
  non-numeric key raised at load, and a message with no `#` in it was dropped whole - start message
  included - behind a warning that called a losing-spree message a "killingspree message". Each entry
  is refused on its own here, by name, and the `end` half is optional: an operator who wants an
  announcement only when a spree *starts* can have one.
* **The tables are rebuilt on every config load**, where the classic's were class attributes it only
  ever added to: a threshold removed from the config went on firing until the bot was restarted, and
  two plugin instances in one process shared one table.
* **Placeholders are checked when the config is read**, not silently left in the line. The classic
  substituted `%player%` and `%victim%` and sent whatever else it found to the server as literal text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: What an operator may write in a spree message. `player` is whoever the line is *about* - the player
#: on the spree when it starts, and the one who ended it when it ends - and `victim` is the other
#: party. Confusing read cold, and kept exactly as the classic had it, because these two names are the
#: contract every existing spree config is written against.
PLACEHOLDERS = ("player", "victim")

#: The separator in the classic's compact form, `start # end`, which its config file shipped and every
#: operator's copy still uses. Accepted alongside the explicit `start:`/`end:` mapping.
COMPACT_SEPARATOR = "#"

#: A kill that counts towards a spree, and takes one away from the player on the receiving end. A gib
#: is a kill on the engines that report one (Enemy Territory), and leaving it out is why the classic
#: announced no sprees at all there.
KILL_EVENTS = (EventType.CLIENT_KILL, EventType.CLIENT_GIB)

#: A death that ends the victim's spree while crediting nobody. Team kills, because a spree is
#: something to be proud of and shooting your own side is not.
TEAM_KILL_EVENTS = (EventType.CLIENT_KILL_TEAM, EventType.CLIENT_GIB_TEAM)

#: A death nobody else caused. Ends a spree, and quietly: the end message names whoever did it, and
#: there is no such person.
SELF_KILL_EVENTS = (EventType.CLIENT_SUICIDE, EventType.CLIENT_GIB_SELF)

DEFAULTS: dict[str, object] = {
    # When everybody's counts go back to zero: `map` (the classic's, and both the end of a map and a
    # change of map count), `round` (also between rounds, for the round-based titles), or `never`.
    "reset_on": "map",
    # Level for `!spree`. `user` is the classic's default.
    "min_level": 1,
}

#: The classic's shipped tables, and they are a sensible default: nothing until five, then every five.
DEFAULT_KILLING_SPREES: dict[int, tuple[str, str]] = {
    5: (
        "{player} is on a killing spree - 5 kills in a row",
        "{player} stopped the spree of {victim}",
    ),
    10: ("{player} is on fire! 10 kills in a row", "{player} iced {victim}"),
    15: (
        "{player} is GODLIKE! 15 kills in a row",
        "{player} took {victim} back to the ground again",
    ),
    20: ("{player} is UNSTOPPABLE! 20 kills in a row", "finally, {player} stopped {victim}"),
}

DEFAULT_LOSING_SPREES: dict[int, tuple[str, str]] = {
    7: ("keep it up {player}, it will come eventually", "{player} is back in business"),
}

MESSAGES = {
    "spree_kills_self": "you have {count} kills in a row",
    "spree_kills_other": "{name} has {count} kills in a row",
    "spree_deaths_self": "you have {count} deaths in a row",
    "spree_deaths_other": "{name} has {count} deaths in a row",
    "spree_none_self": "you are not having a spree right now",
    "spree_none_other": "{name} is not having a spree right now",
}


@dataclass
class Spree:
    """One player's streak.

    Only one of the two counts can be non-zero: a kill zeroes the deaths and a death zeroes the
    kills, which is what makes "a spree" mean "without the other thing happening".

    The two messages are held rather than looked up again when the streak ends, because the threshold
    that started it is no longer derivable from the count once it has moved on - and because it is the
    message the operator paired with *that* threshold that belongs with it.
    """

    kills: int = 0
    deaths: int = 0
    end_kill_message: str | None = None
    end_loss_message: str | None = None


def parse_spree_messages(section: object, where: str) -> dict[int, tuple[str, str]]:
    """Read a `count: message` table, refusing bad entries one at a time rather than in bulk.

    Two spellings are accepted, because operators have the first one already: the classic's compact
    `start # end` string, and a mapping with `start:` and an optional `end:`. A missing `end` means
    the start of that spree is announced and its end is not, which the classic could not express - it
    dropped the whole entry, start message included.
    """
    if not isinstance(section, dict):
        return {}
    table: dict[int, tuple[str, str]] = {}
    for key, value in section.items():
        count = _threshold(key, where)
        if count is None:
            continue
        pair = _message_pair(value, f"{where}.{key}")
        if pair is None:
            continue
        table[count] = pair
    return table


def _threshold(key: object, where: str) -> int | None:
    text = str(key).strip()
    if not text.isdigit() or int(text) < 1:
        log.error(
            "spree: %s has the entry %r, which is not a count of 1 or more; ignored", where, key
        )
        return None
    return int(text)


def _message_pair(value: object, where: str) -> tuple[str, str] | None:
    if isinstance(value, dict):
        start = str(value.get("start") or "").strip()
        end = str(value.get("end") or "").strip()
    else:
        text = str(value)
        start, _, end = text.partition(COMPACT_SEPARATOR)
        start, end = start.strip(), end.strip()
    if not start:
        log.error("spree: %s has no start message; ignored", where)
        return None
    for message in (start, end):
        bad = _bad_placeholder(message)
        if bad is not None:
            log.error(
                "spree: %s uses {%s}, which is not a thing this plugin knows; the message would "
                "reach the server as written. Use %s. Entry ignored",
                where,
                bad,
                " or ".join(f"{{{name}}}" for name in PLACEHOLDERS),
            )
            return None
    return start, end


def _bad_placeholder(message: str) -> str | None:
    """The first `{…}` in a message that is not one this plugin fills in."""
    rest = message
    while "{" in rest:
        _, _, rest = rest.partition("{")
        name, sep, rest = rest.partition("}")
        if not sep:
            return name.strip() or "?"
        if name.strip() not in PLACEHOLDERS:
            return name.strip()
    return None


class SpreePlugin(Plugin):
    """Announces killing and losing streaks, and answers `!spree`."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.killing_sprees: dict[int, tuple[str, str]] = {}
        self.losing_sprees: dict[int, tuple[str, str]] = {}

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        reset = str(self.settings.get("reset_on") or "map").strip().lower()
        if reset not in ("map", "round", "never"):
            log.warning("spree: reset_on %r is not map, round or never; using map", reset)
            reset = "map"
        self.settings["reset_on"] = reset
        # Rebuilt, never added to: the classic held these on the class and only ever inserted, so a
        # threshold taken out of the config kept firing until the bot was restarted.
        #
        # A section that is *present and empty* means the operator wants no announcements of that
        # kind, and gets none. The defaults are for a section nobody wrote at all - which is what a
        # plugin loaded with no config of its own looks like, and where saying nothing would be a
        # plugin that appears to do nothing.
        self.killing_sprees = (
            parse_spree_messages(config.get("killing_sprees"), "killing_sprees")
            if "killing_sprees" in config
            else dict(DEFAULT_KILLING_SPREES)
        )
        self.losing_sprees = (
            parse_spree_messages(config.get("losing_sprees"), "losing_sprees")
            if "losing_sprees" in config
            else dict(DEFAULT_LOSING_SPREES)
        )
        registered = self.console.command_registry.get("spree")
        if registered is not None:
            registered.min_level = as_level(self.settings.get("min_level"), 1)

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        for event_type in KILL_EVENTS:
            self.subscribe(event_type, self.on_kill)
        for event_type in TEAM_KILL_EVENTS:
            self.subscribe(event_type, self.on_team_kill)
        for event_type in SELF_KILL_EVENTS:
            self.subscribe(event_type, self.on_self_kill)
        for event_type in (EventType.GAME_EXIT, EventType.GAME_MAP_CHANGE):
            self.subscribe(event_type, self.on_map_over)
        self.subscribe(EventType.GAME_ROUND_END, self.on_round_over)
        self.subscribe(EventType.CLIENT_DISCONNECT, self.on_disconnect)
        self.on_load_config()

    # -- the record ----------------------------------------------------------

    def spree(self, client: Client) -> Spree:
        record = client.get_var(self, "spree")
        if not isinstance(record, Spree):
            record = Spree()
            client.set_var(self, "spree", record)
        return record

    # -- handlers ------------------------------------------------------------

    def on_kill(self, event: Event) -> None:
        """A kill: one for the killer, and the end of whatever the victim had going."""
        killer, victim = event.client, event.target
        if killer is not None and victim is not None and killer is victim:
            # Some engines report a self-inflicted death as a kill on oneself. It is a death.
            self.record_death(victim, killer=None)
            return
        if killer is not None:
            self.record_kill(killer, victim)
        if victim is not None:
            self.record_death(victim, killer=killer)

    def on_team_kill(self, event: Event) -> None:
        """The victim's spree ends; the killer's does not grow.

        The classic saw neither half of this, listening only for `EVT_CLIENT_KILL` - so on the titles
        where a chaotic round is mostly team kills, a spree survived being shot by your own side.
        """
        victim = event.target
        if victim is not None:
            self.record_death(victim, killer=event.client)

    def on_self_kill(self, event: Event) -> None:
        """A suicide ends a spree, and ends it quietly - there is nobody to credit."""
        victim = event.target if event.target is not None else event.client
        if victim is not None:
            self.record_death(victim, killer=None)

    def on_map_over(self, event: Event) -> None:
        if self.settings.get("reset_on") in ("map", "round"):
            self.reset_everybody()

    def on_round_over(self, event: Event) -> None:
        if self.settings.get("reset_on") == "round":
            self.reset_everybody()

    def on_disconnect(self, event: Event) -> None:
        if event.client is not None:
            event.client.del_var(self, "spree")

    def reset_everybody(self) -> None:
        for client in self.console.clients.connected():
            client.set_var(self, "spree", Spree())

    # -- counting ------------------------------------------------------------

    def record_kill(self, killer: Client, victim: Client | None) -> None:
        record = self.spree(killer)
        record.kills += 1
        # Their own losing streak is over, and that is announced before the spree they may have just
        # ended - the order the classic's tests pin, and the readable one: the two lines are about
        # different people.
        if record.end_loss_message:
            self.announce(record.end_loss_message, player=killer, other=victim)
            record.end_loss_message = None
        pair = self.killing_sprees.get(record.kills)
        if pair is not None:
            record.end_kill_message = pair[1] or None
            self.announce(pair[0], player=killer, other=victim)
        record.deaths = 0

    def record_death(self, victim: Client, *, killer: Client | None) -> None:
        record = self.spree(victim)
        record.deaths += 1
        if record.end_kill_message:
            # Named after whoever did it, which is why a suicide says nothing: the message is written
            # as "{player} stopped the spree of {victim}" and there is no `player`.
            if killer is not None:
                self.announce(record.end_kill_message, player=killer, other=victim)
            record.end_kill_message = None
        pair = self.losing_sprees.get(record.deaths)
        if pair is not None:
            record.end_loss_message = pair[1] or None
            self.announce(pair[0], player=victim, other=killer)
        record.kills = 0

    def announce(self, message: str, *, player: Client, other: Client | None) -> None:
        """Say an operator's line, with the two names in it.

        `{victim}` with nobody to put in it becomes empty rather than the word None - a message
        written for a kill, reached by a death nobody caused, should read as clumsy English at worst.
        """
        self.console.say(
            message.format(player=player.name, victim=other.name if other is not None else "")
        )

    # -- the command ---------------------------------------------------------

    @command("spree", level=1)
    def cmd_spree(self, ctx: CommandContext) -> None:
        """spree [player] - what streak you, or somebody else, is on"""
        handle = ctx.args.strip()
        target = ctx.client
        if handle:
            found = self.resolve_client(ctx, handle)
            if found is None:
                return
            target = found
        record = self.spree(target)
        about_self = target is ctx.client
        if record.kills > 0:
            key = "spree_kills_self" if about_self else "spree_kills_other"
            ctx.reply(self.message(key, name=target.name, count=record.kills))
            return
        if record.deaths > 0:
            key = "spree_deaths_self" if about_self else "spree_deaths_other"
            ctx.reply(self.message(key, name=target.name, count=record.deaths))
            return
        key = "spree_none_self" if about_self else "spree_none_other"
        ctx.reply(self.message(key, name=target.name))


__all__ = [
    "COMPACT_SEPARATOR",
    "DEFAULTS",
    "DEFAULT_KILLING_SPREES",
    "DEFAULT_LOSING_SPREES",
    "KILL_EVENTS",
    "MESSAGES",
    "PLACEHOLDERS",
    "SELF_KILL_EVENTS",
    "TEAM_KILL_EVENTS",
    "Spree",
    "SpreePlugin",
    "parse_spree_messages",
]
