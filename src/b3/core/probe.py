"""`b3 probe` — what does this server actually say, and do our assumptions fit it?

Every unverified claim this bot makes about a game is the same question: the exact shape of a status
table, whether a mod's command exists, which log lines a title really writes. Answering it has meant
either reading regexes or playing twenty questions with an operator over chat. This turns it into one
command whose output can be pasted back verbatim.

**It is not `b3 doctor`.** Doctor is a pre-flight checklist — one line per check, pass or fail,
deliberately hiding the raw data because somebody starting a bot for the first time does not want it.
This is a microscope: the reply as it arrived, which candidate pattern won, what came out of it, and
which log lines matched no handler at all. That last section is the direct answer to "is our grammar
complete for this title?", and it is how a gap like Urban Terror's missing weapons gets found
deliberately rather than by accident years later.

Two properties make it safe to hand to a stranger with a live server:

* **Strictly read-only.** It asks for the status table, reads one cvar, and reads the tail of the log.
  There is no path from here to a kick, a ban or a line of chat. It also never builds the Altitude
  command-file client, because *opening* that clears the file — a side effect a diagnostic has no
  business having.
* **`--redact`**, because the natural next step is pasting the output into a forum thread or an issue,
  and player names, addresses and ids should not leak just because somebody helped us. The RCON
  password is never printed either way.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from b3.config.schema import Config
from b3.parsers import games
from b3.parsers.profile import GameProfile
from b3.parsers.status import parse_status, unparsed_rows

#: How much of the log to look at when nothing else is said. Enough to cover a round on a busy
#: server, small enough to paste.
DEFAULT_LINES = 200

#: How many bytes of a log file to read for that. A local log can be hundreds of megabytes, and a
#: diagnostic that loads all of it to look at the end is its own outage.
TAIL_BYTES = 262_144

#: How many unmatched log lines to show. The point is to show *what kind*, not to dump the log.
MAX_UNMATCHED_SHOWN = 15

_IP_RE = re.compile(r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b")
#: A long run of hex or digits: a guid, a Steam64 id, a vapor id. Deliberately *not* matching
#: anything with a letter past `f` or an underscore, so that `mp_backlot` and `ConquestLarge0`
#: survive redaction — a report with the map names blanked out is much less use.
_ID_RE = re.compile(r"\b(?:[0-9a-f]{8,}|[0-9]{8,})\b", re.IGNORECASE)


#: How wide a section heading rule is drawn.
_RULE_WIDTH = 78


class QueryClient(Protocol):
    """The whole of what probing needs from a client: ask something, read the answer.

    Narrower than :class:`b3.runtime.bot.RconClient` on purpose. A type that cannot express `kick`
    is a stronger guarantee of "read-only" than a comment saying so.
    """

    def command(self, cmd: str) -> str: ...


#: A function that masks anything in a report line that should not be pasted in public.
Scrub = Callable[[str], str]


def _rule(title: str) -> str:
    """A section heading, ruled to a fixed width so the report lines up when pasted.

    **ASCII only, here and throughout the report.** A Windows console is often still cp1252, where
    printing a box-drawing character raises `UnicodeEncodeError` — and a diagnostic that crashes while
    describing somebody's server is worse than no diagnostic. It also survives being pasted into a
    forum, an issue tracker or a chat window without turning into question marks.
    """
    return f"-- {title} " + "-" * max(0, _RULE_WIDTH - len(title) - 4)


def redact(text: str) -> str:
    """Mask addresses and ids in a line of report output."""
    text = _IP_RE.sub("x.x.x.x", text)
    return _ID_RE.sub(lambda m: m.group(0)[:4] + "...", text)


def run_probe(
    config: Config,
    conf_dir: Path | None = None,
    *,
    lines: int = DEFAULT_LINES,
    cvar: str = "sv_maxclients",
    hide: bool = False,
    rcon_factory: Callable[[], QueryClient] | None = None,
) -> list[str]:
    """Probe the configured server and return the report, one string per line.

    Returns the report rather than printing it, so the CLI owns the output and a test can read it.
    """
    profile = games.profile_for(config.server.game)
    out: list[str] = []
    scrub: Scrub = redact if hide else (lambda text: text)

    out.append(f"game: {config.server.game}   (family: {profile.family})")
    out.append(f"server: {config.server.host}:{config.server.port}")
    if hide:
        out.append("(--redact: addresses and ids are masked)")
    out.append("")

    out.extend(_status_section(config, profile, scrub, rcon_factory))
    out.append("")
    out.extend(_cvar_section(config, profile, cvar, scrub, rcon_factory))
    out.append("")
    out.extend(_log_section(config, profile, lines, scrub))
    return out


# -- what the server says about its players ------------------------------------------------------


def _status_section(
    config: Config,
    profile: GameProfile,
    scrub: Scrub,
    rcon_factory: Callable[[], QueryClient] | None,
) -> list[str]:
    from b3.parsers.games import FILE_RCON_FAMILIES, PUSH_FAMILIES

    out = [_rule("status")]
    if not profile.status_commands:
        out.append(f"nothing to ask: a {profile.name} server cannot be queried at all.")
        out.append("Its roster comes from the log, so the log section below is the whole picture.")
        return out
    if profile.family in FILE_RCON_FAMILIES:
        # Unreachable while the family above has no status commands, and kept as a guard: building
        # that client *clears* the command file, which a read-only command must never do.
        out.append("skipped: this family is commanded through a file, which probing must not touch.")
        return out
    if profile.family in PUSH_FAMILIES and rcon_factory is None:
        out.append(f"skipped: a {profile.name} server pushes its events down the rcon connection,")
        out.append("and probing it needs a login handshake. Use `b3 doctor` for that check.")
        return out

    client = _open_rcon(config, rcon_factory)
    if client is None:
        out.append("skipped: no rcon_password set, so the server cannot be asked anything.")
        return out
    try:
        # Every command, not just until one answers — unlike the runtime, which stops at the first
        # that yields players. Here the *comparison* is the diagnostic: if `b3status` lists five
        # players and plain `status` lists four, the b3hide mod is installed and hiding one, which is
        # exactly the question about CoD4X that no amount of reading forks could settle.
        for command in profile.status_commands:
            out.extend(_one_status_command(client, profile, command, scrub))
            out.append("")
        out.pop()  # the trailing blank line
    except Exception as exc:  # noqa: BLE001 - a probe reports failures, it does not raise them
        out.append(f"FAILED: {type(exc).__name__}: {exc}")
    finally:
        closer = getattr(client, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # pragma: no cover - nothing useful to do
                pass
    return out


def _one_status_command(
    client: QueryClient, profile: GameProfile, command: str, scrub: Scrub
) -> list[str]:
    out = [f"asked: {command}"]
    try:
        raw = client.command(command)
    except Exception as exc:  # noqa: BLE001
        return out + [f"  no reply: {type(exc).__name__}: {exc}"]

    if not raw.strip():
        return out + ["  (empty reply: the server does not know this command, or said nothing)"]

    out.append(f"raw reply ({len(raw)} bytes):")
    out.extend(f"| {scrub(line)}" for line in raw.splitlines())

    # Which candidate won, stated per candidate rather than only for the winner: "the strict shape
    # matched 0 rows and the one without a steam column matched 4" is the interesting sentence when a
    # table has changed. Each is named by the *columns* it expects rather than by an excerpt of its
    # regex — every one of these patterns starts `^\s*(?P<slot>…`, so a prefix distinguishes nothing.
    for index, pattern in enumerate(profile.status_patterns or (), start=1):
        hits = sum(1 for line in raw.splitlines() if pattern.match(line.strip()))
        columns = " ".join(pattern.groupindex) or "(none)"
        out.append(f"  candidate {index}: {hits:>3} row(s) matched   columns: {columns}")

    _map, players = parse_status(raw, profile.status_patterns, profile.identity_field)
    out.append(f"map: {_map or '(not stated)'}")
    if not players:
        out.append("parsed 0 players.")
        rows = unparsed_rows(raw, profile.status_patterns)
        if rows:
            out.append(
                f"  {len(rows)} row(s) look like players but match no known pattern. "
                f"This is what needs a new status pattern:"
            )
            out.extend(f"  ! {scrub(row)}" for row in rows)
        else:
            out.append("  and no row looked like a player, so the server is probably empty.")
        return out

    out.append(f"parsed {len(players)} player(s):")
    for p in players:
        out.append(
            scrub(
                f"  cid={p.cid} name={p.name!r} guid={p.guid or '(none)'}"
                f" ip={p.ip or '(none)'}:{p.port} ping={p.ping} score={p.score}"
            )
        )
    if profile.identity_field != "guid":
        out.append(f"  (identity is the `{profile.identity_field}` column for this title)")
    return out


# -- reading a cvar ------------------------------------------------------------------------------


def _cvar_section(
    config: Config,
    profile: GameProfile,
    cvar: str,
    scrub: Scrub,
    rcon_factory: Callable[[], QueryClient] | None,
) -> list[str]:
    from b3.parsers.games import FILE_RCON_FAMILIES, PUSH_FAMILIES
    from b3.parsers.status import parse_cvar

    out = [_rule("cvar read")]
    if profile.family in PUSH_FAMILIES or profile.family in FILE_RCON_FAMILIES:
        out.append(f"skipped: {profile.name} has no cvars to read this way.")
        return out
    client = _open_rcon(config, rcon_factory)
    if client is None:
        out.append("skipped: no rcon_password set.")
        return out

    asked = profile.get_cvar_template % {"name": cvar}
    try:
        raw = client.command(asked)
    except Exception as exc:  # noqa: BLE001
        return out + [f"asked: {asked}", f"  no reply: {type(exc).__name__}: {exc}"]
    finally:
        closer = getattr(client, "close", None)
        if closer is not None:
            try:
                closer()
            except Exception:  # pragma: no cover
                pass

    out.append(f"asked: {asked}")
    out.append(f"reply: {scrub(raw.strip()) or '(empty)'}")
    value = parse_cvar(cvar, raw)
    if value is None:
        out.append(f"  parsed: NOTHING. The reply does not match the shape `{cvar}` was expected in.")
        out.append("  If the server clearly answered, the reply form is what needs recording.")
    else:
        out.append(f"  parsed: {value}")
    return out


def _open_rcon(
    config: Config, rcon_factory: Callable[[], QueryClient] | None
) -> QueryClient | None:
    """A client for read-only queries, or None when there is nothing to query with."""
    if rcon_factory is not None:
        return rcon_factory()
    if not config.server.rcon_password:
        return None
    from b3.net.rcon import Rcon, UdpRconTransport, dialect_for

    profile = games.profile_for(config.server.game)
    if profile.family in games.TCP_RCON_FAMILIES:
        # A stateful session rather than connectionless datagrams, so it has to be opened and logged
        # in to before anything can be asked. Still read-only: everything probe sends is a question.
        from b3.net.source import SourceRconClient

        client = SourceRconClient(
            config.server.host,
            config.server.port,
            config.server.rcon_password,
            timeout=max(config.server.rcon_timeout, 2.0),
            encoding="utf-8",
        )
        client.open()
        return client

    transport = UdpRconTransport(
        config.server.host, config.server.port, timeout=config.server.rcon_timeout
    )
    return Rcon(
        transport,
        password=config.server.rcon_password,
        encoding=config.server.encoding,
        dialect=dialect_for(profile.rcon_dialect),
    )


# -- which log lines this bot understands --------------------------------------------------------


def _log_section(config: Config, profile: GameProfile, limit: int, scrub: Scrub) -> list[str]:
    from b3.parsers.games import PUSH_FAMILIES

    out = [_rule(f"game log (last {limit} lines)")]
    if profile.family in PUSH_FAMILIES:
        out.append(f"skipped: a {profile.name} server has no log file: events arrive over rcon.")
        return out

    try:
        tail = _tail_lines(config, limit)
    except Exception as exc:  # noqa: BLE001
        return out + [f"could not read {config.server.game_log}: {type(exc).__name__}: {exc}"]

    if not tail:
        out.append(f"{config.server.game_log} yielded no lines (empty, or nothing written yet).")
        return out

    parser = games.parser_for(profile, None, config.server.port)
    by_handler: dict[str, int] = {}
    unmatched: list[str] = []
    for line in tail:
        handler = parser.handler_for(line)
        if handler is None:
            unmatched.append(line)
        else:
            by_handler[handler] = by_handler.get(handler, 0) + 1

    matched = sum(by_handler.values())
    out.append(f"read {len(tail)} line(s) from {config.server.game_log}")
    out.append(f"matched   {matched}")
    for handler, count in sorted(by_handler.items(), key=lambda kv: -kv[1]):
        out.append(f"  {count:>5}  {handler}")
    out.append(f"UNMATCHED {len(unmatched)}")
    if unmatched:
        out.append("  these are the interesting ones. A line this bot does not understand looks")
        out.append("  exactly like a line the server never wrote:")
        for line in unmatched[:MAX_UNMATCHED_SHOWN]:
            out.append(f"  ! {scrub(line)}")
        if len(unmatched) > MAX_UNMATCHED_SHOWN:
            out.append(f"  ... and {len(unmatched) - MAX_UNMATCHED_SHOWN} more")
    return out


def _tail_lines(config: Config, limit: int) -> list[str]:
    """The last ``limit`` lines of the game log, local or remote.

    A local file is read from the end, because loading a 500 MB log to look at the last page of it is
    the sort of thing a diagnostic should not do to a live server. A remote source is asked to skip
    ahead instead, which is the same trick the live tail uses on a first poll.
    """
    spec = config.server.game_log
    from b3.net.logsource import URL_SCHEMES, create_log_source

    scheme = spec.split("://", 1)[0].lower() if "://" in spec else ""
    if scheme in URL_SCHEMES and scheme != "file":
        source = create_log_source(
            spec,
            encoding=config.server.encoding,
            from_start=True,
            timeout=config.server.log_timeout,
            max_gap=TAIL_BYTES,
        )
        source.open()
        try:
            return source.read_lines()[-limit:]
        finally:
            source.close()

    path = Path(spec)
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        data = handle.read()
    text = data.decode(config.server.encoding, errors="replace")
    tail = text.splitlines()
    if size > TAIL_BYTES and tail:
        tail = tail[1:]  # the first line is a fragment of one we started in the middle of
    return [line for line in tail if line.strip()][-limit:]


__all__ = ["DEFAULT_LINES", "redact", "run_probe"]
