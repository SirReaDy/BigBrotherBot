"""Applies ban lists somebody else maintains — a file, or a URL that is fetched and cached.

A port of the classic `banlist` plugin. Communities share ban lists (the big Call of Duty and Urban
Terror ones especially), and this is how a server honours one without its admins retyping it: on
authentication the player's IP, guid or PunkBuster id is looked up in each list, and a match is a kick
with the list named. Whitelists come first and win, so a list somebody else maintains can never remove
one of your own regulars.

Four list kinds, all of them the classic's:

* **ip** — one address per line, with a range convention: an entry ending `.0` bans the last octet's
  worth of addresses, `.0.0` two octets' worth, `.0.0.0` three. `force_ip_range` treats *any* entry
  as its /24. Trailing text after the address is ignored, which is what makes the published lists
  (`11.22.33.44:-1`, `11.22.33.44 some cheater`) usable as they are.
* **guid** — the engine's own player id.
* **pbid** — a PunkBuster id, for the titles that have one (see `b3.parsers.punkbuster`).
* **roc** — "Rules of Combat", an XML-ish list whose entries are `BannedID="..."`.

Kept from the classic: the range convention, whitelists-win, immunity by level, conditional downloads
(`If-Modified-Since`/`ETag`, so a list nobody has changed costs one round trip and no bytes), and the
hourly refresh at a random minute so a hundred servers do not all fetch the same list on the hour.

Changed, and each for a reason:

* **`thread.start_new_thread` is gone**, and it was the worst of the classic's threading: one raw
  thread per *authentication* to run a file read, plus more for every download. Lookups here are
  against content already in memory, and downloads are a scheduled coroutine that hands the blocking
  fetch to a worker.
* **A list that cannot be loaded is loud and inert**, rather than raising out of config-loading. The
  classic raised `BanlistException` per list and caught it around the loop, so a typo'd path left a
  plugin that looked healthy and enforced one list fewer than the operator thought.
* **The kick reason names the list.** It did classically too — but silently, with `silent=True`, so
  nobody on the server (including the admins) saw why a player vanished.
* **Comment lines are recognised as comments**, not merely missed. `//`, `#` and `;` entries are
  skipped when the file is parsed, so a commented-out entry cannot match by accident and the parsed
  count in the log is the number of *entries* rather than of lines.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from b3.core.commands import CommandContext, command
from b3.core.console import Console
from b3.core.events import Event, EventType
from b3.core.plugin import Plugin
from b3.core.util import as_level
from b3.domain.client import Client

log = logging.getLogger(__name__)

#: Sent with every request, so a list maintainer can see who is fetching it.
USER_AGENT = "b3ng banlist"

#: How long a download may take, and how often lists are refreshed.
DOWNLOAD_TIMEOUT = 20.0
REFRESH_MINUTES = 60

#: An entry line that is really a comment. The classic matched entries with `^<ip>`, so a commented
#: line happened not to match — true for `//1.2.3.4` and luck rather than intent.
COMMENT_PREFIXES = ("//", "#", ";", "--")

#: `BannedID="0123..."` — one entry of a Rules of Combat list.
ROC_ENTRY_RE = re.compile(r'BannedID\s*=\s*"(?P<id>[^"]+)"', re.IGNORECASE)

DEFAULTS: dict[str, object] = {
    # At or above this level a match is recorded as a notice rather than acted on. An admin who turns
    # up on somebody else's list should be told, not kicked by it.
    "immunity_level": 100,
    # Refresh lists that have a URL, hourly. Off means they are only fetched when missing, or when an
    # admin types `!banlistupdate`.
    "auto_update": True,
}

MESSAGES = {
    "banlist_kicked": "banned by {list} ({entry})",
    "banlist_immune": "{name} is on {list} but is immune at level {level}",
    "banlist_lists": "ban lists: {lists}",
    "banlist_none": "no ban lists are configured",
    "banlist_updated": "{list}: updated, {entries} entries",
    "banlist_unchanged": "{list}: unchanged",
    "banlist_failed": "{list}: update failed - {reason}",
    "banlist_checking": "checking {count} player(s) against {lists} list(s)",
}


@dataclass
class BanList:
    """One list: where it comes from, what kind of ids it holds, and what it currently holds.

    The content is parsed into a set once and looked up per player, where the classic re-read the file
    and ran a regex per player per list. That is not only slower: a regex built from the player's own
    id is a regex built from data off the network, and the escaping was the only thing between the two.
    """

    name: str
    kind: str  # ip | guid | pbid | roc
    path: Path | None = None
    url: str | None = None
    #: A whitelist is looked up first and stops the search; a match means "leave this player alone".
    whitelist: bool = False
    #: Treat every entry as its /24, not only entries ending in `.0`. `ip` lists only.
    force_ip_range: bool = False
    #: Entries, lower-cased. IP entries keep their dotted form; the range logic works on the text.
    entries: set[str] = field(default_factory=set)
    #: What the server said last time, so the next fetch can ask for changes only.
    last_modified: str | None = None
    etag: str | None = None
    loaded: bool = False

    def describe(self) -> str:
        kind = "whitelist" if self.whitelist else "banlist"
        where = self.url or (str(self.path) if self.path else "nowhere")
        return f"{self.name} ({self.kind} {kind}, {len(self.entries)} entries, from {where})"


def parse_entries(text: str, kind: str) -> set[str]:
    """Read a list file into the ids it holds.

    Comments (`//`, `#`, `;`, `--`) are dropped, and so is everything after the id on a line: the
    published lists carry a port (`11.22.33.44:-1`), a nickname or a reason there, and treating the
    whole line as the id would match nothing at all.
    """
    if kind == "roc":
        # An XML-ish file where the ids are attributes rather than lines.
        return {m["id"].strip().lower() for m in ROC_ENTRY_RE.finditer(text)}
    found: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(COMMENT_PREFIXES):
            continue
        # The id is the first token, and for an IP it ends at the first character that cannot be part
        # of one — `11.22.33.44:-1` and `11.22.33.44foo` both hold the same address.
        token = line.split()[0]
        if kind == "ip":
            match = re.match(r"\d{1,3}(?:\.\d{1,3}){3}", token)
            if match is None:
                continue
            token = match.group(0)
        found.add(token.strip().lower())
    return found


def ip_matches(ip: str, entries: set[str], *, force_range: bool) -> str | None:
    """The entry that bans ``ip``, or None. The classic's range convention, as a lookup.

    An entry is either the address itself or the address of a *range*: `11.22.33.0` covers
    `11.22.33.*`, `11.22.0.0` covers `11.22.*.*`, and `11.0.0.0` covers `11.*.*.*`. That convention
    is what the published lists use, and it is why an entry ending in a real `.0` host address cannot
    be expressed — a limitation of the format rather than of this code.

    ``force_range`` additionally treats every entry as its /24, for lists that are known to be
    per-network but were not written with the `.0` convention. Checked last, because it is the loosest
    thing here and an exact entry should be the one reported.
    """
    ip = ip.strip().lower()
    if not ip:
        return None
    if ip in entries:
        return ip
    octets = ip.split(".")
    if len(octets) != 4:
        return None
    for width in (3, 2, 1):
        candidate = ".".join(octets[:width] + ["0"] * (4 - width))
        if candidate in entries:
            return candidate
    if force_range:
        prefix = ".".join(octets[:3]) + "."
        for entry in entries:
            if entry.startswith(prefix):
                return entry
    return None


class BanlistPlugin(Plugin):
    """Kicks players who are on a list somebody else maintains."""

    requires_plugins = ("admin",)

    def __init__(self, console: Console, config: object | None = None) -> None:
        super().__init__(console, config)
        self.settings = dict(DEFAULTS)
        self.lists: list[BanList] = []

    # -- setup ---------------------------------------------------------------

    def on_load_config(self) -> None:
        config = self.config if isinstance(self.config, dict) else {}
        self.settings = {**DEFAULTS, **(config.get("settings") or {})}
        self.lists = []
        for entry in config.get("lists") or []:
            built = self._build_list(entry)
            if built is not None:
                self.lists.append(built)
        if not self.lists:
            log.warning("banlist: no lists configured, so this plugin will do nothing")

    def _build_list(self, entry: object) -> BanList | None:
        """One `lists:` entry, or None with the reason said out loud.

        Refused rather than raised: the classic raised per list and caught it around the loop, which
        left a plugin that looked healthy while enforcing one list fewer than the operator believed.
        """
        if not isinstance(entry, dict):
            log.error("banlist: a list entry must be a mapping, not %r", entry)
            return None
        kind = str(entry.get("kind") or "").strip().lower()
        if kind not in ("ip", "guid", "pbid", "roc"):
            log.error(
                "banlist: %r is not a list kind — use ip, guid, pbid or roc", entry.get("kind")
            )
            return None
        name = str(entry.get("name") or "").strip()
        raw_path, url = entry.get("file"), entry.get("url")
        if not raw_path and not url:
            log.error("banlist: list %r names neither a file nor a url", name or kind)
            return None
        if not name:
            name = str(raw_path or url)
        built = BanList(
            name=name,
            kind=kind,
            path=self._resolve(str(raw_path)) if raw_path else None,
            url=str(url) if url else None,
            whitelist=bool(entry.get("whitelist")),
            force_ip_range=bool(entry.get("force_ip_range")),
        )
        return built

    def _resolve(self, value: str) -> Path:
        """Expand the `@b3` / `@conf` / `@home` tokens the rest of the config accepts.

        `@conf` is the directory the main config was loaded from, which the runtime knows as
        `config_path`; a bot built in a test has none, and then a relative path is just a relative
        path.
        """
        from b3.config.loader import resolve_path_token

        config_path = getattr(self.console, "config_path", None)
        conf_dir = Path(config_path).parent if config_path else None
        return Path(resolve_path_token(value, conf_dir))

    def on_startup(self) -> None:
        self.register_messages(MESSAGES)
        self.subscribe(EventType.CLIENT_AUTH, self.on_auth)
        # **And when their address turns up**, which on the Call of Duty and Quake 3 engines is not at
        # authentication: their log lines carry no IP, and it is the status poll moments later that
        # resolves one. An IP list checked only at auth would match nobody at all on those titles —
        # silently, which is how the classic's IP lists came to depend on somebody typing
        # `!banlistcheck`.
        self.subscribe(EventType.CLIENT_UPDATE, self.on_auth)
        for entry in self.lists:
            self.load(entry)
        if self.settings.get("auto_update") and any(entry.url for entry in self.lists):
            # A random minute rather than the top of the hour, as the classic did: a hundred servers
            # honouring the same list should not all fetch it in the same second.
            minute = self._staggered_minute()
            self.schedule(self._refresh_all, minute=str(minute), name="BanlistPlugin.refresh")
            log.info("banlist: lists with a url refresh at %d minutes past each hour", minute)

    def _staggered_minute(self) -> int:
        """A stable per-server minute, from the bot's own name rather than from a random number.

        Random would re-roll at every restart, which is the same thundering herd spread over restarts
        instead of over the hour. Hashing something stable spreads servers apart and keeps each one's
        schedule predictable — which matters when an operator is reading a log and asking why a fetch
        happened at 07:43.
        """
        seed = getattr(getattr(self.console, "config", None), "bot", None)
        name = str(getattr(seed, "name", "") or "b3")
        return sum(name.encode()) % REFRESH_MINUTES

    # -- loading -------------------------------------------------------------

    def load(self, entry: BanList) -> bool:
        """Read a list into memory, fetching it first if it is missing and has a URL."""
        if entry.path is not None and not entry.path.is_file() and entry.url:
            log.info("banlist: %s is not on disk yet; fetching it", entry.name)
            self._download(entry)
        if entry.path is None:
            return entry.loaded  # url-only list, already in memory from the fetch
        try:
            text = entry.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # Loud and inert, and it stays in the list so `!banlistinfo` shows it as empty rather
            # than the operator wondering which of their lists is missing.
            log.error("banlist: cannot read %s (%s); it will match nobody", entry.path, exc)
            entry.entries = set()
            entry.loaded = False
            return False
        entry.entries = parse_entries(text, entry.kind)
        entry.loaded = True
        log.info("banlist: loaded %s", entry.describe())
        return True

    def _download(self, entry: BanList) -> str | None:
        """Fetch a list over HTTP. Returns None on success, or why it failed.

        Conditional: the server is told what we already have (`If-Modified-Since`, `If-None-Match`),
        so a list nobody has changed costs a round trip and no bytes. Blocking, and called through a
        worker thread — see `_refresh_all`.
        """
        from urllib.error import HTTPError, URLError
        from urllib.request import Request, urlopen

        if not entry.url:
            return "no url"
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        if entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        try:
            request = Request(entry.url, headers=headers)  # noqa: S310 - operator-configured url
            with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310
                payload = response.read()
                if response.headers.get("Content-Encoding", "") == "gzip":
                    import gzip

                    payload = gzip.decompress(payload)
                entry.last_modified = response.headers.get("Last-Modified")
                entry.etag = response.headers.get("ETag")
        except HTTPError as exc:
            if exc.code == 304:
                return None  # unchanged, which is a success
            entry.last_modified = entry.etag = None
            return f"HTTP {exc.code}"
        except (URLError, OSError) as exc:
            # Cleared so the next attempt asks for the whole thing: a conditional request against a
            # cached copy we failed to write would be answered "unchanged" forever.
            entry.last_modified = entry.etag = None
            return str(exc)
        text = payload.decode("utf-8", errors="replace")
        entry.entries = parse_entries(text, entry.kind)
        entry.loaded = True
        if entry.path is not None:
            try:
                entry.path.parent.mkdir(parents=True, exist_ok=True)
                entry.path.write_text(text, encoding="utf-8")
            except OSError as exc:
                # The list is in memory and working; only the cache failed. Worth saying, because the
                # next restart will fetch again and an operator watching disk usage should know why.
                log.warning("banlist: fetched %s but could not cache it (%s)", entry.name, exc)
        return None

    async def _refresh_all(self) -> None:
        """The hourly refresh. Blocking fetches go to a worker so the event loop keeps running."""
        for entry in self.lists:
            if not entry.url:
                continue
            failure = await asyncio.to_thread(self._download, entry)
            if failure is not None:
                log.warning("banlist: %s did not update (%s)", entry.name, failure)
                continue
            log.info("banlist: %s refreshed, %d entries", entry.name, len(entry.entries))
        self.check_everybody()

    # -- checking ------------------------------------------------------------

    def on_auth(self, event: Event) -> None:
        """Check a player when the bot learns who they are, or learns more about them.

        Synchronous, unlike the classic's thread-per-authentication: the lists are already in memory,
        so this is a handful of set lookups. Starting a thread to do that was more expensive than the
        work itself.

        The same identity is not checked twice. Both events can fire repeatedly for one player — a
        roster poll every five minutes publishes an update whenever anything changed — and a player
        who is not on a list should not be looked up forever, while one who is has already been
        kicked.
        """
        client = event.client
        if client is None or not self.lists:
            return
        identity = (client.ip, client.guid, client.pbid)
        if client.get_var(self, "checked") == identity:
            return
        client.set_var(self, "checked", identity)
        self.check(client)

    def check(self, client: Client) -> bool:
        """Look this player up in every list. True if they were acted on.

        Whitelists first and they win outright, which is the point of them: a list maintained by
        somebody else must not be able to remove one of your own regulars, and the only way to
        guarantee that is to stop looking once a whitelist matches.
        """
        for entry in self.lists:
            if not entry.whitelist:
                continue
            matched = self.matches(entry, client)
            if matched is not None:
                log.info("banlist: %s is on whitelist %s (%s)", client.name, entry.name, matched)
                return False
        for entry in self.lists:
            if entry.whitelist:
                continue
            matched = self.matches(entry, client)
            if matched is None:
                continue
            immunity = as_level(self.settings.get("immunity_level"), 100)
            level = client.max_level()
            if level >= immunity:
                # Recorded rather than acted on, and recorded where an admin can find it: somebody
                # trusted turning up on a shared list is worth knowing about either way.
                text = self.message(
                    "banlist_immune", name=client.name, list=entry.name, level=level
                )
                log.info("banlist: %s", text)
                self.console.notice(client, reason=text)
                return False
            self.console.kick(
                client, reason=self.message("banlist_kicked", list=entry.name, entry=matched)
            )
            log.info(
                "banlist: kicked %s — %s matches %s in %s",
                client.name,
                matched,
                entry.kind,
                entry.name,
            )
            return True
        return False

    def matches(self, entry: BanList, client: Client) -> str | None:
        """The entry of ``entry`` that this player matches, or None."""
        if entry.kind == "ip":
            if not client.ip:
                return None
            return ip_matches(client.ip, entry.entries, force_range=entry.force_ip_range)
        if entry.kind == "pbid":
            value = (client.pbid or "").strip().lower()
        else:  # guid, and roc which is a list of guids in another shape
            value = (client.guid or "").strip().lower()
        if not value:
            return None
        return value if value in entry.entries else None

    def check_everybody(self) -> int:
        """Check every connected player. Used after an update, and by `!banlistcheck`."""
        checked = 0
        for client in list(self.console.clients.connected()):
            if self.check(client):
                checked += 1
        return checked

    # -- commands ------------------------------------------------------------

    @command(level=20, alias="blinfo")
    def cmd_banlistinfo(self, ctx: CommandContext) -> None:
        """banlistinfo - what lists are loaded, where from, and how many entries each holds"""
        if not self.lists:
            ctx.reply(self.message("banlist_none"))
            return
        ctx.reply(self.message("banlist_lists", lists="; ".join(e.describe() for e in self.lists)))

    @command(level=80, alias="blupdate")
    def cmd_banlistupdate(self, ctx: CommandContext) -> None:
        """banlistupdate - fetch every list that has a url, now"""
        remote = [e for e in self.lists if e.url]
        if not remote:
            ctx.reply(self.message("banlist_none"))
            return
        for entry in remote:
            before = len(entry.entries)
            failure = self._download(entry)
            if failure is not None:
                ctx.reply(self.message("banlist_failed", list=entry.name, reason=failure))
            elif len(entry.entries) == before:
                ctx.reply(self.message("banlist_unchanged", list=entry.name))
            else:
                ctx.reply(
                    self.message("banlist_updated", list=entry.name, entries=len(entry.entries))
                )
        self.check_everybody()

    @command(level=20, alias="blcheck")
    def cmd_banlistcheck(self, ctx: CommandContext) -> None:
        """banlistcheck - check everybody on the server against the lists again"""
        players = len(self.console.clients.connected())
        ctx.reply(self.message("banlist_checking", count=players, lists=len(self.lists)))
        self.check_everybody()


__all__ = [
    "DEFAULTS",
    "MESSAGES",
    "ROC_ENTRY_RE",
    "BanList",
    "BanlistPlugin",
    "ip_matches",
    "parse_entries",
]
