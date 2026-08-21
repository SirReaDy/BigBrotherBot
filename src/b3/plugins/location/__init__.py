"""Announces where players are connecting from, and answers questions about it.

A port of the classic `location` plugin: the user-facing half of the geolocation family. It announces
each arrival's country, and adds `!locate`, `!distance` and `!isp`. Everything it knows comes from
`geolocation`, which reads a local database — see that plugin for where the database comes from.

Kept from the classic: the four messages, the announcement on arrival, the silence for a few minutes
after the bot starts, and the Haversine distance in kilometres — including its constants, so an
operator who knew the old numbers gets the same ones.

Changed, and the first two are faults:

* **Two players in the same place could no longer be "not computable".** The distance function
  returned `False` for "not enough data" and a number otherwise, and the caller tested `if not
  distance:` — so a genuine distance of **0.0 km** was reported as a failure. Two players in one city
  is not an exotic case; on a country-level database *every* pair in the same country has the same
  coordinates.
* **"I do not know" is no longer printed as `--`.** Missing fields were substituted as the two
  characters `--`, so with a country-only database `!locate bob` answered "Bob is connected from --
  (Germany)" and `!isp bob` answered "Bob is using -- as isp". A field the database does not carry now
  means the message that says so.
* **It does not switch other plugins on.** `onEnable` walked its `requiresPlugins` and called
  `enable()` on any that were off — a plugin overriding the operator's decision about a different
  plugin, and an `AttributeError` if one of them was not loaded at all. Dependencies are the loader's
  job (`requires_plugins`), and a provider that is disabled simply places nobody.
* **The startup silence is a setting.** The classic hardcoded five minutes as `upTime() > 300`, which
  is right — a bot restarted mid-match authenticates everybody at once — but undocumented and
  unchangeable, and measured from the *console's* uptime rather than from this plugin starting.
"""

from __future__ import annotations

import logging
import math

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int
from b3.domain.client import Client
from b3.plugins.geolocation import GeolocationPlugin, Location

log = logging.getLogger(__name__)

#: Earth's radius in kilometres, as the classic used it. The number matters: an operator who knew the
#: old answers should get the same ones.
EARTH_RADIUS_KM = 6371

DEFAULTS: dict[str, object] = {
    # Announce each arrival's location to the server.
    "announce": True,
    # Say nothing for this many seconds after the plugin starts. A bot restarted mid-match
    # authenticates everybody at once, and announcing a full server is a wall of text nobody reads.
    "startup_silence": 300,
    # Levels for the three commands. The classic's config: user, user, mod.
    "locate_level": 1,
    "distance_level": 1,
    "isp_level": 20,
}

MESSAGES = {
    "location_connected": "{name} connected from {place}",
    "location_locate": "{name} is connected from {place}",
    "location_locate_failed": "I do not know where {name} is connecting from",
    "location_distance": "{name} is {distance} km away from you",
    "location_distance_self": "you are exactly where you are",
    "location_distance_failed": "I cannot work out the distance to {name}",
    "location_isp": "{name} is on {isp}",
    "location_isp_failed": "I do not know whose network {name} is on",
    "location_usage": "name a player: !{command} <player>",
}


def distance_km(one: Location, other: Location) -> float | None:
    """Great-circle distance between two locations in kilometres, or None if either has no position.

    The Haversine formula and the constant the classic used. None rather than `False` for "cannot
    say", because 0.0 is a perfectly good answer — two players in the same city, or any two players at
    all on a country-level database — and the classic's caller could not tell the two apart.
    """
    if one.latitude is None or one.longitude is None:
        return None
    if other.latitude is None or other.longitude is None:
        return None
    lat1, lon1 = math.radians(one.latitude), math.radians(one.longitude)
    lat2, lon2 = math.radians(other.latitude), math.radians(other.longitude)
    d_lat, d_lon = lat2 - lat1, lon2 - lon1
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lon / 2) ** 2
    return round(abs(EARTH_RADIUS_KM * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))), 2)


class LocationPlugin(Plugin):
    """Announces where players connect from, and answers `!locate`, `!distance` and `!isp`."""

    requires_plugins = ("admin", "geolocation")
    load_after = ("geolocation",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        #: When this plugin started, for the startup silence. Measured here rather than from the
        #: console's uptime, which is a different question and not this plugin's to ask.
        self._started_at = 0.0

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        for name, key in (
            ("locate", "locate_level"),
            ("distance", "distance_level"),
            ("isp", "isp_level"),
        ):
            registered = self.console.command_registry.get(name)
            if registered is not None:
                registered.min_level = as_int(self.settings.get(key), 1)

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self._started_at = self.console.clock.now()
        self.subscribe(EventType.CLIENT_GEOLOCATION_SUCCESS, self.on_located)
        self.on_load_config()

    # -- where anybody is ----------------------------------------------------

    def provider(self) -> GeolocationPlugin | None:
        """The `geolocation` plugin, which owns the database and the answers."""
        found = self.console.get_plugin("geolocation")
        return found if isinstance(found, GeolocationPlugin) else None

    def location_of(self, client: Client) -> Location | None:
        provider = self.provider()
        return None if provider is None else provider.location_of(client)

    # -- announcing ----------------------------------------------------------

    def on_located(self, event: Event) -> None:
        """Somebody has been placed. Announce it, unless the bot has only just started."""
        client = event.client
        place = event.data if isinstance(event.data, Location) else None
        if client is None or place is None or not self.settings.get("announce"):
            return
        silence = as_int(self.settings.get("startup_silence"), 300)
        if self.console.clock.now() - self._started_at < silence:
            log.debug("location: not announcing %s — still inside the startup silence", client.name)
            return
        described = place.describe()
        if not described:
            return
        self.console.say(self.message("location_connected", name=client.name, place=described))

    # -- commands ------------------------------------------------------------

    @command("locate", level=1)
    def cmd_locate(self, ctx: CommandContext) -> None:
        """locate <player> - where somebody is connecting from"""
        target = self._target(ctx)
        if target is None:
            return
        place = self.location_of(target)
        described = place.describe() if place is not None else ""
        if not described:
            ctx.reply(self.message("location_locate_failed", name=target.name))
            return
        ctx.reply(self.message("location_locate", name=target.name, place=described))

    @command("distance", level=1)
    def cmd_distance(self, ctx: CommandContext) -> None:
        """distance <player> - how far away somebody is, in kilometres"""
        target = self._target(ctx)
        if target is None:
            return
        if target is ctx.client:
            ctx.reply(self.message("location_distance_self", name=target.name))
            return
        mine, theirs = self.location_of(ctx.client), self.location_of(target)
        if mine is None or theirs is None:
            ctx.reply(self.message("location_distance_failed", name=target.name))
            return
        distance = distance_km(mine, theirs)
        if distance is None:
            # Both are placed, but the database is country-level and carries no coordinates. Worth
            # distinguishing from "who?" — it means "get a city database", not "try again".
            ctx.reply(self.message("location_distance_failed", name=target.name))
            return
        ctx.reply(self.message("location_distance", name=target.name, distance=distance))

    @command("isp", level=20)
    def cmd_isp(self, ctx: CommandContext) -> None:
        """isp <player> - whose network somebody is on"""
        target = self._target(ctx)
        if target is None:
            return
        place = self.location_of(target)
        if place is None or not place.isp:
            # Needs the ASN database, which is a separate file. The classic printed `--` here, which
            # reads as an answer.
            ctx.reply(self.message("location_isp_failed", name=target.name))
            return
        ctx.reply(self.message("location_isp", name=target.name, isp=place.isp))

    def _target(self, ctx: CommandContext) -> Client | None:
        """The player a command is about. All three take one and none of them defaults to you."""
        handle = ctx.args.strip()
        if not handle:
            ctx.reply(self.message("location_usage", command=ctx.command.name))
            return None
        return self.resolve_client(ctx, handle)


__all__ = [
    "DEFAULTS",
    "EARTH_RADIUS_KM",
    "MESSAGES",
    "LocationPlugin",
    "distance_km",
]
