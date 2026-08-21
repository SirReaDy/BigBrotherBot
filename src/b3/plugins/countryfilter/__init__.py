"""Decides which countries may play here.

A port of the classic `countryfilter` plugin. It is the bluntest thing in the tree — it removes people
for where they are, not for anything they did — and operators run it for real reasons: a server whose
players all share a timezone, a ping ceiling nobody enforces, a wave of nuisance from one range. The
lists and the two orderings are Apache's `Order allow,deny` model, which is what the classic modelled
it on and what its documentation pointed at.

Kept from the classic: both orderings, the allow and deny lists with `all` as a wildcard, the level that
is exempt, the name and address exemptions, the address blocklist, the two announcements and the
per-country silence list.

Changed, and the first is a fault that would empty a server:

* **A country code the database did not supply no longer matches every list.** The classic tested
  membership with `self.cf_deny_from.find(cc)` — `str.find`, on the raw config string. An empty needle
  is found at position 0 in any string, so a player whose record carried **no country code** was
  "in" the deny list and kicked, on any server with a deny list at all. Addresses with no country in
  the database are common (new ranges, satellite, carrier NAT), and the classic's own success event
  fires for a record that has a city and no code.
* **Country codes are compared, not searched for.** `find` also means the *format* of the config
  changes the behaviour: written without separators, `CNRU` matches `NR`; written in the wrong case,
  `all` is not `ALL`. The lists are parsed into sets of codes here, upper-cased, and compared for
  equality.
* **The startup silence applies to both announcements.** The classic checked `upTime() > 300` before
  announcing an *accepted* player and not before announcing a rejected one, so a bot restarting on a
  full server was silent about the arrivals it allowed and loud about the ones it removed.
* **No `VetoEvent`.** Kicking the player and then vetoing the event stopped every other plugin's
  handler for it, which is a plugin deciding what other plugins may know.
* **The shipped default no longer makes a blocklist a no-op.** Under `deny,allow` the allow list is
  applied *last* and overrides the deny list — Apache's documented behaviour, and faithfully
  implemented — and the classic shipped `allow_from: all`. So an operator adding a country to
  `deny_from` saw nothing happen, with nothing to tell them why. `allow_from` starts empty here, and
  writing that combination deliberately produces a warning naming it.

**A warning kept in one place, because it is the only real hole here.** `exempt_names` matches on a
*name*, and a name is whatever the player typed. Anybody who knows an exempt name can wear it and walk
past this filter. It is ported because operators use it, and the config says plainly that
`exempt_ips` or a group level is the exemption that cannot be spoofed.
"""

from __future__ import annotations

import logging

from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_int, strip_colors
from b3.domain.client import Client
from b3.plugins.geolocation import Location

log = logging.getLogger(__name__)

#: The wildcard, in either list. Apache's spelling, case-insensitive here where the classic's `find`
#: made `all` and `ALL` two different things.
WILDCARD = "ALL"

#: The two orderings, as Apache spells them and as the classic's config did.
DENY_ALLOW = "deny,allow"
ALLOW_DENY = "allow,deny"

DEFAULTS: dict[str, object] = {
    # `deny,allow` (the classic's default): allowed unless denied — a blocklist.
    # `allow,deny`: denied unless allowed — a whitelist, which is a very short guest list.
    "order": DENY_ALLOW,
    # Country codes (ISO two-letter), or `all`. Both empty means everybody is allowed under
    # `deny,allow`, which is the sane starting point.
    #
    # `allow_from` is **empty** here where the classic shipped `all`, and that is a fault fix rather
    # than a preference: under `deny,allow` the allow list is applied *last* and overrides the deny
    # list — Apache's documented behaviour, faithfully implemented — so the classic's own default
    # configuration made any `deny_from` an operator wrote a **no-op**. They would add a country,
    # restart, watch nothing happen, and have no way to tell why.
    "deny_from": [],
    "allow_from": [],
    # At or below this level a player is subject to the filter. 1 (`user`) is the classic's default, so
    # anybody who has registered is already exempt.
    "max_level": 1,
    # Exemptions. `exempt_ips` and the level above are the ones that cannot be faked; see the note in
    # this module's docstring about `exempt_names`.
    "exempt_names": [],
    "exempt_ips": [],
    # Addresses that may never connect whatever the country lists say.
    "blocked_ips": [],
    "announce_accepted": True,
    "announce_rejected": True,
    # Countries to say nothing about either way — for the ones that arrive constantly and would
    # otherwise fill the chat.
    "quiet_countries": [],
    # Say nothing at all for this many seconds after starting. A bot restarted mid-match places
    # everybody at once.
    "startup_silence": 300,
}

MESSAGES = {
    "countryfilter_accepted": "{name} is connecting from {country}",
    "countryfilter_rejected": "{name} was refused: this server does not accept players from "
    "{country}",
    "countryfilter_kick_reason": "this server does not accept connections from your country",
}


def codes(value: object) -> set[str]:
    """Read a list (or a comma-separated string) of country codes into upper-case set membership.

    Both spellings are accepted because operators have the second one already: the classic's config
    held `deny_from: CN, RU` as one string. Nothing is matched by substring, which is the whole point —
    see this module's docstring.
    """
    if isinstance(value, str):
        parts = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        if value is not None:
            log.error("countryfilter: %r is not a list of country codes; treated as empty", value)
        return set()
    return {part.strip().upper() for part in parts if part.strip()}


class CountryfilterPlugin(Plugin):
    """Removes players connecting from countries this server does not accept."""

    requires_plugins = ("admin", "geolocation")
    load_after = ("geolocation",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.deny_from: set[str] = set()
        self.allow_from: set[str] = set()
        self.quiet: set[str] = set()
        self._started_at = 0.0

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        order = str(self.settings.get("order") or DENY_ALLOW).strip().lower().replace(" ", "")
        if order not in (DENY_ALLOW, ALLOW_DENY):
            log.warning(
                "countryfilter: order %r is neither %r nor %r; using %r",
                order,
                DENY_ALLOW,
                ALLOW_DENY,
                DENY_ALLOW,
            )
            order = DENY_ALLOW
        self.settings["order"] = order
        self.deny_from = codes(self.settings.get("deny_from"))
        self.allow_from = codes(self.settings.get("allow_from"))
        self.quiet = codes(self.settings.get("quiet_countries"))

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self._started_at = self.console.clock.now()
        self.subscribe(EventType.CLIENT_GEOLOCATION_SUCCESS, self.on_located)
        if self.settings.get("order") == ALLOW_DENY and self.allow_from == {WILDCARD}:
            log.warning(
                "countryfilter: order is allow,deny with allow_from: all — that accepts everybody, "
                "which is probably not what a whitelist was meant to be"
            )
        if (
            self.settings.get("order") == DENY_ALLOW
            and WILDCARD in self.allow_from
            and self.deny_from
        ):
            # The trap the classic shipped as its default. Under deny,allow the allow list wins, so
            # this configuration denies nobody — said out loud, because the alternative is an operator
            # watching their blocklist do nothing.
            log.warning(
                "countryfilter: order is deny,allow with allow_from: all, which overrides the deny "
                "list — nobody will be refused. Remove `all` from allow_from, or use order "
                "allow,deny"
            )
        log.info(
            "countryfilter: %s; deny %s; allow %s",
            self.settings.get("order"),
            ", ".join(sorted(self.deny_from)) or "nothing",
            ", ".join(sorted(self.allow_from)) or "nothing",
        )

    # -- deciding ------------------------------------------------------------

    def on_located(self, event: Event) -> None:
        client = event.client
        place = event.data if isinstance(event.data, Location) else None
        if client is None or place is None:
            return
        country_code = place.country_code.strip().upper()
        country = place.country or country_code or "an unknown country"
        if self.may_connect(client, country_code):
            if self._announcing("announce_accepted", country_code):
                self.console.say(
                    self.message("countryfilter_accepted", name=client.name, country=country)
                )
            return
        log.info("countryfilter: refusing %s, connecting from %s", client.name, country or "?")
        if self._announcing("announce_rejected", country_code):
            self.console.say(
                self.message("countryfilter_rejected", name=client.name, country=country)
            )
        self.console.kick(client, reason=self.message("countryfilter_kick_reason"))

    def may_connect(self, client: Client, country_code: str) -> bool:
        """Whether this player is allowed on, and the order the exemptions are checked in.

        Level first, then the two exemption lists, then the address blocklist, then the country
        lists — the classic's order, which is the useful one: an exemption should beat a block.
        """
        if client.max_level() > as_int(self.settings.get("max_level"), 1):
            return True
        name = strip_colors(client.name).strip().lower()
        if name and name in {
            strip_colors(entry).strip().lower() for entry in self._list("exempt_names")
        }:
            # Spoofable by design; the config says so. Colour codes are stripped from both sides,
            # because `^1Bob` and `Bob` are one name to everybody in the server.
            return True
        address = client.ip.strip()
        if address and address in self._list("exempt_ips"):
            return True
        if address and address in self._list("blocked_ips"):
            return False
        return self.country_allowed(country_code)

    def _list(self, key: str) -> set[str]:
        """A configured list of plain strings, trimmed. A single string counts as one entry."""
        value = self.settings.get(key)
        if isinstance(value, str):
            entries = [value]
        elif isinstance(value, (list, tuple, set)):
            entries = [str(item) for item in value]
        else:
            if value is not None:
                log.error("countryfilter: %s must be a list; treated as empty", key)
            return set()
        return {entry.strip() for entry in entries if entry.strip()}

    def country_allowed(self, country_code: str) -> bool:
        """Apply the two lists in the configured order.

        A code the database did not supply is **in neither list**: it is not a country, so it cannot be
        the one that was denied. The classic searched for it with `str.find`, where an empty needle
        matches at position 0 — so an unplaceable address was in every list, and any deny list kicked
        those players.
        """
        denied = bool(country_code) and country_code in self.deny_from
        allowed = bool(country_code) and country_code in self.allow_from
        deny_all = WILDCARD in self.deny_from
        allow_all = WILDCARD in self.allow_from

        if self.settings.get("order") == ALLOW_DENY:
            result = allow_all or allowed
            if deny_all or denied:
                result = False
            return result
        result = not (deny_all or denied)
        if allow_all or allowed:
            result = True
        return result

    def _announcing(self, key: str, country_code: str) -> bool:
        if not self.settings.get(key):
            return False
        if country_code and (country_code in self.quiet or WILDCARD in self.quiet):
            return False
        silence = as_int(self.settings.get("startup_silence"), 300)
        return self.console.clock.now() - self._started_at >= silence


__all__ = [
    "ALLOW_DENY",
    "DEFAULTS",
    "DENY_ALLOW",
    "MESSAGES",
    "WILDCARD",
    "CountryfilterPlugin",
    "codes",
]
